"""Trevor 2026-10-28 14:15 MT (SOL 14:01 MT): a resting add fill whose size growth shows up BEFORE its order record
disappears is booked as that add fill (new lot, avg, n_entries, leverage, fresh TP1/TP2) at once; the record is kept
flagged booked (never booked twice, never refunded, not in hwm_equity's resting-add term, not re-planned) and removed
quietly when it disappears. hwm_equity never counts an add already in the position size twice, so the profit guard does
not fire on that inflated equity. Normal ordering unchanged. Fake chain only."""
import logging

import pandas as pd
import pytest

import live.bot as B
from strategy import SHORT
from tests.test_resting import ON, mkbot

NOW = pd.Timestamp.now(tz="UTC")
T1, T2, T3 = (str(NOW - pd.Timedelta(hours=h)) for h in (3, 2, 1))
LOTS = [(T1, 101.03441, 50.0, True), (T2, 101.93, 75.0, False), (T3, 102.519998, 100.0, False)]
AVG = 225.0 / sum(s / p for _, p, s, _ in LOTS)
TRIG, ADD, NAT, MARGIN, LEV = 103.3, 125.0, 0.097589897, 12.5, 10.0
AVG2 = 350.0 / (225.0 / AVG + ADD / TRIG)
COLL1, COLL2 = 25.37868, 37.87868
TP3_PX, TP_PX = 102.17, 101.42


def req(pk, px, change="decrease"):
    return {"pubkey": pk, "triggerPrice": px, "executed": False, "requestType": "trigger", "requestChange": change}


LADDER = [req("T1", TP_PX), req("T2", TP_PX), req("TP3", TP3_PX)]
LIM = req("LIM9", TRIG, "increase")


def st_short(size, coll, avg, price, reqs):
    short = {"pubkey": "P", "sizeUsd": size, "collateralUsd": coll, "price": avg, "realisedPnlUsd": 0.0} if size else {}
    return {"openRequests": reqs, "oracle": {"price": price, "ageSec": 1}, "solOracle": {"price": price},
            "positions": {"long": {}, "short": short}, "wallet": {"sol": 1.2}}


BEFORE = st_short(225.0, COLL1, AVG, 103.0, [*LADDER, LIM])
GROWN_LISTED = st_short(350.0, COLL2, AVG2, 103.31, [*LADDER, LIM])   # SOL 14:01: filled on chain, record still listed
GROWN_GONE = st_short(350.0, COLL2, AVG2, 103.31, list(LADDER))


@pytest.fixture
def b(monkeypatch, tmp_path):
    """SOL-like short: $50 first entry 101.03 + $75 add 101.93 + $100 add 102.52, resting add $125 @ 103.3 (debited)."""
    b = mkbot(monkeypatch, tmp_path, ON)
    L = b.local
    L.side, L.opened, L.n_entries, L.first_margin, L.last_leverage = SHORT, T1, 3, 10.0, 9.0
    L.first_entry, L.last_entry, L.last_size, L.trade_opened_usd = LOTS[0][1], LOTS[2][1], 225.0, 225.0
    L.lots = [{"time": t, "price": p, "size": s, "coll": s / 10, "lev": 10, "first": f} for t, p, s, f in LOTS]
    L.partials = [{"pubkey": "TP3", "lots": [T3], "lot_time": T3, "size": 100.0, "px": TP3_PX, "level": None, "n": 0,
                   "rung": 2, "entry": LOTS[2][1]}]
    L.tp_px, L.tp_hits = [TP_PX, TP_PX], 0
    L.rest_last_avg = AVG
    L.rest_add = {"pubkey": "LIM9", "side": "short", "trigger": TRIG, "size_usd": ADD, "margin": MARGIN, "lev": LEV,
                  "nat": NAT, "sol": NAT, "debited": True, "placed": str(NOW - pd.Timedelta(minutes=30)), "sig": "SL"}
    b._sol_px = 103.0
    return b


class _Stop(Exception):
    pass


def run_tick(b, st, monkeypatch):
    """bot.tick() through chain reconcile + TP fill detection + the fresh-TP re-sync, stopped at the budget step."""
    b.chain = lambda: st
    b.sweep = lambda *a, **k: None
    b.recover_missing_add = lambda *a, **k: False
    b.synced = getattr(b, "synced", [])
    b.sync_tp_ladder = lambda pos: b.synced.append(pos.size)
    b.partial_sync = lambda pos, price: None
    b.resize_tps = lambda pos: None

    def stop(*a, **k):
        raise _Stop
    monkeypatch.setattr(B, "budget_from_state", stop)
    with pytest.raises(_Stop):
        b.tick()


def bank_on(b, monkeypatch):
    b.local.bank = {"coin": "SOL", "bal": 1.0, "hwm": 1.0, "wd_mark": 1.0, "totals": {}, "trade_dep": 0.0}
    object.__setattr__(b.cfg.ledger, "enabled", True)
    monkeypatch.setattr(b, "sol_fees_unbooked", lambda pos: 0.0)


def booked_once(b):
    L = b.local
    assert [l["price"] for l in L.lots[:3]] == [p for _, p, _, _ in LOTS] and len(L.lots) == 4
    lot = L.lots[-1]
    assert lot["size"] == ADD and lot["via"] == "limit" and lot["req"] == "LIM9" and lot["price"] == pytest.approx(TRIG, abs=0.01)
    assert L.n_entries == 4 and L.last_size == 350.0 and L.last_leverage == LEV and L.tp_hits == 0
    assert L.trade_opened_usd == 350.0 and L.guard_armed


def test_sol_sequence_size_growth_first_then_record_gone(b, monkeypatch, caplog):
    L = b.local
    caplog.set_level(logging.INFO)
    run_tick(b, GROWN_LISTED, monkeypatch)   # size 225 -> 350, LIM9 still listed
    booked_once(b)
    assert L.rest_add["booked"] and L.rest_add["pubkey"] == "LIM9" and b.synced == [350.0]
    assert "size growth before its order record disappeared" in caplog.text
    caplog.clear()
    run_tick(b, GROWN_GONE, monkeypatch)   # the stale record disappears: removed quietly
    booked_once(b)
    assert L.rest_add is None and b.synced == [350.0]
    assert "gone without a fill" not in caplog.text and "RESTING ADD FILLED" not in caplog.text


def test_normal_ordering_record_gone_with_the_size_growth(b, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    run_tick(b, GROWN_GONE, monkeypatch)
    booked_once(b)
    assert b.local.rest_add is None and b.synced == [350.0]
    assert "RESTING ADD FILLED:" in caplog.text and "size growth before" not in caplog.text
    run_tick(b, GROWN_GONE, monkeypatch)
    booked_once(b)


def test_size_growth_first_books_once_while_record_lingers_no_refund_no_replan(b, monkeypatch, caplog):
    bank_on(b, monkeypatch)
    bal0 = b.local.bank["bal"]
    for _ in range(3):
        run_tick(b, GROWN_LISTED, monkeypatch)
    booked_once(b)
    assert b.synced == [350.0] and b.local.bank["bal"] == pytest.approx(bal0)
    b.calls.clear()   # while the booked record lingers: no new add, no re-plan / re-arm cancel
    b.resting_tick(b.chain_position(GROWN_LISTED), GROWN_LISTED, 103.31, type("A", (), {"cash": 60.0})(), 100.0)
    assert not [n for n, _ in b.calls if n in ("create-limit-order", "cancel-limit-order", "cancel")]
    caplog.set_level(logging.INFO)
    run_tick(b, GROWN_GONE, monkeypatch)
    booked_once(b)
    assert b.local.rest_add is None and b.local.bank["bal"] == pytest.approx(bal0)   # never refunded
    assert "refund" not in caplog.text


def test_guard_does_not_fire_on_the_double_counted_add(b, monkeypatch):
    bank_on(b, monkeypatch)
    L = b.local
    pos = b.chain_position(GROWN_LISTED)
    ra = L.rest_add
    L.rest_add = None
    true_eq = b.hwm_equity(pos, GROWN_LISTED, 103.31)   # the add's collateral is in the close value only
    L.rest_add = ra
    # target between the true equity and the inflated one (true + the add's nat, the 14:01 MT false trigger)
    L.bank["trade_start_equity"] = (true_eq + NAT / 2) / (1 + b.cfg.profit_guard.min_profit_pct_trade)
    # before the reconcile books it (size grown, record listed): never counted twice
    assert b.hwm_equity(pos, GROWN_LISTED, 103.31) == pytest.approx(true_eq)
    assert not b.guard_check(pos, GROWN_LISTED, 103.31)[0]
    pos, resync = b.resting_reconcile(GROWN_LISTED, pos, 103.31)   # booked, record flagged
    assert resync and L.rest_add["booked"]
    assert b.hwm_equity(pos, GROWN_LISTED, 103.31) == pytest.approx(true_eq)
    assert not b.guard_check(pos, GROWN_LISTED, 103.31)[0]
    # a resting add NOT filled yet still counts its collateral (unchanged)
    fresh = {k: v for k, v in ra.items() if k != "booked"}
    L.last_size, L.lots = 225.0, L.lots[:3]
    pos0 = b.chain_position(BEFORE)
    L.rest_add = None
    no_rest = b.hwm_equity(pos0, BEFORE, 103.0)
    L.rest_add = fresh
    assert b.hwm_equity(pos0, BEFORE, 103.0) == pytest.approx(no_rest + NAT)


def test_normal_add_path_unchanged(b, monkeypatch, caplog):
    L = b.local
    caplog.set_level(logging.INFO)
    run_tick(b, BEFORE, monkeypatch)   # resting, not filled: nothing booked
    assert len(L.lots) == 3 and L.rest_add and not L.rest_add.get("booked") and L.last_size == 225.0
    # growth that is not this add (a different size, market far from the trigger): not booked as it
    other = st_short(265.0, COLL1 + 4, AVG, 102.0, [*LADDER, LIM])
    pos = b.chain_position(other)
    b.resting_reconcile(other, pos, 102.0)
    assert len(L.lots) == 3 and not L.rest_add.get("booked") and L.last_size == 225.0
    # the record gone without growth: unfilled -> dropped (refund path), as before
    L.rest_add["placed"] = str(NOW - pd.Timedelta(minutes=5))
    b.resting_reconcile(st_short(225.0, COLL1, AVG, 103.0, list(LADDER)), b.chain_position(BEFORE), 103.0)
    assert L.rest_add is None and len(L.lots) == 3 and "gone without a fill" in caplog.text
