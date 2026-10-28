"""Trevor 2026-10-27 14:55 MT: guard TP price rounded against itself (short floor, long ceil, price tick) and TP1/TP2
re-placed when the live guard price moves past the resting TPs (wrong side only, cooldown, existing retry paths)."""
from strategy import LONG, SHORT, TpTarget
from tests.test_resting import ON, mkbot, req, state


def gbot(monkeypatch, tmp_path, gp=107.125, mins=(100.0, 100.0)):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.gp = gp
    monkeypatch.setattr(b, "guard_tps", lambda: True)
    monkeypatch.setattr(b, "guard_price", lambda pos, st, extra=0.0: b.gp)
    monkeypatch.setattr(b, "tp_fee_mins", lambda pos, st: (list(mins), 0.0))
    monkeypatch.setattr(b, "guard_bank_stale", lambda st: False)
    b.synced = []
    monkeypatch.setattr(b, "sync_tp_ladder", lambda pos: b.synced.append(pos))
    b.local.tp_px = [107.12, 107.12]
    return b


def st_tp(px=107.12):
    return state(reqs=[req("R1", px), req("R2", px)])


def test_guard_round_against_itself(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    assert b.guard_round(SHORT, 100.2599) == 100.25 and b.guard_round(SHORT, 100.25) == 100.25
    assert b.guard_round(SHORT, 100.2500000001) == 100.25 and b.guard_round(SHORT, 80414.829) == 80414.82
    assert b.guard_round(LONG, 107.1201) == 107.13 and b.guard_round(LONG, 107.12) == 107.12
    assert b.guard_round(LONG, 2314.87000000001) == 2314.87 and b.guard_round(LONG, 2314.8700001) == 2314.88


def test_guard_tp_targets_conservative_and_fee_floor(monkeypatch, tmp_path):
    b = gbot(monkeypatch, tmp_path)
    st = st_tp()
    pos = b.chain_position(st)
    t = (TpTarget(110.0, 0.5), TpTarget(111.0, 1.0))
    assert [x.price for x in b.guard_tp_targets(pos, st, t)] == [107.13, 107.13]   # long: ceil (round() gave 107.12)
    monkeypatch.setattr(b, "tp_fee_mins", lambda pos, st: ([107.5, 107.6], 0.0))
    assert [x.price for x in b.guard_tp_targets(pos, st, t)] == [107.6, 107.6]     # stricter fee floor still wins


def test_drift_replaces_only_when_wrong_side_with_cooldown(monkeypatch, tmp_path):
    b = gbot(monkeypatch, tmp_path, gp=107.125)
    st = st_tp()
    pos = b.chain_position(st)
    assert b.guard_drift_check(pos, st) and len(b.synced) == 1      # 107.12 below the long guard TP 107.13 -> re-place
    assert not b.guard_drift_check(pos, st) and len(b.synced) == 1  # cooldown
    b._guard_drift_until = 0.0
    b.local.tp_px = [107.13, 107.13]
    assert not b.guard_drift_check(pos, st_tp(107.13))              # at the guard TP: kept, no churn


def test_drift_keeps_tp_further_out_and_skips_other_states(monkeypatch, tmp_path):
    b = gbot(monkeypatch, tmp_path, gp=107.05)
    st = st_tp()
    pos = b.chain_position(st)
    assert not b.guard_drift_check(pos, st)                          # resting 107.12 beyond the needed 107.05: kept
    b.gp = 107.2
    for attr, val in (("tp_retry", True), ("pending_cancels", [{"pubkey": "X"}])):
        old = getattr(b.local, attr)
        setattr(b.local, attr, val)
        assert not b.guard_drift_check(pos, st)
        setattr(b.local, attr, old)
    b.gp = None                                                      # guard met at market: market-close guard's job
    assert not b.guard_drift_check(pos, st)
    b.gp = 107.2
    assert not b.guard_drift_check(pos, state(reqs=[]))              # no resting TPs: tp_retry path's job
    monkeypatch.setattr(b, "guard_tps", lambda: False)
    assert not b.guard_drift_check(pos, st)                          # guard does not own the TPs
    assert b.synced == []


def test_drift_short_side(monkeypatch, tmp_path):
    """SOL 2026-10-27 14:41: TPs resting at 100.26, live guard 100.2499 -> conservative 100.24 -> re-place."""
    b = gbot(monkeypatch, tmp_path, gp=100.2499, mins=(101.0, 101.0))
    st = st_tp(100.26)
    pos = b.chain_position(st)
    pos.side = SHORT
    b.local.tp_px = [100.26, 100.26]
    assert b.guard_tp_px(pos, st, verbose=False)[0] == 100.24
    assert b.guard_drift_check(pos, st) and len(b.synced) == 1
    b._guard_drift_until, b.local.tp_px, b.gp = 0.0, [100.24, 100.24], 100.2499
    assert not b.guard_drift_check(pos, st_tp(100.24))


def test_fee_floor_move_alone_does_not_replace(monkeypatch, tmp_path):
    """BTC 2026-10-27 15:02 MT: TPs at the fee floor far beyond the guard; borrow accrual moves the floor by cents: no re-place."""
    b = gbot(monkeypatch, tmp_path, gp=80205.48, mins=(79942.65, 79950.0))
    st = st_tp(79942.73)
    pos = b.chain_position(st)
    pos.side = SHORT
    b.local.tp_px = [79942.73, 79942.73]
    assert b.guard_tp_px(pos, st, verbose=False)[0] == 79942.65
    assert not b.guard_drift_check(pos, st) and b.synced == []
