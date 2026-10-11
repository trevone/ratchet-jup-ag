# RESCUE_PLAYBOOK — JupSOL liquidation rescue (Trevor 2026-10-11 08:23 MT)

Grok Bot's hourly check. All commands on the droplet in /workspace/sol-bot. Everything is a DRY RUN unless `--send`.
Config: `live/rescue_cfg.json` (trigger 3%, target 10%, collateral_share 0.6, repay_margin 5%, emergency thresholds).

1. Run quickcheck (box `live/quickcheck.sh`). On `ALERT LIQ <bot>` it also prints `RESCUE ...` lines.
   Or directly: `bash live/rescue.sh status` (exit 3 = EMERGENCY).
2. Line ends with `EMERGENCY` (in trigger and JupSOL can't push liq out >= 2 pts / is < 10% of collateral):
   **notify Trevor immediately** (liq px, distance, JupSOL ~$, $ needed "at 3% trigger"). Still lend what there is if he
   doesn't answer and distance keeps shrinking.
3. `IN-TRIGGER` without EMERGENCY: `bash live/rescue.sh lend <bot> auto` (dry run: check every step `ok: true`,
   `new_dist_pct`), then `bash live/rescue.sh lend <bot> auto --send` (stops the bot service, unstakes JupSOL -> SOL
   [-> ETH/WBTC], deposits collateral via increase sizeUsd 0, credits the rest to the bank as `bank.loan`, restarts).
   Refuses if not within trigger unless `--force`.
4. Rungs: status shows `rungs used/max`. If used >= max: `bash live/rescue.sh extend <bot> --send` (+6 rungs;
   original saved in `bank.rescue_settings`) then `sudo systemctl restart ratchet-<bot>`. If the ladder still won't add
   (no levels beyond), add on its behalf: `bash live/rescue.sh add <bot> <usd> [--lev X]` dry, then `--send`
   (touches RETP_NOW so TPs are re-placed for the new average).
5. Repay is automatic: when the bot is flat with an open loan, `bank_tick` repays principal x 1.05 (+ profit share if
   set) to JupSOL and restores settings. Manual: `bash live/rescue.sh restore <bot> --send`.
6. Log every send in `live/changes.log`. Loan never touches HWM / wd_mark; profit guard and withdrawals use bal - loan.

## Live test 2026-10-11 08:39 MT (Trevor approved 08:38)
`rescue.sh lend sol 0.0015 --send --force --collateral-usd 1` (override caps at coin received = 100% to collateral): unstake 0.0015 JupSOL -> 0.001819479 SOL (sig 5U91tXCw...oqet), collateral add sizeUsd 0 (sig 4M5GAoGd...iTJ4G). Short collateral $10.45 -> $10.65, liq 130.12 -> 130.54, size $52.50 unchanged, TPs 109.01/108.73 kept, bank.loan principal 0.001819479 SOL, bal/hwm unchanged. Loan left open to exercise auto-repay (+5% = 0.001910453 SOL) at close.
