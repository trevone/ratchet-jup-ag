"""Trevor 2026-10-29 21:05 MT: no profit-guard MARKET close while guard TP1/TP2 are resting on chain."""
import logging

from tests.test_guard_onchain import _tick_to_guard, b, chain_eq, sb, set_target, sol_st  # noqa: F401 - fixtures


def _armed_met(sb, st):
    sb.local.guard_armed = True
    sb.hwm_equity = lambda *a, **k: 99.0
    set_target(sb, chain_eq(sb, st) - 1e-6)   # guard met on chain


def test_guard_tps_resting_on_chain_no_market_close(sb, monkeypatch, caplog):
    st = sol_st(price=101.0)
    assert sb.tp_requests(st), "fixture must have TP triggers resting"
    _armed_met(sb, st)
    sb.local.partials_done = [{"px": 100.0}]          # TP3 partial filled -> TP1/TP2 at guard
    assert sb.guard_tps_resting(st)
    caplog.set_level(logging.INFO)
    _tick_to_guard(sb, st, monkeypatch)
    assert sb.closed == []
    assert "guard TPs are resting on chain" in caplog.text


def test_no_guard_tps_resting_market_close_with_recheck(sb, monkeypatch):
    st = sol_st(price=101.0, reqs=[])                 # guard TP mode but nothing resting on chain
    _armed_met(sb, st)
    sb.local.partials_done = [{"px": 100.0}]
    assert not sb.guard_tps_resting(st)
    calls = []
    orig = sb.guard_recheck_chain
    sb.guard_recheck_chain = lambda pos: calls.append(1) or orig(pos)
    _tick_to_guard(sb, st, monkeypatch)
    assert calls == [1]                               # fresh chain re-check still runs before the close
    assert len(sb.closed) == 1 and sb.closed[0][2].startswith("profit_guard")


def test_pct_tps_not_guard_mode_close_unchanged(sb, monkeypatch):
    st = sol_st(price=101.0)                           # TPs resting but at pct (no partial yet): not guard TPs
    _armed_met(sb, st)
    sb.local.partials_done = []
    assert not sb.guard_tps_resting(st)
    _tick_to_guard(sb, st, monkeypatch)
    assert len(sb.closed) == 1
