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
