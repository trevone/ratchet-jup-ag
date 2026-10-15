"""Smart TP2 / TP1 sanity and idle RSI loosening (Trevor 2026-10-07 20:14 MT). Pure logic plus the bot's TP
check with a fake chain; conftest.py still blocks every chain call, and STATE / KILL are redirected to tmp."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from config import load_config
from dca_levels import Levels, TpFade, smart_tps, tp_fee_floor
from strategy import LONG, SHORT, Action, Bar, DcaStrategy

ATR = 0.761
LV = Levels(supports=(115.32, 115.69, 112.99), resistances=(117.18, 119.05, 119.88), atr=ATR)
LIVE = "configs/live_100.toml"
# Every tunable the expected prices / thresholds below are derived from, pinned so that live tuning (e.g. Trevor's TP1 =
# ~60% of TP2 rule, which moved live TP1 0.50 -> 0.45 on 2026-10-07 21:21 MT) never changes what these tests verify. The
# live file only supplies the structure (levels-mode DCA, sizing, ...); test_live_keys checks the live keys separately.
PINNED = [
    'tp.mode="price_abs"', 'tp.anchor="avg_entry"',
    'tp.levels=[{"dist":0.50,"close_pct":0.5},{"dist":0.75,"close_pct":1.0}]',
    "tp.smart_tp2=true", "tp.tp_res_buffer_atr=0.1", "tp.tp_min_net_usd=0.05", "tp.tp_min_gap_usd=0.05",
    "tp.tp_min_move=0.05", "tp.tp_market_gap=0.02", "tp.tp_near_miss_frac=0.85",
    "tp.tp_fade_atr=0.3", "tp.tp_fade_rsi_drop=8.0", "tp.tp_swing_buffer_atr=0.05", "tp.tp_res_min_touches=0",
    'tp.tp1_mode="ladder"', "tp.tp_fade_floor_at_tp1=false",   # ladder TP1 rules verified here; ratio: test_tp1_ratio.py
    "costs.open_fee=0.0006", "costs.close_fee=0.0006",
    "signal.oversold=45.0", "signal.overbought=55.0", "signal.idle_enabled=true", "signal.idle_after_min=60.0",
    "signal.idle_step_min=30.0", "signal.idle_long_rsi_steps=[48.0, 50.0]", "signal.idle_short_rsi_steps=[52.0, 50.0]",
    "signal.idle_max_long_rsi=50.0", "signal.idle_min_short_rsi=50.0",
    "sides=both",   # live files may carry a one-sided bias; these tests need both sides
    "live.dry_run=true",   # the bot fixture never sends anyway (cmd is faked, conftest blocks the CLI)
]


def pinned(*over):
    return load_config(LIVE, [*PINNED, *over])


CFG = pinned()


def pos(side=LONG, avg=115.90, size=73.50, tp_hits=0, n_entries=1):
    return NS(side=side, avg_entry=avg, size=size, qty=size / avg, margin=10.5, n_entries=n_entries, first_entry=avg,
              last_entry=avg, tp_hits=tp_hits, borrow=0.0, last_leverage=7.0, first_margin=10.5, opened=None,
              first_tp_time=None, price_for_net_pnl=lambda x: avg)


def strat(*over):
    return DcaStrategy(pinned(*over))


def net(side, avg, chunk, px, of=0.0006, cf=0.0006):
    q = chunk / avg
    return side * q * (px - avg) - chunk * of - q * px * cf


# ------------------------------------------------------------------ config
def test_pinned_fixture_values():
    """The values every expected number in this file is computed from (independent of live tuning)."""
    t, s = CFG.tp, CFG.signal
    assert t.smart_tp2 and t.tp_res_buffer_atr == 0.1 and t.tp_min_net_usd == 0.05 and t.tp_min_move == 0.05
    assert t.tp_near_miss_frac == 0.85 and t.tp_fade_atr == 0.3 and t.tp_fade_rsi_drop == 8.0
    assert t.tp_min_gap_usd == 0.05 and t.tp_market_gap == 0.02 and t.tp_swing_buffer_atr == 0.05
    assert [lv.dist for lv in t.levels] == [0.50, 0.75] and [lv.close_pct for lv in t.levels] == [0.5, 1.0]
    assert CFG.costs.open_fee == CFG.costs.close_fee == 0.0006
    assert (s.oversold, s.overbought) == (45.0, 55.0)
    assert s.idle_enabled and s.idle_after_min == 60 and s.idle_step_min == 30
    assert s.idle_long_rsi_steps == (48.0, 50.0) and s.idle_short_rsi_steps == (52.0, 50.0)
    assert CFG.live.dry_run is True


def test_live_keys():
    """The LIVE SOL config has every smart-TP / idle key with a sane type and range; exact values are live tuning and are
    deliberately not asserted here."""
    live = load_config(LIVE)
    t, s = live.tp, live.signal
    assert t.smart_tp2 is True and t.mode == "price_abs"
    lv = t.levels
    assert len(lv) == 2 and all(isinstance(x.dist, float) and isinstance(x.close_pct, float) for x in lv)
    assert 0.0 < lv[0].dist < lv[1].dist and 0.0 < lv[0].close_pct < lv[1].close_pct == 1.0
    for k in ("tp_res_buffer_atr", "tp_min_net_usd", "tp_min_gap_usd", "tp_min_move", "tp_market_gap", "tp_fade_atr",
              "tp_swing_buffer_atr"):
        v = getattr(t, k)
        assert isinstance(v, float) and v >= 0.0, k
    assert t.tp_min_gap_usd < lv[1].dist
    assert 0.0 < t.tp_near_miss_frac <= 1.0
    assert t.tp_fade_rsi_drop is None or (isinstance(t.tp_fade_rsi_drop, float) and t.tp_fade_rsi_drop > 0.0)
    assert isinstance(t.tp_res_min_touches, int) and t.tp_res_min_touches >= 0
    assert isinstance(s.idle_enabled, bool) and s.idle_after_min > 0.0 and s.idle_step_min > 0.0
    assert 0.0 < s.oversold < s.overbought < 100.0
    longs, shorts = s.idle_long_rsi_steps, s.idle_short_rsi_steps
    assert isinstance(longs, tuple) and isinstance(shorts, tuple) and len(longs) >= 1 and len(shorts) >= 1
    assert all(isinstance(x, float) for x in (*longs, *shorts))
    assert list(longs) == sorted(longs) and longs[0] >= s.oversold and max(longs) <= s.idle_max_long_rsi   # loosen only
    assert list(shorts) == sorted(shorts, reverse=True) and shorts[0] <= s.overbought and min(shorts) >= s.idle_min_short_rsi
    assert DcaStrategy(load_config("configs/default.toml")).cfg.tp.smart_tp2 is False     # backtests unchanged


# ------------------------------------------------------------------ TP2 under resistance
def test_tp2_not_clamped_when_resistance_far():
    s = strat()
    t = s.tp_targets(pos(), price=115.95, levels=LV)
    assert [round(x.price, 3) for x in t] == [116.40, 116.65]            # 117.18 - 0.076 = 117.104 > 116.65


def test_tp2_clamped_under_resistance():
    s = strat("tp.levels=[{\"dist\":0.5,\"close_pct\":0.5},{\"dist\":0.9,\"close_pct\":1.0}]")
    lv = Levels((115.32,), (116.70, 117.18), ATR)
    t = s.tp_targets(pos(), price=115.95, levels=lv)
    assert t[1].price == pytest.approx(116.70 - 0.1 * ATR)               # min(116.80, 116.624)
    assert t[0].price == pytest.approx(116.40) and t[0].price < t[1].price
    assert any("under resistance 116.70" in r for r in s.last_tp.reasons)
    # the 2026-10-07 miss: TP2 116.80 (old +0.90) vs resistance 117.18 -> min() keeps 116.80; with 0.05 ATR buffer too
    s2 = strat("tp.levels=[{\"dist\":0.6,\"close_pct\":0.5},{\"dist\":1.5,\"close_pct\":1.0}]", "tp.tp_res_buffer_atr=0.05")
    assert s2.tp_targets(pos(avg=115.897), price=116.0, levels=LV)[1].price == pytest.approx(117.18 - 0.05 * ATR)


def test_short_mirror_clamped_above_support():
    s = strat("tp.levels=[{\"dist\":0.5,\"close_pct\":0.5},{\"dist\":0.9,\"close_pct\":1.0}]")
    t = s.tp_targets(pos(side=SHORT, avg=116.20), price=116.10, levels=LV)   # nearest support below 116.20 = 115.69
    assert t[1].price == pytest.approx(115.69 + 0.1 * ATR)
    assert t[0].price == pytest.approx(t[1].price + 0.05)                  # TP1 kept $0.05 inside TP2 (above, shorts)


# ------------------------------------------------------------------ fee floor
def test_fee_floor_math():
    for side in (LONG, SHORT):
        f = tp_fee_floor(side, 115.9, 36.75, 0.05, 0.0006, 0.0006)
        assert net(side, 115.9, 36.75, f) == pytest.approx(0.05, abs=1e-9)


def test_fee_floor_raises_tp2_and_tp1_kept_inside():
    s = strat("tp.levels=[{\"dist\":0.05,\"close_pct\":0.5},{\"dist\":0.10,\"close_pct\":1.0}]")
    t = s.tp_targets(pos(size=21.0), price=115.92, levels=LV)        # tiny chunks: fees dominate
    f1, f2 = s.last_tp.floors
    assert t[0].price == pytest.approx(f1) and net(LONG, 115.9, 10.5, t[0].price) == pytest.approx(0.05)
    assert t[1].price == pytest.approx(f1 + 0.05) and t[1].price >= f2 - 1e-9   # same-size halves: TP2 = TP1 + gap
    assert t[0].price > 115.95 and t[1].price > 116.0                    # raised from 115.95 / 116.00
    # resistance right above avg would put TP2 under the floor -> floor wins, TP1 still inside and above its floor
    s2 = strat()
    t = s2.tp_targets(pos(), price=115.92, levels=Levels((115.3,), (115.95,), ATR))
    f = tp_fee_floor(1, 115.9, 36.75, 0.05, 6e-4, 6e-4)
    assert t[1].price == pytest.approx(f + 0.05) and t[0].price == pytest.approx(f)
    assert any("fee floor" in r for r in s2.last_tp.reasons)


def test_after_tp1_fill_tp2_floor_uses_whole_remainder():
    s = strat()
    s.tp_targets(pos(size=36.75, tp_hits=1), price=116.0, levels=LV)
    assert s.last_tp.floors[0] is None
    assert s.last_tp.floors[1] == pytest.approx(tp_fee_floor(1, 115.9, 36.75, 0.05, 6e-4, 6e-4))


# ------------------------------------------------------------------ never past market
@pytest.mark.parametrize("side,market", [(LONG, 116.45), (LONG, 116.70), (SHORT, 115.35), (SHORT, 114.90)])
def test_no_trigger_past_market(side, market):
    s = strat()
    avg = 115.90
    t = s.tp_targets(pos(side=side, avg=avg), price=market, levels=Levels((), (), ATR))
    for x in t:
        assert side * (x.price - market) >= 0.02 - 1e-9
    assert side * (t[1].price - t[0].price) >= 0.05 - 1e-9
    assert any(s.last_tp.past_market)


# ------------------------------------------------------------------ momentum fade
def test_near_miss_pullback_pulls_tp2_in():
    s = strat()
    # TP2 116.65 (dist 0.75); 85% = 116.5375. Best 116.60 then back to 116.30 (0.30 > 0.3 ATR = 0.228)
    t = s.tp_targets(pos(tp_hits=1, size=36.75), price=116.30, levels=LV, fade=TpFade(116.60, 116.30))
    assert s.last_tp.faded
    assert t[1].price == pytest.approx(116.60 - 0.05 * ATR)
    assert any("momentum fade" in r and "pulled back" in r for r in s.last_tp.reasons)


def test_near_miss_rsi_rollover_and_fee_floor_bound():
    s = strat()
    r = smart_tps(LONG, 115.90, 36.75, [116.40, 116.65], [0.5, 1.0], 1, LV, s.smart_tp_params(),
                  fade=TpFade(116.56, 116.50, rsi_ext=68.0, rsi_now=59.0))
    assert r.faded and r.prices[1] == pytest.approx(116.56 - 0.05 * ATR)
    # pull-in never below the fee floor: tiny reach via a low near_miss_frac
    p = s.smart_tp_params()
    from dataclasses import replace
    r = smart_tps(LONG, 115.90, 36.75, [116.40, 116.65], [0.5, 1.0], 1, LV, replace(p, near_miss_frac=0.2),
                  fade=TpFade(116.10, 115.80))
    assert r.faded and r.prices[1] == pytest.approx(r.floors[1])


def test_no_fade_without_near_miss_or_fallback():
    s = strat()
    p = s.smart_tp_params()
    r = smart_tps(LONG, 115.90, 36.75, [116.40, 116.65], [0.5, 1.0], 1, LV, p, fade=TpFade(116.50, 116.00))  # 80% < 85%
    assert not r.faded and r.prices[1] == pytest.approx(116.65)
    r = smart_tps(LONG, 115.90, 36.75, [116.40, 116.65], [0.5, 1.0], 1, LV, p, fade=TpFade(116.60, 116.45, 60, 57))
    assert not r.faded                                                   # small pullback, RSI only -3
    r = smart_tps(LONG, 115.90, 36.75, [116.40, 116.65], [0.5, 1.0], 1, LV, p, fade=TpFade(116.60, 116.45), prev_faded=True)
    assert r.faded and r.prices[1] == pytest.approx(116.562)             # stays faded once armed; never pushed out


def test_short_fade_mirror():
    s = strat()
    r = smart_tps(SHORT, 116.20, 36.75, [115.70, 115.45], [0.5, 1.0], 1, Levels((), (), ATR), s.smart_tp_params(),
                  fade=TpFade(115.55, 115.85))
    assert r.faded and r.prices[1] == pytest.approx(115.55 + 0.05 * ATR)


# ------------------------------------------------------------------ bot periodic check (fake chain)
@pytest.fixture
def bot(monkeypatch, tmp_path):
    import live.bot as B
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    b = B.Bot(CFG)
    b.kill = tmp_path / "KILL"
    b.calls = []

    def fake_cmd(name, args):
        b.calls.append((name, args))
        return {"ok": True}
    b.cmd = fake_cmd
    b.strategy.levels_provider = lambda now: LV
    return b


def chain_pos(**k):
    from live.bot import ChainPosition
    d = dict(side=LONG, opened=pd.Timestamp.now(tz="UTC"), margin=10.5, size=73.5, borrow=0.0,
             n_entries=1, last_leverage=7.0, first_margin=10.5, first_entry=115.9, last_entry=115.9, tp_hits=0,
             first_tp_time=None)
    d.update(k)
    d["qty"] = d["size"] / 115.9
    return ChainPosition(**d)


def reqs(*px):
    return {"openRequests": [{"pubkey": f"R{p}", "triggerPrice": p, "executed": False} for p in px]}


def test_sync_places_smart_ladder_never_past_market(bot):
    bot.chain = lambda: {"openRequests": [], "oracle": {"price": 115.95}}
    bot.sync_tp_ladder(chain_pos())
    dec = [a for n, a in bot.calls if n == "decrease"]
    assert [a["triggerPriceUsd"] for a in dec] == [116.40, 116.65]
    assert dec[0]["sizeUsd"] == 36.75 and dec[1]["entire"] is True
    assert bot.local.tp_px == [116.40, 116.65] and bot.local.tp_peak == 115.95 and not bot.kill.exists()


# ------------------------------------------------------------------ cancel already gone (3012) / stale oracle (6003)
# 2026-10-08 05:12 MT BTC halt: the escalated re-sign of a TP2 cancel landed first, the original then failed with Custom 3012
# (AccountNotInitialized: request already closed) and the bot halted with TP2 gone. Now: re-read the chain and proceed.
ERR_3012 = {"InstructionError": [3, {"Custom": 3012}]}
ERR_6003 = {"InstructionError": [2, {"Custom": 6003}]}


def chain_st(size, *px):
    long = {"sizeUsd": size, "collateralUsd": 10.5, "price": 115.9} if size else {}
    return {"openRequests": reqs(*px)["openRequests"], "oracle": {"price": 116.0},
            "positions": {"long": long, "short": {}}}


def script_cmd(bot, *cancel_results):
    """Cancels return the scripted results in order (then ok); every other command succeeds."""
    queue = list(cancel_results)

    def fake_cmd(name, args):
        bot.calls.append((name, args))
        if name == "cancel" and queue:
            return queue.pop(0)
        return {"ok": True}
    bot.cmd = fake_cmd


def test_cancel_all_treats_gone_request_as_cancelled(bot):
    script_cmd(bot, {"ok": False, "err": ERR_3012}, {"ok": True})
    bot.chain = lambda: chain_st(73.5, 116.65)                           # R116.40 gone, R116.65 cancelled next
    assert bot.cancel_all(chain_st(73.5, 116.40, 116.65)) is True
    script_cmd(bot, {"ok": False, "err": ERR_3012})
    bot.chain = lambda: chain_st(73.5, 116.40)                           # still there -> failure
    assert bot.cancel_all(chain_st(73.5, 116.40)) is False


def test_local_state_compat(tmp_path):
    from live.bot import load_local
    f = tmp_path / "s.json"
    f.write_text('{"side": 0, "n_entries": 0, "future_key": 1}')
    L = load_local(f)
    assert L.flat_since is None and L.tp_px == [] and L.tp_faded is False


# ------------------------------------------------------------------ idle loosening
T0 = pd.Timestamp("2026-10-08 02:00:00+00:00")


def bar_at(minutes_after_flat, rsi, trend_up=True):
    t = T0 + pd.Timedelta(minutes=minutes_after_flat) - pd.Timedelta("15m")   # bar closes at T0 + minutes
    return Bar(t, 116.0, 116.1, 115.9, 116.0, rsi, trend_up)


def flat_account(cash=100.0):
    return NS(cash=cash, position=None, equity=lambda px: cash)


def go(s, bar, acct):
    s.begin_bar(bar, acct)
    return s.entry_intents(bar, acct)


@pytest.mark.parametrize("mins,long_th,short_th", [(0, 45, 55), (45, 45, 55), (60, 48, 52), (89, 48, 52),
                                                    (90, 50, 50), (240, 50, 50)])
def test_idle_step_down(mins, long_th, short_th):
    s = strat()
    s._flat_since = T0
    lo, hi, desc = s.entry_thresholds(bar_at(mins, 50))
    assert (lo, hi) == (long_th, short_th)
    assert f"long RSI < {long_th:g}" in desc and f"short RSI > {short_th:g}" in desc


def test_idle_caps_and_off():
    s = strat("signal.idle_long_rsi_steps=[48, 53]", "signal.idle_short_rsi_steps=[52, 47]")
    s._flat_since = T0
    assert s.entry_thresholds(bar_at(120, 50))[:2] == (50.0, 50.0)       # capped at idle_max_long / idle_min_short
    s = strat("signal.idle_enabled=false")
    s._flat_since = T0
    assert s.entry_thresholds(bar_at(240, 50))[:2] == (45.0, 55.0)


def test_idle_entries_respect_trend_and_reset_after_entry():
    s = strat()
    s._flat_since = T0
    assert go(s, bar_at(30, 47.0), flat_account()) == []        # not idle yet: 47 >= 45
    its = go(s, bar_at(60, 47.0), flat_account())               # step 1: 47 < 48 -> long
    assert len(its) == 1 and its[0].action is Action.OPEN and its[0].side == LONG
    assert go(s, bar_at(60, 51.0, trend_up=False), flat_account()) == []   # short still needs > 52
    its = go(s, bar_at(90, 51.0, trend_up=False), flat_account())          # step 2: > 50, downtrend -> short
    assert len(its) == 1 and its[0].side == SHORT
    assert go(s, bar_at(90, 49.0, trend_up=False), flat_account()) == []   # trend filter: no long in downtrend
    # an entry resets the idle clock: open position clears _flat_since, next flat bar starts a new clock
    acct = NS(cash=100.0, position=pos(), equity=lambda px: 100.0)
    go(s, bar_at(105, 47.0), acct)
    assert s._flat_since is None
    go(s, bar_at(120, 49.0), flat_account())
    assert s.entry_thresholds(bar_at(120, 49.0))[:2] == (45.0, 55.0)
    assert s.entry_thresholds(bar_at(120 + 60, 49.0))[:2] == (48.0, 52.0)


def test_cancel_request_outcomes(bot, monkeypatch):
    # (was covered through the removed periodic check_tps, 2026-10-14) 3012 gone + size unchanged -> ok; gone + size
    # changed -> filled; still on-chain -> failed; StaleOracle 6003 retried 2x with 5 s waits
    import live.bot as B
    sleeps = []
    monkeypatch.setattr(B.time, "sleep", lambda s: sleeps.append(s))
    script_cmd(bot, {"ok": False, "sig": "x", "err": ERR_3012})
    bot.chain = lambda: chain_st(36.75)
    assert bot.cancel_request("R116.65", 36.75) == "ok"
    script_cmd(bot, {"ok": False, "sig": "x", "err": ERR_3012})
    bot.chain = lambda: chain_st(0.0)
    assert bot.cancel_request("R116.65", 36.75) == "filled"
    script_cmd(bot, {"ok": False, "sig": "x", "err": ERR_3012})
    bot.chain = lambda: chain_st(36.75, 116.65)
    assert bot.cancel_request("R116.65", 36.75) == "failed"
    bot.calls.clear()
    script_cmd(bot, {"ok": False, "err": ERR_6003}, {"ok": False, "err": ERR_6003})
    assert bot.cancel_request("R116.65", 36.75) == "ok" and [n for n, _ in bot.calls] == ["cancel"] * 3 and sleeps == [5, 5]
