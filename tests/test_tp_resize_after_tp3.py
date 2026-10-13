"""Trevor 2026-10-12 06:07 MT: after a TP3 fill, TP1/TP2 are re-sized to the remaining position at the same prices."""
import pytest

from tests.test_resting import ON, mkbot, state


def test_tps_resized_same_prices_after_tp3(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=115.50)
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", [x["pubkey"] for x in r])) or True
    pos = b.chain_position(st)
    b.resize_tps(pos)
    lad = b.cfg.tp.ladder
    assert b.calls[0] == ("cancel", ["R110.33", "R110.63"])
    decs = [a for n, a in b.calls if n == "decrease"]
    assert [d["triggerPriceUsd"] for d in decs] == [110.33, 110.63]
    assert decs[0]["sizeUsd"] == pytest.approx(round(115.50 * lad[0].close_pct, 2))
    if lad[1].close_pct >= 1.0:
        assert decs[1].get("entire") is True
    assert b.local.tp_px == [110.33, 110.63]


def test_resize_falls_back_to_full_replace_without_prices(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.local.tp_px = [0.0, 0.0]
    hit = []
    b.sync_tp_ladder = lambda p: hit.append(p)
    st = state(size=115.50)
    b.resize_tps(b.chain_position(st))
    assert len(hit) == 1
