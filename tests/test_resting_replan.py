"""2026-10-24 (Trevor): resting add re-planned when the analyst level moves; no bare-level add on the first tick after a
restart (front-run ATR falls back to the analyst ATR, else no new add this tick). Fake chain only."""
import json
import logging

import pandas as pd
import pytest

from dca_levels import Levels, level_tf, load_analyst_levels
import types

from tests.test_resting import LV, ON, mkbot, req, state

ACC = types.SimpleNamespace(cash=600.0)   # without the deep rung the fixture ladder needs more free collateral

LV2 = Levels(supports=(104.7, 103.5), resistances=LV.resistances, atr=LV.atr,
             fine_supports=LV.fine_supports, fine_resistances=LV.fine_resistances)


@pytest.fixture
def bot(monkeypatch, tmp_path):
    return mkbot(monkeypatch, tmp_path, [*ON, "dca.ladder_deep=false"])


def placed(bot):
    st = state()
    pos = bot.chain_position(st)
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    ra = bot.local.rest_add
    assert ra and ra["pubkey"] == "LIM1"
    plan, _ = bot.rest_add_plan(pos, ACC.cash, 106.6)
    assert plan.source == "levels" and plan.trigger == 105.0
    st2 = state(reqs=st["openRequests"] + [req("LIM1", ra["trigger"], "increase")])
    bot.calls.clear()
    return st2, pos, ra


def lims(bot):
    return [c for c in bot.calls if c[0] in ("cancel-limit-order", "create-limit-order")]


def test_level_shift_beyond_threshold_replans_at_rule_price(bot, caplog):
    st2, pos, ra = placed(bot)
    assert ra["trigger"] == round(105.0 + 0.1 * LV.atr, 2)
    bot.strategy.levels_provider = lambda now: LV2   # support 105.0 -> 104.7: moved 0.3 > max(0.09, 0.25 x 0.9)
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
    c = lims(bot)
    assert [n for n, _ in c] == ["cancel-limit-order", "create-limit-order"] and c[0][1] == {"request": "LIM1"}
    new = round(104.7 + 0.1 * LV.atr, 2)
    plan, _ = bot.rest_add_plan(pos, ACC.cash + ra["margin"], 106.6)   # the cancel frees the old add's collateral
    assert c[1][1]["triggerPriceUsd"] == new and c[1][1]["sizeUsd"] == round(plan.margin * plan.leverage, 2)
    assert bot.local.rest_add["pubkey"] == "LIM2" and bot.local.rest_add["trigger"] == new
    assert any("RESTING ADD re-planned: %.2f -> %.2f" % (ra["trigger"], new) in r.getMessage() for r in caplog.records)
    bot.calls.clear()   # next tick: at the rule price -> nothing
    bot.resting_tick(pos, state(reqs=[req("R1", 110.33), req("LIM2", new, "increase")]), 106.6, ACC, 100.0)
    assert not lims(bot)


def test_small_level_shift_keeps_the_add(bot):
    st2, pos, ra = placed(bot)
    bot.strategy.levels_provider = lambda now: Levels((104.85, 103.5), LV.resistances, LV.atr)   # 0.15 <= 0.225
    bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
    assert not lims(bot) and bot.local.rest_add == ra


def test_replan_failed_cancel_keeps_the_old_add(bot):
    st2, pos, ra = placed(bot)
    bot.strategy.levels_provider = lambda now: LV2
    bot.chain = lambda: st2   # still on-chain
    real = bot.cmd

    def cmd(n, a):
        if n == "cancel-limit-order":
            bot.calls.append((n, a))
            return {"ok": False, "error": "boom"}
        return real(n, a)
    bot.cmd = cmd
    old = dict(bot.local.rest_add)
    bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
    assert [n for n, _ in lims(bot)] == ["cancel-limit-order"] and bot.local.rest_add == old


def test_replan_blocked_by_guard_keeps_the_add(bot):
    st2, pos, ra = placed(bot)
    bot.strategy.levels_provider = lambda now: LV2
    bot.guard = lambda *a: "blocked for test"
    bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
    assert not lims(bot) and bot.local.rest_add == ra


def test_unconfirmed_add_is_not_replanned(bot):
    st2, pos, ra = placed(bot)
    bot.local.rest_add["unconfirmed"] = True
    bot.strategy.levels_provider = lambda now: LV2
    bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
    assert not lims(bot)


def test_fixed_plan_is_not_replanned(bot):
    st2, pos, ra = placed(bot)
    bot._own_atr = 0.9
    bot.strategy.levels_provider = lambda now: Levels((), (), 0.9)   # stale analyst -> own-ATR fallback, "fixed" spacing
    plan, _ = bot.rest_add_plan(pos, ACC.cash, 106.6)
    assert plan is not None and plan.source == "fixed"
    bot.resting_tick(pos, st2, 106.6, ACC, 100.0)
    assert not lims(bot)


def test_front_uses_own_atr_then_analyst_then_none(bot):
    bot.strategy._levels = LV
    bot._own_atr = 2.0
    assert bot.add_front() == pytest.approx(0.2)
    bot._own_atr = None
    assert bot.front_atr() == LV.atr and bot.add_front() == pytest.approx(0.09)
    bot.strategy._levels = None
    assert bot.front_atr() is None and bot.add_front() == 0.0


def test_no_atr_at_all_skips_a_new_add_until_next_tick(bot, caplog):
    bot.strategy.levels_provider = lambda now: None   # no analyst levels, own ATR not fetched yet (first tick)
    st = state()
    pos = bot.chain_position(st)
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot.resting_tick(pos, st, 106.6, ACC, 100.0)
        bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    assert not [1 for n, _ in bot.calls if n == "create-limit-order"] and bot.local.rest_add is None
    assert sum("no ATR yet for the front-run" in r.getMessage() for r in caplog.records) == 1   # logged once
    bot._own_atr = 0.9   # next tick: klines fetched
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    lim = [a for n, a in bot.calls if n == "create-limit-order"]
    assert len(lim) == 1
    plan, _ = bot.rest_add_plan(pos, ACC.cash, 106.6)
    assert lim[0]["triggerPriceUsd"] == round(plan.trigger + 0.09, 2)


def test_zero_frontrun_places_without_atr(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, [*ON, "dca.ladder_deep=false", "dca.add_frontrun_atr=0"])
    b.strategy.levels_provider = lambda now: None
    st = state()
    b.resting_tick(b.chain_position(st), st, 106.6, ACC, 100.0)
    assert len([1 for n, _ in b.calls if n == "create-limit-order"]) == 1


def test_level_tf_from_analyst_file(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps({"timestamp": str(pd.Timestamp.now(tz="UTC")), "volatility": {"atr14_1h_usd_per_hour": 300.0},
                             "levels": {"h1": {"support": [80000.0], "resistance": [81099.7, 81461.0]},
                                        "h4": {"support": [79500.0], "resistance": [81461.0]}}}))
    lv = load_analyst_levels(p, 60)
    assert level_tf(lv, 81099.7) == "h1" and level_tf(lv, 81461.0) == "h1+h4" and level_tf(lv, 79500.0) == "h4"
    assert level_tf(lv, 1.0) == "?" and level_tf(None, 1.0) == "?"
