"""Analyst levels loader: null / empty h1+h4 never crash (Analyst 20:48 MT), own-ATR fallback."""
import json

import pandas as pd

from dca_levels import load_analyst_levels

ATR = 357.0


def _write(tmp_path, levels):
    f = tmp_path / "latest.json"
    f.write_text(json.dumps({"timestamp": pd.Timestamp.now(tz="UTC").isoformat(), "levels": levels,
                             "volatility": {"atr14_1h_usd_per_hour": ATR}}))
    return f


def test_null_or_empty_levels_mean_no_levels_never_crash(tmp_path):
    """Analyst 20:48 MT: a failed candle fetch writes h1/h4 = null -> treated like a stale file (None)."""
    from asset_scale import levels_with_fallback
    for lv in ({"h1": None, "h4": None}, None, {}, {"h1": {"support": [], "resistance": []}, "h4": None},
               {"h1": {"support": None, "resistance": None}}, {"h1": None, "h4": {"support": None}}):
        assert load_analyst_levels(_write(tmp_path, lv), 60) is None, lv
    # partial: h1 null, h4 fine -> h4 levels only
    got = load_analyst_levels(_write(tmp_path, {"h1": None, "h4": {"support": [82921.77], "resistance": [84432.6],
                                                                "support_touches": [4], "resistance_touches": [2]}}), 60)
    assert got.supports == (82921.77,) and got.resistances == (84432.6,)
    # own-ATR fallback (ETH/BTC): no S/R levels, own ATR
    fb = levels_with_fallback(None, 350.0, True)
    assert fb.supports == () and fb.resistances == () and fb.atr == 350.0
