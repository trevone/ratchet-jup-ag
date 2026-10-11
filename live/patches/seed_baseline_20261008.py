"""One-off (2026-10-08 17:15 MT): seed the wallet-baseline profit guard for positions opened before it existed.
Run while the bot's python child is stopped (wrapper restart window). Values reconstructed from each bot's log:
baseline = the 'budget <ASSET>: ... -> free $X' line just before the open / SOL price on the bar line before it;
deposited = sum of collateralAtoms of every increase of the open position (lamports). Payouts so far = 0 (no TP hit;
BTC's 13:35 manual trim was a loss taken from collateral 19.33 -> 19.02)."""
import json
import sys
from pathlib import Path

SEEDS = {
    "live/state.json": (97.11 / 110.304, [95191312]),                                  # SOL short open 17:00:24 MT
    "live/eth/state.json": (47.89 / 109.425, [95956563]),                              # ETH short open 14:15:44 MT
    "live/btc/state.json": (52.07 / 116.526, [90108661, 26009499, 26897324, 27630792]),  # BTC long open 10-07 20:42 MT + 3 adds
}

for path in sys.argv[1:]:
    p = Path(path)
    d = json.loads(p.read_text())
    if d.get("pg_base_sol") is not None or not d.get("side"):
        print(f"{path}: skipped (already seeded or flat)")
        continue
    base, atoms = SEEDS[path]
    d["pg_base_sol"], d["pg_dep_sol"], d["pg_recv_sol"] = round(base, 9), round(sum(atoms) / 1e9, 9), 0.0
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(p)
    print(f"{path}: pg_base_sol {d['pg_base_sol']:.6f} pg_dep_sol {d['pg_dep_sol']:.6f}")
