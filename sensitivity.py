"""Borrow-fee and last-2-years sensitivity for presets A and B (thin wrapper over sweep logic)."""
from __future__ import annotations

import pandas as pd

from config import load_config
from sweep import METRIC_KEYS, _run_one

PRESETS = ["configs/pure_dca.toml", "configs/with_stops.toml"]
CASES = [{"costs.borrow_per_hour": 0.00002}, {"costs.borrow_per_hour": 0.0},
         {"market.start": "2024-10-01"}, {"market.start": "2024-10-01", "costs.borrow_per_hour": 0.00002}]

if __name__ == "__main__":
    rows = [_run_one(load_config(p, params=c)) for p in PRESETS for c in CASES]
    df = pd.DataFrame(rows)
    print(df[["name", "costs.borrow_per_hour", "market.start", *METRIC_KEYS]].to_string(index=False))
    df.to_csv("sensitivity.csv", index=False)
