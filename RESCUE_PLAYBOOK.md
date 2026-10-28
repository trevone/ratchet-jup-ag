# RESCUE_PLAYBOOK — JupSOL liquidation rescue (Trevor 2026-10-11 08:23 MT)

Grok Bot's hourly check. All commands on the droplet in /workspace/sol-bot. Everything is a DRY RUN unless `--send`.
Config: `live/rescue_cfg.json` (trigger 3%, target 10%, keep_jupsol, haircut 1%, emergency_min_push 2 pts, extend_rungs).
2026-10-27 (Trevor): collateral only, max 1/3 of the usable JupSOL per rescue, exact JupSOL repayment (no 5% margin). With
`[ledger] jupsol_loans = "on"` the bot does steps 2-3 itself (`JUPSOL RESCUE ...` lines in its log); this playbook is for "off".

1. Run quickcheck (box `live/quickcheck.sh`). On `ALERT LIQ <bot>` it also prints `RESCUE ...` lines.
   Or directly: `bash live/rescue.sh status` (exit 3 = EMERGENCY). The distance includes the accrued borrow fee.
2. Line ends with `EMERGENCY` (in trigger and min(to 10%, 1/3 of usable JupSOL) moves liq < 2 pts):
   **notify Trevor immediately** (liq px, distance, JupSOL ~$, $ needed to 10%, the 1/3 cap). Still lend what the rule allows
   if he doesn't answer and distance keeps shrinking.
3. `IN-TRIGGER` without EMERGENCY: `bash live/rescue.sh lend <bot> auto` (dry run: check every step `ok: true`,
   `new_dist_pct`), then `bash live/rescue.sh lend <bot> auto --send` (stops the bot service, ONE ExactOut JupSOL -> own-coin
   swap, books the measured coin as `bank.loan` with the measured JupSOL owed, deposits all of it as collateral via increase
   sizeUsd 0, restarts). `lend <bot> <usd>` = that collateral, still max 1/3. Refuses if not within trigger unless `--force`.
4. Rungs: status shows `rungs used/max`. If used >= max: `bash live/rescue.sh extend <bot> --send` (+6 rungs;
   original saved in `bank.rescue_settings`) then `sudo systemctl restart ratchet-<bot>`. If the ladder still won't add
   (no levels beyond), add on its behalf: `bash live/rescue.sh add <bot> <usd> [--lev X]` dry, then `--send`
   (touches RETP_NOW so TPs are re-placed for the new average).
5. Repay is automatic in every mode: when the bot is flat (after any inter-bot loan; not while pend / book_pend /
   trueup_block / a payout window), `bank_tick` buys back exactly the JupSOL still owed (ExactOut) and restores settings. Not
   enough balance -> it repays what it can and carries the rest to its next flat (status shows `carried to next flat`).
   Manual: `bash live/rescue.sh restore <bot> --send` (refuses when the balance can't buy it all back).
6. Log every send in `live/changes.log`. Loan never touches HWM / wd_mark; profit guard and withdrawals use bal - loan.

## Live test 2026-10-11 08:39 MT (Trevor approved 08:38)
`rescue.sh lend sol 0.0015 --send --force --collateral-usd 1` (override caps at coin received = 100% to collateral): unstake 0.0015 JupSOL -> 0.001819479 SOL (sig 5U91tXCw...oqet), collateral add sizeUsd 0 (sig 4M5GAoGd...iTJ4G). Short collateral $10.45 -> $10.65, liq 130.12 -> 130.54, size $52.50 unchanged, TPs 109.01/108.73 kept, bank.loan principal 0.001819479 SOL, bal/hwm unchanged. Loan left open to exercise auto-repay (+5% = 0.001910453 SOL) at close.
