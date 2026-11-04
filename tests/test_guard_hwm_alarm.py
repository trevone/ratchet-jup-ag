"""Trevor 2026-11-03: guard target = HWM x (1 + min_profit_pct_trade); flat balance < HWM -> ALARM HWM (no halt)."""
import pytest

import live.bank as K
import live.bot as botmod
from tests.test_bank import _st
from tests.test_chain_balance import _bot


def _b(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_btc.toml", "BTC")
    b.local.bank = K.new_bank("BTC", 0.002, 0.002)
    monkeypatch.setattr(botmod, "HWM_ALARM_PATH", str(tmp_path / "HWM_ALARM"))
    return b


def test_guard_target_is_hwm(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    b.local.bank["trade_start_equity"] = 0.0015   # ignored by the guard
    assert b.guard_target(_st(asset="BTC")) == pytest.approx(b.local.bank["hwm"] * (1 + b.cfg.profit_guard.min_profit_pct_trade))


def test_guard_target_follows_hwm_deposit(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    t0 = b.guard_target(_st(asset="BTC"))
    K.external(b.local.bank, 0.0001, "deposit") if hasattr(K, "external") else b.local.bank.update(hwm=b.local.bank["hwm"] + 0.0001)
    assert b.guard_target(_st(asset="BTC")) > t0


def test_alarm_below_hwm(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["bal"] = bk["hwm"] - 2 * b.cfg.ledger.hwm_alarm_tol_native
    assert b.hwm_alarm_check(bk, 150.0, 75000.0) is True
    assert "< HWM" in (tmp_path / "HWM_ALARM").read_text()


def test_no_alarm_within_tol(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["bal"] = bk["hwm"] - 0.5 * b.cfg.ledger.hwm_alarm_tol_native
    assert b.hwm_alarm_check(bk, 150.0, 75000.0) is False
    assert not (tmp_path / "HWM_ALARM").exists()
