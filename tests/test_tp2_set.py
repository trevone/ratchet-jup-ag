"""Trevor 2026-10-13 20:57 MT: one-shot manual TP2 move via TP2_SET."""
from tests.test_resting import ON, mkbot, state


def _bot(monkeypatch, tmp_path, size=115.50):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=size)
    st["positions"]["long"]["borrowFeeUsd"] = 0.0
    b.chain = lambda: st
    b.local.lots, b.local.trade_opened_usd = [], size
    b.local.tp_px = [110.33, 110.63]
    b.cancel_request = lambda pk, sz: b.calls.append(("cancel1", pk)) or "ok"
    return b, st


def _f(tmp_path, txt):
    f = tmp_path / "TP2_SET"
    f.write_text(txt)
    return f


def test_tp2_set_moves_tp2_records_and_deletes(monkeypatch, tmp_path):
    b, st = _bot(monkeypatch, tmp_path)
    f = _f(tmp_path, "111.50\n")
    b.tp2_set(b.chain_position(st), st, f)
    assert not f.exists()
    assert b.calls == [("update-trigger", {"request": "R110.63", "triggerPriceUsd": 111.50})]
    assert b.local.tp_px == [110.33, 111.50] and b.local.tp2_override == 111.50


def test_tp2_set_clamped_to_fee_min_and_tp1_gap(monkeypatch, tmp_path):
    b, st = _bot(monkeypatch, tmp_path)
    pos = b.chain_position(st)
    mins, _ = b.tp_fee_mins(pos, st)
    b.tp2_set(pos, st, _f(tmp_path, "109.00"))
    want = max(mins[1], round(110.33 + b.cfg.tp.tp_min_gap_usd, 2))
    assert b.calls[0][1]["triggerPriceUsd"] == want and b.local.tp2_override == want


def test_tp2_set_invalid_file_deleted_no_halt(monkeypatch, tmp_path):
    b, st = _bot(monkeypatch, tmp_path)
    halted = []
    b.halt = lambda *a: halted.append(a)
    f = _f(tmp_path, "abc")
    b.tp2_set(b.chain_position(st), st, f)
    assert not f.exists() and not halted and not b.calls and b.local.tp2_override is None


def test_override_kept_by_resize_and_replace(monkeypatch, tmp_path):
    b, st = _bot(monkeypatch, tmp_path)
    b.cancel_reqs = lambda s, r: True
    pos = b.chain_position(st)
    b.tp2_set(pos, st, _f(tmp_path, "111.50"))
    st["openRequests"] = [{"pubkey": f"R{p}", "triggerPrice": p, "executed": False, "requestType": "trigger",
                           "requestChange": "decrease"} for p in b.local.tp_px]
    b.calls.clear()
    b.resize_tps(pos)
    assert [a["triggerPriceUsd"] for n, a in b.calls if n == "decrease"][-1] == 111.50
    b.calls.clear()
    b.sync_tp_ladder(pos)
    assert [a["triggerPriceUsd"] for n, a in b.calls if n == "decrease"][-1] == 111.50


def test_tp3_set_moves_partial_in_place(monkeypatch, tmp_path):
    b, st = _bot(monkeypatch, tmp_path)
    b.local.partials = [{"pubkey": "PT", "px": 108.0, "entry": 105.0, "size": 20.0}]
    f = tmp_path / "TP3_SET"
    f.write_text("108.40")
    b.tp3_set(b.chain_position(st), st, f)
    assert not f.exists() and b.calls == [("update-trigger", {"request": "PT", "triggerPriceUsd": 108.40})]
    assert b.local.partials[0]["px"] == 108.40
    f.write_text("100")   # below market -> fee-min clamp still inside market: nothing changed
    b.tp3_set(b.chain_position(st), st, f)
    assert not f.exists() and len(b.calls) == 1 and b.local.partials[0]["px"] == 108.40
