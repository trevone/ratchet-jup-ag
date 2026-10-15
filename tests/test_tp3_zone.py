"""TP3 zone rule (Trevor 2026-10-15): zone edge entry + gap x f is the target; f tapers f_first (rung 1) -> f_deep (rung >= max);
analyst-level snapping optional (partial_snap_levels, default off)."""
import pytest

from config import RestingConfig
from dca_levels import Levels
from live import resting as R
from live import trim as T
from strategy import LONG, SHORT

LV = Levels(supports=(95.0, 92.0), resistances=(101.0, 103.0, 106.0), atr=1.0,
            fine_supports=(96.5,), fine_resistances=(100.4, 102.2, 104.5))
KW = dict(tol_atr=0.05, front_atr=0.0, front_pct=0.0, open_fee=0.0006, close_fee=0.0006, network_usd=0.02)


def Z(side, entry, avg, rung, lv=LV, f_first=0.5, f_deep=0.27, **kw):
    return R.partial_target_zone(side, entry, avg, 30.0, lv, rung, 10, f_first, f_deep, **{**KW, **kw})


def test_config_defaults():
    c = RestingConfig()
    assert (c.partial_zone_f_first, c.partial_zone_f_deep, c.partial_snap_levels) == (0.50, 0.27, False)
    assert not hasattr(c, "partial_zone_f_max")


@pytest.mark.parametrize("rung,f", [(1, 0.5), (4, 0.4233), (10, 0.27), (14, 0.27)])
def test_zone_f_linear_taper(rung, f):
    assert R.zone_f(rung, 10, 0.5, 0.27) == pytest.approx(f, abs=1e-4)


def test_zone_f_configurable():
    assert R.zone_f(1, 10, 0.4, 0.1) == pytest.approx(0.4) and R.zone_f(10, 10, 0.4, 0.1) == pytest.approx(0.1)


def test_rung1_is_zone_edge_no_special_case():
    t = Z(LONG, 100.0, 106.0, 1)   # f 0.5: edge 103.0 (no level snapping, no tolerance)
    assert t["mode"] == "zone_edge" and t["px"] == 103.0 and t["f"] == 0.5


def test_deep_rung_edge():
    t = Z(LONG, 100.0, 106.0, 10)   # f 0.27: 101.62
    assert t["mode"] == "zone_edge" and t["px"] == 101.62 and 100.0 < t["px"] < 106.0
    assert Z(LONG, 100.0, 106.0, 10, f_deep=0.25)["px"] == 101.5


def test_snap_levels_farthest_inside_zone_with_tolerance():
    t = Z(LONG, 100.0, 106.0, 1, snap_levels=True)   # zone .. 103.0: farthest level inside = 103.0 -> 102.95
    assert t["mode"] == "level" and t["level"] == 103.0 and t["px"] == round(T.trigger_price(LONG, 103.0, 1.0, 0.05), 2)
    t = Z(LONG, 100.0, 106.0, 10, snap_levels=True)   # zone .. 101.62: 101.0 farthest inside
    assert t["level"] == 101.0 and t["px"] == 100.95


def test_snap_no_level_inside_zone_uses_plain_edge():
    t = Z(LONG, 100.0, 100.5, 1, snap_levels=True)   # zone .. 100.25, no level inside (100.4 outside)
    assert t["mode"] == "zone_edge" and t["px"] == 100.25


def test_edge_not_covering_fees_falls_back_to_fee_minimum():
    t = Z(LONG, 100.0, 100.1, 2)   # edge ~100.05... too close: fee minimum (< avg is not required)
    assert t["mode"] == "fee_min" and t["net_usd"] > 0
    assert R.lot_net_at(LONG, 100.0, 30.0, round(t["px"] - 0.01, 2), 0.0006, 0.0006, 0.02) <= 0   # the minimum


def test_short_mirror():
    t = Z(SHORT, 100.0, 94.0, 10)   # edge 100 - 6 x 0.27 = 98.38
    assert t["mode"] == "zone_edge" and t["px"] == 98.38
    t = Z(SHORT, 100.0, 92.0, 1, snap_levels=True)   # zone 100 .. 96: 96.5 farthest inside -> 96.55
    assert t["level"] == 96.5 and t["px"] == 96.55


def test_tp3_at_or_past_avg_allowed_fee_min_floor():
    t = Z(LONG, 100.0, 99.0, 3)   # entry past avg: zone width 0 -> fee minimum, always a price
    assert t["mode"] == "fee_min" and t["px"] > 100.0 and t["net_usd"] > 0
    t2 = Z(LONG, 100.0, 100.0, 1, lv=None)
    assert t2["mode"] == "fee_min" and t2["px"] > 100.0 and t2["net_usd"] > 0
    s_ = Z(SHORT, 100.0, 101.0, 1, lv=None)
    assert s_["mode"] == "fee_min" and s_["px"] < 100.0
