"""Trevor 2026-10-27: (1) TP3 minimum net per partial as a % of the furthest add lot's notional ([resting]
partial_min_net_pct, net of open + close fees and network; f unchanged), (2) re-add spacing after a TP3 fill ([resting]
readd_gap_atr: more adverse of the closed lot's entry and last partial +/- gap x ladder ATR, set at fill time, placed exactly
there - no front-run creep, no cancel / re-place loop)."""
import dataclasses
import types

import pytest

from config import RestingConfig, load_config
from live import resting as R
from strategy import LONG, SHORT

from test_resting import ACC, ON, mkbot, rearm_setup   # noqa: E402  (same fake chain / fixture bot)

KW = dict(open_fee=0.0006, close_fee=0.0006, network_usd=0.02)


def lot(price, size, t=None):
    return {"price": price, "size": size, "time": t or f"t{price}"}


def P(side, avg, adds, pct=0.0, f_first=0.5, f_deep=0.27, max_rungs=10):
    return R.tp3_plan(side, avg, adds, max_rungs, f_first, f_deep, min_net_pct=pct, **KW)


# ------------------------------------------------------------------ config
def test_defaults_off_and_live_values():
    c = RestingConfig()
    assert (c.partial_min_net_pct, c.readd_gap_atr) == (0.0, 0.0)
    for f in ("configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"):
        r = load_config(f).resting
        assert (r.partial_min_net_pct, r.readd_gap_atr) == (0.002, 0.6)
        assert (r.partial_zone_f_first, r.partial_zone_f_deep) == (0.50, 0.27)   # f unchanged
    with pytest.raises(ValueError):
        RestingConfig(partial_min_net_pct=-0.001)
    with pytest.raises(ValueError):
        RestingConfig(readd_gap_atr=-1)


# ------------------------------------------------------------------ (1) TP3 minimum net %
def test_sol_20261027_cycle_target_moves_to_min_net():
    """Live 2026-10-27 07:09 MT: BE 100.2802, add $75 @ 100.70 -> f 0.5 target 100.49 netted ~$0.046."""
    old = P(SHORT, 100.2802, [lot(100.70, 75)])
    assert old["px"] == 100.49 and old["net_usd"] == pytest.approx(0.0464, abs=1e-3)
    new = P(SHORT, 100.2802, [lot(100.70, 75)], pct=0.002)
    assert new["px"] == 100.35 and new["size"] == 75.0
    assert new["net_usd"] >= 0.002 * 75 - 1e-9                      # >= $0.15 after fees + network
    assert R.lot_net_at(SHORT, 100.70, 75, 100.36, **KW) < 0.15     # one cent less far would miss it


def test_min_net_scales_with_lot_notional():
    a = P(SHORT, 99.0, [lot(100.0, 75)], pct=0.002)
    b = P(SHORT, 99.9, [lot(100.0, 150)], pct=0.002)
    assert a["net_usd"] >= 0.15 - 1e-9 and b["net_usd"] >= 0.30 - 1e-9
    assert b["px"] > a["px"] or b["px"] == pytest.approx(99.66, abs=0.01)   # bigger lot: network is a smaller %


def test_f_wins_when_already_further():
    assert P(SHORT, 100.0, [lot(104.0, 75)], pct=0.002)["px"] == P(SHORT, 100.0, [lot(104.0, 75)])["px"] == 102.0


def test_long_mirror_rounds_up():
    p = P(LONG, 100.0, [lot(99.9, 75)], pct=0.002)
    assert p["px"] == 100.25 and p["net_usd"] >= 0.15 - 1e-9          # 99.9 x (1 + 0.002 + 0.0012 + 0.02/75) = 100.2463 -> up
    assert P(LONG, 100.0, [lot(99.0, 75)], pct=0.002)["px"] == 99.5   # f further: unchanged


def test_pct_zero_is_the_old_rule_and_deep_rungs_keep_f():
    eth = [lot(2390.870017, 75), lot(2409.510011, 100), lot(2441.00001, 125), lot(2462.790006, 150)]
    assert P(SHORT, 2425.74, eth) == P(SHORT, 2425.74, eth, pct=0.0)
    p = P(SHORT, 2425.74, eth, pct=0.002)   # rung 4: f target 2441.42 is further than the min-net cap (~2454.6)
    assert p["px"] == 2441.42 and p["lots"] == ["t2462.790006"]


def test_min_net_cap_uses_the_furthest_lot_size_across_rungs():
    adds = [lot(100.5, 75), lot(100.9, 100)]   # rung 2, short; far lot $100 @ 100.9
    p = P(SHORT, 100.45, adds, pct=0.002)
    assert p["far"] == 100.9 and R.lot_net_at(SHORT, 100.9, 100, p["px"], **KW) >= 0.002 * 100 - 1e-9
    assert all(R.lot_net_at(SHORT, float(a["price"]), float(a["size"]), p["px"], **KW) > 0
               for a in adds if a["time"] in p["lots"])


def test_skip_why_reports_the_min_net_target():
    adds = [lot(100.70, 75)]
    why = R.tp3_skip_why(SHORT, 100.2802, adds, 10, 0.5, 0.27, min_net_pct=0.002, **KW)
    assert "target 100.35" in why


# ------------------------------------------------------------------ (2) re-add spacing after a TP3 fill
GAP = [*ON, "resting.readd_gap_atr=0.6", "resting.partial_min_net_pct=0.002"]


@pytest.fixture
def gbot(monkeypatch, tmp_path):
    return mkbot(monkeypatch, tmp_path, GAP)


def test_rearm_px_short_and_long_never_inside_the_closed_entry(gbot):
    gbot.strategy._levels = types.SimpleNamespace(atr=0.75)
    s, l = types.SimpleNamespace(side=SHORT), types.SimpleNamespace(side=LONG)
    # short, live 2026-10-27 numbers: closed $75 @ 100.70, partial 100.35 -> 100.35 + 0.45 = 100.80 (> entry)
    assert gbot.rearm_px(s, 100.70, 100.35) == 100.80
    # short, partial close to the entry: entry is more adverse -> entry, rounded UP (never below it)
    assert gbot.rearm_px(s, 100.7031, 100.20) == 100.71
    # long mirror: partial 106.8 -> 106.8 - 0.45 = 106.35 (< entry 106.44) ; entry more adverse -> entry rounded down
    assert gbot.rearm_px(l, 106.44, 106.8) == 106.35
    assert gbot.rearm_px(l, 106.4396, 106.6) == 106.15 and gbot.rearm_px(l, 106.4396, 106.9) == 106.43


def test_gap_off_keeps_the_old_entry(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    assert b.rearm_px(types.SimpleNamespace(side=SHORT), 100.7031, 100.35) == 100.7031


def test_readd_after_partial_set_at_fill_placed_exactly_and_no_loop(gbot):
    st2, pos, lot_ = rearm_setup(gbot)        # long fixture: TP3 closed the deepest add at 106.8, ladder ATR 0.9
    L = gbot.local
    want = min(round(lot_["price"], 2), round(106.8 - 0.6 * 0.9, 2))
    assert L.rearm["px"] == pytest.approx(want) and L.rearm["px"] <= lot_["price"]   # long: never above the closed entry
    gbot.calls.clear()
    gbot.resting_tick(pos, st2, 106.7, ACC, 100.0)
    calls = [c for c in gbot.calls if c[0] != "decrease"]
    assert [n for n, _ in calls] == ["cancel-limit-order", "create-limit-order"]
    assert calls[1][1]["triggerPriceUsd"] == L.rearm["px"]          # exactly the re-arm price: no 0.1 ATR front-run
    assert L.rest_add["trigger"] == L.rearm["px"]
    st3 = dict(st2, openRequests=[r for r in st2["openRequests"] if r["pubkey"] != "LIM1"]
               + [{"pubkey": "LIM2", "triggerPrice": L.rearm["px"], "executed": False, "requestType": "trigger",
                   "requestChange": "increase"}])
    for px in (106.7, 106.65, 106.9, 106.6, 106.75):   # many ticks, moving market: stays put, nothing cancelled / placed
        gbot.calls.clear()
        gbot.resting_tick(pos, st3, px, ACC, 100.0)
        assert not [c for c in gbot.calls if c[0] in ("cancel-limit-order", "create-limit-order")]
    assert L.rest_add["pubkey"] == "LIM2" and L.rearm["px"] == pytest.approx(want)


def test_restored_old_fronted_readd_is_not_churned(gbot):
    """Deploy-time state: a re-arm recorded by the old code (px = closed entry, order at entry - front) is left alone."""
    st2, pos, lot_ = rearm_setup(gbot)
    L = gbot.local
    L.rearm["px"] = lot_["price"]                                  # as the old code stored it
    L.rest_add = dict(L.rest_add, pubkey="OLD", trigger=gbot.add_px(pos, lot_["price"]))
    st3 = dict(st2, openRequests=[r for r in st2["openRequests"] if r["pubkey"] != "LIM1"]
               + [{"pubkey": "OLD", "triggerPrice": L.rest_add["trigger"], "executed": False, "requestType": "trigger",
                   "requestChange": "increase"}])
    gbot.calls.clear()
    gbot.resting_tick(pos, st3, 106.7, ACC, 100.0)
    assert not [c for c in gbot.calls if c[0] in ("cancel-limit-order", "create-limit-order")]
