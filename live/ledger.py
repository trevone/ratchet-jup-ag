"""Per-trade ledger for the live bot: live/trades.csv + running totals in live/ledger_summary.json.

One row per position (open -> flat). Network fees are exact (from each confirmed tx); Jupiter open/close
fees and borrow are estimates (6 bps of size per open/close, custody borrow rate); the authoritative
result is ``net_sol``.

Multi-asset (2026-10-07): the SOL / ETH / BTC bots share one wallet, so each bot has its own ledger dir
(``configure(state_dir)``: live/ for SOL, live/eth/, live/btc/) and ``net_sol`` is the owner's SOL flow of the txs that
touched this trade's own position account since its open tx (live/flows.py, ``net_sol_method`` = position_flows).
Only if that read fails it falls back to the old wallet delta (``wallet_delta``), which on a shared wallet can include
other bots' flows; the withdraw's min(net_sol, net_sol_est) still caps it at the fee-model estimate.

Token-account rent (2026-10-07): creating a token account (USDC / ETH / WBTC payout ATA, wSOL ATA) parks ~0.0015 SOL rent
in it. That SOL is still in the wallet and comes back when the account is closed, so it is recoverable capital, not trade
P&L: ``net_sol`` excludes it (position flows and sweeps count the owner's token-account lamports as owner SOL) and
``rent_sol`` records how much rent this trade parked (+) or got back (-); ledger_summary ``token_rent_sol`` keeps the
running total. ``gross_pnl_usd`` comes from the program's decrease events (``gross_pnl_source`` = position_events) since
a fully closed position account no longer holds its realized P&L (it read 0 before, which made net_sol_est fees-only).
"""
from __future__ import annotations

import csv
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LIVE = Path(__file__).resolve().parent
TRADES = LIVE / "trades.csv"
SUMMARY = LIVE / "ledger_summary.json"
OPEN = LIVE / "ledger_open.json"
JUP_FEE_BPS = 6
LAMPORTS = 1_000_000_000
FIELDS = ["open_time", "close_time", "side", "entries", "adds", "avg_entry", "max_size_usd", "exits",
          "jup_open_close_fees_usd_est", "borrow_usd_est", "network_fee_sol", "n_txs", "gross_pnl_usd",
          "wallet_sol_before", "wallet_sol_after", "net_sol", "sol_price_close", "net_usd_at_close", "open_sig", "note",
          "net_sol_est",   # fee-model estimate; the withdraw uses min(net_sol, net_sol_est) so a mid-trade deposit never counts as profit
          "asset", "position", "net_sol_method",   # multi-asset: market, position account, position_flows | wallet_delta
          "rent_sol",            # token-account rent parked (+) / refunded (-) during the trade: capital, NOT in net_sol
          "gross_pnl_source",    # position_events (program decrease events) | chain_position (position account) | none
          # per-add trim (2026-10-08): trade rows carry the trims' totals + the whole-trade true result (trims + final close
          # + all fees = net_sol / its USD value); side "trim" rows describe one trim (net_sol 0: booked in the trade row)
          "trims", "trim_onchain_pnl_usd", "trim_lot_gain_usd", "true_result_usd", "true_result_sol",
          # own-coin ledger (2026-10-09): ETH / BTC bots trade in their own coin (ETH / WBTC), so their result is the bank's
          # own-coin balance change (received - deposited); net_sol / net_usd are its SOL / USD value (net_sol_method
          # bank_native). The position's SOL flows (fees only, the collateral is not SOL) are kept in flows_net_sol.
          "native_coin", "native_deposited", "native_received", "net_native", "coin_px_close", "flows_net_sol"]
ASSET = "SOL"


def configure(state_dir: str | Path, asset: str = "SOL") -> None:
    """Point the trade ledger at one bot's state dir (called once from live.bot main; never from tests). The stake
    ledger (STAKE) stays live/stake_ledger.csv: only the SOL bot runs the daily stake."""
    global TRADES, SUMMARY, OPEN, ASSET
    d = Path(state_dir)
    d = d if d.is_absolute() else LIVE.parent / d
    d.mkdir(parents=True, exist_ok=True)
    TRADES, SUMMARY, OPEN, ASSET = d / "trades.csv", d / "ledger_summary.json", d / "ledger_open.json", asset


@contextmanager
def at(state_dir: str | Path, asset: str) -> Iterator[None]:
    """Temporarily point the trade ledger at ANOTHER bot's state dir (live/sweep.py books a late sweep into the trade
    that produced the tokens, even when a different bot ran the swap). Bots are single-threaded; restored on exit."""
    global TRADES, SUMMARY, OPEN, ASSET
    saved = (TRADES, SUMMARY, OPEN, ASSET)
    d = Path(state_dir)
    d = d if d.is_absolute() else LIVE.parent / d
    d.mkdir(parents=True, exist_ok=True)
    TRADES, SUMMARY, OPEN, ASSET = d / "trades.csv", d / "ledger_summary.json", d / "ledger_open.json", asset
    try:
        yield
    finally:
        TRADES, SUMMARY, OPEN, ASSET = saved


@contextmanager
def _dir_lock() -> Iterator[None]:
    """Serialise read-modify-write of one ledger dir between processes (2026-10-08: a bot may append a late sweep row to
    another bot's ledger). Short critical sections only; never nested."""
    TRADES.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES.parent / ".ledger.lock", "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def on_open(time: str, side: int, wallet_sol_before: float, sig: str | None, fee_lamports: int | None,
            position: str | None = None) -> None:
    OPEN.write_text(json.dumps({"open_time": time, "side": "long" if side > 0 else "short", "entries": 1,
                                "asset": ASSET, "position": position,
                                "wallet_sol_before": wallet_sol_before, "open_sig": sig, "exits": [],
                                "opened_size_usd": 0.0, "max_size_usd": 0.0, "avg_entry": None,
                                "network_fee_lamports": fee_lamports or 0, "n_txs": 1}, indent=1))


def update(**kw: Any) -> None:
    """Accumulate into the open record: add_fee_lamports, add_entry, exit, size_usd, avg_entry, opened_size."""
    with _dir_lock():
        _update(**kw)


def _update(**kw: Any) -> None:
    rec = _load(OPEN, None)
    if rec is None:
        return
    if kw.get("fee_lamports") is not None:
        rec["network_fee_lamports"] += int(kw["fee_lamports"])
        rec["n_txs"] += 1
    if kw.get("add_entry"):
        rec["entries"] += 1
    if kw.get("exit"):
        rec["exits"].append(kw["exit"])
    if kw.get("size_usd") is not None:
        rec["max_size_usd"] = max(rec["max_size_usd"], kw["size_usd"])
    if kw.get("opened_size_usd") is not None:
        rec["opened_size_usd"] += kw["opened_size_usd"]
    if kw.get("avg_entry") is not None:
        rec["avg_entry"] = kw["avg_entry"]
    if kw.get("sweep_sol"):  # USDC / ETH / WBTC proceeds swapped back to SOL (live/sweep.py): part of this trade's result
        rec["sweep_sol"] = round(rec.get("sweep_sol", 0.0) + float(kw["sweep_sol"]), 9)
    if kw.get("rent_sol"):  # token-account rent moved by this trade's swaps: recoverable capital, not P&L (info)
        rec["rent_sol"] = round(rec.get("rent_sol", 0.0) + float(kw["rent_sol"]), 9)
    if kw.get("external_out_sol"):  # SOL sent out of the wallet mid-trade (profit withdraw): not a trade loss
        rec["external_out_sol"] = rec.get("external_out_sol", 0.0) + float(kw["external_out_sol"])
    OPEN.write_text(json.dumps(rec, indent=1))


def write_trim_row(t: dict[str, Any], sol_price: float, asset: str | None = None) -> dict[str, Any]:
    """One per-add trim: its own trades.csv row (side "trim") with the on-chain realized P&L (Jupiter books it against
    the position average) and the lot-based true gain. net_sol = 0: the money is part of the trade's final row
    (position flows), so the summary's trade count / net_sol are untouched (summary["trims"] counts them). The open
    record accumulates the trim totals for the final row."""
    with _dir_lock():
        rec = _load(OPEN, None) or {}
        lots = t["lots"]
        sz = sum(l["size"] for l in lots) or 1.0
        row = {"open_time": rec.get("open_time") or t["time"], "close_time": t["time"], "side": "trim",
               "entries": len(lots), "adds": len(lots), "avg_entry": round(sz / sum(l["size"] / l["price"] for l in lots), 6),
               "max_size_usd": t["size"], "exits": f"TRIM@{str(t['time'])[11:16]}",
               "jup_open_close_fees_usd_est": round(2 * t["size"] * JUP_FEE_BPS / 10_000, 4), "n_txs": 1,
               "gross_pnl_usd": t["onchain_pnl_usd"], "gross_pnl_source": "trim_onchain", "net_sol": 0.0, "net_sol_est": 0.0,
               "sol_price_close": sol_price, "net_usd_at_close": 0.0, "open_sig": rec.get("open_sig"),
               "position": rec.get("position"), "asset": asset or rec.get("asset") or ASSET, "net_sol_method": "trim",
               "trims": 1, "trim_onchain_pnl_usd": t["onchain_pnl_usd"], "trim_lot_gain_usd": t["lot_gain_usd"],
               "note": (f"trim lots {[(str(l['time'])[:16], round(l['price'], 3), l['size']) for l in lots]} exit {t['exit']} "
                        f"({t.get('exit_src')}) avg {t['avg']}; on-chain {t['onchain_pnl_usd']:+.4f} USD, lot-based true gain "
                        f"{t['lot_gain_usd']:+.4f} USD; money is booked in the trade row")}
        _migrate_header()
        new = not TRADES.exists()
        with open(TRADES, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k) for k in FIELDS})
        s = _load(SUMMARY, {"trades": 0, "net_sol": 0.0, "network_fee_sol": 0.0, "jup_fees_usd_est": 0.0,
                            "borrow_usd_est": 0.0, "gross_pnl_usd": 0.0})
        s["trims"] = s.get("trims", 0) + 1
        SUMMARY.write_text(json.dumps(s, indent=1))
        if rec:
            rec["trims"] = rec.get("trims", 0) + 1
            rec["trim_onchain_pnl_usd"] = round(rec.get("trim_onchain_pnl_usd", 0.0) + t["onchain_pnl_usd"], 4)
            rec["trim_lot_gain_usd"] = round(rec.get("trim_lot_gain_usd", 0.0) + t["lot_gain_usd"], 4)
            rec["trimmed_size_usd"] = round(rec.get("trimmed_size_usd", 0.0) + t["size"], 2)
            OPEN.write_text(json.dumps(rec, indent=1))
    return row


def _migrate_header() -> None:
    if TRADES.exists():
        with open(TRADES, newline="") as f:
            r = csv.DictReader(f)
            if r.fieldnames == FIELDS:
                return
            rows = list(r)
        with open(TRADES, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows({k: x.get(k) for k in FIELDS} for x in rows)


def write_row(row: dict[str, Any], keep_header: bool = False) -> None:
    """Append one row and update the summary. ``keep_header``: append in the file's EXISTING column layout (no header
    migration); used when writing into another bot's ledger, whose running process may still have the older FIELDS
    (columns it lacks, e.g. rent_sol, are then left out of that row; net_sol always fits)."""
    with _dir_lock():
        _write_row(row, keep_header)


def _write_row(row: dict[str, Any], keep_header: bool) -> None:
    fields = FIELDS
    if keep_header and TRADES.exists():
        with open(TRADES, newline="") as f:
            fields = next(csv.reader(f), None) or FIELDS
    else:
        _migrate_header()
    new = not TRADES.exists()
    with open(TRADES, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k) for k in fields})
    s = _load(SUMMARY, {"trades": 0, "net_sol": 0.0, "network_fee_sol": 0.0, "jup_fees_usd_est": 0.0,
                        "borrow_usd_est": 0.0, "gross_pnl_usd": 0.0})
    s["trades"] += 1
    s["net_sol"] = round(s["net_sol"] + float(row["net_sol"]), 9)
    s["network_fee_sol"] = round(s["network_fee_sol"] + float(row.get("network_fee_sol") or 0), 9)
    s["jup_fees_usd_est"] = round(s["jup_fees_usd_est"] + float(row.get("jup_open_close_fees_usd_est") or 0), 4)
    s["borrow_usd_est"] = round(s["borrow_usd_est"] + float(row.get("borrow_usd_est") or 0), 4)
    s["gross_pnl_usd"] = round(s["gross_pnl_usd"] + float(row.get("gross_pnl_usd") or 0), 4)
    s["total_fees_usd_est_excl_network"] = round(s["jup_fees_usd_est"] + s["borrow_usd_est"], 4)
    if row.get("net_native") not in (None, ""):   # own-coin result (ETH / BTC bank_native rows)
        s["net_native"] = round(s.get("net_native", 0.0) + float(row["net_native"]), 9)
        s["native_coin"] = row.get("native_coin")
    if row.get("rent_sol") not in (None, ""):   # recoverable capital parked in token accounts (not part of net_sol)
        s["token_rent_sol"] = round(s.get("token_rent_sol", 0.0) + float(row["rent_sol"]), 9)
    SUMMARY.write_text(json.dumps(s, indent=1))


def write_sweep_row(time: str, sol: float, note: str, rent_sol: float = 0.0, open_sig: str | None = None,
                    position: str | None = None, keep_header: bool = False) -> dict[str, Any]:
    """A sweep (non-SOL proceeds -> SOL) that arrived after its trade was already booked: its own row, counted once.
    ``sol`` excludes token-account rent (``rent_sol``, capital). ``open_sig`` / ``position`` name the trade that produced
    the tokens (2026-10-08 late-sweep attribution); its close_time is the sweep time, so the daily stake counts it once,
    in the run after the sweep (even if the trade itself was already counted)."""
    row = {"open_time": time, "close_time": time, "side": "sweep", "entries": 0, "adds": 0, "exits": "sweep", "n_txs": 1,
           "net_sol": round(sol, 9), "net_sol_est": round(sol, 9), "asset": ASSET, "net_sol_method": "sweep", "note": note,
           "rent_sol": round(rent_sol, 9), "open_sig": open_sig, "position": position}
    write_row(row, keep_header=keep_header)
    return row


def on_flat(time: str, wallet_sol_after: float, price: float, gross_pnl_usd: float | None,
            borrow_rate_per_hour: float, flows_net_sol: float | None = None, flows_rent_sol: float | None = None,
            events_gross_usd: float | None = None, native: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """``price`` = SOL price (USD -> SOL conversion for every asset). ``flows_net_sol`` = this position's own SOL flows
    (live/flows.py, token-account rent already excluded); None -> old wallet delta. ``flows_rent_sol`` = rent parked by
    the position's txs; ``events_gross_usd`` = realized gross P&L from the program's decrease events (preferred over
    ``gross_pnl_usd``, the closed position account's field, which reads 0 once the position is fully closed).
    ``native`` (ETH / BTC bots with the own-coin ledger, 2026-10-09): {coin, deposited, received, coin_px} from the bank;
    the trade result is then received - deposited in the bot's own coin, net_sol = its SOL value at close (coin_px /
    price), net_sol_method = bank_native, no fee-model estimate (the own-coin result is exact; SOL network fees are the
    bank's fee debt, shown in network_fee_sol). The position's SOL flows are kept in flows_net_sol (informational)."""
    rec = _load(OPEN, None)
    if rec is None:
        return None
    if events_gross_usd is not None:
        gross_pnl_usd, gsrc = events_gross_usd, "position_events"
    else:
        gsrc = "chain_position" if gross_pnl_usd else "none"
    rent_sol = round(rec.get("rent_sol", 0.0) + (flows_rent_sol or 0.0), 9)
    hours = (__import__("pandas").Timestamp(time) - __import__("pandas").Timestamp(rec["open_time"])).total_seconds() / 3600
    nat_cols: dict[str, Any] = {}
    if native is not None and price:
        net_native = round(float(native["received"]) - float(native["deposited"]), 9)
        net_sol, method = net_native * float(native["coin_px"]) / price, "bank_native"
        nat_cols = {"native_coin": native["coin"], "native_deposited": round(float(native["deposited"]), 9),
                    "native_received": round(float(native["received"]), 9), "net_native": net_native,
                    "coin_px_close": native["coin_px"], "flows_net_sol": flows_net_sol}
        rec = {**rec, "sweep_sol": 0.0}   # USDC -> own-coin sweeps land in the bank's received; their SOL is only fees
    elif flows_net_sol is not None:
        net_sol, method = flows_net_sol, "position_flows"
    else:
        net_sol, method = wallet_sol_after - rec["wallet_sol_before"] + rec.get("external_out_sol", 0.0), "wallet_delta"
    net_sol += rec.get("sweep_sol", 0.0)   # swap tx of non-SOL proceeds never touches the position / wallet_after
    rec = {**rec, "asset": rec.get("asset") or ASSET, "net_sol_method": method}
    row = {**rec, "close_time": time, "adds": rec["entries"] - 1, "exits": "|".join(rec["exits"]),
           "jup_open_close_fees_usd_est": round(2 * rec["opened_size_usd"] * JUP_FEE_BPS / 10_000, 4),
           "borrow_usd_est": round(rec["max_size_usd"] * borrow_rate_per_hour * hours, 4),
           "network_fee_sol": rec["network_fee_lamports"] / LAMPORTS, "gross_pnl_usd": gross_pnl_usd,
           "wallet_sol_after": wallet_sol_after, "net_sol": round(net_sol, 9), "sol_price_close": price,
           "net_usd_at_close": round(net_sol * price, 4), "rent_sol": rent_sol, "gross_pnl_source": gsrc,
           "trims": rec.get("trims", 0), "trim_onchain_pnl_usd": rec.get("trim_onchain_pnl_usd", 0.0),
           "trim_lot_gain_usd": rec.get("trim_lot_gain_usd", 0.0),
           # whole trade incl. every trim (their decreases are position txs) + final close + all fees
           "true_result_sol": round(net_sol, 9), "true_result_usd": round(net_sol * price, 4), **nat_cols}
    # gross unknown on a position_flows trade -> no fee-only estimate (min(net, est) would cap the profit at -fees);
    # position flows can't contain deposits. A wallet_delta fallback (deposits possible) keeps the conservative estimate
    # with gross 0.
    if price and method != "bank_native" and (gsrc != "none" or method == "wallet_delta"):
        g = gross_pnl_usd if gsrc != "none" else 0.0
        row["net_sol_est"] = round((g - row["jup_open_close_fees_usd_est"] - row["borrow_usd_est"]) / price
                                   - row["network_fee_sol"], 9)
    write_row(row)
    OPEN.unlink()
    return row


# ------------------------------------------------------------------ stake bucket (JupSOL kept in the bot wallet)
# Separate from trades: staking is neither a trade loss, a withdrawal nor a deposit. live/stake_ledger.csv has one row
# per stake op; ledger_summary.json["stake"] keeps the totals. ``net_sol`` (realized trade profit) is never touched.
STAKE = LIVE / "stake_ledger.csv"
STAKE_FIELDS = ["time", "kind", "source", "sol_in", "sol_out", "jupsol_delta", "wallet_sol_change", "fee_sol",
                "sol_per_jupsol", "sig", "solscan", "note"]
STAKE_TOTALS = {"ops": 0, "staked_sol_total": 0.0, "staked_jupsol_total": 0.0, "unstaked_jupsol_total": 0.0,
                "unstaked_sol_total": 0.0, "sent_jupsol_total": 0.0, "net_jupsol": 0.0, "costs_sol_total": 0.0,
                # 2026-10-27 JupSOL loans (live/jupsol_loans.py) and the live-test park tool: bucket movements only
                "lent_jupsol_total": 0.0, "repaid_jupsol_total": 0.0, "parked_jupsol_total": 0.0, "unparked_jupsol_total": 0.0}


def read_stake_rows() -> list[dict[str, Any]]:
    if not STAKE.exists():
        return []
    with open(STAKE, newline="") as f:
        return list(csv.DictReader(f))


def stake_totals() -> dict[str, Any]:
    return {**STAKE_TOTALS, **_load(SUMMARY, {}).get("stake", {})}


def record_stake(row: dict[str, Any]) -> dict[str, Any]:
    """Append one stake-bucket op. kind: stake (SOL -> JupSOL) | unstake (JupSOL -> SOL) | send_to_withdraw (JupSOL out)
    | loan_out (JupSOL lent to a bot) | loan_repay (JupSOL bought back by it) | park / unpark (a bot's capital in / out).
    wallet_sol_change = native + wSOL change of the bot wallet in that tx (fees and token-account rent included)."""
    new = not STAKE.exists()
    with open(STAKE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STAKE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k) for k in STAKE_FIELDS})
    s = _load(SUMMARY, {"trades": 0, "net_sol": 0.0, "network_fee_sol": 0.0, "jup_fees_usd_est": 0.0,
                        "borrow_usd_est": 0.0, "gross_pnl_usd": 0.0})
    t = {**STAKE_TOTALS, **s.get("stake", {})}
    jd, chg = float(row.get("jupsol_delta") or 0), float(row.get("wallet_sol_change") or 0)
    t["ops"] += 1
    if row["kind"] == "stake":
        t["staked_sol_total"] += float(row.get("sol_in") or 0)
        t["staked_jupsol_total"] += jd
        t["costs_sol_total"] += -chg - float(row.get("sol_in") or 0)
    elif row["kind"] == "unstake":
        t["unstaked_jupsol_total"] += -jd
        t["unstaked_sol_total"] += float(row.get("sol_out") or 0)
        t["costs_sol_total"] += float(row.get("sol_out") or 0) - chg
    elif row["kind"] == "send_to_withdraw":
        t["sent_jupsol_total"] += -jd
        t["costs_sol_total"] += -chg
    elif row["kind"] == "loan_out":
        t["lent_jupsol_total"] += -jd
    elif row["kind"] == "loan_repay":
        t["repaid_jupsol_total"] += jd
    elif row["kind"] == "park":
        t["parked_jupsol_total"] += jd
    elif row["kind"] == "unpark":
        t["unparked_jupsol_total"] += -jd
    t["net_jupsol"] = (t["staked_jupsol_total"] - t["unstaked_jupsol_total"] - t["sent_jupsol_total"]
                       - t["lent_jupsol_total"] + t["repaid_jupsol_total"] + t["parked_jupsol_total"] - t["unparked_jupsol_total"])
    s["stake"] = {k: (round(v, 9) if isinstance(v, float) else v) for k, v in t.items()}
    SUMMARY.write_text(json.dumps(s, indent=1))
    return s["stake"]
