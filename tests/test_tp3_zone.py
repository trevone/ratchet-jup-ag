"""TP3 zone rule (Trevor 2026-10-12 00:53 MT): target between the covered entry and the trade avg."""
import pytest

from dca_levels import Levels
from live import resting as R
from live import trim as T
from strategy import LONG, SHORT

LV = Levels(supports=(95.0, 92.0), resistances=(101.0, 103.0, 106.0), atr=1.0,
            fine_supports=(96.5,), fine_resistances=(100.4, 102.2, 104.5))
KW = dict(tol_atr=0.05, front_atr=0.0, front_pct=0.0, open_fee=0.0006, close_fee=0.0006, network_usd=0.02)


@pytest.mark.parametrize("rung,f", [(1, 0.0), (4, 0.1667), (10, 0.5), (14, 0.5)])
def test_zone_f_linear_to_half(rung, f):
    assert R.zone_f(rung, 10, 0.5) == pytest.approx(f, abs=1e-4)


def test_rung1_first_level_past_entry():
    t = R.partial_target_zone(LONG, 100.0, 106.0, 30.0, LV, 1, 10, 0.5, **KW)
    assert t["mode"] == "level" and t["level"] == 100.4 and t["px"] == round(T.trigger_price(LONG, 100.4, 1.0, 0.05), 2)


def test_deep_rung_farthest_level_inside_zone_never_at_avg():
    # rung 10, f 0.5: zone 100 .. 103 -> farthest level inside = 103.0 (101, 102.2 nearer; 104.5 outside)
    t = R.partial_target_zone(LONG, 100.0, 106.0, 30.0, LV, 10, 10, 0.5, **KW)
    assert t["level"] == 103.0 and t["px"] == 102.95 and 100.0 < t["px"] < 106.0
    t2 = R.partial_target_zone(LONG, 100.0, 106.0, 30.0, LV, 10, 10, 0.25, **KW)   # f_max configurable: zone .. 101.5
    assert t2["level"] == 101.0


def test_no_level_in_zone_uses_zone_edge_with_front_run():
    t = R.partial_target_zone(LONG, 100.0, 106.0, 30.0, LV, 2, 10, 0.5, **KW)   # f 0.0556: zone .. 100.333, no level
    assert t["mode"] == "zone_edge" and t["px"] == round(100.0 + 6.0 * 0.5 / 9 - 0.05, 2) and t["net_usd"] > 0


def test_zone_edge_not_covering_fees_falls_back_to_fee_minimum():
    # entry 100, avg 101 -> rung 2 edge 100.0556 - 0.05 = 100.01: does not cover fees -> fee minimum (< avg)
    t = R.partial_target_zone(LONG, 100.0, 101.0, 30.0, LV, 2, 10, 0.5, **KW)
    assert t["mode"] == "fee_min" and t["net_usd"] > 0 and t["px"] < 101.0
    assert R.lot_net_at(LONG, 100.0, 30.0, round(t["px"] - 0.01, 2), 0.0006, 0.0006, 0.02) <= 0   # the minimum


def test_short_mirror_uses_supports_between_entry_and_avg():
    t = R.partial_target_zone(SHORT, 100.0, 94.0, 30.0, LV, 10, 10, 0.5, **KW)   # zone 100 .. 97: 96.5 outside -> edge
    assert t["mode"] == "zone_edge" and t["px"] == 97.05
    t = R.partial_target_zone(SHORT, 100.0, 92.0, 30.0, LV, 10, 10, 0.5, **KW)   # zone .. 96: 96.5 farthest inside
    assert t["level"] == 96.5 and t["px"] == 96.55


def test_entry_not_underwater_or_level_beyond_avg_refused():
    assert R.partial_target_zone(LONG, 100.0, 99.0, 30.0, LV, 3, 10, 0.5, **KW)["px"] is None
    # rung 1 first level 100.4 lies beyond avg 100.3 -> fee minimum instead (still < avg)
    t = R.partial_target_zone(LONG, 100.0, 100.3, 30.0, LV, 1, 10, 0.5, **KW)
    assert t["mode"] == "fee_min" and t["px"] < 100.3
    assert R.partial_target_zone(LONG, 100.0, 100.1, 30.0, LV, 1, 10, 0.5, **KW)["px"] is None   # fees need > avg
