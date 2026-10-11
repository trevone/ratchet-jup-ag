"""Time-based per-add trim (Trevor 2026-10-08 13:00 MT): pure helpers, no chain access.

A lot = one entry or add of the open position: {"time" (UTC ISO), "price" (fill), "size" (USD notional), "coll" (USD),
"lev", "first" (True for the opening entry)}. Lot order = fill order.

Accounting note: Jupiter keeps ONE average per position. A partial close books ``side * s * (exit - avg) / avg`` (the
real money, usually a loss below avg) and leaves the avg unchanged, so the money result of the whole trade is always the
on-chain realized P&L of every decrease minus fees. The lot-based gain (lot price -> exit) is the per-add attribution
Trevor tracks; the overall-net-positive TP guard uses the real (on-chain) money.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

LotT = dict[str, Any]


def fill_price(size_old: float, avg_old: float, size_new: float, avg_new: float) -> float | None:
    """Fill price of an add from the position before / after (Jupiter's avg is size-over-qty: qty = size / avg)."""
    q_old = size_old / avg_old if size_old and avg_old else 0.0
    dq = size_new / avg_new - q_old
    ds = size_new - size_old
    return ds / dq if dq > 0 and ds > 0 else None


def lot_gain(lot: LotT, side: int, price: float, open_fee: float, close_fee: float, network_usd: float = 0.0) -> float:
    """Lot-based net gain of closing ``lot`` at ``price``: own gross P&L minus its open + close fee and network est."""
    gross = side * lot["size"] * (price - lot["price"]) / lot["price"]
    return gross - (open_fee + close_fee) * lot["size"] - network_usd


def jup_realized(side: int, size: float, exit_px: float, avg: float) -> float:
    """What Jupiter books for a partial close of ``size`` at ``exit_px`` (against the position average)."""
    return side * size * (exit_px - avg) / avg


def clock_ref(lots: Sequence[LotT], opened: str | None, clock: str, net_pos_last: str | None) -> pd.Timestamp | None:
    """Reference time of the trim clock: the most recent add (or the open when there were no adds) for ``last_add``,
    the open for ``open``; moved forward to the last time the position was seen net positive (the position must stay
    under water for the whole wait)."""
    adds = [l for l in lots if not l.get("first")]
    if clock == "last_add" and adds:
        ref = pd.Timestamp(adds[-1]["time"])
    else:
        first = [l for l in lots if l.get("first")]
        t = opened or (first[0]["time"] if first else None)
        ref = pd.Timestamp(t) if t else None
    if ref is not None and net_pos_last:
        ref = max(ref, pd.Timestamp(net_pos_last))
    return ref


def eligible_at(lots: Sequence[LotT], opened: str | None, clock: str, hours: float,
                net_pos_last: str | None) -> pd.Timestamp | None:
    ref = clock_ref(lots, opened, clock, net_pos_last)
    return None if ref is None else ref + pd.Timedelta(hours=hours)


def lot_threshold(lot: LotT, side: int, tcfg: Any, open_fee: float, close_fee: float) -> float:
    """Price at which ``lot`` qualifies: own gain >= fees + network + trim_min_net_usd AND >= trim_min_gain_pct."""
    s, p = lot["size"], lot["price"]
    need_usd = (open_fee + close_fee) * s + tcfg.trim_network_usd + tcfg.trim_min_net_usd
    frac = max(need_usd / s, tcfg.trim_min_gain_pct)
    return p * (1 + side * frac)


def select_lots(lots: Sequence[LotT], side: int, price: float, tcfg: Any, open_fee: float, close_fee: float) -> list[int]:
    """Indices of the lots to trim at ``price``: every non-first lot past its own threshold. Never the first entry,
    never a lot that is not in its own net profit."""
    out = []
    for i, l in enumerate(lots):
        if l.get("first") or i == 0:
            continue
        if side * (price - lot_threshold(l, side, tcfg, open_fee, close_fee)) >= 0 \
                and lot_gain(l, side, price, open_fee, close_fee, tcfg.trim_network_usd) > 0:
            out.append(i)
    return out


def lots_match(lots: Sequence[LotT], size: float) -> bool:
    tot = sum(l["size"] for l in lots)
    return bool(lots) and abs(tot - size) <= max(0.10, 0.001 * size)


@dataclass
class Guard:
    prices: list[float]
    raised: bool
    net_before: float
    net_after: float
    reason: str = ""


def overall_net(side: int, avg: float, chunks: Sequence[float], prices: Sequence[float], fixed_usd: float) -> float:
    return fixed_usd + sum(side * c * (p - avg) / avg for c, p in zip(chunks, prices))


def tp_guard(side: int, avg: float, chunks: Sequence[float], prices: Sequence[float], fixed_usd: float, min_net: float,
             ratio: float | None, min_gap: float = 0.05) -> Guard:
    """Raise the pending TPs so the whole trade (``fixed_usd`` = realized trims - all fees, plus the pending TP chunks
    at their prices) nets >= ``min_net``. TP2 (last) moves out; TP1 follows ``ratio`` x the TP2 distance (else keeps
    its current fraction). Prices never move inward."""
    prices = list(prices)
    before = overall_net(side, avg, chunks, prices, fixed_usd)
    if before >= min_net - 1e-9 or not prices:
        return Guard(prices, False, before, before)
    d_last = side * (prices[-1] - avg)
    if len(prices) == 1:
        fr = [1.0]
    else:
        r = ratio if ratio is not None else (side * (prices[0] - avg) / d_last if d_last > 0 else 0.6)
        fr = [r] * (len(prices) - 1) + [1.0]
    coef = sum(c * f for c, f in zip(chunks, fr)) / avg
    d_need = (min_net - fixed_usd) / coef if coef > 0 else float("inf")
    new = []
    for i, (p, f) in enumerate(zip(prices, fr)):
        want = avg + side * f * d_need
        want = (math.ceil(want * 100 - 1e-6) if side > 0 else math.floor(want * 100 + 1e-6)) / 100
        new.append(want if side * (want - p) > 0 else p)
    for i in range(len(new) - 1):   # TP1 at least min_gap inside TP2
        if side * (new[-1] - new[i]) < min_gap - 1e-9:
            new[i] = round(new[-1] - side * min_gap, 2)
    after = overall_net(side, avg, chunks, new, fixed_usd)
    why = (f"overall-net guard: trade incl. trims would net {before:+.3f} at TPs {[round(p, 2) for p in prices]} "
           f"(< {min_net:.2f}); raised to {new} -> {after:+.3f}")
    return Guard(new, True, before, after, why)


# ---------------------------------------------------------------- distance + level trigger (Trevor 2026-10-08 17:06 MT)
def arm_price(side: int, avg: float, dist: float) -> float:
    """Price at/beyond which trim mode arms: long avg x (1 - dist), short avg x (1 + dist)."""
    return avg * (1 - side * dist)


def arm_hit(side: int, avg: float, price: float, dist: float) -> bool:
    return avg > 0 and side * (price - arm_price(side, avg, dist)) <= 0


def trigger_price(side: int, level: float, atr: float, tol_atr: float, front_atr: float = 0.0, front_pct: float = 0.0) -> float:
    """Price at/through which a level counts as reached: level - side x ((front_atr + tol_atr) x ATR + front_pct x level)."""
    return level - side * ((front_atr + tol_atr) * atr + front_pct * level)


def trigger_level(side: int, price: float, lv: Any, tol_atr: float, front_atr: float = 0.0,
                  front_pct: float = 0.0) -> tuple[float | None, bool]:
    """(level, reached) for a trim trigger: longs use analyst resistances, shorts supports. Reached = price at/through
    level - side x (front-run offset + tolerance), offset = front_atr x 1h ATR + front_pct x level (fires just BEFORE the
    level; returns the furthest reached level); else the next level ahead (or None)."""
    if lv is None:
        return None, False
    fine = tuple(getattr(lv, "fine_resistances" if side > 0 else "fine_supports", ()) or ())
    levels = [x for x in (lv.resistances if side > 0 else lv.supports) + fine if x and x > 0]   # main + fine levels
    if not levels:
        return None, False
    hit = [x for x in levels if side * (price - trigger_price(side, x, lv.atr, tol_atr, front_atr, front_pct)) >= 0]
    if hit:
        return (max(hit) if side > 0 else min(hit)), True
    return (min(levels) if side > 0 else max(levels)), False


# ---------------------------------------------------------------- wallet-baseline profit guard (Trevor 2026-10-08 17:11 MT)
def trade_sol(side: int, avg: float, margin: float, borrow: float, chunks: Sequence[float], prices: Sequence[float],
              close_fee: float, sunk_sol: float, conv: float | None = None) -> float:
    """Projected SOL result of the whole trade if the pending ``chunks`` close at ``prices`` (the last returns the
    collateral). ``sunk_sol`` = SOL received so far - SOL deposited - network fees (paid + still to pay). Every payout is
    converted to SOL at its own exit price (SOL market; short proceeds are swept USDC -> SOL), or at ``conv`` (the SOL
    price, for ETH / BTC markets)."""
    out = sunk_sol + (margin - borrow) / (conv or prices[-1])
    for c, p in zip(chunks, prices):
        out += (side * c * (p - avg) / avg - close_fee * c) / (conv or p)
    return out


def sol_guard(side: int, avg: float, margin: float, borrow: float, chunks: Sequence[float], prices: Sequence[float],
              close_fee: float, sunk_sol: float, min_sol: float, ratio: float | None, min_gap: float = 0.05,
              conv: float | None = None) -> Guard:
    """Raise the pending TPs (never inward) until the projected trade result >= ``min_sol`` SOL. TP2 (last) moves out,
    TP1 keeps ``ratio`` x the TP2 distance (else its current fraction); smallest distance found by bisection."""
    prices = list(prices)
    f = lambda px: trade_sol(side, avg, margin, borrow, chunks, px, close_fee, sunk_sol, conv)  # noqa: E731
    before = f(prices)
    if before >= min_sol - 1e-12 or not prices:
        return Guard(prices, False, before, before)
    d_last = max(side * (prices[-1] - avg), 0.0)
    if len(prices) == 1:
        fr = [1.0]
    else:
        r = ratio if ratio is not None else (side * (prices[0] - avg) / d_last if d_last > 0 else 0.6)
        fr = [r] * (len(prices) - 1) + [1.0]

    def at(d: float) -> list[float]:
        new = []
        for p, k in zip(prices, fr):
            want = avg + side * k * d
            want = (math.ceil(want * 100 - 1e-6) if side > 0 else math.floor(want * 100 + 1e-6)) / 100
            new.append(want if side * (want - p) > 0 else p)
        for i in range(len(new) - 1):   # TP1 at least min_gap inside TP2
            if side * (new[-1] - new[i]) < min_gap - 1e-9:
                new[i] = round(new[-1] - side * min_gap, 2)
        return new

    lo, hi = d_last, 0.2 * avg
    if f(at(hi)) < min_sol:
        new = at(hi)
    else:
        for _ in range(40):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if f(at(mid)) >= min_sol else (mid, hi)
        new = at(hi)
    after = f(new)
    why = (f"baseline guard: trade would end {before:+.6f} SOL at TPs {[round(p, 2) for p in prices]} "
           f"(< +{min_sol:.6f}); raised to {new} -> {after:+.6f} SOL")
    return Guard(new, True, before, after, why)


def pg_sanity(side: int, avg: float, old: Sequence[float], new: Sequence[float], sol_before: float, usd_est: float,
              min_sol: float, max_mult: float = 2.0, max_dist_pct: float = 0.10) -> str:
    """Why a baseline-guard TP raise must NOT be applied ('' = fine): the SOL projection's sign disagrees with the simple
    USD estimate, a TP would move by more than ``max_mult`` x its distance from the avg (floor 0.25 % of avg), or a TP
    would land absurdly (non-finite, <= 0, > ``max_dist_pct`` from the avg). (Fix 2026-10-08 18:03 MT.)"""
    tol = max(min_sol, 1e-6)
    if (sol_before < -tol and usd_est > 0) or (sol_before > tol and usd_est < 0):
        return f"sanity: SOL projection {sol_before:+.6f} vs USD estimate {usd_est:+.4f} disagree in sign"
    for o, n in zip(old, new):
        if not (math.isfinite(n) and n > 0) or abs(n - avg) > max_dist_pct * avg:
            return f"sanity: TP {o} -> {n} absurd (avg {avg:.4f})"
        dist = max(side * (o - avg), 0.0025 * avg)
        if abs(n - o) > max_mult * dist:
            return f"sanity: TP {o} -> {n} moves {abs(n - o):.2f} > {max_mult:g}x distance {dist:.2f} from avg {avg:.4f}"
    return ""
