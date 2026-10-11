#!/usr/bin/env bash
# DROPLET: run live/patches/manual_trim_20261008.py for one market with its bot service stopped.
# usage: live/manual_trim.sh sol|eth|btc [--send] [--set k=v ...]   (no --send = dry run, service left running)
set -u
m=${1:?sol|eth|btc}; shift
case $m in sol) c=configs/live_100.toml;; eth) c=configs/live_eth.toml;; btc) c=configs/live_btc.toml;; *) echo bad market; exit 2;; esac
cd /workspace/sol-bot
set -a; . /home/ratchet/.secrets/ratchet.env; set +a
send=0; for x in "$@"; do [ "$x" = --send ] && send=1; done
[ $send = 1 ] && sudo systemctl stop ratchet-$m
.venv/bin/python -m live.patches.manual_trim_20261008 --config $c "$@"; rc=$?
[ $send = 1 ] && sudo systemctl start ratchet-$m && echo "ratchet-$m restarted"
exit $rc
