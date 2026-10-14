"""Trevor 2026-10-13: fee-based minimum TP1 / TP2 distance (tp1_fee_mult / tp2_fee_mult)."""
import pytest

from live.bot import apply_tp_fee_min, tp_fee_min_price, tp_fee_total
from tests.test_resting import ON, mkbot, state


def test_total_fees():
    assert tp_fee_total(200.0, 300.0, 0.0006, 0.0006, 0.05) == pytest.approx(0.18 + 0.12 + 0.05)
    assert tp_fee_total(200.0, 300.0, 0.0006, 0.0006, -1.0) == pytest.approx(0.30)


def test_min_price_portion_nets_mult_x_fee_share():
    side, avg, size, F, m = 1, 100.0, 200.0, 0.35, 2.0
    p = tp_fee_min_price(side, avg, size, F, m)
    chunk = 100.0
    net = chunk * (p - avg) / avg - F * chunk / size
    assert net >= m * F * chunk / size - 1e-9
    assert p == pytest.approx(100.53)        # 100 * (1 + 3 * 0.35/200) = 100.525 -> ceil
    s = tp_fee_min_price(-1, avg, size, F, 3.0)
    assert s == pytest.approx(99.30)         # 100 * (1 - 4 * 0.00175) = 99.30
    assert s < avg


def test_level_kept_beyond_min_else_at_min_and_tp2_beyond_tp1():
    out, why = apply_tp_fee_min(1, [101.0, 102.0], [0, 1], [100.5, 100.8], 0.05)
    assert out == [101.0, 102.0] and not why
    out, _ = apply_tp_fee_min(1, [100.2, 100.4], [0, 1], [100.5, 100.8], 0.05)
    assert out == [100.5, 100.8]
    out, _ = apply_tp_fee_min(1, [100.2, 100.52], [0, 1], [100.5, 100.51], 0.05)
    assert out == [100.5, 100.55]            # TP2 pushed beyond TP1 + gap
    out, _ = apply_tp_fee_min(-1, [99.9, 99.8], [0, 1], [99.5, 99.3], 0.05)
    assert out == [99.5, 99.3]
    out, _ = apply_tp_fee_min(1, [100.2, 100.4], [1], [100.5, 100.8], 0.05)
    assert out == [100.2, 100.8]             # TP1 already filled: untouched


def test_config_defaults():
    from config import TakeProfitConfig
    t = TakeProfitConfig()
    assert t.tp1_fee_mult == 2.0 and t.tp2_fee_mult == 3.0


def test_bot_fee_mins_use_all_opens_close_and_borrow(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=115.50)
    side = "long" if b.chain_position(st).side == 1 else "short"
    st["positions"][side]["borrowFeeUsd"] = 0.04
    pos = b.chain_position(st)
    b.local.trade_opened_usd = 300.0
    mins, fees = b.tp_fee_mins(pos, st)
    c = b.cfg.costs
    exp = c.open_fee * max(300.0, sum(l["size"] for l in b.local.lots), pos.size) + c.close_fee * pos.size + 0.04
    assert fees == pytest.approx(exp)
    assert mins[0] == tp_fee_min_price(pos.side, pos.avg_entry, pos.size, exp, b.cfg.tp.tp1_fee_mult)
    assert mins[1] == tp_fee_min_price(pos.side, pos.avg_entry, pos.size, exp, b.cfg.tp.tp2_fee_mult)


def test_recheck_replaces_tps_inside_min(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=115.50)
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", [x["pubkey"] for x in r])) or True
    pos = b.chain_position(st)
    mins, _ = b.tp_fee_mins(pos, st)
    a = pos.avg_entry
    b.local.tp_px = [round(a + pos.side * 0.01, 2), round(mins[1] + pos.side * 1.0, 2)]
    assert b.fee_min_recheck(pos, st) is True
    decs = [x for n, x in b.calls if n == "decrease"]
    assert decs[0]["triggerPriceUsd"] == mins[0]
    assert decs[1]["triggerPriceUsd"] == round(mins[1] + pos.side * 1.0, 2)   # analyst level beyond min kept
    b.calls.clear()
    assert b.fee_min_recheck(pos, st) is False and not b.calls
