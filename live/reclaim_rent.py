"""Token-account rent reclaim -> JupSOL stake bucket. DRY RUN BY DEFAULT; run live only when EVERY bot is flat.

The bots keep persistent token accounts (wSOL / USDC / ETH / WBTC ATAs, ~0.0015 SOL rent each; live/src/jup.ts). That rent
is the owner's capital (ledger ``rent_sol`` / ``token_rent_sol``), never trade P&L. When all three bots are flat and no
request (market or TP trigger) is open, this helper closes the EMPTY ones (token balance exactly 0; destination and
authority locked to the owner by the TS builder) and stakes the reclaimed SOL into JupSOL in the same wallet
(live/stake.py stake_sol, source "rent_reclaim"). The JupSOL account itself is never closed. Booked as capital in
live/capital.csv (kind rent_reclaim), so the daily realized-profit stake never sees it.

    .venv/bin/python -m live.reclaim_rent                 # dry run: plan + simulate the close + simulate the stake
    .venv/bin/python -m live.reclaim_rent --yes           # live; refused unless every bot is flat with no open requests
    .venv/bin/python -m live.reclaim_rent --no-stake ...  # reclaim only (SOL stays native)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "live"

from config import Config, load_config  # noqa: E402

LAMPORTS = 1_000_000_000
CLOSABLE = ("USDC", "ETH", "WBTC", "WSOL")      # bot token accounts; JupSOL (the stake bucket) is never closed
CAPITAL = Path(__file__).resolve().parent / "capital.csv"
Jup = Callable[[str, dict[str, Any]], dict[str, Any]]


def rent_held(st: dict[str, Any]) -> dict[str, Any]:
    """Rent (lamports) currently parked in the owner's token accounts, from a TS ``state`` result."""
    toks = st.get("sweepTokens") or {}
    per = {n: int((toks.get(n) or {}).get("lamports") or 0) for n in CLOSABLE if isinstance(toks.get(n), dict)}
    jl = int((st.get("stake") or {}).get("ataLamports") or 0)
    if jl:
        per["JUPSOL"] = jl
    return {"per_account_lamports": per, "total_sol": sum(per.values()) / LAMPORTS,
            "reclaimable_sol": sum(v for k, v in per.items() if k != "JUPSOL") / LAMPORTS}


def plan_reclaim(st: dict[str, Any], names: tuple[str, ...] = CLOSABLE) -> dict[str, Any]:
    """Which empty token accounts may be closed now. Blocked while any position or request (incl. TP triggers, whose
    payouts land in these accounts) is open anywhere on the wallet, or if the token state could not be read."""
    blocked = []
    if st.get("allPositions"):
        blocked.append(f"{len(st['allPositions'])} open position(s): {[(p.get('market'), p.get('side')) for p in st['allPositions']]}")
    if st.get("allOpenRequests", st.get("openRequests")):
        blocked.append(f"{len(st.get('allOpenRequests', st.get('openRequests')))} open request(s) (TP payouts may land in these accounts)")
    toks = st.get("sweepTokens") or {}
    if not toks or "error" in toks:
        blocked.append(f"token accounts unreadable: {toks.get('error') if toks else 'missing'}")
    close, skip, lam = [], [], 0
    for n in names:
        if n not in CLOSABLE:
            raise ValueError(f"{n} is not a closable bot token account")
        t = toks.get(n) or {}
        if not t.get("exists"):
            skip.append({"name": n, "why": "absent"})
        elif int(t.get("atoms") or 0) != 0:
            skip.append({"name": n, "why": f"balance {t.get('atoms')} atoms (sweep it first)"})
        else:
            close.append(n)
            lam += int(t.get("lamports") or 0)
    return {"blocked": blocked, "close": close, "skipped": skip, "reclaim_lamports": lam, "reclaim_sol": lam / LAMPORTS}


def stake_amount_sol(reclaim_sol: float, native_sol_after: float, reserve_sol: float, fee_buffer_sol: float) -> float:
    """Stake at most the reclaimed rent, never touching the SOL fee reserve (+ swap fee buffer)."""
    free = native_sol_after - reserve_sol - fee_buffer_sol
    return max(0.0, float(int(min(reclaim_sol, free) * LAMPORTS)) / LAMPORTS)


def record_capital(time_mt: str, sig: str | None, sol: float, note: str, path: Path = CAPITAL) -> None:
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_mt", "sig", "from", "sol", "kind", "note"])
        w.writerow([time_mt, sig, "own token accounts", f"{sol:.9f}", "rent_reclaim", note])


def run(cfg: Config, jup: Jup, send: bool, stake: bool = True, names: tuple[str, ...] = CLOSABLE) -> dict[str, Any]:
    st = jup("state", {"market": "SOL"})
    if "wallet" not in st:
        return {"ok": False, "error": f"state read failed: {st}"}
    held, p = rent_held(st), plan_reclaim(st, names)
    out: dict[str, Any] = {"dry_run": not send, "rent_held": held, "plan": p}
    if p["blocked"]:
        out.update(ok=False, error="refused: " + "; ".join(p["blocked"]))
        return out
    if not p["close"]:
        out.update(ok=True, note="nothing to close")
        return out
    if send:
        os.environ["JUP_LIVE"] = "1"
    res = jup("close-empty-atas" if send else "simulate-close-empty-atas", {"names": p["close"], "send": send})
    out["close"] = {k: res.get(k) for k in ("ok", "sig", "err", "error", "closed", "skipped", "reclaimLamports", "unitsConsumed")}
    if not res.get("ok"):
        out["ok"] = False
        return out
    reclaim = int(res.get("reclaimLamports") or p["reclaim_lamports"]) / LAMPORTS
    if send:
        import pandas as pd
        record_capital(str(pd.Timestamp.now(tz="America/Denver"))[:19], res.get("sig"), reclaim,
                       f"rent of closed empty token accounts {p['close']}: capital, not profit")
    native_after = float(st["wallet"]["sol"]) + reclaim   # dry run: as if the close had landed
    amt = stake_amount_sol(reclaim, native_after, cfg.live.sol_fee_reserve, cfg.withdraw.stake_fee_buffer_sol)
    out["stake_amount_sol"] = amt
    if stake and amt > 0:
        from live.stake import stake_sol
        r = stake_sol(cfg, amt, source="rent_reclaim", send=send, jup=jup)
        out["stake"] = {k: r.get(k) for k in ("ok", "sig", "err", "error", "quotedOut", "simulated", "jupsol_out", "solscan")}
    out["ok"] = True
    return out


def main(argv: list[str] | None = None, jup: Jup | None = None) -> int:
    ap = argparse.ArgumentParser(prog="live.reclaim_rent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/live_100.toml")
    ap.add_argument("--yes", action="store_true", help="really close + stake (default: dry run)")
    ap.add_argument("--no-stake", action="store_true")
    ap.add_argument("--names", default=",".join(CLOSABLE))
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if jup is None:
        from live.live_broker import jup as _j
        jup = _j
    send = bool(a.yes) and not cfg.live.dry_run
    res = run(cfg, jup, send, stake=not a.no_stake, names=tuple(x.strip().upper() for x in a.names.split(",") if x.strip()))
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
