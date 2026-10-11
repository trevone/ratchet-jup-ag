"""Stake bucket (withdraw.mode = "stake_jupsol"): mode switch, min/carry, stake ledger, native-only margin, CLI dry-run."""
import json
from datetime import datetime, timezone

import pytest

from config import JUPSOL_MINT, WithdrawConfig, load_config
from live import ledger, stake
from live.bot import ChainAccount, margin_sol, stake_line
from live.withdraw import run_withdraw, withdraw_amount_sol

AT = datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc)  # 00:10 MT


def row(close, net):
    return {"close_time": close, "net_sol": str(net), "net_sol_est": ""}


def cfg(**kw):
    return load_config("configs/live_100.toml", params={"withdraw.enabled": True, "live.dry_run": False, **kw})


def chain(sol, pos=False, jupsol=0.0, reqs=()):
    return {"wallet": {"sol": sol}, "positions": {"long": {"sizeUsd": 47.25 if pos else 0}, "short": {}},
            "openRequests": list(reqs),
            "stake": {"jupsol": jupsol, "jupsolAtoms": str(int(jupsol * 1e9)), "solPerJupsol": 1.2, "solValue": jupsol * 1.2}}


@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "SUMMARY", tmp_path / "ledger_summary.json")
    monkeypatch.setattr(ledger, "STAKE", tmp_path / "stake_ledger.csv")
    monkeypatch.setattr(ledger, "OPEN", tmp_path / "ledger_open.json")
    monkeypatch.setattr(ledger, "TRADES", tmp_path / "trades.csv")
    monkeypatch.setattr(ledger, "LIVE", tmp_path)
    (tmp_path / "ledger_summary.json").write_text(json.dumps({"trades": 1, "net_sol": 0.001315074, "network_fee_sol": 0.001039551,
                                                          "jup_fees_usd_est": 0.063, "borrow_usd_est": 0.0013, "gross_pnl_usd": 0.41}))
    return tmp_path


def test_live_config_is_stake_mode_and_locked():
    c = load_config("configs/live_100.toml")
    assert c.withdraw.mode == "stake_jupsol" and c.withdraw.action == "stake_jupsol"
    assert c.withdraw.stake_mint == JUPSOL_MINT and c.withdraw.stake_slippage_bps == 50
    assert WithdrawConfig(mode="realized_profit_sol").action == "send"          # legacy value = old behaviour
    with pytest.raises(ValueError):
        WithdrawConfig(stake_mint="So11111111111111111111111111111111111111112")
    with pytest.raises(ValueError):
        WithdrawConfig(stake_slippage_bps=500)


def test_mode_switch_send_vs_stake():
    rows = [row("2026-10-07 16:00+00:00", 0.004), row("2026-10-07 20:00+00:00", 0.008)]
    sent, staked = [], []
    snd = lambda a: sent.append(a) or {"ok": True, "sig": "S"}
    stk = lambda a: staked.append(a) or {"ok": True, "sig": "K", "jupsol_out": a / 1.2}
    st = {}
    rec = run_withdraw(cfg(), st, chain(0.9), AT, rows=rows, send=lambda a: 1 / 0, stake=stk)
    assert staked == [pytest.approx(0.012)] and rec["action"] == "stake_jupsol" and rec["staked_sol"] == pytest.approx(0.012)
    assert st["withdraw_carry_sol"] == 0.0 and st["withdraw_cutoff"].startswith("2026-10-07 20:00")   # never counted again
    st = {}
    rec = run_withdraw(cfg(**{"withdraw.mode": "send"}), st, chain(0.9), AT, rows=rows, send=snd, stake=lambda a: 1 / 0)
    assert sent == [pytest.approx(0.012)] and rec["action"] == "send" and "staked_sol" not in rec


def test_stake_min_carry_and_failure():
    st = {}
    rows = [row("2026-10-07 16:00+00:00", 0.0003)]
    run_withdraw(cfg(), st, chain(0.9), AT, rows=rows, stake=lambda a: 1 / 0)
    assert st["withdraw_carry_sol"] == pytest.approx(0.0003)                   # < 0.0005 stake floor carried
    rows.append(row("2026-10-08 16:00+00:00", -0.007))
    run_withdraw(cfg(), st, chain(0.9), datetime(2026, 10, 9, 6, 10, tzinfo=timezone.utc), rows=rows, stake=lambda a: 1 / 0)
    assert st["withdraw_carry_sol"] == pytest.approx(-0.0067)                  # loss carried
    rows.append(row("2026-10-09 16:00+00:00", 0.015))
    rec = run_withdraw(cfg(), st, chain(0.9), datetime(2026, 10, 10, 6, 10, tzinfo=timezone.utc), rows=rows,
                       stake=lambda a: {"ok": False, "error": "quote failed"})
    assert rec["amount"] == pytest.approx(0.0083) and st["withdraw_carry_sol"] == pytest.approx(0.0083)   # failed -> carried


def test_stake_amount_ignores_jupsol_and_keeps_buffer():
    c = cfg()
    # 0.04 native SOL, lots of JupSOL: free = 0.04 - 0.02 reserve - 0.003 buffer = 0.017 -> 0.02 not covered;
    # JupSOL never counts, so only the 0.017 that fits is staked and 0.003 is carried (require_next_add_cover = false)
    st, staked = {}, []
    run_withdraw(c, st, chain(0.04, jupsol=10.0), AT, rows=[row("2026-10-07 16:00+00:00", 0.02)],
                 stake=lambda a: staked.append(a) or {"ok": True, "sig": "K"})
    assert staked == [pytest.approx(0.017)] and st["withdraw_carry_sol"] == pytest.approx(0.003)
    st = {}
    run_withdraw(cfg(**{"withdraw.require_next_add_cover": True}), st, chain(0.04, jupsol=10.0), AT,
                 rows=[row("2026-10-07 16:00+00:00", 0.02)], stake=lambda a: 1 / 0)
    assert st["withdraw_carry_sol"] == pytest.approx(0.02)                     # old all-or-nothing rule when flag is true
    assert withdraw_amount_sol(0.015, 0.04, 0.02, 0.01, 0.0, 0, 0.003) == pytest.approx(0.015)
    assert withdraw_amount_sol(0.015, 0.04, 0.02, 0.01, 0.003, 0, 0.003) == 0.0  # next add kept


def test_margin_is_native_sol_only():
    st = chain(0.8147, pos=True, jupsol=5.0)
    assert margin_sol(st) == 0.8147
    acct = ChainAccount(margin_sol(st), 116.0, None, 0.02)
    assert acct.cash == pytest.approx((0.8147 - 0.02) * 116.0)
    assert margin_sol(chain(0.8147, jupsol=0.0)) == margin_sol(st)


def _swap_res(kind, native, wsol, jup, fee=10_000, inAmount="2000000", out="1650000"):
    return {"ok": True, "sig": "SIG", "inAmount": inAmount, "quotedOut": out,
            "deltas": {"feeLamports": fee, "nativeDeltaLamports": native, "wsolDeltaAtoms": str(wsol), "jupsolDeltaAtoms": str(jup)}}


def test_ledger_staked_entries_and_open_trade_not_a_loss(tmp_ledger):
    ledger.on_open("2026-10-07 17:30+00:00", 1, 0.893123334, "OPEN", 5000)
    # stake 0.002 SOL; fee 10k lamports; JupSOL ATA rent 2039280 paid, wSOL ATA rent closed+recreated (net 0)
    b = stake.book("stake", "test", _swap_res("stake", -(2_000_000 + 10_000 + 2_039_280), 0, 1_648_000))
    assert b["sol_in"] == 0.002 and b["jupsol_delta"] == 0.001648 and b["totals"]["staked_sol_total"] == 0.002
    assert b["totals"]["costs_sol_total"] == pytest.approx(0.00204928)
    rec = json.loads(ledger.OPEN.read_text())
    assert rec["external_out_sol"] == pytest.approx(0.00404928)            # trade P&L unaffected by the swap
    summ = json.loads(ledger.SUMMARY.read_text())
    assert summ["net_sol"] == 0.001315074 and summ["trades"] == 1          # realized profit unchanged by stake ops
    # unstake 0.001 JupSOL -> 0.0012 SOL: wallet +, booked as negative external (not a deposit/profit)
    u = stake.book("unstake", "cli", _swap_res("unstake", 1_190_000, 0, -1_000_000))
    assert u["sol_out"] == pytest.approx(0.0012) and u["totals"]["net_jupsol"] == pytest.approx(0.000648)
    assert json.loads(ledger.OPEN.read_text())["external_out_sol"] == pytest.approx(0.00404928 - 0.00119)
    # flat later: the trade's net_sol excludes both moves
    row_ = ledger.on_flat("2026-10-07 19:00+00:00", 0.893123334 - 0.00404928 + 0.00119 + 0.003, 116.5, 0.35, 0.0)
    assert row_["net_sol"] == pytest.approx(0.003)
    assert len(ledger.read_stake_rows()) == 2
    assert json.loads(ledger.SUMMARY.read_text())["stake"]["staked_sol_total"] == 0.002   # survives the trade booking


def test_stake_sol_books_only_when_sent(tmp_ledger):
    calls = []
    def jup(cmd, args):
        calls.append((cmd, args))
        return _swap_res("stake", -2_012_000, 0, 1_649_000) | {"simulated": cmd.startswith("simulate")}
    out = stake.stake_sol(cfg(), 0.002, "test", send=False, jup=jup)
    assert calls[-1][0] == "simulate-swap" and calls[-1][1]["send"] is False and calls[-1][1]["amountAtoms"] == "2000000"
    assert calls[-1][1]["direction"] == "stake" and calls[-1][1]["slippageBps"] == 50
    assert ledger.read_stake_rows() == [] and "jupsol_out" not in out
    out = stake.stake_sol(cfg(), 0.002, "test", send=True, jup=jup)
    assert calls[-1][0] == "swap" and out["jupsol_out"] == 0.001649 and len(ledger.read_stake_rows()) == 1
    with pytest.raises(ValueError):
        stake.swap_args(cfg(), "stake", 1000, slippage_bps=150)              # above stake_max_slippage_bps


def _fake_jup(calls, bal_atoms=1_000_000):
    def jup(cmd, args):
        calls.append((cmd, args))
        if cmd == "state":
            return chain(0.8147, pos=True, jupsol=bal_atoms / 1e9)
        if cmd == "swap-quote":
            return {"ok": True, "outAmount": "1200000"}
        return {"ok": True, "simulated": True, "inAmount": args.get("amountAtoms"), "quotedOut": "1"}
    return jup


@pytest.mark.parametrize("argv,expect_cmd", [
    (["unstake", "all", "--dry-run"], "simulate-swap"),
    (["unstake", "0.0005"], "simulate-swap"),                  # no --yes -> simulate only
    (["send-to-withdraw", "all", "--dry-run"], "simulate-send-jupsol"),
    (["stake", "0.002", "--dry-run", "--yes"], "simulate-swap"),   # --dry-run wins over --yes
])
def test_cli_dry_run_never_sends(argv, expect_cmd, tmp_ledger, capsys):
    calls = []
    assert stake.main(argv, jup=_fake_jup(calls)) == 0
    moving = [c for c in calls if c[0] not in ("state", "swap-quote")]
    assert len(moving) == 1 and moving[0][0] == expect_cmd and moving[0][1]["send"] is False
    out = json.loads(capsys.readouterr().out)
    assert out["DRY_RUN"] is True and ledger.read_stake_rows() == []
    if expect_cmd == "simulate-send-jupsol":
        assert moving[0][1]["to"] == "59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp"


def test_cli_amount_bounds_and_status(tmp_ledger, capsys):
    with pytest.raises(SystemExit):
        stake.main(["unstake", "5", "--dry-run"], jup=_fake_jup([]))        # more than the balance
    with pytest.raises(SystemExit):
        stake.main(["stake", "0.8", "--dry-run"], jup=_fake_jup([]))        # would eat the reserve
    assert stake.main(["status"], jup=_fake_jup([])) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["jupsol_balance"] == 0.001 and s["sol_value_swap_quote"] == 0.0012 and s["wallet_native_sol"] == 0.8147


def test_bot_status_line_shows_stake(tmp_ledger):
    line = stake_line(chain(0.8, jupsol=0.00165))
    assert "JupSOL 0.001650" in line and "staked_total" in line
