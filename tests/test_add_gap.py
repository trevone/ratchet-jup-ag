"""Trevor 2026-10-25: minimum add distance 0.3 -> 0.6 analyst ATR on all three bots (upper bound 1.2, front 0.1 unchanged)."""
import pytest

from config import load_config
from dca_levels import Levels, next_add_level
from strategy import LONG, SHORT


@pytest.mark.parametrize("path", ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"])
def test_live_add_gap_bounds(path):
    d = load_config(path).dca
    assert (d.min_gap_atr, d.max_gap_atr, d.add_frontrun_atr) == (0.95, 1.2, 0.1)   # 2026-10-27: 0.6 -> 0.95 (test_place_gap.py)


def test_level_inside_0p6_1p2_else_unchanged_fixed_fallback():
    lv = Levels(supports=(99.5, 99.0), resistances=(100.5, 101.0), atr=1.0)
    # short from 100: 100.5 is 0.5 ATR (now too close), 101.0 is 1.0 ATR -> picked
    assert next_add_level(SHORT, 100.0, lv, 0.6, 1.2, 0.85) == (101.0, "levels")
    assert next_add_level(LONG, 100.0, lv, 0.6, 1.2, 0.85) == (99.0, "levels")
    # nothing within 0.6..1.2 ATR -> the existing fixed trigger_dist fallback
    lv2 = Levels(supports=(99.5,), resistances=(100.5,), atr=1.0)
    assert next_add_level(SHORT, 100.0, lv2, 0.6, 1.2, 0.85) == (100.85, "fixed")
    assert next_add_level(SHORT, 100.0, lv2, 0.3, 1.2, 0.85) == (100.5, "levels")   # old 0.3 bound took it
