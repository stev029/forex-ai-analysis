#!/usr/bin/env bash
# Native Ubuntu/Debian server launcher (no proot). venv + optional systemd service.
# Usage: ./run-server.sh {install|cli <SYMBOL> <TF>|bot|test|start|stop|restart|status|logs|install-service|uninstall-service}
set -euo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ"

PY="${PYTHON_BIN:-python3}"
VENV="$PROJ/.venv"
PYBIN="$VENV/bin/python"
PIPBIN="$VENV/bin/pip"
SERVICE="${SERVICE_NAME:-forex-bot}"
RUN_USER="${RUN_USER:-$(id -un)}"
LOG_DIR="$PROJ/logs"
UNIT="/etc/systemd/system/${SERVICE}.service"

# systemd only when it is actually the init system (containers/proot often ship the binary
# without a running PID 1, where `systemctl start` would hang or fail).
have_systemd() {
  [ -f "$UNIT" ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]
}

need_venv() {
  [ -x "$PYBIN" ] || { echo "venv missing: run ./run-server.sh install"; exit 1; }
}

ensure_logs() { mkdir -p "$LOG_DIR"; }

case "${1:-help}" in
  install)
    echo "== apt deps =="
    if command -v apt-get >/dev/null; then
      SUDO=""
      [ "$(id -u)" -ne 0 ] && SUDO="sudo"
      $SUDO apt-get update -qq
      $SUDO apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl
    fi
    echo "== venv =="
    [ -d "$VENV" ] || "$PY" -m venv "$VENV"
    "$PIPBIN" install --upgrade -q pip wheel setuptools
    echo "== project deps =="
    "$PIPBIN" install -q -r requirements.txt
    "$PIPBIN" install -q -e .
    [ -f .env ] || { cp .env.example .env; echo "created .env — fill DATABASE_URL / TELEGRAM_BOT_TOKEN"; }
    ensure_logs
    echo "install done: $("$PYBIN" -c 'import sys;print(sys.version.split()[0])')"
    ;;

  cli)
    need_venv; shift
    ensure_logs
    "$PYBIN" -m forex_agents.cli "$@"
    ;;

  bot)
    need_venv
    ensure_logs
    echo "bot foreground (Ctrl-C to stop). For daemon use: $0 start"
    exec "$PYBIN" -m forex_agents.bot
    ;;

  test)
    need_venv
    "$PYBIN" -m pytest -q
    ;;

  dev)
    need_venv
    "$PIPBIN" install -q -r requirements-dev.txt
    echo "dev deps ready: $("$PYBIN" -m pytest --version 2>&1 | head -1)"
    ;;

  start)
    need_venv; ensure_logs
    if have_systemd; then
      sudo systemctl start "$SERVICE"
      sudo systemctl --no-pager --lines=0 status "$SERVICE" || true
    else
      if [ -f "$LOG_DIR/bot.pid" ] && kill -0 "$(cat "$LOG_DIR/bot.pid")" 2>/dev/null; then
        echo "already running pid $(cat "$LOG_DIR/bot.pid")"; exit 0
      fi
      nohup "$PYBIN" -m forex_agents.bot >> "$LOG_DIR/bot.log" 2>&1 &
      echo $! > "$LOG_DIR/bot.pid"
      echo "started pid $(cat "$LOG_DIR/bot.pid") -> $LOG_DIR/bot.log"
    fi
    ;;

  stop)
    if have_systemd; then
      sudo systemctl stop "$SERVICE" || true
      echo "stopped $SERVICE"
    elif [ -f "$LOG_DIR/bot.pid" ]; then
      PID="$(cat "$LOG_DIR/bot.pid")"
      kill "$PID" 2>/dev/null || true
      rm -f "$LOG_DIR/bot.pid"
      echo "stopped pid $PID"
    else
      echo "not running"
    fi
    ;;

  restart)
    "$0" stop; sleep 1; "$0" start
    ;;

  status)
    if have_systemd; then
      sudo systemctl --no-pager status "$SERVICE" || true
    elif [ -f "$LOG_DIR/bot.pid" ] && kill -0 "$(cat "$LOG_DIR/bot.pid")" 2>/dev/null; then
      echo "running pid $(cat "$LOG_DIR/bot.pid")"
    else
      echo "not running"
    fi
    ;;

  logs)
    if have_systemd; then
      sudo journalctl -u "$SERVICE" -n "${2:-100}" -f
    else
      tail -n "${2:-100}" -f "$LOG_DIR/bot.log"
    fi
    ;;

  install-service)
    need_venv; ensure_logs
    [ "$(id -u)" -eq 0 ] || { echo "run as root (sudo $0 install-service)"; exit 1; }
    [ -d /run/systemd/system ] || { echo "systemd not running as PID 1 here; use ./run-server.sh start instead"; exit 1; }
    cat > "$UNIT" <<EOF
[Unit]
Description=forex-agents Telegram bot (LangGraph FX cascade)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJ
EnvironmentFile=$PROJ/.env
ExecStart=$PYBIN -m forex_agents.bot
Restart=always
RestartSec=5
StandardOutput=append:$LOG_DIR/bot.log
StandardError=append:$LOG_DIR/bot.log
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$SERVICE"
    echo "unit written: $UNIT"
    echo "start with: sudo systemctl start $SERVICE"
    ;;

  uninstall-service)
    [ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "removed $SERVICE"
    ;;

  *)
    cat <<EOF
usage: $0 {install|dev|cli <SYMBOL> <TF>|bot|test|start|stop|restart|status|logs|install-service|uninstall-service}

  install            apt deps + venv + pip install -e . + .env scaffold
  cli EURUSD m1      one-shot analysis, printed
  bot                run bot in foreground
  test               pytest
  dev                install requirements-dev.txt (pytest, pytest-asyncio)
  start|stop|status  systemd if installed, else nohup + logs/bot.pid
  logs [N]           journalctl if systemd, else tail logs/bot.log
  install-service    write /etc/systemd/system/${SERVICE}.service (root)

env: PYTHON_BIN, RUN_USER, SERVICE_NAME
EOF
    ;;
esac
