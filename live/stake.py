"""Stake bucket: realized profit kept in the bot wallet as JupSOL (never bot margin) + oversight CLI.

The bot's margin / ladder budget is native SOL only (``state.wallet.sol`` = getBalance lamports); JupSOL sits in its own
token account and is reported separately (``state.stake``). Every stake op is booked in live/stake_ledger.csv and
ledger_summary.json["stake"]; while a trade is open its wallet change is booked as external (not trade P&L).

    .venv/bin/python -m live.stake status
    .venv/bin/python -m live.stake stake 0.002 --dry-run | --yes            (same code path as the daily run)
    .venv/bin/python -m live.stake unstake <jupsol|all> --dry-run | --yes   (JupSOL -> SOL in the bot wallet = margin)
    .venv/bin/python -m live.stake send-to-withdraw <jupsol|all> --dry-run | --yes   (only to the locked address)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):   # allow `python live/stake.py ...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "live"

from config import JUPSOL_MINT, WITHDRAW_ADDRESS, Config, load_config  # noqa: E402
from live import ledger  # noqa: E402

LAMPORTS = 1_000_000_000
log = logging.getLogger("stake")
Jup = Callable[[str, dict[str, Any]], dict[str, Any]]


def _jup() -> Jup:
    from live.live_broker import jup
    return jup


def now_utc() -> str:
    import pandas as pd
    return str(pd.Timestamp.now(tz="UTC"))


def swap_args(cfg: Config, direction: str, atoms: int, slippage_bps: int | None = None) -> dict[str, Any]:
    w = cfg.withdraw
    bps = slippage_bps or (w.stake_slippage_bps if direction == "stake" else w.unstake_slippage_bps)
    if not 1 <= bps <= w.stake_max_slippage_bps:
        raise ValueError(f"slippage {bps} bps outside 1..{w.stake_max_slippage_bps} (withdraw.stake_max_slippage_bps)")
    if atoms <= 0:
        raise ValueError("amount must be > 0")
    return {"direction": direction, "amountAtoms": str(int(atoms)), "slippageBps": int(bps),
            "maxPriceImpactPct": w.stake_max_price_impact_pct, "apiBases": list(w.stake_api_bases),
            "onlyDirectRoutes": w.stake_only_direct_routes}


def _run(jup: Jup, cmd: str, args: dict[str, Any], send: bool) -> dict[str, Any]:
    if send:
        os.environ["JUP_LIVE"] = "1"
    return jup(cmd if send else f"simulate-{cmd}", {**args, "send": send})


def book(kind: str, source: str, res: dict[str, Any], note: str = "", rate: float | None = None) -> dict[str, Any]:
    """Write a confirmed stake op to the stake ledger; book its wallet change as external for an open trade."""
    d = res.get("deltas") or {}
    fee = (d.get("feeLamports") or res.get("feeLamports") or 0) / LAMPORTS
    if d.get("nativeDeltaLamports") is not None:
        chg = d["nativeDeltaLamports"] / LAMPORTS + int(d.get("wsolDeltaAtoms") or 0) / LAMPORTS
        jd = int(d.get("jupsolDeltaAtoms") or 0) / LAMPORTS
    else:   # tx meta unavailable: fall back to the quote (marked in the note)
        note = (note + "; deltas from quote (tx meta unavailable)").strip("; ")
        if kind == "stake":
            chg, jd = -int(res["inAmount"]) / LAMPORTS - fee, int(res["quotedOut"]) / LAMPORTS
        elif kind == "unstake":
            chg, jd = int(res["quotedOut"]) / LAMPORTS - fee, -int(res["inAmount"]) / LAMPORTS
        else:
            chg, jd = -fee, -int(res["amountAtoms"]) / LAMPORTS
    row = {"time": now_utc(), "kind": kind, "source": source, "jupsol_delta": round(jd, 9),
           "wallet_sol_change": round(chg, 9), "fee_sol": round(fee, 9), "sol_per_jupsol": rate,
           "sig": res.get("sig"), "solscan": f"https://solscan.io/tx/{res['sig']}" if res.get("sig") else None, "note": note}
    if kind == "stake":
        row["sol_in"] = int(res["inAmount"]) / LAMPORTS
    elif kind == "unstake":
        row["sol_out"] = round(chg + fee, 9)
    ledger.update(external_out_sol=-chg)   # open trade only (no-op when flat): stake moves are not trade P&L
    totals = ledger.record_stake(row)
    return {**row, "totals": totals}


def stake_sol(cfg: Config, sol: float, source: str = "daily", send: bool = True, jup: Jup | None = None,
              slippage_bps: int | None = None) -> dict[str, Any]:
    """Swap ``sol`` native SOL -> JupSOL into the bot wallet's own JupSOL account. Used by the daily run and the CLI."""
    jup = jup or _jup()
    res = _run(jup, "swap", swap_args(cfg, "stake", int(round(sol * LAMPORTS)), slippage_bps), send)
    out = {k: res.get(k) for k in ("ok", "sig", "err", "error", "inAmount", "quotedOut", "minOut", "route", "api",
                                   "priceImpactPct", "simulated", "feeLamports", "cuPrice")}
    if send and res.get("ok") and res.get("sig"):
        b = book("stake", source, res)
        out.update(sol_in=b["sol_in"], jupsol_out=b["jupsol_delta"], wallet_sol_change=b["wallet_sol_change"],
                   solscan=b["solscan"], totals=b["totals"])
        log.info("STAKE %.9f SOL -> %.9f JupSOL (wallet %+.9f SOL incl. fees/rent) %s", b["sol_in"], b["jupsol_delta"],
                 b["wallet_sol_change"], b["solscan"])
    return out


# ------------------------------------------------------------------ CLI
def _amount_atoms(arg: str, balance_atoms: int) -> int:
    atoms = balance_atoms if arg == "all" else int(round(float(arg) * LAMPORTS))
    if atoms <= 0:
        raise SystemExit("nothing to move (amount/balance is 0)")
    if atoms > balance_atoms:
        raise SystemExit(f"amount {atoms / LAMPORTS} JupSOL > balance {balance_atoms / LAMPORTS}")
    return atoms


def status(cfg: Config, jup: Jup, n_hist: int = 20) -> dict[str, Any]:
    st = jup("state", {})
    sk = st.get("stake") or {}
    atoms = int(sk.get("jupsolAtoms") or 0)
    quote_sol = None
    if atoms > 0:
        q = jup("swap-quote", swap_args(cfg, "unstake", atoms))
        quote_sol = int(q["outAmount"]) / LAMPORTS if q.get("outAmount") else None
    summ = json.loads(ledger.SUMMARY.read_text()) if ledger.SUMMARY.exists() else {}
    wpath = ledger.LIVE / "withdraw_state.json"
    wst = json.loads(wpath.read_text()) if wpath.exists() else {}
    sol = st["wallet"]["sol"]
    return {
        "wallet_native_sol": sol, "bot_margin_sol (native - reserve)": round(sol - cfg.live.sol_fee_reserve, 9),
        "jupsol_balance": sk.get("jupsol"), "jupsol_account": sk.get("ata"),
        "sol_per_jupsol (stake pool)": sk.get("solPerJupsol"), "sol_value_pool_rate": sk.get("solValue"),
        "sol_value_swap_quote": quote_sol, "stake_totals": ledger.stake_totals(),
        "withdraw_mode": cfg.withdraw.mode, "realized_trade_net_sol": summ.get("net_sol"),
        "withdraw_carry_sol": wst.get("withdraw_carry_sol"), "withdraw_last_date": wst.get("withdraw_last_date"),
        "history": ledger.read_stake_rows()[-n_hist:],
    }


def main(argv: list[str] | None = None, jup: Jup | None = None) -> int:
    ap = argparse.ArgumentParser(prog="live.stake", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/live_100.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name, help_ in (("stake", "SOL amount -> JupSOL (bot wallet)"), ("unstake", "JupSOL amount|all -> SOL (bot wallet)"),
                        ("send-to-withdraw", f"JupSOL amount|all -> {WITHDRAW_ADDRESS}")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("amount")
        p.add_argument("--dry-run", action="store_true", help="quote + simulate only")
        p.add_argument("--yes", action="store_true", help="really send (required)")
        if name != "send-to-withdraw":
            p.add_argument("--slippage-bps", type=int, default=None)
        if name == "stake":
            p.add_argument("--source", default="cli")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    jup = jup or _jup()
    pr = lambda x: print(json.dumps(x, indent=1, default=str))  # noqa: E731
    if a.cmd == "status":
        pr(status(cfg, jup))
        return 0
    send = bool(a.yes) and not a.dry_run and not cfg.live.dry_run
    st = jup("state", {})
    if [r for r in st.get("allOpenRequests", st.get("openRequests", [])) if r.get("requestType") != "trigger"]:   # any bot
        raise SystemExit("a market request is pending; try again later")
    bal = int((st.get("stake") or {}).get("jupsolAtoms") or 0)
    if a.cmd == "stake":
        sol = float(a.amount)
        free = st["wallet"]["sol"] - cfg.live.sol_fee_reserve - cfg.withdraw.stake_fee_buffer_sol
        if sol > free:
            raise SystemExit(f"{sol} SOL > free {free:.9f} (wallet - sol_fee_reserve - stake_fee_buffer_sol)")
        res = stake_sol(cfg, sol, a.source, send, jup, a.slippage_bps)
    elif a.cmd == "unstake":
        res = _run(jup, "swap", swap_args(cfg, "unstake", _amount_atoms(a.amount, bal), a.slippage_bps), send)
        if send and res.get("ok") and res.get("sig"):
            res["booked"] = book("unstake", "cli", res, rate=(st.get("stake") or {}).get("solPerJupsol"))
    else:
        if st["wallet"]["sol"] - cfg.live.sol_fee_reserve < 0.003:
            raise SystemExit("not enough free SOL for the transfer fee / recipient token-account rent")
        res = _run(jup, "send-jupsol", {"to": WITHDRAW_ADDRESS, "amountAtoms": str(_amount_atoms(a.amount, bal))}, send)
        if send and res.get("ok") and res.get("sig"):
            res["booked"] = book("send_to_withdraw", "cli", {**res, "amountAtoms": res.get("amountAtoms")},
                                 rate=(st.get("stake") or {}).get("solPerJupsol"))
    if not send:
        res = {"DRY_RUN": True, **res}
        if not a.dry_run and not a.yes:
            res["note"] = "nothing sent: re-run with --yes to execute (or --dry-run to only quote)"
        if cfg.live.dry_run:
            res["note"] = "live.dry_run=true in config: nothing is ever sent"
    pr(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
