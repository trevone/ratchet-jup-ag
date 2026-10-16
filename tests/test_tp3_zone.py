"""TP3 rule (Trevor 2026-10-15): target = avg + f x (furthest add - avg), f tapers f_first (rung 1) -> f_deep (rung >= max);
TP3 covers exactly the adds that net a profit after fees + network at the target (no fee-minimum clamp, no level snap)."""
import pytest

from config import RestingConfig, TakeProfitConfig
from live import resting as R
from strategy import LONG, SHORT

KW = dict(open_fee=0.0006, close_fee=0.0006, network_usd=0.02)


def lot(price, size, t=None):
    return {"price": price, "size": size, "time": t or f"t{price}"}


def P(side, avg, adds, f_first=0.5, f_deep=0.27, max_rungs=10, **kw):
    return R.tp3_plan(side, avg, adds, max_rungs, f_first, f_deep, **{**KW, **kw})


def test_config_defaults_and_removed_keys():
    c = RestingConfig()
    assert (c.partial_zone_f_first, c.partial_zone_f_deep) == (0.50, 0.27)
    assert not hasattr(c, "partial_snap_levels") and not hasattr(c, "partial_zone_f_max")
    assert not hasattr(TakeProfitConfig(), "tp3_fee_mult")


@pytest.mark.parametrize("rung,f", [(1, 0.5), (4, 0.4233), (10, 0.27), (14, 0.27)])
def test_zone_f_linear_taper(rung, f):
    assert R.zone_f(rung, 10, 0.5, 0.27) == pytest.approx(f, abs=1e-4)


def test_zone_f_configurable():
    assert R.zone_f(1, 10, 0.4, 0.1) == pytest.approx(0.4) and R.zone_f(10, 10, 0.4, 0.1) == pytest.approx(0.1)


def test_eth_example_rung4_only_deepest_add_qualifies():
    adds = [lot(2390.870017, 75), lot(2409.510011, 100), lot(2441.00001, 125), lot(2462.790006, 150)]
    p = P(SHORT, 2425.74, adds)
    assert p["rung"] == 4 and p["far"] == 2462.790006 and p["f"] == pytest.approx(0.4233, abs=1e-4)
    assert p["px"] == 2441.42 and p["lots"] == ["t2462.790006"] and p["size"] == 150.0 and p["net_usd"] > 0


def test_long_mirror_deepest_is_lowest():
    p = P(LONG, 100.0, [lot(99.0, 50), lot(90.0, 60)], f_first=0.5)   # rung 2: f 0.5 - 0.23/9 -> target ~ 95.4
    assert p["far"] == 90.0 and 90.0 < p["px"] < 100.0 and p["lots"] == ["t90.0"] and p["size"] == 60.0


def test_no_add_nets_profit_no_tp3():
    assert P(LONG, 100.0, [lot(99.99, 50)]) is None            # target ~99.995 < entry fees
    assert P(LONG, 100.0, [lot(101.0, 50)]) is None            # add not beyond the avg (adverse side)
    assert P(LONG, 100.0, []) is None


def test_fees_and_network_decide_not_a_multiple():
    # target 100 - 0.5 = 99.5?? long avg 100, add 99 rung1 f .5 -> 99.5; add entry 99: gross 0.505%, fees 0.12% -> nets
    p = P(LONG, 100.0, [lot(99.0, 100)])
    assert p["px"] == 99.5 and p["lots"] == ["t99.0"]
    assert R.lot_net_at(LONG, 99.0, 100.0, 99.5, 0.0006, 0.0006, 0.02) > 0
    assert P(LONG, 100.0, [lot(99.0, 100)], network_usd=1.0) is None   # network eats it: no TP3


def test_all_adds_qualify_sums_sizes():
    p = P(SHORT, 100.0, [lot(104.0, 40), lot(106.0, 60)], f_first=0.5)   # rung 2 target ~102.85: both above
    assert p["size"] == 100.0 and len(p["lots"]) == 2

def test_live_sol_eth_20261016_no_lot_nets_positive_and_skip_reason():
    """Live 2026-10-16: chain avg 105.9849 / 2445.4447 with the re-armed add right at the market -> target ~ add, nets < 0."""
    sol = [lot(104.14, 75), lot(105.02, 100), lot(105.77, 125), lot(106.15, 150)]
    assert P(SHORT, 105.9849, sol) is None
    why = R.tp3_skip_why(SHORT, 105.9849, sol, 10, 0.5, 0.27, **KW)
    assert why.startswith("no lot nets positive: target 106.05") and "rung 4" in why and "106.15" in why
    eth = [lot(2390.87, 75), lot(2409.51, 100), lot(2438.85, 125), lot(2450.0, 150)]
    assert P(SHORT, 2445.4447, eth) is None
    assert R.tp3_skip_why(SHORT, 2445.4447, eth, 10, 0.5, 0.27, **KW).startswith("no lot nets positive: target 2447.37")
    # same lots, avg lower (a deeper add): the 106.15 lot nets positive again
    assert P(SHORT, 105.5, sol) is not None


def test_lots_avg_and_rebased_tp3_live_numbers():
    sol_all = [lot(102.830726, 50), lot(104.139999, 75), lot(105.02, 100), lot(105.769999, 125), lot(106.15, 150)]
    avg = R.lots_avg(sol_all)
    assert avg == pytest.approx(105.1851, abs=1e-3)
    t = P(SHORT, avg, sol_all[1:])
    assert t["px"] == 105.59 and t["size"] == 275.0 and t["rung"] == 4 and t["net_usd"] > 0
    assert R.lots_avg([]) is None
