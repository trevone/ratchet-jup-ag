"""2026-10-12 SOL lot mismatch: add filled, restart before the lot was saved. Pending marker + chain recovery. Fake chain only."""
import json

import pandas as pd
import pytest

import live.bot as B
from strategy import LONG, Action, Intent
from tests.test_resting import ON, SIZE, COLL, AVG, mkbot, state, fill_setup

NOW = pd.Timestamp.now(tz="UTC")


def ev(size=24.0, px=105.9, ago_s=60, coll=2.996787, fee=0.016822, sig="KEEPER1"):
    return {"sig": sig, "blockTime": int((NOW - pd.Timedelta(seconds=ago_s)).timestamp()), "event": "IncreasePositionEvent",
            "price": px, "sizeUsdDelta": size, "collateralUsdDelta": coll, "feeUsd": fee, "positionSizeUsd": SIZE + size}


@pytest.fixture
def off(monkeypatch, tmp_path):
    return mkbot(monkeypatch, tmp_path, [])


def fake_jup(monkeypatch, events, calls):
    def j(cmd, args=None):
        calls.append((cmd, args))
        assert cmd == "position-increases"
        return {"ok": True, "increases": events}
    monkeypatch.setattr(B, "jup", j)


def saved(b):
    return json.loads(b.state_path.read_text())


def test_missing_add_rebuilt_from_chain_and_saved(off, monkeypatch):
    calls = []
    fake_jup(monkeypatch, [ev()], calls)
    st = state(size=SIZE + 24.0, coll=COLL + 2.98)
    pos = off.chain_position(st)
    L = off.local
    assert off.recover_missing_add(st, pos, 106.0)
    lot = L.lots[-1]
    assert sum(l["size"] for l in L.lots) == pytest.approx(pos.size)
    assert (lot["price"], lot["size"], lot["lev"], lot["recovered"]) == (105.9, 24.0, 8.0, "KEEPER1")
    assert lot["coll"] == pytest.approx(2.98, abs=1e-3) and "partial_due" not in lot   # [resting] flags off
    assert L.n_entries == 8 and L.last_leverage == 8.0 and L.last_size == pos.size and L.tp_hits == 0
    assert len(saved(off)["lots"]) == 8   # persisted at once
    assert calls[0][1]["position"] == "P"


def test_pending_marker_gives_leverage_sig_and_last_entry(off, monkeypatch):
    fake_jup(monkeypatch, [ev()], [])
    off.local.add_pending = {"time": str(NOW), "size": 24.0, "lev": 20.0, "margin": 1.2, "price": 106.05, "size0": SIZE, "sig": "REQ"}
    st = state(size=SIZE + 24.0)
    assert off.recover_missing_add(st, off.chain_position(st), 106.0)
    L = off.local
    assert (L.lots[-1]["lev"], L.lots[-1]["sig"], L.last_entry, L.add_pending) == (20.0, "REQ", 106.05, None)


def test_no_exact_single_fill_leaves_lots_and_queries_once(off, monkeypatch):
    calls = []
    fake_jup(monkeypatch, [ev(size=20.0), ev(size=4.0, sig="K2")], calls)
    st = state(size=SIZE + 24.0)
    before = [dict(l) for l in off.local.lots]
    assert not off.recover_missing_add(st, off.chain_position(st), 106.0)
    assert not off.recover_missing_add(st, off.chain_position(st), 106.0)
    assert off.local.lots == before and len(calls) == 1


def test_fill_that_already_has_a_lot_is_not_reused(off, monkeypatch):
    t_last = pd.Timestamp(off.local.lots[-1]["time"])
    e = ev(ago_s=int((NOW - t_last).total_seconds()) - 30)
    fake_jup(monkeypatch, [e], [])
    st = state(size=SIZE + 24.0)
    assert not off.recover_missing_add(st, off.chain_position(st), 106.0)


def test_no_gap_no_chain_lookup(off, monkeypatch):
    calls = []
    fake_jup(monkeypatch, [ev()], calls)
    st = state()
    assert not off.recover_missing_add(st, off.chain_position(st), 106.0) and not calls


def test_add_pending_persisted_before_send_and_cleared_after_lot(off):
    seen = {}
    real = off.cmd

    def cmd(name, args):
        if name == "increase":
            seen["pending"] = saved(off).get("add_pending")
        return real(name, args)
    off.cmd = cmd
    before = state(size=SIZE, coll=COLL)
    grown = state(size=SIZE + 78.0)
    off.chain = lambda: grown
    off.open_or_add(Intent(action=Action.ADD, side=LONG, price=106.6, margin=4.11, leverage=19.0),
                    off.chain_position(before), before, 100.0)
    assert seen["pending"]["size"] == round(4.11 * 19, 2) and seen["pending"]["lev"] == 19.0
    assert off.local.add_pending is None and saved(off)["add_pending"] is None and len(off.local.lots) == 8


def test_add_pending_cleared_when_never_filled(off):
    before = state(size=SIZE, coll=COLL)
    off.chain = lambda: before
    off.confirm_tries = 1
    off.open_or_add(Intent(action=Action.ADD, side=LONG, price=106.6, margin=4.11, leverage=19.0),
                    off.chain_position(before), before, 100.0)
    assert off.local.add_pending is not None and len(off.local.lots) == 7


@pytest.fixture
def on(monkeypatch, tmp_path):
    return mkbot(monkeypatch, tmp_path, ON)


def test_resting_fill_saved_immediately_and_not_double_booked(on, monkeypatch):
    calls = []
    fake_jup(monkeypatch, [ev()], calls)
    ra, st2 = fill_setup(on)
    pos2 = on.chain_position(st2)
    # before resting_reconcile books it, the generic recovery leaves a matching resting fill alone
    assert not on.recover_missing_add(st2, pos2, 106.0) and not calls
    saves = []
    real_save = on.save
    on.save = lambda: (saves.append(len(on.local.lots)), real_save())
    pos, resync = on.resting_reconcile(st2, pos2, st2["oracle"]["price"])
    assert resync and saves and saves[0] == 8 and len(saved(on)["lots"]) == 8
    assert not on.recover_missing_add(st2, pos, 106.0)   # lots now match: nothing more booked
    assert len(on.local.lots) == 8
