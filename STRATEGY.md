# Ratchet — Jupiter Perps live strategy (SOL / ETH / BTC)

Owner reference for Trevor’s three live bots. Numbers from `configs/live_100.toml` (SOL), `live_eth.toml`, `live_btc.toml`, plus `live/bot.py` / `live/trim.py` / `live/bank.py` / `dca_levels.py`. If we get misaligned, come back here.

---

## Core design (Trevor 2026-10-30 07:15-07:17 MT)

In Trevor's words:

- Each bot is a separate account with its own token and its own HWM, all measured on-chain (SOL bot = native SOL, ETH bot = Wormhole ETH, BTC bot = WBTC).
- Bots don't share funds; they only borrow from each other.
- A bot can use all of its token.
- HWM is the only bookkeeping number. When flat and the token is higher than the HWM, HWM = token. HWM never moves down.
- Only problems should leave a bot below its HWM (a loss, a loan out, fee debt). Normal operations (deposits, the nightly withdrawal, repaid loans) must not leave a bot stuck below its HWM.
- Only USDC deposits need processing (thirds into SOL / ETH / WBTC, `live/deposit.py`, unchanged).

How the code does it (`[ledger] ext_flow_mode = "chain"`, live on all three bots from this PR):

- Every bank tick, when the bot has no non-trigger request in flight, its balance is set to its on-chain token: SOL = wallet native SOL - `live.sol_fee_reserve` (0.02); ETH / BTC = the wallet's Wormhole ETH / WBTC. Logged as `BANK chain balance`. Sizing (free) uses this balance, so any token that arrives is usable on the next tick; there is no own-token deposit detection and no `EXT_FLOW_ALERT` for own-token changes.
- HWM: unchanged `update_hwm` (only when flat: `hwm = max(hwm, bal - fee debt - loan out)`), so it never moves down from that path and loans / fee debt keep a bot below HWM.
- Profit guard: target = on-chain balance at trade start x 1.003 (`trade_start_equity`, set from the synced balance).
- Known exceptions to "never moves down": the nightly withdrawal lowers HWM (and wd_mark) by the amount withdrawn, otherwise the bot would sit below HWM after a normal withdrawal. A USDC deposit credit raises bal, HWM and wd_mark together (unchanged).
- Open question for Trevor: an own-token deposit raises HWM while flat but not wd_mark, so the nightly withdrawal pays out `withdraw_fraction` (0.90) of it to JupSOL.

## TP rules (Trevor 2026-10-15, Option A; replaces the 2026-10-11 level rule)

Applies identically to all three bots (SOL, ETH, BTC). Config: `[tp] tp1_pct`, `tp2_pct` (fractions, `0 < tp1_pct < tp2_pct`, set together, two-level ladder). Live (Trevor 2026-10-27 09:19 MT): **TP1 0.75% on SOL and ETH, 0.64% on BTC; TP2 1.2% on all three** (was 0.8% / 1.5% at 2026-10-15, cut to 0.64% / 1.2% on 2026-10-24).

- **TP1 = avg x (1 -/+ tp1_pct), TP2 = avg x (1 -/+ tp2_pct)** (shorts below, longs above the average entry), set at open / add / re-place. Analyst levels, ATR and the old 60% ratio play no part. `tp.levels` only supplies the 50% / 100% close split (its `dist` is unused while the pct keys are set; SOL's `tp.mode = "price_abs"` now says its dists are USD).
- Safety kept: fee-floor minimums (`tp_min_net_usd` chunk floor, then `apply_tp_fee_min` 3.0x / 4.5x; the floor wins if the pct price is inside it), TP1 at least `tp_min_gap_usd` inside TP2, never a trigger at/through market (`tp_market_gap`).
- **TP2 only moves when Grok Bot decides** (`TP2_SET`). No automatic process moves TP1 or TP2 between adds (no periodic re-place, no fade, no guard moving TPs). A one-shot manual re-place via the normal placement path: `touch live/RETP_NOW` (or `live/eth/`, `live/btc/`).
- All three bots: TP1/TP2 at `tp1_pct`/`tp2_pct` from avg until a TP3 partial fills on the trade; after that TP1 and TP2 both sit at the SAME price = the profit-guard price clamped by the stricter fee floor (2026-10-16: no `tp_guard_gap_pct` spacing, no `tp_min_gap_usd`; `tps_at_guard` removed).
- Removed with this rule: the level rule (TP1 front-running analyst levels, `tp1_mode`, `tp2_from_tp1_ratio`), the 60% ratio (`tp1_ratio*`), the TP2 resistance cap (`tp_res_*`, analyst touch counts), the momentum fade (`tp_fade_*`, `tp_near_miss_frac`, `tp_swing_buffer_atr`; the bot never passed fade inputs) and the above-water TP1 (`deep_buffer_*`).

## Profit-guard market close vs resting guard TPs (Trevor 2026-10-29 21:05 MT)

While guard TPs are resting on chain (TP1/TP2 at the guard price, i.e. after a TP3 partial on this trade, and at least one of their trigger requests in this tick's chain `openRequests`), the profit guard does **not** market-close: it logs `profit_guard: met (...) but guard TPs are resting on chain (...) -> NO market close; the TPs handle it`. Only when no guard TP is resting does the guard market-close, still after the fresh on-chain re-check (`guard_recheck_chain`). Applies to the bank guard and the `pg_close_armed` branch, all three bots.

## 1. Goal and philosophy

Grow each bot’s own coin (SOL / Wormhole ETH / WBTC) with small, fee-aware profits. JupSOL is a stake bucket from daily withdrawals, never margin.

**Go for broke:** no stop-loss, no daily loss limit, no collateral or notional cap (`max_*` = `1e12`). Early close only to lock **net profit after fees** (profit guard / trims) or for exchange / infra problems. **Never** close to cut a loss. **No** loss-taking trims (adds only trim when that lot is itself net profitable).

`trend_flip_exit = false`. Open positions ride through 4h trend flips; the EMA200 + SMA10 rule (section 3) only gates **new** entries.

---

## 2. Wallet and per-bot ledger

| Item | Value |
|------|--------|
| Shared bot wallet | `Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx` |
| Fee reserve | `0.02` SOL native (not in any bot’s bank) |
| Withdraw-to address | locked `59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp` (send mode only; daily job stakes/swaps to JupSOL) |
| Deposits | capital, not P&L |

**Per-bot bank** (`state.json` → `bank`, `live/bank.py`, `[ledger]` enabled):

| Bot | Coin | Min profit (guard) |
|-----|------|--------------------|
| SOL | native SOL | `0.001` SOL |
| ETH | Wormhole ETH | `0.000044` ETH |
| BTC | WBTC | `0.0000013` WBTC |

(~$0.11 each.)

- Trades fund from and pay back to **that bot’s balance only**. Longs: own coin = collateral. Shorts: Jupiter swaps own coin → USDC inside the open request; every short decrease pays out in the bot’s own coin (SOL; ETH / BTC via desiredMint), so nothing is swept. USDC dust below $2 is only logged (`sweep: stray USDC`), never swapped; $2 or more is a deposit (next point).
- Superseded by Core design (2026-10-30): bots borrow from each other (loans), never share funds.
- **USDC deposit fold-in (Trevor 2026-10-26, `live/deposit.py`).** USDC >= **$2** in the shared wallet (seen with the same amount on 2 consecutive reads) is a deposit. Only the SOL bot swaps it, inside `sweep()` (needs `sweep_enabled`, live, not kill-switched): equal thirds by atoms into SOL / ETH / WBTC, the remainder atoms to SOL, one Jupiter swap per leg with the normal sweep slippage/impact settings, legs in order SOL, ETH, BTC, at most one leg unresolved at a time. Journal `live/deposit_journal.json` (flock `live/deposit.lock`, fsync + atomic replace; non-blocking run lock `live/deposit_run.lock`): a leg is marked `sending` BEFORE the swap; after a crash it is resolved from the chain (sig -> `tx-deltas`; no sig -> after 300 s the USDC ATA signatures are scanned for that exact USDC amount + coin) and re-sent only when no matching swap exists, so it never swaps twice. A failed leg (retry/fatal) is retried after 60 s; a leg rejected at simulation (send.ts own pre-sign simulation `simulation failed {...}` or the RPC preflight `Simulation failed. ... Transaction simulation failed`, no sig, nothing sent) is a clean failure retried after 60 s too, not the 300 s unknown-outcome path (2026-10-26 fix). A Jupiter slippage failure (6001 = 0x1771 SlippageToleranceExceeded, at simulation or on-chain) retries that leg at 2 x the sweep slippage, capped at `[withdraw] stake_max_slippage_bps` (50 -> 100 bps live); other callers of `swap_with_backoff` are unchanged. Cause (deposit #1, 2026-10-26 21:45-21:58 MT): the $1.67 WBTC leg is only ~2095 WBTC atoms and Jupiter's Manifest USDC/WBTC route fills ~0.5-0.9% below its quote at that size, so 50 bps failed every time it was routed there (simulation at 90 bps passed); landed legs are never redone (partial failure resumes the missing legs only). If the wallet holds less USDC than the unswapped legs need, nothing is sent (WARNING). Logs `WARNING DEPOSIT USDC: $X -> SOL a / ETH b / WBTC c, sigs ...` once all legs landed. **Credit**: each bot, on its own tick (first step of bank_tick), books the **measured on-chain delta** of its own leg (SOL: gross native+wSOL change + network fee; fee stays in the SOL reserve float), never the quote, via `apply_external`: bal, hwm and wd_mark all rise by the same amount (withdrawable / daily withdrawal unchanged), totals ext_in, hist kind external, `trade_start_equity` and `day_start_equity` raised by the same amount (profit guard and daily loss guard unaffected), record in `bank.deposits`; then `WARNING BANK DEPOSIT credited #id ...`, then the journal leg is marked credited (state is saved first; a crash in between is detected by the deposit id in `bank.deposits`, never credited twice). Deferred while the bot has a non-trigger request in flight (works flat or in a trade). While its own leg is in flight or uncredited the bot skips recon and holds its payout window / pay_wait, so a deposit never alerts EXT_FLOW and is never booked as a payout or profit. Loans, fee debt, rescue, TP and add logic are untouched. When all three are credited a line goes to `live/deposits.jsonl` (once per deposit; quickcheck greps `DEPOSIT`). Read-only CLI helpers: `tx-deltas`, `usdc-sigs`. Risk: any USDC >= $2 is treated as a deposit whatever its source.
- *Superseded 2026-10-30 by Core design / `ext_flow_mode = "chain"`; kept for history and for `alert` mode.* **External wallet changes: alert only (Trevor 2026-10-11 19:08 MT).** An unexplained wallet change **≥ $1.5** persisting **2 ticks** is never P&L and is no longer charged to the bots: the bot logs `WARNING UNEXPLAINED WALLET CHANGE`, appends to `live/EXT_FLOW_ALERT` (quickcheck prints `ALERT EXT`), and changes nothing in any bank (no 50/25/25 split, no ETH/BTC coin swaps). `[ledger] ext_flow_mode = "alert"` in all three configs (`"split"` = old rule, off). Resolve by hand (book a correction), then delete the flag file. Smaller SOL noise = fee reserve float. **Shared-wallet request rent (2026-10-15, corrected same day):** the SOL bot's native-SOL check (`bank.recon_sol`) adds back the Perps request-account rent currently locked by ALL bots: `implied reserve = wallet + rent_held - bal - others' shares`, `rent_held` = open request accounts (`allOpenRequests`, any bot's TP1/TP2/TP3/resting add/open) x 0.0037236 SOL (read in the same state call). Each request moves 0.0037236 SOL out of the wallet (refunded on execute/cancel), so the wallet drops and rent_held rises and the two cancel. The first version (56be44e) SUBTRACTED the rent (double counting: a fresh open + 2 TPs + resting add alerted -0.0224 = 6 x 0.0037236, +-0.0149 = 4 x on TP/add churn) and also counted open positions x 0.00174752 (the position PDA persists across trades; an open tx moves only the request rent, so closes/opens alerted +0.0166 = 4 x + 0.00175); both fixed. Baseline `res_v` = 3 (older baselines re-init once at restart). Tx fees / temp Jupiter fees stay float below `ext_min_usd`; collateral movements are in the bank. A real outflow >= ~$1.5 (e.g. 0.02 SOL) persisting 2 ticks still alerts. Only the SOL bot runs this check. Background: 2026-10-11 18:41 MT a stray 0.207 SOL JupSOL stake was split as an external withdrawal and made ETH/BTC sell coin; reversed 19:10-19:15 MT.
- ETH/BTC SOL network fees tracked as a small **fee debt**, settled daily when ≥ `0.005` SOL.
- **HWM** (own coin): only rises, and only while flat (`hwm = max(hwm, bal − fee debt)`).

First-entry sizing uses **10% of the bot’s free bank balance (USD)** (`first_collateral_pct`), floor $10.50, with `$0.10` fee reserve for adds.

**Live since 2026-10-29 21:24 MT (Trevor):** `[entry] first_collateral_mode = "pct"` on SOL, ETH and BTC (was `"fixed"` = $10 from `[sizing]` since 2026-10-12). First entry collateral = max(10% x free, $10.50), no cap, never above free - $0.10. Free = the bot's own bank balance x coin price (the `free $` on the `budget` log line). The ladder budgets from what is left after the open; rungs stay `ladder_fixed_usd` $10.

---

## 3. Entries

**Entry rule (Trevor 2026-10-29 20:52 MT, all three bots, `[signal] entry_rule = "price_ma"`):** two signals, both from **price vs the line, never the slope**. Price = the latest closed 15m candle close (the same value for both comparisons).

| Signal | Price > line | Price < line | Price == line |
|--------|--------------|--------------|---------------|
| Long-period: 4h EMA200 | long signal | short signal | no signal |
| Short-period: 15m SMA10 (`entry_sma_length = 10`) | long signal | short signal | no signal |

| 4h EMA200 | 15m SMA10 | Action |
|-----------|-----------|--------|
| long | long | **open LONG** |
| short | short | **open SHORT** |
| long | short | no entry (disagree) |
| short | long | no entry (disagree) |
| either equal | any | no entry |

- RSI and the idle RSI loosening are **not** used for entries (the code is kept for backtests: `entry_rule = "rsi"`; the RSI/idle keys in the live tomls are inert). Adds, ladder, TP1/TP2/TP3, guard, leverage, timer, HWM, withdrawals and loans are unchanged.
- `sides = "both"` on all three bots (was `"short"`, an analyst bias). The levels / outlook script (`ratchet-levels`, red / amber / `pause_new_opens` / hold / stay-out, side bias) **no longer blocks or alters entries**; it is used for exit / position management only. Do not set `sides` or `PAUSE_UNTIL` from an outlook call.
- `PAUSE_UNTIL` / `KILL` / `KILL_ALL` / lender lock still block opens (operator / safety switches, not outlook).
- Log: each bar line shows `ema200_4h=... sma10_15m=...`; when flat the next line is `entry: entry check: price P vs 4h EMA200 E -> long|short; vs 15m SMA10 S -> long|short => OPEN LONG | OPEN SHORT | stay flat (...) (RSI off)`.

---

## 4. Leverage

| | Value |
|--|--------|
| First entry | **5x** (`lev_start`) |
| Each add | **+1x** (`lev_step`) |
| Cap | **20x** (`lev_cap` / `max_leverage`) |

Open trades keep current size; the next add uses step/cap from `last_leverage`.

---

## 5. Adds (DCA ladder)

`dca.mode = "levels"`. Analyst S/R (main 1h/4h merged):

| Bot | Levels file |
|-----|-------------|
| SOL | `/workspace/market-analysis/latest.json` |
| ETH | `/workspace/market-analysis/eth/latest.json` |
| BTC | `/workspace/market-analysis/btc/latest.json` |

| Rule | Value |
|------|--------|
| Stale / missing | `levels_max_age_min = 45` → fixed spacing (ETH/BTC: own-ATR fallback, no S/R) |
| Gap to last fill | `0.5`–`2.0` × 1h ATR |
| Fallback spacing | SOL `$0.85`; ETH/BTC `1.15 ×` own 1h ATR |
| Rungs | `ladder_min_rungs = 4`, `ladder_max_rungs = 12`, weight mult `1.5` |
| Deep level | deepest support `6`–`12` ATR below first entry, else first − `8` ATR; anchor = first entry |
| Min add | $3; liq ≥ `2.0` ATR beyond next rung or add shrinks/skips |
| Signal on add | off; spaced from `last_entry` |

Ladder budgets remaining free (own-coin) collateral across rungs. Insufficient own balance → add skipped.

---

## 6. Take-profit

Two on-chain TPs: **TP1 closes 50%**, **TP2 the rest**. Calculated at **open and on each add**.

**Placement (Trevor 2026-10-15):** TP1 = avg x (1 -/+ 0.8%), TP2 = avg x (1 -/+ 1.5%), all bots (see "TP rules" at the top). Fee floors, TP1 >= `tp_min_gap_usd` inside TP2 and the market clamp still apply. Example: SOL short avg 104.3207 -> TP1 103.49, TP2 102.76.

**Who moves TPs:** set at open/add. **No automatic guard ever moves TPs.** Only **Grok Bot** moves TP2 from the analyst read. No fade (removed 2026-10-15). Grok Bot also manually closes partials the bot misses. **TP2_SET** (Trevor 2026-10-13 20:57 MT): Grok Bot moves TP2 proactively by writing a price to `live/TP2_SET` (`live/eth/`, `live/btc/`). Next tick the bot moves the resting TP2 trigger in place (Jupiter `updateDecreasePositionRequest2`, same size; 2026-10-14) to that price clamped to >= the TP2 fee minimum and beyond TP1 + `tp_min_gap_usd`; recorded as `tp2_override` in state so restarts / resizes / re-places keep it until the next add re-places TPs normally. File deleted after use; invalid content is logged and deleted (never halts/KILL). Log tag `TP2_SET`. **TP3_SET** (Trevor 2026-10-14): same for the resting TP3 (partial) via `live/TP3_SET` (`live/eth/`, `live/btc/`): one price, moved in place, no fee clamp (since 2026-10-15), must be beyond market else warning + nothing changed; updates the partial record's `px` and marks it `manual`, so `partial_sync` keeps that price (no hysteresis re-plan back to its own target; 2026-10-29) until the next add / resize changes the TP3 size, which re-plans TP3 normally. Log tag `TP3_SET`.

**TP timer:** `close_remainder_after_min = 55`, counted from the first TP1 fill (`first_tp_time`). It closes only IN PROFIT (Trevor 2026-10-14): price beyond the average entry plus fees, i.e. `net_close_pnl > 0` (after accrued borrow, close fee and slippage; open fees are already sunk) with `[tp] timer_require_profit = true`, plus the HWM check. Exactly what the code does:
- **Live per-tick path** (`live/bot.py` tick): after 55 min, closes only if `net_close_pnl > 0` **and** `hwm_check(..., 0)` (equity-if-closed incl. the resting add >= HWM; `pg_close_ok` when the bank is off); otherwise logs `timer expired ... holding` and keeps TPs / DCA active.
- **Bar-close path** (`strategy.exit_intents`, backtests / bar logic): closes on `net_close_pnl > 0` alone, with NO HWM check.
- **Discrepancy (noted, not changed; Trevor 2026-10-14 decided the timer behaviour stays):** the bar-close path lacks the HWM gate the per-tick path has. The live bot's per-tick path is the one that governs live closes; the intent in both is "close only in profit".

---

## 7. Per-add trim (`trim_trigger = distance_level`)

All three bots. Old 4h timer only applies in unused `trim_trigger = "time"` mode.

1. **Arm** when price is **≥ 0.65%** against average (`trim_arm_dist_pct = 0.0065`, Trevor 2026-10-12 20:29 MT, back from 1%; long ≤ avg×0.9935, short ≥ avg×1.0065). With `[resting] partials_enabled` (live) the arm only decides TP3; no market trim is sent. Stays armed until the trade closes (`state.json`).
2. **While armed:** fire slightly **before** the next analyst level — front-run **0.15 ATR + 0.1 ATR tolerance** — using main 1h/4h levels **plus** `levels.fine` (15m/1h intraday, prior-day H/L, round numbers). Close every profitable **add** (never the first entry) with **≥ 0.5%** gain and **≥ $0.25** net (6+6 bps + `$0.02` network) in one market decrease.
3. Collateral **stays** in the position. Liq does not move on partial close; later adds push it out. Rungs re-arm; TPs re-placed for new size.
4. **Skip** if level files **&gt; 45 min** stale.

Jupiter books trims against the average; bot tracks true per-add gain separately.

---

## 8. Profit guard (HWM)

**Trevor 2026-10-14 - per-trade guard (replaces the HWM target and the underwater gate)**: the guard market-closes the whole position (adds/trims trades only, `close_requires`) once `hwm_equity()` (equity if closed now, incl. a resting add's collateral) >= `trade_start_equity` x (1 + `[profit_guard] min_profit_pct_trade`, code default 0.002 = 0.2%, plain, no per-partial uplift). `trade_start_equity` (state/bank) = `bal - fee_debt - loan + ib_adj` immediately before the opening debit of a trade opened from flat (set in `open_or_add`; also on adoption); it persists in state.json and is overwritten by the next trade. HWM/wd_mark play no part in the test, and the guard no longer depends on price vs avg (`guard_only_underwater` is obsolete; the key stays in the tomls only so they load). A position open without a baseline (deploy) gets a one-off logged `MIGRATION` = bank hwm (HWM only rises while flat, so it equals the flat equity at that open; BTC 2026-10-14: 0.000707734 = bal 0.00070939 - fee debt 0.000001656). The 55-min TP timer is unchanged (`hwm_check(..., 0.0)`: close only if equity >= HWM). TP1/TP2 stay as backup, never moved by the guard. The old `guard_min_native`/`min_profit_native`/`min_profit_pct_*` ledger keys are unused by the live guard.

**Trevor 2026-10-11 - HWM reset on nightly withdrawal** (already implemented, all bots via `bank.apply_withdrawal`): when the daily job withdraws X of a bot's coin to JupSOL, bal and hwm both drop by X and wd_mark = the new hwm, so the guard never chases withdrawn money (e.g. SOL 10-11 00:05 MT: hwm 0.7879 -> 0.7832).


When the trade had **adds or trims** and equity-if-closed-now ≥ **HWM + min profit** (own-coin amounts above) → **market-close the whole position**. Never moves TPs. TP timer will not close below HWM. First-entry-only trades ride their TPs.

Close value in own coin: collateral + PnL − fees − slippage (− ~0.2% USDC→coin est. on ETH/BTC shorts).

---

## 9. Daily withdrawal and ops cadence

**Trevor 2026-10-11 06:53 MT - no fixed minimum:** the old $0.50 minimum is gone. The 00:05 MT withdrawal (`withdraw_fraction` of HWM gain; 0.50 originally, now SOL 0.65 / ETH 0.65 / BTC 0.70) is sent whenever amount_usd > `withdraw_fee_factor` (2) x estimated cost, where cost = amount_usd x `withdraw_cost_pct` + `withdraw_network_sol` x SOL price (SOL->JupSOL: 0.1% + 0.00005 SOL; ETH/WBTC->JupSOL: 0.3% + 0.0001 SOL). Smaller amounts are carried forward as before. Same logic on all three bots.


| Cadence | Action |
|---------|--------|
| Daily **00:05** MT | Per bot: `withdraw_fraction` (now **SOL 0.65, ETH 0.65, BTC 0.70**, nudged +0.05 nightly at 00:23 MT by Grok Bot) of HWM gain since last withdrawal → **JupSOL** (SOL stakes; ETH/WBTC swap coin → JupSOL directly). Balance and HWM reduced by the amount; &lt; **$0.50** carries over. Each bot’s balance logged in coin and USD. ETH/BTC settle SOL fee debt when due. |
| Sundays **18:40** MT | Weekly SOL / JupSOL rebalance (ops). |
| When flat / safe | Recover empty ATA rent into JupSOL when appropriate. |

---

## 10. Oversight

| Who | Cadence | Notes |
|-----|---------|--------|
| Analyst | Levels every **~15 min** (free refresh); news / events ~**every 2h** | Urgent pings only |
| Grok Bot | Hourly overseer checks (see "Overseer checks (Grok Bot hourly)"); the older 3h check-in is superseded | Message owner only on fills / adds / trims / closes /halts / errors; may move TP2 or close missed partials |

**Quoting:** read **live on-chain** before quoting size. Give jup.ag-style **size** plus **collateral at risk**.

---

## 11. Safety

- Private key never printed or logged (loaded in-process only).
- Backups before config/code edits (`*.bak-…`, tarball when large).
- `state.json` / `bank` kept across restarts.
- Strategy / ops notes → `live/changes.log`.
- Tests never touch chain (`conftest` redirects / blocks).

Kill files: per-bot `live/{,eth/,btc/}KILL`; global `live/KILL_ALL`. `close_all_on_kill = false`. Manual operator stop only: since 2026-10-12 08:46 MT the bot never writes KILL itself; failures are logged as ERROR and reconciled on the next tick (see below).

---

## 12. Change history (2026-10-08)

- **13:54** — Leverage **5x / +1x / cap 20x** on all bots.
- **17:11–17:18** — Trim → **distance + level** (front-run 0.15 ATR) on all three; first wallet-baseline profit guard.
- **18:02–18:04** — Profit guard: **never moves TPs**; action = market-close when met (`adds_or_trims`). Arm distance **3% → 1.5%**.
- **18:25** — Only Grok Bot moves TP2 (bot fade kept); TPs set at open/add only.
- **18:32** — Trim uses analyst **`levels.fine`** (15m/1h, PDH/PDL, rounds) plus main levels.
- **21:48** — **Per-bot own-coin ledger + HWM** (`live/bank.py`); daily 50% HWM→JupSOL; guard = equity ≥ HWM + min profit; no cross-bot borrowing.
- **22:15** — Trim arm **1.5% → 0.85%** on all bots.

---

Last updated 2026-10-08 22:27 MT

## JupSOL rescue (Trevor 2026-10-11; reworked 2026-10-27)

When a bot's open position is within **3%** of liquidation (`live/rescue_cfg.json` trigger_pct; the distance uses the
collateral minus the **accrued borrow fee**), JupSOL is **lent** to that bot (the one exception to "JupSOL is never margin").
**2026-10-27 (Trevor):** collateral only (no bank credit split): ONE ExactOut JupSOL -> own-coin swap (no stranded SOL leg)
for min(collateral that moves liq to `target_pct` 10% away, **1/3** of the usable JupSOL after the 1% `haircut_pct`), all of the
**measured** coin deposited as collateral (increase, sizeUsd 0). EMERGENCY (notify Trevor) when that amount moves liq by less
than `emergency_min_push_pct` 2 points. Booked as a **liability** `bank.loan` (coin received = principal, JupSOL measured out =
what is owed): excluded from HWM, profit-guard equity, guard target and withdrawals. Repayment: the **exact JupSOL quantity lent**
(ExactOut own coin -> JupSOL; no 5% margin, no profit share) when the bot is flat, after any inter-bot loan, never while a
reconcile / own-coin booking / SOL true-up is pending. If the balance cannot buy it all back, what it buys is repaid and the rest is
carried to the bot's **next** flat (deposits do not pay it while it is still flat); nothing is ever written off. Settings
extended for the rescue are restored on full repayment. Out of rungs -> `rescue.sh extend` / `rescue.sh add` (manual only).
Automatic version: `[ledger] jupsol_loans` (below); manual tool: `live/rescue.sh` (dry run by default); steps: `RESCUE_PLAYBOOK.md`.

### Automatic JupSOL loans (Trevor 2026-10-27, `live/jupsol_loans.py`, `[ledger] jupsol_loans` = off | observe | on, default off)
- **Rescue** as above, automatic in `on` (observe: logs `JUPSOL observe: would RESCUE ...`). KILL present -> skipped and logged.
  A bot in trigger registers in `live/jupsol_loans.json` (`rescue_want`) and every bot's next-add loan waits (rescue first).
- **Next-add loan**: when the next resting rung needs more than the free balance, peers go first exactly as today (the
  inter-bot request is filed). Only when no peer can cover it (no flat peer with free >= max($2, need) + $5, or another pair's
  loan record busy; checked on 2 consecutive ticks; or `<state dir>/IB_BORROW_OFF` present) JupSOL lends **exactly the shortfall
  + $1** (rung margin - free + $1, no minimum, no cap) and only if the whole amount is usable; the bot's own inter-bot request is
  withdrawn first (a peer that already started funding wins). The add is then placed by the normal resting-add path.
  **2026-10-28 (SOL fee reserve):** the shortfall is max(rung margin - free, SOL fee-reserve gap) + $1, where the gap (SOL bot
  only, `Bot.add_reserve_short_usd`) = (rung margin / SOL px + `sol_fee_reserve` - native wallet SOL) x SOL px, i.e. what the
  add guard's `native SOL - need >= sol_fee_reserve` check is missing. A rung the free balance covers but that only the fee
  reserve blocks (`resting add skipped: would breach SOL fee reserve`) takes the same JupSOL path (same peer / 2-tick rules, no
  inter-bot request filed). A second loan adds to the open liability (principal and JupSOL owed summed, one tranche each); the
  flat repay buys back the total JupSOL owed. No want is decided in the tick a loan was booked (its wallet snapshot predates the
  loan). ETH / BTC: unchanged (gap always 0).
- **Booking**: measured tx deltas (quote only as a logged fallback); SOL network fee via bank_fee (ETH / BTC fee debt);
  `live/stake_ledger.csv` rows `loan_out` / `loan_repay` (+ totals); SOL open trade: `ledger.update(external_out_sol)` so the
  wallet-delta fallback never sees the loan as P&L; `day_start_equity` moved by the same USD so the daily-loss guard is
  unaffected. Every send is journalled first in `bank.jl_op`; an unknown outcome is resolved from `tx-deltas` (never re-sent).
  The tick that swapped holds the wallet reconcile. **Invariant**: borrow and repay leave equity, hwm, wd_mark, guard target and
  withdrawable unchanged (only the JupSOL price move / swap cost between lend and repay lands in bal, as with inter-bot loans).
- `extend` / manual `add` stay manual. `<state dir>/JUPSOL_PARK` (park / unpark own coin in JupSOL as a receivable) exists for
  the live test only (docs/JUPSOL_LIVE_TEST.md). Parked JupSOL is never lent.

## Withdrawal balancing (Trevor 2026-10-11 08:43 MT)
- Each bot has its own `[ledger] withdraw_fraction` (share of HWM gain sent to JupSOL nightly at 00:05 MT); all started at 0.5; **now SOL 0.65, ETH 0.65, BTC 0.70** (2026-10-14). No $0.50 minimum: a withdrawal goes whenever it beats its swap costs.
- Grok Bot nudges these nightly at 00:23 MT: +0.05 on all while JupSOL < the largest single bot; -0.05 on all once JupSOL > all bots combined; an extra +/-0.05 on any bot drifting from the 50/25/25 split. Bounds 0.2-0.9. Every change logged in live/changes.log. Current: SOL 0.65, ETH 0.65, BTC 0.70, the +0.05 nightly nudge continuing while JupSOL < the largest single bot.
- Goal: JupSOL at least the size of any one bot, so it can serve as the liquidation rescue reserve.

## JupSOL rescue, live test (2026-10-11 08:39 MT)
- SOL short: 0.0015 JupSOL -> 0.00182 SOL added as collateral; collateral $10.45 -> $10.65, liq 130.12 -> 130.54; size/TPs/HWM unchanged; loan open, auto-repay +5% to be confirmed on close. ETH/BTC paths (extra swap) not yet tested live.

## Partials: no minimum (Trevor 2026-10-11 14:03 MT)
An add qualifies for a partial as soon as its own gain net of open+close fees and network is > 0. No per-trim USD minimum and no % minimum (trim_min_net_usd = 0, trim_min_gain_pct = 0, all bots). Do not reintroduce a minimum without Trevor.

## Partial trigger offset (Trevor 2026-10-11 20:48 MT)
Partials fire 0.05 x 1h ATR before the support/resistance level (trim_level_tol_atr 0.05, trim_level_front_run_atr 0; was 0.25 ATR total). Adds unchanged (fire at the rung).

## Tick interval (Trevor 2026-10-11 21:02 MT)
Bots tick every 15 s (was 60). 5 s was tried and hit RPC 429 rate limits.

## Resting orders: limit adds, per-add partials, last-partial rule (Trevor 2026-10-11) — LIVE since 2026-10-12 00:30 MT
Built 2026-10-11 23:15 MT, approved spec; `adds_enabled`, `partials_enabled` and `last_partial_rule` are **true** on all three bots since 2026-10-12 00:30 MT (partials = TP3, see below). Config section `[resting]`:
- `adds_enabled` (default false): the next ladder add rests as a Jupiter **limit-open order** at the rung price, same collateral / leverage (+1x per add, cap 20x) as the ladder plans now; the order sits `[dca] add_frontrun_atr` x ATR before the rung (2026-10-15/24, see below). Built by Jupiter's hosted Perps API (`POST /orders/limit`), decoded + checked locally (`live/src/limit.ts`: payer = owner, only Perps/system/token/ATA/compute/oracle programs, side/size/trigger/collateral match), signed by the droplet wallet and submitted via `POST /transaction/execute` (keeper co-signature). Only one resting add at a time; never modified in place (moved only by cancel + re-place: TP3 re-arm, level re-plan 2026-10-24); after a fill the next missing rung is placed. Resting adds are cancelled (API `DELETE /orders/limit`) only when the trade closes or the side changes (Jupiter keeps limit orders after a close). Refused creation (e.g. utilisation > 80 %, Jupiter's **$10 minimum collateral per limit order**) is logged and retried next tick, no fallback. The market add on the bar is not used while on. Fills are booked like a market add (lot, leverage step, ledger, bank debit at placement / refund on cancel, profit-guard deposit) and the TP1/TP2 ladder is re-placed as after any add.
- `partials_enabled` (default false): **ONE partial TP per position** (Trevor 23:09/23:10 MT), a trigger decrease placed when the trim is **armed** (existing arm rule, `trim_arm_dist_pct`) **and** at least one add is trim-eligible (`trim.select_lots` at the current price; never the first entry). Size = the eligible adds' total; target = the N-th trim level (main + fine) beyond their size-weighted entry in the profit direction, N = round(`partial_levels_min` + (max − min) × (rung − 1)/(`ladder_max_rungs` − 1)) for the highest rung covered (1 → 4 over 14 rungs); fires `trim_level_tol_atr` (+ trim front-run, now 0) before the level; must cover fees (6+6 bps + `trim_network_usd`), else the next level out is used. Not touched otherwise (no toggling with the arm state); when a new add fills it is cancelled + replaced to also cover that add. Leftovers after a full close are cancelled.
- Only ONE resting add order at a time (Trevor 23:11 MT); after it fills the next one is placed.
- Jupiter limits found: limit orders need **≥ $10 collateral each** (API `collateral_size_below_minimum`; the live ladder's $3 rungs are refused → with adds_enabled those rungs would never be placed, only logged/retried); max 20 limit orders per market+side; refused when utilisation > 80 %; ~0.0052 SOL request rent per order (refunded on fill/cancel).
- `last_partial_rule` (default false): the price of the last partial (per-add partial fill or trim exit) is recorded; the next add order is planned from the more adverse of the last fill and that price and must lie beyond it.
- TP1 / TP2 logic unchanged (its re-place never touches resting adds or per-add partials).


## Lot recovery after an unsaved add (2026-10-12)
Before a market add is sent the bot saves an `add_pending` marker; once the fill is booked the lot is saved and the
marker cleared. Every tick, if the on-chain size is larger than the lots total, the bot reads the position's recent
Jupiter IncreasePositionEvents and rebuilds the single unrecorded fill whose size equals the difference exactly (fill
price / size / collateral from the event), booking it like a normal add (fresh TP1/TP2). If there is no exact single
match nothing changes and trims keep skipping on the mismatch. Resting limit-add fills ([resting], live) are saved
the moment they are booked and are never rebuilt twice.


## Ladder preset C (2026-10-12, LIVE since 00:30 MT)
`dca.ladder_fixed_usd` (default 0 = off): when > 0 every add rung gets exactly this collateral instead of the weighted
split. Preset C (configs/preset_ladder_c.txt): $10 open (entry.first_collateral_mode = "fixed", sizing fixed_usd $10),
$10 per rung (ladder_fixed_usd 10, min_add_usd 10), leverage 5x +2.5x per add capped at 20x, 10 main rungs, no deep
rung. Rungs the free collateral cannot cover are simply not placed ($10 minimum = Jupiter's limit-order minimum).


## TP3 = the only partial mechanism ([resting] partials_enabled, LIVE 2026-10-12 00:30 MT)
When partials_enabled is on, the single partial TP order per position (TP3) is the only way lots are partially closed.
The old market trim still arms (0.65% against avg; was 1% 2026-10-12 06:22-20:29 MT) and computes eligible adds - that decides when / for which adds TP3
is placed - but it never sends a market close. TP3 is cancelled and re-placed when a new add fills. TP1/TP2 unchanged.
live/manual_trim.sh <bot> --send refuses while a TP3 order is live (dry run works).

## Profit guard target (superseded 2026-10-14)
Was HWM x (0.2% + 0.05% x partials). Now per trade: see section 8 (trade_start_equity x 1.002).

## Ladder preset C (2026-10-12, LIVE since 00:30 MT)
`dca.ladder_fixed_usd` (default 0 = off): when > 0 every add rung gets exactly this collateral instead of the weighted
split. Preset C (configs/preset_ladder_c.txt): $10 open (entry.first_collateral_mode = "fixed", sizing fixed_usd $10),
$10 per rung (ladder_fixed_usd 10, min_add_usd 10), leverage 5x +2.5x per add capped at 20x, 10 main rungs, no deep
rung. Rungs the free collateral cannot cover are simply not placed ($10 minimum = Jupiter's limit-order minimum).


## TP3 = the only partial mechanism ([resting] partials_enabled, LIVE 2026-10-12 00:30 MT)
When partials_enabled is on, the single partial TP order per position (TP3) is the only way lots are partially closed.
The old market trim still arms (0.65% against avg; was 1% 2026-10-12 06:22-20:29 MT) and computes eligible adds - that decides when / for which adds TP3
is placed - but it never sends a market close. TP3 is cancelled and re-placed when a new add fills. TP1/TP2 unchanged.
live/manual_trim.sh <bot> --send refuses while a TP3 order is live (dry run works).

## Profit guard target as % of HWM (LIVE 2026-10-12 00:30 MT)
target = HWM x (min_profit_pct_hwm 0.2% + min_profit_pct_per_partial 0.05% x partials taken this trade
[market trims + TP3 fills]). Replaces the fixed min_profit_native amounts (used only if the pct is 0). Still a market
close of the whole position, still only while underwater.

## Ladder preset C + resting orders: LIVE since 2026-10-12 00:30 MT on all three bots.


## TP3 = ONE partial on the adds that net a profit at avg + f x (furthest add - avg) (Trevor 2026-10-15; replaces the zone-edge / fee-minimum / snap rules)
D = |furthest open add entry - trade avg entry| (the deepest add; avg = on-chain average). **Target = avg + f x (furthest add - avg)**, rounded to
the cent: a point f of the way from the avg toward the furthest add. f tapers linearly with the **rung** = number of OPEN adds (the first entry is
not an add): `f = f_first - (f_first - f_deep) x min(1, (rung-1)/(ladder_max_rungs-1))`, `[resting] partial_zone_f_first` (**0.50**, rung 1) and
`partial_zone_f_deep` (**0.27**, rung >= ladder_max_rungs). At that price every add lot that nets > 0 AFTER fees (open_fee + close_fee) and the
network estimate (`trim_network_usd`) is covered; **TP3 size = the sum of those adds only**, ONE order. No add nets a profit (rung 1 normally) = no TP3.
Rung tracking = the open add lots in `local.lots`: an add fill appends one (rung + 1); a TP3 fill removes the closed lots (rung drops, the same
closed-lot tracking that re-arms the resting add, be77f08). Removed: `tp3_fee_mult` and the fee-minimum price push, `partial_snap_levels`
and level snapping, the arm / trim-eligibility gate on placing TP3. `partial_sync` runs every tick and right after an add: TP3 is placed when missing,
cancelled + re-placed when its size or price change (new add, avg / rung change, position shrank - size is capped at the position, adds closed by a
TP3 fill), cancelled when no add nets a profit any more; a changed target not beyond the market leaves the old order alone. TP1 / TP2 and the
profit-guard move after a TP3 fill are untouched. TP3_SET now only needs a price beyond the market (no fee clamp).
Example (ETH, avg 2425.74, adds 75@2390.87 100@2409.51 125@2441.00 150@2462.79): rung 4, f 0.4233, target 2441.42, only the $150 add qualifies.

### TP3 minimum net per partial ([resting] partial_min_net_pct = 0.002, Trevor 2026-10-27 09:19 MT, all three bots)
The f target alone made rung-1 partials tiny: a $75 add closed ~0.2% below its entry, ~$0.13 of fees per add+partial cycle left
$0.00-0.05 net (SOL 10-27: 3 cycles netted ~$0.05 on ~$0.53 gross). Now the target is at least far enough that the **furthest add lot nets
>= partial_min_net_pct x its own notional after open + close fees and network**: `cap = far x (1 -/+ (pct + open_fee + close_fee +
network / far_size))` (short below, long above; rounded to the cent in the profit direction), and TP3 = whichever of the f target and the cap is
further from the add. 0.002 = **$0.15 on a $75 add** (~0.35% below a short's add, e.g. add 100.70 -> TP3 <= 100.35 instead of 100.49), scales with
the lot. f (0.50 -> 0.27) is unchanged and still wins at deeper rungs where its target is already further. Coverage / size rule unchanged (adds
netting > 0 at the target). 0 = off. Code: `live/resting.py` `tp3_px` / `far_size`, used by `tp3_plan` and `tp3_skip_why`.

### Re-add spacing after a TP3 fill ([resting] readd_gap_atr = 0.6, Trevor 2026-10-27 09:19 MT, all three bots)
Before: the re-arm went to the closed lot's entry and the order then got the 0.1 ATR add front-run again, so each re-add sat one front-run nearer the
market (SOL 10-27: 100.70 -> 100.62 -> 100.55 -> 100.47, only 0.07-0.13 above the partial) and each cycle's net shrank toward zero. Now, set ONCE at
TP3-fill time (`Bot.rearm_px`, stored in `L.rearm["px"]`): **re-arm price = the more adverse of the closed lot's entry and the last partial price
+/- readd_gap_atr x ladder (analyst) ATR** (short: higher, long: lower; own ATR if the analyst ATR is missing), rounded to the adverse cent so it
is never inside the closed lot's entry, and the order is placed **exactly there (no front-run)** (`rest_add_args`). The resting tick's re-arm check
compares the order with that same stored price, so the order is placed once and not cancelled / re-placed again. Everything else unchanged
(re-arm only while the market is on the favourable side of it; otherwise the normal ladder plan from the more adverse of last fill and last partial).
0 = off (old rule). A re-arm recorded before this deploy keeps its old price (its fronted order is left alone).
**2026-10-27 13:50 MT (Trevor)**: the re-arm price is also never inside `[dca] min_place_gap_atr` (0.85) x ladder ATR beyond the REMAINING last entry (the last lot left after the TP3 fill; long: below it): re-arm = the most adverse of the closed lot's entry, last partial +/- readd_gap_atr x ATR and remaining last entry +/- min_place_gap_atr x ATR, rounded to the adverse cent, set once at fill time (`Bot.rearm_px(rest_last)`), stored in L.rearm px and placed exactly (no front-run, no add_px clamp on top). Same stored price for resting_tick / rest_add_plan / rearm_add -> no churn; a re-arm stored before the deploy is not recomputed.

### TP1/TP2 re-size after a TP3 fill (Trevor 2026-10-12 06:07 MT)
When a TP3 (per-add partial TP order) fills, the pending TP1/TP2 trigger orders are re-placed at the same prices, sized on the remaining position with the original % split (e.g. TP1 = 50% of remaining). Nothing else changes.

### TP3 fill seen as a size drop before its order record disappears (Trevor 2026-10-28 13:49 MT, all three bots)
The chain state can show a TP3 (per-add partial) fill as a position size drop while the TP3 request account is still listed
(BTC 2026-10-28 12:46 MT: booked as a plain TP hit `tp_hits=1`, the add lot kept; 12:47 "gone without a size drop: records dropped";
a new $50 TP3 then sat on the first entry and closed the trade at a small loss). Now `resting_reconcile` (`Bot.early_partial_fill`):
a size drop while the TP3 record is still on-chain that matches it - closed size = its size (+-max($0.05, 0.5%)), or the market
reached its trigger (tolerance max(tp_market_gap, 0.05%)) with no pending TP1/TP2 trigger reached and at most its size closed - is
booked (never when a TP1/TP2 trigger was crossed but the TP3 one was not) as that TP3 partial exactly like a normal TP3 fill (add lot(s) removed, first entry and other lots kept, payout, partials_done,
last partial price, re-arm, guard latch, TP1/TP2 resized to the remaining position) and NOT as a TP hit (`tp_hits` unchanged). The record
stays in `local.partials` flagged `booked` until its request account disappears, then it is removed quietly (no "records dropped", no
second booking); while flagged, `partial_sync` neither cancels nor re-places TP3, TP3_SET ignores it, and the TP1/TP2 ladder never
touches it. Normal ordering (record gone with the size drop) is unchanged. Any other size drop is an ordinary TP1/TP2 fill.

### Resting add fill seen as size growth before its order record disappears (Trevor 2026-10-28 14:15 MT, all three bots)
The chain state can show a resting add fill as position size growth while the add's increase request is still listed (SOL
2026-10-28 14:01 MT: the $125 add @ 103.3 had filled, size 225 -> 350, but its record was still listed, so the fill was not booked
and `hwm_equity` counted the add's collateral twice - in the close value and again as the resting-add term, ~+0.0976 SOL - and the
profit guard closed SOL on that false equity). Now `resting_reconcile` (`Bot.early_add_fill`, mirror of `early_partial_fill`): growth
while the record is still on-chain that matches the add - grown by its size (+-max($0.05, 0.5%)), or the market reached its trigger
(tolerance max(tp_market_gap, 0.05%)) with growth above half of it - never more than the add - is booked at once exactly like a normal
resting add fill (new lot, fill price from the avg change, n_entries, leverage step, guard latch, fresh TP1/TP2 ladder and TP3). The
record stays in `local.rest_add` flagged `booked` until its request account disappears, then it is removed quietly (no second
booking, no refund); while flagged it is not in `hwm_equity`'s resting-add term, is never refunded, and no new resting add is placed,
re-planned or re-armed. `hwm_equity` also never counts a not-yet-booked resting add whose fill is already in the position size
(size above the booked size by more than half the add). Normal ordering (record gone with the growth) is unchanged.

### TP3 arm distance 1 % (Trevor 2026-10-12 06:22 MT)
trim_arm_dist_pct = 0.01 on all three bots (was 0.0065): trim/TP3 arms once price is > 1 % against the avg.

### Unconfirmed cancels never halt (Trevor 2026-10-12 08:28 MT)
A cancel (resting add, TP, leftover order) that does not confirm is logged and retried every tick while the order is still on-chain; once gone any resting-add refund is booked and TP1/TP2 re-placed if needed. No KILL. KILL stays for opens/increases/unknown positions.

### The bot never stops itself (Trevor 2026-10-12 08:46 MT)
No automatic KILL. Unconfirmed open/add -> next tick reads the chain (late fill booked via lot recovery / late open adopted, else normal logic). TP placement failure -> TP1/TP2 re-placed next tick (TP3 re-placed each tick as before). Unconfirmed close / trim -> next tick reconciles. A position the bot did not open (incl. manual) is ADOPTED as one lot (chain side/size/avg/collateral), TPs placed, managed normally. All logged as ERROR. Manual KILL / KILL_ALL files still stop new orders.

### No double add / adopted deposit (Trevor 2026-10-12 08:52 MT)
While an add/open is unresolved (add_pending), no new add (market or resting) is sent until the chain shows it landed (booked via lot recovery / adoption) or absent with no increase request pending. An adopted position collateral is booked as a trade deposit in the bot own coin at the adoption price (HWM unchanged).

### Inter-bot loans (Trevor 2026-10-12 18:09 / 18:13 MT)
A bot without enough free equity for its next resting rung requests a loan (shortfall + $1, min $2) through live/ib_loan.json. **2026-10-25 (Trevor, fix)**: the shortfall is the next rung's normal collateral planned with a sane budget (search: free cash + 1 rung, + 2 rungs ... up to `IB_PROBE_STEPS` 100 steps, a search bound only; step = max(`min_add_usd`, `ladder_fixed_usd`) = $10 live; the first plan found is the real next rung), never the old cash + 1e9 probe (that sized the rung from the 1e9 budget: SOL asked for $180,893,111 and no lender could fund it). The real rung's shortfall is requested in full: NO cap (Trevor 2026-10-15, re-confirmed 2026-10-25; a 5-rung cap added earlier on 2026-10-25 was removed). An own `requested` record is withdrawn (-> `settled`, `cancelled_need_usd` kept) when the borrower is flat or its next rung becomes affordable without a loan (a pending own top-up -> back to `open`). Any flat bot without a loan funds it by swapping its own coin into the borrower coin. The lender opens nothing until repaid. On the borrower full close it swaps back the quantity lent (swap cost on the borrower); the lender books it and is re-enabled. Loans never move HWM, equity or withdrawable amounts on either side. One loan at a time (live/ib_loan.json: requested -> funding -> funded -> open -> repaid -> settled). **Top-up (Trevor 2026-10-15)**: a borrower with an open loan asks its lender for more instead of being blocked: status open -> `topup_requested` (need = shortfall + $1, min $2, no cap; `topup_prev` keeps sol/coin/need_usd) -> `funding` -> `funded` -> open; the lender (same pair only, flat, no position, not busy, window 0, balance >= amount + $5) swaps and adds to its lent quantity (`sol`, lender coin) and `coin`; the borrower books only the delta. `ib_loan.sol` is therefore the cumulative lender-coin quantity and repayment stays one ExactOut swap for all of it (price moves between top-ups only change the borrower's cost). HWM / wd_mark / trade_start_equity are never touched (bal and ib_adj move together). A borrower that goes flat (or ~30 min without funding) cancels the pending top-up back to `open` and repays; a `funding` top-up stuck > 30 min (crash mid-swap) is reverted to `open` with an EXT_FLOW_ALERT to check the wallet. Old open records without topup keys stay valid. A lender must be flat with no loan of its own and keep >= $5 spare beyond the loan. Repayment targets the lent quantity at oracle prices + 0.4% buffer; the first repayment came back 0.000964 SOL short (swap price below oracle), which stayed with the borrower. Since 2026-10-13 (Trevor) repayment is a Jupiter ExactOut swap for exactly the owed lender quantity (borrower coin in = whatever it costs, swap cost on the borrower; only the usual slippage/impact caps, no extra min-out floor). The received amount is verified from the tx balance deltas (SOL: network fee excluded, it is booked on the borrower); if still short the loan stays open with the remaining shortfall (ib_loan.repaid_got accumulates) and is topped up next tick before status -> repaid. Never a halt. Errors are logged and retried; never a halt. First loan: SOL -> BTC 0.478070233 SOL ($50) 18:13 MT.

### TP3 arm distance back to 0.65 % (Trevor 2026-10-12 20:29 MT)
trim_arm_dist_pct = 0.0065 on all three bots (was 0.01 since 06:22 MT).

### HWM reset (Trevor 2026-10-12 20:39 MT)
SOL and ETH HWM set to the balance just before their current positions opened (SOL 0.771337416, ETH 0.02026193). BTC unchanged.

### Withdrawal marks reset (Trevor 2026-10-12 20:47 MT)
SOL and ETH wd_mark set to their new HWMs (SOL 0.771337416, ETH 0.02026193).

### Short payout in own coin (permanent since 2026-10-14)
ETH/BTC short decreases (TP1/TP2/TP3, closes) always request the market coin (desiredMint; no flag). Jupiter rejects any
jupiterMinimumOut on a decrease (6015), so payouts carry no min-out. The old USDC → coin sweep and its cross-bot USDC
attribution were removed (2026-10-14; on 2026-10-13 04:31 MT it had handed an ETH close's $15.26 to the BTC bot).

## Fee-based minimum TP1 / TP2 distance (Trevor 2026-10-13)
- At open, after every add, and whenever TP1/TP2 are re-placed or re-sized (and once per bot start on the open position), the fees of the CURRENTLY OPEN size are computed (TP3 partials covered their own; Trevor 2026-10-13 20:37 MT): F = open_fee (0.06%) x current size + close_fee (0.06%) x current size + accrued borrow fee of the open position (on-chain: (collateral-custody cumulativeInterestRate - position snapshot) x current size / 1e9; Jupiter settles borrow on every increase/decrease, so this is already the current size share; fallback est. hourly rate x size x hours).
- A TP portion of c USD carries fees F x c / size and must net >= mult x that after fees: c x |P - avg| / avg - F c/size >= mult F c/size, i.e. minimum price P_min = avg x (1 +/- (1 + mult) x F / size) (+ long, - short), rounded outward to the cent. mult = tp1_fee_mult (default **3.0**, was 2.0) for TP1, tp2_fee_mult (default **4.5**, was 3.0) for TP2 (Trevor 2026-10-15 10:58 MT; same 1.5x ratio), per bot in [tp]; code defaults in config.py, not in any toml.
- Analyst / strategy TP1 / TP2 beyond P_min are kept; otherwise the TP is placed exactly at P_min. TP2 then stays beyond TP1 by >= tp_min_gap_usd. Re-placement uses the existing cancel/replace path. TP3, guard, entries unchanged; no min-out; no halt from this rule.
- TP3 has no fee-minimum clamp any more (2026-10-15): it is sized to the adds that net a profit after fees at its target (see the TP3 section).


### SOL guard equity uses the real payouts and the network fees paid so far (2026-10-27, Trevor)
Why: 10-27 08:01 SOL closed at its TP@guard price 0.00126 SOL below the guard target. Each partial payout was booked as an
estimate (~0.0002-0.00025 SOL high) and the trade's network fees (~0.00055 SOL over 51 txs) reached bal only at the flat
true-up, so the guard equity (and the TP@guard price found from it) ran high.
Now (SOL bot only; ETH / BTC already book the real own-coin change after a payout window and carry their network fees as
`fee_sol` debt):
- A payout booked as an estimate while the position stays open (TP fill, TP3 partial, trim; `bank_payout`) sets
  `local.pay_check`. In the same tick, before the TP@guard re-place and the guard check, `sol_pay_trueup` reads this
  trade's own position flows (position txs since the open, the same `position-flows` read as the flat true-up) and
  credits `flows + open request rent - booked` to bal (`payout_trueup`; the reserve float moves the other way as in the
  flat true-up). Request rent of the still-open requests is in the flows as gone but never left bal (`rent_held`), so it is
  added back (REQ_RENT x open requests). It only measures a consistent snapshot: every known decrease (TP fills + TP3
  partials + trims) is in the flows with no event errors, no cancel pending, and the open requests and size are the same
  after the read; otherwise it is retried next tick (estimate kept meanwhile). The flat true-up then only has the rest.
- `hwm_equity` (guard check, TP@guard price, BANK line) deducts the network fees this trade has paid (ledger_open.json
  `network_fee_lamports`) that are not in bal yet: all of them before the first payout true-up, only those since
  (`local.fee_mark_sol`) after it. The closing tx's own fee stays in the close value (`close_network_native`).
- 'Position gone' payout estimate: uses the last seen on-chain avg of the whole position (`local.pg_avg`), not the
  average of the lots left (after partials only the first lot, which understated a short's profit). Only the estimate
  between flat and the flat true-up changes.

### SOL flat true-up always applied; withdrawal blocked if it cannot be (2026-10-27, Trevor)
The SOL bot's flat true-up (bank net of the trade -> the position's own on-chain SOL flows, `Bot._sol_trueup`, kind
`flat_trueup`) is now ALWAYS applied, whatever its size; the old "skip if |adj| > 0.02 SOL" rule is gone. The SOL
booking re-read of the flows (when the trade row is booked after its payout window, late refunds included; not on a forced booking at a new open) applies it once more, so the bank's
net for every trade equals the chain's. If the flows cannot be read at flat, `bank.trueup_block` is set (trade = its
open sig): that bot's daily withdrawal is BLOCKED (ERROR logged once a day, retried every tick) and its HWM is held,
and the row's booking is held and re-read each tick until the flows are readable; the true-up is then applied and the
block cleared. A block from an older trade is never cleared by a later one (resolve by hand). ETH / BTC banks are
measured exactly from their own token and are unchanged.
**Root cause of the 2026-10-26 08:24 MT over-booking (+0.022548071 SOL):** the 2026-10-15 rule below. A resting add's
request rent is never debited from bal at placement (it is reserve float: recon_sol adds `rent_held` back), so
crediting it back on cancel booked +0.0037236 of phantom trade income per cancel; that trade re-planned its resting add
about six times. Earlier true-ups of -0.011462666 / -0.015223225 were the same pattern (under 0.02, so applied). Now
`_rest_bank_refund` credits the rent only when the request was counted as gone by a true-up (`rent_out`: a cancel still
pending at flat or a resting add still listed then), i.e. the 2026-10-14 late-refund case the rule was meant for.

### Cancelled resting add refunds its request rent too (2026-10-15, Trevor) - SUPERSEDED 2026-10-27 (see above)
Cancelling a resting add refunds the collateral AND the request-account rent (0.0037236 SOL). `_rest_bank_refund` now credits the rent to the SOL bot bank (`bank.REQ_RENT`, kind `refund_rent`) along with the collateral; ETH / BTC banks are other coins and are unchanged (their rent stays in the SOL reserve float). Before, a refund landing after the flat true-up left the rent in the reserve float (SOL 23:49 MT 2026-10-14 trade: row +0.007816 vs bank +0.004109). hwm / wd_mark are not edited; the HWM catches up when flat.

### Trade row waits for the cancelled resting add's refund (2026-10-14, Trevor)
A closed trade's row (trades.csv / ledger_summary) is no longer written at flat. Every bank bot stores the booking args
(`bank.book_pend`) and books once the payout window is over and this market has no open request in flight
(`bank_tick`). The SOL bot then re-reads the position's own flows, so the cancelled resting add's collateral refund
(landing up to a minute after flat; SOL 0.0958 on 3p82BqdH.., F7RkVw.. earlier) is in the row's net. ETH / BTC already
booked from the own-coin bank this way. Never halts, no KILL; a new open books the pending row first.

### Equity includes the resting add's collateral (2026-10-14, Trevor)
`hwm_equity()` (profit guard, `hwm_check`, status line) now adds the resting add's collateral (`rest_add.nat`, while
`debited`) to the bank equity: it leaves `bal` at placement, so without it the bot looked further below HWM than it is.
No double count: on a fill the record is cleared as the collateral becomes position margin; on cancel the refund returns
to `bal` and the record is cleared. `bank.update_hwm` (flat only, from `bal`) and HWM / wd_mark are unchanged.

## Corrections and operating rules (2026-10-14 review)
- **Withdraw fractions** are now SOL 0.65, ETH 0.65, BTC 0.70, with a +0.05 nightly nudge (00:23 MT, Grok Bot) while the JupSOL reserve is below the largest bot.
- **Resting add in equity:** `hwm_equity` includes the debited resting add's collateral (section above), so the guard does not understate equity while an add rests.
- **SOL trade rows are deferred** until the cancelled resting add's refund lands (`book_pend`, section above); ETH / BTC book from their own-coin bank the same way.
- **Inter-bot loans (`ib_*`):** ANY bot can borrow from ANY other bot (not just SOL as lender) via `live/ib_loan.json`: **no cap** (request = real next-rung shortfall + $1, min $2), **one loan at a time** but an open loan can be **topped up** by the same lender; the lender must be flat, no position, without a loan to anyone else, with >= $5 spare beyond the amount. HWM / equity / withdrawable amounts are never moved.
- **JupSOL rescue:** manual (`live/rescue.sh`, `RESCUE_PLAYBOOK.md`) while `[ledger] jupsol_loans = "off"` (all three bots); automatic in `"on"` (2026-10-27, see "Automatic JupSOL loans"): within **3%** of liquidation (borrow fee included), collateral only, min(to **10%** away, 1/3 of JupSOL), repaid with the exact JupSOL quantity.
- **PAUSE_UNTIL management is Grok Bot's job** (`live/PAUSE_UNTIL`, `live/eth/`, `live/btc/`; ISO time, naive = America/Denver): blocks fresh opens only; never closes positions or cancels adds. Grok Bot sets and lifts it and tells Trevor.
- **Direction management via `[bot] sides`** (`both` / `long` / `short`) is operational and done by Grok Bot from the Analyst bias and the news. Config change then restart right after a `sweep:` log line. **The bots never read Analyst data** (the bot only reads its own `sides`); the `sides` lines in `configs/live_*.toml` are uncommitted on the droplet and must not be committed or reverted.
- **TP2_SET / TP3_SET** overrides (Grok Bot, one-shot files; see section 6): moved in place, clamped to fee minimums; TP2 override kept in `tp2_override` until the next add.
- **TP re-place safety (2026-10-14):** `sync_tp_ladder` / `resize_tps` compute all targets (tp_targets, trim_tp_guard, fee minimums) BEFORE cancelling the old TPs, so an exception cannot leave a position without TPs; any exception sets `tp_retry` (no halt, no KILL). The `tp_retry` tick branch makes at most 3 consecutive attempts, then backs off 5 minutes (`tp_retry_until`); while a retry fails or backs off the rest of the tick (guard, timer, trim, resting, adds) continues.
- **TP timer:** see section 6 (bar-close vs per-tick difference).

## Overseer checks (Grok Bot hourly)
Silence rule: message Trevor only on issues or events (fills, closes, restarts, PAUSE_UNTIL set/lifted, alerts); otherwise stay quiet.
1. **Direction alignment:** Analyst bias + news vs each bot's `sides`; change `sides` in `configs/live_*.toml` if misaligned, restart only right after a `sweep:` log line, one bot at a time, verify health. Never commit those config lines.
2. **TP2 management:** volume, ATR and the 55-min timer (from first TP1 fill; closes only in profit + HWM check). Never place TP2 closer than the fee minimums except in a dire case.
3. **Brownie tally and rules:** +1 for a TP2 moved and hit, +1 for a TP3 moved and hit, +1 for a new real-token HWM; tally target 5.
4. **Liquidation within 3%:** flag; JupSOL rescue is manual while `jupsol_loans` is off (aim 10%, max 1/3 of JupSOL).
5. **Red risk** (positions deeply underwater / low margin): flag.
6. **Missing TPs / adds:** every open position has TP1/TP2 (and TP3 when adds filled) and its resting add; a missing one is a `tp_retry` / `RETP_NOW` matter, never closed or cancelled by hand.
7. **Borrow guard:** if any bot's free cash is below ~$12 while a flat bot can lend, pause fresh opens on another bot via PAUSE_UNTIL (prefer pausing ETH and keeping SOL as the lender); lift when not needed; tell Trevor when set and when lifted.
8. **Profit-guard watching:** equity vs the HWM target, including the resting add's collateral.
9. **EXT_FLOW_ALERT / wallet drain watch:** any new line in `live/EXT_FLOW_ALERT` is investigated on-chain; check again within 24 h.
10. **Reboot-required / kernel pending** on the droplet: report.
11. **state.json backup** exists and is fresh.
12. **News days:** Thu Oct 15 06:30 MT PPI and Retail Sales (watch direction / pause if needed).
13. **Nightly 00:47 audit** and **daily 00:23 withdraw nudge** (withdraw_fraction +0.05 rule above).
Standing rules: never close positions, never cancel adds; HWM / wd_mark (marks) are sacred, never adjusted; bots never halt and never write KILL; restart a bot only right after a `sweep:` log line, one at a time, then verify health; bots never read Analyst data.

**Trevor 2026-10-15 - TP1/TP2 at the profit-guard price (`[profit_guard] tps_at_guard`, code default false, enabled ONLY in live_btc.toml; replaces and removes the short-lived `tps_off_after_partial`, commit 514f80a)**: TP1 and TP2 are placed AT the guard close price `p` where `hwm_equity(p)` (bal - fee debt - loan + ib_adj + close value incl. slippage + resting-add collateral) equals `guard_target()` (trade_start_equity x (1+min_profit_pct_trade)); found by 60 bisection steps on the existing functions (`guard_price`), above market for a long, below for a short. Then the normal fee-minimum clamps (`fee_min_targets`: TP1/TP2 never closer than their floors, TP2 >= TP1 + `tp_min_gap_usd`) apply; if the guard is already met at market the TPs go to the fee floors and the guard fires this tick. Recomputed on every `_sync_tp_ladder` (open, add, RETP_NOW, tp_retry, adopt) and every `_resize_tps` (partial; with the flag on it does a full re-place at the new price). Targets are computed before the cancel; any exception sets `tp_retry`; resting add / TP3 partials are never cancelled. `tp2_override` / TP2_SET are ignored while on. Guard status/close logs add `TP@GUARD target <coin> = USD, TP price, placed [...]`. Flag off (SOL, ETH, code default) = unchanged. HWM/wd_mark/bank untouched.

**Trevor 2026-10-15 - tps_at_guard waits for the payout credit (fix, BTC)**: after a TP3 partial the guard price was computed before the partial's WBTC payout reached the bank (booking lags 30-90 s), so TP1/TP2 went ~7% too far (86343/86378 vs ~81884). Now `_sync_tp_ladder` (every tps_at_guard re-place, incl. the partial resize) first calls `guard_defer`: while resting TPs exist and (`bank["pay_wait"]` > 0, set by `bank_payout` for ETH/BTC and cleared when recon credits payout/adjust/external or after 20 ticks; or wallet token != bank bal) it returns BEFORE any cancel, keeping the old TPs, and sets `bank["tp_defer"]`; the next ticks re-place once the credit landed. After computing, a TP1 more than 3% beyond max(market, TP2 fee floor) is also deferred, not placed. Cap GUARD_DEFER_MAX = 20 ticks, then it places normally; no resting TPs = no deferral (never a TP-less window). HWM / wd_mark / bank untouched.

**Trevor 2026-10-15 - resting add re-arms at a TP3-closed lot (all bots, resting adds on)**: when a per-add partial (TP3) closes add lot(s), `resting_reconcile` records `L.rearm` = entry (long: highest closed lot entry, short: lowest), lev, margin (= lot size / lev) of that lot. While the market is on the favourable side of it (long: above), `rest_add_plan` plans the resting add at exactly that price / size / lev (skips next_add_level, min/max gap and the last-partial shift; `_adds_left` and free-collateral still apply, `guard` as always). If the single resting add sits elsewhere, `rearm_add` (from `resting_tick`) cancels it (`cancel_limit`, refund incl. rent) and ONLY on a confirmed cancel places the new one (never two; failed cancel = nothing placed, retried). Otherwise (market beyond the price) the normal ladder plan applies. `rearm` is cleared when that add fills, on a market add / lot recovery, and on trade close (state reset). TPs are untouched (independent of guard_defer / tps_at_guard). HWM / wd_mark untouched.

**Trevor 2026-10-15 - tps_at_guard: TP1 and TP2 at exactly the SAME price (BTC)**: in guard mode `fee_min_prices` passes gap 0 to `apply_tp_fee_min` instead of `tp_min_gap_usd`, so TP1 == TP2 == guard price (rounded 2 dp) and both trigger together; the per-TP fee-minimum floors still clamp each (TP1 3x, TP2 4.5x fees (2026-10-15 10:58 MT)), so when the guard is met at market / the guard price sits under the TP2 floor they go to their (different) floors exactly as before (existing behaviour kept). Same code path for placement, resize and `fee_min_recheck`, so equal prices are never a mismatch (no re-place churn). Flag off = unchanged (gap kept). Two decrease triggers at one price are independent requests (sized by ladder split / entire) and place_tp matches nothing by price in guard mode (TP2_SET ignored). Test: tests/test_tps_at_guard.py.

**Trevor 2026-10-15 - hwm_equity: coin price follows the evaluated price (fix, all bots)**: in `hwm_equity(pos, st, price)` the coin price `cpx` (close value USD->coin and fee debt SOL->coin) was held at the current oracle while `guard_price` bisected `price`, so the BTC/ETH guard price depended on the oracle (BTC 08:15: 81760.95 vs 81823.41 without it). Now `cpx = price` when price > 0; `sol_px` stays `sol_price(st)`. `guard_check`/`hwm_check`/`bank_status` pass the live oracle, so at market the result is identical. SOL bot: coin price is the SOL price, so cpx = evaluated price is the same correct rule. `bank_equity_flat` untouched. Existing TPs stay until the next re-place. HWM / wd_mark untouched. Tests in tests/test_bank.py.


**Trevor 2026-10-15 - three-bot unification (replaces `tps_at_guard`)**: (1) TP1/TP2 = `tp1_pct`/`tp2_pct` from avg (0.8% / 1.5%) at open and on every add on ALL bots. Once a TP3 partial has filled on the current trade (`local.partials_done` non-empty, reset on flat) TP2 is placed at the guard close price (`guard_price`) and TP1 `[profit_guard] tp_guard_gap_pct` (default 0.003) inside it (long: lower, short: higher), then the fee floors and `tp_min_gap_usd` still apply, so TP1 != TP2. Re-placed on every add/partial/RETP_NOW; stale-bank / far-price deferral (`guard_defer`) unchanged. BTC no longer places at guard from the start. (2) Resting adds are placed `[dca] add_frontrun_atr` (default 0.1) x own 1h ATR before the level (toward market: long up, short down; no ATR = at the level); the re-arm check tolerates that offset. Existing resting adds are never moved; the next add uses it. (3) Live tomls aligned: withdraw_fraction 0.75, SOL `extra_pause_files = ["live/PAUSE_UNTIL"]`, SOL `[scale] own_atr_fallback = true` (scale itself still off). Asset-specific units / paths / ledger min_profit_native / withdraw cost / SOL daily stake kept. (4) Analyst: with no/stale latest.json `levels_with_fallback` gives Levels(no S/R, own ATR) on all bots. Remaining analyst dependencies: S/R rungs and the TP3/target sizing from levels when fresh, the distance_level trim (needs levels, none = no trim), `sides` bias set by the operator, ladder_deep anchors (fall back to ATR multiples).

**Trevor 2026-10-16 - TP1 == TP2 at the guard**: after a TP3 fill (`partials_done`) `guard_tp_targets` places TP1 (50%) and TP2 (remainder, entire) at one price: guard close price, clamped only by the stricter of the TP1/TP2 fee floors (long: higher, short: lower = beyond both). `profit_guard.tp_guard_gap_pct` is no longer used; `tp_min_gap_usd` is not applied in guard mode (also in the fee-min recheck, so no churn). Initial placement (tp1_pct 0.008 / tp2_pct 0.015) unchanged. Also: place_tp retries StaleOraclePrice (6003) when it appears in `error` as well as `err`. TP3 not re-placed after an add is by design when no add lot nets > 0 after fees + network at the target (tp3_plan returns None), or the target is not beyond market.

**TP3 skipped log (2026-10-16 03:20 MT).** When no open add nets > 0 after fees at the target, the bot logs once per change . That is the rule working as designed, not a bug: the target uses the on-chain avg (Jupiter keeps the entry price when a lot is partially closed, so after TP3 fills the chain avg stays above the lot-ledger average), and a re-armed add right at the market sits only a few cents beyond that avg, so f x (far - avg) is smaller than the round-trip fee (12 bps + network). SOL 106.15 add: avg 105.985, target 106.05, lot nets -$0.06; ETH 2450.00 add: avg 2445.44, target 2447.37, nets -$0.04. Older (lower-priced) shorts lose at that target, so they never count.

**TP3 reference avg re-based (Trevor 2026-10-16 03:15 MT).** The TP3 distance is NOT measured from Jupiter's reported avg (it keeps the old entry when a lot is partially closed, so it drifts above the bot's lots and put the target within fee distance of the add). Reference = the bot's own size-weighted (harmonic) average entry of its open lots (first + adds), `resting.lots_avg` / `bot.tp3_ref_avg`; target = ref + f x (furthest open add - ref); size = open adds netting > 0 after fees at that price. TP1/TP2/guard/trim still use Jupiter's avg. Live example SOL: lots avg 105.185, furthest add 106.15, rung 4, f 0.423 -> TP3 105.59, size $275 (106.15 and 105.77 lots).

**TP3 reference = whole-position breakeven (Trevor 2026-10-16 03:30 MT, supersedes the lot-avg re-base above).** `bot.breakeven_price`: the price where `hwm_equity` (own-coin bank incl. banked partial payouts, fee debt, close fee/slippage) equals `trade_start_equity` (the guard target without its min-profit margin), by bisection. TP3 target = breakeven + f x (furthest open add - breakeven); size = open adds netting > 0 after fees at that price. Fallback (no bank baseline): bot's lot-weighted avg, then Jupiter avg. Placement logs `TP3 reference breakeven|lot avg = X`. TP3 is kept (no cancel/re-place) while the price moved < 0.03% and size is unchanged.

**Trevor 2026-10-24 - resting add follows moved analyst levels; no bare-level add after a restart (all bots, `live/bot.py`)**:
(1) Re-plan: every tick with a confirmed (not `unconfirmed`) resting add and no re-arm action, `resting_tick` -> `replan_add` re-plans the next rung (`rest_add_plan` with the old add's collateral counted as free, no inter-bot loan request). Only when the plan source is `"levels"` (an analyst h1/h4 S/R rung; `fixed`, `deep-*` and `rearm` plans never move the add): new = `add_px(pos, plan.trigger)`; if |new - current add trigger| > max(`add_front()`, 0.25 x ATR) (ATR = the ladder's analyst 1h ATR, `strategy._levels.atr`, the one the min/max gaps use) the add is cancelled and re-placed at new with the normal ladder size / leverage, via the same confirmed cancel-then-place as the TP3 re-arm (`guard` pre-checked with the old debit counted as refunded; failed cancel = old add stays, nothing placed, retried next tick; confirmed cancel + failed placement = the normal "no resting add -> place" path next tick; never two). Log `RESTING ADD re-planned: old -> new (level X src h1/h4 ...)`. Within the threshold the add stays. Levels carry the analyst timeframe of each level (`Levels.tfs`, `level_tf()`; logging only).
(2) Front-run ATR fallback: the resting tick runs before the kline fetch, so on the first tick after a restart the own 1h ATR is unset and the add used to be placed at the bare level. `front_atr()` = own 1h ATR, else the analyst ATR (`strategy._levels.atr`); `add_front()` uses it. With neither (and `add_frontrun_atr` > 0) no NEW resting add is placed that tick (logged once, retried next tick). An existing add is untouched.
(3) The `next add plan` bar line now shows the level source (h1/h4), the actual add price (`add_px`), the front-run distance and the ATR it used (own / analyst) and the ladder ATR.

**Trevor 2026-10-25 - minimum add distance 0.6 ATR**: `[dca] min_gap_atr` 0.3 -> 0.6 on all three bots (configs). The next add level is the nearest analyst h1/h4 level 0.6-1.2 analyst ATR beyond the last entry (`next_add_level`), placed 0.1 own ATR before it (`add_frontrun_atr`, unchanged). No level in 0.6-1.2 -> the unchanged fixed fallback (last entry +/- `trigger_dist`: SOL $0.85, ETH/BTC 1.15 x own ATR). The same `min_gap_atr` also feeds the existing ladder deep-level checks in `ladder_rungs`. The re-place rule is unchanged: a resting add is only re-planned when the new plan's source is `levels`; a `fixed` plan never moves an existing add.

**Trevor 2026-10-27 - minimum 0.85 ATR between rungs (all three bots; supersedes the 0.6 above)**: the resting ladder add is PLACED at least `[dca] min_place_gap_atr` = 0.85 ladder (analyst 1h) ATR beyond the add ref (the more adverse of the last entry and the last partial price, as the plan uses), measured after the 0.1 own-ATR front-run. (1) `min_gap_atr` 0.6 -> 0.95 (= 0.85 + the 0.1 front-run), `max_gap_atr` stays 1.2 (window 0.95-1.2, not empty): the add level is the nearest h1/h4 level in that window. (2) `Bot.add_px` clamp: when the own-ATR front-run would pull the placed price inside 0.85 x ladder ATR (own ATR > ladder ATR), it is placed at exactly ref +/- 0.85 x ATR, rounded to the adverse cent. (3) `next_add_level(fixed_floor_atr)`: with levels, the fixed fallback distance is max(trigger_dist, min_gap_atr x ATR), so fallback rungs and every projected rung beyond rung 0 (`ladder_rungs`, the liq-rule next rung) keep the same >= 0.95 ATR level spacing; `plan_kwargs` passes it only while min_place_gap_atr > 0 (0 = old behaviour everywhere, backtests unchanged). Re-arms after a TP3 fill (`readd_gap_atr` 0.6, placed exactly, not clamped) were exempt at first; since 13:50 MT they also keep >= 0.85 ATR beyond the remaining last entry (see the re-add spacing section), the re-plan rule (levels plans only, |diff| > max(front, 0.25 ATR); the clamp moves with ATR by far less than 0.25 ATR, so no churn; a fixed plan never moves an existing add), TP3 / guard (guard and liq rule use the level, the clamp only moves the order back toward the level), inter-bot loan sizing (rung collateral unchanged). Basis: BTC 10-25..10-27 rungs were 0.52-0.76 ATR (0.25-0.40%).

**Trevor 2026-10-27 - guard stays armed for the whole trade after an add or a partial; live guard base 0.3%**: (1) `hwm_close_armed()` / `pg_close_armed()` (close_requires "adds_or_trims") now use `trade_guard_armed()`: armed once this trade had ANY add fill (market, resting, recovered) or ANY partial (TP3 fill or trim), and it stays armed until the trade is flat. It is latched in `state.json` `guard_armed` (set at the add-fill / TP3-fill / trim sites; a new `Local` at open / flat resets it, so a new trade starts unarmed). It used to be `n_entries > 1 or lots > 1 or trims`, so a TP3 partial that closed the only add (lots=1, n_entries=1, no trims) DISARMED the market-close guard ("guard not armed (first entry: TPs only)") until the next add filled (SOL 2026-10-27: 07:09-07:13, 07:25-07:40, 07:45-08:02 MT; ETH/BTC the same pattern). Restart / old state without the key: derived from n_entries > 1, > 1 lot, trims or `partials_done` (all reset only at a new trade), then latched + saved (`GUARD ARMED for the rest of this trade` WARNING). Plain first entries (no add, no partial) stay TPs-only. TP placement is unchanged: TP1/TP2 at the TP@guard price whenever `partials_done` is non-empty (`guard_tps`, already sticky until flat; after an add with no partial yet, tp1/tp2 pct as before). Status line: `guard ARMED (adds/partials this trade)` + the `TP@GUARD` note while armed. (2) `[profit_guard] min_profit_pct_trade = 0.003` set in all three live tomls (code default still 0.002): target = `trade_start_equity` x 1.003 (was x 1.002). The target has no per-partial uplift (the `[ledger] min_profit_pct_per_partial` 0.0005 key is unchanged and, as before, unused by the live guard). Tests: tests/test_guard_arm_latch.py (6), live-config test, test_bank guard tests (1.003).

**Trevor 2026-10-27 14:55 MT - guard TPs rounded against themselves and re-placed when the guard price moves past them (all bots, shared code)**: (1) `guard_tp_px` rounds the guard close price to the price tick (`PRICE_TICK` 0.01 = the cent precision `place_tp` / `move_trigger` send, same for SOL, ETH, BTC) AGAINST itself: short floor, long ceil (was plain `round()`, which could put TP1/TP2 one tick short of the guard target: SOL 14:41 MT TPs 100.26 vs guard 100.25). The stricter-fee-floor clamp after it is unchanged (TP1 == TP2, no `tp_guard_gap_pct`, which stays unused). (2) `guard_drift_check` (every tick with an open position, after the once-per-process fee-minimum recheck): while `guard_tps` owns TP1/TP2, guard placement fixed them once (TP3 fill / add / restart re-place) and fees keep accruing (borrow, network fees). When a pending resting TP (`local.tp_px`) is now >= 1 tick on the WRONG side of the current conservative guard price (before the fee-floor clamp; a fee-floor move alone stays fee_min_recheck's job) (short: above it, long: below it: a fill there would land below the guard target), TP1/TP2 are cancelled and re-placed through the normal `sync_tp_ladder` path (guard_defer for an un-credited payout / implausible price, cancel_failed / pending cancels, tp_retry + back-off; TP3 partials and the resting add are never touched; sizes = ladder split on the whole position as every guard re-place). A TP merely further out than needed (e.g. at the fee floor) is kept. Hysteresis: compared on the rounded tick, and at most one drift re-place per 10 min (`GUARD_DRIFT_COOLDOWN_S`, per process) because the re-place's own network fees move the guard again; skipped while tp_retry / tp_defer / pending cancels / an un-credited ETH/BTC payout are pending, when no TP rests, or when the guard is already met at market (the market-close guard handles that). Restart: the first tick re-checks the resting TPs (no recomputation otherwise). TP2_SET stays ignored in guard mode. Tests: tests/test_guard_drift.py (6).

**Trevor 2026-10-28 09:36 MT - guard TP fee buffer (stops the guard re-place loop; all bots, shared code)**: ETH 2026-10-28 03:35-06:35 MT re-placed its guard TP1/TP2 every 10 min (2340.18, 2340.16, ... 2339.99): each re-place (2 cancels + 2 places, ~0.000024 SOL network fees) raised the fee debt, which moved the short guard price ~1.4-1.7 cents lower, so the TPs just placed at the rounded guard price were 1 tick on the wrong side again. Now `guard_tp_px` places TP1/TP2 at the price where equity = guard target + `GUARD_TP_BUFFER_SOL` (code constant 0.0001 SOL ~ 4 re-places of fees, in own coin x SOL px / coin px; SOL bot: as is), i.e. further on the SAFE side (short lower, long higher), then rounds against itself and clamps to the stricter fee floor as before. `guard_drift_check` is unchanged and still compares the resting TPs with the UN-buffered conservative guard price, so a drift smaller than the buffer (a re-place's own fees, small borrow accrual) never re-places, while a real drift past the resting TPs still re-places (10-min cooldown unchanged); after it the new TPs again carry the full buffer. Price size of the buffer = 0.0001 SOL in USD / position size in coin (ETH $350 short: ~7 cents; scales with size, so SOL's and BTC's own fee drift is covered the same way). Profit safety only stronger: a fill at the TP gives >= guard target + buffer. Guard met at market / buffered price unreachable: as before (fee floors / raw guard price). Status note: `TP price <buffered> (guard <raw> + fee buffer)`. Tests: tests/test_guard_tp_buffer.py (8: loop reproduced without the buffer, no re-place with it, buffer on the safe side, real drift still re-places; short and long).

**Trevor 2026-10-28 14:42 / 14:48 / 14:49 MT - the profit guard's close decision reads ONLY the chain ("there's only one truth, and that is what is on the table")**: the per-trade guard market-close (`hwm_close_armed`, tick) no longer uses `hwm_equity()`. It uses `Bot.guard_chain_equity(st, pos)`, from the tick's own chain read (`jup state`):
- **wallet**: ETH / BTC = the bot's own token in its wallet ATA (`sweepTokens` atoms, Wormhole ETH / WBTC). SOL = native SOL (getBalance) + the rent locked in the wallet's open request accounts (`allOpenRequests` x `REQ_RENT`, refundable) - `sol_fee_reserve` (0.02 SOL, the fee reserve that is never in bal / HWM). No JupSOL (the stake bucket is withdrawn capital, outside bal / HWM; an open JupSOL park = no close, its JupSOL is not read).
- **+ escrow**: the own-coin amount held on chain in this market's open request accounts (new additive `jup state` fields per `openRequests` entry: `mint`, `collateralDelta`, `escrowAta`, `escrowAtoms` = amount of ATA(request.mint, request), or `escrowError`): a resting add's collateral until it fills or is cancelled. A filled add's escrow is empty, so it can never be counted twice (SOL 14:01 MT).
- **+ close value**: from the position account: `collateralUsd` - accrued `borrowFeeUsd` + PnL at the on-chain (Doves) oracle - close fee at the custody's on-chain `decreasePositionBps`, converted at the oracle, less the same execution haircuts as before (`[costs] slippage`, ETH / BTC short `swap_cost_pct`, SOL `close_network_native`).
- **- loans / fee debt** (the only bookkeeping): JupSOL loan principal, inter-bot loan (`ib_adj`), and the SOL network-fee debt between the bots: ETH / BTC subtract their `fee_sol` (SOL their fees took from the shared wallet), the SOL bot adds the ETH + BTC `fee_sol` (owed back to its wallet).
- No lot, order, payout, true-up or ledger_open fee records. Like-for-like with HWM / `trade_start_equity` (both bal - fee debt - loans; bal excludes the reserve and request rent). Verified live 2026-10-28 ~15:00 MT: ETH flat on-chain 0.052951532 = bank flat equity 0.052951532; BTC 0.001537229 vs record-based 0.001537232; SOL 1.167375944 vs record-based 1.167594620 (on chain 0.00022 SOL lower = the safe side).
- **Failed / stale read = no close that tick**: chain read older than 60 s (`GUARD_CHAIN_MAX_AGE_S`), oracle or SOL oracle older than `max_oracle_age_sec` / missing, own-coin balance unreadable, any request escrow unreadable or holding another mint, `borrowFeeUsd` / `decreasePositionBps` missing, peer state unreadable (SOL), any exception. WARNING `on-chain guard: NO CLOSE this tick (...)`.
- **Pre-close re-check**: when it is met, the chain is read again right before the close (`guard_recheck_chain`); the close goes out only if the same side is still open and the fresh on-chain equity is still >= target, else WARNING `profit_guard: pre-close re-check -> NO close` and nothing is sent. Close reason: `profit_guard (trade guard (on chain): on-chain W wallet + E escrow + C close - F fee debt - L loans = X vs start S x (1+0.003) = T)`.
- Unchanged: target (`guard_target`), arming, HWM / wd_mark, TP placement incl. the TP@guard price (`guard_price` still bisects `hwm_equity`), the 55-min TP timer (`hwm_check`), status lines, all settings. Tests: tests/test_guard_onchain.py (18).

**Trevor 2026-10-29 - guard TP price from the chain only (all bots, shared code)**: `guard_price` (TP1 == TP2 at the profit-guard price, and the drift re-check) now bisects `guard_equity_at` = `guard_chain_equity(st, pos, price)`: wallet own coin (SOL: native + request rent - fee reserve) + own-coin escrow of open requests + the position account closed at the evaluated price (collateralUsd - accrued borrowFeeUsd + PnL - custody decreasePositionBps close fee, slippage / swap / close-tx haircuts) - loans / inter-bot fee debt (the only bookkeeping). It no longer uses `hwm_equity` (bank bal incl. payout ESTIMATES + a close value without the accrued borrow fee, at the config close fee). Why: SOL 2026-10-29 closed at its guard TPs 101.09 and booked 1.169343619 vs target 1.171192491 (0.001849 SOL short): the TP was set at 10:37 MT while the payout true-up had timed out (bal held the TP3 partial estimate) and the close value ignored the accrued borrow fee; the flat true-up then corrected bal by -0.002408 SOL. A failed / stale (> 60 s) / incomplete chain read raises `GuardChainUnreadable`: `_sync_tp_ladder` keeps the resting TPs (no cancel, `tp_retry` set, retried on the next fresh read; no TP resting at all -> fee-floor TPs, never a TP-less window) and `guard_drift_check` skips. The market-close guard was already on chain (2026-10-28). `hwm_equity` stays for the BANK status line / HWM timer / breakeven. HWM / wd_mark untouched. Tests: tests/test_guard_tp_onchain.py (8); older guard-price mechanics tests opt into fixture `guard_eq_from_bank`.

## Scaled ladder (Trevor 2026-10-29 21:43 MT; `[dca] ladder_mode = "scaled"`)

Base collateral **B** is set ONCE when the trade opens: B = max(first_collateral_pct x free, first_collateral_min_usd)
(5% of this bot's free collateral, floor $10.50). It is stored in the trade state (`first_margin`), survives restarts,
and on adoption of a foreign position is the on-chain collateral of that first lot. It is never recomputed from equity.

Rung n (n = 0 is the first entry, n = 1, 2, ... the adds):

    m_n = lev_start + n x lev_step         (uncapped multiplier)
    L_n = min(m_n, lev_cap)                (leverage)
    N_n = m_n x B                          (notional)
    C_n = N_n / L_n                        (collateral; = B until the cap, then grows so notional keeps rising by lev_step x B)

Live: lev_start 5, lev_step 2.5, lev_cap 20. Example (B = $10.50):

| n | lev | collateral | notional |
|---|-----|-----------|----------|
| 0 | 5 | 10.50 | 52.50 |
| 1 | 7.5 | 10.50 | 78.75 |
| 2 | 10 | 10.50 | 105.00 |
| 3 | 12.5 | 10.50 | 131.25 |
| 4 | 15 | 10.50 | 157.50 |
| 5 | 17.5 | 10.50 | 183.75 |
| 6 | 20 | 10.50 | 210.00 |
| 7 | 20 | 11.81 | 236.25 |
| 8 | 20 | 13.13 | 262.50 |
| 9 | 20 | 14.44 | 288.75 |
| 10 | 20 | 15.75 | 315.00 |

No maximum rung count: rungs are generated on demand. A rung is skipped (never shrunk) only when free collateral can not
cover C_n + the per-add fee reserve (`ladder_fee_usd_per_add`; the SOL fee-reserve check still applies when placing),
or when the post-add liquidation is not `liq_buffer_atr` ATR beyond the next rung. Rung spacing, resting adds,
front-run, TP1/2/3, guard, timer and entry rule are unchanged. The inter-bot / JupSOL next-add loan asks for C_n.
`ladder_max_rungs` no longer sizes anything; it is kept for the TP3 taper and the partial-zone N scaling.
`ladder_mode = "legacy"` restores the budget / flat `ladder_fixed_usd` ladder (backtests).

### Liquidation model: Jupiter formula (2026-10-29, 21:59 MT)
The ladder liq-buffer check now uses Jupiter Perps liquidation: liq = avg ∓ (collateral − close_fee − borrow_fee − size/maxLeverage)·avg/size (− long, + short).
maxLeverage (500x, 0.2% maint) and decreasePositionBps (6 bps close fee) are read from on-chain custody each tick; config values are fallbacks.
Accrued borrow (on-chain borrowFeeUsd) is subtracted for the live position, and 0 for planned adds. Size, collateral (net of open fee) and avg are recomputed per rung.
The previous model (1% maint, no close fee, borrow 0) put liq ~0.74%·size/qty too close and stopped the ladder about one rung early.
Note: live/rescue.py liq_price (jupsol loan sizing) still uses the 1% model, which is more conservative and unchanged.

**Trevor 2026-10-30 05:51 MT - no TP timer after a TP3**: "If a tp3 is taken then it should only close with profit guard." Once any TP3 (per-add partial) fills in a trade, the 55-min TP timer is off for the rest of that trade: `local.tp3_filled` latches in the partial-fill path (also derived from `partials_done`). It is persisted in state.json and reset by the new Local on flat/open. The tick logs once 'timer expired but a TP3 filled this trade: no timer close'. The position then closes only via the profit guard or its resting TP1/TP2. TP1-only trades keep the timer unchanged (55 min from the TP1 fill, net > 0 + HWM check). Backtest mirror: broker `Position.tp3_filled` (always False, since the backtest has no TP3) is checked in `DcaStrategy.exit_intents`. Tests: tests/test_tp3_no_timer.py.
