#!/usr/bin/env bash
# Auto-restart wrapper. Stop: touch live/btc/KILL (or live/KILL_ALL) and/or kill this wrapper's PID (live/btc/bot.pid).
cd "$(dirname "$0")/.."
echo $$ > live/btc/bot.pid
while true; do
  .venv/bin/python -m live.bot --config configs/live_btc.toml >> live/btc/logs/wrapper.log 2>&1
  echo "$(date -Is) bot exited with $?, restarting in 15s" >> live/btc/logs/wrapper.log
  sleep 15
done
