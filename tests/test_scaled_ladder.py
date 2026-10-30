"""Trevor 2026-10-29 21:43 MT: infinite scaled ladder. B = first-entry collateral (stored once per trade);
rung n: L_n = min(lev_start + n*lev_step, lev_cap), N_n = (lev_start + n*lev_step)*B, C_n = N_n / L_n."""
import json

import pytest

from config import load_config
from dca_levels import Levels, _SimPos, plan_add, project_ladder, scaled_rung
from strategy import DcaStrategy

LIVE = ("configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml")
# (collateral, leverage, notional) for rungs 0..10 at lev_start 5, step 2.5, cap 20
TABLE = {
    10.0: [(10, 5, 50), (10, 7.5, 75), (10, 10, 100), (10, 12.5, 125), (10, 15, 150), (10, 17.5, 175), (10, 20, 200),
           (11.25, 20, 225), (12.5, 20, 250), (13.75, 20, 275), (15, 20, 300)],
    11.0: [(11, 5, 55), (11, 7.5, 82.5), (11, 10, 110), (11, 12.5, 137.5), (11, 15, 165), (11, 17.5, 192.5), (11, 20, 220),
           (12.375, 20, 247.5), (13.75, 20, 275), (15.125, 20, 302.5), (16.5, 20, 330)],
    20.0: [(20, 5, 100), (20, 7.5, 150), (20, 10, 200), (20, 12.5, 250), (20, 15, 300), (20, 17.5, 350), (20, 20, 400),
           (22.5, 20, 450), (25, 20, 500), (27.5, 20, 550), (30, 20, 600)],
}


@pytest.mark.parametrize("base", sorted(TABLE))
def test_rung_table(base):
    for n, want in enumerate(TABLE[base]):
        assert scaled_rung(base, n, 5.0, 2.5, 20.0) == pytest.approx(want), n


@pytest.mark.parametrize("path", LIVE)
def test_live_keys(path):
    c = load_config(path)
    d = c.dca
    assert d.ladder_mode == "scaled" and (d.lev_start, d.lev_step, d.lev_cap) == (5.0, 2.5, 20.0)
    assert d.min_add_usd == 10.0 and c.entry.first_collateral_pct == 0.05 and c.entry.first_collateral_min_usd == 10.5
    assert d.ladder_max_rungs == 10   # kept: TP3 taper / partial N scaling only


def _pos(base, n_entries, px=100.0, side=-1, margin=None, size=None):
    size = size if size is not None else base * 5.0
    return _SimPos(side, size, size / px, margin if margin is not None else base, 0.0, px, 5.0, px, n_entries, base)


LV = Levels(supports=(), resistances=(), atr=1.0)


def _kw(path="configs/live_100.toml", **over):
    s = DcaStrategy(load_config(path))
    kw = s.plan_kwargs()
    kw.update(fixed_dist=1.0, liq_buffer_atr=over.pop("liq_buffer_atr", kw["liq_buffer_atr"]), **over)
    return kw


@pytest.mark.parametrize("path", LIVE)
def test_projection_follows_table_and_is_not_capped_by_rung_count(path):
    kw = _kw(path)
    r = project_ladder(_pos(10.0, 1, px=1000.0), None, available_usd=1e6, max_steps=14, **kw)
    assert len(r) == 14 > load_config(path).dca.ladder_max_rungs   # no max rung count
    for n, a in enumerate(r[:10], 1):
        c, l, nn = TABLE[10.0][n]
        assert (a.margin, a.leverage, a.margin * a.leverage) == pytest.approx((c, l, nn))


def test_base_never_recomputed_from_equity():
    kw = _kw()
    a1 = plan_add(_pos(10.5, 3, px=1000.0), None, available_usd=50.0, **kw)
    a2 = plan_add(_pos(10.5, 3, px=1000.0), None, available_usd=5000.0, **kw)
    assert a1.margin == a2.margin == pytest.approx(10.5) and a1.leverage == 12.5


def test_budget_skip_never_shrinks():
    kw = _kw()
    p = _pos(10.0, 8, px=1000.0)                                 # rung 8: $12.50 x20
    assert plan_add(p, None, available_usd=12.59, **kw) is None  # 12.50 + 0.10 fee reserve not covered
    assert plan_add(p, None, available_usd=12.60, **kw).margin == pytest.approx(12.5)


def test_liq_skip_never_shrinks():
    kw = _kw(liq_buffer_atr=500.0)                             # impossible buffer -> skipped, not shrunk
    assert plan_add(_pos(10.0, 2, px=1000.0), None, available_usd=1e4, **kw) is None
    assert plan_add(_pos(10.0, 2, px=1000.0), None, available_usd=1e4, **_kw(liq_buffer_atr=0.0)) is not None


def test_legacy_mode_unchanged():
    s = DcaStrategy(load_config("configs/live_100.toml", ["dca.ladder_mode=legacy"]))
    kw = s.plan_kwargs()
    kw.update(fixed_dist=1.0)
    a = plan_add(_pos(10.0, 8, px=1000.0), None, available_usd=1e4, **kw)
    assert a.margin == pytest.approx(10.0) and a.leverage == 7.5   # flat $10 rung, last_leverage 5 + 2.5


def test_base_persists_across_restart(tmp_path):
    import live.bot as B
    p = tmp_path / "state.json"
    L = B.Local(side=-1, n_entries=4, first_margin=10.5, last_leverage=12.5, first_entry=100.0, last_entry=103.0)
    p.write_text(json.dumps(__import__("dataclasses").asdict(L)))
    L2 = B.load_local(p)
    assert (L2.first_margin, L2.n_entries) == (10.5, 4)
