#!/bin/bash
# Wrapper (2026-10-10 04:55 MT, credit saving): runs quickcheck_core.sh; on QUIET logs the quiet line itself and prints one compact line.
cd /workspace/sol-bot
out=$(bash live/quickcheck_core.sh 2>&1)
# Trevor 2026-10-11 19:08 MT: unexplained wallet change flag (bank ext_flow_mode=alert) -> ALERT EXT (delete the file once resolved)
[ -f live/EXT_FLOW_ALERT ] && out="$out"$'\n'"ALERT EXT $(wc -l < live/EXT_FLOW_ALERT) entr(y/ies), last: $(tail -1 live/EXT_FLOW_ALERT | cut -c1-300)"
if echo "$out" | grep -qE '^EVENTS$|ALERT'; then echo "$out"
  # JupSOL rescue (Trevor 2026-10-11): on ALERT LIQ also print the rescue status (EMERGENCY => notify Trevor now)
  if echo "$out" | grep -q 'ALERT LIQ'; then (set -a; . /home/ratchet/.secrets/ratchet.env 2>/dev/null; set +a; unset JUP_LIVE; timeout 120 .venv/bin/python -m live.rescue status 2>&1 | grep RESCUE); fi
  echo "VERDICT: EVENTS"; else
  s=$(echo "$out" | grep -E '^(sol|eth|btc) (liq|hwm|risk)|close=' | tr '\n' ';' | cut -c1-600)
  echo "$(date '+%Y-%m-%d %H:%M') MT quickcheck: quiet" >> live/changes.log
  echo "VERDICT: QUIET (logged) | $s"; fi
