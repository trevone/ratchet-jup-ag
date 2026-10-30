"""Trevor 2026-10-30 fixes: post-close cooldown + booking/HWM before entry (1, 3), chain-mode booked net (2),
recoverable own request rent in 'equity if closed' (4)."""
import pandas as pd
import pytest

import live.bank as K
from config import load_config
from strategy import SHORT
from strategy import Action, Intent
from tests.test_bank import _st
from tests.test_chain_balance import _bot


@pytest.mark.parametrize("p", ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"])
def test_cooldown_config_all_bots(p):
    assert load_config(p).entry.post_close_cooldown_sec == 300.0


def test_cooldown_default_and_validation():
    from config import EntryConfig
    assert EntryConfig().post_close_cooldown_sec == 300.0
    with pytest.raises(ValueError):
        EntryConfig(post_close_cooldown_sec=-1)


def _btc(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_btc.toml", "BTC")
    b.local.bank = K.new_bank("BTC", 0.001478590, 0.001539433)
    return b


def _btc_st(atoms):
    st = _st(asset="BTC", size=0)
    st["sweepTokens"] = {"WBTC": {"mint": K.MINT["WBTC"], "atoms": str(atoms)}}
    return st


def test_open_blocked_during_cooldown_then_after_booking_and_hwm(monkeypatch, tmp_path):
    b = _btc(monkeypatch, tmp_path)
    b.mark_closed("tp_timer")
    assert "cooldown" in b.post_close_block()
    b.local.bank["closed_at"] = str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=301))
    assert "flat HWM" in b.post_close_block()
    b.local.bank["book_pend"] = {"x": 1}
    assert "not booked" in b.post_close_block()
    b.local.bank.pop("book_pend")
    # BTC 08:45 MT: chain 0.001548240 > HWM 0.001539433 -> flat tick raises HWM and clears the gate
    b.bank_tick(_btc_st(154824), None, 77000.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.00154824) and bk["hwm"] == pytest.approx(0.00154824)
    assert "flat_hwm_due" not in bk and b.post_close_block() is None


def test_hwm_gate_waits_for_window(monkeypatch, tmp_path):
    b = _btc(monkeypatch, tmp_path)
    b.mark_closed("x")
    b.local.bank["closed_at"] = str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=400))
    b.local.bank["window"] = 3
    b.bank_tick(_btc_st(154824), None, 77000.0)
    assert b.local.bank.get("flat_hwm_due") is True           # payout window still open: not flat yet


def test_open_or_add_skips_open_in_cooldown(monkeypatch, tmp_path):
    b = _btc(monkeypatch, tmp_path)
    calls = []
    b.cmd = lambda n, a: calls.append(n) or {"ok": True, "sig": "S"}
    b.mark_closed("x")
    it = Intent(Action.OPEN, SHORT, 77000.0, margin=10.5, leverage=5)
    b.open_or_add(it, None, _btc_st(154824), 100.0)
    assert calls == []


def test_no_block_without_close_state(monkeypatch, tmp_path):
    b = _btc(monkeypatch, tmp_path)
    assert b.post_close_block() is None                        # live state before the deploy: no keys -> no block


def test_zero_cooldown_still_needs_booking(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_btc.toml", "BTC")
    import dataclasses
    b.cfg = dataclasses.replace(b.cfg, entry=dataclasses.replace(b.cfg.entry, post_close_cooldown_sec=0.0))
    b.local.bank = K.new_bank("BTC", 0.0015, 0.0015)
    b.mark_closed("x")
    assert "flat HWM" in b.post_close_block()


def test_chain_booking_matches_chain_not_minus_deposit(monkeypatch, tmp_path):
    """BTC 08:46 MT: row was dep 0.000135780 recv 0 = -$10.44; the chain result is what the token balance did."""
    b = _btc(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["trade_start_equity"] = 0.001534000
    bk["trade_start_debt"] = 0.0
    bk["trade_dep"], bk["trade_recv"] = 0.000135780, 0.0
    bk["bal"] = 0.001548240
    net = b.chain_trade_net(110.0, 77000.0)
    assert net == pytest.approx(0.001548240 - 0.001534000)
    from live import ledger
    ledger.OPEN.write_text('{"open_time": "2026-10-30T13:30:00+00:00"}')
    rows = []
    monkeypatch.setattr(b, "_book_row", lambda a, native=None: rows.append(native))
    bk["book_pend"] = {"time": "t", "wallet_sol": 1.0, "sol_px": 110.0, "realised": 0, "hourly": 0, "flows": None,
                       "rent": None, "gross": None}
    b.bank_book_pending(110.0, 77000.0)
    n = rows[0]
    assert n["received"] - n["deposited"] == pytest.approx(net) and n["received"] > 0
    assert bk["trade_dep"] == 0.0 and "trade_start_debt" not in bk


def test_chain_booking_excludes_fee_debt_growth(monkeypatch, tmp_path):
    b = _btc(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["trade_start_equity"], bk["trade_start_debt"], bk["bal"] = 0.0015, 0.000005, 0.0016
    bk["fee_sol"] = 0.01                                        # fee debt grew during the trade: reported separately
    assert b.chain_trade_net(110.0, 77000.0) == pytest.approx(0.0016 - 0.001505)


def test_alert_mode_keeps_counters(monkeypatch, tmp_path):
    import dataclasses
    b = _btc(monkeypatch, tmp_path)
    b.cfg = dataclasses.replace(b.cfg, ledger=dataclasses.replace(b.cfg.ledger, ext_flow_mode="alert"))
    b.local.bank["trade_start_equity"] = 0.0015
    assert b.chain_trade_net(110.0, 77000.0) is None


def test_open_records_debt_at_open(monkeypatch, tmp_path):
    b = _btc(monkeypatch, tmp_path)
    b.local.bank["fee_sol"] = 0.0041
    b.cmd = lambda n, a: {"ok": True, "sig": "S"}
    monkeypatch.setattr(b, "guard", lambda *a: None)
    monkeypatch.setattr(b, "add_pending_wait", lambda *a: False)
    monkeypatch.setattr(b, "chain", lambda: (_ for _ in ()).throw(RuntimeError("stop after open")))
    it = Intent(Action.OPEN, SHORT, 77000.0, margin=10.5, leverage=5)
    st = _btc_st(154824)
    try:
        b.open_or_add(it, None, st, 100.0)
    except RuntimeError:
        pass
    bk = b.local.bank
    assert bk["trade_start_debt"] == pytest.approx(K.fee_debt_native(bk, b.sol_price(st), b.coin_px(st))) and bk["trade_start_debt"] > 0


def _sol_pos(monkeypatch, tmp_path, nreq):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 1.0, 1.2)
    st = _st(asset="SOL", side="short", size=50.0, coll=10.0, avg=100.0, price=100.0, reqs=[99.0, 98.0][:nreq])
    b.local.side, b.local.last_size = SHORT, 50.0
    return b, st, b.chain_position(st)


def test_equity_if_closed_includes_own_request_rent_sol(monkeypatch, tmp_path):
    b, st, pos = _sol_pos(monkeypatch, tmp_path, 2)
    b0, st0, pos0 = _sol_pos(monkeypatch, tmp_path / "z" if (tmp_path / "z").mkdir() is None else tmp_path, 0)
    assert b.own_request_rent(pos, st) == pytest.approx(2 * K.REQ_RENT)                 # ~0.0074 SOL
    assert b.hwm_equity(pos, st, 100.0) - b0.hwm_equity(pos0, st0, 100.0) == pytest.approx(2 * K.REQ_RENT, abs=1e-9)
    ok, note = b.hwm_check(pos, st, 100.0, 0.0)
    assert f"{b.hwm_equity(pos, st, 100.0):.9f}" in note


def test_timer_hwm_check_passes_with_rent(monkeypatch, tmp_path):
    b, st, pos = _sol_pos(monkeypatch, tmp_path, 2)
    eq_no_rent = b.hwm_equity(pos, st, 100.0) - 2 * K.REQ_RENT
    b.local.bank["hwm"] = eq_no_rent + 0.005                     # below HWM without the rent, above with it
    assert b.hwm_check(pos, st, 100.0, 0.0)[0] is True


def test_no_rent_flat_or_eth(monkeypatch, tmp_path):
    b, st, pos = _sol_pos(monkeypatch, tmp_path, 2)
    assert b.own_request_rent(None, st) == 0.0
    e = _bot(monkeypatch, tmp_path, "configs/live_eth.toml", "ETH")
    e.local.bank = K.new_bank("ETH", 0.05, 0.05)
    assert e.own_request_rent(pos, st) == 0.0


def test_tp3_rule_unchanged(monkeypatch, tmp_path):
    b, st, pos = _sol_pos(monkeypatch, tmp_path, 2)
    b.local.partials_done = [{"px": 96.0, "size": 10.0}]
    assert b.timer_blocked_tp3() is True
