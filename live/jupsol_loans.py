"""Automatic JupSOL loans (Trevor 2026-10-27). [ledger] jupsol_loans = "off" | "observe" | "on" (default "off").

The JupSOL bucket (the shared wallet's JupSOL, the bots' past withdrawals) lends to a bot in two cases, rescue first:

1. RESCUE (mode "on"): the bot's open position is closer than rescue_cfg trigger_pct (3 %) to liquidation, the accrued
   borrow fee included (Jupiter takes it out of the collateral). Collateral only (no bank credit split): one ExactOut
   JupSOL -> own-coin swap for min(collateral that moves liq to target_pct (10 %) away, 1/3 of the usable JupSOL after the
   haircut_pct (1 %) haircut), then the measured coin goes straight in as collateral (increase, sizeUsd 0). EMERGENCY is
   logged when that amount moves liq by less than emergency_min_push_pct points. KILL present -> skipped and logged.
2. NEXT ADD (mode "on"): the next resting add needs more than the bot's free balance and NO peer bot can cover it (peers
   go first exactly as today; checked on 2 consecutive ticks, or peer borrowing switched off with <state dir>/IB_BORROW_OFF
   for a test). JupSOL lends exactly the shortfall + the inter-bot $1 rounding buffer (rung margin - free + $1), and only
   if the whole amount is available (rescues always go first). 2026-10-28: the shortfall is the larger of the bank
   shortfall and (SOL bot) the native SOL fee-reserve gap of the add (Bot.add_reserve_short_usd); a rung the free balance
   covers but that only the fee reserve blocks takes the same path (Bot.place_rest_add, no inter-bot request).

Booking (live/bank.py): the coin MEASURED from the tx deltas is booked as bal + liability (bank.loan_open), the JupSOL
measured out of the wallet is what is owed. Equity / hwm / wd_mark / guard target / withdrawable do not move. Repayment
(every mode, on flat, after any inter-bot loan, never while a reconcile / own-coin booking / SOL true-up is pending): an
ExactOut own-coin -> JupSOL swap for exactly the JupSOL quantity still owed. Not enough balance -> what the balance buys
is repaid (ExactIn) and the rest is carried to the bot's NEXT flat (deposits do not pay it while it is still flat).

Every send is journalled in bank["jl_op"] first (state.json); an outcome that is not known is resolved from the tx deltas
or alerted, never re-sent and never guessed. Shared bookkeeping (rescue priority, parked JupSOL, op log):
live/jupsol_loans.json under live/jupsol.lock. Nothing here ever raises into the trading loop.
"""
from __future__ import annotations

import fcntl
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from live import bank as K
from live import ledger
from live import sweep as W

log = logging.getLogger("live")
RESCUE_SHARE = 1.0 / 3.0          # Trevor 2026-10-27: each rescue takes at most ONE THIRD of the usable JupSOL
NEXT_ADD_BUFFER_USD = 1.0         # the same $1 rounding buffer inter-bot loans use (bank.ib_need_usd)
NOPEER_TICKS = 2                  # "no peer can cover" must hold on this many consecutive ticks
WANT_MAX_AGE_S = 120.0            # a next-add loan decided this long ago is re-decided (bal / price moved)
RESCUE_WANT_MAX_AGE_S = 120.0     # a bot in rescue trigger blocks next-add loans of every bot for this long
UNKNOWN_GIVEUP_S = 300.0          # sent tx not found after this long -> not landed (alerted)
PEER_OFF_FILE = "IB_BORROW_OFF"   # test switch: this bot never borrows from a peer bot (lending unchanged)
PARK_FILE = "JUPSOL_PARK"         # live test: {"op": "park", "native": x} | {"op": "unpark"} (processed once, then removed)
STATE_FILES = {"SOL": "live/state.json", "ETH": "live/eth/state.json", "BTC": "live/btc/state.json"}
LAMPORTS = 1_000_000_000


def mode(cfg: Any) -> str:
    return str(getattr(cfg.ledger, "jupsol_loans", "off") or "off")


def _jup(cmd: str, args: dict[str, Any]) -> dict[str, Any]:
    from live import bot as B   # tests replace B.jup
    return B.jup(cmd, args)


def rcfg() -> dict[str, Any]:
    from live.rescue import rcfg as _r
    return _r()


def _now() -> str:
    return str(pd.Timestamp.now(tz="UTC"))


def _every(bot: Any, key: str, sec: float = 3600.0) -> bool:
    """Rate limit for repeating log lines (per process)."""
    d = bot.__dict__.setdefault("_jl_logged", {})
    if time.time() - d.get(key, 0.0) >= sec:
        d[key] = time.time()
        return True
    return False


# ------------------------------------------------------------------ shared registry (live/jupsol_loans.json)
def _reg_path() -> Path:
    return K.ROOT / "live" / "jupsol_loans.json"


_LOCK_DEPTH = [0]   # re-entrant within this process (a booking inside a locked lend writes the registry again)


@contextmanager
def lock() -> Iterator[None]:
    """Cross-bot lock for the JupSOL bucket (one lend at a time); re-entrant in the same process."""
    if _LOCK_DEPTH[0]:
        _LOCK_DEPTH[0] += 1
        try:
            yield
        finally:
            _LOCK_DEPTH[0] -= 1
        return
    p = K.ROOT / "live" / "jupsol.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        _LOCK_DEPTH[0] = 1
        try:
            yield
        finally:
            _LOCK_DEPTH[0] = 0
            fcntl.flock(f, fcntl.LOCK_UN)


def reg_read() -> dict[str, Any]:
    try:
        r = json.loads(_reg_path().read_text())
    except (OSError, ValueError):
        r = {}
    r.setdefault("rescue_want", {})
    r.setdefault("parked", {})
    r.setdefault("ops", [])
    return r


def reg_write(r: dict[str, Any]) -> None:
    p = _reg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(r, indent=1))
    tmp.replace(p)


def _reg_op(op: dict[str, Any]) -> None:
    with lock():
        r = reg_read()
        r["ops"] = r["ops"][-49:] + [{"t": _now(), **op}]
        reg_write(r)


def parked_jupsol(r: dict[str, Any]) -> float:
    return sum(float((v or {}).get("jupsol", 0.0)) for v in r.get("parked", {}).values())


def rescue_wanted(r: dict[str, Any], now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    return [a for a, t in r.get("rescue_want", {}).items() if now - float(t or 0) < RESCUE_WANT_MAX_AGE_S]


# ------------------------------------------------------------------ pure maths (tested)
def usable_jupsol(jupsol: float, keep: float, parked: float) -> float:
    """JupSOL that may be lent: the bucket minus keep_jupsol minus JupSOL parked by a bot (its own capital)."""
    return max(0.0, float(jupsol) - float(keep) - float(parked))


def rescue_plan(side: int, size: float, avg: float, margin: float, borrow_usd: float, px: float, jupsol_usd: float,
                c: dict[str, Any]) -> dict[str, Any]:
    """Collateral-only rescue. Liq uses margin - accrued borrow fee. Amount = min(collateral to reach target_pct,
    1/3 of the usable JupSOL after the haircut). EMERGENCY when that moves liq by < emergency_min_push_pct points."""
    from live.rescue import collateral_for, dist_pct, liq_price
    m = c["maint"]
    me = float(margin) - max(float(borrow_usd), 0.0)
    liq0 = liq_price(side, size, avg, me, m)
    d0 = dist_pct(side, px, liq0)
    avail = max(0.0, jupsol_usd) * (1 - c["haircut_pct"] / 100)
    cap = avail * RESCUE_SHARE
    need = collateral_for(side, size, avg, me, px, c["target_pct"], m)
    coll = min(need, cap)
    liq1 = liq_price(side, size, avg, me + coll, m)
    d1 = dist_pct(side, px, liq1)
    return {"liq": round(liq0, 6), "dist_pct": round(d0, 3), "eff_margin": round(me, 6), "need_usd": round(need, 6),
            "cap_usd": round(cap, 6), "collateral_usd": round(coll, 6), "new_liq": round(liq1, 6),
            "new_dist_pct": round(d1, 3), "push_pts": round(d1 - d0, 3), "capped": need > cap,
            "emergency": (d1 - d0) < c["emergency_min_push_pct"], "in_trigger": d0 < c["trigger_pct"]}


def next_add_need_usd(rung_margin: float, cash: float, reserve_short_usd: float = 0.0) -> float:
    """Exactly the shortfall + the $1 inter-bot rounding buffer (no minimum, no cap). 2026-10-28: the shortfall is the
    larger of the bank shortfall (rung margin - free) and the native SOL fee-reserve gap of the add (SOL bot; the lent
    SOL raises the bank balance and the native wallet by the same amount)."""
    return max(0.0, float(rung_margin) - float(cash), float(reserve_short_usd)) + NEXT_ADD_BUFFER_USD


# ------------------------------------------------------------------ measured amounts (tx deltas; quote only as fallback)
def lend_measured(res: dict[str, Any], coin: str) -> tuple[float, float, bool]:
    """(own coin received, JupSOL spent, measured?) of a confirmed JupSOL -> coin swap."""
    d = res.get("deltas") or {}
    measured = bool(d)
    if coin == "SOL":
        got = W.swap_sol_change(d)[0] if d.get("nativeDeltaLamports") is not None else int(res.get("quotedOut") or 0) / LAMPORTS
    else:
        ta = (d.get("tokenDeltaAtoms") or {}).get(coin)
        got = int(ta if ta is not None else res.get("quotedOut") or 0) / 10 ** K.DECIMALS[coin]
        measured = measured and ta is not None
    jd = d.get("jupsolDeltaAtoms")
    js = (-int(jd) if jd is not None else int(res.get("inAmount") or 0)) / LAMPORTS
    return round(got, 9), round(js, 9), measured and jd is not None


def repay_measured(res: dict[str, Any], coin: str) -> tuple[float, float, bool]:
    """(own coin spent incl. a SOL bot's network fee, JupSOL received, measured?) of a confirmed coin -> JupSOL swap."""
    d = res.get("deltas") or {}
    if coin == "SOL":
        ok = d.get("nativeDeltaLamports") is not None
        spent = -W.swap_sol_change(d)[0] if ok else int(res.get("inAmount") or 0) / LAMPORTS
    else:
        ta = (d.get("tokenDeltaAtoms") or {}).get(coin)
        ok = ta is not None
        spent = (-int(ta) if ok else int(res.get("inAmount") or 0)) / 10 ** K.DECIMALS[coin]
    jd = d.get("jupsolDeltaAtoms")
    got = int(jd if jd is not None else res.get("quotedOut") or 0) / LAMPORTS
    return round(spent, 9), round(got, 9), ok and jd is not None


def _fee(res: dict[str, Any]) -> Any:
    return (res.get("deltas") or {}).get("feeLamports") or res.get("feeLamports")


def _stake_row(kind: str, source: str, res: dict[str, Any], jupsol_delta: float, note: str) -> None:
    """One row in live/stake_ledger.csv (JupSOL bucket movements are never trade P&L)."""
    d = res.get("deltas") or {}
    try:
        with ledger.at(ledger.LIVE, "SOL"):
            ledger.record_stake({"time": _now(), "kind": kind, "source": source, "jupsol_delta": round(jupsol_delta, 9),
                                 "wallet_sol_change": W.swap_sol_change(d)[0] if d.get("nativeDeltaLamports") is not None else None,
                                 "fee_sol": (int(d.get("feeLamports") or 0)) / LAMPORTS, "sig": res.get("sig"),
                                 "solscan": f"https://solscan.io/tx/{res.get('sig')}", "note": note})
    except Exception as e:  # noqa: BLE001 - audit row only
        log.warning("JUPSOL stake ledger row failed: %s", e)


def _sol_ledger_external(bot: Any, chg_in: float) -> None:
    """SOL bot, open trade: coin that entered (+) / left (-) the wallet for a loan / park is not trade P&L
    (trades.csv wallet-delta fallback)."""
    if bot.asset == "SOL" and ledger.OPEN.exists():
        try:
            ledger.update(external_out_sol=-float(chg_in))
        except Exception as e:  # noqa: BLE001
            log.warning("JUPSOL open-trade ledger note failed: %s", e)


# ------------------------------------------------------------------ swaps
def _swap(bot: Any, in_mint: str, atoms: int, out_mint: str, swap_mode: str | None) -> tuple[dict[str, Any], str, list[str]]:
    return W.swap_with_backoff(bot.cfg, {"mint": in_mint, "atoms": int(atoms)}, _jup, out_mint=out_mint, swap_mode=swap_mode)


def _fresh_stake(st: dict[str, Any]) -> dict[str, Any]:
    """Fresh JupSOL balance (another bot may have just used some); the tick snapshot as fallback."""
    try:
        s = _jup("stake-state", {})
        if isinstance(s, dict) and s.get("jupsolAtoms") is not None:
            return s
    except Exception as e:  # noqa: BLE001
        log.warning("JUPSOL stake-state read failed (%s): tick snapshot used", e)
    return st.get("stake") or {}


def _jupsol_px_usd(stake: dict[str, Any], sol_px: float) -> float:
    return float(stake.get("solPerJupsol") or 0.0) * float(sol_px)


def peer_borrow_off(bot: Any) -> bool:
    return (bot.dir / PEER_OFF_FILE).exists()


# ------------------------------------------------------------------ "can a peer cover it?" (peers first, as today)
def _peer_local(asset: str) -> dict[str, Any] | None:
    from config import ROOT
    p = ROOT / STATE_FILES[asset]
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def peers_can_cover(bot: Any, need_usd: float, read_peer: Any = None) -> tuple[bool, str]:
    """True when a peer bot could fund this inter-bot request now or shortly (the lender rule of Bot.ib_tick: flat, no
    loan of its own, free (bal - JupSOL liability) >= need + $5); a peer still settling a payout (window > 0) counts as
    "can" (wait for it). One loan at a time: another pair's open record means no peer can fund this bot now."""
    if peer_borrow_off(bot):
        return False, f"peer borrowing switched off ({PEER_OFF_FILE})"
    read_peer = read_peer or _peer_local
    b, me = bot.local.bank, bot.asset
    rec = bot._ib_read()
    s = rec.get("status")
    if rec.get("borrower") == me and s in ("funding", "funded"):
        return True, f"a peer is funding this bot ({s})"
    mine = (b.get("ib_loan") or {}).get("role") == "borrower"
    if s not in (None, "settled", "requested") and rec.get("borrower") != me:
        return False, f"inter-bot loan record busy ({rec.get('borrower')} <- {rec.get('lender')}, {s})"
    if s == "requested" and rec.get("borrower") != me:
        return False, f"inter-bot record holds {rec.get('borrower')}'s request"
    cands = [str(rec.get("lender") or (b.get("ib_loan") or {}).get("peer"))] if mine else [a for a in STATE_FILES if a != me]
    need_ib = max(K.IB_MIN_USD, need_usd)   # what the inter-bot request would ask (bank.ib_need_usd)
    for a in cands:
        loc = read_peer(a) if a in STATE_FILES else None
        pb = (loc or {}).get("bank")
        if not pb:
            continue
        if int(loc.get("side") or 0) != 0:
            continue
        pl = pb.get("ib_loan")
        if pl and not (mine and pl.get("role") == "lender" and pl.get("peer") == me):
            continue
        if int(pb.get("window") or 0) > 0:
            return True, f"{a} is flat and settling a payout (window {pb.get('window')}): wait"
        try:
            px = bot._coin_usd(pb["coin"])
        except Exception as e:  # noqa: BLE001 - unknown price: assume it might cover (never JupSOL on a guess)
            return True, f"{a} price unavailable ({e}): wait"
        free = (float(pb["bal"]) - K.loan_outstanding(pb)) * px
        if free >= need_ib + 5.0:
            return True, f"{a} flat with ${free:.2f} free"
    return False, "no flat peer with enough free balance"


# ------------------------------------------------------------------ next add (called from Bot.rest_add_plan)
def next_add_shortfall(bot: Any, p: Any, cash: float, big: Any, reserve_short_usd: float = 0.0) -> bool:
    """The next rung needs big.margin > cash (or, 2026-10-28, its add would breach the SOL fee reserve by
    reserve_short_usd). Decide who lends. True = skip the inter-bot request (JupSOL will lend / peer borrowing switched
    off); False = file the inter-bot request exactly as today."""
    md, off = mode(bot.cfg), peer_borrow_off(bot)
    if md == "off":
        if off:
            bot.ib_cancel_request(f"peer borrowing switched off ({PEER_OFF_FILE})")
        return off
    need = next_add_need_usd(big.margin, cash, reserve_short_usd)
    can, why = peers_can_cover(bot, need)
    if can:
        bot._jl_nopeer = 0
        bot._jl_want = None
        return False
    bot._jl_nopeer = int(getattr(bot, "_jl_nopeer", 0) or 0) + 1
    if bot._jl_nopeer < NOPEER_TICKS:
        return off
    plan2 = bot.strategy.plan_next_add(p, cash + need, pd.Timestamp.now(tz="UTC"))
    if plan2 is None:
        if _every(bot, "na_infeasible"):
            log.error("JUPSOL next-add: free $%.2f + shortfall $%.2f still gives no ladder plan; nothing lent", cash, need)
        return off
    if md == "observe":
        if _every(bot, "na_observe", 900):
            log.warning("JUPSOL observe: would lend $%.2f (max(rung margin $%.2f - free $%.2f, SOL fee-reserve gap $%.2f) "
                        "+ $1) for the next add (%s); nothing sent", need, big.margin, cash, reserve_short_usd, why)
        return off
    if getattr(bot, "_jl_just_lent", False):   # a loan was booked this tick: the wallet snapshot (st) predates it
        return off
    bot._jl_want = {"need_usd": round(need, 6), "cash": round(cash, 6), "margin": round(float(big.margin), 6),
                    "reserve_short_usd": round(float(reserve_short_usd), 6), "bal": bot.local.bank["bal"],
                    "t": time.time(), "why": why}
    return True


# ------------------------------------------------------------------ the tick (Bot.bank_tick, after ib_tick)
def tick(bot: Any, st: dict[str, Any], pos: Any, price: float) -> bool:
    """True = this bot's wallet moved / may have moved this tick: hold the wallet reconcile. Never raises."""
    try:
        return _tick(bot, st, pos, price)
    except Exception as e:  # noqa: BLE001 - loans never stop the bot
        log.error("JUPSOL tick error (retry next tick): %s", e)
        return True


def _gates_ok(bot: Any, st: dict[str, Any]) -> str:
    b, L = bot.local.bank, bot.local
    if bot._busy(st, False):
        return "an order request is in flight"
    if L.add_pending:
        return "a market add is pending"
    if L.pending_cancels:
        return "cancels pending"
    if b.get("window", 0) > 0:
        return "payout window open"
    if b.get("pend"):
        return "wallet reconcile pending"
    return ""


def _tick(bot: Any, st: dict[str, Any], pos: Any, price: float) -> bool:
    b = bot.local.bank
    if b.get("jl_op"):
        return _resolve_op(bot, st, pos, price)
    if park_tick(bot, st, pos):
        return True
    md = mode(bot.cfg)
    if md == "off" or pos is None:
        bot._jl_want = None
        if pos is None:
            _rescue_want(bot, False)
        return False
    if _rescue_tick(bot, st, pos, price, md):
        return True
    if md == "on" and getattr(bot, "_jl_want", None):
        return _next_add_tick(bot, st, pos)
    return False


def _rescue_want(bot: Any, on: bool) -> None:
    try:
        with lock():
            r = reg_read()
            if on:
                r["rescue_want"][bot.asset] = time.time()
            elif bot.asset in r["rescue_want"]:
                r["rescue_want"].pop(bot.asset, None)
            else:
                return
            reg_write(r)
    except Exception as e:  # noqa: BLE001
        log.warning("JUPSOL registry write failed: %s", e)


def _rescue_tick(bot: Any, st: dict[str, Any], pos: Any, price: float, md: str) -> bool:
    c = rcfg()
    sol_px, cpx = bot.sol_price(st), bot.coin_px(st)
    stake = st.get("stake") or {}
    borrow = bot._accrued_borrow_usd(pos, st)
    r = reg_read()
    usable = usable_jupsol(float(stake.get("jupsol") or 0.0), c["keep_jupsol"], parked_jupsol(r))
    pl = rescue_plan(pos.side, pos.size, pos.avg_entry, pos.margin, borrow, price, usable * _jupsol_px_usd(stake, sol_px), c)
    if not pl["in_trigger"]:
        if bot.asset in r.get("rescue_want", {}):
            _rescue_want(bot, False)
        return False
    if md == "observe":
        if _every(bot, "rescue_observe", 300):
            log.warning("JUPSOL observe: would RESCUE %s: liq %.4f %.2f%% away (borrow fee $%.4f incl.) -> collateral $%.2f "
                        "(need $%.2f, 1/3 cap $%.2f) -> %.2f%% away%s; nothing sent", bot.asset, pl["liq"], pl["dist_pct"],
                        borrow, pl["collateral_usd"], pl["need_usd"], pl["cap_usd"], pl["new_dist_pct"],
                        " EMERGENCY" if pl["emergency"] else "")
        return False
    _rescue_want(bot, True)   # rescues go before every next-add loan (all bots)
    if bot.killed():
        if _every(bot, "rescue_kill", 300):
            log.error("JUPSOL RESCUE skipped: KILL present (%s liq %.2f%% away)", bot.asset, pl["dist_pct"])
        return False
    why = _gates_ok(bot, st)
    if why:
        if _every(bot, "rescue_gate", 300):
            log.warning("JUPSOL RESCUE waiting: %s (liq %.2f%% away)", why, pl["dist_pct"])
        return False
    b, coin = bot.local.bank, bot.local.bank["coin"]
    with lock():
        stake = _fresh_stake(st)
        usable = usable_jupsol(float(stake.get("jupsol") or 0.0), c["keep_jupsol"], parked_jupsol(reg_read()))
        pl = rescue_plan(pos.side, pos.size, pos.avg_entry, pos.margin, borrow, price,
                         usable * _jupsol_px_usd(stake, sol_px), c)
        dec = K.DECIMALS[coin]
        atoms = int(pl["collateral_usd"] / cpx * 10 ** dec) if cpx > 0 else 0
        if atoms <= 0:
            if _every(bot, "rescue_empty", 300):
                log.error("JUPSOL RESCUE EMERGENCY %s: liq %.2f%% away and no usable JupSOL (%.9f)", bot.asset,
                          pl["dist_pct"], usable)
            return False
        if pl["emergency"]:
            log.error("JUPSOL RESCUE EMERGENCY %s: collateral $%.2f (1/3 cap $%.2f, need $%.2f) moves liq only %.2f pts "
                      "(< %.2f): notify Trevor", bot.asset, pl["collateral_usd"], pl["cap_usd"], pl["need_usd"],
                      pl["push_pts"], c["emergency_min_push_pct"])
        b["jl_op"] = {"kind": "rescue", "stage": "lend_sending", "t": time.time(), "atoms": atoms, "plan": pl}
        bot.save()
        res, kind, att = _swap(bot, K.MINT["JUPSOL"], atoms, K.MINT[coin], "ExactOut")
        bot.bank_fee(_fee(res))
        return _after_lend(bot, res, kind, att, "rescue", pos, price, cpx)


def _next_add_tick(bot: Any, st: dict[str, Any], pos: Any) -> bool:
    w, b = bot._jl_want, bot.local.bank
    if time.time() - float(w["t"]) > WANT_MAX_AGE_S or abs(float(w["bal"]) - b["bal"]) > 1e-12:
        bot._jl_want = None   # stale (bal moved): re-decided by the next rest_add_plan
        return False
    why = _gates_ok(bot, st) or ("own-coin booking pending" if b.get("book_pend") else "")
    if why:
        return False
    if rescue_wanted(reg_read()):
        if _every(bot, "na_rescue", 300):
            log.warning("JUPSOL next-add loan waits: a rescue is in trigger (%s)", rescue_wanted(reg_read()))
        return False
    with K.journal_lock():   # the inter-bot request: a peer that already started funding wins; else withdraw it
        rec = bot._ib_read()
        if rec.get("borrower") == bot.asset and rec.get("status") in ("funding", "funded"):
            bot._jl_want = None
            return False
    bot.ib_cancel_request("next add funded from JupSOL (no peer could cover it)")
    rec = bot._ib_read()
    if rec.get("borrower") == bot.asset and rec.get("status") in ("funding", "funded", "requested", "topup_requested"):
        bot._jl_want = None   # a peer took it between the checks
        return False
    c, coin = rcfg(), b["coin"]
    sol_px, cpx = bot.sol_price(st), bot.coin_px(st)
    need = float(w["need_usd"])
    with lock():
        stake = _fresh_stake(st)
        usable = usable_jupsol(float(stake.get("jupsol") or 0.0), c["keep_jupsol"], parked_jupsol(reg_read()))
        jpx = _jupsol_px_usd(stake, sol_px)
        avail_usd = usable * jpx * (1 - c["haircut_pct"] / 100)
        if jpx <= 0 or need > avail_usd:
            bot._jl_want = None
            if _every(bot, "na_short"):
                log.error("JUPSOL next-add: needs $%.2f but only $%.2f of JupSOL is usable; nothing lent", need, avail_usd)
            return False
        atoms = int(need / cpx * 10 ** K.DECIMALS[coin])
        b["jl_op"] = {"kind": "next_add", "stage": "lend_sending", "t": time.time(), "atoms": atoms, "want": w}
        bot.save()
        bot._jl_want = None
        res, kind, att = _swap(bot, K.MINT["JUPSOL"], atoms, K.MINT[coin], "ExactOut")
        bot.bank_fee(_fee(res))
        return _after_lend(bot, res, kind, att, "next_add", pos, None, cpx)


def _after_lend(bot: Any, res: dict[str, Any], kind: str, att: list[str], what: str, pos: Any, price: float | None,
                cpx: float) -> bool:
    b = bot.local.bank
    op = b["jl_op"]
    if kind != "ok":
        if kind == "unknown":
            op.update(stage="lend_unknown", sig=res.get("sig"), err=str(res.get("error") or res.get("err"))[:200])
            bot.save()
            K.write_alert(bot.asset, f"JupSOL {what} loan swap outcome unknown ({att}); resolved from the tx if it has a "
                                     f"sig, else check the wallet vs bank (bank.jl_op)")
            log.error("JUPSOL %s loan swap outcome UNKNOWN (%s): not re-sent; resolving", what, att)
            return True
        b.pop("jl_op", None)
        bot.save()
        log.error("JUPSOL %s loan swap failed (%s): %s; nothing sent", what, kind, att)
        return False
    _book_lend(bot, res, what, cpx)
    if what == "rescue":
        _rescue_add(bot, pos, price)
    return True


def _book_lend(bot: Any, res: dict[str, Any], what: str, cpx: float) -> None:
    b, coin = bot.local.bank, bot.local.bank["coin"]
    got, js, measured = lend_measured(res, coin)
    before = {k: b[k] for k in ("hwm", "wd_mark")}
    bot._jl_just_lent = True   # this tick's wallet snapshot predates the loan (reset by Bot.bank_tick)
    K.loan_open(b, got, js, 0.0, f"JupSOL {what} loan: {js:.9f} JupSOL -> {got:.9f} {coin} sig {res.get('sig')}"
                + ("" if measured else " (QUOTE fallback: tx deltas missing)"), kind=what, sig=res.get("sig"))
    assert {k: b[k] for k in ("hwm", "wd_mark")} == before
    bot.local.day_start_equity += got * cpx   # daily-loss baseline: borrowed coin is not a gain (nor its repay a loss)
    _sol_ledger_external(bot, got)
    _stake_row("loan_out", f"jupsol-{what}-{bot.asset}", res, -js, f"{js:.9f} JupSOL lent -> {got:.9f} {coin}")
    op = b["jl_op"]
    op.update(stage="lent", sig=res.get("sig"), got=got, jupsol=js)
    if what != "rescue":
        b.pop("jl_op", None)
    bot.save()
    _reg_op({"bot": bot.asset, "op": f"lend_{what}", "coin": got, "jupsol": js, "sig": res.get("sig")})
    log.error("JUPSOL %s LOAN: %.9f JupSOL -> %.9f %s (~$%.2f) sig %s; liability %.9f %s, %.9f JupSOL owed; "
              "hwm %.9f / wd_mark %.9f unchanged%s", what.upper().replace("_", "-"), js, got, coin, got * cpx,
              res.get("sig"), K.loan_outstanding(b), coin, K.loan_jupsol_owed(b), b["hwm"], b["wd_mark"],
              "" if measured else " (QUOTE fallback)")


def _rescue_add(bot: Any, pos: Any, price: float | None) -> None:
    """Deposit the borrowed coin as collateral (increase, sizeUsd 0, the open_or_add builder args)."""
    b, coin, asset = bot.local.bank, bot.local.bank["coin"], bot.asset
    op = b["jl_op"]
    got = float(op["got"])
    dec = K.DECIMALS[coin]
    slip = bot.cfg.live.price_slippage_pct
    px = float(price or 0.0) or (bot._mkt_px or 0.0)
    cpx = bot._coin_px_now() or 0.0
    args: dict[str, Any] = {"side": "long" if pos.side > 0 else "short", "collateralAtoms": str(int(round(got * 10 ** dec))),
                            "sizeUsd": 0, "priceSlippageUsd": round(px * (1 + slip) if pos.side > 0 else px * (1 - slip), 2)}
    if asset != "SOL":
        args["inputMint"] = K.MINT[coin]
    if pos.side < 0:
        args["jupiterMinimumOut"] = str(int(got * cpx * (1 - slip) * 1_000_000))
    op.update(stage="add_sending", add_args=args)
    bot.save()
    r = bot.cmd("increase", args)
    if r.get("sig") or r.get("ok"):
        if asset == "SOL" and r.get("sig"):
            try:
                ledger.update(fee_lamports=r.get("feeLamports"))   # cmd() leaves increase fees to the caller
            except Exception as e:  # noqa: BLE001
                log.warning("JUPSOL rescue fee ledger note failed: %s", e)
        K.debit(b, got, "collateral", f"JupSOL rescue collateral {got:.9f} {coin} sig {r.get('sig')}")
        b["window"] = max(b.get("window", 0), bot.cfg.ledger.window_ticks)   # a refunded request = own-coin payout
        b.pop("jl_op", None)
        bot.save()
        _reg_op({"bot": asset, "op": "rescue_collateral", "coin": got, "sig": r.get("sig")})
        log.error("JUPSOL RESCUE collateral sent: %.9f %s (~$%.2f) sig %s; liq was %.2f%% away -> ~%.2f%% (planned)", got,
                  coin, got * cpx, r.get("sig"), op["plan"]["dist_pct"], op["plan"]["new_dist_pct"])
        return
    op.update(stage="add_retry", add_err=str(r.get("error") or r.get("err"))[:200])
    bot.save()
    log.error("JUPSOL RESCUE collateral add failed (%s): the borrowed %.9f %s stays in bal; retried next tick",
              op["add_err"], got, coin)


def _resolve_op(bot: Any, st: dict[str, Any], pos: Any, price: float) -> bool:
    """A journalled op from an earlier tick: finish it (collateral add retry) or resolve an unknown send."""
    b = bot.local.bank
    op = b["jl_op"]
    stage = op.get("stage")
    if op.get("kind") == "rescue" and stage in ("lent", "add_retry"):
        if pos is None:
            b.pop("jl_op", None)
            bot.save()
            log.error("JUPSOL rescue: position closed before the collateral add; the borrowed coin stays in bal (repaid on flat)")
            return False
        if _gates_ok(bot, st):
            return False
        _rescue_add(bot, pos, price)
        return True
    if op.get("kind") == "rescue" and stage == "add_sending":   # crash during the add send: never re-send blind
        if _every(bot, "op_check", 900):
            K.write_alert(bot.asset, "JupSOL rescue collateral add outcome unknown after a restart; check the position "
                                     "collateral vs bank (bank.jl_op: debit the collateral if it landed), then delete bank.jl_op")
            log.error("JUPSOL rescue collateral add outcome UNKNOWN (bank.jl_op): manual check; nothing re-sent")
        return False
    if stage in ("lend_unknown", "lend_sending", "repay_unknown", "repay_sending", "park_unknown", "park_sending"):
        sig = op.get("sig")
        if not sig:
            if _every(bot, "op_nosig", 900):
                K.write_alert(bot.asset, f"JupSOL {op.get('kind')} send without a known sig (bank.jl_op {stage}); check "
                                         f"the wallet (JupSOL / coin) vs bank, book by hand, then delete bank.jl_op")
                log.error("JUPSOL %s op %s has no sig: manual check (bank.jl_op); JupSOL loans held for this bot",
                          op.get("kind"), stage)
            return True
        r = _jup("tx-deltas", {"sig": sig})
        if r.get("ok") and r.get("found") and (r.get("deltas") or {}).get("err") is None:
            res = {"ok": True, "sig": sig, "deltas": r.get("deltas")}
            bot.bank_fee(_fee(res))
            cpx = bot.coin_px(st)
            if stage.startswith("lend"):
                _book_lend(bot, res, op.get("kind", "rescue"), cpx)
                if op.get("kind") == "rescue" and pos is not None and not _gates_ok(bot, st):
                    _rescue_add(bot, pos, price)
            elif stage.startswith("repay"):
                _book_repay(bot, res, op, cpx)
            else:
                _book_park(bot, res, op, cpx)
            return True
        if (r.get("ok") and r.get("found")) or (r.get("ok") and time.time() - float(op.get("t") or 0) > UNKNOWN_GIVEUP_S):
            b.pop("jl_op", None)
            bot.save()
            log.error("JUPSOL %s tx %s %s: not landed, nothing booked", op.get("kind"), sig,
                      "failed on-chain" if r.get("found") else f"not found after {UNKNOWN_GIVEUP_S:.0f}s")
            return True
        return True
    b.pop("jl_op", None)
    bot.save()
    return False


# ------------------------------------------------------------------ repay (Bot.bank_tick, flat, after the inter-bot loan)
def repay_due_now(bot: Any) -> bool:
    b = bot.local.bank
    ln = b.get("loan") or {}
    if not ln or K.loan_jupsol_owed(b) <= 0 or b.get("jl_op"):
        return False
    if b.get("pend") or b.get("book_pend") or b.get("trueup_block") or b.get("window", 0) > 0:
        return False
    if (b.get("ib_loan") or {}).get("role") == "borrower":
        return False   # the peer is repaid first
    if "carry_flat" in ln and ln["carry_flat"] == bot.local.flat_since:
        if _every(bot, "carry"):
            log.warning("JUPSOL loan: %.9f JupSOL still owed, carried to this bot's next flat (deposits do not pay it)",
                        K.loan_jupsol_owed(b))
        return False
    return True


def repay_tick(bot: Any, sol_px: float, cpx: float) -> None:
    """Exact JupSOL quantity back (ExactOut own coin -> JupSOL); not enough balance -> ExactIn of what there is, the rest
    is carried to the next flat. Errors are logged and retried next tick."""
    b, coin = bot.local.bank, bot.local.bank["coin"]
    dec = K.DECIMALS[coin]
    owed_atoms = int(round(K.loan_jupsol_owed(b) * LAMPORTS))
    if owed_atoms <= 0:
        return
    keep = bot.cfg.ledger.close_network_native if bot.asset == "SOL" else K.fee_debt_native(b, sol_px, cpx)
    avail_atoms = int(max(0.0, b["bal"] - keep) * 10 ** dec)
    q = _jup("swap-quote", W.sweep_args(bot.cfg, K.MINT[coin], owed_atoms, K.MINT["JUPSOL"], "ExactOut"))
    if not q.get("ok") or q.get("inAmount") is None:
        if _every(bot, "repay_quote", 600):
            log.error("JUPSOL repay quote failed (%s); retry next tick", str(q.get("error") or q)[:160])
        return
    bps = int(bot.cfg.withdraw.unstake_slippage_bps)
    max_in = int(q.get("otherAmountThreshold") or int(int(q["inAmount"]) * (1 + bps / 10_000)) + 1)
    partial = max_in > avail_atoms
    if partial and avail_atoms <= 0:
        b["loan"]["carry_flat"] = bot.local.flat_since
        bot.save()
        log.error("JUPSOL repay: no free balance; %.9f JupSOL carried to the next flat", owed_atoms / LAMPORTS)
        return
    b["jl_op"] = {"kind": "repay", "stage": "repay_sending", "t": time.time(), "partial": partial,
                  "atoms": avail_atoms if partial else owed_atoms}
    bot.save()
    if partial:
        res, kind, att = _swap(bot, K.MINT[coin], avail_atoms, K.MINT["JUPSOL"], None)
    else:
        res, kind, att = _swap(bot, K.MINT[coin], owed_atoms, K.MINT["JUPSOL"], "ExactOut")
    bot.bank_fee(_fee(res))
    if kind != "ok":
        if kind == "unknown":
            b["jl_op"].update(stage="repay_unknown", sig=res.get("sig"))
            bot.save()
            K.write_alert(bot.asset, f"JupSOL repay swap outcome unknown ({att}); resolved from the tx if it has a sig")
            log.error("JUPSOL repay swap outcome UNKNOWN (%s): not re-sent; resolving", att)
            return
        b.pop("jl_op", None)
        bot.save()
        log.error("JUPSOL repay swap failed (%s): %s; retry next tick", kind, att)
        return
    _book_repay(bot, res, b["jl_op"], cpx)


def _book_repay(bot: Any, res: dict[str, Any], op: dict[str, Any], cpx: float) -> None:
    b, coin = bot.local.bank, bot.local.bank["coin"]
    spent, got, measured = repay_measured(res, coin)
    before = {k: b[k] for k in ("hwm", "wd_mark")}
    owed_before = K.loan_jupsol_owed(b)
    r = K.loan_repay_jupsol(b, spent, got, f"JupSOL repay: {spent:.9f} {coin} -> {got:.9f} JupSOL "
                                           f"({'partial ExactIn' if op.get('partial') else 'ExactOut'}) sig {res.get('sig')}"
                                           + ("" if measured else " (QUOTE fallback)"))
    assert {k: b[k] for k in ("hwm", "wd_mark")} == before
    bot.local.day_start_equity -= spent * cpx
    b["window"] = max(b.get("window", 0), bot.cfg.ledger.window_ticks)
    b.pop("jl_op", None)
    if not r["closed"] and op.get("partial"):
        b["loan"]["carry_flat"] = bot.local.flat_since
    _stake_row("loan_repay", f"jupsol-repay-{bot.asset}", res, got, f"{spent:.9f} {coin} -> {got:.9f} JupSOL repaid")
    restored: list[str] = []
    if r["closed"]:
        try:
            from live import rescue
            restored = rescue.restore_settings({"SOL": "sol", "ETH": "eth", "BTC": "btc"}[bot.asset], b, bot.cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("JUPSOL rescue settings restore failed: %s", e)
    bot.save()
    _reg_op({"bot": bot.asset, "op": "repay", "coin": spent, "jupsol": got, "sig": res.get("sig"), "closed": r["closed"]})
    log.error("JUPSOL LOAN %s: %.9f %s -> %.9f JupSOL (owed %.9f) sig %s; liability cleared %.9f, cost %+.9f %s "
              "(through bal, not trade P&L); hwm %.9f / wd_mark %.9f unchanged%s%s",
              "REPAID" if r["closed"] else "PART-REPAID", spent, coin, got, owed_before, res.get("sig"), r["cleared"],
              r["cost"], coin, b["hwm"], b["wd_mark"],
              "" if r["closed"] else f"; {r['owed_jupsol']:.9f} JupSOL still owed"
              + (" (carried to the next flat)" if op.get("partial") else " (retried next tick)"),
              f"; restored {restored}" if restored else "")


# ------------------------------------------------------------------ park / unpark (live test tool, request file)
def park_tick(bot: Any, st: dict[str, Any], pos: Any) -> bool:
    """<state dir>/JUPSOL_PARK = {"op": "park", "native": x}: x own coin -> JupSOL, booked as a receivable (bank.park:
    equity / hwm / wd_mark unchanged, free balance down). {"op": "unpark"}: the parked JupSOL -> own coin back. Processed
    once (file removed, result in JUPSOL_PARK.result). Any mode; nothing happens without the file."""
    f = bot.dir / PARK_FILE
    if not f.exists() or bot.live.dry_run:
        return False
    b, coin = bot.local.bank, bot.local.bank["coin"]
    why = _gates_ok(bot, st)
    if why:
        if _every(bot, "park_wait", 300):
            log.warning("JUPSOL park request waits: %s", why)
        return False

    def done(msg: str) -> bool:
        f.unlink(missing_ok=True)
        (bot.dir / (PARK_FILE + ".result")).write_text(json.dumps({"t": _now(), "result": msg}) + "\n")
        log.error("JUPSOL park request: %s", msg)
        return True
    try:
        req = json.loads(f.read_text() or "{}")
    except ValueError:
        return done("unreadable request (expected JSON); ignored")
    dec = K.DECIMALS[coin]
    if req.get("op") == "park":
        amt = float(req.get("native") or 0)
        free = b["bal"] - K.loan_outstanding(b) - (bot.cfg.ledger.close_network_native if bot.asset == "SOL" else 0.0)
        if amt <= 0 or amt > free:
            return done(f"park {amt} {coin} refused: must be > 0 and <= free {free:.9f}")
        b["jl_op"] = {"kind": "park", "stage": "park_sending", "t": time.time(), "op": "park", "atoms": int(amt * 10 ** dec)}
        bot.save()
        res, kind, att = _swap(bot, K.MINT[coin], int(amt * 10 ** dec), K.MINT["JUPSOL"], None)
    elif req.get("op") == "unpark":
        pk = b.get("park")
        if not pk:
            return done("unpark refused: nothing parked")
        b["jl_op"] = {"kind": "park", "stage": "park_sending", "t": time.time(), "op": "unpark",
                      "atoms": int(round(float(pk["jupsol"]) * LAMPORTS))}
        bot.save()
        res, kind, att = _swap(bot, K.MINT["JUPSOL"], int(round(float(pk["jupsol"]) * LAMPORTS)), K.MINT[coin], None)
    else:
        return done(f"unknown op {req.get('op')!r}; ignored")
    bot.bank_fee(_fee(res))
    if kind != "ok":
        if kind == "unknown":
            b["jl_op"].update(stage="park_unknown", sig=res.get("sig"))
            bot.save()
            return done(f"{req.get('op')} swap outcome UNKNOWN ({att}); resolved from the tx (bank.jl_op)")
        b.pop("jl_op", None)
        bot.save()
        return done(f"{req.get('op')} swap failed ({kind}): {att}; nothing booked")
    _book_park(bot, res, b["jl_op"], bot.coin_px(st))
    return done(f"{req.get('op')} done sig {res.get('sig')}; bank {json.dumps({k: b.get(k) for k in ('bal', 'hwm', 'wd_mark', 'park')})}")


def _book_park(bot: Any, res: dict[str, Any], op: dict[str, Any], cpx: float) -> None:
    b, coin = bot.local.bank, bot.local.bank["coin"]
    before = {k: b[k] for k in ("hwm", "wd_mark")}
    if op.get("op") == "park":
        spent, got, measured = repay_measured(res, coin)   # coin -> JupSOL: same delta reading as a repay
        K.park_open(b, spent, got, f"parked {spent:.9f} {coin} -> {got:.9f} JupSOL (receivable, not a withdrawal) sig "
                                   f"{res.get('sig')}")
        bot.local.day_start_equity -= spent * cpx
        _sol_ledger_external(bot, -spent)
        _stake_row("park", f"jupsol-park-{bot.asset}", res, got, f"{spent:.9f} {coin} parked (bot capital, not profit)")
        with lock():
            r = reg_read()
            r["parked"][bot.asset] = {"jupsol": round(float((r["parked"].get(bot.asset) or {}).get("jupsol", 0.0)) + got, 9)}
            reg_write(r)
        log.error("JUPSOL PARK %s: %.9f %s -> %.9f JupSOL sig %s; bal %.9f, parked %.9f (equity / hwm %.9f / wd_mark %.9f "
                  "unchanged)", bot.asset, spent, coin, got, res.get("sig"), b["bal"], K.park_adj(b), b["hwm"], b["wd_mark"])
    else:
        got, js, measured = lend_measured(res, coin)
        cost = K.park_close(b, got, f"unparked {js:.9f} JupSOL -> {got:.9f} {coin} sig {res.get('sig')}")
        bot.local.day_start_equity += got * cpx
        _sol_ledger_external(bot, got)
        _stake_row("unpark", f"jupsol-unpark-{bot.asset}", res, -js, f"{js:.9f} JupSOL -> {got:.9f} {coin} unparked")
        with lock():
            r = reg_read()
            r["parked"].pop(bot.asset, None)
            reg_write(r)
        log.error("JUPSOL UNPARK %s: %.9f JupSOL -> %.9f %s sig %s; difference %+.9f %s through bal (swap cost / JupSOL "
                  "move, not profit); hwm %.9f / wd_mark %.9f unchanged", bot.asset, js, got, coin, res.get("sig"), cost,
                  coin, b["hwm"], b["wd_mark"])
    assert {k: b[k] for k in ("hwm", "wd_mark")} == before
    b["window"] = max(b.get("window", 0), bot.cfg.ledger.window_ticks)
    b.pop("jl_op", None)
    bot.save()
    _reg_op({"bot": bot.asset, "op": op.get("op"), "sig": res.get("sig")})
