"""Trevor 2026-10-12 08:28 MT: unconfirmed cancels never halt; retried each tick; refund booked once gone."""
from tests.test_resting import ON, mkbot, req, state


def test_unconfirmed_cancel_no_kill_then_retry_and_refund(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    ra = {"pubkey": "ADD1", "debited": True, "nat": 0.004}
    b.local.rest_add = ra
    monkeypatch.setattr(type(b), "bank_on", property(lambda s: True))
    b.local.bank = {"bal": 0.01, "coin": "ETH", "hwm": 0.02, "trade_dep": 0.0, "hist": []}
    st = state(reqs=[req("ADD1", 2569.3, "increase")])
    b.chain = lambda: st
    b.cancel_failed(st["openRequests"], None, "test")
    assert not b.kill.exists() and b.local.pending_cancels[0]["pubkey"] == "ADD1"
    b.local.rest_add = None   # position closed meanwhile (local reset)
    nopos = state(size=0, reqs=[req("ADD1", 2569.3, "increase")])
    b.calls.clear()
    assert b.retry_cancels(nopos, None)
    assert b.calls[0][0] == "cancel-limit-order" and b.local.pending_cancels
    assert b.retry_cancels(state(size=0, reqs=[]), None)
    assert b.local.pending_cancels == [] and abs(b.local.bank["bal"] - 0.014) < 1e-9 and not b.kill.exists()


def test_tp_cancel_failure_no_halt_resyncs_when_gone(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state()
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: False
    b.sync_tp_ladder(b.chain_position(st))
    assert not b.kill.exists() and len(b.local.pending_cancels) == 2
    hit = []
    b.sync_tp_ladder = lambda p: hit.append(p)
    st2 = state(reqs=[])
    b.retry_cancels(st2, b.chain_position(st2))
    assert b.local.pending_cancels == [] and len(hit) == 1
