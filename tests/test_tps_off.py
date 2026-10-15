"""Trevor 2026-10-15: [profit_guard] tps_off_after_partial: after a TP3 partial / trim, TP1/TP2 are removed and not re-placed
(the profit guard is the sole exit); the resting add and TP3 partial requests are never cancelled. Default off."""
from tests.test_resting import ON, mkbot, req, state

FLAG = "profit_guard.tps_off_after_partial=true"


def _setup(monkeypatch, tmp_path, flags, partial=True):
    b = mkbot(monkeypatch, tmp_path, flags)
    st = state(size=115.50, reqs=[req("R110.33", 110.33), req("R110.63", 110.63), req("PT", 112.0), req("RA", 100.0, "increase")])
    st["positions"]["long"]["borrowFeeUsd"] = 0.0
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", [x["pubkey"] for x in r])) or True
    b.local.partials = [{"pubkey": "PT", "px": 112.0, "size": 10.0}]
    if partial:
        b.local.partials_done = [{"pubkey": "old"}]
    b.local.tp_px = [110.33, 110.63]
    return b, st


def test_default_off():
    from config import ProfitGuardConfig
    assert ProfitGuardConfig().tps_off_after_partial is False


def test_on_with_partials_resize_cancels_tps_only_and_places_nothing(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG])
    b.resize_tps(b.chain_position(st))
    assert b.calls == [("cancel", ["R110.33", "R110.63"])]   # not PT (TP3 partial), not RA (resting add)
    assert b.local.tp_px == [0.0, 0.0] and not b.local.tp_retry


def test_on_with_partials_sync_cancels_tps_only_and_places_nothing(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG])
    b.sync_tp_ladder(b.chain_position(st))
    assert b.calls == [("cancel", ["R110.33", "R110.63"])]
    assert b.local.tp_px == [0.0, 0.0]


def test_on_with_trims_only_also_counts(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG], partial=False)
    assert not b.tps_off()
    b.local.trims = [{"onchain_pnl_usd": 0.1}]
    assert b.tps_off()


def test_on_with_partials_no_tp_requests_is_noop(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG])
    st["openRequests"] = [req("PT", 112.0), req("RA", 100.0, "increase")]
    b.sync_tp_ladder(b.chain_position(st))
    assert b.calls == []


def test_on_cancel_exception_sets_tp_retry_no_halt(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG])

    def boom(s, r):
        raise RuntimeError("boom")
    b.cancel_reqs = boom
    b.resize_tps(b.chain_position(st))
    assert b.local.tp_retry and not b.kill.exists()


def test_flag_off_with_partials_unchanged(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON)
    assert not b.tps_off()
    b.resize_tps(b.chain_position(st))
    assert b.calls[0] == ("cancel", ["R110.33", "R110.63"])
    assert [a["triggerPriceUsd"] for n, a in b.calls if n == "decrease"] == [110.33, 110.63]


def test_flag_on_without_partials_places_tps_normally(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG], partial=False)
    assert not b.tps_off()
    b.resize_tps(b.chain_position(st))
    assert [a["triggerPriceUsd"] for n, a in b.calls if n == "decrease"] == [110.33, 110.63]
    assert all("RA" not in c[1] and "PT" not in c[1] for c in b.calls if c[0] == "cancel")


def test_guard_note_names_sole_exit(monkeypatch, tmp_path):
    b, st = _setup(monkeypatch, tmp_path, ON + [FLAG])
    pos = b.chain_position(st)
    assert b.guard_sole_note(pos, st, 106.6, 1.0, 1.1) == "" or "SOLE EXIT" in b.guard_sole_note(pos, st, 106.6, 1.0, 1.1)
    b.local.partials_done = []
    assert b.guard_sole_note(pos, st, 106.6, 1.0, 1.1) == ""
