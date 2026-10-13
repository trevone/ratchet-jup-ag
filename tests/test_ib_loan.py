"""Trevor 2026-10-12 18:09 / 18:13 MT: inter-bot loans; HWMs untouched; lender opens disabled; repaid on borrower flat."""
import json

import pytest

from live import bank as bankmod
from live import sweep as sweepmod
import live.bot as B
from tests.test_resting import ON, mkbot, state


def bk(coin, bal, hwm, ln=None):
    b = {"coin": coin, "bal": bal, "hwm": hwm, "wd_mark": hwm, "hist": [], "totals": {}}
    if ln:
        b["ib_loan"] = ln
    return b


@pytest.fixture
def env(monkeypatch, tmp_path):
    f = tmp_path / "ib.json"
    monkeypatch.setattr(B.Bot, "_ib_file", lambda self: f)
    monkeypatch.setattr(bankmod, "journal_lock", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(B.Bot, "bank_on", property(lambda s: True))

    def mk(asset, bank):
        b = mkbot(monkeypatch, tmp_path, ON)
        b.asset = asset
        b.local.bank = bank
        b.bank_fee = lambda *a: None
        b._busy = lambda *a: False
        b._sol_px, b._mkt_px = 100.0, 80000.0
        return b
    return f, mk


def test_hwm_equity_neutral_both_sides():
    lender = bk("SOL", 0.30, 0.77, {"role": "lender", "sol": 0.46, "coin": 0.0006})
    borrower = bk("WBTC", 0.0007, 0.0006, {"role": "borrower", "sol": 0.46, "coin": 0.0006})
    assert not bankmod.update_hwm(lender, True) and lender["hwm"] == 0.77
    assert abs(bankmod.equity(lender, 0.0) - 0.76) < 1e-9
    assert not bankmod.update_hwm(borrower, True) and borrower["hwm"] == 0.0006
    assert abs(bankmod.equity(borrower, 0.0) - 0.0001) < 1e-12
    assert bankmod.withdraw_amount({**borrower, "wd_mark": 0.0}, 1.0, 0.0)[0] == 0.0


def test_request_fund_receive_repay_settle(env, monkeypatch):
    f, mk = env
    btc = mk("BTC", bk("WBTC", 0.0001, 0.0007))
    btc.ib_request(50.0)
    assert json.loads(f.read_text())["status"] == "requested"
    sol = mk("SOL", bk("SOL", 0.77, 0.78))
    sol.coin_px = lambda st: 100.0
    monkeypatch.setattr(sweepmod, "swap_with_backoff", lambda *a, **k: (
        {"ok": True, "sig": "F", "deltas": {"tokenDeltaAtoms": {"WBTC": "62000"}}}, "ok", []))
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert json.loads(f.read_text())["status"] == "funded" and abs(sol.local.bank["bal"] - 0.27) < 1e-9
    assert sol.entries_paused() and "opens disabled" in sol.entries_paused() and sol.local.bank["hwm"] == 0.78
    assert btc.ib_tick(state(), btc.chain_position(state())) is False
    assert abs(btc.local.bank["bal"] - 0.00072) < 1e-12 and btc.local.bank["hwm"] == 0.0007
    monkeypatch.setattr(sweepmod, "swap_with_backoff", lambda *a, **k: (
        {"ok": True, "sig": "R", "deltas": {"nativeDeltaLamports": 501_000_000}}, "ok", []))
    btc.ib_borrower_repay(100.0, 80000.0)
    assert "ib_loan" not in btc.local.bank and json.loads(f.read_text())["status"] == "repaid"
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert abs(sol.local.bank["bal"] - 0.771) < 1e-9 and "ib_loan" not in sol.local.bank and sol.entries_paused() is None
    assert json.loads(f.read_text())["status"] == "settled"


def test_lender_not_flat_does_not_fund_and_errors_never_raise(env, monkeypatch):
    f, mk = env
    f.write_text(json.dumps({"status": "requested", "borrower": "BTC", "coin_name": "WBTC", "need_usd": 50}))
    sol = mk("SOL", bk("SOL", 0.77, 0.78))
    st = state()
    sol.ib_tick(st, sol.chain_position(st))
    assert json.loads(f.read_text())["status"] == "requested"
    f.write_text("{bad")
    assert sol.ib_tick(st, None) is False
