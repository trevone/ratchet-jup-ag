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
- Unexplained wallet changes **≥ $1.5** persisting **2 ticks** = external deposit/withdrawal (not P&L). Native SOL externals split **50 / 25 / 25** across SOL / ETH / BTC (ETH/BTC swap their share). Smaller SOL noise = fee reserve float.
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

**Who moves TPs:** set at open/add. **No automatic guard ever moves TPs.** Only **Grok Bot** moves TP2 from the analyst read. Bot keeps its own **post-TP1 fade** for TP2 (85% of distance, then pullback &gt; `0.3` ATR or 15m RSI rolls ≥ `8` → pull to `max(fee floor, TP1 + gap, best − 0.05 ATR)`; `tp_fade_floor_at_tp1 = true`). Grok Bot also manually closes partials the bot misses.

**TP timer:** `close_remainder_after_min = 55` after TP1. Closes only if remainder nets &gt; 0 **and** equity-if-closed ≥ HWM; else hold.

---

## 7. Per-add trim (`trim_trigger = distance_level`)

All three bots. Old 4h timer only applies in unused `trim_trigger = "time"` mode.

1. **Arm** when price is **≥ 0.85%** against average (`trim_arm_dist_pct = 0.0085`; long ≤ avg×0.9915, short ≥ avg×1.0085). Stays armed until the trade closes (`state.json`).
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

Kill files: per-bot `live/{,eth/,btc/}KILL`; global `live/KILL_ALL`. `close_all_on_kill = false`.

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
