"""Explicit TP placement (Trevor 2026-10-15, Option A): tp1_pct / tp2_pct = percent from the average entry."""
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from config import TakeProfitConfig, TpLevel, load_config
from strategy import LONG, SHORT, DcaStrategy

LIVE_FILES = ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"]


def pos(side, avg, size, tp_hits=0):
    return NS(side=side, avg_entry=avg, size=size, qty=size / avg, margin=size / 5, n_entries=1, first_entry=avg,
              last_entry=avg, tp_hits=tp_hits, borrow=0.0, last_leverage=5.0, first_margin=size / 5, opened=None,
              first_tp_time=None, price_for_net_pnl=lambda x: avg)


def strat(path="configs/live_100.toml", **tp):
    c = load_config(path, ["live.dry_run=true"])
    return DcaStrategy(replace(c, tp=replace(c.tp, **tp)))


def prices(s, p, market):
    return [round(t.price, 2) for t in s.tp_targets(p, price=market)]


def test_validation():
    lv = (TpLevel(0.45, 0.5), TpLevel(0.75, 1.0))
    TakeProfitConfig(levels=lv, tp1_pct=0.008, tp2_pct=0.015)
    for a, b in ((0.0, 0.01), (0.01, 0.01), (0.02, 0.01), (-0.01, 0.01)):
        with pytest.raises(ValueError):
            TakeProfitConfig(levels=lv, tp1_pct=a, tp2_pct=b)
    with pytest.raises(ValueError):
        TakeProfitConfig(levels=lv, tp1_pct=0.008)            # set together
    with pytest.raises(ValueError):
        TakeProfitConfig(tp1_pct=0.008, tp2_pct=0.015)        # needs a two-level ladder
    assert TakeProfitConfig().tp1_pct is None


@pytest.mark.parametrize("f", LIVE_FILES)
def test_live_files_set_pct(f):
    t = load_config(f).tp
    assert (t.tp1_pct, t.tp2_pct) == (0.008, 0.015)


def test_sol_mode_is_price_abs():
    t = load_config("configs/live_100.toml").tp
    assert t.mode == "price_abs" and [x.dist for x in t.levels] == [0.45, 0.75]


def test_sol_short_dry_run():
    assert prices(strat(), pos(SHORT, 104.3207, 225.0), 104.30) == [103.49, 102.76]


def test_eth_short_dry_run():
    s = strat("configs/live_eth.toml")
    assert prices(s, pos(SHORT, 2425.74, 500.0), 2420.0) == [2406.33, 2389.35]


def test_long_above_avg_and_bypasses_levels_and_levels_provider():
    s = strat()
    s.levels_provider = lambda now: (_ for _ in ()).throw(AssertionError("analyst levels must not be read"))
    assert prices(s, pos(LONG, 100.0, 200.0), 100.0) == [100.8, 101.5]


def test_fee_floor_wins_when_pct_price_is_inside_it():
    s = strat(tp1_pct=0.0001, tp2_pct=0.0002)
    t = s.tp_targets(pos(LONG, 100.0, 200.0), price=100.0)
    f1, f2 = s.last_tp.floors
    assert t[0].price == pytest.approx(f1) and t[1].price >= f2 - 1e-9 and t[0].price > 100.0 * 1.0001


def test_market_clamp_and_min_gap_still_apply():
    s = strat()
    t = s.tp_targets(pos(SHORT, 100.0, 200.0), price=99.0)   # TP1 99.2 / TP2 98.5 inside; push market below TP1
    assert t[0].price < 99.0 - 0.02 + 1e-9 and any(s.last_tp.past_market)
    assert t[0].price - t[1].price >= 0.05 - 1e-9


def test_after_tp1_fill_tp2_only_pending():
    s = strat()
    t = s.tp_targets(pos(SHORT, 100.0, 100.0, tp_hits=1), price=99.0)
    assert round(t[1].price, 2) == 98.5 and s.last_tp.floors[0] is None


def test_unset_keeps_levels_ladder():
    s = strat(tp1_pct=None, tp2_pct=None)
    assert prices(s, pos(SHORT, 104.3207, 225.0), 104.30) == [103.87, 103.57]   # avg -0.45 / -0.75 (price_abs)
