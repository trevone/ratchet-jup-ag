"""Trevor 2026-10-12 08:46 MT: the bot never writes KILL; reconcile next tick (TP retry, adopt unknown position)."""
from tests.test_resting import ON, mkbot, state


def test_halt_never_writes_kill(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.halt("x")
    assert not b.kill.exists() and b.local.tp_retry


def test_tp_placement_failure_no_kill_and_flagged(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(reqs=[])
    b.chain = lambda: st
    b.place_tp = lambda *a: False
    b.sync_tp_ladder(b.chain_position(st))
    assert not b.kill.exists() and b.local.tp_retry


def test_adopt_unknown_position(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    from live.bot import Local
    b.local = Local()
    st = state(reqs=[])
    b.chain = lambda: st
    synced = []
    b.sync_tp_ladder = lambda p: synced.append(p)
    pos = b.chain_position(st)
    b.adopt_position(st, pos)
    L = b.local
    assert not b.kill.exists() and L.side == pos.side and len(L.lots) == 1
    assert L.lots[0]["size"] == round(pos.size, 2) and L.lots[0]["price"] == round(pos.avg_entry, 6) and synced
