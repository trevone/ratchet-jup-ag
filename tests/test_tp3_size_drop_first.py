"""Trevor 2026-10-28 (BTC 12:46 MT): a TP3 (per-add partial) fill whose size drop shows up BEFORE its order record
disappears is booked as that TP3 partial (add lot removed, first entry kept, TP1/TP2 resized, tp_hits unchanged); the stale
record disappearing later drops nothing, books nothing and re-places no TP3. Normal ordering unchanged. Fake chain only."""
import logging

import pandas as pd
import pytest

import live.bot as B
from strategy import SHORT
from tests.test_resting import ON, mkbot

NOW = pd.Timestamp.now(tz="UTC")
FIRST_T, ADD_T = str(NOW - pd.Timedelta(hours=2)), str(NOW - pd.Timedelta(minutes=10))
FIRST_PX, ADD_PX = 80819.18, 81187.6109
AVG = 125.0 / (50.0 / FIRST_PX + 75.0 / ADD_PX)
TP3_PX, TP1_PX, TP2_PX = 80906.16, 80521.18, 80300.0


def req(pk, px, change="decrease"):
    return {"pubkey": pk, "triggerPrice": px, "executed": False, "requestType": "trigger", "requestChange": change}


def st_short(size, coll, price, reqs):
    short = {"pubkey": "P", "sizeUsd": size, "collateralUsd": coll, "price": AVG, "realisedPnlUsd": 0.0} if size else {}
    return {"openRequests": reqs, "oracle": {"price": price, "ageSec": 1}, "solOracle": {"price": 101.8},
            "positions": {"long": {}, "short": short}, "wallet": {"sol": 1.2}}


@pytest.fixture
def b(monkeypatch, tmp_path):
    """BTC-like short: $50 first entry 80819.18 + $75 add 81187.61, TP3 $75 at 80906.16 resting, TP1/TP2 below."""
    b = mkbot(monkeypatch, tmp_path, ON)
    L = b.local
    L.side, L.opened, L.n_entries, L.first_margin, L.last_leverage = SHORT, FIRST_T, 2, 10.0, 7.5
    L.first_entry, L.last_entry, L.last_size, L.trade_opened_usd = FIRST_PX, ADD_PX, 125.0, 125.0
    L.lots = [{"time": FIRST_T, "price": FIRST_PX, "size": 50.0, "coll": 10.0, "lev": 5, "first": True},
              {"time": ADD_T, "price": ADD_PX, "size": 75.0, "coll": 10.0, "lev": 7.5, "first": False, "via": "limit"}]
    L.partials = [{"pubkey": "TP3", "lots": [ADD_T], "lot_time": ADD_T, "size": 75.0, "px": TP3_PX, "level": None, "n": 0,
                   "rung": 1, "entry": ADD_PX}]
    L.tp_px, L.tp_hits = [TP1_PX, TP2_PX], 0
    b._sol_px = 101.8
    return b


LADDER = [req("T1", TP1_PX), req("T2", TP2_PX)]


class _Stop(Exception):
    pass


def run_tick(b, st, monkeypatch):
    """bot.tick() through chain reconcile + TP fill detection + TP resize, stopped right after (at the budget step)."""
    b.chain = lambda: st
    b.sweep = lambda *a, **k: None
    b.recover_missing_add = lambda *a, **k: False
    b.resized = getattr(b, "resized", [])
    b.resize_tps = lambda pos: b.resized.append(pos.size)
    b.sync_tp_ladder = lambda pos: (_ for _ in ()).throw(AssertionError("no fresh TP ladder expected"))

    def stop(*a, **k):
        raise _Stop
    monkeypatch.setattr(B, "budget_from_state", stop)
    with pytest.raises(_Stop):
        b.tick()


def decreases(b):
    return [a for n, a in b.calls if n == "decrease"]


def test_btc_sequence_size_drop_first_then_record_gone(b, monkeypatch, caplog):
    L = b.local
    caplog.set_level(logging.INFO)
    # tick 1 (12:46:24): size 125 -> 50, TP3 request account still listed
    run_tick(b, st_short(50.0, 10.0, 80902.41, [*LADDER, req("TP3", TP3_PX)]), monkeypatch)
    assert L.tp_hits == 0 and L.last_size == 50.0
    assert [l["time"] for l in L.lots] == [FIRST_T] and L.lots[0]["first"] and L.lots[0]["price"] == FIRST_PX
    assert L.n_entries == 1 and L.last_entry == FIRST_PX
    assert len(L.partials_done) == 1 and L.partials_done[0]["pubkey"] == "TP3" and L.last_partial_px == TP3_PX
    assert len(L.partials) == 1 and L.partials[0]["booked"]   # stale record kept (flagged) until it disappears
    assert b.resized == [50.0]   # TP1/TP2 resized to the remaining position, as after a normal TP3 fill
    assert L.guard_armed and L.rearm and L.rearm["lev"] == 7.5
    assert "TP fill detected" not in caplog.text
    pos = b.chain_position(st_short(50.0, 10.0, 80902.41, [*LADDER, req("TP3", TP3_PX)]))
    b.calls.clear()
    b.partial_sync(pos, 80902.41)   # stale record on-chain: nothing cancelled / placed
    assert not b.calls
    # tick 2 (12:47:03): the stale record disappears -> removed quietly, nothing re-booked, no TP3 re-placed
    caplog.clear()
    run_tick(b, st_short(50.0, 10.0, 80903.0, list(LADDER)), monkeypatch)
    assert "records dropped" not in caplog.text and "TP fill detected" not in caplog.text
    assert L.partials == [] and len(L.partials_done) == 1 and L.tp_hits == 0 and L.last_size == 50.0
    assert [l["time"] for l in L.lots] == [FIRST_T] and b.resized == [50.0]
    b.calls.clear()
    b.partial_sync(b.chain_position(st_short(50.0, 10.0, 80903.0, list(LADDER))), 80903.0)
    assert not decreases(b)   # no TP3 on the first entry
    assert b.partial_plan(pos) is None


def test_normal_ordering_record_gone_with_the_size_drop(b, monkeypatch, caplog):
    L = b.local
    caplog.set_level(logging.INFO)
    run_tick(b, st_short(50.0, 10.0, 80902.41, list(LADDER)), monkeypatch)
    assert L.tp_hits == 0 and L.last_size == 50.0 and L.partials == [] and len(L.partials_done) == 1
    assert [l["time"] for l in L.lots] == [FIRST_T] and b.resized == [50.0]
    assert "TP fill detected" not in caplog.text and "records dropped" not in caplog.text
    caplog.clear()   # next tick: nothing more booked, no TP3 on the first entry
    run_tick(b, st_short(50.0, 10.0, 80903.0, list(LADDER)), monkeypatch)
    assert len(L.partials_done) == 1 and L.tp_hits == 0 and [l["time"] for l in L.lots] == [FIRST_T]
    b.calls.clear()
    b.partial_sync(b.chain_position(st_short(50.0, 10.0, 80903.0, list(LADDER))), 80903.0)
    assert not decreases(b)


def test_size_drop_first_books_once_even_if_record_lingers(b, monkeypatch):
    L = b.local
    st = st_short(50.0, 10.0, 80902.41, [*LADDER, req("TP3", TP3_PX)])
    for _ in range(3):   # record still listed for several ticks
        run_tick(b, st, monkeypatch)
    assert len(L.partials_done) == 1 and L.tp_hits == 0 and b.resized == [50.0] and len(L.partials) == 1


def test_price_match_books_tp3_when_size_differs_slightly(b, monkeypatch):
    L = b.local
    L.partials[0]["size"] = 75.0
    run_tick(b, st_short(51.0, 10.0, 80906.0, [*LADDER, req("TP3", TP3_PX)]), monkeypatch)   # closed 74 at the TP3 price
    assert L.tp_hits == 0 and [l["time"] for l in L.lots] == [FIRST_T] and L.partials[0]["booked"]


def test_ordinary_tp1_fill_still_counts_as_a_tp_hit(b, monkeypatch, caplog):
    L = b.local
    caplog.set_level(logging.INFO)
    run_tick(b, st_short(62.5, 5.0, 80515.0, [req("T2", TP2_PX), req("TP3", TP3_PX)]), monkeypatch)   # TP1 50% at 80521
    assert L.tp_hits == 1 and "TP fill detected" in caplog.text
    assert len(L.lots) == 2 and L.partials_done == [] and not L.partials[0].get("booked")
