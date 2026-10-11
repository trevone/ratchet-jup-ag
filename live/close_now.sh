#!/usr/bin/env bash
# DROPLET version: market-close the whole SOL position now (cancels TPs first). Stops ONLY ratchet-sol so the bot
# and this script never act at once, then starts it again (ETH/BTC keep running).
set -u
cd /workspace/sol-bot
sudo systemctl stop ratchet-sol
set -a; . /home/ratchet/.secrets/ratchet.env; set +a
.venv/bin/python -m live.close_now
sudo systemctl start ratchet-sol
echo "ratchet-sol restarted"
