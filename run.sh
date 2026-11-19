#!/data/data/com.termux/files/usr/bin/bash
# Launch helpers that survive the proot session dying with the parent shell.
set -euo pipefail
cd "$(dirname "$0")"

PROJ=/data/data/com.termux/files/home/forex-agents

case "${1:-help}" in
  install)
    nohup proot-distro login ubuntu -- bash -c \
      "cd $PROJ && pip3 install --break-system-packages -r requirements.txt" \
      > .tmp/install.log 2>&1 &
    echo "install running -> .tmp/install.log"
    ;;
  cli)
    shift
    proot-distro login ubuntu -- bash -c "cd $PROJ && python3 -m forex_agents.cli $*"
    ;;
  bot)
    nohup proot-distro login ubuntu -- bash -c \
      "cd $PROJ && python3 -m forex_agents.bot" > .tmp/bot.log 2>&1 &
    echo "bot running -> .tmp/bot.log"
    ;;
  test)
    proot-distro login ubuntu -- bash -c "cd $PROJ && python3 -m pytest -q"
    ;;
  *)
    echo "usage: $0 {install|cli <SYMBOL> <TF>|bot|test}"
    ;;
esac
