"""A1: TP targets are computed before the old TPs are cancelled; any exception sets tp_retry (no halt).
A2: tp_retry is capped (3 consecutive failures, then back-off) and never freezes the rest of the tick."""
import pandas as pd

from tests.test_resting import ON, mkbot, state


def _boom(*a, **k):
    raise RuntimeError("boom")


def test_sync_exception_in_targets_keeps_old_tps(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(reqs=[])
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", r)) or True
    b.strategy.tp_targets = _boom
    b.sync_tp_ladder(b.chain_position(st))
    assert not [c for c in b.calls if c[0] == "cancel"] and not [c for c in b.calls if c[0] == "decrease"]
    assert b.local.tp_retry and not b.kill.exists()


def test_sync_exception_in_fee_min_keeps_old_tps(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(reqs=[])
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", r)) or True
    b.fee_min_targets = _boom
    b.sync_tp_ladder(b.chain_position(st))
    assert not [c for c in b.calls if c[0] == "cancel"] and b.local.tp_retry


def test_resize_exception_before_cancel_keeps_old_tps(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=115.50)
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", r)) or True
    b.fee_min_prices = _boom
    b.resize_tps(b.chain_position(st))
    assert not [c for c in b.calls if c[0] == "cancel"] and b.local.tp_retry and not b.kill.exists()


def test_exception_after_cancel_sets_tp_retry(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(reqs=[])
    b.chain = lambda: st
    b.place_tp = _boom
    b.sync_tp_ladder(b.chain_position(st))
    assert b.local.tp_retry and not b.kill.exists()


def test_tp_retry_caps_and_backs_off(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(reqs=[])
    pos = b.chain_position(st)
    n = []

    def fail(p):
        n.append(1)
        b.local.tp_retry = True
    b.sync_tp_ladder = fail
    b.local.tp_retry = True
    for _ in range(3):
        assert b.tp_retry_step(pos) is False   # failed: the rest of the tick continues
    assert len(n) == 3 and b.local.tp_retry_until and b.local.tp_retry_n == 0
    assert b.tp_retry_step(pos) is False and len(n) == 3   # backing off: no attempt
    b.local.tp_retry_until = str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=1))
    b.tp_retry_step(pos)
    assert len(n) == 4   # back-off over: tries again


def test_tp_retry_success_ends_tick_and_resets(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(reqs=[])
    pos = b.chain_position(st)
    b.sync_tp_ladder = lambda p: None
    b.local.tp_retry, b.local.tp_retry_n = True, 2
    assert b.tp_retry_step(pos) is True
    assert not b.local.tp_retry and b.local.tp_retry_n == 0 and b.local.tp_retry_until == ""
