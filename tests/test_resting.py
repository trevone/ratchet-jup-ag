"""[resting] resting limit adds, per-add partial TPs, last-partial-price rule (Trevor spec 2026-10-11). Fake chain only."""
import dataclasses
import types

import pandas as pd
import pytest

from config import load_config
from dca_levels import Levels
from live import resting as R
from live import trim as T
from strategy import LONG, SHORT

LIVE = "tests/fixtures/configs_pre_c/live_100.toml"
ON = ["resting.adds_enabled=true", "resting.partials_enabled=true", "resting.last_partial_rule=true"]
LV = Levels(supports=(105.0, 103.5), resistances=(112.5, 114.0), atr=0.9,
            fine_supports=(104.2,), fine_resistances=(107.2, 108.1, 110.9))
NOW = pd.Timestamp.now(tz="UTC")


# ------------------------------------------------------------------ config: everything OFF live
@pytest.mark.parametrize("path", ["tests/fixtures/configs_pre_c/live_100.toml", "tests/fixtures/configs_pre_c/live_eth.toml", "tests/fixtures/configs_pre_c/live_btc.toml"])
def test_live_configs_all_flags_off(path):
    r = load_config(path).resting
    assert (r.adds_enabled, r.partials_enabled, r.last_partial_rule, r.any_on) == (False, False, False, False)
    assert (r.partial_levels_min, r.partial_levels_max) == (1, 4)


def test_code_defaults_off():
    from config import RestingConfig
    assert not RestingConfig().any_on


# ------------------------------------------------------------------ pure helpers
@pytest.mark.parametrize("rung,n", [(1, 1), (2, 1), (5, 2), (7, 2), (8, 3), (12, 4), (13, 4), (14, 4), (30, 4)])
def test_partial_levels_n_linear_rounded(rung, n):
    assert R.partial_levels_n(rung, 1, 4, 14) == n


def test_last_partial_reference_and_beyond():
    assert R.add_ref(LONG, 100.0, 102.0) == 100.0 and R.add_ref(LONG, 100.0, 97.0) == 97.0
    assert R.add_ref(SHORT, 100.0, 98.0) == 100.0 and R.add_ref(SHORT, 100.0, 103.0) == 103.0
    assert R.add_ref(LONG, 100.0, None) == 100.0
    assert R.beyond_last_partial(LONG, 96.9, 97.0) and not R.beyond_last_partial(LONG, 97.5, 97.0)
    assert R.beyond_last_partial(SHORT, 103.1, 103.0) and not R.beyond_last_partial(SHORT, 102.0, 103.0)


# ------------------------------------------------------------------ bot with a fake chain
AVG, SIZE, COLL = 109.882318, 581.0, 44.719152


def lots():
    raw = [(115.806777, 73.5, 10.5, 7, True), (114.52679, 27.0, 3.0, 9, False), (112.694296, 33.0, 3.0, 11, False),
           (111.518876, 51.33, 3.95, 13, False), (108.87995, 241.63, 16.1, 15, False), (107.516917, 76.49, 4.5, 17, False),
           (106.439585, 78.05, 4.11, 19, False)]
    return [{"time": str(NOW - pd.Timedelta(hours=10 - i)), "price": p, "size": s, "coll": c, "lev": lev, "first": f}
            for i, (p, s, c, lev, f) in enumerate(raw)]


def state(size=SIZE, coll=COLL, price=106.6, avg=AVG, reqs=None, rp=0.0):
    long = {"pubkey": "P", "sizeUsd": size, "collateralUsd": coll, "price": avg, "realisedPnlUsd": rp} if size else {}
    rq = reqs if reqs is not None else [{"pubkey": f"R{p}", "triggerPrice": p, "executed": False, "requestType": "trigger",
                                         "requestChange": "decrease"} for p in (110.33, 110.63)]
    return {"openRequests": rq, "oracle": {"price": price, "ageSec": 1}, "solOracle": {"price": price},
            "positions": {"long": long, "short": {}}, "wallet": {"sol": 1.2}}


def req(pk, px, change="decrease"):
    return {"pubkey": pk, "triggerPrice": px, "executed": False, "requestType": "trigger", "requestChange": change}


def mkbot(monkeypatch, tmp_path, flags):
    import live.bot as B
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    cfg = load_config(LIVE, ["live.dry_run=true", *flags])
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)   # cmd is faked; conftest blocks the CLI anyway
    b.kill = tmp_path / "KILL"
    b.confirm_wait = 0.0
    b.calls = []
    b.strategy.levels_provider = lambda now: LV
    n = {"lim": 0, "dec": 0}

    def fake_cmd(name, args):
        b.calls.append((name, args))
        if name == "create-limit-order":
            n["lim"] += 1
            return b.__dict__.get("lim_res") or {"ok": True, "sent": True, "sig": "SL", "positionRequest": f"LIM{n['lim']}",
                                                 "feeLamports": 5000}
        if name == "decrease":
            n["dec"] += 1
            return {"ok": True, "sig": "SD", "positionRequest": f"DEC{n['dec']}", "feeLamports": 5000}
        return {"ok": True, "sig": "S", "feeLamports": 5000}
    b.cmd = fake_cmd
    L = b.local
    L.side, L.opened, L.n_entries, L.first_margin, L.last_leverage = LONG, lots()[0]["time"], 7, 10.5, 19.0
    # first entry 115.8 keeps the 105.0 support inside the 12-ATR deep window (fixture levels, ATR 0.9)
    L.first_entry, L.last_entry, L.last_size, L.lots, L.trade_opened_usd = 115.8, 106.439585, SIZE, lots(), SIZE
    L.tp_px = [110.33, 110.63]
    b._sol_px = 106.6
    b.guard = lambda *a: None
    return b


@pytest.fixture
def bot(monkeypatch, tmp_path):
    return mkbot(monkeypatch, tmp_path, ON)


@pytest.fixture
def offbot(monkeypatch, tmp_path):
    return mkbot(monkeypatch, tmp_path, [])


ACC = types.SimpleNamespace(cash=60.0)


def test_flags_off_requests_and_lots_unchanged(offbot):
    st = state(reqs=[req("R1", 110.33), req("LIM", 104.0, "increase"), req("PX", 107.16)])
    assert offbot.ladder_requests(st) == st["openRequests"]
    assert [r["pubkey"] for r in offbot.tp_requests(st)] == ["R1", "LIM", "PX"]


def test_resting_add_placed_with_ladder_size_and_rung_price(bot):
    st = state()
    pos = bot.chain_position(st)
    plan, why = bot.rest_add_plan(pos, ACC.cash)
    assert plan is not None, why
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    lim = [a for n, a in bot.calls if n == "create-limit-order"]
    assert len(lim) == 1
    assert lim[0]["side"] == "long" and lim[0]["triggerPriceUsd"] == bot.add_px(pos, plan.trigger)
    assert bot.add_front() == pytest.approx(0.1 * LV.atr)   # 2026-10-24: own ATR unset -> analyst ATR front-run
    assert lim[0]["sizeUsd"] == round(plan.margin * plan.leverage, 2) and plan.leverage == 20.0   # 19x + 1, cap 20
    assert int(lim[0]["inputAtoms"]) == int(plan.margin / 106.6 * 1e9)
    ra = bot.local.rest_add
    assert ra["pubkey"] == "LIM1" and ra["trigger"] == lim[0]["triggerPriceUsd"]
    # an order is resting: nothing new placed, never modified
    bot.resting_tick(pos, state(reqs=st["openRequests"] + [req("LIM1", ra["trigger"], "increase")]), 106.0, ACC, 100.0)
    assert len([1 for n, _ in bot.calls if n == "create-limit-order"]) == 1
    assert not [1 for n, _ in bot.calls if n in ("cancel", "cancel-limit-order")]


def test_refused_creation_logged_and_retried_next_tick(bot, caplog):
    bot.lim_res = {"ok": False, "refused": True, "code": "collateral_size_below_minimum",
                   "error": "Collateral size must be at least $10 for new positions"}
    st = state()
    pos = bot.chain_position(st)
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    assert len([1 for n, _ in bot.calls if n == "create-limit-order"]) == 2 and bot.local.rest_add is None
    assert sum("refused / failed" in r.getMessage() for r in caplog.records) == 1   # logged once per distinct reason


def test_untracked_increase_request_blocks_a_second_order(bot):
    st = state(reqs=[req("R1", 110.33), req("FOREIGN", 104.0, "increase")])
    bot.resting_tick(bot.chain_position(st), st, 106.6, ACC, 100.0)
    assert not [1 for n, _ in bot.calls if n == "create-limit-order"]


def fill_setup(bot):
    st = state()
    pos = bot.chain_position(st)
    bot.local.rest_last_avg = AVG
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    ra = dict(bot.local.rest_add)
    new_avg = (SIZE + ra["size_usd"]) / (SIZE / AVG + ra["size_usd"] / ra["trigger"])
    st2 = state(size=SIZE + ra["size_usd"], coll=COLL + ra["margin"], avg=new_avg, price=ra["trigger"] - 0.05)
    return ra, st2


def test_add_fill_via_order_books_lot_leverage_step_and_partial(bot):
    ra, st2 = fill_setup(bot)
    L = bot.local
    pos, resync = bot.resting_reconcile(st2, bot.chain_position(st2), st2["oracle"]["price"])
    assert resync and L.rest_add is None and L.n_entries == 8 and L.last_leverage == 20.0 and L.tp_hits == 0
    lot = L.lots[-1]
    assert lot["size"] == ra["size_usd"] and lot["via"] == "limit" and lot["partial_due"] and lot["rung"] == 7
    assert lot["price"] == pytest.approx(ra["trigger"], abs=1e-6) and L.last_size == pytest.approx(st2["positions"]["long"]["sizeUsd"])
    # TP3: the target is between the avg and the furthest add; only adds netting > 0 after fees at it are covered
    bot.calls.clear()
    plan = bot.partial_plan(pos)
    far = min(l["price"] for l in L.lots if not l.get("first"))
    f = R.zone_f(len(L.lots) - 1, bot.cfg.dca.ladder_max_rungs, bot.cfg.resting.partial_zone_f_first, bot.cfg.resting.partial_zone_f_deep)
    assert plan["rung"] == len(L.lots) - 1 and plan["far"] == far and plan["px"] == round(pos.avg_entry + (far - pos.avg_entry) * f, 2)
    assert far < plan["px"] < pos.avg_entry and plan["lots"] and plan["size"] <= sum(l["size"] for l in L.lots)
    bot.partial_sync(pos, 105.6)   # target beyond market 105.6 (price below target for a long): placed, one order
    dec = [a for n, a in bot.calls if n == "decrease"]
    assert len(dec) == 1 and dec[0]["sizeUsd"] == plan["size"] and dec[0]["triggerPriceUsd"] == plan["px"]
    assert len(L.partials) == 1 and L.partials[0]["lots"] == plan["lots"]


def test_tp3_covers_only_net_positive_adds_and_resizes_on_new_add_and_shrink(monkeypatch, tmp_path):
    bot = mkbot(monkeypatch, tmp_path, [*ON, "dca.ladder_max_rungs=7"])
    L = bot.local
    pos = bot.chain_position(state())
    plan = bot.partial_plan(pos)
    adds = [l for l in L.lots if not l.get("first")]
    c, t = bot.cfg.costs, bot.cfg.trim
    ok = [l for l in adds if R.lot_net_at(LONG, l["price"], l["size"], plan["px"], c.open_fee, c.close_fee, t.trim_network_usd) > 0]
    assert plan["lots"] == [l["time"] for l in ok] and 0 < len(ok) < len(adds)   # shallow adds do not net at the target
    assert plan["size"] == round(sum(l["size"] for l in ok), 2)
    bot.partial_sync(pos, 105.5)
    dec = [a for n, a in bot.calls if n == "decrease"]
    assert len(dec) == 1 and dec[0]["sizeUsd"] == plan["size"] and len(L.partials) == 1
    old = L.partials[0]["pubkey"]
    bot.calls.clear()
    bot.partial_sync(pos, 105.5)   # unchanged plan: nothing cancelled / placed
    assert not bot.calls
    new = {"time": str(NOW), "price": 105.0, "size": 90.0, "coll": 4.5, "lev": 20.0, "first": False, "rung": 7}
    L.lots.append(new)   # new deeper add: avg, furthest add, rung change -> cancel + one new decrease
    bot.chain = lambda: state()
    pos2 = bot.chain_position(state(size=SIZE + 90, avg=AVG - 0.2))
    bot.partial_sync(pos2, 105.2)
    assert [a["request"] for n, a in bot.calls if n == "cancel"] == [old]
    dec = [a for n, a in bot.calls if n == "decrease"]
    p2 = bot.partial_plan(pos2)
    assert len(dec) == 1 and dec[0]["sizeUsd"] == p2["size"] and p2["rung"] == len(adds) + 1 and p2["far"] == 105.0
    # position shrank below the TP3 size: resized to the position
    bot.calls.clear()
    small = bot.chain_position(state(size=p2["size"] - 20.0, avg=AVG - 0.2))
    bot.partial_sync(small, 105.2)
    assert [a["sizeUsd"] for n, a in bot.calls if n == "decrease"] == [round(p2["size"] - 20.0, 2)]


def test_no_qualifying_add_no_tp3_and_old_one_cancelled(bot):
    L = bot.local
    L.lots = [l for l in L.lots if l.get("first")]   # no adds
    pos = bot.chain_position(state())
    assert bot.partial_plan(pos) is None
    bot.partial_sync(pos, 105.0)
    assert not bot.calls and not L.partials
    L.partials = [{"pubkey": "PX", "lots": ["t"], "lot_time": "t", "size": 10.0, "px": 107.0}]
    bot.chain = lambda: state()
    bot.partial_sync(pos, 105.0)
    assert [a["request"] for n, a in bot.calls if n == "cancel"] == ["PX"] and not L.partials


def test_add_order_gone_without_fill_drops_record(bot):
    ra, _ = fill_setup(bot)
    st = state()   # request gone, size unchanged
    bot.local.rest_add["placed"] = str(NOW - pd.Timedelta(minutes=5))
    bot.resting_reconcile(st, bot.chain_position(st), 106.6)
    assert bot.local.rest_add is None and bot.local.n_entries == 7


def test_partial_fill_records_last_partial_and_not_a_tp_hit(bot):
    L = bot.local
    lot = L.lots[-1]
    L.partials = [{"pubkey": "PX", "lots": [lot["time"]], "lot_time": lot["time"], "size": lot["size"], "px": 107.16,
                   "level": 107.2, "n": 1, "rung": 6}]
    st = state(size=round(SIZE - lot["size"], 2), coll=COLL - 0.5, price=107.2,
               reqs=[req("R1", 110.33), req("R2", 110.63)])
    pos, _ = bot.resting_reconcile(st, bot.chain_position(st), 107.2)
    assert L.last_partial_px == 107.16 and L.partials == [] and len(L.lots) == 6 and L.tp_hits == 0
    assert L.last_size == pytest.approx(SIZE - lot["size"]) and L.last_entry == pytest.approx(107.516917)
    # rule 3: next add is planned beyond the last partial price (rungs from min(last fill, last partial))
    plan, _ = bot.rest_add_plan(pos, ACC.cash)
    assert plan is not None and plan.trigger < 107.16


def test_rule3_blocks_a_rung_between_price_and_last_partial(bot, monkeypatch):
    bot.local.last_partial_px = 104.0
    pos = bot.chain_position(state())
    fake = types.SimpleNamespace(trigger=104.5, margin=5.0, leverage=20.0)
    monkeypatch.setattr(bot.strategy, "plan_next_add", lambda *a, **k: fake)
    plan, why = bot.rest_add_plan(pos, ACC.cash)
    assert plan is None and "not beyond the last partial price" in why


def test_tp_resync_leaves_partials_and_resting_add_alone(bot):
    bot.local.partials = [{"pubkey": "PX", "lot_time": "t", "size": 10, "px": 107.16}]
    st = state(reqs=[req("R1", 110.33), req("R2", 110.63), req("PX", 107.16), req("LIM1", 104.0, "increase")])
    bot.chain = lambda: st
    bot.sync_tp_ladder(bot.chain_position(st))
    assert [a["request"] for n, a in bot.calls if n == "cancel"] == ["R1", "R2"]
    assert not [1 for n, _ in bot.calls if n == "cancel-limit-order"]


def test_close_cancels_resting_add_via_api_and_partials(bot):
    bot.local.rest_add = {"pubkey": "LIM1", "trigger": 104.0, "size_usd": 90, "nat": 0.04}
    bot.local.partials = [{"pubkey": "PX", "lot_time": "t", "size": 10, "px": 107.16}]
    st = state(reqs=[req("R1", 110.33), req("PX", 107.16), req("LIM1", 104.0, "increase")])
    bot.chain = lambda: st
    assert bot.cancel_all(st)
    assert [a["request"] for n, a in bot.calls if n == "cancel-limit-order"] == ["LIM1"]
    assert [a["request"] for n, a in bot.calls if n == "cancel"] == ["R1", "PX"]
    assert bot.local.rest_add is None


def test_bank_debit_at_placement_refund_on_cancel_no_double_debit_on_fill(bot):
    from live import bank as bankmod
    b = {"coin": "SOL", "bal": 1.0, "hwm": 1.0, "wd_mark": 1.0, "totals": {}, "trade_dep": 0.0}
    bot.local.bank = b
    object.__setattr__(bot.cfg.ledger, "enabled", True)
    st = state()
    bot.resting_tick(bot.chain_position(st), st, 106.6, ACC, 100.0)
    nat = bot.local.rest_add["nat"]
    assert b["bal"] == pytest.approx(1.0 - nat) and b["trade_dep"] == pytest.approx(nat)
    st2 = state(reqs=[req("LIM1", 104.0, "increase")])
    bot.chain = lambda: st2
    assert bot.cancel_limit("LIM1") and bot.local.rest_add is None
    assert b["bal"] == pytest.approx(1.0 + bankmod.REQ_RENT) and b["trade_dep"] == pytest.approx(0.0)   # + request rent (SOL)
    assert bankmod  # (module used for the ledger maths)
    # fill: the placement debit stands, nothing debited again
    bot.local.rest_last_avg = AVG
    bot.resting_tick(bot.chain_position(st), st, 106.6, ACC, 100.0)
    ra = dict(bot.local.rest_add)
    bal = b["bal"]
    new_avg = (SIZE + ra["size_usd"]) / (SIZE / AVG + ra["size_usd"] / ra["trigger"])
    st3 = state(size=SIZE + ra["size_usd"], avg=new_avg, price=ra["trigger"])
    bot.resting_reconcile(st3, bot.chain_position(st3), ra["trigger"])
    assert b["bal"] == pytest.approx(bal) and bot.local.n_entries == 8


def test_market_add_marks_partial_due_only_when_on(offbot, bot):
    from strategy import Action, Intent
    for b, want in ((offbot, False), (bot, True)):
        b.local.lots = lots()[:-1]
        b.local.n_entries, b.local.last_size = 6, 502.95
        before = state(size=502.95, coll=40.6)
        before["positions"]["long"]["price"] = 110.437
        grown = state(size=581.0)
        b.chain = lambda g=grown: g
        b.open_or_add(Intent(action=Action.ADD, side=LONG, price=106.6, margin=4.11, leverage=19.0),
                      b.chain_position(before), before, 100.0)
        assert bool(b.local.lots[-1].get("partial_due")) is want


def test_max_one_resting_add_order(bot):
    st = state()
    pos = bot.chain_position(st)
    for _ in range(3):   # tracked order (even before it shows on-chain) -> never a second one
        bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    assert len([1 for n, _ in bot.calls if n == "create-limit-order"]) == 1


def test_equity_includes_resting_add_collateral_not_double_counted(bot):
    b = {"coin": "SOL", "bal": 1.0, "hwm": 1.0, "wd_mark": 1.0, "totals": {}, "trade_dep": 0.0}
    bot.local.bank = b
    object.__setattr__(bot.cfg.ledger, "enabled", True)
    st = state()
    pos = bot.chain_position(st)
    base = bot.hwm_equity(pos, st, 106.6)
    bot.resting_tick(pos, st, 106.6, ACC, 100.0)
    nat = bot.local.rest_add["nat"]
    assert b["bal"] == pytest.approx(1.0 - nat)
    assert bot.hwm_equity(pos, st, 106.6) == pytest.approx(base)   # debit offset by the resting collateral
    bot.local.rest_add["debited"] = False
    assert bot.hwm_equity(pos, st, 106.6) == pytest.approx(base - nat)   # not debited -> not added
    bot.local.rest_add["debited"] = True
    st2 = state(reqs=[req("LIM1", 104.0, "increase")])
    bot.chain = lambda: st2
    assert bot.cancel_limit("LIM1")   # refund returns to bal, record cleared
    assert bot.hwm_equity(pos, st, 106.6) == pytest.approx(base + 0.0037236)   # + request rent refunded to the SOL bank


def test_sol_cancel_refund_credits_request_rent_too_eth_does_not(bot):
    from live import bank as bankmod
    b = {"coin": "SOL", "asset": "SOL", "bal": 1.0, "hwm": 1.0, "wd_mark": 1.0, "totals": {}, "trade_dep": 0.0, "hist": []}
    bot.local.bank = b
    object.__setattr__(bot.cfg.ledger, "enabled", True)
    st = state()
    bot.resting_tick(bot.chain_position(st), st, 106.6, ACC, 100.0)
    nat = bot.local.rest_add["nat"]
    bot.chain = lambda: state(reqs=[req("LIM1", 104.0, "increase")])
    assert bot.cancel_limit("LIM1")
    assert b["bal"] == pytest.approx(1.0 + bankmod.REQ_RENT) and b["trade_dep"] == pytest.approx(0.0)
    assert nat > 0
    # ETH / BTC banks are own-coin: the SOL rent is not theirs
    ra = {"debited": True, "nat": 0.5}
    b2 = {"coin": "ETH", "bal": 1.0, "hwm": 1.0, "wd_mark": 1.0, "totals": {}, "trade_dep": 0.5, "hist": []}
    bot.local.bank = b2
    bot._rest_bank_refund(ra, "x")
    assert b2["bal"] == pytest.approx(1.5)


# ------------------------------------------------------------------ 2026-10-15 re-arm: resting add returns to a TP3-closed lot's entry
def rearm_setup(bot):
    st = state()
    bot.resting_tick(bot.chain_position(st), st, 106.6, ACC, 100.0)   # the resting add (LIM1) sits below
    L = bot.local
    lot = L.lots[-1]
    L.partials = [{"pubkey": "PX", "lots": [lot["time"]], "lot_time": lot["time"], "size": lot["size"], "px": 106.8,
                   "level": 106.8, "n": 1, "rung": 6}]
    st2 = state(size=round(SIZE - lot["size"], 2), coll=COLL - 0.5, price=106.7,
                reqs=[req("R1", 110.33), req("LIM1", L.rest_add["trigger"], "increase")])
    pos, _ = bot.resting_reconcile(st2, bot.chain_position(st2), 106.7)   # TP3 closed the add 0.36 (< 0.5 ATR) above its entry
    return st2, pos, lot


def test_tp3_closing_an_add_moves_the_resting_add_to_the_lots_entry(bot):
    st2, pos, lot = rearm_setup(bot)
    L = bot.local
    assert L.rearm == {"px": lot["price"], "lev": lot["lev"], "margin": round(lot["size"] / lot["lev"], 4)}
    old = L.rest_add["trigger"]
    assert old != round(lot["price"], 2)
    bot.calls.clear()
    bot.resting_tick(pos, st2, 106.7, ACC, 100.0)
    calls = [c for c in bot.calls if c[0] != "decrease"]   # (TP3 re-plan for the shrunk position is separate)
    assert [n for n, _ in calls] == ["cancel-limit-order", "create-limit-order"] and calls[0][1] == {"request": "LIM1"}
    args = calls[1][1]
    assert args["triggerPriceUsd"] == bot.add_px(pos, lot["price"]) and args["sizeUsd"] == pytest.approx(lot["size"], abs=0.01)
    assert L.rest_add["pubkey"] == "LIM2" and L.rest_add["trigger"] == bot.add_px(pos, lot["price"]) and L.rest_add["lev"] == lot["lev"]
    bot.calls.clear()   # next tick: already there -> no new calls, still exactly one resting add
    bot.resting_tick(pos, st2, 106.7, ACC, 100.0)
    assert not bot.calls


def test_rearm_failed_cancel_places_nothing(bot):
    st2, pos, lot = rearm_setup(bot)
    bot.chain = lambda: st2   # the old add is still on-chain
    real = bot.cmd
    def cmd(n, a):
        if n == "cancel-limit-order":
            bot.calls.append((n, a))
            return {"ok": False, "error": "boom"}
        return real(n, a)
    bot.cmd = cmd
    old = dict(bot.local.rest_add)
    bot.calls.clear()
    bot.resting_tick(pos, st2, 106.7, ACC, 100.0)
    assert [n for n, _ in bot.calls if n != "decrease"] == ["cancel-limit-order"] and bot.local.rest_add == old


def test_rearm_cleared_on_fill_and_market_unfavourable_keeps_plan(bot):
    st2, pos, lot = rearm_setup(bot)
    L = bot.local
    plan, _ = bot.rest_add_plan(pos, ACC.cash, lot["price"] - 0.1)   # market below the entry: normal ladder plan
    assert plan is not None and getattr(plan, "source", "") != "rearm"
    ra = dict(L.rest_add)
    st3 = state(size=pos.size + ra["size_usd"], coll=COLL + ra["margin"], price=ra["trigger"] - 0.05,
                reqs=[req("R1", 110.33)])
    bot.resting_reconcile(st3, bot.chain_position(st3), ra["trigger"] - 0.05)
    assert L.rearm is None and L.rest_add is None
