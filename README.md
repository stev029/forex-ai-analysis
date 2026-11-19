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
  market.py      YFinanceProvider (cached 30s), h4 resample
  indicators.py  EMA/ATR/RSI/MACD, swing points, structure, snapshot()
  schemas.py     Pydantic contracts per stage
  llm.py         ChatOpenAI factory + structured() with JSON fallback
  state.py       AnalysisState TypedDict (reducers for errors/trace)
  nodes.py       fetch / trend / validation / critical / decision
  graph.py       StateGraph wiring + data gate
  memory.py      AsyncPostgresSaver checkpoints + analyses table
  runner.py      run_analysis() entrypoint
  render.py      Telegram HTML renderer
  bot.py         /analyze /tf /history /help
  cli.py         one-shot CLI
run.sh           Termux/proot launcher
run-server.sh    Ubuntu server launcher (venv + systemd)
tests/test_core.py
tests/test_decision.py
```

## Setup (Termux → Ubuntu proot)

```bash
proot-distro login ubuntu
apt-get update && apt-get install -y python3-pip
pip3 install --break-system-packages -r requirements.txt
pip3 install --break-system-packages -e .
```

`proot-distro login ubuntu -- <cmd>` works for one-shot commands; long installs should run
detached (`nohup ... &`) because the proot session dies with the parent shell.

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
```

## Tests

```bash
pytest -q
```

Covers cascade mapping (including clamp at `w1`), indicator finiteness, snapshot shape,
structure detection, decision guardrails (low RR, wrong SL/TP side, stop > 3x ATR) and HTML
escaping in the renderer. No network or LLM calls.

## Notes / limits

- `yfinance` intraday history is limited (1m ≈ 7d, 5m/15m ≈ 60d); fine for the cascade, not for
  backtesting years of m1.
- Every stage is one LLM call ⇒ 4 calls per analysis. Failures degrade to neutral defaults and are
  surfaced in the `errors` list instead of crashing the graph.
- Not financial advice; the decision node is deliberately biased to `NO_TRADE`.
