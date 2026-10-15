"""Support/resistance-driven DCA planning (dca.mode = "levels"). Pure functions shared by backtest and live.

Longs add at supports below the last fill, shorts at resistances above it. Gap to the previous fill must be
>= min_gap_atr * ATR and <= max_gap_atr * ATR, else fixed ``trigger_dist`` spacing is used. Each add is sized
so the new average lands ``target_avg_atr`` * ATR short of the next opposing level (resistance for longs; a level
within ``min_target_gap_atr`` ATR of / behind the add price is never used), capped by the add's ladder budget (free
collateral spread over the planned rungs with weights ``ladder_weight_mult``**k), clamped to [min_add_usd,
available] and shrunk until liquidation stays beyond the next rung.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import pandas as pd

LONG = 1


@dataclass(frozen=True)
class Levels:
    supports: tuple[float, ...]       # any order
    resistances: tuple[float, ...]
    atr: float                        # 1h ATR, USD
    # analyst levels.fine (15m/1h intraday S/R, 2026-10-08): used by the per-add trim trigger only, never by the ladder
    fine_supports: tuple[float, ...] = ()
    fine_resistances: tuple[float, ...] = ()


@dataclass(frozen=True)
class AddPlan:
    trigger: float                    # add when price is at/beyond this
    margin: float                     # USD collateral
    leverage: float
    new_avg: float
    liq: float
    next_level: float                 # the level after this add (liq must stay beyond it)
    target_level: float               # opposing level used for sizing
    source: str                       # "levels" or "fixed"


class PosLike(Protocol):
    side: int
    size: float
    qty: float
    margin: float
    borrow: float
    last_entry: float
    last_leverage: float


def load_analyst_levels(path: str | Path, max_age_min: float, now: pd.Timestamp | None = None) -> Levels | None:
    """Read /workspace/market-analysis/latest.json (h1+h4 levels, 1h ATR); None if missing/stale/bad."""
    try:
        d = json.loads(Path(path).read_text())
        ts = pd.Timestamp(d["timestamp"])
        now = now or pd.Timestamp.now(tz="UTC")
        if (now - ts) > pd.Timedelta(minutes=max_age_min):
            return None
        # analyst writes h1/h4 = null when its candle fetch failed: null / missing / empty -> no levels (= stale file)
        lv = d.get("levels") if isinstance(d.get("levels"), dict) else {}
        lv = {tf: (lv.get(tf) if isinstance(lv.get(tf), dict) else {}) for tf in ("h1", "h4")}
        sup = tuple(float(x) for tf in ("h1", "h4") for x in (lv[tf].get("support") or []) if x is not None)
        res = tuple(float(x) for tf in ("h1", "h4") for x in (lv[tf].get("resistance") or []) if x is not None)
        if not sup and not res:
            _warn_once(str(path), "analyst file has no h1/h4 levels (null/empty) -> treated as no levels "
                                  "(stale-file behaviour: own-ATR spacing if enabled, no S/R TP2 cap)")
            return None
        _LEVEL_WARNED.pop(str(path), None)
        atr = float(d["volatility"]["atr14_1h_usd_per_hour"])
        if atr <= 0:
            return None
        fine = d["levels"].get("fine") if isinstance(d["levels"].get("fine"), dict) else {}
        def _px(items):
            out = []
            for it in items or []:
                v = it.get("price") if isinstance(it, dict) else it
                try:
                    if v is not None and float(v) > 0:
                        out.append(float(v))
                except (TypeError, ValueError):
                    pass
            return tuple(out)
        return Levels(sup, res, atr, fine_supports=_px(fine.get("supports")), fine_resistances=_px(fine.get("resistances")))
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None


_LEVEL_WARNED: dict[str, str] = {}


def _warn_once(key: str, msg: str) -> None:
    if _LEVEL_WARNED.get(key) != msg:
        _LEVEL_WARNED[key] = msg
        logging.getLogger("bot").warning("%s: %s", key, msg)


def liq_price(side: int, size: float, qty: float, margin: float, borrow: float, maint: float) -> float:
    return (size + side * (borrow + maint * size - margin)) / qty


def next_add_level(side: int, ref: float, lv: Levels | None, min_gap: float, max_gap: float,
                   fixed_dist: float) -> tuple[float, str]:
    """Next add price beyond ``ref`` (below for longs): nearest qualifying level, else fixed spacing."""
    if lv is not None:
        pool = lv.supports if side == LONG else lv.resistances
        cands = [x for x in pool if min_gap * lv.atr <= side * (ref - x) <= max_gap * lv.atr]
        if cands:
            return (max(cands) if side == LONG else min(cands)), "levels"
    return ref - side * fixed_dist, "fixed"


def opposing_level(side: int, price: float, lv: Levels | None, max_gap: float, min_gap_atr: float = 0.5,
                   flipped: bool = False) -> float:
    """Sizing target for an add at ``price``: nearest resistance >= ``min_gap_atr`` * ATR beyond it (longs).

    Supports are never the target unless ``flipped`` (a support behind the add price is broken by the time the add
    fills); even then they must clear the add price by ``min_gap_atr`` ATR. 16:15 bug: broken 115.44 support vs a
    114.55 add put the wanted avg $0.06 above the fill and sized the add to the whole wallet.
    Fallback: price +- max(max_gap * ATR, $1)."""
    atr = lv.atr if lv else 0.0
    if lv is not None:
        pool = lv.resistances + (lv.supports if flipped else ()) if side == LONG else \
            lv.supports + (lv.resistances if flipped else ())
        cands = [x for x in pool if side * (x - price) >= max(min_gap_atr * atr, 1e-9)]
        if cands:
            return min(cands) if side == LONG else max(cands)
    return price + side * max(max_gap * atr, 1.0)


@dataclass(frozen=True)
class LadderParams:
    """Ladder budgeting: free collateral is spread over the planned rungs with geometric weights."""
    enabled: bool = True
    sizing: str = "budget"            # "budget": the rung budget; "min": min(formula, rung budget)
    min_rungs: int = 4                # at least this many main rungs before the deep rung
    max_rungs: int = 12               # main rungs above the deep level are capped at this many
    weight_mult: float = 1.5
    deep: bool = True
    deep_anchor: str = "first_entry"  # deep level measured from: first_entry (stays put) | last_entry (rolls)
    deep_min_atr: float = 6.0         # deep = deepest analyst support deep_min_atr..deep_max_atr ATR below the anchor
    deep_max_atr: float = 12.0
    deep_atr: float = 8.0             # ... else anchor - this many ATR
    fee_usd_per_add: float = 0.10
    weight_mode: str = "geometric"    # "geometric": mult**k | "accel": ratio starts at mult, +mult_step per rung, <= mult_cap
    weight_mult_step: float = 0.10
    weight_mult_cap: float = 2.5
    fixed_usd: float = 0.0            # > 0: flat collateral per rung (weights / remainder ignored); 0 = off
    min_usd: float = 0.0              # accel mode: per-rung floor fitted INSIDE the budget (floored rungs fixed, rest rescaled)


def ladder_weights(n: int, lp: "LadderParams") -> list[float]:
    """Rung weights: geometric mult**k, or accelerating (ratio itself grows by weight_mult_step, capped)."""
    if lp.weight_mode != "accel":
        return [lp.weight_mult ** k for k in range(n)]
    w, r = [], lp.weight_mult
    for k in range(n):
        w.append(1.0 if k == 0 else w[-1] * r)
        if k:
            r = min(lp.weight_mult_cap, r + lp.weight_mult_step)
    return w


def fit_min_budgets(w: list[float], budget: float, min_usd: float) -> list[float]:
    """Split ``budget`` by weights with every rung >= min_usd: floored rungs fixed at min_usd, the rest rescaled.
    If the floor can't be met for all rungs, every rung gets budget/n (plan_add then floors/caps per add)."""
    n = len(w)
    if n == 0:
        return []
    if budget <= 0 or min_usd <= 0 or min_usd * n > budget:
        tot = sum(w) or 1.0
        return [max(0.0, budget) * x / tot for x in w] if min_usd * n <= budget or min_usd <= 0 else [max(0.0, budget) / n] * n
    fl = [False] * n
    out = [budget * x / sum(w) for x in w]
    for _ in range(n + 1):
        new = [i for i in range(n) if not fl[i] and out[i] < min_usd - 1e-12]
        if not new:
            break
        for i in new:
            fl[i] = True
        rest = budget - min_usd * sum(fl)
        tw = sum(x for x, f in zip(w, fl) if not f) or 1.0
        out = [min_usd if f else rest * x / tw for x, f in zip(w, fl)]
    return out


@dataclass(frozen=True)
class Rung:
    price: float
    leverage: float
    source: str                       # "levels", "fixed", "deep-levels", "deep-fixed"
    budget: float = 0.0               # USD collateral allotted (notional = budget * leverage)


def _deep_level(side: int, anchor: float, lv: Levels | None, atr: float, lp: LadderParams) -> tuple[float, str]:
    """Deepest analyst support between deep_min_atr and deep_max_atr ATR below ``anchor``, else anchor - deep_atr ATR."""
    pool = (lv.supports if side == LONG else lv.resistances) if lv is not None else ()
    deep = [x for x in pool if lp.deep_min_atr * atr <= side * (anchor - x) <= lp.deep_max_atr * atr]
    if deep:
        return (min(deep) if side == LONG else max(deep)), "deep-levels"
    return anchor - side * lp.deep_atr * atr, "deep-fixed"


def ladder_rungs(side: int, ref: float, lv: Levels | None, *, last_leverage: float, lev_step: float, lev_cap: float,
                 min_gap_atr: float, max_gap_atr: float, fixed_dist: float, lp: LadderParams,
                 first_entry: float | None = None) -> list[Rung]:
    """Remaining planned add prices beyond ``ref`` (the last fill).

    Deep level is anchored (first entry by default): deepest analyst support deep_min_atr..deep_max_atr ATR below
    it, else anchor - deep_atr ATR. It stays put, so as adds fill the remaining rungs shrink toward it, their budgets
    grow and the deep rung takes the remainder. Main rungs: chained next_add_level (supports 0.5-2 ATR apart, else
    trigger_dist spacing) down to the deep level, at most max_rungs. If price is already past the deep level (or no
    anchor), the ladder rolls: min_rungs chained rungs from the last fill plus a deep level below them.
    Rung k (0 = next add) uses leverage min(cap, last_leverage + step * (k + 1))."""
    atr = lv.atr if lv else fixed_dist
    nmin = max(lp.min_rungs, 1)
    anchor = first_entry if (lp.deep_anchor == "first_entry" and first_entry is not None) else None
    deep, dsrc = (_deep_level(side, anchor, lv, atr, lp) if (lp.deep and anchor is not None) else (None, ""))
    if deep is not None and side * (ref - deep) < min_gap_atr * atr:
        deep = None                        # at/past the anchored deep level -> rolling ladder
    main: list[tuple[float, str]] = []
    p = ref
    while len(main) < max(lp.max_rungs, nmin):
        nxt, src = next_add_level(side, p, lv, min_gap_atr, max_gap_atr, fixed_dist)
        if deep is not None and side * (nxt - deep) < min_gap_atr * atr:   # reached the deep level
            break
        if deep is None and len(main) >= nmin:
            break
        main.append((nxt, src))
        p = nxt
    if deep is None and lp.deep:           # rolling: deep level below the last main rung
        deep, dsrc = _deep_level(side, p, lv, atr, replace(lp, deep_min_atr=min_gap_atr))
    pts = main + ([(deep, dsrc)] if lp.deep and deep is not None else [])
    return [Rung(px, min(lev_cap, last_leverage + lev_step * (k + 1)), src) for k, (px, src) in enumerate(pts)]


def ladder_budgets(rungs: list[Rung], available_usd: float, lp: LadderParams) -> list[Rung]:
    """Split free collateral (minus per-rung fee reserve) by weights mult**k; the deepest rung gets the rest."""
    free = max(0.0, available_usd - lp.fee_usd_per_add * len(rungs))
    if lp.fixed_usd > 0:   # flat ladder (2026-10-12 candidate C): each rung = fixed_usd; plan_add caps at what is available
        return [Rung(r.price, r.leverage, r.source, lp.fixed_usd) for r in rungs]
    if lp.weight_mode == "accel":
        b = fit_min_budgets(ladder_weights(len(rungs), lp), free, lp.min_usd)
        return [Rung(r.price, r.leverage, r.source, x) for r, x in zip(rungs, b)]
    w = [lp.weight_mult ** k for k in range(len(rungs))]
    tot = sum(w) or 1.0
    out, used = [], 0.0
    for i, (r, wk) in enumerate(zip(rungs, w)):
        b = free - used if i == len(rungs) - 1 else free * wk / tot
        used += b
        out.append(Rung(r.price, r.leverage, r.source, b))
    return out


def _formula_notional(side: int, size: float, qty: float, trig: float, want_avg: float) -> float:
    """Notional x added at ``trig`` so (size + x) / (qty + x/trig) = want_avg (0 if avg already there)."""
    avg = size / qty
    if side * (avg - want_avg) <= 0:
        return 0.0
    return qty * side * (avg - want_avg) / (side * (want_avg / trig - 1))


def plan_add(pos: PosLike, lv: Levels | None, *, available_usd: float, lev_step: float, lev_cap: float,
             min_add_usd: float, min_gap_atr: float, max_gap_atr: float, target_avg_atr: float,
             fixed_dist: float, maint: float, max_add_frac: float = 1.0, min_target_gap_atr: float = 0.5,
             min_avg_gap_atr: float = 0.25, fallback_avg_frac: float = 0.5, open_fee: float = 0.0,
             target_flipped_supports: bool = False, liq_buffer_atr: float = 0.0,
             ladder: LadderParams | None = None) -> AddPlan | None:
    """Next add. Collateral = min(formula, ladder budget[, available * max_add_frac]), >= min_add_usd, <= available,
    then shrunk until the post-add liquidation (collateral net of the open fee) sits beyond the next rung.
    Notional = collateral * leverage; avg and liq are computed from that notional."""
    s = pos.side
    atr = lv.atr if lv else fixed_dist
    lev = min(lev_cap, pos.last_leverage + lev_step)
    rungs: list[Rung] = []
    if ladder is not None and ladder.enabled:
        rungs = ladder_budgets(ladder_rungs(s, pos.last_entry, lv, last_leverage=pos.last_leverage,
                                            lev_step=lev_step, lev_cap=lev_cap, min_gap_atr=min_gap_atr,
                                            max_gap_atr=max_gap_atr, fixed_dist=fixed_dist, lp=ladder,
                                            first_entry=getattr(pos, "first_entry", None)), available_usd, ladder)
    if rungs:
        trig, src = rungs[0].price, rungs[0].source
    else:
        trig, src = next_add_level(s, pos.last_entry, lv, min_gap_atr, max_gap_atr, fixed_dist)
    target = opposing_level(s, trig, lv, max_gap_atr, min_target_gap_atr, target_flipped_supports)
    want_avg = target - s * target_avg_atr * atr
    avg = pos.size / pos.qty
    gap_min = max(min_avg_gap_atr * atr, 1e-6 * trig)
    if s * (want_avg - trig) < gap_min:   # target unreachable/too tight from this fill: cut the avg-to-fill gap
        want_avg = avg - fallback_avg_frac * (avg - trig)
    if s * (want_avg - trig) < gap_min:   # guard: never ask for an avg within gap_min of the fill (x -> infinity)
        want_avg = trig + s * gap_min
    formula = _formula_notional(s, pos.size, pos.qty, trig, want_avg) / lev   # collateral
    cap = available_usd * max_add_frac
    nxt = None
    if rungs:
        budget = rungs[0].budget
        want = budget if ladder.sizing == "budget" else min(formula, budget)
        if len(rungs) > 1:
            nxt = rungs[1].price
    else:
        want = formula
    margin = min(max(min(want, cap), min_add_usd), available_usd)
    if margin < min_add_usd:
        return None
    if nxt is None:
        nxt, _ = next_add_level(s, trig, lv, min_gap_atr, max_gap_atr, fixed_dist)
    for _ in range(60):  # shrink until liquidation sits beyond the next add level
        notional = margin * lev
        size = pos.size + notional
        qty = pos.qty + notional / trig
        liq = liq_price(s, size, qty, pos.margin + margin - notional * open_fee, pos.borrow, maint)
        if s * (nxt - liq) > liq_buffer_atr * atr:   # liq must sit >= liq_buffer_atr ATR beyond the next rung
            return AddPlan(trig, margin, lev, size / qty, liq, nxt, target, src)
        margin *= 0.8
        if margin < min_add_usd:
            return None
    return None


@dataclass
class _SimPos:
    side: int
    size: float
    qty: float
    margin: float
    borrow: float
    last_entry: float
    last_leverage: float
    first_entry: float | None = None


def project_ladder(pos: PosLike, lv: Levels | None, *, available_usd: float, max_steps: int = 12,
                   **kw) -> list[AddPlan]:
    """Simulate successive adds (each fills at its trigger, re-planned with the remaining collateral)."""
    p = _SimPos(pos.side, pos.size, pos.qty, pos.margin, pos.borrow, pos.last_entry, pos.last_leverage,
                getattr(pos, "first_entry", None))
    out: list[AddPlan] = []
    avail = available_usd
    fee_per = kw["ladder"].fee_usd_per_add if kw.get("ladder") is not None else 0.0
    of = kw.get("open_fee", 0.0)
    for _ in range(max_steps):
        a = plan_add(p, lv, available_usd=avail, **kw)
        if a is None:
            break
        out.append(a)
        if a.source.startswith("deep"):
            break
        n = a.margin * a.leverage
        p = _SimPos(p.side, p.size + n, p.qty + n / a.trigger, p.margin + a.margin - n * of, p.borrow,
                    a.trigger, a.leverage, p.first_entry)
        avail -= a.margin + fee_per
    return out


# ---------------------------------------------------------------------------------------------------- smart TPs
# Trevor 2026-10-07 20:14 MT. Fee floors (each TP's closing chunk nets >= min_net_usd after open+close fees), TP1 kept
# min_gap_usd inside TP2, never a trigger at/through market. Where the prices come from (tp1_pct / tp2_pct from avg, or
# the plain ladder) is DcaStrategy.tp_targets. Pure functions, no I/O.

@dataclass(frozen=True)
class SmartTpParams:
    min_net_usd: float = 0.05
    min_gap_usd: float = 0.05
    market_gap: float = 0.02
    open_fee: float = 0.0006
    close_fee: float = 0.0006


@dataclass(frozen=True)
class SmartTpResult:
    prices: tuple[float, ...]            # whole ladder (filled levels keep their base price, unused)
    floors: tuple[float | None, ...]     # fee floor per level (None = already filled)
    reasons: tuple[str, ...]
    past_market: tuple[bool, ...]        # pending level would sit at/through market (+ market_gap)
    unclamped: tuple[float, ...] = ()    # prices before the market clamp


def tp_fee_floor(side: int, avg: float, chunk_usd: float, min_net: float, open_fee: float, close_fee: float) -> float:
    """Trigger price at which closing ``chunk_usd`` notional (opened at ``avg``) nets ``min_net`` after its open fee
    (open_fee * chunk) and the close fee (close_fee * exit notional)."""
    q = chunk_usd / avg
    if side == LONG:
        return (min_net + chunk_usd * (1 + open_fee)) / (q * (1 - close_fee))
    return (chunk_usd * (1 - open_fee) - min_net) / (q * (1 + close_fee))


def smart_tps(side: int, avg: float, size: float, base: list[float] | tuple[float, ...],
              close_pcts: list[float] | tuple[float, ...], tp_hits: int, p: SmartTpParams, *,
              market: float | None = None) -> SmartTpResult:
    """Fee floors / ordering / market clamp on the current position. ``size`` = current notional (after any filled TPs);
    ``base`` = the ladder prices; ``market`` given -> pending triggers are clamped to >= market + market_gap (longs)."""
    s, n = side, len(base)
    prices = [float(x) for x in base]
    reasons: list[str] = []
    last = n - 1
    lvl = lambda i: f"TP{i + 1}"   # noqa: E731
    beyond = lambda a, b: s * (a - b) > 0   # a further from avg (in profit direction) than b   # noqa: E731
    # fee floor per pending level: chunk = its share of the remaining position
    done = close_pcts[tp_hits - 1] if tp_hits else 0.0
    floors: list[float | None] = []
    for i in range(n):
        if i < tp_hits:
            floors.append(None)
            continue
        prev = close_pcts[i - 1] if i else 0.0
        chunk = size * (close_pcts[i] - prev) / (1.0 - done) if done < 1.0 else size
        floors.append(tp_fee_floor(s, avg, chunk, p.min_net_usd, p.open_fee, p.close_fee))
    if tp_hits > last:
        return SmartTpResult(tuple(prices), tuple(floors), (), tuple(False for _ in prices), tuple(prices))
    # fee floors + ordering (each pending level inside the next by min_gap_usd)
    def floor_and_order() -> None:
        f_last = floors[last]
        if f_last is not None and beyond(f_last, prices[last]):
            reasons.append(f"{lvl(last)} {prices[last]:.3f}->{f_last:.3f}: fee floor (nets ${p.min_net_usd:.2f})")
            prices[last] = f_last
        for i in range(last - 1, tp_hits - 1, -1):
            lim = prices[i + 1] - s * p.min_gap_usd
            if beyond(prices[i], lim):
                reasons.append(f"{lvl(i)} {prices[i]:.3f}->{lim:.3f}: kept {p.min_gap_usd:.2f} inside {lvl(i + 1)}")
                prices[i] = lim
            f = floors[i]
            if f is not None and beyond(f, prices[i]):
                reasons.append(f"{lvl(i)} {prices[i]:.3f}->{f:.3f}: fee floor (nets ${p.min_net_usd:.2f})")
                prices[i] = f
        for i in range(tp_hits, last):
            lim = prices[i] + s * p.min_gap_usd
            if beyond(lim, prices[i + 1]):
                reasons.append(f"{lvl(i + 1)} {prices[i + 1]:.3f}->{lim:.3f}: kept {p.min_gap_usd:.2f} beyond {lvl(i)}")
                prices[i + 1] = lim
    floor_and_order()
    # never a trigger at/through market
    raw = tuple(prices)                  # before the market clamp
    past = [False] * n
    if market is not None:
        lim = market + s * p.market_gap
        lim = (math.ceil(lim * 100 - 1e-9) if s == LONG else math.floor(lim * 100 + 1e-9)) / 100   # cents, away from market
        for i in range(tp_hits, n):
            if not beyond(prices[i], lim):
                past[i] = True
                reasons.append(f"{lvl(i)} {prices[i]:.3f}->{lim:.3f}: not beyond market {market:.3f} + {p.market_gap:g}")
                prices[i] = lim
        for i in range(tp_hits, last):   # keep the ladder ordered after the market clamp
            lo = prices[i] + s * p.min_gap_usd
            if beyond(lo, prices[i + 1]):
                prices[i + 1] = lo
    return SmartTpResult(tuple(prices), tuple(floors), tuple(reasons), tuple(past), raw)
