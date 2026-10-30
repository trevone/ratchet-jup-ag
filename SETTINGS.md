# Ratchet settings cheat sheet

Values read from `configs/live_100.toml` (SOL), `configs/live_eth.toml` (ETH), `configs/live_btc.toml` (BTC) on 2026-10-10 19:10 MT.
Edit the config, back it up first (`*.bak-YYYYMMDD-HHMM`), then `sudo systemctl restart ratchet-<sol|eth|btc>`.

| Section | Key | SOL | ETH | BTC | What it does |
|---|---|---|---|---|---|
| [signal] | entry_rule | price_ma | price_ma | price_ma | 2026-10-29: LONG when price > 4h EMA200 AND > 15m SMA10; SHORT when below both; disagree/equal = flat. Price vs line, never slope. "rsi" = old rule |
| (top) | sides | both | both | both | 2026-10-29: both (was short, analyst bias); outlook/levels no longer restrict entry side |
| [signal] | entry_sma_length | 10 | 10 | 10 | SMA length on the signal TF (15m) for the entry rule |
| [signal] | tf | 15m | 15m | 15m | Signal timeframe (15m SMA10; RSI logged only) (trend filter = 4h EMA200) |
| [signal] | oversold / overbought | 45 / 55 | 45 / 55 | 45 / 55 | Long entry RSI < oversold, short entry RSI > overbought (UNUSED for entries since 2026-10-29: entry_rule = price_ma) |
| [signal] | idle_after_min / idle_step_min | 60 / 30 | 60 / 30 | 60 / 30 | Flat this long -> loosen RSI thresholds in steps (UNUSED for entries since 2026-10-29: entry_rule = price_ma) |
| [signal] | idle_long_rsi_steps / idle_short_rsi_steps | [48,50] / [52,50] | same | same | Loosened RSI thresholds while idle (UNUSED for entries since 2026-10-29: entry_rule = price_ma) |
| [entry] | first_collateral_pct | 0.10 | 0.10 | 0.10 | First entry = 10% of bot's free bank (USD) |
| [entry] | first_collateral_min_usd | 10.50 | 10.50 | 10.50 | Floor for first entry collateral |
| [entry] | fee_reserve_usd | 0.10 | 0.10 | 0.10 | Kept back from first entry for add fees |
| [dca] | mode | levels | levels | levels | Adds at analyst S/R levels |
| [dca] | levels_path | market-analysis/latest.json | market-analysis/eth/latest.json | market-analysis/btc/latest.json | Analyst levels file |
| [dca] | levels_max_age_min | 45 | 45 | 45 | Older -> fallback spacing |
| [dca] | min_gap_atr / max_gap_atr | 0.95 / 1.2 | 0.95 / 1.2 | 0.95 / 1.2 | Allowed gap add ref -> add level (1h analyst ATR); Trevor 2026-10-25: min 0.3 -> 0.6; 2026-10-27: 0.6 -> 0.95 (= 0.85 placed + 0.1 front-run). No level in range -> fixed trigger_dist fallback (SOL $0.85; ETH/BTC 1.15 x own ATR), floored at 0.95 x ladder ATR while min_place_gap_atr > 0 |
| [dca] | min_place_gap_atr | 0.85 | 0.85 | 0.85 | Trevor 2026-10-27: the resting ladder add is PLACED >= this x ladder (analyst) ATR beyond the add ref (more adverse of last entry / last partial) after the front-run (Bot.add_px clamp, adverse cent); > 0 also floors the fixed fallback (and every projected rung) at min_gap_atr x ATR. Re-arms after a TP3 fill: also >= this x ATR beyond the remaining last entry (2026-10-27 13:50 MT, see readd_gap_atr). 0 = off |
| [dca] | trigger_dist | 0.85 ($) | (overridden by [scale]) | (overridden by [scale]) | Fallback rung spacing |
| [scale] | trigger_dist_atr | - | 1.15 | 1.15 | ETH/BTC fallback spacing = 1.15 x own 1h ATR |
| [dca] | ladder_min_rungs / ladder_max_rungs | 4 / 14 | 4 / 14 | 4 / 14 | Ladder rung count |
| [dca] | ladder_weight_mode / mult / step / cap | accel / 1.15 / 0.10 / 2.5 | same | same | Rung budget weighting (deeper rungs bigger) |
| [dca] | ladder_deep_min_atr / max_atr / atr | 6 / 12 / 8 | same | same | Deep level: deepest support 6-12 ATR below first entry, else first - 8 ATR |
| [dca] | min_add_usd | 3.0 | 3.0 | 3.0 | Minimum add size |
| [dca] | liq_buffer_atr | 2.0 | 2.0 | 2.0 | Post-add liq must be >= 2 ATR beyond next rung |
| [dca] | lev_start / lev_step / lev_cap | 5 / 1 / 20 | 5 / 1 / 20 | 5 / 1 / 20 | Leverage 5x first entry, +1x per add, cap 20x |
| [live] | max_leverage | 20 | 20 | 20 | Hard leverage cap |
| [live] | sol_fee_reserve | 0.02 | 0.02 | 0.02 | Native SOL kept for fees |
| [live] | max_collateral_usd / max_position_usd / max_daily_loss_usd | 1e12 | 1e12 | 1e12 | Caps effectively off |
| [live] | kill_switch_path | live/KILL | live/eth/KILL | live/btc/KILL | Per-bot kill file (global: live/KILL_ALL) |
| [tp] | levels (TP1 / TP2 dist) | $0.45 / $0.75 (mode price_abs) | 0.45% / 0.75% | 0.45% / 0.75% | Close split 50% / 100%; dist only used when tp1_pct / tp2_pct are unset |
| [tp] | tp1_pct / tp2_pct | 0.0075 / 0.012 | 0.0075 / 0.012 | 0.0064 / 0.012 | Trevor 2026-10-27 09:19 MT: TP1 0.64% -> 0.75% on SOL / ETH (BTC stays 0.64%: 0.75% is ~1.6x its 1h ATR). 2026-10-24: both cut ~20% (was 0.008 / 0.015, Option A 2026-10-15): TP1 = avg x (1 -/+ tp1_pct), TP2 = avg x (1 -/+ 1.2%); 0 < tp1_pct < tp2_pct, set together; fee floors / min gap / market clamp still apply; levels give only the close split. Unset = levels dist |
| [tp] | smart_tp2 | true | true | true | Fee floors, TP1 inside TP2, never past market (resistance cap / fade removed 2026-10-15) |
| [tp] | tp_periodic | false | false | false | TPs set only at open/add |
| [tp] | tp1_fee_mult / tp2_fee_mult | 3.0 / 4.5 | same | same | Fee-minimum TP1 / TP2 floors (code defaults, config.py): a TP portion nets >= mult x its fees. 2026-10-15: was 2.0 / 3.0. tp3_fee_mult removed (TP3 = adds that net > 0 after fees) |
| [resting] | partial_zone_f_first / partial_zone_f_deep | 0.50 / 0.27 | same | same | TP3 target = avg + f x (furthest open add - avg); f tapers linearly rung 1 (open adds) -> ladder_max_rungs; TP3 size = adds netting > 0 after fees + network at the target |
| [resting] | partial_min_net_pct | 0.002 | 0.002 | 0.002 | Trevor 2026-10-27 09:19 MT: TP3 at least far enough that the furthest add lot nets this x its own notional after open + close fees + network ($0.15 on a $75 add); the f target wins when already further; 0 = off |
| [resting] | readd_gap_atr | 0.6 | 0.6 | 0.6 | Trevor 2026-10-27 09:19 MT: after a TP3 fill the resting add re-arms at the more adverse of the closed lot entry and last partial +/- this x ladder ATR, set at fill time, placed exactly there (no front-run creep); 0 = off (closed lot entry + front-run) 2026-10-27 13:50 MT: also >= dca.min_place_gap_atr (0.85) x ATR beyond the remaining last entry (most adverse wins) |
| [tp] | close_remainder_after_min | 55 | 55 | 55 | After TP1, close remainder if net > 0 and equity >= HWM |
| [trim] | trim_trigger | distance_level | distance_level | distance_level | Per-add trim mode |
| [trim] | trim_arm_dist_pct | 0.0065 | 0.0065 | 0.0065 | Arm when price 0.65% against avg |
| [trim] | trim_level_front_run_atr / tol_atr | 0.15 / 0.1 | same | same | Fire 0.25 ATR before next S/R level |
| [trim] | trim_min_gain_pct / trim_min_net_usd | 0.005 / 0.25 | same | same | Only adds with >= 0.5% gain and >= $0.25 net |
| [profit_guard] | enabled / close_when_met / close_requires | true / true / adds_or_trims | same | same | Market-close whole position when equity >= trade_start_equity x (1 + min_profit_pct_trade); adds_or_trims = armed once the trade had any add fill or partial (TP3 / trim), latched until flat (state guard_armed, 2026-10-27) |
| [profit_guard] | min_profit_pct_trade | 0.003 | 0.003 | 0.003 | Trevor 2026-10-27 08:56 MT: live per-trade guard base 0.2% -> 0.3% (target = trade_start_equity x 1.003; code default 0.002; no per-partial uplift) |
| [profit_guard] | (code) guard equity source | on chain | on chain | on chain | Trevor 2026-10-28 14:42-14:49 MT: the guard's close decision = wallet own coin (SOL: native + request rent - sol_fee_reserve) + own request escrow + position close value (collateral - borrow + PnL - on-chain close fee) - loans / inter-bot SOL fee debt; any failed or > 60 s old read (`GUARD_CHAIN_MAX_AGE_S`, code constant) = no close; re-read right before the close, aborted below target. No new config key (STRATEGY.md) |
| [profit_guard] | min_trade_profit_sol | 0.001 | 0.001 | 0.001 | **Legacy field, not used by the live guard** (see [ledger] min_profit_native) |
| [ledger] | enabled | true | true | true | Per-bot own-coin bank + HWM |
| [ledger] | min_profit_native | 0.0015 SOL | 0.000066 ETH | 0.00000195 WBTC | Obsolete (guard is per trade: [profit_guard] min_profit_pct_trade 0.003 in the live tomls since 2026-10-27) |
| [ledger] | ext_min_usd / window_ticks | 1.5 / 3 | same | same | External deposit/withdrawal detection |
| [ledger] | withdraw_enabled / time / fraction / min_usd | true / 00:05 / 0.90 / 0.50 | same | same | Daily withdraw_fraction (0.90 since 2026-10-27 00:32 MT, the cap; nudged nightly) of HWM gain -> JupSOL |
| (code) bot.py | SOL flat true-up | always applied | n/a | n/a | 2026-10-27: no 0.02 SOL skip; re-applied at booking; if flows unreadable -> bank.trueup_block: SOL daily withdrawal blocked + HWM held until applied |
| (code) live/deposit.py | MIN_USD / RESOLVE_AFTER_S / RETRY_S | $2 / 300 s / 60 s | same (one shared journal) | same | USDC deposit fold-in (2026-10-26): >= $2 USDC in the wallet -> SOL bot swaps equal thirds into SOL / ETH / WBTC (remainder SOL) with the sweep slippage settings (a leg that failed on Jupiter slippage 6001 retries at 2x, capped at stake_max_slippage_bps = 100 bps; simulation rejections retry after 60 s); each bot credits its measured leg to bal + hwm + wd_mark. Files: live/deposit_journal.json, live/deposits.jsonl, live/deposit*.lock (gitignored). Needs [bot] sweep_enabled on the SOL bot |
| [ledger] | fee_settle_min_sol | 0.005 | 0.005 | 0.005 | ETH/BTC settle SOL fee debt above this |
| [ledger] | jupsol_loans | "off" | "off" | "off" | Trevor 2026-10-27 (branch jupsol-loans): automatic JupSOL loans `off` / `observe` (log what it would do, send nothing) / `on` (live/jupsol_loans.py). Code default "off". Repayment of an outstanding JupSOL loan (exact JupSOL quantity, on flat, after any peer loan) runs in every mode |
| (code) jupsol_loans.py | RESCUE_SHARE / NEXT_ADD_BUFFER_USD / NOPEER_TICKS | 1/3 / $1 / 2 | same | same | Rescue takes at most 1/3 of the usable JupSOL (after haircut_pct); next-add loan = max(rung margin - free, SOL bot's native fee-reserve gap of the add) + $1 (no min, no cap; 2026-10-28), only after 2 consecutive ticks with no peer able to cover it |
| (file) <state dir>/IB_BORROW_OFF | peer-borrow test switch | absent | absent | absent | Present = this bot never borrows from a peer bot (own pending request withdrawn); lending to peers unchanged; any mode; no restart. SOL: live/IB_BORROW_OFF |
| (file) <state dir>/JUPSOL_PARK | park / unpark request | absent | absent | absent | Live-test tool: {"op":"park","native":x} moves x own coin into JupSOL as a receivable (bank.park; equity / hwm / wd_mark unchanged), {"op":"unpark"} brings it back; result in JUPSOL_PARK.result |
| [withdraw] | enabled | true | false | false | Legacy SOL daily stake job (SOL bot only) |
| [bot] | asset / share / state_dir | SOL / 0.5 / live | ETH / 0.25 / live/eth | BTC / 0.25 / live/btc | Market, wallet share, state dir |
| [ledger] | guard_only_underwater | true | true | true | Obsolete since 2026-10-14 (ignored; per-trade guard) |
| [ledger] | withdraw_cost_pct / withdraw_network_sol / withdraw_fee_factor | 0.001 / 0.00005 / 2 | 0.003 / 0.0001 / 2 | 0.003 / 0.0001 / 2 | Trevor 2026-10-11: replaces withdraw_min_usd 0.50; withdraw when amount_usd > factor x est. cost, else carry |
| rescue_cfg.json | trigger_pct / target_pct | 3 / 10 | 3 / 10 | 3 / 10 | Trevor 2026-10-11: JupSOL rescue when liq within 3%; collateral aims liq 10% away |
| rescue_cfg.json | keep_jupsol / haircut_pct | 0 / 1 | same | same | JupSOL kept back; swap-cost haircut (usable JupSOL x 0.99 before the 1/3 cap). 2026-10-27: collateral_share (no bank credit split), repay_margin_pct / repay_profit_share (exact JupSOL repayment) and emergency_min_collat_pct removed |
| rescue_cfg.json | emergency_min_push_pct | 2 | same | same | EMERGENCY (notify Trevor) if the rescue amount (min(to 10 %, 1/3 of JupSOL)) moves liq < 2 points; the trigger distance includes the accrued borrow fee |
| rescue_cfg.json | extend_rungs | 6 | 6 | 6 | `rescue.sh extend`: temp +ladder_max_rungs, auto-restored on repay |
| (code) bank.py | IB_MIN_USD / IB_TOPUP_TIMEOUT_S / IB_PROBE_STEPS | 2.0 / 1800 / 100 | same | same | Inter-bot loans (Trevor 2026-10-15, re-confirmed 2026-10-25): NO loan cap; request/top-up = real next-rung shortfall + $1, min $2; 2026-10-25: the rung is found by a search with cash + 1..100 steps of max(min_add_usd, ladder_fixed_usd) (search bound, not a cap; never cash + 1e9); own request withdrawn when flat / no longer needed; pending/stuck top-up cancelled after 30 min |

| [profit_guard] | tp_guard_gap_pct | 0.003 | 0.003 | 0.003 | Trevor 2026-10-16: UNUSED - after a TP3 fill TP1 == TP2 = guard price clamped by the stricter fee floor (no gap); 2026-10-27 14:55 MT: guard price rounded against itself (short floor / long ceil to the 0.01 tick) and TP1/TP2 re-placed when a resting TP is >= 1 tick on the wrong side of the live guard price, fee-floor moves excluded (code constants, no config key; 10-min cooldown); 2026-10-28 09:36 MT: guard TPs placed `GUARD_TP_BUFFER_SOL` = 0.0001 SOL of fees beyond the guard target (safe side; code constant) while the drift check still uses the un-buffered guard price, so a re-place's own fees no longer trigger the next re-place |
| [dca] | add_frontrun_atr | 0.1 | 0.1 | 0.1 | Trevor 2026-10-15: resting add placed this x own 1h ATR before the level (2026-10-24: own ATR unset -> analyst ATR; neither -> no new add that tick); 0 = at the level. 2026-10-24: a resting add on a `levels` rung is cancelled + re-placed when the level moves it by > max(front, 0.25 x analyst ATR) (fixed in code, no key) |
| [ledger] | withdraw_fraction | 0.75 | 0.75 | 0.75 | unified 2026-10-15 (SOL/ETH were 0.70) |
| [scale] | own_atr_fallback | true | true | true | SOL added 2026-10-15 |

Note 2026-10-16 03:20 MT: no new setting; TP3 planner now logs `resting: TP3 skipped: no lot nets positive ...` (once per change) when it places nothing.

Note 2026-10-16 03:15 MT: TP3 reference avg = bot's own lot-weighted entry (not Jupiter avg); no new setting.

Note 2026-10-16 03:30 MT: TP3 reference = whole-position breakeven (bank equity == trade_start_equity); no new setting.

Note 2026-10-28 13:49 MT: no new setting; a TP3 fill seen as a size drop before its order record disappears is booked as that TP3 partial (not a TP hit), and the stale record later disappearing books nothing (STRATEGY.md, `Bot.early_partial_fill`).

Note 2026-10-28 14:15 MT: no new setting; a resting add fill seen as size growth before its order record disappears is booked as that add fill at once, never counted twice in the guard equity, and the stale record later disappearing books nothing (STRATEGY.md, `Bot.early_add_fill`).

Note 2026-10-29 MT: no new setting; a TP3_SET price is now held by partial_sync (record flag manual) until the next add / resize changes the TP3 size.

- 2026-10-29: the guard TP price (TP1 == TP2 after a TP3 fill) is computed from the on-chain equity only (`guard_chain_equity`); no setting; `GUARD_CHAIN_MAX_AGE_S` (60 s, code constant) is the freshness limit. A stale read keeps the resting TPs.


**Profit-guard market close (2026-10-29 21:05 MT, no config key):** skipped while guard TP1/TP2 are resting on chain (`guard_tps_resting`); the TPs handle the close. Fires (after the fresh chain re-check) only when no guard TP is resting.


**[entry] first_collateral_mode (2026-10-29 21:24 MT):** `pct` on SOL / ETH / BTC (was `fixed`). `first_collateral_pct` 0.10, `first_collateral_min_usd` 10.50, `first_collateral_max_usd` off. Free = own bank bal x coin price. `[sizing] initial_margin_usd` is only used in fixed mode.
