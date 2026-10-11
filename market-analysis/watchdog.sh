#!/bin/bash
# Restart refresh_loop.sh if it isn't running. Run by cron every 5 min.
pgrep -f refresh_loop.sh >/dev/null || (cd /workspace/market-analysis && nohup setsid ./refresh_loop.sh >/dev/null 2>&1 & echo "$(date -Is) restarted refresh_loop" >> /workspace/market-analysis/watchdog.log)
