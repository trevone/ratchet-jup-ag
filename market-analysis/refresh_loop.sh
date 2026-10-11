#!/bin/bash
# Keeps outlook files fresh (bots skip trims if files >45 min old).
# Age-based: runs outlook.py whenever latest.json is >= 14 min old (robust to missed minutes / box pauses).
cd /workspace/market-analysis
while true; do
  age=$(( $(date +%s) - $(stat -c %Y latest.json 2>/dev/null || echo 0) ))
  if [ "$age" -ge 840 ]; then
    timeout 180 python3 outlook.py > /tmp/outlook_loop_last.log 2>&1
    echo "$(date -Is) exit=$? age_before=${age}s" >> refresh_loop.log
  fi
  sleep 30
done
