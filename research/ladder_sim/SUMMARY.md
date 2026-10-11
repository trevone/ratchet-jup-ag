# DCA add-ladder sizing sim (offline, 2026-10-09)
Files: `sim.py` (sim) → `results.csv` (14,940 rows: balance × scheme × N × spacing × widening × lev step), `rank.py` → `ranking.csv`, `report.py` → `ladders.png`.

## Model / assumptions
- Long from 100, price falls straight through the rungs. First entry = max($10.50, 10% balance) at 5x; free = balance − first − $0.10; $0.10 reserve per rung.
- Fees 6 bps open/close, maintenance 1% (config.py `maintenance_ratio`), borrow = 0, liq via `dca_levels.liq_price`.
- **ATR assumed = 1% of price**, so the live "liq ≥ 2 ATR beyond the next rung" rule = liq must stay ≥2% below the next rung. An add that breaks it ends the ladder (not shrunk, unlike live).
- Sizes fitted to the whole free balance with a $3 floor (floored rungs fixed at $3, the rest rescaled). Fixed spacing replaces analyst levels.
- Score (needs ≥8 adds at every balance): 35% adds (capped at 16), 40% "catch-up" (1 − break-even bounce ÷ depth, averaged over balances), 15% depth (capped at 15%), 10% liq margin (capped at 3%).

## Main findings
1. **The liquidation rule is what limits the ladder, not money.** Spacing of 1.5–2%, or gaps that widen ×1.1/×1.2, hit the 2-ATR liq rule after 5–9 adds. Only constant spacing of 0.75–1% gets 14–16+ adds, and the most depth any safe ladder covered was about 11–15%.
2. **Accelerating growth wins.** With ratio starting at 1.05–1.2 and rising +0.10–0.15 per add, the size jumps only at the last 3–4 rungs, where leverage is 17–20x. At $200 the break-even bounce from the deepest fill is ~1.4–1.6%, versus 2.2% for the current ladder at 1% spacing.
3. **The $3 floor dominates at small balances.** At $75–$200 the first 9–12 adds are $3 in every good scheme. The "small, slowly rising" early adds Trevor wants only show up from about $1000. Lowering the floor is not possible: $3 is the existing minimum.
4. Raising leverage by +1.5 or +2 per add never helped. It reaches 20x sooner and squeezes the liq margin, so keep +1x.

## Top configs (constant spacing, lev +1x/add, cap 20x)
| # | config | adds | depth | BE bounce $200 / $1000 | liq beyond deepest | min liq margin over 2-ATR | partial (last add, 1-rung bounce) $200 |
|---|---|---|---|---|---|---|---|
| A | accel r0=1.15 +0.10, N=16, 0.75% | 16 | 11.3% | 1.60% / 1.03% | 3.5–3.6% | 0.21% (at $75) | $10.97 |
| B | accel r0=1.15 +0.10, N=14, 0.75% | 14 | 10.0% | 1.41% / 1.03% | 4.0% | 1.19% | $10.38 |
| C | accel r0=1.05 +0.15, N=14, 1.0% | 14 | 13.1% | 1.76% / 1.21% | 3.7–3.9% | 0.10% | $15.28 |
| D | geom 1.6, N=16, 0.75% | 16 | 11.3% | 1.97% | ~2.9% | 0.15% | $7.42 |
| E | power p=3, N=16, 0.75% | 16 | 11.3% | 2.55% / 2.29% | ~2.9% | 0.05% | $4.58 |
| baseline | current (geom 1.5, 12 rungs), 1% | 12 | 11.4% | 2.22% / 2.07% | 4.2% | 1.19% | $8.64 |

Add sizes:
- A $200: 3×12, then 6.05, 14.23, 34.86, 87.15. $1000: 3×9, then 3.62, 7.43, 15.97, 35.93, 84.42, 206.84, 517.10.
- B $200: 3×9, then 3.72, 7.63, 16.41, 36.93, 86.79. $1000: 3×6, then 3.35, 5.86, 10.85, 21.15, 43.36, 93.23, 209.76, 492.94.
- C $200: 3×10, then 5.85, 14.63, 36.58, 91.44. $1000: 3×8, then 6.23, 14.03, 33.66, 84.16, 210.40, 526.01.
- Current $200: 3, 3, 3, 3, 3.38, 5.08, 7.61, 11.42, 17.13, 25.70, 38.55, 57.82. $1000: 3.49, 5.24, 7.85, … 134, 201, 302.

## Recommendation
**B: accelerating growth, 14 rungs, ratio starts at 1.15 and rises +0.10 per add (cap 2.5), lev +1x/add, cap 20x.**
- Versus the current ladder: 14 adds instead of 12, and the break-even bounce drops from about 2.1–2.2% to 1.0–1.4%.
- It keeps a healthy liq margin (≥1.2% beyond the 2-ATR rule at every balance), while A and C are only 0.1–0.2% over the rule at $75.
- A is the better choice if the extra 2 adds matter more than the liq margin.

Changes needed in the live bot (not made):
- `configs/live_100.toml [dca]`:
  - `ladder_max_rungs = 14` (was 12)
  - new keys `ladder_weight_mode = "accel"`, `ladder_weight_mult = 1.15`, `ladder_weight_mult_step = 0.10`, `ladder_weight_mult_cap = 2.5`
- `dca_levels.py`:
  - `LadderParams`: add those 3 fields.
  - `ladder_budgets()`: build accelerating weights (`w[k] = w[k-1]*r; r = min(cap, r+step)`) instead of `mult**k`.
  - Apply the $3 floor inside the budget, like `fit()` in sim.py: floored rungs fixed at $3 and the rest rescaled. Today the floor is applied per add, which over-spends.
- `config.py`: load the new keys.
- Rung spacing in levels mode comes from analyst levels (0.5–2 ATR). The sim says to keep real gaps around 0.75–1 ATR and avoid widening gaps.
- Backtest it with backtest.py before going live.
