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
    exp = c.open_fee * pos.size + c.close_fee * pos.size + 0.04   # current size only (300 opened ignored)
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


def test_fees_only_on_open_size_after_tp3(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=115.50)
    st["positions"]["long"]["borrowFeeUsd"] = 0.01
    pos = b.chain_position(st)
    assert b.local.trade_opened_usd > 500   # most of the trade already closed by TP3 partials
    mins, fees = b.tp_fee_mins(pos, st)
    assert fees == pytest.approx(0.0012 * 115.50 + 0.01)
    assert mins[0] == tp_fee_min_price(1, pos.avg_entry, 115.50, fees, 2.0)


def test_tp3_fee_min_pushes_inside_levels_and_keeps_beyond(monkeypatch, tmp_path):
    from tests.test_resting import LV
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state(size=581.0)
    st["positions"]["long"]["borrowFeeUsd"] = 0.0
    b._last_st = st
    pos = b.chain_position(st)
    lot = b.local.lots[-1]
    base = b.partial_plan(pos, [lot], LV)
    c, size, entry = b.cfg.costs, float(lot["size"]), float(lot["price"])
    f3 = (c.open_fee + c.close_fee) * size
    assert base["fee_min"] == tp_fee_min_price(1, entry, size, f3, b.cfg.tp.tp3_fee_mult)
    assert base["px"] >= base["fee_min"]
    net = size * (base["px"] - entry) / entry - f3
    assert net >= b.cfg.tp.tp3_fee_mult * f3 - 0.01 * size / entry   # within one cent of rounding
    # big borrow on the position -> pro-rated share raises TP3's minimum beyond the level -> placed at the minimum
    st["positions"]["long"]["borrowFeeUsd"] = 20.0
    hi = b.partial_plan(pos, [lot], LV)
    exp = tp_fee_min_price(1, entry, size, f3 + 20.0 * size / pos.size, b.cfg.tp.tp3_fee_mult)
    assert hi["fee_min"] == exp and hi["px"] == max(exp, base["px"]) and exp > base["fee_min"]


def test_tp3_config_default():
    from config import TakeProfitConfig
    assert TakeProfitConfig().tp3_fee_mult == 2.5
