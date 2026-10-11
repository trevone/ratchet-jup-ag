# Ratchet settings cheat sheet

Values read from `configs/live_100.toml` (SOL), `configs/live_eth.toml` (ETH), `configs/live_btc.toml` (BTC) on 2026-10-10 19:10 MT.
Edit the config, back it up first (`*.bak-YYYYMMDD-HHMM`), then `sudo systemctl restart ratchet-<sol|eth|btc>`.

| Section | Key | SOL | ETH | BTC | What it does |
|---|---|---|---|---|---|
| [signal] | tf | 15m | 15m | 15m | RSI timeframe for entries (trend filter = 4h EMA200) |
| [signal] | oversold / overbought | 45 / 55 | 45 / 55 | 45 / 55 | Long entry RSI < oversold, short entry RSI > overbought |
| [signal] | idle_after_min / idle_step_min | 60 / 30 | 60 / 30 | 60 / 30 | Flat this long -> loosen RSI thresholds in steps |
| [signal] | idle_long_rsi_steps / idle_short_rsi_steps | [48,50] / [52,50] | same | same | Loosened RSI thresholds while idle |
| [entry] | first_collateral_pct | 0.10 | 0.10 | 0.10 | First entry = 10% of bot's free bank (USD) |
| [entry] | first_collateral_min_usd | 10.50 | 10.50 | 10.50 | Floor for first entry collateral |
| [entry] | fee_reserve_usd | 0.10 | 0.10 | 0.10 | Kept back from first entry for add fees |
| [dca] | mode | levels | levels | levels | Adds at analyst S/R levels |
| [dca] | levels_path | market-analysis/latest.json | market-analysis/eth/latest.json | market-analysis/btc/latest.json | Analyst levels file |
| [dca] | levels_max_age_min | 45 | 45 | 45 | Older -> fallback spacing |
| [dca] | min_gap_atr / max_gap_atr | 0.5 / 2.0 | 0.5 / 2.0 | 0.5 / 2.0 | Allowed gap last fill -> add level (1h ATR) |
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
| [tp] | levels (TP1 / TP2 dist) | $0.45 / $0.75 | 0.45% / 0.75% | 0.45% / 0.75% | Base TP distances (TP1 closes 50%, TP2 rest) |
| [tp] | tp1_mode / tp2_from_tp1_ratio | level / 0.6 | level / 0.6 | level / 0.6 | Trevor 2026-10-11: TP1 front-runs nearest analyst level (main+fine) beyond avg clearing fee floor/market gap, set once at open/add; TP2 = avg + (TP1-avg)/0.6 (see STRATEGY.md TP rules). tp1_ratio unused in level mode |
| [tp] | smart_tp2 / tp_res_buffer_atr | true / 0.1 | true / 0.1 | true / 0.1 | TP2 capped at nearest resistance - 0.1 ATR |
| [tp] | tp_res_min_touches | 2 | 2 | 2 | Only levels with >= 2 touches cap TP2 |
| [tp] | tp_periodic | false | false | false | TPs set only at open/add |
| [tp] | close_remainder_after_min | 55 | 55 | 55 | After TP1, close remainder if net > 0 and equity >= HWM |
| [trim] | trim_trigger | distance_level | distance_level | distance_level | Per-add trim mode |
| [trim] | trim_arm_dist_pct | 0.0065 | 0.0065 | 0.0065 | Arm when price 0.65% against avg |
| [trim] | trim_level_front_run_atr / tol_atr | 0.15 / 0.1 | same | same | Fire 0.25 ATR before next S/R level |
| [trim] | trim_min_gain_pct / trim_min_net_usd | 0.005 / 0.25 | same | same | Only adds with >= 0.5% gain and >= $0.25 net |
| [profit_guard] | enabled / close_when_met / close_requires | true / true / adds_or_trims | same | same | Market-close whole position when equity >= HWM + min profit |
| [profit_guard] | min_trade_profit_sol | 0.001 | 0.001 | 0.001 | **Legacy field, not used by the live guard** (see [ledger] min_profit_native) |
| [ledger] | enabled | true | true | true | Per-bot own-coin bank + HWM |
| [ledger] | min_profit_native | 0.0015 SOL | 0.000066 ETH | 0.00000195 WBTC | Live guard threshold above HWM (~$0.17) |
| [ledger] | ext_min_usd / window_ticks | 1.5 / 3 | same | same | External deposit/withdrawal detection |
| [ledger] | withdraw_enabled / time / fraction / min_usd | true / 00:05 / 0.5 / 0.50 | same | same | Daily 50% of HWM gain -> JupSOL |
| [ledger] | fee_settle_min_sol | 0.005 | 0.005 | 0.005 | ETH/BTC settle SOL fee debt above this |
| [withdraw] | enabled | true | false | false | Legacy SOL daily stake job (SOL bot only) |
| [bot] | asset / share / state_dir | SOL / 0.5 / live | ETH / 0.25 / live/eth | BTC / 0.25 / live/btc | Market, wallet share, state dir |
