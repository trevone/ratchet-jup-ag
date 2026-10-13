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


@pytest.mark.parametrize("trims,partials,want", [(0, 0, 0.002), (3, 0, 0.0035), (2, 2, 0.004)])
def test_guard_target_pct_of_hwm_grows_per_partial(monkeypatch, tmp_path, trims, partials, want):
    b = mkbot(monkeypatch, tmp_path, ["ledger.min_profit_pct_hwm=0.002", "ledger.min_profit_pct_per_partial=0.0005"])
    b.local.bank = {"hwm": 2.0, "coin": "SOL"}
    b.local.trims = [{}] * trims
    b.local.partials_done = [{}] * partials
    assert b.guard_min_native() == pytest.approx(2.0 * want)


def test_guard_falls_back_to_fixed_native_when_pct_off(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ["ledger.min_profit_native=0.0015"])
    b.local.bank = {"hwm": 2.0, "coin": "SOL"}
    assert b.guard_min_native() == 0.0015


def test_manual_trim_refuses_send_while_tp3_live(tmp_path, monkeypatch):
    import runpy
    (tmp_path / "logs").mkdir()
    (tmp_path / "state.json").write_text(json.dumps({"partials": [{"pubkey": "TP3X"}]}))
    monkeypatch.setattr(sys, "argv", ["x", "--config", "configs/live_100.toml", "--send", "--set", f"bot.state_dir={tmp_path}",
                                      "--set", "resting.partials_enabled=true"])
    monkeypatch.delenv("JUP_LIVE", raising=False)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("live.patches.manual_trim_20261008", run_name="__main__")
    assert "manual trim refused" in str(e.value) and "TP3X" in str(e.value)
