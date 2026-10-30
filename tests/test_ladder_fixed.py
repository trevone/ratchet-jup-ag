"""ladder_fixed_usd (2026-10-12, Trevor ladder candidate C): flat collateral per rung. Default off in all live configs."""
import dataclasses

import pytest

from config import load_config
from dca_levels import LadderParams, Rung, ladder_budgets

C = ["dca.ladder_fixed_usd=10", "dca.min_add_usd=10", "dca.lev_start=5", "dca.lev_step=2.5", "dca.lev_cap=20",
     "dca.ladder_max_rungs=10", "dca.ladder_deep=false", "entry.first_collateral_mode=fixed",
     "sizing.mode=fixed_usd", "sizing.initial_margin_usd=10"]


@pytest.mark.parametrize("path", ["tests/fixtures/configs_pre_c/live_100.toml", "tests/fixtures/configs_pre_c/live_eth.toml", "tests/fixtures/configs_pre_c/live_btc.toml"])
def test_live_configs_fixed_ladder_off(path):
    assert load_config(path).dca.ladder_fixed_usd == 0.0


def test_fixed_budget_flat_and_weights_ignored():
    rungs = [Rung(100 - i, 5 + 2.5 * (i + 1), "levels") for i in range(10)]
    for mode in ("accel", "geometric"):
        lp = LadderParams(max_rungs=10, deep=False, weight_mode=mode, weight_mult=1.15, fixed_usd=10.0, min_usd=10.0)
        assert [r.budget for r in ladder_budgets(rungs, 37.0, lp)] == [10.0] * 10
    lp0 = LadderParams(weight_mode="accel", weight_mult=1.15)
    assert len({round(r.budget, 4) for r in ladder_budgets(rungs, 300.0, lp0)}) > 1   # off: unchanged weighting


def test_preset_c_wires_through_strategy():
    cfg = load_config("tests/fixtures/configs_pre_c/live_100.toml", C)
    from strategy import DcaStrategy as Strategy
    s = Strategy(cfg)
    assert s.initial_margin(500.0) == 10.0 and s.plan_kwargs()["ladder"].fixed_usd == 10.0
    assert (cfg.dca.lev_start, cfg.dca.lev_step, cfg.dca.lev_cap, cfg.dca.ladder_max_rungs, cfg.dca.ladder_deep) == (5, 2.5, 20, 10, False)


@pytest.mark.parametrize("path", ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"])
def test_live_configs_run_preset_c_resting_and_pct_guard(path):   # Trevor 2026-10-12 00:27 MT: live
    c = load_config(path)
    d = c.dca
    assert (d.ladder_fixed_usd, d.min_add_usd, d.lev_start, d.lev_step, d.lev_cap, d.ladder_max_rungs, d.ladder_deep) == \
        (10.0, 10.0, 5.0, 2.5, 20.0, 10, False)
    assert (c.entry.first_collateral_mode, c.sizing.mode, c.sizing.initial_margin_usd) == ("pct", "fixed_usd", 10.0)   # 2026-10-29 21:24 MT: first entry pct; [sizing] kept for fixed mode
    r = c.resting
    assert r.adds_enabled and r.partials_enabled and r.last_partial_rule
    assert (c.ledger.min_profit_pct_hwm, c.ledger.min_profit_pct_per_partial) == (0.002, 0.0005)
    assert c.profit_guard.min_profit_pct_trade == 0.003   # Trevor 2026-10-27 08:56 MT: live guard base 0.2% -> 0.3%
