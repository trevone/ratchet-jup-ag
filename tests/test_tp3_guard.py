"""2026-10-12 00:27 MT: TP3 is the only partial mechanism when [resting] partials_enabled; profit guard as % of HWM."""
import json
import sys

import pytest

import live.bot as B
from live import trim as T
from tests.test_resting import ON, mkbot, state


def trim_ready(b, monkeypatch):
    calls = []
    b.local.trim_armed = True
    monkeypatch.setattr(b.strategy, "net_close_pnl", lambda *a: -1.0)
    monkeypatch.setattr(T, "trigger_level", lambda *a, **k: (105.0, True))
    monkeypatch.setattr(T, "select_lots", lambda *a, **k: [6])
    b.execute_trim = lambda *a: calls.append(a) or True
    b._trim_checked = None
    return calls


def test_market_trim_not_executed_in_tp3_mode(monkeypatch, tmp_path):
    on = mkbot(monkeypatch, tmp_path, ON)
    calls = trim_ready(on, monkeypatch)
    st = state()
    assert on.maybe_trim(on.chain_position(st), st, 106.6) is False and not calls


def test_market_trim_unchanged_when_partials_off(monkeypatch, tmp_path):
    off = mkbot(monkeypatch, tmp_path, [])
    calls = trim_ready(off, monkeypatch)
    st = state()
    assert off.maybe_trim(off.chain_position(st), st, 106.6) is True and len(calls) == 1


def test_breakeven_price_is_root_of_equity_and_fallback(monkeypatch, tmp_path):
    """TP3 reference = price where hwm_equity == trade_start_equity (whole-position breakeven); lot avg when no baseline."""
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state()
    pos = b.chain_position(st)
    monkeypatch.setattr(type(b), "bank_on", property(lambda self: True))
    b.local.bank = {"trade_start_equity": 10.0}
    # short-like: equity falls with price, equals 10.0 at 104.25
    monkeypatch.setattr(b, "hwm_equity", lambda p, s, px: 10.0 - (px - 104.25) * 0.1)
    be = b.breakeven_price(pos, st)
    assert be == pytest.approx(104.25, abs=1e-6)
    b._tp3_st = st
    assert b.tp3_ref_avg(pos) == pytest.approx(104.25, abs=1e-6) and b._tp3_ref[1] == "breakeven"
    b.local.bank = {"trade_start_equity": None}
    assert b.breakeven_price(pos, st) is None
    b.tp3_ref_avg(pos)
    assert b._tp3_ref[1] == "lot avg"
