# Ratchet on the droplet (since 2026-10-10 18:13 MT)

Host: ratchet-1vcpu-2gb-nyc1, 161.35.1.72, Ubuntu 24.04, TZ America/Denver, NTP on, ufw (OpenSSH only), no SSH passwords,
unattended-upgrades. User `ratchet` owns /workspace/sol-bot and /workspace/market-analysis (same paths as the box).
Python 3.13 venv /workspace/sol-bot/.venv (requirements.lock.txt = box pip freeze), Node 20, live/node_modules via npm ci.
Secrets: /home/ratchet/.secrets/jup-trader.json (600) and /home/ratchet/.secrets/ratchet.env (600: HELIUS_API_KEY, JUP_KEY_PATH),
loaded by systemd EnvironmentFile. Never cat them.

    SSH="ssh -i /home/box/.ssh/ratchet_droplet ratchet@161.35.1.72"    # root@ also works with the same key

## Services (Restart=always, RestartSec=15, enabled at boot)
- ratchet-sol    python -m live.bot --config configs/live_100.toml   logs live/logs/bot.log (+stdout -> wrapper.log)
- ratchet-eth    ... configs/live_eth.toml                            live/eth/logs/
- ratchet-btc    ... configs/live_btc.toml                            live/btc/logs/
- ratchet-levels /workspace/market-analysis/refresh_loop.sh (latest.json + eth/ btc/ every ~15 min)
logrotate: /etc/logrotate.d/ratchet (daily/20M, 14 kept, copytruncate). ratchet may sudo systemctl stop/start/restart ratchet-*.

    $SSH 'systemctl status ratchet-sol ratchet-eth ratchet-btc ratchet-levels --no-pager'
    $SSH 'sudo systemctl restart ratchet-eth'
    $SSH 'touch /workspace/sol-bot/live/KILL_ALL'      # stop new orders on all bots (KILL / eth/KILL / btc/KILL per bot)

## From the box
    bash /workspace/sol-bot/live/quickcheck.sh       # = $SSH 'bash /workspace/sol-bot/live/quickcheck.sh' (QUIET logged on droplet changes.log)
    bash /workspace/sol-bot/live/close_now.sh        # = $SSH 'bash /workspace/sol-bot/live/close_now.sh'  (SOL market close; stops/starts ratchet-sol only)
    # manual trim (runs the patch with that market's service stopped only when --send):
    $SSH 'bash /workspace/sol-bot/live/manual_trim.sh sol'            # dry run (simulate)
    $SSH 'bash /workspace/sol-bot/live/manual_trim.sh btc --send'     # real trim  (sol|eth|btc, extra --set k=v passed through)
    bash /workspace/sol-bot/live/push_events.sh      # copy box market-analysis/events.json (analyst news layer) to droplet

## events.json
The droplet's refresh loop reads its own /workspace/market-analysis/events.json. The analyst agent edits the box copy;
after an edit run live/push_events.sh (cron-less: just call it right after events_update.py / any events.json edit).
Until pushed, the droplet uses the copy taken at migration.

## Notes
- Box run_bot.sh/run_eth.sh/run_btc.sh renamed *.disabled; box state is a frozen snapshot. Box refresh_loop keeps running for the analyst tools.
- Old close_now.sh killed all three bots but restarted only SOL; the droplet version stops/starts only ratchet-sol.
