"""Trevor 2026-10-27: minimum 0.85 ladder ATR between rungs on all three bots. The resting ladder add is PLACED >= 0.85
ladder (analyst) ATR beyond the add ref after the 0.1 own-ATR front-run ([dca] min_place_gap_atr, clamp in Bot.add_px);
levels must sit >= 0.95 ATR (min_gap_atr) and the fixed fallback is floored at the same 0.95 ATR (uniform spacing).
Re-arms after a TP3 fill keep readd_gap_atr (placed exactly, not clamped). Fake chain only."""
import types

import pytest

from config import DcaConfig, load_config
from dca_levels import LadderParams, Levels, ladder_rungs, next_add_level
from strategy import LONG, SHORT

from tests.test_resting import LV, ON, mkbot, req, state

ACC = types.SimpleNamespace(cash=600.0)
FLAGS = [*ON, "dca.ladder_deep=false", "dca.min_gap_atr=0.95", "dca.min_place_gap_atr=0.85"]


# ------------------------------------------------------------------ config
@pytest.mark.parametrize("path", ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"])
def test_live_values(path):
    c = load_config(path)
    d = c.dca
    assert (d.min_gap_atr, d.max_gap_atr, d.add_frontrun_atr, d.min_place_gap_atr) == (0.95, 1.2, 0.1, 0.85)
    assert d.min_gap_atr < d.max_gap_atr                            # level window not empty
    assert c.resting.readd_gap_atr == 0.6                            # unchanged


def test_default_off():
    assert DcaConfig().min_place_gap_atr == 0.0


def test_strategy_passes_the_fallback_floor_only_when_on():
    from strategy import DcaStrategy
    on = DcaStrategy(load_config("configs/live_btc.toml")).plan_kwargs()
    assert on["fixed_floor_atr"] == 0.95 and on["min_gap_atr"] == 0.95
    off = DcaStrategy(load_config("tests/fixtures/configs_pre_c/live_100.toml")).plan_kwargs()
    assert off["fixed_floor_atr"] == 0.0


# ------------------------------------------------------------------ pure: fallback floor + uniform ladder
def test_fixed_fallback_floored_at_min_gap_atr():
    lv = Levels(supports=(99.5,), resistances=(100.5,), atr=1.0)   # nothing within 0.95..1.2
    assert next_add_level(SHORT, 100.0, lv, 0.95, 1.2, 0.85) == (100.85, "fixed")           # floor off: unchanged
    assert next_add_level(SHORT, 100.0, lv, 0.95, 1.2, 0.85, 0.95) == (100.95, "fixed")
    assert next_add_level(LONG, 100.0, lv, 0.95, 1.2, 0.85, 0.95) == (99.05, "fixed")
    assert next_add_level(SHORT, 100.0, lv, 0.95, 1.2, 1.15, 0.95) == (101.15, "fixed")      # wider fallback kept
    assert next_add_level(SHORT, 100.0, None, 0.95, 1.2, 0.85, 0.95) == (100.85, "fixed")    # no levels: no ATR, unchanged


def test_level_window_0p95_1p2():
    lv = Levels(supports=(), resistances=(100.9, 101.1, 101.3), atr=1.0)
    assert next_add_level(SHORT, 100.0, lv, 0.95, 1.2, 0.85, 0.95) == (101.1, "levels")      # 0.9 too close, 1.1 nearest
    assert next_add_level(SHORT, 100.0, lv, 0.6, 1.2, 0.85) == (100.9, "levels")             # old 0.6 bound took 0.9


def test_ladder_rungs_beyond_rung0_keep_the_minimum():
    lv = Levels(supports=(), resistances=(100.5, 101.0, 101.4, 102.0), atr=1.0)
    lp = LadderParams(deep=False, min_rungs=4, max_rungs=4)
    r = ladder_rungs(SHORT, 100.0, lv, last_leverage=5, lev_step=2.5, lev_cap=20, min_gap_atr=0.95, max_gap_atr=1.2,
                     fixed_dist=0.5, lp=lp, fixed_floor_atr=0.95)
    px = [100.0] + [x.price for x in r]
    assert [x.source for x in r] == ["levels", "levels", "fixed", "fixed"]
    assert px[1:] == pytest.approx([101.0, 102.0, 102.95, 103.9])
    assert all(b - a >= 0.95 - 1e-9 for a, b in zip(px, px[1:]))
    old = ladder_rungs(SHORT, 100.0, lv, last_leverage=5, lev_step=2.5, lev_cap=20, min_gap_atr=0.95, max_gap_atr=1.2,
                       fixed_dist=0.5, lp=lp)
    assert old[2].price == pytest.approx(102.5)                     # floor off: the 0.5 fixed spacing as before


# ------------------------------------------------------------------ bot: clamp in add_px
@pytest.fixture
def bot(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, FLAGS)
    b.strategy._levels = LV            # live: set by rest_add_plan / plan_next_add just before add_px
    return b


def test_front_run_never_pulls_inside_0p85_atr(bot):
    pos = bot.chain_position(state())                     # long, last entry 106.439585, ladder ATR 0.9
    trig = 106.439585 - 0.95 * 0.9                        # level exactly at the 0.95 minimum
    bot._own_atr = 0.9                                    # front 0.09: placed 0.86 ATR away -> untouched
    assert bot.add_px(pos, trig) == round(trig + 0.09, 2)
    bot._own_atr = 3.0                                    # front 0.30 would place it 0.62 ATR away -> clamped
    px = bot.add_px(pos, trig)
    assert px == 105.67 and 106.439585 - px >= 0.85 * 0.9   # 106.439585 - 0.765 = 105.674585, rounded down (further)
    assert bot.add_px(pos, 105.0) == round(105.0 + 0.3, 2)   # far level: the normal front-run


def test_clamp_measured_from_the_last_partial_when_more_adverse(bot):
    pos = bot.chain_position(state())
    bot.local.last_partial_px = 106.2                     # long: lower than the last entry -> ref
    bot._own_atr = 3.0
    assert bot.add_px(pos, 106.2 - 0.95 * 0.9) == 105.43   # 106.2 - 0.765 = 105.435 -> down


def test_short_clamp_rounds_up(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, FLAGS)
    b.strategy._levels, b._own_atr = LV, 3.0
    pos = types.SimpleNamespace(side=SHORT, last_entry=100.001)
    assert b.add_px(pos, 100.001 + 0.95 * 0.9) == 100.77      # 100.001 + 0.765 = 100.766 -> up


def test_off_is_the_old_front_run(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.strategy._levels, b._own_atr = LV, 3.0
    pos = b.chain_position(state())
    assert b.add_px(pos, 105.6) == round(105.6 + 0.3, 2)


def test_placed_order_uses_the_clamp_and_does_not_churn(bot):
    bot._own_atr = 3.0
    bot.strategy.levels_provider = lambda now: Levels((105.58, 103.5), LV.resistances, LV.atr)   # 0.955 ATR below
    st = state()
    pos = bot.chain_position(st)
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    ra = bot.local.rest_add
    assert ra and ra["trigger"] == 105.67                  # clamped (front-run alone would give 105.88)
    st2 = state(reqs=st["openRequests"] + [req("LIM1", ra["trigger"], "increase")])
    for own, atr in ((3.2, 0.9), (3.0, 0.92), (2.5, 0.88)):  # ATR wiggles: plan moves < max(front, 0.25 ATR) -> stays
        bot._own_atr = own
        bot.strategy.levels_provider = lambda now, a=atr: Levels((105.58, 103.5), LV.resistances, a)
        bot.calls.clear()
        bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
        assert not [c for c in bot.calls if c[0] in ("cancel-limit-order", "create-limit-order")]
        assert bot.local.rest_add == ra


def test_rearm_is_placed_exactly_not_clamped(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, [*FLAGS, "resting.readd_gap_atr=0.6"])
    b.strategy._levels, b._own_atr = LV, 3.0
    pos = b.chain_position(state())
    plan = types.SimpleNamespace(trigger=106.2, margin=5.0, leverage=10.0, source="rearm")   # 0.27 ATR from the last entry
    args, _, _ = b.rest_add_args(pos, state(), plan)
    assert args["triggerPriceUsd"] == 106.2
