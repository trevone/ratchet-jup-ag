"""Per-asset scaling of the price-distance parameters (ETH / BTC bots; SOL keeps its USD values).

SOL's live config is written in USD price distances tuned at ~$116 (TP +$0.50 / +$0.75, fixed spacing $0.85, smart-TP
gaps of a few cents). Those are meaningless at ETH ~$2.6k or BTC ~$83k, so the [scale] section re-derives them every
tick from the current price and the asset's own 1h ATR:

    dca.trigger_dist        = trigger_dist_atr * ATR(1h)        (or trigger_dist_pct * price)
    tp.tp_min_gap_usd       = tp_min_gap_pct * price
    tp.tp_min_move          = tp_min_move_pct * price
    tp.tp_market_gap        = tp_market_gap_pct * price

TP distances themselves use tp.mode = "price_pct" (fraction of the average entry). Everything else in levels mode is
already in ATR multiples (min/max gap, target, liq buffer, deep rung) or USD money (fees, min add, first-entry floor),
which does not depend on the asset's price. ``scale.own_atr_fallback``: when the analyst file for the asset is
missing/stale, levels mode gets Levels(no S/R, ATR = own 1h ATR) instead of None, so the ATR-based ladder, deep rung and
2-ATR liquidation buffer still use a real ATR (not trigger_dist as a stand-in) with fixed ATR spacing between rungs.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from dca_levels import Levels


def atr_from_klines(df: pd.DataFrame, tf: str = "1h", length: int = 14) -> float | None:
    """Wilder ATR on ``tf`` bars resampled from (closed) klines with open/high/low/close columns."""
    if df is None or df.empty:
        return None
    h = df.resample(tf).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(h) < length + 1:
        return None
    tr = pd.concat([h.high - h.low, (h.high - h.close.shift()).abs(), (h.low - h.close.shift()).abs()], axis=1).max(axis=1)
    v = float(tr.ewm(alpha=1 / length, adjust=False).mean().iloc[-1])
    return v if v > 0 else None


def scale_config(cfg: Any, price: float, atr: float | None) -> Any:
    """Copy of ``cfg`` with the [scale] keys applied (no-op when scale.enabled is false or price is unknown)."""
    sc = getattr(cfg, "scale", None)
    if sc is None or not sc.enabled or not price or price <= 0:
        return cfg
    dca, tp = {}, {}
    if sc.trigger_dist_atr is not None and atr:
        dca["trigger_dist"] = round(sc.trigger_dist_atr * atr, 6)
    elif sc.trigger_dist_pct is not None:
        dca["trigger_dist"] = round(sc.trigger_dist_pct * price, 6)
    for key, pct in (("tp_min_gap_usd", sc.tp_min_gap_pct),
                     ("tp_min_move", sc.tp_min_move_pct), ("tp_market_gap", sc.tp_market_gap_pct)):
        if pct is not None:
            tp[key] = round(pct * price, 6)
    out = cfg
    if dca:
        out = replace(out, dca=replace(out.dca, **dca))
    if tp:
        out = replace(out, tp=replace(out.tp, **tp))
    return out


def scaled_summary(cfg: Any) -> str:
    return (f"trigger_dist {cfg.dca.trigger_dist:.4f}, tp_min_gap "
            f"{cfg.tp.tp_min_gap_usd:.4f}, tp_min_move {cfg.tp.tp_min_move:.4f}, tp_market_gap {cfg.tp.tp_market_gap:.4f}, "
            f"TP ladder {cfg.tp.mode} " + ", ".join(f"{lv.dist:g}->{lv.close_pct:.0%}" for lv in cfg.tp.ladder))


def levels_with_fallback(analyst: Levels | None, own_atr: float | None, enabled: bool) -> Levels | None:
    """Analyst levels if fresh; else (enabled) no S/R but the asset's own 1h ATR; else None (old behaviour)."""
    if analyst is not None or not enabled or not own_atr:
        return analyst
    return Levels((), (), float(own_atr))
