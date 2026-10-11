"""3-month sanity check for dca.mode="levels" using confirmed 1h swing highs/lows as the S/R proxy."""
from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd

from backtest import run_backtest
from config import load_config
from dca_levels import Levels
from indicators import compute_features

P = "data/SOLUSDT_15m_recent.pkl"
PIVOT = 5          # bars each side on 1h
KEEP = 12          # most recent confirmed pivots of each kind


def swing_provider(base: pd.DataFrame):
    h = base.resample("1h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = pd.concat([h.high - h.low, (h.high - h.close.shift()).abs(), (h.low - h.close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    atr_avail = atr.copy(); atr_avail.index = atr.index + pd.Timedelta("1h")          # known after the bar closes
    hi, lo = h.high.to_numpy(), h.low.to_numpy()
    piv = []
    for i in range(PIVOT, len(h) - PIVOT):
        conf = h.index[i + PIVOT] + pd.Timedelta("1h")
        if hi[i] == hi[i - PIVOT:i + PIVOT + 1].max():
            piv.append((conf, hi[i], "r"))
        if lo[i] == lo[i - PIVOT:i + PIVOT + 1].min():
            piv.append((conf, lo[i], "s"))
    piv.sort()
    conf_t = pd.DatetimeIndex([p[0] for p in piv])

    def provider(now: pd.Timestamp) -> Levels | None:
        t = now + pd.Timedelta("15m")
        k = conf_t.searchsorted(t, side="right")
        ai = atr_avail.index.searchsorted(t, side="right") - 1
        if ai < 14:
            return None
        recent = piv[max(0, k - 4 * KEEP):k]
        # role by position vs price is handled by the planner; supply both kinds as both pools
        lv = [p[1] for p in recent]
        return Levels(tuple(lv), tuple(lv), float(atr_avail.iloc[ai]))
    return provider


def go(name: str, params: dict) -> None:
    base = pd.read_pickle(P)
    start = str((base.index[-1] - pd.Timedelta(days=91)).date())
    c = load_config("configs/live_100.toml", params={**params, "market.start": start})
    f = compute_features(base, c)
    t0 = time.time()
    import backtest
    orig = backtest.DcaStrategy

    class S(orig):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.levels_provider = swing_provider(base)
    backtest.DcaStrategy = S
    try:
        r = run_backtest(c, f)
    finally:
        backtest.DcaStrategy = orig
    m = r.metrics
    days = (f.index[-1] - f.index[0]) / pd.Timedelta("1D")
    tr = r.trades
    adds = tr["adds"].mean()
    mx = f"max_adds {tr['adds'].max()}  max_coll ${tr['margin'].max():.2f}  max_size ${tr['size'].max():.0f}  worst_trade_adverse ${tr['max_adverse'].min():.2f}"
    print(f"{name:16s} trades/day {m['trades'] / days:.2f}  trades {m['trades']}  final ${m['final_equity']:.2f}  "
          f"ret {m['return_pct']:.2%}  maxDD {m['max_drawdown']:.2%}  liqs {m['liquidations']}  "
          f"avg_adds {adds:.2f}  {mx}  exits {m.get('exit_reasons')}  ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    go("fixed $0.85/$5", {"dca.mode": "fixed"})
    go("levels", {"dca.mode": "levels"})
