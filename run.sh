#!/data/data/com.termux/files/usr/bin/bash
# Launch helpers that survive the proot session dying with the parent shell.
# Semua perintah Python dijalankan lewat venv di dalam proot ($PROJ/.venv),
# bukan pip sistem + --break-system-packages.
set -euo pipefail
cd "$(dirname "$0")"

PROJ=/data/data/com.termux/files/home/forex-agents
VENV="$PROJ/.venv"
PYBIN="$VENV/bin/python"
PIPBIN="$VENV/bin/pip"
mkdir -p .tmp

# Jalankan perintah di dalam proot Ubuntu dengan venv aktif.
in_proot() {
  proot-distro login ubuntu -- bash -c "cd $PROJ && $*"
}

case "${1:-help}" in
  install)
    nohup proot-distro login ubuntu -- bash -c "
      set -e
      cd $PROJ
      command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3 python3-venv; }
      [ -d $VENV ] || python3 -m venv $VENV
      $PIPBIN install -q --upgrade pip wheel setuptools
      $PIPBIN install -q -r requirements.txt
      $PIPBIN install -q -e .
      [ -f .env ] || cp .env.example .env
      echo venv-ready \$($PYBIN -V)
    " > .tmp/install.log 2>&1 &
    echo "install running -> .tmp/install.log"
    ;;
  cli)
    shift
    in_proot "$PYBIN -m forex_agents.cli $*"
    ;;
  bot)
    nohup proot-distro login ubuntu -- bash -c \
      "cd $PROJ && exec $PYBIN -m forex_agents.bot" > .tmp/bot.log 2>&1 &
    echo "bot running -> .tmp/bot.log"
    ;;
  test)
    in_proot "$PYBIN -m pytest -q"
    ;;
  dev)
    nohup proot-distro login ubuntu -- bash -c \
      "cd $PROJ && $PIPBIN install -q -r requirements-dev.txt && echo dev-deps-ready" \
      > .tmp/dev-install.log 2>&1 &
    echo "dev deps running -> .tmp/dev-install.log"
    ;;
  shell)
    in_proot "exec bash -i"
    ;;
  info)
    in_proot "echo venv=\$($PYBIN -c 'import sys;print(sys.prefix)') && $PYBIN -V && $PIPBIN list | head -20"
    ;;
  *)
    echo "usage: $0 {install|dev|cli <SYMBOL> <TF>|bot|test|shell|info}"
    ;;
esac
