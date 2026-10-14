# Ratchet — Jupiter Perps live strategy (SOL / ETH / BTC)

Owner reference for Trevor’s three live bots. Numbers from `configs/live_100.toml` (SOL), `live_eth.toml`, `live_btc.toml`, plus `live/bot.py` / `live/trim.py` / `live/bank.py` / `dca_levels.py`. If we get misaligned, come back here.

---

## TP rules (Trevor 2026-10-11, do not change without his say-so)

Applies identically to all three bots (SOL, ETH, BTC); only per-asset unit scaling differs. Config: `[tp] tp1_mode = "level"`, `tp2_from_tp1_ratio = 0.6`, `tp_periodic = false`.

- **TP1 is set ONCE** when a position is opened or added to. It goes just before (front-run on the profit side by `tp_res_buffer_atr` x 1h ATR) the nearest support (shorts) / resistance (longs) / analyst level beyond the average in the profit direction. Analyst levels come from `market-analysis/latest.json` (`eth/`, `btc/`) via `dca_levels.py`; both the main supports/resistances and `levels.fine` are considered, and the nearest qualifying level is used. TP1 must NOT move until the next add.
- **TP2 = avg + (TP1 - avg) / 0.6** (longs; mirrored for shorts), so TP1 sits ~60% of the way from the average to TP2.
- **TP2 only moves when Grok Bot (the monitoring agent) decides there is more profit to be made.** No automatic process moves TP1 or TP2 between adds (no periodic re-place, no fade, no guard moving TPs). A one-shot manual re-place via the normal placement path: `touch live/RETP_NOW` (or `live/eth/`, `live/btc/`).
- Safety floors kept: each TP must net >= fees (`tp_min_net_usd` fee floor) and stay beyond market by `tp_market_gap`; if the nearest level is inside the fee floor (or market gap), the next level out is used. No qualifying level -> ladder TP1 (or its fee floor), TP2 by the same 0.6 rule.

## 1. Goal and philosophy

Grow each bot’s own coin (SOL / Wormhole ETH / WBTC) with small, fee-aware profits. JupSOL is a stake bucket from daily withdrawals, never margin.

**Go for broke:** no stop-loss, no daily loss limit, no collateral or notional cap (`max_*` = `1e12`). Early close only to lock **net profit after fees** (profit guard / trims) or for exchange / infra problems. **Never** close to cut a loss. **No** loss-taking trims (adds only trim when that lot is itself net profitable).

`trend_flip_exit = false`. Open positions ride through 4h trend flips; the filter only gates **new** entries.

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

- Trades fund from and pay back to **that bot’s balance only**. Longs: own coin = collateral. Shorts: Jupiter swaps own coin → USDC inside the open request; USDC payouts swept back to the bot’s own coin.
- **No borrowing** from other bots: open/add skipped if own balance is short.
- **External wallet changes: alert only (Trevor 2026-10-11 19:08 MT).** An unexplained wallet change **≥ $1.5** persisting **2 ticks** is never P&L and is no longer charged to the bots: the bot logs `WARNING UNEXPLAINED WALLET CHANGE`, appends to `live/EXT_FLOW_ALERT` (quickcheck prints `ALERT EXT`), and changes nothing in any bank (no 50/25/25 split, no ETH/BTC coin swaps). `[ledger] ext_flow_mode = "alert"` in all three configs (`"split"` = old rule, off). Resolve by hand (book a correction), then delete the flag file. Smaller SOL noise = fee reserve float. Background: 2026-10-11 18:41 MT a stray 0.207 SOL JupSOL stake was split as an external withdrawal and made ETH/BTC sell coin; reversed 19:10-19:15 MT.
- ETH/BTC SOL network fees tracked as a small **fee debt**, settled daily when ≥ `0.005` SOL.
- **HWM** (own coin): only rises, and only while flat (`hwm = max(hwm, bal − fee debt)`).

First-entry sizing uses **10% of the bot’s free bank balance (USD)** (`first_collateral_pct`), floor $10.50, with `$0.10` fee reserve for adds.

---

## 3. Entries

**Side filter:** 4h EMA200. One 4h close flips the allowed side for **new** entries only.

**Signal TF:** 15m RSI (all three bots):

| Mode | Long | Short |
|------|------|-------|
| Base | RSI &lt; 45 | RSI &gt; 55 |
| Idle step 1 (≥ 60 min flat) | RSI &lt; 48 | RSI &gt; 52 |
| Idle step 2 (+30 min, capped) | RSI &lt; 50 | RSI &gt; 50 |

Idle resets on the next entry. `PAUSE_UNTIL` / `KILL` / `KILL_ALL` still block opens.

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

**Base distances** (ladder fallback; ratio mode sets TP1 from actual TP2):

| Bot | TP2 base | TP1 base (ladder) |
|-----|----------|-------------------|
| SOL | avg + **$0.75** | avg + **$0.45** |
| ETH / BTC | avg × **0.75%** | avg × **0.45%** |

**TP1 ratio (all bots, scope `all`):**  
`TP1 = avg + 0.6 × (actual smart TP2 − avg)`.

**Smart TP2:** `min(avg + distance, nearest qualifying resistance − 0.1 × 1h ATR)` (shorts: support + buffer). ETH/BTC: only levels with ≥ 2 touches. Fee floors unchanged (chunk ≥ $0.05 net, TP1 ≥ $0.05 inside TP2, etc.).

**Who moves TPs:** set at open/add. **No automatic guard ever moves TPs.** Only **Grok Bot** moves TP2 from the analyst read. Bot keeps its own **post-TP1 fade** for TP2 (85% of distance, then pullback &gt; `0.3` ATR or 15m RSI rolls ≥ `8` → pull to `max(fee floor, TP1 + gap, best − 0.05 ATR)`; `tp_fade_floor_at_tp1 = true`). Grok Bot also manually closes partials the bot misses. **TP2_SET** (Trevor 2026-10-13 20:57 MT): Grok Bot moves TP2 proactively by writing a price to `live/TP2_SET` (`live/eth/`, `live/btc/`). Next tick the bot cancels the resting TP2 and re-places it (same TP2 size, normal place/cancel-retry path) at that price clamped to >= the TP2 fee minimum and beyond TP1 + `tp_min_gap_usd`; recorded as `tp2_override` in state so restarts / resizes / re-places keep it until the next add re-places TPs normally. File deleted after use; invalid content is logged and deleted (never halts/KILL). Log tag `TP2_SET`.

**TP timer:** `close_remainder_after_min = 55` after TP1. Closes only if remainder nets &gt; 0 **and** equity-if-closed ≥ HWM; else hold.

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

**Trevor 2026-10-11 06:31 MT - guard only when underwater** (`[ledger] guard_only_underwater = true`, all three bots): the profit-guard market close (HWM guard `hwm + min_profit_native`, or the legacy pg_close) may only fire while the position is on the losing side of its average (long: price < avg; short: price > avg). At/through the average on the profit side the guard does nothing; TP1/TP2 and Grok Bot's TP2 moves decide the exit. The 55-min TP timer (`close_remainder_after_min`) is unchanged: it still requires profit + the HWM check.

**Trevor 2026-10-11 - HWM reset on nightly withdrawal** (already implemented, all bots via `bank.apply_withdrawal`): when the daily job withdraws X of a bot's coin to JupSOL, bal and hwm both drop by X and wd_mark = the new hwm, so the guard never chases withdrawn money (e.g. SOL 10-11 00:05 MT: hwm 0.7879 -> 0.7832).


When the trade had **adds or trims** and equity-if-closed-now ≥ **HWM + min profit** (own-coin amounts above) → **market-close the whole position**. Never moves TPs. TP timer will not close below HWM. First-entry-only trades ride their TPs.

Close value in own coin: collateral + PnL − fees − slippage (− ~0.2% USDC→coin est. on ETH/BTC shorts).

---

## 9. Daily withdrawal and ops cadence

**Trevor 2026-10-11 06:53 MT - no fixed minimum:** the old $0.50 minimum is gone. The 00:05 MT withdrawal (50% of HWM gain) is sent whenever amount_usd > `withdraw_fee_factor` (2) x estimated cost, where cost = amount_usd x `withdraw_cost_pct` + `withdraw_network_sol` x SOL price (SOL->JupSOL: 0.1% + 0.00005 SOL; ETH/WBTC->JupSOL: 0.3% + 0.0001 SOL). Smaller amounts are carried forward as before. Same logic on all three bots.


| Cadence | Action |
|---------|--------|
| Daily **00:05** MT | Per bot: **50%** (`withdraw_fraction`) of HWM gain since last withdrawal → **JupSOL** (SOL stakes; ETH/WBTC swap coin → JupSOL directly). Balance and HWM reduced by the amount; &lt; **$0.50** carries over. Each bot’s balance logged in coin and USD. ETH/BTC settle SOL fee debt when due. |
| Sundays **18:40** MT | Weekly SOL / JupSOL rebalance (ops). |
| When flat / safe | Recover empty ATA rent into JupSOL when appropriate. |

---

## 10. Oversight

| Who | Cadence | Notes |
|-----|---------|--------|
| Analyst | Levels every **~15 min** (free refresh); news / events ~**every 2h** | Urgent pings only |
| Grok Bot | Check-in every **3h at :52** | Message owner only on fills / adds / trims / closes /halts / errors; may move TP2 or close missed partials |

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

## JupSOL rescue (Trevor 2026-10-11)

When a bot's open position is within **3%** of liquidation (`live/rescue_cfg.json` trigger_pct), JupSOL is **lent** to that bot
(the one exception to "JupSOL is never margin"): JupSOL -> SOL (stake unstake path) -> ETH / WBTC (own-coin swap path);
part is deposited as extra collateral (increase with sizeUsd 0, shorts swapped to USDC by the keeper as usual) to push liq
toward `target_pct` 10% (max `collateral_share` 60% of the loan unless needed to clear the trigger), the rest is credited to
the bot's balance as add budget. Booked as a **liability** `bank.loan` (principal, JupSOL in, hwm_at_open): excluded from
HWM, profit guard equity and withdrawals. Out of rungs -> `rescue.sh extend` (+6 `ladder_max_rungs`, original saved) or a manual
add via `rescue.sh add`. When flat the bot auto-repays principal x (1 + `repay_margin_pct` 5%) (+ `repay_profit_share`) to
JupSOL and restores settings. JupSOL too small (full JupSOL pushes liq < 2 pts or < 10% of collateral) -> **EMERGENCY**:
notify Trevor. Tool: `live/rescue.sh` (dry run by default); steps: `RESCUE_PLAYBOOK.md`.

## Withdrawal balancing (Trevor 2026-10-11 08:43 MT)
- Each bot has its own `[ledger] withdraw_fraction` (share of HWM gain sent to JupSOL nightly at 00:05 MT); all start at 0.5. No $0.50 minimum: a withdrawal goes whenever it beats its swap costs.
- Grok Bot nudges these nightly at 00:23 MT: +0.05 on all while JupSOL < the largest single bot; -0.05 on all once JupSOL > all bots combined; an extra +/-0.05 on any bot drifting from the 50/25/25 split. Bounds 0.2-0.9. Every change logged in live/changes.log.
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
- `adds_enabled` (default false): the next ladder add rests as a Jupiter **limit-open order** at the rung price, same collateral / leverage (+1x per add, cap 20x) as the ladder plans now; the add trigger has no front-run today, so the order sits exactly at the rung. Built by Jupiter's hosted Perps API (`POST /orders/limit`), decoded + checked locally (`live/src/limit.ts`: payer = owner, only Perps/system/token/ATA/compute/oracle programs, side/size/trigger/collateral match), signed by the droplet wallet and submitted via `POST /transaction/execute` (keeper co-signature). Only one resting add at a time; never modified; after a fill the next missing rung is placed. Resting adds are cancelled (API `DELETE /orders/limit`) only when the trade closes or the side changes (Jupiter keeps limit orders after a close). Refused creation (e.g. utilisation > 80 %, Jupiter's **$10 minimum collateral per limit order**) is logged and retried next tick, no fallback. The market add on the bar is not used while on. Fills are booked like a market add (lot, leverage step, ledger, bank debit at placement / refund on cancel, profit-guard deposit) and the TP1/TP2 ladder is re-placed as after any add.
- `partials_enabled` (default false): **ONE partial TP per position** (Trevor 23:09/23:10 MT), a trigger decrease placed when the trim is **armed** (existing arm rule, `trim_arm_dist_pct`) **and** at least one add is trim-eligible (`trim.select_lots` at the current price; never the first entry). Size = the eligible adds' total; target = the N-th trim level (main + fine) beyond their size-weighted entry in the profit direction, N = round(`partial_levels_min` + (max − min) × (rung − 1)/(`ladder_max_rungs` − 1)) for the highest rung covered (1 → 4 over 14 rungs); fires `trim_level_tol_atr` (+ trim front-run, now 0) before the level; must cover fees (6+6 bps + `trim_network_usd`), else the next level out is used. Not touched otherwise (no toggling with the arm state); when a new add fills it is cancelled + replaced to also cover that add. Leftovers after a full close are cancelled.
- Only ONE resting add order at a time (Trevor 23:11 MT); after it fills the next one is placed.
- Jupiter limits found: limit orders need **≥ $10 collateral each** (API `collateral_size_below_minimum`; the live ladder's $3 rungs are refused → with adds_enabled those rungs would never be placed, only logged/retried); max 20 limit orders per market+side; refused when utilisation > 80 %; ~0.0052 SOL request rent per order (refunded on fill/cancel).
- `last_partial_rule` (default false): the price of the last partial (per-add partial fill or trim exit) is recorded; the next add order is planned from the more adverse of the last fill and that price and must lie beyond it.
- TP1 / TP2 logic unchanged (its re-place never touches resting adds or per-add partials).
- Preview (sends nothing): `.venv/bin/python -m live.resting_preview --config configs/live_100.toml`.


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

## Profit guard target as % of HWM (LIVE 2026-10-12 00:30 MT)
target = HWM x (min_profit_pct_hwm 0.2% + min_profit_pct_per_partial 0.05% x partials taken this trade
[market trims + TP3 fills]). Replaces the fixed min_profit_native amounts (used only if the pct is 0). Still a market
close of the whole position, still only while underwater.

## Ladder preset C + resting orders: LIVE since 2026-10-12 00:30 MT on all three bots.


## TP3 target: between the add entry and the average (LIVE 2026-10-12 01:01 MT, replaces "N levels beyond entry")
TP3 always closes while the covered add(s) are still underwater versus the trade average: target strictly between the
add entry and avg. Allowed distance = (entry -> avg gap) x f, f rising linearly from 0 at rung 1 to
[resting] partial_zone_f_max (0.5 = halfway to avg) at the deepest rung (ladder_max_rungs). Rung 1: the first analyst
level (main + fine) past its entry; deeper rungs: the farthest analyst level inside the allowed zone. Fired 0.05 ATR
before the level; must cover fees. No level in the zone: the fee-covering minimum just past the entry. Several adds in
one TP3: size-weighted entry and highest rung.

TP3 zone edge (LIVE 2026-10-12 01:45 MT): when no analyst level lies inside the allowed zone (rungs >= 2), TP3 targets the
zone edge (entry + gap x f) less the 0.05 ATR front-run, if that covers fees; otherwise the fee-covering minimum just past
the entry; if even that is at/beyond the average, no TP3 (logged). Rung 1: first analyst level past its entry, as before.

### TP1/TP2 re-size after a TP3 fill (Trevor 2026-10-12 06:07 MT)
When a TP3 (per-add partial TP order) fills, the pending TP1/TP2 trigger orders are re-placed at the same prices, sized on the remaining position with the original % split (e.g. TP1 = 50% of remaining). Nothing else changes.

### TP3 arm distance 1 % (Trevor 2026-10-12 06:22 MT)
trim_arm_dist_pct = 0.01 on all three bots (was 0.0065): trim/TP3 arms once price is > 1 % against the avg.

### Unconfirmed cancels never halt (Trevor 2026-10-12 08:28 MT)
A cancel (resting add, TP, leftover order) that does not confirm is logged and retried every tick while the order is still on-chain; once gone any resting-add refund is booked and TP1/TP2 re-placed if needed. No KILL. KILL stays for opens/increases/unknown positions.

### The bot never stops itself (Trevor 2026-10-12 08:46 MT)
No automatic KILL. Unconfirmed open/add -> next tick reads the chain (late fill booked via lot recovery / late open adopted, else normal logic). TP placement failure -> TP1/TP2 re-placed next tick (TP3 re-placed each tick as before). Unconfirmed close / trim -> next tick reconciles. A position the bot did not open (incl. manual) is ADOPTED as one lot (chain side/size/avg/collateral), TPs placed, managed normally. All logged as ERROR. Manual KILL / KILL_ALL files still stop new orders.

### No double add / adopted deposit (Trevor 2026-10-12 08:52 MT)
While an add/open is unresolved (add_pending), no new add (market or resting) is sent until the chain shows it landed (booked via lot recovery / adoption) or absent with no increase request pending. An adopted position collateral is booked as a trade deposit in the bot own coin at the adoption price (HWM unchanged).

### Inter-bot loans (Trevor 2026-10-12 18:09 / 18:13 MT)
A bot without enough free equity for its next resting rung requests a loan (what the rung needs, max $50) through live/ib_loan.json. Any flat bot without a loan funds it by swapping its own coin into the borrower coin. The lender opens nothing until repaid. On the borrower full close it swaps back the quantity lent (swap cost on the borrower); the lender books it and is re-enabled. Loans never move HWM, equity or withdrawable amounts on either side. One loan at a time (live/ib_loan.json: requested -> funding -> funded -> open -> repaid -> settled). A lender must be flat with no loan of its own and keep >= $5 spare beyond the loan. Repayment targets the lent quantity at oracle prices + 0.4% buffer; the first repayment came back 0.000964 SOL short (swap price below oracle), which stayed with the borrower. Since 2026-10-13 (Trevor) repayment is a Jupiter ExactOut swap for exactly the owed lender quantity (borrower coin in = whatever it costs, swap cost on the borrower; only the usual slippage/impact caps, no extra min-out floor). The received amount is verified from the tx balance deltas (SOL: network fee excluded, it is booked on the borrower); if still short the loan stays open with the remaining shortfall (ib_loan.repaid_got accumulates) and is topped up next tick before status -> repaid. Never a halt. Errors are logged and retried; never a halt. First loan: SOL -> BTC 0.478070233 SOL ($50) 18:13 MT.

### TP3 arm distance back to 0.65 % (Trevor 2026-10-12 20:29 MT)
trim_arm_dist_pct = 0.0065 on all three bots (was 0.01 since 06:22 MT).

### HWM reset (Trevor 2026-10-12 20:39 MT)
SOL and ETH HWM set to the balance just before their current positions opened (SOL 0.771337416, ETH 0.02026193). BTC unchanged.

### Withdrawal marks reset (Trevor 2026-10-12 20:47 MT)
SOL and ETH wd_mark set to their new HWMs (SOL 0.771337416, ETH 0.02026193).

### Short payout in own coin (2026-10-13, LIVE on ETH/BTC since 08:58 MT)
`[live] short_payout_native` (default false): ETH/BTC short decreases (TP1/TP2/TP3, closes) request the market coin (desiredMint) instead of USDC. Simulation 2026-10-13 on the ETH short: accepted without min-out; any jupiterMinimumOut on a decrease is rejected (6015), so it cannot carry min-out protection. Trevor 08:56 MT: ON for ETH and BTC with no min-out (closes never blocked); sweep.py stays as fallback for any USDC; native payouts are booked as normal payouts.

## Fee-based minimum TP1 / TP2 distance (Trevor 2026-10-13)
- At open, after every add, and whenever TP1/TP2 are re-placed or re-sized (and once per bot start on the open position), the fees of the CURRENTLY OPEN size are computed (TP3 partials covered their own; Trevor 2026-10-13 20:37 MT): F = open_fee (0.06%) x current size + close_fee (0.06%) x current size + accrued borrow fee of the open position (on-chain: (collateral-custody cumulativeInterestRate - position snapshot) x current size / 1e9; Jupiter settles borrow on every increase/decrease, so this is already the current size share; fallback est. hourly rate x size x hours).
- A TP portion of c USD carries fees F x c / size and must net >= mult x that after fees: c x |P - avg| / avg - F c/size >= mult F c/size, i.e. minimum price P_min = avg x (1 +/- (1 + mult) x F / size) (+ long, - short), rounded outward to the cent. mult = tp1_fee_mult (default 2.0) for TP1, tp2_fee_mult (default 3.0) for TP2, per bot in [tp].
- Analyst / strategy TP1 / TP2 beyond P_min are kept; otherwise the TP is placed exactly at P_min. TP2 then stays beyond TP1 by >= tp_min_gap_usd. Re-placement uses the existing cancel/replace path. TP3, guard, entries unchanged; no min-out; no halt from this rule.
- TP3 (per-add partial, Trevor 2026-10-13 20:38 MT): its portion (covered adds, size s, size-weighted entry e) must net >= tp3_fee_mult (default 2.0, [tp]) x its own fees F3 = (open_fee + close_fee) x s + accrued borrow x s / position size. P3_min = e x (1 +/- (1 + tp3_fee_mult) x F3 / s), rounded outward to the cent. The zone / level / zone-edge / fee-covering target is kept when beyond P3_min, else TP3 is placed exactly at P3_min (mode "...->fee_mult_min"). Zone logic otherwise unchanged.


## USDC sweep attribution (2026-10-14, Trevor)

USDC in the shared wallet is swept only by the bot whose short decrease produced it. Each ETH / BTC bot posts a pending
payout claim (`live/usdc_claim_<ASSET>.json`) when it sends a USDC-paying short decrease and on every payout event; the
USDC goes to the one claim whose amount matches (max $0.50 / 3 %). Without claims, the only short candidate (open, or closed
and not yet booked) gets it. If two candidates or claims could own it, nothing is swept: it is logged and retried next tick,
never guessed and never a halt. (Before: the open short won, ETH > BTC > SOL, which on 2026-10-13 04:31 MT handed the ETH
close's $15.26 to the BTC bot.)
