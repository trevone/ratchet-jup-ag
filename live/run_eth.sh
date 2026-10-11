#!/usr/bin/env bash
# Auto-restart wrapper. Stop: touch live/eth/KILL (or live/KILL_ALL) and/or kill this wrapper's PID (live/eth/bot.pid).
cd "$(dirname "$0")/.."
echo $$ > live/eth/bot.pid
while true; do
  .venv/bin/python -m live.bot --config configs/live_eth.toml >> live/eth/logs/wrapper.log 2>&1
  echo "$(date -Is) bot exited with $?, restarting in 15s" >> live/eth/logs/wrapper.log
  sleep 15
done
