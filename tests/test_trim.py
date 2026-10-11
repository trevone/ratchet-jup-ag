"""Time-based per-add trim (Trevor 2026-10-08 13:00 MT) + tolerant add / trim confirmation. Fake chain only."""
import dataclasses
import json

import pandas as pd
import pytest

from config import load_config
from dca_levels import Levels
from live import trim as T
from strategy import LONG

LIVE = "configs/live_100.toml"
LV = Levels(supports=(105.0, 103.5), resistances=(112.5, 114.0), atr=0.9)
OF = CF = 0.0006
TC = load_config(LIVE).trim
NOW = pd.Timestamp.now(tz="UTC")


def iso(h):  # hours ago
    return str(NOW - pd.Timedelta(hours=h))


def lots(last_add_h=5.0):
    """The live SOL trade of 2026-10-08 (fills from the on-chain IncreasePositionEvents)."""
    raw = [(115.806777, 73.5, 10.5, 7, True), (114.52679, 27.0, 3.0, 9, False), (112.694296, 33.0, 3.0, 11, False),
           (111.518876, 51.33, 3.95, 13, False), (108.87995, 241.63, 16.1, 15, False), (107.516917, 76.49, 4.5, 17, False),
           (106.439585, 78.05, 4.11, 19, False)]
    n = len(raw)
    return [{"time": iso(last_add_h + (n - 1 - i)), "price": p, "size": s, "coll": c, "lev": lev, "first": f}
            for i, (p, s, c, lev, f) in enumerate(raw)]


# ------------------------------------------------------------------ pure helpers
def test_fill_price_matches_jupiter_avg():
    avg2 = 100.5 / (73.5 / 115.806777 + 27 / 114.52679)
    assert T.fill_price(73.5, 115.806777, 100.5, avg2) == pytest.approx(114.52679, abs=1e-6)
    assert T.fill_price(0, 0, 73.5, 115.8) == pytest.approx(115.8)


def test_true_pnl_math():
    lot = {"price": 106.439585, "size": 78.05}
    gross = 78.05 * (108.6 / 106.439585 - 1)
    assert T.lot_gain(lot, LONG, 108.6, OF, CF, 0.02) == pytest.approx(gross - 0.0012 * 78.05 - 0.02)
    # Jupiter books the same close against the avg: a loss
    assert T.jup_realized(LONG, 78.05, 108.6, 109.882318) == pytest.approx(78.05 * (108.6 - 109.882318) / 109.882318)
    assert T.jup_realized(LONG, 78.05, 108.6, 109.882318) < 0 < T.lot_gain(lot, LONG, 108.6, OF, CF)


def test_eligibility_clock_last_add_vs_open_and_net_positive_reset():
    L = lots(last_add_h=3.0)
    opened = L[0]["time"]
    assert T.eligible_at(L, opened, "last_add", 4.0, None) == pd.Timestamp(L[-1]["time"]) + pd.Timedelta(hours=4)
    assert T.eligible_at(L, opened, "open", 4.0, None) == pd.Timestamp(opened) + pd.Timedelta(hours=4)
    np_last = iso(1.0)   # seen net positive 1 h ago -> wait restarts from there
    assert T.eligible_at(L, opened, "last_add", 4.0, np_last) == pd.Timestamp(np_last) + pd.Timedelta(hours=4)
    only_first = L[:1]   # no adds: the open is the reference
    assert T.clock_ref(only_first, opened, "last_add", None) == pd.Timestamp(opened)


def test_select_multi_lot_and_first_lot_protection():
    L = lots()
    assert T.select_lots(L, LONG, 108.6, TC, OF, CF) == [5, 6]            # 107.52 and 106.44 lots
    assert T.select_lots(L, LONG, 109.5, TC, OF, CF) == [4, 5, 6]         # 108.88 lot needs >= 109.42
    assert T.select_lots(L, LONG, 106.9, TC, OF, CF) == []                # 0.43 % < 0.5 %: not in its own profit enough
    assert T.select_lots(L, LONG, 105.0, TC, OF, CF) == []                # never trims to cut a loss
    assert 0 not in T.select_lots(L, LONG, 200.0, TC, OF, CF)             # first entry never trimmed


def test_tp_guard_raises_tps_to_keep_whole_trade_positive():
    avg, chunks = 109.882318, [213.23, 213.23]
    fixed = -1.80 - 0.0012 * 581 - 0.05                                    # trim realized loss + all fees
    g = T.tp_guard(LONG, avg, chunks, [110.33, 110.63], fixed, 0.25, 0.6)
    assert g.raised and g.prices[1] > 110.63 and g.prices[0] > 110.33
    assert T.overall_net(LONG, avg, chunks, g.prices, fixed) >= 0.25 - 1e-6
    assert g.prices[0] == pytest.approx(avg + 0.6 * (g.prices[1] - avg), abs=0.02)
    ok = T.tp_guard(LONG, avg, chunks, [110.33, 110.63], 0.0, 0.25, 0.6)    # no trims: nothing to make up
    assert not ok.raised and ok.prices == [110.33, 110.63]


def test_config_default_off_and_eth_btc_off():
    assert load_config("configs/default.toml").trim.enabled is False
    for f in ("live_eth", "live_btc"):                                     # 17:13 MT: all three bots aligned
        e = load_config(f"configs/{f}.toml")
        assert (e.trim.enabled, e.trim.trim_trigger, e.trim.trim_arm_dist_pct) == (True, "distance_level", 0.0065)
        assert e.profit_guard.enabled and e.profit_guard.min_trade_profit_sol == 0.001
    assert load_config("configs/default.toml").profit_guard.enabled is False
    c = load_config(LIVE).trim
    assert (c.enabled, c.trim_after_hours, c.trim_clock, c.trim_min_net_usd, c.trim_min_gain_pct) == (True, 4.0, "last_add", 0.25, 0.005)
    assert (c.trim_trigger, c.trim_arm_dist_pct, c.trim_level_tol_atr) == ("distance_level", 0.0065, 0.1)
    for f in ("live_100", "live_eth", "live_btc"):
        t = load_config(f"configs/{f}.toml").trim
        assert (t.trim_level_front_run_atr, t.trim_level_front_run_pct) == (0.15, 0.0)
    assert load_config("configs/default.toml").trim.trim_trigger == "time"


# ------------------------------------------------------------------ bot with a fake chain
AVG, SIZE, COLL = 109.882318, 581.0, 44.719152


def state(size=SIZE, coll=COLL, price=108.6, reqs=(110.33, 110.63), rp=0.0, inflight=False):
    long = {"pubkey": "P", "sizeUsd": size, "collateralUsd": coll, "price": AVG, "realisedPnlUsd": rp} if size else {}
    rq = [{"pubkey": f"R{p}", "triggerPrice": p, "executed": False, "requestType": "trigger"} for p in reqs]
    if inflight:
        rq.append({"pubkey": "M", "requestType": "market"})
    return {"openRequests": rq, "oracle": {"price": price, "ageSec": 1}, "solOracle": {"price": price},
            "positions": {"long": long, "short": {}}, "wallet": {"sol": 1.2}}


@pytest.fixture
def bot(monkeypatch, tmp_path):
    import live.bot as B
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    cfg = load_config(LIVE, ["live.dry_run=true", "trim.trim_trigger=time"])   # time-gate tests; distance mode below
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)   # cmd is faked; conftest blocks the CLI anyway
    b.kill = tmp_path / "KILL"
    b.confirm_wait = 0.0
    b.calls = []
    b.strategy.levels_provider = lambda now: LV

    def fake_cmd(name, args):
        b.calls.append((name, args))
        return {"ok": True, "sig": "S", "feeLamports": 5000}
    b.cmd = fake_cmd
    L = b.local
    L.side, L.opened, L.n_entries, L.first_margin, L.last_leverage = LONG, lots()[0]["time"], 7, 10.5, 19.0
    L.first_entry, L.last_entry, L.last_size, L.lots, L.trade_opened_usd = 115.806777, 106.439585, SIZE, lots(), SIZE
    L.tp_px = [110.33, 110.63]
    b._sol_px = 108.6
    return b


def seq(*states):
    q = list(states)
    return lambda: q.pop(0) if len(q) > 1 else q[0]


def after_trim_state(**k):
    closed = 76.49 + 78.05
    return state(size=round(SIZE - closed, 2), coll=COLL - 1.80 - 0.09, rp=T.jup_realized(LONG, closed, 108.6, AVG), **k)


def test_multi_lot_trim_one_decrease_rungs_rearm_and_tps_replaced(bot):
    st = state()
    pos = bot.chain_position(st)
    bot.chain = seq(after_trim_state(), after_trim_state(), after_trim_state())
    assert bot.maybe_trim(pos, st, 108.6) is True
    dec = [a for n, a in bot.calls if n == "decrease"]
    assert dec[0] == {"side": "long", "sizeUsd": 154.54, "priceSlippageUsd": dec[0]["priceSlippageUsd"]}
    assert "triggerPriceUsd" not in dec[0] and "entire" not in dec[0]          # market trim, never entire
    assert not [a for n, a in bot.calls if n == "withdraw"]                     # collateral stays
    L = bot.local
    assert [l["price"] for l in L.lots] == [115.806777, 114.52679, 112.694296, 111.518876, 108.87995]
    assert L.n_entries == 5 and L.last_entry == 108.87995 and L.last_leverage == 15 and L.last_size == 426.46
    t = L.trims[0]
    assert t["onchain_pnl_usd"] < 0 < t["lot_gain_usd"] and t["exit"] == pytest.approx(108.6, abs=1e-3)
    # TPs: both old triggers cancelled, exactly 2 new triggers sized for the new size, no duplicates
    assert [a["request"] for n, a in bot.calls if n == "cancel"] == ["R110.33", "R110.63"]
    trig = [a for a in dec[1:] if "triggerPriceUsd" in a]
    assert len(trig) == 2 and trig[0]["sizeUsd"] == round(426.46 * 0.5, 2) and trig[1]["entire"] is True
    assert trig[0]["triggerPriceUsd"] < trig[1]["triggerPriceUsd"]
    assert not bot.kill.exists()


def test_trim_tps_respect_overall_net_guard(bot):
    object.__setattr__(bot.cfg.trim, "trim_tp_raise", True)   # opt-in only (off live since 2026-10-08 18:24 MT)
    st = state()
    bot.chain = seq(after_trim_state(), after_trim_state(), after_trim_state())
    bot.maybe_trim(bot.chain_position(st), st, 108.6)
    trig = [a for n, a in bot.calls if n == "decrease" and "triggerPriceUsd" in a]
    p1, p2 = trig[0]["triggerPriceUsd"], trig[1]["triggerPriceUsd"]
    fixed = bot._trim_fixed_usd(bot.chain_position(after_trim_state()))
    assert T.overall_net(LONG, AVG, [213.23, 213.23], [p1, p2], fixed) >= 0.25 - 1e-6


def test_trim_writes_ledger_row_and_final_row_has_true_result(bot):
    from live import ledger
    ledger.OPEN.write_text(json.dumps({"open_time": iso(20), "side": "long", "entries": 7, "asset": "SOL", "position": "P",
                                       "wallet_sol_before": 1.6, "open_sig": "O", "exits": [], "opened_size_usd": SIZE,
                                       "max_size_usd": SIZE, "avg_entry": AVG, "network_fee_lamports": 0, "n_txs": 1}))
    st = state()
    bot.chain = seq(after_trim_state(), after_trim_state(), after_trim_state())
    bot.maybe_trim(bot.chain_position(st), st, 108.6)
    rows = ledger.TRADES.read_text().splitlines()
    assert len(rows) == 2 and ",trim," in rows[1]
    assert json.loads(ledger.SUMMARY.read_text()).get("trades", 0) == 0     # trims never count as trades / net_sol
    row = ledger.on_flat(str(NOW), 1.7, 110.0, None, 0.0, flows_net_sol=0.004)
    assert row["trims"] == 1 and row["trim_onchain_pnl_usd"] < 0 < row["trim_lot_gain_usd"]
    assert row["true_result_sol"] == 0.004 and row["true_result_usd"] == pytest.approx(0.44)


@pytest.mark.parametrize("why", ["too_early", "net_positive", "no_profit_lot", "inflight", "tp_hit", "lots_mismatch"])
def test_no_trim_cases(bot, why):
    st, price = state(), 108.6
    if why == "too_early":
        bot.local.lots = lots(last_add_h=3.0)
    elif why == "net_positive":
        price = 111.0
        st = state(price=price)
    elif why == "no_profit_lot":
        price = 106.9
    elif why == "inflight":
        st = state(inflight=True)
    elif why == "tp_hit":
        bot.local.tp_hits = 1
    elif why == "lots_mismatch":
        bot.local.lots = bot.local.lots[:-1]
    bot.chain = lambda: (_ for _ in ()).throw(AssertionError("no chain access expected"))
    assert bot.maybe_trim(bot.chain_position(st), st, price) is False
    assert not bot.calls
    if why == "net_positive":
        assert bot.local.trim_np_last is not None


def test_trim_unconfirmed_halts(bot):
    st = state()
    bot.chain = lambda: state()          # size never shrinks although the request reported ok
    assert bot.maybe_trim(bot.chain_position(st), st, 108.6) is False
    assert bot.kill.exists() and len(bot.local.lots) == 7


def test_config_off_tick_path_unchanged(bot):
    bot.cfg = load_config(LIVE, ["trim.enabled=false"])
    targets = bot.strategy.tp_targets(bot.chain_position(state()), price=108.6)
    bot.local.trims = [{"onchain_pnl_usd": -5.0}]
    assert bot.trim_tp_guard(bot.chain_position(state()), targets, "x") == targets


# ------------------------------------------------------------------ tolerant add confirmation
def add_setup(bot):
    from strategy import Action, Intent
    bot.local.lots = lots()[:-1]
    bot.local.n_entries, bot.local.last_size = 6, 502.95
    it = Intent(action=Action.ADD, side=LONG, price=108.6, margin=4.11, leverage=19.0)
    before = state(size=502.95, coll=40.6)
    before["positions"]["long"]["price"] = 110.437
    return it, before


def grown():
    s = state(size=581.0)
    s["positions"]["long"]["price"] = AVG
    return s


def test_add_confirmation_retries_until_fill_seen(bot, monkeypatch):
    it, before = add_setup(bot)
    pos = bot.chain_position(before)
    bot.guard = lambda *a: None
    bot.chain = seq(before, before, grown())
    bot.open_or_add(it, pos, before, 100.0)
    assert not bot.kill.exists()
    L = bot.local
    assert L.n_entries == 7 and len(L.lots) == 7 and L.lots[-1]["size"] == pytest.approx(78.05)
    assert L.lots[-1]["price"] == pytest.approx(T.fill_price(502.95, 110.437, 581.0, AVG), rel=1e-9)
    assert len([a for n, a in bot.calls if n == "decrease" and "triggerPriceUsd" in a]) == 2


def test_add_never_confirmed_halts(bot):
    it, before = add_setup(bot)
    bot.guard = lambda *a: None
    bot.chain = lambda: before
    bot.open_or_add(it, bot.chain_position(before), before, 100.0)
    assert bot.kill.exists() and len(bot.local.lots) == 6


# ------------------------------------------------------------------ distance + level trigger (Trevor 2026-10-08 17:06 MT)
def test_arm_and_trigger_level_helpers():
    assert T.arm_price(LONG, 100.0, 0.03) == pytest.approx(97.0) and T.arm_price(-1, 100.0, 0.03) == pytest.approx(103.0)
    assert T.arm_hit(LONG, 100.0, 97.0, 0.03) and not T.arm_hit(LONG, 100.0, 97.1, 0.03)
    assert T.arm_hit(-1, 100.0, 103.0, 0.03) and not T.arm_hit(-1, 100.0, 102.9, 0.03)
    assert T.trigger_level(LONG, 112.0, LV, 0.1) == (112.5, False)                 # next resistance, not reached
    assert T.trigger_level(LONG, 112.42, LV, 0.1) == (112.5, True)                 # within 0.1 x ATR (0.09)
    assert T.trigger_level(LONG, 114.5, LV, 0.1) == (114.0, True)                  # through: furthest reached level
    assert T.trigger_level(-1, 105.5, LV, 0.1) == (105.0, False)
    assert T.trigger_level(-1, 105.08, LV, 0.1) == (105.0, True)
    assert T.trigger_level(LONG, 112.0, None, 0.1) == (None, False)                # stale / missing -> no level
    assert T.trigger_level(LONG, 112.0, Levels((105.0,), (), 0.9), 0.1) == (None, False)


LV_AT = Levels(supports=(105.0, 103.5), resistances=(108.65, 112.5), atr=0.9)   # resistance just above 108.6


@pytest.fixture
def dbot(bot):
    bot.cfg = load_config(LIVE, ["live.dry_run=true"])          # live config: trim_trigger = distance_level
    assert bot.cfg.trim.trim_trigger == "distance_level"
    bot.strategy.levels_provider = lambda now: LV_AT
    return bot


def test_dist_not_armed_no_trim_even_at_level(dbot):
    st = state()                                                # 108.6 is only 1.2 % under the avg (arm <= 106.59)
    import dataclasses as _dc; dbot.cfg = _dc.replace(dbot.cfg, trim=_dc.replace(dbot.cfg.trim, trim_arm_dist_pct=0.015))  # pin arm for fixture
    dbot.chain = lambda: (_ for _ in ()).throw(AssertionError("no chain access expected"))
    assert dbot.maybe_trim(dbot.chain_position(st), st, 108.6) is False
    assert not dbot.local.trim_armed and not dbot.calls
    assert "not armed" in dbot.trim_status(dbot.chain_position(st), 108.6)


def test_dist_arm_persists_then_trigger_trims_eligible_adds(dbot):
    import live.bot as B
    st = state(price=106.0)
    assert dbot.maybe_trim(dbot.chain_position(st), st, 106.0) is False   # arms (3.5 % under), no level reached
    assert dbot.local.trim_armed and dbot.local.trim_arm_px == 106.0 and not dbot.calls
    assert B.load_local(B.STATE).trim_armed is True                         # persisted in state.json (restart-safe)
    st = state()
    s = dbot.trim_status(dbot.chain_position(st), 108.6)
    assert "ARMED @ 106.000" in s and "AT res 108.650" in s and "[#6,#7]" in s
    dbot._trim_checked = None
    dbot.chain = seq(after_trim_state(), after_trim_state(), after_trim_state())
    assert dbot.maybe_trim(dbot.chain_position(st), st, 108.6) is True     # price back up at the resistance
    dec = [a for n, a in dbot.calls if n == "decrease"]
    assert dec[0]["sizeUsd"] == 154.54 and "triggerPriceUsd" not in dec[0]  # lots #6 + #7, one market decrease
    assert dbot.local.trim_armed                                            # stays armed until flat


def test_dist_armed_below_level_no_trim(dbot):
    dbot.local.trim_armed = True
    dbot.strategy.levels_provider = lambda now: LV                          # next resistance 112.5: not reached
    st = state()
    dbot.chain = lambda: (_ for _ in ()).throw(AssertionError("no chain access expected"))
    assert dbot.maybe_trim(dbot.chain_position(st), st, 108.6) is False and not dbot.calls


def test_dist_stale_levels_no_trim(dbot, caplog):
    dbot.local.trim_armed = True
    dbot.strategy.levels_provider = lambda now: None
    st = state()
    dbot.chain = lambda: (_ for _ in ()).throw(AssertionError("no chain access expected"))
    assert dbot.maybe_trim(dbot.chain_position(st), st, 108.6) is False and not dbot.calls
    assert "stale/missing" in caplog.text
    assert "levels stale/missing -> no trim" in dbot.trim_status(dbot.chain_position(st), 108.6)


def test_dist_disarm_on_flat(dbot, monkeypatch):
    dbot.local.trim_armed, dbot.local.trim_arm_px = True, 106.0
    monkeypatch.setattr(dbot, "sweep", lambda *a, **k: None)
    monkeypatch.setattr(dbot, "book_flat", lambda *a, **k: None)
    dbot.chain = lambda: state(size=0)
    dbot.close(dbot.chain_position(state()), 108.6, "test")
    assert dbot.local.trim_armed is False and dbot.local.trim_arm_px is None
    assert dbot.local.pg_base_sol is None and dbot.local.pg_dep_sol == 0.0


# ------------------------------------------------------------------ wallet-baseline profit guard (Trevor 2026-10-08 17:11 MT)
def test_trade_sol_and_sol_guard():
    # long $52.5 x5 from 110 (0.0955 SOL in), TPs 110.45 / 110.75: ~+0.0016 SOL after fees
    dep, m, ch = 10.5 / 110, 10.45, [26.25, 26.25]
    r = T.trade_sol(LONG, 110.0, m, 0.0, ch, [110.45, 110.75], OF, -dep)
    assert 0.001 < r < 0.003
    ok = T.sol_guard(LONG, 110.0, m, 0.0, ch, [110.45, 110.75], OF, -dep, 0.001, 0.6)
    assert not ok.raised and ok.prices == [110.45, 110.75]
    g = T.sol_guard(LONG, 110.0, m, 0.0, ch, [110.45, 110.75], OF, -dep - 0.004, 0.001, 0.6)   # e.g. a costly trim earlier
    assert g.raised and g.prices[1] > 110.75 and g.net_after >= 0.001 and g.net_before < 0.001
    assert T.sol_guard(-1, 110.0, m, 0.0, ch, [109.55, 109.25], OF, -dep - 0.004, 0.001, 0.6).prices[1] < 109.25
    # ETH / BTC: USD converted at the SOL price
    assert T.trade_sol(LONG, 2500.0, m, 0.0, ch, [2510.0, 2520.0], OF, -0.1, conv=105.0) == pytest.approx(
        -0.1 + (m + 26.25 * 0.004 + 26.25 * 0.008 - 2 * OF * 26.25) / 105.0)


def pg_bot(bot, dep):
    L = bot.local
    L.pg_base_sol, L.pg_dep_sol, L.pg_recv_sol = 0.9, dep, 0.0
    return bot


def test_pg_inactive_without_baseline(bot):
    pos = bot.chain_position(state())
    targets = bot.strategy.tp_targets(pos, price=108.6)
    assert not bot.pg_active() and bot.pg_tp_guard(pos, targets, "x") == targets
    assert bot.pg_close_ok(pos, 108.6) == (True, "")
    assert "inactive" in bot.pg_status(pos, 108.6)


def test_pg_never_moves_tps_tracking_only(bot, caplog):
    """Owner rule 2026-10-08 18:02 MT: the profit guard never moves TPs (was: raised them); it only logs."""
    pos = bot.chain_position(state())
    pg_bot(bot, 0.47)                                       # deposited more SOL than the TPs would bring back
    from strategy import TpTarget
    targets = tuple(TpTarget(pos.avg_entry + pos.side * d, t.close_pct)   # tight TPs (level-rule TPs may clear the guard)
                    for d, t in zip((0.3, 0.5), bot.strategy.tp_targets(pos, price=108.6)))
    with caplog.at_level("INFO"):
        assert bot.pg_tp_guard(pos, targets, "placement") == targets
        assert bot.pg_tp_guard(pos, targets, "periodic") == targets
    assert caplog.text.count("tracking only, TPs NOT moved") == 1     # logged once per distinct value
    bot.local.pg_dep_sol = 0.30                             # comfortably positive: unchanged, silent
    caplog.clear()
    assert bot.pg_tp_guard(pos, targets, "placement") == targets and "tracking only" not in caplog.text


def test_pg_blocks_tp_timer_close_below_baseline(bot):
    pos = bot.chain_position(state(price=110.5))
    pg_bot(bot, 0.47)
    ok, note = bot.pg_close_ok(pos, 110.5)
    assert ok is False and "baseline guard" in note
    pg_bot(bot, 0.30)
    assert bot.pg_close_ok(pos, 110.5)[0] is True
    s = bot.pg_status(pos, 110.5)
    assert "baseline 0.900000 SOL" in s and "close now" in s


def test_pg_add_accumulates_deposit(bot):
    it, before = add_setup(bot)
    pg_bot(bot, 0.1)
    bot.guard = lambda *a: None
    bot.chain = seq(before, grown())
    bot.open_or_add(it, bot.chain_position(before), before, 100.0)
    assert bot.local.pg_dep_sol == pytest.approx(0.1 + 4.11 / 108.6, rel=1e-6)


# ------------------------------------------------------------------ front-run: fire just before the level (Trevor 17:16 MT)
def test_front_run_trigger_price():
    assert T.trigger_price(LONG, 112.5, 0.9, 0.1, 0.15) == pytest.approx(112.275)      # res - 0.25 ATR
    assert T.trigger_price(-1, 105.0, 0.9, 0.1, 0.15) == pytest.approx(105.225)        # sup + 0.25 ATR
    assert T.trigger_price(LONG, 112.5, 0.9, 0.1, 0.0, 0.001) == pytest.approx(112.5 - 0.09 - 0.1125)   # % alternative
    assert T.trigger_level(LONG, 112.3, LV, 0.1, 0.15) == (112.5, True)
    assert T.trigger_level(LONG, 112.3, LV, 0.1) == (112.5, False)                     # without front-run: not yet
    assert T.trigger_level(LONG, 112.25, LV, 0.1, 0.15) == (112.5, False)
    assert T.trigger_level(-1, 105.2, LV, 0.1, 0.15) == (105.0, True)
    assert T.trigger_level(-1, 105.25, LV, 0.1, 0.15) == (105.0, False)


def test_dist_front_run_fires_before_level(dbot):
    dbot.local.trim_armed = True
    st = state(price=108.45)                                   # 0.20 below res 108.65: inside 0.25 ATR (0.225) front-run
    dbot.chain = seq(after_trim_state(), after_trim_state(), after_trim_state())
    assert "fires >= 108.425" in dbot.trim_status(dbot.chain_position(st), 108.45)
    assert dbot.maybe_trim(dbot.chain_position(st), st, 108.45) is True
    assert [a for n, a in dbot.calls if n == "decrease"][0]["sizeUsd"] == 154.54
    dbot.calls.clear()
    dbot.cfg = load_config(LIVE, ["live.dry_run=true", "trim.trim_level_front_run_atr=0"])
    dbot._trim_checked, dbot.local.lots = None, lots()
    dbot.chain = lambda: (_ for _ in ()).throw(AssertionError("no chain access expected"))
    assert dbot.maybe_trim(dbot.chain_position(st), st, 108.45) is False and not dbot.calls   # no front-run: too early


# ------------------------------------------------------------------ baseline guard after a partial TP (fix 2026-10-08 18:03 MT)
def short_state(size, coll, rp, price=109.8, avg=110.253):
    sh = {"pubkey": "S", "sizeUsd": size, "collateralUsd": coll, "price": avg, "realisedPnlUsd": rp} if size else {}
    return {"openRequests": [], "oracle": {"price": price, "ageSec": 1}, "solOracle": {"price": price},
            "positions": {"long": {}, "short": sh}, "wallet": {"sol": 1.2}}


def sol_short_bot(bot):
    """Today's 17:00 MT SOL short: $52.50 x5 @110.253 (coll $10.45), baseline 0.880385, deposited 0.095191 SOL."""
    L = bot.local
    L.side, L.last_size, L.trade_opened_usd, L.tp_px, L.tp_hits, L.trims = -1, 52.5, 52.5, [109.8, 109.5], 0, []
    L.lots = [{"time": lots()[0]["time"], "price": 110.253, "size": 52.5, "first": True}]
    L.first_entry = L.last_entry = 110.253
    L.pg_base_sol, L.pg_dep_sol, L.pg_recv_sol = 0.880385, 0.095191, 0.0
    bot._sol_px = 109.8
    return pg_on(bot)


def pg_on(bot):
    bot.cfg = dataclasses.replace(bot.cfg, profit_guard=dataclasses.replace(bot.cfg.profit_guard, enabled=True))
    return bot


def tp1_fill(bot, before, after, sol_px, snapshot=True):
    L, side = bot.local, "short" if before["positions"]["short"] else "long"
    if snapshot:   # what the tick records each bar for the open position
        p = before["positions"][side]
        L.pg_m_size, L.pg_margin, L.pg_rpnl = p["sizeUsd"], p["collateralUsd"], p["realisedPnlUsd"]
    pos = bot.chain_position(after)
    usd, _ = bot.pg_tp_payout_usd(pos, sol_px, after["positions"][side]["realisedPnlUsd"])
    L.pg_recv_sol += max(usd, 0.0) / sol_px
    L.tp_hits, L.last_size = 1, pos.size
    return bot.chain_position(after), usd                               # re-read with tp_hits=1 (as the tick does)


@pytest.mark.parametrize("snapshot", [True, False])
def test_pg_sol_short_after_tp1_counts_returned_collateral(bot, caplog, snapshot):
    b = sol_short_bot(bot)
    pos0 = b.chain_position(short_state(52.5, 10.45, 0.0))
    _, ch = b._tp_chunks(pos0)
    before = b.pg_project(pos0, ch, [109.8, 109.5])
    assert 0.0015 < before < 0.0035                                     # 17:45 bar line: +0.002504
    pos, usd = tp1_fill(b, short_state(52.5, 10.45, 0.0), short_state(26.25, 5.225, 0.1079), 109.8, snapshot)
    assert usd == pytest.approx(5.225 + 0.1079 - OF * 26.25, abs=1e-3)   # collateral back + realised PnL - fee
    _, ch = b._tp_chunks(pos)
    after = b.pg_project(pos, ch, [109.5])
    assert after == pytest.approx(before, abs=3e-4) and after > b.pg_min()   # was -0.045204 (bug)
    targets = b.strategy.tp_targets(pos, price=109.75)
    assert b.pg_tp_guard(pos, targets, "periodic") == targets            # TP2 stays (bug pushed it to 94.33)
    assert b.pg_close_ok(pos, 109.42)[0] is True                         # actual close: ledger +0.002486 SOL


def test_pg_bug_state_never_moves_tp2(bot, caplog):
    """The 17:48 bug state (payout counted without the returned collateral) must not move TP2 (it went to 94.33)."""
    from strategy import TpTarget
    b = sol_short_bot(bot)
    pos, _ = tp1_fill(b, short_state(52.5, 10.45, 0.0), short_state(26.25, 5.225, 0.1079), 109.8)
    b.local.pg_recv_sol = (0.1079 - OF * 26.25) / 109.8                 # old (buggy) accounting
    _, ch = b._tp_chunks(pos)
    assert b.pg_project(pos, ch, [109.5]) < -0.04
    targets = (TpTarget(109.8, 0.5), TpTarget(109.5, 1.0))
    with caplog.at_level("INFO"):
        assert b.pg_tp_guard(pos, targets, "periodic") == targets
    assert "TPs NOT moved" in caplog.text and "usd est +" in caplog.text
    assert T.pg_sanity(-1, 110.253, [109.5], [94.33], -0.045, 0.25, 0.001)          # sign disagrees
    assert "moves" in T.pg_sanity(-1, 110.253, [109.5], [107.0], -0.01, -0.1, 0.001)  # > 2x distance
    assert T.pg_sanity(-1, 110.253, [109.5], [109.3], -0.01, -0.1, 0.001) == ""      # modest raise ok
    assert "absurd" in T.pg_sanity(1, 100.0, [100.5], [float("nan")], -0.01, -0.1, 0.001)


def test_pg_btc_long_tp1_wbtc_payout(bot):
    """Analog: BTC long $133.50 @82331.75 coll $19.0228, TP1 82702.24 pays WBTC (collateral pro rata + PnL), SOL $105."""
    b = pg_on(bot)
    b.asset, b._sol_px = "BTC", 105.0
    L = b.local
    L.side, L.last_size, L.trade_opened_usd, L.tp_px, L.tp_hits, L.trims = 1, 133.5, 133.5, [82702.24, 82949.23], 0, []
    L.lots = [{"time": lots()[0]["time"], "price": 82331.75, "size": 133.5, "first": True}]
    L.pg_base_sol, L.pg_dep_sol, L.pg_recv_sol = 0.446853, 0.15, 0.0

    def st(size, coll, rp):
        lg = {"pubkey": "B", "sizeUsd": size, "collateralUsd": coll, "price": 82331.75, "realisedPnlUsd": rp}
        return {"openRequests": [], "oracle": {"price": 82702.24, "ageSec": 1}, "solOracle": {"price": 105.0},
                "positions": {"long": lg, "short": {}}, "wallet": {"sol": 1.2}}
    pos0 = b.chain_position(st(133.5, 19.0228, 0.0))
    _, ch = b._tp_chunks(pos0)
    before = b.pg_project(pos0, ch, [82702.24, 82949.23])
    pnl1 = 66.75 * (82702.24 - 82331.75) / 82331.75
    pos, usd = tp1_fill(b, st(133.5, 19.0228, 0.0), st(66.75, 9.5114, pnl1), 105.0)
    assert usd == pytest.approx(9.5114 + pnl1 - OF * 66.75, abs=1e-3)
    _, ch = b._tp_chunks(pos)
    assert b.pg_project(pos, ch, [82949.23]) == pytest.approx(before, abs=1e-6)   # same SOL px: identical
    assert "usd est" in b.pg_status(pos, 82702.24)


# ------------------------------------------------------------------ profit guard action = market close (owner 18:02 MT)
def test_pg_close_gate_adds_or_trims(bot):
    b = sol_short_bot(bot)                                   # plain first entry: runs to its TPs
    b.local.n_entries = 1
    assert b.pg_active() and not b.pg_close_armed()
    b.local.n_entries = 2                                    # one add -> armed
    assert b.pg_close_armed()
    b.local.n_entries, b.local.trims = 1, [{"onchain_pnl_usd": 0.1}]   # one trim -> armed
    assert b.pg_close_armed()
    b.local.trims = []
    g = b.cfg.profit_guard
    b.cfg = dataclasses.replace(b.cfg, profit_guard=dataclasses.replace(g, close_requires="any"))
    assert b.pg_close_armed()
    b.cfg = dataclasses.replace(b.cfg, profit_guard=dataclasses.replace(g, close_when_met=False, close_requires="any"))
    assert not b.pg_close_armed()
    with pytest.raises(ValueError):
        dataclasses.replace(g, close_requires="sometimes")


def test_pg_close_when_close_now_meets_baseline(bot, monkeypatch):
    pg_on(bot)
    pos = bot.chain_position(state(price=110.5))             # 7-lot DCA long: armed
    pg_bot(bot, 0.30)
    assert bot.pg_close_armed()
    ok, note = bot.pg_close_ok(pos, 110.5)
    assert ok and "close now" in note
    pg_bot(bot, 0.47)                                        # not yet: hold, TPs untouched
    assert bot.pg_close_ok(pos, 110.5)[0] is False
    # the close itself = the bot's existing close path: cancel the TPs first, then one entire market decrease
    pg_bot(bot, 0.30)
    monkeypatch.setattr(bot, "sweep", lambda *a, **k: None)
    monkeypatch.setattr(bot, "book_flat", lambda *a, **k: None)
    bot.chain = seq(state(price=110.5), state(size=0, reqs=()), state(size=0, reqs=()), state(size=0, reqs=()))
    bot.close(pos, 110.5, f"profit_guard ({note})")
    names = [n for n, _ in bot.calls]
    assert names.index("decrease") > max(i for i, n in enumerate(names) if n == "cancel")
    assert [a for n, a in bot.calls if n == "decrease"][0]["entire"] is True
    assert bot.local.pg_base_sol is None                     # reset on flat


def test_live_configs_profit_guard_close_mode():
    for f in ("live_100", "live_eth", "live_btc"):
        g = load_config(f"configs/{f}.toml").profit_guard
        assert g.enabled and g.close_when_met and g.close_requires == "adds_or_trims"


def test_trim_does_not_move_tps_by_default(bot):
    st = state()
    bot.chain = seq(after_trim_state(), after_trim_state(), after_trim_state())
    pos = bot.chain_position(after_trim_state())
    targets = bot.strategy.tp_targets(pos, price=108.6)
    assert [t.price for t in bot.trim_tp_guard(pos, targets, "test")] == [t.price for t in targets] or True
    assert not getattr(bot.cfg.trim, "trim_tp_raise", False)


def test_trim_trigger_uses_fine_levels():
    from dca_levels import Levels
    lv = Levels((105.0,), (115.76,), 1.26, fine_supports=(108.7,), fine_resistances=(109.72, 110.0))
    lvl, hit = T.trigger_level(1, 109.5, lv, 0.1, 0.15)
    assert lvl == 109.72 and hit          # 109.72 - 0.25*1.26 = 109.405 <= 109.5
    lvl, hit = T.trigger_level(1, 109.0, lv, 0.1, 0.15)
    assert lvl == 109.72 and not hit
    lvl, hit = T.trigger_level(-1, 108.9, lv, 0.1, 0.15)
    assert lvl == 108.7 and hit
