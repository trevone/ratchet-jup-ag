"""DCA add sizing (levels mode): broken-support target guard, formula guard, ladder budgeting, liq check."""
from dataclasses import replace

import pytest

from config import load_config
from dca_levels import (LadderParams, Levels, _SimPos, ladder_budgets, ladder_rungs, opposing_level, plan_add,
                        project_ladder)
from strategy import DcaStrategy

ATR = 0.835
# analyst levels around the 16:15 bug: h1 support 115.44 (broken), h4 supports 112.99/112.48
LV = Levels(supports=(115.44, 112.99, 112.48), resistances=(117.18, 119.05, 119.85, 119.88), atr=ATR)
PRICE = 115.70
WALLET_94 = (0.8147 - 0.02) * PRICE          # ~$92 free after the 0.02 SOL reserve (~$94 wallet)


def live_pos(**kw):
    p = dict(side=1, size=47.25, qty=47.25 / 115.897, margin=8.21, borrow=0.0, last_entry=115.40038741,
             last_leverage=7.0, first_entry=116.294316)
    p.update(kw)
    return _SimPos(**p)


@pytest.fixture(scope="module")
def kw():
    return DcaStrategy(load_config("tests/fixtures/configs_pre_c/live_100.toml")).plan_kwargs()


def test_broken_support_is_not_the_target():
    # add at 114.55: the 115.44 support is above the add price (broken) -> next real resistance 117.18
    assert opposing_level(1, 114.55, LV, 2.0, 0.5) == 117.18
    # even with flipped supports allowed, a level within 0.5 ATR of the add price is skipped
    lv = Levels((114.80, 112.99), (117.18,), ATR)
    assert opposing_level(1, 114.55, lv, 2.0, 0.5, flipped=True) == 117.18
    assert opposing_level(1, 114.55, LV, 2.0, 0.5, flipped=True) == 115.44   # 0.89 > 0.5 ATR: allowed if flipped


def test_bug_case_no_longer_all_in(kw):
    # old code: $91.91 x9 (whole wallet). Without any ladder or max_add_frac cap the formula alone must be sane.
    k = dict(kw, ladder=None, max_add_frac=1.0)
    a = plan_add(live_pos(), LV, available_usd=WALLET_94, **k)
    assert a is not None and a.trigger == pytest.approx(114.55, abs=0.01) and a.target_level == 117.18
    assert a.margin < 0.1 * WALLET_94
    # same with flipped supports on (target 115.44 -> wanted avg $0.06 above the fill): guard bounds it
    a2 = plan_add(live_pos(), LV, available_usd=WALLET_94, **dict(k, target_flipped_supports=True))
    assert a2 is not None and a2.margin < 0.1 * WALLET_94


def test_formula_guard_tiny_gap_is_finite(kw):
    # resistance placed so the wanted avg lands 0.001 above the trigger -> formula would explode
    lv = Levels((), (114.55 + ATR + 0.001,), ATR)
    k = dict(kw, ladder=None, max_add_frac=1.0, min_gap_atr=0.5, max_gap_atr=2.0)
    a = plan_add(live_pos(), lv, available_usd=1e6, **k)
    assert a is not None and a.margin * a.leverage < 3 * 47.25   # bounded, not toward infinity / whole wallet


def test_ladder_94_wallet(kw):
    lp = kw["ladder"]
    rungs = ladder_rungs(1, 115.40038741, LV, last_leverage=7, lev_step=2, lev_cap=30, min_gap_atr=0.5,
                         max_gap_atr=2.0, fixed_dist=0.85, lp=lp, first_entry=116.294316)
    assert len(rungs) >= lp.min_rungs + 1
    assert all(a.price > b.price for a, b in zip(rungs, rungs[1:]))          # strictly descending
    assert [r.leverage for r in rungs][:3] == [9, 11, 13]                     # 5x +2 per add (7 -> 9 ...)
    assert rungs[1].price == 112.99 and rungs[2].price == 112.48              # analyst supports used
    b = ladder_budgets(rungs, WALLET_94, lp)
    assert sum(r.budget for r in b) == pytest.approx(WALLET_94 - lp.fee_usd_per_add * len(b))
    assert all(x.budget < y.budget for x, y in zip(b, b[1:]))                 # deeper = more
    assert b[1].budget / b[0].budget == pytest.approx(lp.weight_mult)
    a = plan_add(live_pos(), LV, available_usd=WALLET_94, **kw)
    assert a is not None and 3.0 <= a.margin <= b[0].budget + 1e-9 and a.margin < 0.10 * WALLET_94


def test_deposit_flows_in(kw):
    a = plan_add(live_pos(), LV, available_usd=WALLET_94, **kw)
    b = plan_add(live_pos(), LV, available_usd=WALLET_94 * 2, **kw)
    assert b.margin > a.margin


def test_min_floor_and_tiny_wallet(kw):
    assert plan_add(live_pos(), LV, available_usd=2.0, **kw) is None          # under $3 -> no add
    a = plan_add(live_pos(), LV, available_usd=10.0, **kw)
    assert a is not None and a.margin == pytest.approx(3.0)


def test_min_mode_is_min_of_formula_and_budget(kw):
    k = dict(kw, ladder=replace(kw["ladder"], sizing="min"))
    a = plan_add(live_pos(), LV, available_usd=WALLET_94, **k)
    budget_only = plan_add(live_pos(), LV, available_usd=WALLET_94, **kw)
    assert a.margin <= budget_only.margin + 1e-9


def test_projection_liq_beyond_next_rung_and_within_wallet(kw):
    plans = project_ladder(live_pos(), LV, available_usd=WALLET_94, **kw)
    assert len(plans) >= 4
    used = 0.0
    for p in plans:
        assert p.liq < p.next_level < p.trigger                              # liq stays below the next rung
        assert p.leverage <= 30
        used += p.margin
    assert used <= WALLET_94
    assert plans[0].new_avg == pytest.approx(
        (47.25 + plans[0].margin * plans[0].leverage) /
        (47.25 / 115.897 + plans[0].margin * plans[0].leverage / plans[0].trigger))


def test_stale_levels_fixed_spacing(kw):
    a = plan_add(live_pos(), None, available_usd=WALLET_94, **kw)
    assert a is not None and a.trigger == pytest.approx(115.40038741 - 0.85) and a.margin < 0.15 * WALLET_94


def test_max_add_frac_still_caps(kw):
    a = plan_add(live_pos(), LV, available_usd=WALLET_94, **dict(kw, max_add_frac=0.04))
    assert a.margin == pytest.approx(max(3.0, 0.04 * WALLET_94))


def test_projection_grows_with_depth_uses_wallet_and_keeps_liq_buffer(kw):
    plans = project_ladder(live_pos(), LV, available_usd=WALLET_94, **kw)
    assert 4 <= len(plans) <= 7 and plans[-1].source.startswith("deep")
    assert all(b.margin > a.margin for a, b in zip(plans, plans[1:]))         # collateral grows with depth
    assert sum(p.margin for p in plans) > 0.95 * WALLET_94                     # whole wallet used by the deep rung
    for p in plans:
        assert p.next_level - p.liq >= 2.0 * ATR - 1e-9                        # liq >= 2 ATR below the next rung
        assert p.margin * p.leverage == pytest.approx(p.margin * p.leverage)
