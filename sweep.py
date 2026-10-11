"""Grid sweep over any parameters, in parallel.

    python sweep.py --config configs/with_stops.toml --grid sweep.toml [--workers 8]

``sweep.toml`` maps dotted keys to value lists, e.g. ``"dca.max_adds" = [2, 3, 4]``.
Writes runs/sweep_<timestamp>_<hash>/results.csv: one row per run, flattened params + metrics.
"""
from __future__ import annotations

import argparse
import itertools
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Any

import pandas as pd

from backtest import RUNS_DIR, run_backtest
from config import Config, load_config, read_toml
from data import load
from indicators import compute_features

METRIC_KEYS = ["trades", "final_equity", "return_pct", "max_drawdown", "win_rate", "profit_factor",
               "liquidations", "avg_adds", "final_sol", "sharpe", "sortino"]
_features: dict[tuple[Any, ...], pd.DataFrame] = {}


def _feature_key(cfg: Config) -> tuple[Any, ...]:
    return (cfg.market, cfg.trend, cfg.signal)  # frozen dataclasses are hashable


def _run_one(cfg: Config) -> dict[str, Any]:
    key = _feature_key(cfg)
    if key not in _features:  # per-worker cache: indicators computed once per feature set
        _features[key] = compute_features(load(cfg.market.symbol, cfg.market.base_tf), cfg)
    m = run_backtest(cfg, _features[key]).metrics
    return {"hash": cfg.short_hash(), **cfg.flat(), **{k: m.get(k) for k in METRIC_KEYS}}


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    a = ap.parse_args()

    configs = [load_config(a.config, a.overrides, params=p) for p in expand_grid(read_toml(a.grid))]
    with ProcessPoolExecutor(a.workers) as pool:
        rows = list(pool.map(_run_one, configs))
    out = RUNS_DIR / f"sweep_{datetime.now():%Y%m%d-%H%M%S}_{configs[0].short_hash()}"
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out / "results.csv", index=False)
    print(df[["hash", *read_toml(a.grid), *METRIC_KEYS]].to_string(index=False))
    print("saved to", out / "results.csv")


if __name__ == "__main__":
    main()
