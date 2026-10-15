"""Resting on-chain orders (Trevor 2026-10-11 spec; [resting] flags, all default OFF). Pure helpers, no I/O.

1. resting add orders: the next ladder rung rests as a Jupiter limit-open order (live/src/limit.ts).
2. the single partial TP (TP3): a trigger decrease sized to the adds that net a profit after fees at the target (tp3_plan).
3. last partial price: the next add order must lie beyond the last partial price in the adverse direction.
"""
from __future__ import annotations

from typing import Any, Sequence

from live import trim as trimmod


def partial_levels_n(rung: int, n_min: int, n_max: int, max_rungs: int) -> int:
    """N for the add on ladder rung ``rung`` (1 = first add): linear from n_min at rung 1 to n_max at rung max_rungs,
    rounded half up, capped to [n_min, n_max] (rungs beyond max_rungs keep n_max)."""
    if max_rungs <= 1:
        return n_max
    r = min(max(rung, 1), max_rungs)
    x = n_min + (n_max - n_min) * (r - 1) / (max_rungs - 1)
    return int(min(n_max, max(n_min, int(x + 0.5))))


def trim_levels(side: int, lv: Any) -> list[float]:
    """The levels the trim uses on the profit side: resistances + fine resistances (long) / supports + fine supports
    (short), positive, exact duplicates dropped."""
    if lv is None:
        return []
    fine = tuple(getattr(lv, "fine_resistances" if side > 0 else "fine_supports", ()) or ())
    pool = tuple(lv.resistances if side > 0 else lv.supports) + fine
    return sorted({float(x) for x in pool if x and x > 0})


def lot_net_at(side: int, entry: float, size: float, px: float, open_fee: float, close_fee: float, network_usd: float) -> float:
    """Net USD of closing a lot of ``size`` USD opened at ``entry`` at ``px`` after 6+6 bps style fees + network est."""
    return side * size * (px - entry) / entry - (open_fee + close_fee) * size - network_usd


def partial_target(side: int, entry: float, size: float, lv: Any, n: int, *, tol_atr: float, front_atr: float,
                   front_pct: float, open_fee: float, close_fee: float, network_usd: float) -> dict[str, Any] | None:
    """Partial TP for an add filled at ``entry``: the n-th trim level beyond ``entry`` in the profit direction; fired at
    trim.trigger_price (tol + front-run before the level). Must cover fees: if the n-th level's fire price does not, the
    next level further out is used (the first one that does). None = not enough levels / none covers fees."""
    if lv is None or n < 1:
        return None
    beyond = [x for x in trim_levels(side, lv) if side * (x - entry) > 0]
    beyond.sort(key=lambda x: side * x)          # nearest first
    for k in range(n - 1, len(beyond)):
        level = beyond[k]
        fire = trimmod.trigger_price(side, level, lv.atr, tol_atr, front_atr, front_pct)
        net = lot_net_at(side, entry, size, fire, open_fee, close_fee, network_usd)
        if side * (fire - entry) > 0 and net > 0:
            return {"level": round(level, 6), "px": round(fire, 2), "n": n, "level_index": k + 1, "net_usd": round(net, 4),
                    "levels_beyond": len(beyond)}
    return None


def zone_f(rung: int, max_rungs: int, f_first: float, f_deep: float) -> float:
    """TP3 zone fraction (Trevor 2026-10-15): linear taper f_first at rung 1 -> f_deep at rung >= max_rungs."""
    if max_rungs <= 1:
        return f_deep
    return f_first - (f_first - f_deep) * min(1.0, max(0.0, (rung - 1) / (max_rungs - 1)))


def tp3_plan(side: int, avg: float, adds: Sequence[dict[str, Any]], max_rungs: int, f_first: float, f_deep: float, *,
             open_fee: float, close_fee: float, network_usd: float) -> dict[str, Any] | None:
    """The single TP3 (Trevor 2026-10-15). ``adds`` = the open add lots (price / size), rung = how many. D = |furthest add - avg|;
    target = avg + f x (furthest add - avg), f = zone_f(rung): a point between the trade avg and the furthest (deepest) add.
    TP3 covers every add that nets > 0 after fees (open + close) and network at the target; size = their total. None = no add
    does (or no add lies beyond the avg): no TP3."""
    adds = [a for a in adds if float(a["size"]) > 0 and float(a["price"]) > 0]
    if not adds:
        return None
    far = min(adds, key=lambda a: side * float(a["price"]))["price"]
    far = float(far)
    if side * (avg - far) <= 0:
        return None
    rung = len(adds)
    f = zone_f(rung, max_rungs, f_first, f_deep)
    px = round(avg + (far - avg) * f, 2)
    cov = [a for a in adds if lot_net_at(side, float(a["price"]), float(a["size"]), px, open_fee, close_fee, network_usd) > 0]
    if not cov:
        return None
    size = sum(float(a["size"]) for a in cov)
    entry = size / sum(float(a["size"]) / float(a["price"]) for a in cov)
    net = sum(lot_net_at(side, float(a["price"]), float(a["size"]), px, open_fee, close_fee, 0.0) for a in cov) - network_usd
    return {"px": px, "size": round(size, 2), "entry": round(entry, 6), "rung": rung, "f": round(f, 4), "far": far,
            "dist": round(abs(far - avg), 6), "avg": round(avg, 6), "lots": [a["time"] for a in cov], "net_usd": round(net, 4)}


def add_ref(side: int, last_entry: float, last_partial_px: float | None) -> float:
    """Ladder reference for the next add: the more adverse of the last fill and the last partial price."""
    if not last_partial_px:
        return last_entry
    return min(last_entry, last_partial_px) if side > 0 else max(last_entry, last_partial_px)


def beyond_last_partial(side: int, trigger: float, last_partial_px: float | None) -> bool:
    """True when ``trigger`` lies strictly beyond the last partial price in the adverse direction (or there is none)."""
    return not last_partial_px or side * (last_partial_px - trigger) > 0


def unmatched(tracked: Sequence[dict[str, Any]], reqs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tracked order records whose request account is no longer on-chain."""
    live = {r.get("pubkey") for r in reqs}
    return [t for t in tracked if t.get("pubkey") not in live]
