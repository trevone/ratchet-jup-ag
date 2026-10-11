from datetime import datetime, timezone

import pytest

from config import Config, WithdrawConfig, load_config
from live.withdraw import TX_FEE_BUFFER_SOL, due, realized_since, run_withdraw, trade_net_sol, withdraw_amount_sol

R = 0.02  # reserve


def row(close, net, est=None):
    return {"close_time": close, "net_sol": str(net), "net_sol_est": "" if est is None else str(est)}


def test_realized_sums_only_trades_after_cutoff():
    rows = [row("2026-10-07 10:00+00:00", 0.004), row("2026-10-08 10:00+00:00", 0.006), row("2026-10-08 12:00+00:00", -0.001)]
    s, newest = realized_since(rows, "2026-10-07 10:00+00:00")
    assert s == pytest.approx(0.005)
    assert newest.startswith("2026-10-08 12:00")
    assert realized_since(rows, None)[0] == pytest.approx(0.009)


def test_deposit_mid_trade_not_counted():
    # wallet delta shows +1.0 SOL (a deposit during the trade), fee-model estimate says +0.003
    assert trade_net_sol(row("t", 1.003, 0.003)) == pytest.approx(0.003)
    assert trade_net_sol(row("t", 0.002, 0.003)) == pytest.approx(0.002)   # conservative: the smaller one


def test_amount_requires_min_and_cover():
    assert withdraw_amount_sol(0.05, 1.0, R, 0.01, 0.0, 0) == pytest.approx(0.05)
    assert withdraw_amount_sol(0.009, 1.0, R, 0.01, 0.0, 0) == 0.0              # under min -> carry
    assert withdraw_amount_sol(-0.02, 1.0, R, 0.01, 0.0, 0) == 0.0              # loss -> carry
    assert withdraw_amount_sol(0.05, 1.0, R, 0.01, 0.0, 1) == 0.0               # market request in flight


def test_open_position_allowed_if_free_sol_covers_and_next_add_kept():
    # free = 0.10 - 0.02 - fee - next_add 0.04 = ~0.04
    assert withdraw_amount_sol(0.03, 0.10, R, 0.01, 0.04, 0) == pytest.approx(0.03)
    assert withdraw_amount_sol(0.05, 0.10, R, 0.01, 0.04, 0) == 0.0             # would starve the next add
    assert withdraw_amount_sol(0.06 - TX_FEE_BUFFER_SOL, 0.08, R, 0.01, 0.0, 0) == pytest.approx(0.06 - TX_FEE_BUFFER_SOL)


def _cfg(**kw):
    # these tests cover the "send" mode (old behaviour); the live config is stake_jupsol (tests/test_stake.py)
    return load_config("configs/live_100.toml", params={"withdraw.enabled": True, "live.dry_run": False,
                                                        "withdraw.mode": "send", **kw})


def _chain(sol, pos=False, reqs=()):
    return {"wallet": {"sol": sol}, "positions": {"long": {"sizeUsd": 26.0 if pos else 0}, "short": {}},
            "openRequests": list(reqs)}


AT = datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc)  # 00:10 MT


def test_run_sends_realized_and_resets_carry():
    sent = []
    st = {}
    rows = [row("2026-10-07 16:00+00:00", 0.004), row("2026-10-07 20:00+00:00", 0.008)]
    rec = run_withdraw(_cfg(), st, _chain(0.9), AT, rows=rows, send=lambda a: sent.append(a) or {"ok": True, "sig": "SIG"})
    assert sent == [pytest.approx(0.012)] and rec["solscan"] == "https://solscan.io/tx/SIG"
    assert st["withdraw_carry_sol"] == 0.0 and st["withdraw_cutoff"].startswith("2026-10-07 20:00")
    # same day again: not due; next day with no new trades: nothing
    assert run_withdraw(_cfg(), st, _chain(0.9), AT, rows=rows, send=lambda a: 1 / 0) is None
    rec = run_withdraw(_cfg(), st, _chain(0.9), datetime(2026, 10, 9, 6, 10, tzinfo=timezone.utc), rows=rows,
                       send=lambda a: 1 / 0)
    assert rec["amount"] == 0.0 and st["withdraw_carry_sol"] == 0.0


def test_run_carries_small_and_uncovered_and_failed():
    st = {}
    rows = [row("2026-10-07 16:00+00:00", 0.006)]
    run_withdraw(_cfg(), st, _chain(0.9), AT, rows=rows, send=lambda a: 1 / 0)
    assert st["withdraw_carry_sol"] == pytest.approx(0.006)                    # < 0.01 carried
    rows.append(row("2026-10-08 16:00+00:00", 0.006))
    day2 = datetime(2026, 10, 9, 6, 10, tzinfo=timezone.utc)
    run_withdraw(_cfg(), st, _chain(0.025, pos=True), day2, rows=rows, send=lambda a: 1 / 0)
    assert st["withdraw_carry_sol"] == pytest.approx(0.012)                    # not covered by free SOL
    rows.append(row("2026-10-09 16:00+00:00", -0.002))
    day3 = datetime(2026, 10, 10, 6, 10, tzinfo=timezone.utc)
    run_withdraw(_cfg(), st, _chain(0.9), day3, rows=rows, send=lambda a: {"ok": False, "err": "x"})
    assert st["withdraw_carry_sol"] == pytest.approx(0.010)                    # send failed -> carried


def test_address_locked():
    with pytest.raises(ValueError):
        WithdrawConfig(address="11111111111111111111111111111111")
    assert Config().withdraw.enabled is False


def test_due_once_per_day_after_time():
    assert due(AT, "00:05", None)
    assert not due(AT, "00:05", "2026-10-08")
    assert not due(datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc), "00:05", None)  # 00:00 MT
