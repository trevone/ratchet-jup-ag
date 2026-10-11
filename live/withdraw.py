"""Daily realized-profit SOL withdrawal.

Amount = realized net SOL of trades closed since the last withdraw run (from live/trades.csv) plus any
carried-forward balance. Deposits never count: per trade we use min(net_sol, net_sol_est), where net_sol is
the wallet delta (withdrawals made mid-trade are added back by the ledger) and net_sol_est is the fee-model
estimate. Losses carry forward as a negative balance and offset later profits.

May run with a position open, but only if free wallet SOL - fee reserve - tx fee - next planned add covers
it; otherwise (or below the minimum, or a market request is pending) the amount is carried forward. Minimum: send mode
min_send_sol; stake mode WithdrawConfig.min_stake_sol() (all profit comfortably above swap fee + slippage, floor 0.0005 SOL).
Destination is hard-locked to WITHDRAW_ADDRESS.

withdraw.mode = "stake_jupsol" (live since 2026-10-07): the same amount is instead swapped SOL -> JupSOL into the bot
wallet's own JupSOL account (live/stake.py) and booked in the stake ledger, not as a withdrawal; "send" = old behaviour.

Multi-asset (2026-10-07): only the bot with withdraw.enabled (SOL) runs this, once a day, over ALL bots' trade files
(withdraw.trade_files: live/trades.csv, live/eth/trades.csv, live/btc/trades.csv) with one cutoff per file
(state["withdraw_cutoffs"]; live/trades.csv seeds from the legacy "withdraw_cutoff"). Each bot's net_sol comes from its
own position's txs (ledger.py), so one bot's open-position flows are never another bot's profit. Any bot's in-flight
market request defers the run (allOpenRequests).
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from config import WITHDRAW_ADDRESS, Config

MT = ZoneInfo("America/Denver")
LAMPORTS = 1_000_000_000
TX_FEE_BUFFER_SOL = 0.00002  # base + priority fee for one transfer
TRADES = Path(__file__).resolve().parent / "trades.csv"
log = logging.getLogger("withdraw")


def trade_net_sol(row: dict[str, Any]) -> float:
    net = float(row["net_sol"])
    est = row.get("net_sol_est")
    return min(net, float(est)) if est not in (None, "") else net


def realized_since(rows: Iterable[dict[str, Any]], cutoff: str | None) -> tuple[float, str | None]:
    """Sum of per-trade net SOL for trades closed strictly after ``cutoff``; returns (sum, newest close_time)."""
    c = pd.Timestamp(cutoff) if cutoff else None
    total, newest = 0.0, cutoff
    for r in rows:
        t = pd.Timestamp(r["close_time"])
        if c is None or t > c:
            total += trade_net_sol(r)
            if newest is None or t > pd.Timestamp(newest):
                newest = str(t)
    return round(total, 9), newest


def withdraw_amount_sol(owed_sol: float, wallet_sol: float, reserve_sol: float, min_send_sol: float,
                        next_add_sol: float, pending_requests: int, fee_buffer_sol: float = TX_FEE_BUFFER_SOL,
                        require_next_add_cover: bool = True, allow_partial: bool = False) -> float:
    """SOL to send/stake now (0.0 = carry forward). ``wallet_sol`` is native SOL only. Pure function; unit-tested.

    Hard check always: native SOL after the op stays >= reserve_sol + fee_buffer_sol. require_next_add_cover also keeps
    the next planned add in the wallet. allow_partial: if the whole amount does not fit, take what fits (>= the
    minimum) and the caller carries the rest; else all-or-nothing."""
    if owed_sol < min_send_sol or pending_requests:
        return 0.0
    free = wallet_sol - reserve_sol - fee_buffer_sol - (max(next_add_sol, 0.0) if require_next_add_cover else 0.0)
    if free >= owed_sol:
        return round(owed_sol, 9)
    if allow_partial and free >= min_send_sol:
        return float(int(free * LAMPORTS)) / LAMPORTS   # floor to whole lamports, never above what fits
    return 0.0


def due(now: datetime, time_mt: str, last_run_date: str | None) -> bool:
    local = now.astimezone(MT)
    hh, mm = map(int, time_mt.split(":"))
    return local.time() >= dtime(hh, mm) and last_run_date != str(local.date())


def read_trades(path: Path = TRADES) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


LEGACY_FILE = "live/trades.csv"
ROOT_DIR = Path(__file__).resolve().parents[1]   # trade_files are relative to the repo root (tests point this at tmp)


def realized_all(files: Iterable[str], state: dict, root: Path | None = None) -> tuple[float, dict[str, str | None], dict[str, float]]:
    """Sum of realized net SOL over several bots' trade files, each with its own cutoff.
    Returns (total, new cutoffs per file, per-file amounts)."""
    root = root or ROOT_DIR
    cuts = dict(state.get("withdraw_cutoffs") or {})
    if LEGACY_FILE not in cuts and state.get("withdraw_cutoff"):
        cuts[LEGACY_FILE] = state["withdraw_cutoff"]
    total, per = 0.0, {}
    for f in dict.fromkeys(files):   # de-duplicated, order kept: a file listed twice is counted once
        rows = read_trades(root / f)
        amt, newest = realized_since(rows, cuts.get(f))
        total += amt
        per[f] = amt
        cuts[f] = newest
    return round(total, 9), cuts, per


def run_withdraw(cfg: Config, state: dict, chain: dict, now: datetime, next_add_sol: float = 0.0,
                 rows: list[dict[str, Any]] | None = None, send=None, stake=None) -> dict | None:
    """Called from the bot tick. Mutates ``state`` (cutoff / carry / last date). Returns a log record."""
    w = cfg.withdraw
    if not w.enabled or w.address != WITHDRAW_ADDRESS:
        return None
    if not due(now, w.time_mt, state.get("withdraw_last_date")):
        return None
    multi = rows is None
    if multi:   # every bot's trade file, one cutoff each
        realized, cuts, per_file = realized_all(w.trade_files, state)
        newest = cuts.get(LEGACY_FILE, state.get("withdraw_cutoff"))
    else:       # explicit rows (tests / legacy single file)
        realized, newest = realized_since(rows, state.get("withdraw_cutoff"))
    owed = round(state.get("withdraw_carry_sol", 0.0) + realized, 9)
    sol = chain["wallet"]["sol"]   # native SOL lamports only; JupSOL (chain["stake"]) is never counted
    pos_open = any(p.get("sizeUsd") for p in chain["positions"].values()) or bool(chain.get("allPositions"))
    action = w.action
    min_send = w.min_amount_sol()   # stake: fee/slippage x min_stake_fee_mult (floor 0.0005); send: min_send_sol
    stake_mode = action == "stake_jupsol"   # send mode keeps the old all-or-nothing + next-add cover rule
    amt = withdraw_amount_sol(owed, sol, cfg.live.sol_fee_reserve, min_send, next_add_sol,
                              len([r for r in chain.get("allOpenRequests", chain["openRequests"])
                                   if r.get("requestType") != "trigger"]),   # any bot's in-flight market request
                              TX_FEE_BUFFER_SOL if action == "send" else w.stake_fee_buffer_sol,
                              require_next_add_cover=w.require_next_add_cover or not stake_mode,
                              allow_partial=stake_mode and not w.require_next_add_cover)
    rec: dict = {"date": str(now.astimezone(MT).date()), "action": action, "realized_new": realized, "owed": owed,
                 **({"realized_per_file": per_file} if multi else {}),
                 "wallet_sol": sol, "min_amount_sol": round(min_send, 9), "next_add_sol": round(next_add_sol, 9), "position_open": pos_open, "amount": amt}
    sent = False
    if amt > 0 and action == "stake_jupsol":
        if stake is None:   # books the stake ledger + open-trade external flow itself (live/stake.py)
            from live.stake import stake_sol
            stake = lambda a: stake_sol(cfg, a, source="daily", send=not cfg.live.dry_run)
        res = stake(amt)
        rec["result"] = {k: res.get(k) for k in ("ok", "sig", "err", "error", "sol_in", "jupsol_out", "wallet_sol_change")}
        sent = bool(res.get("ok")) and bool(res.get("sig")) and not cfg.live.dry_run
        if res.get("sig"):
            rec["solscan"] = f"https://solscan.io/tx/{res['sig']}"
        if sent:
            rec["staked_sol"] = amt
            log.info("STAKED profit %.9f SOL -> %s JupSOL %s", amt, res.get("jupsol_out"), rec.get("solscan"))
    elif amt > 0:
        if send is None:
            from live.live_broker import jup
            send = lambda a: jup("transfer", {"to": WITHDRAW_ADDRESS, "lamports": str(int(a * LAMPORTS)),
                                              "send": not cfg.live.dry_run})
        res = send(amt)
        rec["result"] = {k: res.get(k) for k in ("ok", "sig", "err", "error")}
        sent = bool(res.get("ok")) and not cfg.live.dry_run
        if res.get("sig"):
            rec["solscan"] = f"https://solscan.io/tx/{res['sig']}"
            log.info("WITHDRAW %.9f SOL -> %s %s", amt, WITHDRAW_ADDRESS, rec["solscan"])
        if sent and pos_open:
            from live import ledger
            ledger.update(external_out_sol=amt)
    state["withdraw_carry_sol"] = round(owed - amt, 9) if sent else owed   # partial stake: the rest is carried
    rec["carry_after"] = state["withdraw_carry_sol"]
    state["withdraw_cutoff"] = newest
    if multi:
        state["withdraw_cutoffs"] = cuts
    state["withdraw_last_date"] = rec["date"]
    state.setdefault("history", []).append(rec)
    log.info("withdraw check %s", json.dumps(rec, default=str)[:500])
    return rec
