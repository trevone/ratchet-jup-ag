"""Token-account rent is recoverable capital, not trade P&L (2026-10-07 rent fix) + realized gross from program events +
the rent-reclaim helper (dry run by default). Pure functions, fake CLI results and temp files only: conftest.py blocks
every TS CLI / subprocess call, so nothing here can reach the chain."""
from __future__ import annotations

import csv
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import live.live_broker as lb
from config import load_config
from live import ledger, reclaim_rent as rr, sweep as sw, withdraw as wd
from live.flows import flow_extras
from test_multi_asset import SOL_PX

R = 1_488_440            # rent-exempt minimum of a 165-byte token account (lamports, read on-chain 2026-10-07)
L = 1_000_000_000
ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ the chain stays blocked
def test_conftest_still_blocks_the_chain():
    with pytest.raises(RuntimeError):
        lb.jup("state", {})
    with pytest.raises(RuntimeError):
        subprocess.run(["npx", "tsx", "src/cli.ts", "state"])
    assert ledger.TRADES.parent != ROOT / "live"          # ledger redirected to tmp


# ------------------------------------------------------------------ sweep: rent excluded from the swap result
def test_swap_sol_change_excludes_recreated_wsol_rent():
    # tonight's first ETH sweep (BvycSChw): owner +44101009, wSOL ATA re-created (+R lamports, 0 atoms)
    sol, rent = sw.swap_sol_change({"nativeDeltaLamports": 44_101_009, "wsolDeltaAtoms": "0", "rentDeltaLamports": R})
    assert sol == pytest.approx(0.045589449) and rent == pytest.approx(0.00148844)
    # older CLI output without the field: old formula, rent 0
    assert sw.swap_sol_change({"nativeDeltaLamports": 46_994_710, "wsolDeltaAtoms": "0"}) == (0.04699471, 0.0)
    # stray wrapped SOL unwrapped by the swap's cleanup: native up, wSOL atoms down -> not counted as proceeds
    sol2, _ = sw.swap_sol_change({"nativeDeltaLamports": 1_000_000 + 5_000_000, "wsolDeltaAtoms": "-5000000", "rentDeltaLamports": 0})
    assert sol2 == pytest.approx(0.001)


# ------------------------------------------------------------------ flows extras + ledger booking
def test_flow_extras():
    res = {"ok": True, "netLamports": -91_909_460, "rentLamports": R, "pnlUsd": 0.356529, "decreases": 2, "eventErrors": []}
    assert flow_extras(res) == (pytest.approx(0.00148844), 0.356529)
    assert flow_extras({"ok": True, "netLamports": 5}) == (None, None)                 # older CLI
    assert flow_extras({**res, "decreases": 0}) == (pytest.approx(0.00148844), None)   # no decrease event seen
    assert flow_extras({**res, "eventErrors": ["x"]})[1] is None
    assert flow_extras({"ok": False}) == (None, None) and flow_extras(None) == (None, None)


def test_rent_only_trade_is_zero_and_refund_is_not_profit():
    # a trade whose only effect is rent moving between native SOL and a token account books 0, both ways
    for rent in (R, -R):
        ledger.on_open("2026-10-08 01:00:00+00:00", 1, 1.0, "S", 5000, position="P")
        ledger.update(opened_size_usd=73.5, size_usd=73.5, avg_entry=100.0)
        row = ledger.on_flat("2026-10-08 02:00:00+00:00", 1.0, SOL_PX, None, 0.0, flows_net_sol=0.0,
                             flows_rent_sol=rent / L)
        assert row["net_sol"] == 0.0 and row["rent_sol"] == pytest.approx(rent / L)
        assert "net_sol_est" not in row and wd.trade_net_sol(row) == 0.0
    assert ledger._load(ledger.SUMMARY, {})["token_rent_sol"] == pytest.approx(0.0)


def test_gross_sources_and_estimate_rules():
    def trade(gross, events=None, flows=0.001, after=1.001):
        ledger.on_open("2026-10-08 01:00:00+00:00", 1, 1.0, "S", 5000, position="P")
        ledger.update(opened_size_usd=73.5, size_usd=73.5, avg_entry=100.0)
        return ledger.on_flat("2026-10-08 02:00:00+00:00", after, SOL_PX, gross, 0.0, flows_net_sol=flows,
                              events_gross_usd=events)
    r = trade(0.0, events=0.30)                     # events preferred over the closed account's 0
    assert r["gross_pnl_source"] == "position_events" and r["gross_pnl_usd"] == 0.30
    assert r["net_sol_est"] == pytest.approx((0.30 - 0.0882) / SOL_PX - 5e-6)
    r = trade(0.25)                                 # chain value used when no events
    assert r["gross_pnl_source"] == "chain_position" and r["net_sol_est"] == pytest.approx((0.25 - 0.0882) / SOL_PX - 5e-6)
    r = trade(0.0)                                  # unknown gross on position flows: no fees-only cap
    assert r["gross_pnl_source"] == "none" and "net_sol_est" not in r and wd.trade_net_sol(r) == pytest.approx(0.001)
    r = trade(None)                                 # closed side reported as unknown (TS state null)
    assert r["gross_pnl_source"] == "none" and "net_sol_est" not in r and wd.trade_net_sol(r) == pytest.approx(0.001)
    for g in (0.0, None):                           # wallet-delta fallback (deposit possible) keeps the conservative est
        r = trade(g, flows=None, after=2.0)
        assert r["net_sol_method"] == "wallet_delta" and wd.trade_net_sol(r) == pytest.approx(-0.0882 / SOL_PX - 5e-6)


# ------------------------------------------------------------------ daily stake across bots after the fix
def test_daily_stake_counts_corrected_rows_across_bots(tmp_path, monkeypatch):
    """Tonight's numbers: carry 0.001315074 + SOL trade 2 (net 0.000667895, event-based est 0.00119303) + ETH corrected
    (net 0.000674699) + no BTC trade -> 0.002657668 SOL staked; the old rows would have given a negative total."""
    f = ledger.FIELDS
    (tmp_path / "live/eth").mkdir(parents=True)
    with open(tmp_path / "live/trades.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=f)
        w.writeheader()
        w.writerow({"close_time": "2026-10-07 16:06:27+00:00", "net_sol": 0.001315074})
        w.writerow({"close_time": "2026-10-08 01:58:40+00:00", "net_sol": 0.000667895, "net_sol_est": 0.00119303})
    with open(tmp_path / "live/eth/trades.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=f)
        w.writeheader()
        w.writerow({"close_time": "2026-10-08 03:59:50+00:00", "net_sol": 0.000674699, "net_sol_est": 0.002256652, "rent_sol": 0.00297688})
    monkeypatch.setattr(wd, "ROOT_DIR", tmp_path)
    cfg = load_config("configs/live_100.toml")
    state = {"withdraw_carry_sol": 0.001315074, "withdraw_cutoff": "2026-10-07 16:06:27+00:00", "withdraw_last_date": "2026-10-07"}
    chain = {"wallet": {"sol": 1.6075}, "positions": {"long": {"sizeUsd": 73.5}, "short": {}}, "openRequests": [],
             "allOpenRequests": [{"requestType": "trigger"}] * 4, "allPositions": [{"market": "SOL"}, {"market": "BTC"}]}
    staked = []
    rec = wd.run_withdraw(cfg, state, chain, datetime(2026, 10, 8, 6, 5, tzinfo=timezone.utc), next_add_sol=0.08,
                          stake=lambda a: staked.append(a) or {"ok": True, "sig": "S"})
    assert rec["realized_new"] == pytest.approx(0.001342594, abs=1e-9)
    assert rec["owed"] == pytest.approx(0.002657668, abs=1e-9) and staked == [pytest.approx(0.002657668, abs=1e-9)]
    assert rec["realized_per_file"]["live/eth/trades.csv"] == pytest.approx(0.000674699, abs=1e-9)


# ------------------------------------------------------------------ rent reclaim helper (dry run by default)
def _st(positions=(), requests=(), toks=None):
    toks = toks if toks is not None else {
        "USDC": {"exists": True, "atoms": "0", "lamports": R}, "ETH": {"exists": False, "atoms": "0", "lamports": 0},
        "WBTC": {"exists": True, "atoms": "0", "lamports": R}, "WSOL": {"exists": True, "atoms": "0", "lamports": R}}
    return {"wallet": {"sol": 1.6}, "allPositions": list(positions), "allOpenRequests": list(requests), "openRequests": [],
            "sweepTokens": toks, "stake": {"ataLamports": R, "jupsolAtoms": "1650244"}}


def test_rent_held_and_plan():
    h = rr.rent_held(_st())
    assert h["total_sol"] == pytest.approx(4 * R / L) and h["reclaimable_sol"] == pytest.approx(3 * R / L)
    p = rr.plan_reclaim(_st())
    assert p["blocked"] == [] and p["close"] == ["USDC", "WBTC", "WSOL"] and p["reclaim_lamports"] == 3 * R
    assert {"name": "ETH", "why": "absent"} in p["skipped"]
    busy = rr.plan_reclaim(_st(positions=[{"market": "BTC", "side": "long"}], requests=[{"requestType": "trigger"}]))
    assert len(busy["blocked"]) == 2                                   # open position AND TP triggers block it
    dusty = _st()
    dusty["sweepTokens"]["USDC"]["atoms"] = "12"
    assert "USDC" not in rr.plan_reclaim(dusty)["close"]               # non-zero balance is never closed
    assert rr.plan_reclaim(_st(toks={"error": "x"}))["blocked"]
    with pytest.raises(ValueError):
        rr.plan_reclaim(_st(), names=("JUPSOL",))                      # the stake bucket is never closable


def test_stake_amount_keeps_reserve():
    assert rr.stake_amount_sol(0.00446532, 1.6, 0.02, 0.003) == pytest.approx(0.00446532)
    assert rr.stake_amount_sol(0.00446532, 0.024, 0.02, 0.003) == pytest.approx(0.001)
    assert rr.stake_amount_sol(0.00446532, 0.01, 0.02, 0.003) == 0.0


def test_reclaim_dry_run_only_simulates_and_refuses_when_not_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "CAPITAL", tmp_path / "capital.csv")
    cfg = load_config("configs/live_100.toml")
    calls = []

    def fake(cmd, args):
        calls.append((cmd, args.get("send")))
        if cmd == "state":
            return _st()
        if cmd == "simulate-close-empty-atas":
            return {"ok": True, "simulated": True, "reclaimLamports": 3 * R, "closed": args["names"]}
        if cmd == "simulate-swap":
            return {"ok": True, "simulated": True, "quotedOut": "3600000"}
        raise AssertionError(f"dry run must not call {cmd}")
    out = rr.run(cfg, fake, send=False)
    assert out["ok"] and out["dry_run"] and out["stake_amount_sol"] == pytest.approx(3 * R / L)
    assert [c for c, _ in calls] == ["state", "simulate-close-empty-atas", "simulate-swap"]
    assert all(s is False for c, s in calls if c != "state") and not (tmp_path / "capital.csv").exists()
    # not flat -> refused before any close, even with --yes
    calls.clear()
    out2 = rr.run(cfg, lambda c, a: calls.append(c) or _st(positions=[{"market": "SOL", "side": "long"}]), send=True)
    assert not out2["ok"] and "refused" in out2["error"] and calls == ["state"]
    # CLI default is a dry run
    calls.clear()
    assert rr.main([], jup=fake) == 0 and all(s is False for c, s in calls if c != "state")


# ------------------------------------------------------------------ TS source guards (persistent token accounts)
def test_ts_keeps_token_accounts_persistent():
    jup = (ROOT / "live/src/jup.ts").read_text()
    inc = jup[jup.index("export async function buildIncrease"):jup.index("export async function buildDecrease")]
    # the wSOL ATA is no longer closed unconditionally after every open/add; only a stray balance is unwrapped and the
    # account is re-created in the same tx
    assert "post.push(createCloseAccountInstruction(fundingAccount, owner, owner));" not in inc
    assert re.search(r"if \(stray > 0n\)\s*post\.push\(createCloseAccountInstruction\(fundingAccount, owner, owner\),\s*"
                     r"createAssociatedTokenAccountIdempotentInstruction\(owner, fundingAccount, owner, NATIVE_MINT\)\)", inc)
    dec = jup[jup.index("export async function buildDecrease"):jup.index("export async function buildCancelRequest")]
    assert "createCloseAccountInstruction" not in dec          # TP payout accounts are never closed
    pf = jup[jup.index("export async function positionFlows"):jup.index("export async function buildCloseEmptyAtas")]
    assert "ownerTokenAccounts(owner)" in pf and "net += d.heldLamports" in pf
    stake = (ROOT / "live/src/stake.ts").read_text()
    sw_ = stake[stake.index("export async function buildSwap"):stake.index("export async function buildSendJupsol")]
    # swap: wSOL ATA re-created in the same tx after Jupiter's unwrap; input ATA never closed by our code
    assert "ixs.push(createAssociatedTokenAccountIdempotentInstruction(owner, wsolAta, owner, NATIVE_MINT));" in sw_
    assert "createCloseAccountInstruction" not in stake
    assert "rentDeltaLamports" in stake
    close = jup[jup.index("export async function buildCloseEmptyAtas"):]
    assert "amount !== 0n" in close and "createCloseAccountInstruction(pk, owner, owner)" in close
    st = jup[jup.index("export async function readState"):jup.index("// ------------------------------------------------------------------ builders")]
    assert "open: false, realisedPnlUsd: null" in st              # closed side never reports a fake 0 gross
    cli = (ROOT / "live/src/cli.ts").read_text()
    assert 'a.send && process.env.JUP_LIVE === "1"' in cli      # sending still needs both flags
