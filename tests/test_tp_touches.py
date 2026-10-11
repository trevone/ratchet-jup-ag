"""tp_res_min_touches (Trevor 2026-10-07 20:44/20:46 MT): smart-TP2 cap ignores weak (few-touch) analyst levels."""
import json

import pandas as pd

from config import TakeProfitConfig as TpConfig
from dca_levels import LONG, Levels, SmartTpParams, _touches, load_analyst_levels, nearest_opposing_tp_level, smart_tps

SHORT = -1
ATR = 357.0
LV = Levels(supports=(83074.0, 82921.77), resistances=(83641.9, 84432.6), atr=ATR,
            sup_touches=(1, 4), res_touches=(1, 2))


def test_default_off_every_level_caps():
    assert nearest_opposing_tp_level(LONG, 83163.0, LV) == 83641.9
    assert TpConfig().tp_res_min_touches == 0
    r = smart_tps(LONG, 83163.0, 73.5, [83412.5, 83786.7], [0.5, 1.0], 0, LV, SmartTpParams())
    assert abs(r.prices[1] - (83641.9 - 0.1 * ATR)) < 1e-6


def test_min_touches_skips_weak_resistance_tp2_passes():
    p = SmartTpParams(res_min_touches=2)
    assert nearest_opposing_tp_level(LONG, 83163.0, LV, 2) == 84432.6
    r = smart_tps(LONG, 83163.0, 73.5, [83412.5, 83786.7], [0.5, 1.0], 0, LV, p, market=83250.0)
    assert r.prices == (83412.5, 83786.7) and not r.reasons
    # a strong level still caps, fee floor still applies
    lv = Levels((), (83700.0,), ATR, res_touches=(2,))
    r = smart_tps(LONG, 83163.0, 73.5, [83412.5, 83786.7], [0.5, 1.0], 0, lv, p)
    assert abs(r.prices[1] - (83700.0 - 0.1 * ATR)) < 1e-6 and "touches ignored" in r.reasons[0]


def test_shorts_use_support_touches_and_unknown_is_strong():
    lv = Levels((2563.33, 2552.0), (), 14.0, sup_touches=(1, 1))
    assert nearest_opposing_tp_level(SHORT, 2580.5, lv, 2) is None
    assert nearest_opposing_tp_level(SHORT, 2580.5, lv, 0) == 2563.33
    unknown = Levels((2563.33,), (), 14.0)                       # no touch info -> strong
    assert nearest_opposing_tp_level(SHORT, 2580.5, unknown, 2) == 2563.33
    partial = Levels((2563.33, 2552.0), (), 14.0, sup_touches=(None, 1))
    assert nearest_opposing_tp_level(SHORT, 2580.5, partial, 2) == 2563.33


def test_touches_merge_h1_h4_and_loader(tmp_path):
    lv = {"h1": {"resistance": [83641.9, 84355.2], "resistance_touches": [1, 1], "support": [1.0]},
          "h4": {"resistance": [84432.6], "resistance_touches": [2]}}
    assert _touches(lv, "resistance", ATR) == (1, 3, 3)          # 84355.2 (h1) merges with 84432.6 (h4)
    assert _touches(lv, "support", ATR) == (None,)                # no touch list -> unknown
    f = tmp_path / "latest.json"
    f.write_text(json.dumps({"timestamp": pd.Timestamp.now(tz="UTC").isoformat(), "levels": lv,
                             "volatility": {"atr14_1h_usd_per_hour": ATR}}))
    got = load_analyst_levels(f, 60)
    assert got.res_touches == (1, 3, 3) and nearest_opposing_tp_level(LONG, 83163.0, got, 2) == 84355.2


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
    assert got.supports == (82921.77,) and got.resistances == (84432.6,) and got.res_touches == (2,)
    # own-ATR fallback (ETH/BTC): no S/R -> smart TP2 = plain avg + dist, no cap
    fb = levels_with_fallback(None, 350.0, True)
    assert fb.supports == () and fb.resistances == ()
    r = smart_tps(LONG, 83163.0, 73.5, [83412.5, 83786.7], [0.5, 1.0], 0, fb, SmartTpParams(res_min_touches=2))
    assert r.prices == (83412.5, 83786.7) and not r.reasons
    r = smart_tps(LONG, 83163.0, 73.5, [83412.5, 83786.7], [0.5, 1.0], 0, None, SmartTpParams())
    assert r.prices == (83412.5, 83786.7)
