#!/usr/bin/env bash
# Auto-restart wrapper. Stop: touch live/KILL (no new orders) and/or kill this wrapper's PID (live/bot.pid).
cd "$(dirname "$0")/.."
echo $$ > live/bot.pid
while true; do
  .venv/bin/python -m live.bot --config configs/live_100.toml >> live/logs/wrapper.log 2>&1
  echo "$(date -Is) bot exited with $?, restarting in 15s" >> live/logs/wrapper.log
  sleep 15
done
