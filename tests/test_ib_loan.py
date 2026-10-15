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


# ---------------------------------------------------------------- top-up of an open loan (Trevor 2026-10-15, no cap)
def _open_pair(f, mk, lender_bal=0.77):
    f.write_text(json.dumps({"status": "open", "borrower": "BTC", "coin_name": "WBTC", "need_usd": 2.13, "lender": "SOL",
                             "lender_coin": "SOL", "sol": 0.02, "coin": 0.0000246, "sig": "F0", "opened": "t0"}))
    sol = mk("SOL", bk("SOL", lender_bal, 0.78, {"role": "lender", "peer": "BTC", "sol": 0.02, "coin": 0.0000246,
                                                  "lender_coin": "SOL"}))
    sol.coin_px = lambda st: 100.0
    btc = mk("BTC", bk("WBTC", 0.0010, 0.0007, {"role": "borrower", "peer": "SOL", "sol": 0.02, "coin": 0.0000246,
                                                 "lender_coin": "SOL"}))
    return sol, btc


def _fund(monkeypatch, got="10000", sig="T1"):
    monkeypatch.setattr(sweepmod, "swap_with_backoff", lambda *a, **k: (
        {"ok": True, "sig": sig, "deltas": {"tokenDeltaAtoms": {"WBTC": got}}}, "ok", []))


def test_topup_request_no_cap_min_two_and_gating(env):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    btc.ib_request(0.5)   # below the $2 minimum
    r = json.loads(f.read_text())
    assert r["status"] == "topup_requested" and r["need_usd"] == 2.0 and r["topup"] and r["topup_prev"]["sol"] == 0.02
    f.write_text(json.dumps({**r, "status": "open"}))
    btc.ib_request(500.0)   # no cap
    assert json.loads(f.read_text())["need_usd"] == 500.0
    f.write_text(json.dumps({**r, "status": "open", "borrower": "ETH"}))   # not this borrower's loan
    btc.ib_request(5.0)
    assert json.loads(f.read_text())["status"] == "open"
    assert not hasattr(bankmod, "IB_MAX_USD")


def test_topup_fund_book_equity_neutral_and_cumulative_repay(env, monkeypatch):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    eq_l = bankmod.equity(sol.local.bank, 0.0)
    eq_b = bankmod.equity(btc.local.bank, 0.0)
    btc.ib_request(10.0)
    _fund(monkeypatch)
    sol.ib_tick(state(size=0, reqs=[]), None)
    r = json.loads(f.read_text())
    assert r["status"] == "funded" and abs(r["sol"] - 0.12) < 1e-9 and abs(r["coin"] - 0.0001246) < 1e-12
    ls = sol.local.bank
    assert abs(ls["bal"] - 0.67) < 1e-9 and abs(ls["ib_loan"]["sol"] - 0.12) < 1e-9 and ls["hwm"] == 0.78 and ls["wd_mark"] == 0.78
    assert abs(bankmod.equity(ls, 0.0) - eq_l) < 1e-9
    assert btc.ib_tick(state(), btc.chain_position(state())) is False
    bb = btc.local.bank
    assert abs(bb["bal"] - 0.0011) < 1e-12 and abs(bb["ib_loan"]["sol"] - 0.12) < 1e-9 and bb["hwm"] == 0.0007 and bb["wd_mark"] == 0.0007
    assert abs(bankmod.equity(bb, 0.0) - eq_b) < 1e-12
    r = json.loads(f.read_text())
    assert r["status"] == "open" and "topup" not in r and "topup_prev" not in r and "topup_got" not in r
    calls = []

    def sw(cfg, plan, jup, **k):
        calls.append((plan["atoms"], k["swap_mode"]))
        return ({"ok": True, "sig": "R", "inAmount": "9", "deltas": {"feeLamports": 5000, "nativeDeltaLamports": 120_000_000 - 5000,
                 "tokenDeltaAtoms": {"WBTC": "-9"}}}, "ok", [])
    monkeypatch.setattr(sweepmod, "swap_with_backoff", sw)
    btc.ib_borrower_repay(100.0, 80000.0)   # one ExactOut for the cumulative lent quantity
    assert calls == [(120_000_000, "ExactOut")] and json.loads(f.read_text())["repaid_got"] == 0.12
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert abs(sol.local.bank["bal"] - 0.79) < 1e-9 and "ib_loan" not in sol.local.bank


def test_topup_lender_conditions(env, monkeypatch):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    btc.ib_request(10.0)
    _fund(monkeypatch)
    st = state()
    sol.ib_tick(st, sol.chain_position(st))   # lender has a position
    assert json.loads(f.read_text())["status"] == "topup_requested"
    sol._busy = lambda *a: True
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert json.loads(f.read_text())["status"] == "topup_requested"
    sol._busy = lambda *a: False
    sol.local.bank["window"] = 3
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert json.loads(f.read_text())["status"] == "topup_requested"
    sol.local.bank["window"] = 0
    sol.local.bank["bal"] = 0.14   # 0.14 SOL = $14 < $10 + $5
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert json.loads(f.read_text())["status"] == "topup_requested"
    sol.local.bank["ib_loan"]["peer"] = "ETH"   # other pair
    sol.local.bank["bal"] = 0.77
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert json.loads(f.read_text())["status"] == "topup_requested"


def test_topup_failed_swap_reverts_and_retries(env, monkeypatch):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    btc.ib_request(10.0)
    monkeypatch.setattr(sweepmod, "swap_with_backoff", lambda *a, **k: ({"ok": False}, "fatal", ["x"]))
    sol.ib_tick(state(size=0, reqs=[]), None)
    r = json.loads(f.read_text())
    assert r["status"] == "topup_requested" and r["sol"] == 0.02 and r["need_usd"] == 10.0 and "funding_at" not in r
    assert sol.local.bank["bal"] == 0.77 and sol.local.bank["ib_loan"]["sol"] == 0.02
    _fund(monkeypatch)
    sol.ib_tick(state(size=0, reqs=[]), None)
    assert json.loads(f.read_text())["status"] == "funded"


def test_topup_borrower_flat_cancels_then_repay_proceeds(env, monkeypatch):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    btc.ib_request(10.0)
    btc.ib_tick(state(size=0, reqs=[]), None)   # flat: top-up cancelled
    r = json.loads(f.read_text())
    assert r["status"] == "open" and r["need_usd"] == 2.13 and "topup" not in r and r["sol"] == 0.02
    monkeypatch.setattr(sweepmod, "swap_with_backoff", lambda *a, **k: (
        {"ok": True, "sig": "R", "inAmount": "9", "deltas": {"nativeDeltaLamports": 20_000_000, "tokenDeltaAtoms": {"WBTC": "-9"}}}, "ok", []))
    btc.ib_borrower_repay(100.0, 80000.0)
    assert json.loads(f.read_text())["status"] == "repaid"


def test_topup_timeout_pending_and_stuck_funding(env, monkeypatch):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    btc.ib_request(10.0)
    r = json.loads(f.read_text())
    old = str(__import__("pandas").Timestamp.now(tz="UTC") - __import__("pandas").Timedelta(minutes=45))
    f.write_text(json.dumps({**r, "topup_requested": old}))
    btc.ib_tick(state(), btc.chain_position(state()))   # still in a position, but timed out
    assert json.loads(f.read_text())["status"] == "open"
    btc.ib_request(10.0)
    r = json.loads(f.read_text())
    f.write_text(json.dumps({**r, "status": "funding", "funding_at": old}))   # crash mid-swap
    alerts = []
    monkeypatch.setattr(bankmod, "write_alert", lambda a, t: alerts.append(t) or "x")
    sol.ib_tick(state(size=0, reqs=[]), None)
    r = json.loads(f.read_text())
    assert r["status"] == "open" and r["sol"] == 0.02 and "funding_at" not in r and alerts
    assert sol.local.bank["bal"] == 0.77


def test_legacy_open_record_without_topup_keys_stays_valid(env, monkeypatch):
    f, mk = env
    sol, btc = _open_pair(f, mk)
    assert "topup" not in json.loads(f.read_text())
    assert btc.ib_tick(state(), btc.chain_position(state())) is False
    assert json.loads(f.read_text())["status"] == "open" and btc.local.bank["ib_loan"]["sol"] == 0.02
