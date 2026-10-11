#!/usr/bin/env bash
# DROPLET: JupSOL liquidation rescue (Trevor 2026-10-11). Dry run unless --send; with --send the bot's service is
# stopped for the state write and restarted after. usage: live/rescue.sh status | lend <bot> <amt|auto> [--send] |
# extend <bot> [n] [--send] | add <bot> <usd> [--lev X] [--send] | restore <bot> [--send]
set -u
cd /workspace/sol-bot
set -a; . /home/ratchet/.secrets/ratchet.env 2>/dev/null; set +a
unset JUP_LIVE
send=0; for x in "$@"; do [ "$x" = --send ] && send=1; done
svc=""; [ $send = 1 ] && [ $# -ge 2 ] && svc=ratchet-$2
[ -n "$svc" ] && sudo systemctl stop $svc
[ $send = 1 ] && export JUP_LIVE=1
sudo -u ratchet --preserve-env .venv/bin/python -m live.rescue "$@"; rc=$?
[ -n "$svc" ] && sudo systemctl start $svc && echo "$svc restarted"
exit $rc
