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

Indicators are computed deterministically (`indicators.py`) and the LLM only receives a compact
numeric snapshot — it never sees raw candles and cannot invent prices. The decision node then
applies hard guardrails (RR >= `MIN_RR`, stop <= 3x ATR, SL/TP on the correct side of entry);
any veto downgrades the call to `NO_TRADE`.

## Layout

```
src/forex_agents/
  config.py      pydantic-settings env loading
  timeframes.py  ladder, aliases, cascade(), yfinance interval map
  market.py      YFinanceProvider + get_provider() dispatch
  market_tv.py     TradingViewProvider (websocket tradingview-api)
  indicators.py  EMA/ATR/RSI/MACD, swing points, structure, snapshot()
  schemas.py     Pydantic contracts per stage
  llm.py         ChatOpenAI factory + structured() with JSON fallback
  state.py       AnalysisState TypedDict (reducers for errors/trace)
  nodes.py       fetch / trend / validation / critical / decision
  graph.py       StateGraph wiring + data gate
  memory.py      AsyncPostgresSaver checkpoints + analyses table
  runner.py      run_analysis() entrypoint
  scheduler.py   jadwal otomatis per bar tutup TF target
  render.py      Telegram HTML renderer
  bot.py         /analyze /schedule /tf /history /help
  cli.py         one-shot CLI
run.sh           Termux/proot launcher
run-server.sh    Ubuntu server launcher (venv + systemd)
requirements-dev.txt  pytest + pytest-asyncio
tests/test_core.py
tests/test_decision.py
tests/test_tv.py
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
/schedule add XAUUSD m1          # otomatis tiap 15 menit
/schedule add EURUSD m1 30m      # interval custom
/schedule list
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

## Notes / limits

- `yfinance` intraday history is limited (1m ≈ 7d, 5m/15m ≈ 60d); fine for the cascade, not for
  backtesting years of m1.
- Every stage is one LLM call ⇒ 4 calls per analysis. Failures degrade to neutral defaults and are
  surfaced in the `errors` list instead of crashing the graph.
- Not financial advice; the decision node is deliberately biased to `NO_TRADE`.
