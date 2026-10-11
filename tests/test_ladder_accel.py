"""Accelerating ladder weights + in-budget $3 floor + config loading (2026-10-09 config B). Pure, no chain."""
import pytest
from dca_levels import LadderParams, Rung, ladder_budgets, ladder_weights, fit_min_budgets
from config import load_config


def test_accel_weights():
    lp = LadderParams(weight_mode="accel", weight_mult=1.15, weight_mult_step=0.10, weight_mult_cap=2.5)
    w = ladder_weights(5, lp)
    assert w == pytest.approx([1, 1.15, 1.15 * 1.25, 1.15 * 1.25 * 1.35, 1.15 * 1.25 * 1.35 * 1.45])
    w = ladder_weights(30, lp)
    assert w[-1] / w[-2] == pytest.approx(2.5)        # capped ratio


def test_geometric_default_unchanged():
    lp = LadderParams()
    assert lp.weight_mode == "geometric"
    rungs = [Rung(100 - k, 6 + k, "fixed") for k in range(4)]
    b = [r.budget for r in ladder_budgets(rungs, 100.4, lp)]
    tot = 1 + 1.5 + 2.25 + 3.375
    assert b == pytest.approx([100 / tot, 150 / tot, 225 / tot, 337.5 / tot])


def test_fit_min_inside_budget():
    w = [1, 2, 4, 8, 100]
    b = fit_min_budgets(w, 50.0, 3.0)
    assert sum(b) == pytest.approx(50.0)
    assert min(b) >= 3.0 - 1e-9
    assert b[:3] == pytest.approx([3, 3, 3]) and b[3] == pytest.approx(41 * 8 / 108) and b[4] == pytest.approx(41 * 100 / 108)
    assert fit_min_budgets(w, 10.0, 3.0) == pytest.approx([2.0] * 5)   # floor unmeetable: equal split


def test_accel_budgets_sum_and_floor():
    lp = LadderParams(weight_mode="accel", weight_mult=1.15, min_usd=3.0, fee_usd_per_add=0.10)
    rungs = [Rung(100 - k, 6 + k, "fixed") for k in range(14)]
    b = [r.budget for r in ladder_budgets(rungs, 181.4, lp)]
    assert sum(b) == pytest.approx(180.0)
    assert min(b) >= 3.0 - 1e-9
    assert b == sorted(b)


@pytest.mark.parametrize("name", ["live_100", "live_eth", "live_btc"])
def test_live_configs_load_accel(name):
    d = load_config(f"configs/{name}.toml").dca
    assert (d.ladder_max_rungs, d.ladder_weight_mode, d.ladder_weight_mult, d.ladder_weight_mult_step,
            d.ladder_weight_mult_cap) == (14, "accel", 1.15, 0.10, 2.5)
    assert (d.lev_start, d.lev_step, d.lev_cap) == (5.0, 1.0, 20.0)
