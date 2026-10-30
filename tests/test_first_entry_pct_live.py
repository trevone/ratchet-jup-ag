"""Trevor 2026-10-29 21:24 MT: first entry = 5% (21:43 MT; was 10% at 21:24) of the bot's free collateral (floor $10.50, no cap) on all three live bots."""
import pytest

from config import load_config
from dca_levels import Levels
from strategy import DcaStrategy

LIVE = ("configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml")


@pytest.mark.parametrize("path", LIVE)
def test_live_pct_keys(path):
    e = load_config(path).entry
    assert e.first_collateral_mode == "pct" and e.first_collateral_pct == 0.05
    assert e.first_collateral_min_usd == 10.50 and e.first_collateral_max_usd is None


@pytest.mark.parametrize("path", LIVE)
@pytest.mark.parametrize("free,want", [(111.55, 10.50), (117.11, 10.50), (119.58, 10.50), (300.0, 15.0), (400.0, 20.0),
                                       (80.0, 10.50), (105.0, 10.50), (10.30, 10.20)])
def test_live_pct_sizing_and_floor(path, free, want):
    s = DcaStrategy(load_config(path))
    m = s.initial_margin(free)
    assert m == pytest.approx(want, abs=0.006)
    if free > 20:
        assert m >= s.cfg.live.min_collateral_usd          # Jupiter $10 open minimum met


@pytest.mark.parametrize("path", LIVE)
def test_live_pct_entry_intent_uses_pct(path):
    import pandas as pd
    from strategy import Bar
    s = DcaStrategy(load_config(path, ["sides=both"]))

    class A:
        cash, position = 117.11, None

        def equity(self, px):
            return 117.11
    b = Bar(pd.Timestamp("2026-10-30T03:00Z"), 90.0, 90.0, 90.0, 90.0, 50.0, False, 100.0, 95.0)
    s.begin_bar(b, A())
    it = s.entry_intents(b, A())[0]
    assert it.margin == pytest.approx(10.50, abs=0.006)


@pytest.mark.parametrize("path", LIVE)
def test_live_ladder_after_pct_open_still_sized(path):
    s = DcaStrategy(load_config(path))
    free = 117.11
    m = s.initial_margin(free)
    from dca_levels import _SimPos, project_ladder
    px = 100.0
    lev = s.cfg.dca.lev_start
    pos = _SimPos(-1, m * lev, m * lev / px, m, 0.0, px, lev, px, 1, m)
    lv = Levels(supports=(98.0, 96.0), resistances=(102.0, 104.0, 106.0), atr=1.0)
    rungs = project_ladder(pos, lv, available_usd=free - m, **s.plan_kwargs())
    assert rungs, "ladder must still plan adds after a pct first entry"
    assert all(r.margin >= 10.0 - 1e-9 for r in rungs[:-1] or rungs)   # each add >= Jupiter $10 min
