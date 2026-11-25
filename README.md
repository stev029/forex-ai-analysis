# forex-agents

Multi-timeframe FX analysis cascade: 4 agents chained in LangGraph, Postgres (serverless) memory,
Telegram as the control surface. Runs inside `proot-distro` Ubuntu on Termux.

## Cascade

Ask for `m1` and the graph resolves:

| stage | timeframe | job |
|---|---|---|
| 1 trend | base + 2 (`m15`) | directional bias + strength |
| 2 validation | base + 1 (`m5`) | confirm/contradict, phase, invalidation |
| 3 critical | base (`m1`) | support/resistance, order blocks, entry trigger |
| 4 decision | — | BUY / SELL / NO_TRADE + entry, SL, TP, RR |

Timeframes are resolved by `src/forex_agents/timeframes.py:cascade()`; ladder is
`m1 m5 m15 m30 h1 h4 d1 w1`, so `m5 → m30/m15/m5`, `h1 → d1/h4/h1` (clamped at the top).

## Model per-tahap

Tiap tahap punya model sendiri (`config.py:Settings.model_for()`), supaya tugas ringan tidak
membakar model mahal dan tugas berat tetap akurat.

| tahap | sifat tugas | env | contoh |
|---|---|---|---|
| trend | ringan: klasifikasi bias + kekuatan | `LLM_MODEL_TREND` | `gemini/gemini-3.5-flash-lite` |
| validation | ringan: setuju/tidak + fase | `LLM_MODEL_VALIDATION` | `gemini/gemini-3.5-flash-lite` |
| critical | ringan: zona support/resistance | `LLM_MODEL_CRITICAL` | `gemini/gemini-3.5-flash-lite` |
| decision | berat: entry, SL, TP, RR, veto | `LLM_MODEL_DECISION` | `abacus/gpt-6-sol` |

Kosong ⇒ jatuh ke `LLM_MODEL`. Rantai fallback (`LLM_FALLBACK_<TAHAP>`, lalu `LLM_FALLBACKS`)
dicoba berurutan kalau model utama error — berguna saat kuota provider habis. Log mencatat
`model X gagal (...), fallback ke Y`. Model yang benar-benar dipakai tiap tahap tampil di akhir
pesan Telegram (`model: ...`) dan di baris `trace`.

## Timeframe otomatis (`auto`)

`/analyze XAUUSD auto` (atau `--tf auto`, `/schedule add XAUUSD auto`) menyerahkan pemilihan TF
ke `src/forex_agents/autotf.py`. Semua TF `m1…w1` diskor 0-100 secara deterministik, lalu TF
skor tertinggi jadi acuan entry dan cascade-nya (`+2 / +1 / base`) dipakai untuk 4 tahap.

| komponen | bobot | syarat |
|---|---|---|
| `trend` | 25 | EMA20/50/200 tidak `mixed` |
| `area` | 25 | harga <= 0.5 x ATR dari swing terdekat |
| `momentum` | 20 | `abs(RSI14 - 50) / 25` |
| `wick` | 15 | candle terakhir punya wick penolakan >= 1.5x body |
| `exhaust` | 15 | RSI ekstrem + MACD decay + >= 4 candle searah |
| `fresh` | 10 | bar terakhir <= 2 x panjang TF |
| `vol` | 10 | ATR >= 3 pips |

Ambang `IDEAL_SCORE = 60`, `STRONG_SCORE = 75`. Bila >= 2 TF lolos ambang, hasilnya dilaporkan
sebagai saran multi-timeframe (`auto_tf.multi = True`) dan decision node wajib menyebutnya di
`reasons`. Bila tidak ada TF yang lolos, decision node memveto BUY/SELL menjadi `NO_TRADE`
(`auto-tf: tidak ada timeframe yang lolos ambang skor 60`). Pasar datar (ATR/variasi nol)
selalu skor 0.

Blok hasil: `auto_tf = {primary, trend_tf, validation_tf, critical_tf, ideal, multi, ranked,
skipped, threshold}`; render menampilkannya sebagai seksi `<b>5) Timeframe otomatis</b>` dan
menambah tag `· auto-tf` di header. `/tf auto` menjelaskan mode ini; `/memory` menolak TF auto
karena riwayat terikat TF eksplisit. Cron untuk TF auto memakai ritme `m15` (15 menit) dan
target `m30/m15`.

## Saran entry (BUY & SELL + SL/TP/RR)

`src/forex_agents/advice.py:build()` selalu menghitung dua skenario lengkap, termasuk saat
keputusan berakhir `NO_TRADE`, supaya trader tetap punya angka acuan. Semua level berasal dari
struktur (swing high/low, zona kritis, `range_high/low`) + buffer `0.5 x ATR`, bukan karangan LLM.

- `_nearest(values, price, side)` memilih level terdekat di sisi yang benar: di bawah harga untuk
  BUY, di atas harga untuk SELL. Anchor yang lebih jauh dari `3 x ATR` dibuang supaya stop tidak
  selebar 698 pips pada XAUUSD.
- TP diambil dari zona lawan terdekat; bila RR-nya di bawah `min_rr`, TP digeser ke
  `FALLBACK_RR = 2.0 x risk`.
- Sisi yang searah `trend.bias` jadi `primary`, sisanya `alt`. Bias `range` → pilih RR terbesar.
- `valid = rr >= min_rr and risk <= 3 x ATR`; `note` menjelaskan pelanggarannya.

Blok `advice = {ok, bias, aligned, atr_pips, buffer, levels, primary, alt, buy, sell, min_rr}`
dirender sebagai seksi `<b>5) Saran entry</b>` (nomor seksi bergeser otomatis bila mode scalp
atau auto-tf aktif). Prompt decision node juga menerima ringkasannya dengan aturan: entry/SL/TP
usulan LLM harus dalam `+/-1 ATR` dari angka saran.

Indicators are computed deterministically (`indicators.py`) and the LLM only receives a compact
numeric snapshot — it never sees raw candles and cannot invent prices. The decision node then
applies hard guardrails (RR >= `MIN_RR`, stop <= 3x ATR, SL/TP on the correct side of entry);
any veto downgrades the call to `NO_TRADE`.

## Mode scalping (bisa di-switch kapan pun)

Dua teknik, dipilih per chat lewat `/mode` atau per panggilan lewat `--mode`:

| mode | konteks | acuan area | entry |
|---|---|---|---|
| `cascade` (default) | base+2 / base+1 | TF dasar | TF dasar |
| `scalp` | `h1 h4 d1 w1` (latar saja) | `m15` + `m30` | `m1` + `m5` |

`scalp.py` menghitung semuanya deterministik (tanpa LLM) supaya level tidak dikarang:

- `potential_area(df_area, df_confirm, price)` — swing `m15`/`m30` di-cluster dengan toleransi
  `0.75 x ATR`, menghasilkan `support`/`resistance`/`active`, `touches`, dan `inside`.
- `wick_rejection(df, bars=3)` — wick >= `1.5x` body dengan close position di sisi berlawanan.
- `push(df, lookback=10)` — arah dorongan terakhir, `net`, `consecutive`, `decay` (hist MACD mengecil).
- `exhaustion(df)` — skor 0-3: RSI ekstrem, hist mengecil, >= 4 candle searah.
- `ideal_area(...)` — gerbang keras, semua harus benar: `harga_di_area`, `area_teruji` (>= 2 sentuhan),
  `wick_penolakan`, `wick_searah_area`, `kelelahan` (skor >= 2).
- `plan(...)` — entry di harga sekarang, SL di balik wick + `0.5 x ATR`, TP di zona lawan, RR dihitung.

`decision_node` menambah veto saat mode `scalp`: `scalping: area ideal belum terbentuk (...)` dan
`scalping: area menuntut BUY/SELL tapi agen memilih ...`. Jadi entry hanya terjadi kalau area ideal
sudah terbentuk — sesuai prinsip "harga tidak naik/turun dengan mudah".

Konteks `h1/h4/d1/w1` tetap dianalisis agen tren, tetapi prompt keputusan menandainya sebagai latar,
bukan acuan utama. Output menampilkan seksi `<b>5) Scalping</b>` berisi area potensial, centang gerbang,
dan rencana entry.

```bash
run.sh cli XAUUSD m1 --mode scalp
```

Di Telegram:

```
/mode            # lihat mode aktif
/mode scalp      # switch ke scalping
/mode cascade    # balik ke cascade
```

Mode disimpan di tabel `chat_settings` sehingga `/schedule` ikut memakai mode terakhir chat tersebut.

## Layout

```
src/forex_agents/
  config.py      pydantic-settings env loading
  timeframes.py  ladder, aliases, cascade(), yfinance interval map
  autotf.py      skor TF 0-100, pilih TF entry, saran multi-timeframe
  advice.py      saran BUY/SELL + SL/TP/RR deterministik dari struktur
  market.py      YFinanceProvider + get_provider() dispatch
  market_tv.py     TradingViewProvider (websocket tradingview-api)
  indicators.py  EMA/ATR/RSI/MACD, swing points, structure, snapshot()
  schemas.py     Pydantic contracts per stage
  llm.py         ChatOpenAI factory + structured() with JSON fallback
  state.py       AnalysisState TypedDict (reducers for errors/trace)
  nodes.py       fetch / trend / validation / critical / decision
  graph.py       StateGraph wiring + data gate
  memory.py      AsyncPostgresSaver checkpoints + analyses table
  recall.py      memori jangka panjang: riwayat keputusan + penilaian hasil
  modes.py       switch teknik: cascade | scalp
  scalp.py       area m15/m30, gerbang area ideal, rencana entry m1/m5
  runner.py      run_analysis() entrypoint
  scheduler.py   jadwal otomatis per bar tutup TF target
  render.py      Telegram HTML renderer
  bot.py         /analyze /schedule /mode /tf /history /memory /help
  cli.py         one-shot CLI
run.sh           Termux/proot launcher
run-server.sh    Ubuntu server launcher (venv + systemd)
requirements-dev.txt  pytest + pytest-asyncio
tests/test_core.py
tests/test_decision.py
tests/test_tv.py
tests/test_scalp.py
tests/test_llm.py
tests/test_scheduler.py
```

## Setup (Termux → Ubuntu proot)

Semua dependency masuk ke venv `$PROJ/.venv` di dalam proot — tidak ada lagi
`pip3 install --break-system-packages`.

```bash
./run.sh install     # buat .venv + install requirements.txt + pip install -e . (detached)
./run.sh dev         # tambah pytest + pytest-asyncio dari requirements-dev.txt
./run.sh info        # cek sys.prefix, versi Python, isi pip
./run.sh test        # pytest di dalam venv
./run.sh cli EURUSD m1
./run.sh bot         # nohup -> .tmp/bot.log
./run.sh shell       # bash interaktif dengan venv siap
```

`proot-distro login ubuntu -- <cmd>` works for one-shot commands; long installs should run
detached (`nohup ... &`) because the proot session dies with the parent shell — `run.sh`
sudah melakukannya untuk `install`, `dev`, dan `bot`.

venv dibuat tanpa `--system-site-packages`, jadi paket sistem tidak bocor ke runtime.

## Config

```bash
cp .env.example .env
```

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — any OpenAI-compatible endpoint (9router default).
- `LLM_MODEL_TREND` / `LLM_MODEL_VALIDATION` / `LLM_MODEL_CRITICAL` / `LLM_MODEL_DECISION` —
  model per tahap agent. Kosong ⇒ ikut `LLM_MODEL`. Tahap ringan (tren, validasi, area kritis)
  pakai model murah; tahap berat (keputusan entry + SL/TP) pakai model kuat.
- `LLM_FALLBACK_TREND` / `..._VALIDATION` / `..._CRITICAL` / `..._DECISION` dan `LLM_FALLBACKS` —
  rantai cadangan dipisah koma, dicoba berurutan bila model utama gagal (kuota habis, model tidak
  ada). Setiap kegagalan tercatat sebagai `WARNING` di log.
- `DATABASE_URL` — Neon/Supabase/etc. with `?sslmode=require`. Unset ⇒ graph still runs, checkpoints
  and `/history` are skipped.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` (comma separated; empty ⇒ open bot).

## Run

### Ubuntu server (native, no proot)

`run-server.sh` uses a venv and optionally systemd:

```bash
./run-server.sh install          # apt deps + .venv + pip install -e . + .env scaffold
./run-server.sh cli EURUSD m1    # one-shot, printed
./run-server.sh test             # pytest
./run-server.sh bot              # foreground
./run-server.sh start            # daemon: systemd if available, else nohup + logs/bot.pid
./run-server.sh status|stop|restart|logs [N]
sudo ./run-server.sh install-service   # writes /etc/systemd/system/forex-bot.service
sudo ./run-server.sh uninstall-service
```

`have_systemd()` checks `[ -d /run/systemd/system ]`, so containers/proot that ship the
`systemctl` binary without a running PID 1 fall back to nohup + PID file instead of hanging.
Overrides: `PYTHON_BIN`, `RUN_USER`, `SERVICE_NAME`.

### Termux (proot-distro)

`run.sh` wraps `proot-distro login ubuntu`:

```bash
./run.sh install
./run.sh cli EURUSD m1
./run.sh bot
./run.sh test
```

### Direct

```bash
forex-agents EURUSD m1            # CLI, rendered
forex-agents EURUSD m1 --json     # raw final state
forex-bot                         # Telegram polling
```

Telegram:

```
/analyze EURUSD m1
/tf m1
/history
/memory XAUUSD m1
/schedule add XAUUSD m1          # otomatis tiap 15 menit
/schedule add EURUSD m1 30m      # interval custom
/schedule list
/schedule pause <ID|all>
/schedule resume <ID|all>
/schedule del <ID|all>
/schedule run <ID>
```

## Jadwal otomatis (cron ala bar tutup)

`/schedule add <SIMBOL> <TF> [INTERVAL]` menjalankan cascade berulang tiap bar TF target
terbesar tutup. Interval default = panjang TF `trend` dari cascade (dua langkah di atas TF dasar):

| TF dasar | target TF | interval default |
|---|---|---|
| `m1` | `m5` + `m15` | 15 menit |
| `m5` | `m15` + `m30` | 30 menit |
| `m15` | `m30` + `h1` | 1 jam |
| `m30` | `h1` + `h4` | 4 jam |
| `h1` | `h4` + `d1` | 1 hari |
| `h4` | `d1` + `w1` | 1 minggu |

Interval custom: `15`, `15m`, `15mnt`, `30 menit`, `2h`, `1d`, `1w` (menit–minggu).

Detail perilaku:

- Bar `m15`/`h1` disejajarkan ke grid UTC (`:00 :15 :30 :45`), harian/mingguan ke tengah malam UTC.
- Ada grace 5 detik setelah bar tutup supaya provider sudah menerbitkan bar terakhir.
- Job disimpan di tabel `schedules` (Postgres) dan dipulihkan saat bot start; `next_run` dihitung
  ulang agar tidak menumpuk setelah downtime.
- `/schedule pause <ID|all>` menjeda tanpa menghapus: job tetap di DB (`enabled=false`), `next_run`
  terakhir dipertahankan, dan tidak pernah jatuh tempo sampai di-`resume`.
- `/schedule resume <ID|all>` mengaktifkan lagi dan menjadwalkan bar tutup berikutnya (bukan
  langsung tembak), jadi tidak ada burst analisis setelah jeda panjang.
- Selector menerima `<ID>`, `<SIMBOL>:<TF>` (mis. `XAUUSD:m1`), atau `all`/`semua`.
- Maks 5 jadwal per chat, 20 total. Satu job gagal/timeout tidak mematikan loop; kegagalan
  dilaporkan ke chat dan job dinonaktifkan otomatis setelah 5 kali gagal.
- Scheduler hidup di proses bot (`forex-bot`), jadi bot harus jalan agar jadwal berjalan.
- Cek cepat tanpa menunggu: `python .tmp/smoke_sched.py` atau `/schedule run <ID>`.

## Symbols

Any 6-letter FX code (`EURUSD`, `GBPJPY`, `USDIDR`) maps to `SYMBOL=X`. Non-FX instruments map
through `SYMBOL_ALIASES` in `src/forex_agents/market.py`, because yfinance has no `XAUUSD=X`:

| input | ticker | pip |
|---|---|---|
| `XAUUSD` / `GOLD` | `GC=F` | 0.1 |
| `XAGUSD` / `SILVER` | `SI=F` | 0.1 |
| `BTCUSD` / `ETHUSD` | `BTC-USD` / `ETH-USD` | 1.0 |
| `US30` / `NAS100` / `SPX500` | `^DJI` / `^NDX` / `^GSPC` | 1.0 |
| `USOIL` / `UKOIL` | `CL=F` / `BZ=F` | 1.0 |
| `DXY` | `DX-Y.NYB` | 1.0 |
| `USDJPY` and other JPY crosses | `USDJPY=X` | 0.01 |

`pip_size()` returns the per-instrument pip so ATR-in-pips and the 3x-ATR stop veto stay
meaningful outside FX majors (a fixed 0.0001 made gold ATR read as 36666 pips).
Telegram `/symbols` prints the table.

## Market data providers

`MARKET_PROVIDER` di `.env` memilih sumber OHLCV + tick:

| nilai | sumber | catatan |
|---|---|---|
| `tradingview` (default) | TradingView websocket | anonim, tanpa terminal/broker, lag ~10–60 detik |
| `yfinance` | Yahoo Finance | tanpa login, delay FX ~70–100 detik, emas futures ~10 menit |

### TradingView

`src/forex_agents/market_tv.py` — `TradingViewProvider` memakai package
[`tradingview-api`](https://pypi.org/project/tradingview-api/) (modul import: `TradingView`).
Tidak butuh MetaTrader, container, atau akun broker.

```bash
pip install tradingview-api   # butuh websockets + requests
```

Alur: `Client.connect()` → `ChartSession.subscribe(ticker, tf, count)` → tunggu event
`update` → `periods` dipetakan ke DataFrame OHLCV ber-indeks UTC.

Tiga jebakan library yang sudah ditangani di provider:

1. `Client.send()` menahan semua paket selama `logged` masih `False` dan hanya mem-flush queue
   di `connect()`. Tanpa hook `client.on_logged(lambda *_: client._send_queue())` yang
   didaftarkan **sebelum** `connect()`, `subscribe` tidak pernah terkirim dan session diam
   sampai timeout.
2. Node graph berjalan di dalam event loop aktif, jadi `asyncio.run()` melempar
   `RuntimeError: asyncio.run() cannot be called from a running event loop`. `_run()` mendeteksi
   loop aktif lalu menjalankan coroutine di thread terpisah dengan loop sendiri.
3. Handshake `wss://data.tradingview.com` kadang timeout (`TimeoutError: timed out during
   opening handshake`). `_run_with_retry()` mengulang sampai `TV_RETRIES` kali dengan backoff
   `0.8 * attempt` detik, tapi hanya untuk error yang mengandung `handshake`/`timeout`/
   `connection`/`closed`.

Pemetaan timeframe (`TV_TF`): `m1→1`, `m5→5`, `m15→15`, `m30→30`, `h1→60`, `h4→240`,
`d1→1D`, `w1→1W`.

Pemetaan simbol (`to_tv_symbol`): default prefix exchange `FX`, jadi `EURUSD → FX:EURUSD`.
Instrumen non-FX dipetakan ke ticker TradingView: `XAUUSD→TVC:GOLD`, `XAGUSD→TVC:SILVER`,
`BTCUSD→BITSTAMP:BTCUSD`, `NAS100→TVC:NDX`, `SPX500→TVC:SPX`, `USOIL→TVC:USOIL`,
`DXY→TVC:DXY`. Simbol yang sudah berprefix (`OANDA:EURUSD`) dipakai apa adanya.

Konfigurasi `.env`:

```ini
MARKET_PROVIDER=tradingview
MARKET_FALLBACK=yfinance   # dipakai otomatis bila websocket diblokir
TV_EXCHANGE=FX             # FX | OANDA | FX_IDC | ...
TV_SERVER=data             # data | prodata | widgetdata
TV_TIMEOUT=25              # detik menunggu bar pertama
TV_RANGE=300               # bar yang diminta per subscribe
TV_RETRIES=3               # ulang saat handshake websocket timeout
TV_DEBUG=false
```

`quote()` memakai bar `m1` terbaru dan melaporkan `price`, `time`, `lag_s`, `ticker`, dan
`source: tradingview`. Kegagalan apa pun dibungkus jadi `TVUnavailable` supaya `fetch_node`
bisa pindah ke `MARKET_FALLBACK` dan mencatatnya di bagian `kesalahan:`.

### Fallback

`MARKET_FALLBACK` membuat `fetch_node` mencoba provider utama dulu, lalu provider cadangan
bila `fetch()` pertama gagal. Provider yang gagal dicatat di `errors` dan tetap tampil di
bagian `kesalahan:` pada balasan, jadi tidak pernah senyap.

```ini
MARKET_PROVIDER=tradingview
MARKET_FALLBACK=yfinance
```

## Tests

```bash
pytest -q
```

56 tests: cascade mapping (incl. clamp at `w1`), indicator finiteness, snapshot shape, structure
detection, decision guardrails (low RR, wrong SL/TP side, stop > 3x ATR), HTML escaping, symbol
aliases, pip sizing, lag/quote helpers, dan TradingView provider dengan `TradingView.Client` /
`ChartSession` palsu (subscribe, flush queue saat `logged`, cache TTL, quote lag, retry handshake,
error → `TVUnavailable`, fetch di dalam event loop aktif). No network or LLM calls.

## Schema migration

`memory.py:SCHEMA_SQL` is idempotent and self-migrating. It creates `analyses` if missing, then
`ADD COLUMN IF NOT EXISTS` for `request_id/base_tf/trend_tf/validation_tf/critical_tf/action/confidence/price/payload`,
drops `NOT NULL` from the legacy `decision` column, and widens legacy `numeric(5,4)` price columns
to `DOUBLE PRECISION` (they overflow for gold/indices/crypto).

## Memori jangka panjang

Checkpointer LangGraph hanya menyimpan satu thread (satu request), jadi tidak punya ingatan antar
analisis. `recall.py` + tabel `analyses` memberi ingatan:

- `history_for(conn, symbol, base_tf, limit)` mengambil N keputusan terakhir untuk simbol + TF dasar
  yang sama (bukan TF target), urut terbaru dulu.
- `recall.evaluate(action, entry, stop_loss, take_profit, price_now)` menilai hasil lama terhadap
  harga sekarang: `tp`, `sl`, `terbuka`, atau `netral` (NO_TRADE). Ini cek level, bukan backtest.
- `recall.summarize()` menghasilkan teks "N analisis terakhir, aksi berulang: X xN / hasil: tp a, sl b,
  terbuka c, netral d" + daftar baris `waktu AKSI conf plan -> hasil`.
- `runner.load_memory()` menyuntik blok itu ke `state["memory"]`; `nodes._mem()` menambahkannya ke
  prompt tren, validasi, area kritis, dan keputusan. Kegagalan DB tidak pernah menggagalkan analisis
  (hanya `error` di blok memori).
- Blok memori sengaja TIDAK disimpan ke `payload` (`save_analysis` membuang key `memory`) supaya baris
  tetap kecil dan ingatan dibangun ulang dari baris sebelumnya.
- Output menampilkan seksi `<b>5) Ingatan</b>`; `/memory [SIMBOL] [TF]` menampilkan 10 baris terakhir.

## Notes / limits

- `yfinance` intraday history is limited (1m ≈ 7d, 5m/15m ≈ 60d); fine for the cascade, not for
  backtesting years of m1.
- Every stage is one LLM call ⇒ 4 calls per analysis. Failures degrade to neutral defaults and are
  surfaced in the `errors` list instead of crashing the graph.
- Not financial advice; the decision node is deliberately biased to `NO_TRADE`.
