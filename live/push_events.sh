#!/usr/bin/env bash
# Push the analyst news layer (box /workspace/market-analysis/events.json) to the droplet (atomic rename there).
set -e
K="-i /home/box/.ssh/ratchet_droplet -o BatchMode=yes"
scp -q $K /workspace/market-analysis/events.json ratchet@161.35.1.72:/workspace/market-analysis/events.json.tmp
ssh $K ratchet@161.35.1.72 'mv /workspace/market-analysis/events.json.tmp /workspace/market-analysis/events.json && echo events.json pushed'
