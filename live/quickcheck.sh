#!/bin/bash
# Wrapper (2026-10-10 04:55 MT, credit saving): runs quickcheck_core.sh; on QUIET logs the quiet line itself and prints one compact line.
cd /workspace/sol-bot
out=$(bash live/quickcheck_core.sh 2>&1)
if echo "$out" | grep -qE '^EVENTS$|ALERT'; then echo "$out"; echo "VERDICT: EVENTS"; else
  s=$(echo "$out" | grep -E '^(sol|eth|btc) (liq|hwm|risk)|close=' | tr '\n' ';' | cut -c1-600)
  echo "$(date '+%Y-%m-%d %H:%M') MT quickcheck: quiet" >> live/changes.log
  echo "VERDICT: QUIET (logged) | $s"; fi
