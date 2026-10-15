"""Resting on-chain orders (Trevor 2026-10-11 spec; [resting] flags, all default OFF). Pure helpers, no I/O.

1. resting add orders: the next ladder rung rests as a Jupiter limit-open order (live/src/limit.ts).
2. per-add partial TP: a trigger decrease sized to each filled add, N analyst levels beyond its entry.
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


def partial_target_zone(side: int, entry: float, avg: float, size: float, lv: Any, rung: int, max_rungs: int,
                        f_first: float, f_deep: float, *, snap_levels: bool = False, tol_atr: float, front_atr: float,
                        front_pct: float, open_fee: float, close_fee: float, network_usd: float) -> dict[str, Any]:
    """TP3 target from the covered entry (Trevor 2026-10-15). Primary: the zone edge entry + side x gap x f (gap = entry -> avg,
    f = zone_f), if it covers fees. Optional snap_levels: instead the farthest analyst level (main + fine) inside the zone, fired
    tol (+ front-run) before it; none usable -> the edge. Edge not covering fees -> the fee-covering minimum just past the entry.
    Always returns a price."""
    gap = max(side * (avg - entry), 0.0)   # entry at/past avg -> zone width 0, TP3 falls to the fee minimum (no avg cap)
    f = zone_f(rung, max_rungs, f_first, f_deep)
    zone_end = entry + side * gap * f
    out = {"rung": rung, "f": round(f, 4), "zone_end": round(zone_end, 6), "avg": round(avg, 6), "gap": round(gap, 6)}
    if snap_levels and lv is not None:
        beyond = sorted((x for x in trim_levels(side, lv) if side * (x - entry) > 0), key=lambda x: side * x)
        for level in [x for x in beyond if side * (zone_end - x) >= 0][::-1]:   # farthest first
            fire = trimmod.trigger_price(side, level, lv.atr, tol_atr, front_atr, front_pct)
            net = lot_net_at(side, entry, size, fire, open_fee, close_fee, network_usd)
            if side * (fire - entry) > 0 and net > 0:
                return {**out, "mode": "level", "level": round(level, 6), "px": round(fire, 2), "net_usd": round(net, 4),
                        "level_index": beyond.index(level) + 1}
    fire = round(zone_end, 2)
    net = lot_net_at(side, entry, size, fire, open_fee, close_fee, network_usd)
    if side * (fire - entry) > 0 and net > 0:
        return {**out, "mode": "zone_edge", "level": round(zone_end, 6), "px": fire, "net_usd": round(net, 4), "level_index": 0}
    need = (open_fee + close_fee) + network_usd / size if size > 0 else 0.0
    px = entry * (1 + side * need)
    px = round(px, 2)
    while lot_net_at(side, entry, size, px, open_fee, close_fee, network_usd) <= 0:
        px = round(px + side * 0.01, 2)
    return {**out, "mode": "fee_min", "level": None, "px": px, "level_index": 0,
            "net_usd": round(lot_net_at(side, entry, size, px, open_fee, close_fee, network_usd), 4)}


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
