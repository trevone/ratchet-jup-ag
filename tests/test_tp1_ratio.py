"""TP1 ratio rule (Trevor 2026-10-08 06:51 MT, "be profit aware"): after an add (and, scope "all", on every position)
TP1 = avg + tp1_ratio x (actual smart TP2 - avg); the above-water TP1 (avg + fees + deep buffer) is off in ratio mode;
a momentum-faded TP2 never goes inside that TP1 + tp_min_gap_usd. Pure logic plus the bot's TP check on a fake chain
(conftest.py blocks every chain call)."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from asset_scale import scale_config
from config import load_config
from dca_levels import Levels, SmartTpParams, TpFade, smart_tps, tp_fee_floor
from strategy import LONG, SHORT, DcaStrategy

SOL_LIVE, BTC_LIVE, ETH_LIVE = "configs/live_100.toml", "configs/live_btc.toml", "configs/live_eth.toml"
# pinned so live tuning never changes what is verified here
PIN = ['tp.mode="price_abs"', 'tp.anchor="avg_entry"',
       'tp.levels=[{"dist":0.45,"close_pct":0.5},{"dist":0.75,"close_pct":1.0}]',
       "tp.smart_tp2=true", "tp.tp_res_buffer_atr=0.1", "tp.tp_min_net_usd=0.05", "tp.tp_min_gap_usd=0.05",
       "tp.tp_min_move=0.05", "tp.tp_market_gap=0.02", "tp.tp_check_min=1.0", "tp.tp_near_miss_frac=0.85",
       "tp.tp_fade_atr=0.3", "tp.tp_fade_rsi_drop=8.0", "tp.tp_swing_buffer_atr=0.05", "tp.tp_res_min_touches=0",
       "tp.deep_buffer_usd=0.15", 'tp.tp1_mode="ratio"', "tp.tp1_ratio=0.6", 'tp.tp1_ratio_scope="all"',
       "tp.tp_fade_floor_at_tp1=true", "costs.open_fee=0.0006", "costs.close_fee=0.0006", "live.dry_run=true"]
# SOL 2026-10-08 06:30 MT: long $133.50 avg 114.764 after add #2; 1h resistance 115.76 (ATR 0.87)
AVG, SIZE, ATR = 114.764, 133.5, 0.87
LV = Levels(supports=(112.48, 111.09, 107.38), resistances=(115.76, 116.96, 119.05), atr=ATR)


def cfg(*over, path=SOL_LIVE):
    return load_config(path, [*PIN, *over])


def strat(*over, lv=LV, path=SOL_LIVE):
    s = DcaStrategy(cfg(*over, path=path))
    s.levels_provider = lambda now: lv
    s._levels = lv
    s.last_plan = NS(target_level=115.76)
    return s


def pos(side=LONG, avg=AVG, size=SIZE, n_entries=3, tp_hits=0):
    return NS(side=side, avg_entry=avg, size=size, qty=size / avg, margin=16.39, n_entries=n_entries, first_entry=115.807,
              last_entry=112.79, tp_hits=tp_hits, borrow=0.0, last_leverage=11.0, first_margin=10.5, opened=None,
              first_tp_time=None, price_for_net_pnl=lambda x: avg)


def tps(s, p, price, **k):
    s._last_price = price
    return [round(t.price, 2) for t in s.tp_targets(p, price=price, **k)]


def net(side, avg, chunk, px, of=0.0006, cf=0.0006):
    q = chunk / avg
    return side * q * (px - avg) - chunk * of - q * px * cf


# ------------------------------------------------------------------ config
def test_config_keys_and_validation():
    c = cfg().tp
    assert (c.tp1_mode, c.tp1_ratio, c.tp1_ratio_scope, c.tp_fade_floor_at_tp1) == ("ratio", 0.6, "all", True)
    d = load_config("configs/default.toml").tp
    assert d.tp1_mode == "ladder" and d.tp_fade_floor_at_tp1 is False      # backtests unchanged by default
    for bad in ("tp.tp1_ratio=0.0", "tp.tp1_ratio=1.0", 'tp.tp1_mode="weird"', 'tp.tp1_ratio_scope="x"'):
        with pytest.raises(ValueError):
            cfg(bad)


@pytest.mark.parametrize("path", [SOL_LIVE, BTC_LIVE, ETH_LIVE])
def test_live_configs_use_ratio_rule(path):
    t = load_config(path).tp
    assert t.tp1_mode == "ratio" and 0.0 < t.tp1_ratio < 1.0 and t.tp1_ratio_scope in ("all", "after_add")
    assert t.tp_fade_floor_at_tp1 is True and t.smart_tp2 is True


# ------------------------------------------------------------------ the 60% rule after an add
def test_sol_after_add_ratio_tp1_not_above_water():
    """06:30 MT add: the bot placed above-water TP1 115.05 (avg + fees + 0.15). Ratio mode: 115.21 / 115.51."""
    deep_px = 112.76                                   # > 1 ATR under the average: above-water would engage
    assert tps(strat('tp.tp1_mode="ladder"'), pos(), deep_px)[0] == 115.05   # old behaviour, for reference
    got = tps(strat(), pos(), deep_px)
    assert got == [round(AVG + 0.6 * (115.51 - AVG), 2), 115.51] == [115.21, 115.51]


def test_ratio_uses_smart_tp2_under_resistance():
    lv = Levels((112.48,), (115.30,), ATR)            # resistance cap 115.30 - 0.087 = 115.213 < avg + 0.75
    s = strat(lv=lv)
    got = tps(s, pos(), 112.76)
    cap = 115.30 - 0.1 * ATR
    assert got == [round(AVG + 0.6 * (cap - AVG), 2), round(cap, 2)]
    assert any("0.6 x TP2 distance" in r for r in s.last_tp.reasons)


def test_ratio_honours_res_min_touches():
    lv = Levels((112.48,), (115.30, 115.40), ATR, res_touches=(1, 3))      # 115.30 has 1 touch -> ignored with min 2
    got = tps(strat("tp.tp_res_min_touches=2", lv=lv), pos(), 112.76)
    cap = 115.40 - 0.1 * ATR
    assert got == [round(AVG + 0.6 * (cap - AVG), 2), round(cap, 2)]


def test_btc_scaled_ratio_matches_live_example():
    """BTC long $100.50 avg 83035.32, TP2 83406.95 (under 3-touch R 83448.13 - 0.1 ATR) -> TP1 ~83258."""
    c = scale_config(load_config(BTC_LIVE, ["live.dry_run=true"]), 82300.0, None)
    s = DcaStrategy(c)
    lv = Levels((82191.8,), (83448.13, 84355.2), 411.797, res_touches=(3, 3))
    s.levels_provider = lambda now: lv
    p = pos(avg=83035.32, size=100.5, n_entries=2)
    got = [round(t.price, 2) for t in s.tp_targets(p, price=82300.0)]
    assert got[1] == pytest.approx(83448.13 - 41.1797, abs=0.01)
    assert got[0] == pytest.approx(83035.32 + 0.6 * (got[1] - 83035.32), abs=0.01)
    assert got[0] == pytest.approx(83258.3, abs=0.5)


def test_short_mirror():
    lv = Levels((114.20,), (), ATR)                  # support cap 114.20 + 0.087
    p = pos(side=SHORT, avg=115.0, size=100.5, n_entries=2)
    got = tps(strat(lv=lv), p, 116.5)
    cap = 114.20 + 0.1 * ATR
    assert got == [round(115.0 - 0.6 * (115.0 - cap), 2), round(cap, 2)]


def test_scope_after_add_keeps_ladder_tp1_on_first_entry():
    s = strat('tp.tp1_ratio_scope="after_add"', 'tp.levels=[{"dist":0.30,"close_pct":0.5},{"dist":0.75,"close_pct":1.0}]')
    first = tps(s, pos(n_entries=1, size=73.5), 114.5)
    assert first[0] == round(AVG + 0.30, 2)            # ladder TP1 untouched
    added = tps(s, pos(n_entries=2, size=100.5), 114.5)
    assert added[0] == round(AVG + 0.6 * 0.75, 2)      # ratio after an add


def test_scope_all_applies_to_first_entry_too():
    s = strat('tp.levels=[{"dist":0.30,"close_pct":0.5},{"dist":0.75,"close_pct":1.0}]')
    assert tps(s, pos(n_entries=1, size=73.5), 114.5)[0] == round(AVG + 0.45, 2)


def test_fee_floor_gap_and_market_rules_kept():
    # tiny TP2 distance (resistance just above avg): ratio TP1 would be under its fee floor -> raised to it,
    # TP2 kept >= tp_min_gap_usd beyond TP1
    lv = Levels((112.48,), (AVG + 0.25,), ATR)
    s = strat(lv=lv)
    got = s.tp_targets(pos(), price=112.76)
    f1 = tp_fee_floor(LONG, AVG, SIZE * 0.5, 0.05, 0.0006, 0.0006)
    assert got[0].price >= f1 - 1e-9 and net(LONG, AVG, SIZE * 0.5, got[0].price) >= 0.05 - 1e-6
    assert got[1].price - got[0].price >= 0.05 - 1e-9
    # never at/through market: price already above the ratio TP1
    got = tps(strat(), pos(), 115.30)
    assert got[0] >= 115.32 and got[1] - got[0] >= 0.05 - 1e-9


def test_non_smart_path_ratio():
    s = strat("tp.smart_tp2=false")
    assert tps(s, pos(), 114.0) == [round(AVG + 0.6 * 0.75, 2), round(AVG + 0.75, 2)]


# ------------------------------------------------------------------ fade floor
P = SmartTpParams()


def test_fade_floor_keeps_tp2_beyond_ratio_tp1():
    """Faded TP2 used to be pulled to max(fee floor, best - 0.05 ATR), dragging TP1 with it (SOL 05:13: TP2 115.76 next
    to TP1 115.75). Now it stops at TP1 (pre-fade ratio level) + min_gap, and TP1 stays put."""
    avg, size = 115.46, 100.5
    base = [avg + 0.45, avg + 0.75]
    p = SmartTpParams(near_miss_frac=0.5)              # loosened so the swing target lands inside TP1
    fade = TpFade(peak=115.86, price=115.50)            # reached 53% of 0.75, pulled back 0.36 > 0.3 ATR
    old = smart_tps(LONG, avg, size, base, [0.5, 1.0], 0, None, p, atr=ATR, fade=fade)
    assert old.faded and old.prices[1] == pytest.approx(115.86 - 0.05 * ATR)
    assert old.prices[0] == pytest.approx(old.prices[1] - 0.05)              # TP1 dragged in by the fade
    new = smart_tps(LONG, avg, size, base, [0.5, 1.0], 0, None, p, atr=ATR, fade=fade, tp1_ratio=0.6,
                    fade_floor_tp1=True)
    tp1 = avg + 0.6 * 0.75
    assert new.faded and new.prices[0] == pytest.approx(tp1)
    assert new.prices[1] == pytest.approx(tp1 + 0.05)
    assert any("floor" in r for r in new.reasons)


def test_fade_floor_not_binding_when_swing_target_is_further():
    avg, size = 115.46, 100.5
    fade = TpFade(peak=116.15, price=115.80)            # 92% reach, pulled back 0.35
    r = smart_tps(LONG, avg, size, [avg + 0.45, avg + 0.75], [0.5, 1.0], 0, None, P, atr=ATR, fade=fade,
                  tp1_ratio=0.6, fade_floor_tp1=True)
    assert r.faded and r.prices[1] == pytest.approx(116.15 - 0.05 * ATR)
    assert r.prices[0] == pytest.approx(avg + 0.45)


def test_fade_floor_short_and_after_tp1_fill():
    avg = 116.20
    fade = TpFade(peak=115.80, price=116.10)
    p = SmartTpParams(near_miss_frac=0.5)
    r = smart_tps(SHORT, avg, 36.75, [avg - 0.45, avg - 0.75], [0.5, 1.0], 1, None, p, atr=ATR, fade=fade,
                  tp1_ratio=0.6, fade_floor_tp1=True)
    assert r.faded and r.prices[1] == pytest.approx(avg - 0.45 - 0.05)    # runner floor = TP1 level - gap (short)


def test_strategy_fade_floor_via_config():
    lv = Levels((112.48,), (116.96,), ATR)
    s = strat("tp.tp_near_miss_frac=0.5", lv=lv)
    got = tps(s, pos(avg=115.46, size=100.5, n_entries=2), 115.50, fade=TpFade(115.86, 115.50))
    assert got == [round(115.46 + 0.45, 2), round(115.46 + 0.45 + 0.05, 2)]


def test_sol_0513_squeeze_was_resistance_cap_and_optional_min_dist():
    """05:13 MT case (avg 115.46, $100.50): the new 1h resistance 115.76 sat only 0.30 above avg, so TP2 was capped to
    115.676 and both TPs were pushed to the fee floor (115.71 / 115.76). That is the resistance cap, not the fade, so
    the fade floor alone does not change it. tp_res_min_dist_frac (off by default) skips such near levels."""
    p = pos(avg=115.46, size=100.5, n_entries=2)
    fade = TpFade(115.86, 113.61)
    assert tps(strat(), p, 113.61, fade=fade, prev_faded=True) == [115.71, 115.76]
    on = "tp.tp_res_min_dist_frac=0.6"
    assert tps(strat(on), p, 113.61) == [115.91, 116.21]     # unfaded: 60% rule on avg + 0.75 (116.96 R is further)
    got = tps(strat(on), p, 113.61, fade=fade, prev_faded=True)
    assert got == [115.91, 115.96]                            # faded: swing target 115.82 floored at TP1 + 0.05
    with pytest.raises(ValueError):
        cfg("tp.tp_res_min_dist_frac=1.0")


# ------------------------------------------------------------------ bot: restart adopts and re-places TP1 only
@pytest.fixture
def bot(monkeypatch, tmp_path):
    import live.bot as B
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    b = B.Bot(cfg())
    b.kill = tmp_path / "KILL"
    b.calls = []

    def fake_cmd(name, args):
        b.calls.append((name, args))
        return {"ok": True}
    b.cmd = fake_cmd
    b.strategy.levels_provider = lambda now: LV
    return b


def chain_pos():
    from live.bot import ChainPosition
    return ChainPosition(side=LONG, opened=pd.Timestamp.now(tz="UTC"), margin=16.39, size=SIZE, qty=SIZE / AVG,
                         borrow=0.0, n_entries=3, last_leverage=11.0, first_margin=10.5, first_entry=115.807,
                         last_entry=112.79, tp_hits=0, first_tp_time=None)


def test_restart_adopts_and_replaces_above_water_tp1(bot):
    """Fresh process (no tp_px record) with the 06:30 triggers 115.05 / 115.51 on-chain: adopt both, then re-place TP1
    only (cancel old, place 115.21 for $66.75); TP2 untouched; no halt."""
    st = {"openRequests": [{"pubkey": "R1", "triggerPrice": 115.05, "executed": False},
                           {"pubkey": "R2", "triggerPrice": 115.51, "executed": False}]}
    bot.local.side, bot.local.last_size, bot.local.n_entries = LONG, SIZE, 3
    bot.check_tps(chain_pos(), st, 112.70)
    assert bot.calls == [("cancel", {"request": "R1"}),
                         ("decrease", {"side": "long", "triggerPriceUsd": 115.21, "triggerAbove": True, "sizeUsd": 66.75})]
    assert bot.local.tp_px == [115.21, 115.51] and not bot.kill.exists()
    bot.calls.clear()
    bot._tp_checked = None
    st["openRequests"][0] = {"pubkey": "R3", "triggerPrice": 115.21, "executed": False}
    bot.check_tps(chain_pos(), st, 112.70)               # next check: stable, nothing re-placed
    assert bot.calls == []
