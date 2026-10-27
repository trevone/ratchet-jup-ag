"""Per-bot own-coin ledger + high-water mark (Trevor 2026-10-08 21:37 MT; [ledger] in the configs).

Each bot owns a balance in its OWN coin, kept in its state.json["bank"] (survives restarts and flat resets):

    SOL bot -> SOL (native lamports)      ETH bot -> Wormhole ETH 7vfCXT.. (8 dec)      BTC bot -> WBTC 3NZ9JM.. (8 dec)

  bal      free native balance (NOT counting what sits in the open position)
  hwm      high-water mark (native). Raised ONLY while flat: hwm = max(hwm, bal - fee debt). Capital moves (external
           deposit / withdrawal, daily JupSOL withdrawal) shift it; trading never lowers it.
  wd_mark  hwm right after the last daily withdrawal (or the seed): the daily job withdraws
           withdraw_fraction x max(0, hwm - wd_mark) and then sets wd_mark = the new hwm.
  fee_sol  ETH / BTC bots: SOL network fees they spent (the wallet pays fees in SOL) not yet settled; counted against
           their equity in native units (fee_sol x SOL px / coin px) and settled by a native -> SOL swap once >= min.

Flows (how bal changes):
  * open / add: bal -= collateral actually sent (lamports / ETH / WBTC atoms; the request funds from the bot's own coin;
    shorts: Jupiter's keeper swaps native -> USDC inside the request, its cost shows up in the position's collateral).
  * payouts: ETH / BTC bots own their token exclusively, so after a payout event (TP fill, trim, close, USDC -> native
    sweep) every change of their token balance during ``window_ticks`` ticks IS the payout (exact, incl. swap fees).
    The SOL bot shares native SOL with fees / rents / the reserve, so it credits an estimate (collateral returned + PnL -
    close fee, at the SOL oracle) and trues the trade up on flat with the position's own on-chain SOL flows (ledger.py),
    again when the row is booked; ALWAYS applied, any size (2026-10-27). Flows unreadable -> bank["trueup_block"]:
    the daily withdrawal is blocked and the HWM held until it is applied. Request rent (REQ_RENT) is reserve float,
    never in bal: a cancelled resting add refunds only its collateral, unless a true-up already counted it as gone.
  * reconciliation (every tick, nothing in flight): ETH / BTC: wallet token - bal; SOL: implied reserve = native SOL -
    bal - other bots' unsettled shares of external events. A change that persists 2 ticks and is >= ext_min_usd is
    unexplained. ext_flow_mode = "alert" (live, Trevor 2026-10-11 19:08 MT): WARNING + live/EXT_FLOW_ALERT only, banks
    untouched, no swaps. Old "split" mode: an EXTERNAL deposit / withdrawal, never profit: a SOL change is split ext_split (50/25/25) across the bots'
    capital AND hwm (ETH / BTC swap their share SOL <-> own coin for real via the shared journal live/bank_ext.json);
    an ETH / WBTC change goes to its own bot only. Smaller SOL changes are reserve noise (fees, request rents) and stay
    in the 0.02 SOL reserve float; smaller ETH / WBTC changes are that bot's own trade flow.
Pure helpers are unit-tested (tests/test_bank.py); the bot glue is in live/bot.py (Bot.bank_*).
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")
COIN = {"SOL": "SOL", "ETH": "ETH", "BTC": "WBTC"}
DECIMALS = {"SOL": 9, "ETH": 8, "WBTC": 8}
MINT = {"SOL": "So11111111111111111111111111111111111111112", "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
        "WBTC": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "JUPSOL": "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"}
DUST = {"SOL": 2e-9, "ETH": 2e-8, "WBTC": 2e-8}
ROOT = Path(__file__).resolve().parent.parent              # tests redirect this (conftest)
HIST_MAX = 60


def new_bank(asset: str, bal: float, hwm: float | None = None, note: str = "") -> dict[str, Any]:
    coin = COIN[asset]
    h = bal if hwm is None else hwm
    return {"asset": asset, "coin": coin, "bal": round(bal, 9), "hwm": round(h, 9), "wd_mark": round(h, 9),
            "fee_sol": 0.0, "window": 0, "pend": None, "res_last": None, "trade_dep": 0.0, "trade_recv": 0.0,
            "wd_last_date": None, "ext_done": [], "seeded": str(datetime.now(timezone.utc).replace(tzinfo=None)) + "Z", "seed_note": note,
            "totals": {"ext_in": 0.0, "ext_out": 0.0, "withdrawn": 0.0, "fees_sol": 0.0, "trade_net": 0.0}, "hist": []}


def note(b: dict[str, Any], kind: str, amt: float, why: str = "") -> None:
    b["hist"] = (b.get("hist") or [])[-(HIST_MAX - 1):] + [{"t": str(datetime.now(timezone.utc).replace(tzinfo=None))[:19] + "Z", "kind": kind,
                                                            "amt": round(amt, 9), "bal": round(b["bal"], 9),
                                                            "hwm": round(b["hwm"], 9), "why": why[:160]}]


# ------------------------------------------------------------------ pure accounting
def debit(b: dict[str, Any], amt: float, kind: str = "collateral", why: str = "") -> None:
    """Own trade flow out (collateral sent)."""
    b["bal"] = round(b["bal"] - amt, 9)
    if kind == "collateral":
        b["trade_dep"] = round(b.get("trade_dep", 0.0) + amt, 9)
    note(b, kind, -amt, why)


def credit(b: dict[str, Any], amt: float, kind: str = "payout", why: str = "") -> None:
    """Own trade flow in (payout, payout correction; may be negative for a correction)."""
    b["bal"] = round(b["bal"] + amt, 9)
    b["trade_recv"] = round(b.get("trade_recv", 0.0) + amt, 9)
    b["totals"]["trade_net"] = round(b["totals"].get("trade_net", 0.0) + amt, 9)
    note(b, kind, amt, why)


def apply_external(b: dict[str, Any], amt: float, why: str = "") -> None:
    """External deposit (+) / withdrawal (-) in native units: capital, not P&L -> bal, hwm and wd_mark all move."""
    b["bal"] = round(b["bal"] + amt, 9)
    b["hwm"] = round(b["hwm"] + amt, 9)
    b["wd_mark"] = round(b["wd_mark"] + amt, 9)
    k = "ext_in" if amt >= 0 else "ext_out"
    b["totals"][k] = round(b["totals"].get(k, 0.0) + abs(amt), 9)
    note(b, "external", amt, why)


def fee_debt_native(b: dict[str, Any], sol_px: float, coin_px: float) -> float:
    return b.get("fee_sol", 0.0) * sol_px / coin_px if b["coin"] != "SOL" and coin_px > 0 else 0.0


def update_hwm(b: dict[str, Any], flat: bool, debt_native: float = 0.0) -> bool:
    """HWM only rises and only while flat (and nothing pending): hwm = max(hwm, bal - fee debt). True if raised."""
    if not flat:
        return False
    v = round(b["bal"] - debt_native - loan_outstanding(b) + ib_adj(b), 9)   # JupSOL rescue loan is a liability, never HWM
    if v > b["hwm"] + 1e-12:
        old = b["hwm"]
        b["hwm"] = v
        note(b, "hwm", v - old, f"flat: hwm {old:.9f} -> {v:.9f}")
        return True
    return False


def close_value_native(side: int, size: float, avg: float, margin: float, price: float, close_fee: float,
                       coin_px: float, swap_cost: float = 0.0, network_native: float = 0.0) -> float:
    """What the position returns in the bot's coin if closed now: collateral + PnL - close fee (USD) at the coin price,
    less the USDC -> coin swap cost (shorts of ETH / BTC) and the remaining network cost."""
    if size <= 0:
        return 0.0
    usd = margin + side * size * (price - avg) / avg - close_fee * size
    return max(usd, 0.0) / coin_px * (1.0 - swap_cost) - network_native


def equity(b: dict[str, Any], close_native: float, debt_native: float = 0.0) -> float:
    return b["bal"] - debt_native - loan_outstanding(b) + ib_adj(b) + close_native


def withdraw_amount(b: dict[str, Any], fraction: float, min_native: float) -> tuple[float, float]:
    """(amount to withdraw now, HWM gain since the last withdrawal). 0 below min_native (dust: carried, mark kept) or
    when the free balance cannot cover it."""
    gain = max(0.0, b["hwm"] - b["wd_mark"])
    amt = round(fraction * gain, 9)
    if amt <= 0 or amt < min_native or amt > b["bal"] - loan_outstanding(b) + min(0.0, ib_adj(b)) + 1e-12:
        return 0.0, gain
    return amt, gain


def apply_withdrawal(b: dict[str, Any], amt_in: float, why: str = "") -> None:
    """Withdrawn to JupSOL: capital leaves the bot -> bal and hwm drop by it; the new mark is the new hwm."""
    b["bal"] = round(b["bal"] - amt_in, 9)
    b["hwm"] = round(b["hwm"] - amt_in, 9)
    b["wd_mark"] = b["hwm"]
    b["totals"]["withdrawn"] = round(b["totals"].get("withdrawn", 0.0) + amt_in, 9)
    note(b, "withdraw", -amt_in, why)


# ------------------------------------------------------------------ inter-bot loan (Trevor 2026-10-12 18:09 MT)
IB_FILE = "live/ib_loan.json"
IB_MIN_USD = 2.0            # smallest request / top-up (Trevor 2026-10-15, re-confirmed 2026-10-25: no loan cap)
IB_TOPUP_TIMEOUT_S = 1800   # a pending / stuck top-up is cancelled / reverted after ~30 min
IB_PROBE_STEPS = 100        # 2026-10-25: next-rung SEARCH bound (cash + 1..100 rungs), NOT a loan cap (no cap, Trevor 2026-10-15)


def ib_need_usd(rung_margin: float, cash: float) -> float:
    """Loan size for the next rung: its collateral minus free cash + $1 buffer, min IB_MIN_USD. No cap."""
    return max(IB_MIN_USD, float(rung_margin) - float(cash) + 1.0)


def ib_adj(b: dict[str, Any]) -> float:
    """Inter-bot loan in this bank's own coin: lender +receivable (SOL lent), borrower -payable (coin received).
    Neither side's HWM / equity / withdrawable moves because of the loan."""
    ln = b.get("ib_loan")
    if not ln:
        return 0.0
    return float(ln["sol"]) if ln["role"] == "lender" else -float(ln["coin"])


# ------------------------------------------------------------------ JupSOL rescue loan (Trevor 2026-10-11)
def loan_outstanding(b: dict[str, Any]) -> float:
    return float((b.get("loan") or {}).get("principal", 0.0))


def loan_open(b: dict[str, Any], amt: float, jupsol: float, margin_pct: float, why: str = "") -> None:
    """Lent JupSOL arrives as own coin: bal rises, a liability of the same size is booked; hwm / wd_mark untouched."""
    ln = b.get("loan") or {"principal": 0.0, "jupsol": 0.0, "hwm_at_open": b["hwm"], "opened": None, "n": 0}
    ln["principal"] = round(ln["principal"] + amt, 9)
    ln["jupsol"] = round(ln.get("jupsol", 0.0) + jupsol, 9)
    ln["margin_pct"] = margin_pct
    ln["opened"] = ln["opened"] or str(datetime.now(timezone.utc).replace(tzinfo=None))[:19] + "Z"
    ln["n"] = ln.get("n", 0) + 1
    b["loan"] = ln
    b["bal"] = round(b["bal"] + amt, 9)
    note(b, "loan_in", amt, why)


def loan_repay(b: dict[str, Any], paid: float, why: str = "") -> None:
    """Repaid to JupSOL (principal + margin): bal drops by paid, liability cleared. The margin is a cost of the rescue;
    hwm is untouched (it only rises again when flat from real profit)."""
    ln = b.get("loan") or {}
    b["bal"] = round(b["bal"] - paid, 9)
    done = (b.get("loans_done") or []) + [{**ln, "repaid": round(paid, 9),
                                           "closed": str(datetime.now(timezone.utc).replace(tzinfo=None))[:19] + "Z"}]
    b["loans_done"] = done[-10:]
    b.pop("loan", None)
    note(b, "loan_repay", -paid, why)


def withdraw_min_native(cost_pct: float, network_sol: float, factor: float, sol_px: float, coin_px: float) -> float:
    """Smallest withdrawal (native) worth sending: amount_usd > factor x (amount_usd x cost_pct + network_sol x SOL px)
    (Trevor 2026-10-11 06:53 MT; replaces the fixed $0.50 minimum)."""
    k = 1.0 - factor * cost_pct
    if k <= 0:
        return float("inf")
    return factor * network_sol * sol_px / k / coin_px


def due(now: datetime, time_mt: str, last_date: str | None) -> bool:
    local = now.astimezone(MT)
    hh, mm = map(int, time_mt.split(":"))
    return local.time() >= dtime(hh, mm) and last_date != str(local.date())


def persisted(pend: dict[str, Any] | None, diff: float, dust: float) -> bool:
    """Same unexplained change seen on two consecutive ticks (2 % / dust tolerance)."""
    return bool(pend) and abs(diff - pend["d"]) <= max(dust * 10, 0.02 * abs(diff))


def alert_path() -> Path:
    return ROOT / "live" / "EXT_FLOW_ALERT"


def write_alert(asset: str, text: str) -> str:
    """ext_flow_mode = alert: record an unexplained wallet change in live/EXT_FLOW_ALERT (appended; delete it once
    resolved). Banks are NOT touched."""
    line = f"{str(datetime.now(timezone.utc).replace(tzinfo=None))[:19]}Z {asset}: UNEXPLAINED WALLET CHANGE {text}"
    p = alert_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(line + "\n")
    return line


def recon_token(b: dict[str, Any], wallet: float, coin_px: float, ext_min_usd: float, busy: bool,
                mode: str = "alert") -> dict[str, Any]:
    """ETH / BTC bot, own token exclusive: wallet token vs bal. Returns {"action": ..., "diff": ...}.
    window open -> trade flow; else 2-tick persistence: >= ext_min_usd external (own bot only), smaller own trade flow."""
    dust = DUST[b["coin"]]
    diff = round(wallet - b["bal"], 9)
    if busy:
        return {"action": "wait", "diff": diff}
    if abs(diff) <= dust:
        b["pend"] = None
        b.pop("ext_alert", None)
        return {"action": "none", "diff": diff}
    if mode == "alert" and b.get("ext_alert") and persisted(b["ext_alert"], diff, dust) and b.get("window", 0) <= 0:
        return {"action": "alert_known", "diff": diff}   # already alerted; bank untouched
    if b.get("window", 0) > 0:
        credit(b, diff, "payout", "own-token change in payout window")
        b["pend"] = None
        return {"action": "payout", "diff": diff}
    if persisted(b.get("pend"), diff, dust):
        b["pend"] = None
        if abs(diff) * coin_px >= ext_min_usd and mode == "alert":
            b["ext_alert"] = {"d": diff}
            return {"action": "alert", "diff": diff}
        if abs(diff) * coin_px >= ext_min_usd:
            apply_external(b, diff, f"unexplained {b['coin']} wallet change {diff:+.9f} (~${diff * coin_px:+.2f})")
            return {"action": "external", "diff": diff}
        credit(b, diff, "adjust", "small unexplained own-token change (own flow)")
        return {"action": "adjust", "diff": diff}
    b["pend"] = {"d": diff}
    return {"action": "pending", "diff": diff}


REQ_RENT = 0.0037236       # Jupiter Perps request account rent (any bot's TP / resting add / open); refunded on execute / cancel
POS_RENT = 0.00174752      # position account rent: paid once when the PDA is first created; the account then persists across
                           # trades (verified 2026-10-15: an open tx moves only the request rent), so it is NOT part of rent_held


def rent_held(st: dict[str, Any]) -> float:
    """SOL the shared wallet has locked as Perps request-account rent right now (all three bots), read on-chain by the
    same state call as the wallet balance (allOpenRequests). Position accounts persist and are not counted."""
    return round(len(st.get("allOpenRequests") or []) * REQ_RENT, 9)


def recon_sol(b: dict[str, Any], wallet_sol: float, pending_others: float, sol_px: float, ext_min_usd: float,
              busy: bool, rent: float = 0.0) -> dict[str, Any]:
    """SOL bot: implied reserve = native SOL - bal - others' unsettled external shares. A jump that persists 2 ticks and
    is >= ext_min_usd = external (caller posts the split); smaller jumps are reserve float (fees, request rents)."""
    implied = round(wallet_sol + rent - b["bal"] - pending_others, 9)   # rent locked in request accounts is still ours: ADD it back
    if b.get("res_last") is None or b.get("res_v") != 3:   # baseline from an older formula (v3 = +rent, 2026-10-15): re-init once
        b["res_v"] = 3
        b["res_last"] = implied
        return {"action": "init", "diff": 0.0, "implied": implied}
    d = round(implied - b["res_last"], 9)
    if abs(d) * sol_px < ext_min_usd:
        b["res_last"], b["pend"] = implied, None
        return {"action": "float", "diff": d, "implied": implied}
    if busy or b.get("window", 0) > 0:
        return {"action": "wait", "diff": d, "implied": implied}
    if persisted(b.get("pend"), d, DUST["SOL"]):
        b["pend"] = None
        return {"action": "external", "diff": d, "implied": implied}
    b["pend"] = {"d": d}
    return {"action": "pending", "diff": d, "implied": implied}


def split_shares(delta_sol: float, split: tuple[float, float, float]) -> dict[str, float]:
    s = {"SOL": round(delta_sol * split[0], 9), "ETH": round(delta_sol * split[1], 9)}
    s["BTC"] = round(delta_sol - s["SOL"] - s["ETH"], 9)
    return s


# ------------------------------------------------------------------ shared external-event journal (live/bank_ext.json)
def _jpath() -> Path:
    return ROOT / "live" / "bank_ext.json"


@contextmanager
def journal_lock() -> Iterator[None]:
    p = ROOT / "live" / "bank.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_journal() -> dict[str, Any]:
    try:
        j = json.loads(_jpath().read_text())
    except (OSError, ValueError):
        j = {}
    j.setdefault("events", [])
    return j


def save_journal(j: dict[str, Any]) -> None:
    p = _jpath()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(j, indent=1))
    os.replace(tmp, p)


def post_external(delta_sol: float, sol_px: float, split: tuple[float, float, float], why: str) -> dict[str, Any]:
    """SOL bot: record an external SOL change; ETH / BTC settle their shares (real swaps) and mark them."""
    with journal_lock():
        j = load_journal()
        ev = {"id": (max([e["id"] for e in j["events"]], default=0) + 1), "time": str(datetime.now(timezone.utc).replace(tzinfo=None))[:19] + "Z",
              "delta_sol": round(delta_sol, 9), "sol_px": sol_px, "shares": split_shares(delta_sol, split),
              "settled": {"SOL": round(delta_sol * split[0], 9), "ETH": 0.0, "BTC": 0.0}, "why": why}
        j["events"].append(ev)
        save_journal(j)
    return ev


def pending_others(j: dict[str, Any]) -> float:
    """SOL still owed to (+) / by (-) the ETH and BTC bots for external events (not yet swapped)."""
    return round(sum(e["shares"][a] - e["settled"].get(a, 0.0) for e in j["events"] for a in ("ETH", "BTC")), 9)


def open_shares(j: dict[str, Any], asset: str, min_sol: float) -> list[tuple[int, float]]:
    return [(e["id"], round(e["shares"][asset] - e["settled"].get(asset, 0.0), 9)) for e in j["events"]
            if abs(e["shares"].get(asset, 0.0) - e["settled"].get(asset, 0.0)) > min_sol]


def settle(asset: str, ev_id: int, sol_amount: float, note_: str = "") -> None:
    with journal_lock():
        j = load_journal()
        for e in j["events"]:
            if e["id"] == ev_id:
                e["settled"][asset] = round(e["settled"].get(asset, 0.0) + sol_amount, 9)
                e.setdefault("notes", []).append(f"{asset}: {sol_amount:+.9f} SOL {note_}"[:200])
        save_journal(j)


def status_line(b: dict[str, Any], coin_px: float, sol_px: float, eq: float | None = None, target: float | None = None) -> str:
    debt = fee_debt_native(b, sol_px, coin_px)
    return (f"BANK {b['asset']} ({b['coin']}): bal {b['bal']:.9f} (${b['bal'] * coin_px:.2f}) | hwm {b['hwm']:.9f} "
            f"(${b['hwm'] * coin_px:.2f}) | wd_mark {b['wd_mark']:.9f} (gain since wd {max(0.0, b['hwm'] - b['wd_mark']):.9f})"
            + (f" | fee debt {b.get('fee_sol', 0.0):.9f} SOL (= {debt:.9f} {b['coin']})" if b['coin'] != 'SOL' else
               f" | reserve float {b.get('res_last')}")
            + (f" | equity if closed now {eq:.9f} (${eq * coin_px:.2f})" if eq is not None else "")
            + (f" | guard target {target:.9f} (need {target - eq:+.9f})" if target is not None and eq is not None else ""))
