"""Above-water TP hysteresis (2026-10-08): once a deep-add position's above-water TPs engage, price hovering around
avg - 1 ATR must not flip TP1 back and forth (BTC re-placed TP1 83284.42 <-> 83242 five times in 6.5 h)."""
from types import SimpleNamespace as NS

from config import load_config
from dca_levels import Levels
from strategy import LONG, DcaStrategy

AVG, ATR = 83035.315, 415.0
LV = Levels(supports=(82191.8, 80101.0), resistances=(83448.13, 84432.6), atr=ATR)


def pos(n_entries=2, avg=AVG):
    return NS(side=LONG, avg_entry=avg, size=100.5, qty=100.5 / avg, margin=13.4, n_entries=n_entries,
              first_entry=83162.98, last_entry=82690.67, tp_hits=0, borrow=0.0, last_leverage=9.0, first_margin=10.5,
              opened=None, first_tp_time=None, price_for_net_pnl=lambda x: avg)


def strat(smart=True):
    s = DcaStrategy(load_config("configs/live_btc.toml", [f"tp.smart_tp2={'true' if smart else 'false'}",
                                                         'tp.tp1_mode="ladder"']))   # above-water: ladder mode only
    s.levels_provider = lambda now: LV
    s._levels = LV
    s.last_plan = NS(target_level=83448.13)
    return s


def tp1(s, price, p=None):
    s._last_price = price
    return round(s.tp_targets(p or pos(), price=price)[0].price, 2)


SHALLOW, DEEP = AVG - 0.5 * ATR, AVG - 1.5 * ATR


def test_latched_after_engaging():
    s = strat()
    normal = tp1(s, SHALLOW)
    deep = tp1(s, DEEP)
    assert deep < normal                      # above-water TP1 sits closer to the average
    assert tp1(s, SHALLOW) == deep            # price recovers inside 1 ATR: latched, no flip back


def test_changed_position_is_evaluated_fresh():
    s = strat()
    tp1(s, DEEP)
    p3 = pos(n_entries=3, avg=AVG - 100)
    assert tp1(s, SHALLOW - 100, p3) == tp1(strat(), SHALLOW - 100, p3)


def test_backtests_unaffected_without_smart_tp2():
    s = strat(smart=False)
    normal = tp1(s, SHALLOW)
    assert tp1(s, DEEP) < normal
    assert tp1(s, SHALLOW) == normal
