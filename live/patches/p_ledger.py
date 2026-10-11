"""Integration patch (multi-asset) for live/ledger.py and live/withdraw.py. Usage: python p_ledger.py <root>"""
import sys
from pathlib import Path
R = Path(sys.argv[1])


def rep(rel, old, new, count=1):
    p = R / rel
    s = p.read_text()
    n = s.count(old)
    if n != count:
        raise SystemExit(f"{rel}: expected {count} occurrence(s), found {n}: {old[:80]!r}")
    p.write_text(s.replace(old, new))


# ---------------------------------------------------------------- ledger.py
rep("live/ledger.py", '''One row per position (open -> flat). Network fees are exact (from each confirmed tx); Jupiter open/close
fees and borrow are estimates (6 bps of size per open/close, custody borrow rate); the authoritative
result is ``net_sol`` = wallet SOL after flat minus wallet SOL right before the open.
"""''', '''One row per position (open -> flat). Network fees are exact (from each confirmed tx); Jupiter open/close
fees and borrow are estimates (6 bps of size per open/close, custody borrow rate); the authoritative
result is ``net_sol``.

Multi-asset (2026-10-07): the SOL / ETH / BTC bots share one wallet, so each bot has its own ledger dir
(``configure(state_dir)``: live/ for SOL, live/eth/, live/btc/) and ``net_sol`` is the owner's SOL flow of the txs that
touched this trade's own position account since its open tx (live/flows.py, ``net_sol_method`` = position_flows).
Only if that read fails it falls back to the old wallet delta (``wallet_delta``), which on a shared wallet can include
other bots' flows; the withdraw's min(net_sol, net_sol_est) still caps it at the fee-model estimate.
"""''')
rep("live/ledger.py", '''          "net_sol_est"]   # fee-model estimate; the withdraw uses min(net_sol, net_sol_est) so a mid-trade deposit never counts as profit
''', '''          "net_sol_est",   # fee-model estimate; the withdraw uses min(net_sol, net_sol_est) so a mid-trade deposit never counts as profit
          "asset", "position", "net_sol_method"]   # multi-asset: market, position account, position_flows | wallet_delta
ASSET = "SOL"


def configure(state_dir: str | Path, asset: str = "SOL") -> None:
    """Point the trade ledger at one bot's state dir (called once from live.bot main; never from tests). The stake
    ledger (STAKE) stays live/stake_ledger.csv: only the SOL bot runs the daily stake."""
    global TRADES, SUMMARY, OPEN, ASSET
    d = Path(state_dir)
    d = d if d.is_absolute() else LIVE.parent / d
    d.mkdir(parents=True, exist_ok=True)
    TRADES, SUMMARY, OPEN, ASSET = d / "trades.csv", d / "ledger_summary.json", d / "ledger_open.json", asset
''')
rep("live/ledger.py", '''def on_open(time: str, side: int, wallet_sol_before: float, sig: str | None, fee_lamports: int | None) -> None:
    OPEN.write_text(json.dumps({"open_time": time, "side": "long" if side > 0 else "short", "entries": 1,''',
'''def on_open(time: str, side: int, wallet_sol_before: float, sig: str | None, fee_lamports: int | None,
            position: str | None = None) -> None:
    OPEN.write_text(json.dumps({"open_time": time, "side": "long" if side > 0 else "short", "entries": 1,
                                "asset": ASSET, "position": position,''')
rep("live/ledger.py", '''def on_flat(time: str, wallet_sol_after: float, price: float, gross_pnl_usd: float | None,
            borrow_rate_per_hour: float) -> dict[str, Any] | None:
    rec = _load(OPEN, None)
    if rec is None:
        return None
    hours = (__import__("pandas").Timestamp(time) - __import__("pandas").Timestamp(rec["open_time"])).total_seconds() / 3600
    net_sol = wallet_sol_after - rec["wallet_sol_before"] + rec.get("external_out_sol", 0.0)''',
'''def on_flat(time: str, wallet_sol_after: float, price: float, gross_pnl_usd: float | None,
            borrow_rate_per_hour: float, flows_net_sol: float | None = None) -> dict[str, Any] | None:
    """``price`` = SOL price (USD -> SOL conversion for every asset). ``flows_net_sol`` = this position's own SOL flows
    (live/flows.py); None -> old wallet delta."""
    rec = _load(OPEN, None)
    if rec is None:
        return None
    hours = (__import__("pandas").Timestamp(time) - __import__("pandas").Timestamp(rec["open_time"])).total_seconds() / 3600
    if flows_net_sol is not None:
        net_sol, method = flows_net_sol, "position_flows"
    else:
        net_sol, method = wallet_sol_after - rec["wallet_sol_before"] + rec.get("external_out_sol", 0.0), "wallet_delta"
    rec = {**rec, "asset": rec.get("asset") or ASSET, "net_sol_method": method}''')

# ---------------------------------------------------------------- withdraw.py
rep("live/withdraw.py", '''wallet's own JupSOL account (live/stake.py) and booked in the stake ledger, not as a withdrawal; "send" = old behaviour.
"""''', '''wallet's own JupSOL account (live/stake.py) and booked in the stake ledger, not as a withdrawal; "send" = old behaviour.

Multi-asset (2026-10-07): only the bot with withdraw.enabled (SOL) runs this, once a day, over ALL bots' trade files
(withdraw.trade_files: live/trades.csv, live/eth/trades.csv, live/btc/trades.csv) with one cutoff per file
(state["withdraw_cutoffs"]; live/trades.csv seeds from the legacy "withdraw_cutoff"). Each bot's net_sol comes from its
own position's txs (ledger.py), so one bot's open-position flows are never another bot's profit. Any bot's in-flight
market request defers the run (allOpenRequests).
"""''')
rep("live/withdraw.py", '''def read_trades(path: Path = TRADES) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))
''', '''def read_trades(path: Path = TRADES) -> list[dict[str, Any]]:
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
''')
rep("live/withdraw.py", '''    rows = read_trades() if rows is None else rows
    realized, newest = realized_since(rows, state.get("withdraw_cutoff"))''', '''    multi = rows is None
    if multi:   # every bot's trade file, one cutoff each
        realized, cuts, per_file = realized_all(w.trade_files, state)
        newest = cuts.get(LEGACY_FILE, state.get("withdraw_cutoff"))
    else:       # explicit rows (tests / legacy single file)
        realized, newest = realized_since(rows, state.get("withdraw_cutoff"))''')
rep("live/withdraw.py", '''    pos_open = any(p.get("sizeUsd") for p in chain["positions"].values())''',
'''    pos_open = any(p.get("sizeUsd") for p in chain["positions"].values()) or bool(chain.get("allPositions"))''')
rep("live/withdraw.py", '''                              len([r for r in chain["openRequests"] if r.get("requestType") != "trigger"]),''',
'''                              len([r for r in chain.get("allOpenRequests", chain["openRequests"])
                                   if r.get("requestType") != "trigger"]),   # any bot's in-flight market request''')
rep("live/withdraw.py", '''    rec: dict = {"date": str(now.astimezone(MT).date()), "action": action, "realized_new": realized, "owed": owed,''',
'''    rec: dict = {"date": str(now.astimezone(MT).date()), "action": action, "realized_new": realized, "owed": owed,
                 **({"realized_per_file": per_file} if multi else {}),''')
rep("live/withdraw.py", '''    state["withdraw_cutoff"] = newest
''', '''    state["withdraw_cutoff"] = newest
    if multi:
        state["withdraw_cutoffs"] = cuts
''')
rep("live/ledger.py", '''    if kw.get("external_out_sol"):  # SOL sent out of the wallet mid-trade (profit withdraw): not a trade loss''',
'''    if kw.get("sweep_sol"):  # USDC / ETH / WBTC proceeds swapped back to SOL (live/sweep.py): part of this trade's result
        rec["sweep_sol"] = round(rec.get("sweep_sol", 0.0) + float(kw["sweep_sol"]), 9)
    if kw.get("external_out_sol"):  # SOL sent out of the wallet mid-trade (profit withdraw): not a trade loss''')
rep("live/ledger.py", '''    rec = {**rec, "asset": rec.get("asset") or ASSET, "net_sol_method": method}''',
'''    net_sol += rec.get("sweep_sol", 0.0)   # swap tx of non-SOL proceeds never touches the position / wallet_after
    rec = {**rec, "asset": rec.get("asset") or ASSET, "net_sol_method": method}''')
rep("live/ledger.py", '''def on_flat(time: str,''', '''def write_sweep_row(time: str, sol: float, note: str) -> dict[str, Any]:
    """A sweep (non-SOL proceeds -> SOL) that arrived after its trade was already booked: its own row, counted once."""
    row = {"open_time": time, "close_time": time, "side": "sweep", "entries": 0, "adds": 0, "exits": "sweep", "n_txs": 1,
           "net_sol": round(sol, 9), "net_sol_est": round(sol, 9), "asset": ASSET, "net_sol_method": "sweep", "note": note}
    write_row(row)
    return row


def on_flat(time: str,''')
rep("live/stake.py", '''    if [r for r in st.get("openRequests", []) if r.get("requestType") != "trigger"]:''',
'''    if [r for r in st.get("allOpenRequests", st.get("openRequests", [])) if r.get("requestType") != "trigger"]:   # any bot''')
print("ledger.py / withdraw.py / stake.py patched")
