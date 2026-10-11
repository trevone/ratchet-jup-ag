"""SOL in / SOL out (multi-asset, Trevor 2026-10-07): SOL-market closes ask Jupiter for SOL (createDecreasePositionRequest2
desiredMint = native SOL, the keeper swaps USDC collateral). ETH/BTC markets reject desiredMint = SOL (InvalidMint 6018,
verified by simulation), so their closes pay the collateral token (ETH / WBTC on longs, USDC on shorts) and the bot
swaps it back to SOL with the same Jupiter swap code as the stake bucket (live/src/stake.ts buildSwap,
"sweep" direction: input locked to USDC / ETH / WBTC, output native SOL into the owner's own account, slippage capped).

Attribution (so nothing is counted twice across bots): the ETH bot sweeps ETH, the BTC bot sweeps WBTC, USDC is swept by
the bot whose trade was a short at that moment. The swap's SOL change (native + wSOL, net of fees) is booked into that
bot's open trade record (``sweep_sol``) and added to the trade's net_sol on flat (the swap tx never touches the position,
so position flows alone would miss it). Before the multi-asset launch the wallet held no USDC / ETH / WBTC.

Rent (2026-10-07): the USDC / ETH / WBTC / wSOL token accounts are persistent and reused (live/src/jup.ts); any rent a swap
still moves into / out of them is booked as ``rent_sol`` (recoverable capital), never as part of the swap's SOL result.

Self-healing sweep (2026-10-08, after the 01:59 MT ETH close whose $5.22 USDC swap-back hit Jupiter 429s and was never
retried because the bot was flat): ``self_heal`` runs on EVERY tick of every bot, flat or in a position.
  * Any USDC / ETH / WBTC balance above dust (bot.sweep_min_usd) is swept to native SOL. Never JupSOL / wSOL (and the TS
    side only accepts the three sweep mints).
  * Each attempt asks one Jupiter API base: withdraw.stake_api_bases in order (api.jup.ag, then lite-api.jup.ag). A
    429 / 5xx / timeout before anything is sent tries the next base, then waits 2 s, 4 s, ... (exponential, jittered) for
    up to ROUNDS_PER_TICK rounds. If it still fails, the token gets a shared cross-tick backoff (60 s, 120 s, 240 s, ...,
    max 15 min) that every bot respects, and later ticks keep retrying forever. A failure after the tx was sent is never
    retried in the same tick (the next tick re-reads the balance, so nothing is swapped twice). The slippage / price-impact
    caps are unchanged (sweep_args).
  * One sweeper at a time across the 3 processes: an exclusive flock on live/sweep.lock. live/sweep_state.json (written
    only under that lock) holds each pending token's claim, its backoff and the last confirmed sweep. A bot whose
    balance view predates another bot's sweep of that token skips it.
  * Attribution, fixed when the balance first appears (or grows): ETH -> the ETH bot, WBTC -> the BTC bot (long
    closes); USDC -> the open short (priority ETH > BTC > SOL, approximate when two are short at once, as before), else
    a short that has just closed but isn't booked yet, else the most recently closed short trade. If that trade is
    still open, only its own bot sweeps and books into ledger_open.json (sweep_sol). Once it is booked, the sweep is a
    late ``sweep`` row in THAT bot's trades.csv, carrying the trade's open_sig / position. Its own bot sweeps
    immediately; any other bot takes over after FOREIGN_GRACE_SEC (e.g. the owner runs older code or is down).
"""
from __future__ import annotations

import csv
import fcntl
import json
import logging
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from live import ledger

log = logging.getLogger("sweep")
LAMPORTS = 1_000_000_000
DECIMALS = {"USDC": 6, "ETH": 8, "WBTC": 8}
ASSET_TOKEN = {"ETH": "ETH", "BTC": "WBTC"}     # long collateral token of each market (SOL longs are SOL already)
Jup = Callable[[str, dict[str, Any]], dict[str, Any]]


def tokens_for(asset: str, side: int | None) -> list[str]:
    """Tokens this bot is responsible for: its own long collateral token, plus USDC while its trade is a short."""
    out = [ASSET_TOKEN[asset]] if asset in ASSET_TOKEN else []
    if side is not None and side < 0:
        out.append("USDC")
    return out


def token_usd(name: str, atoms: int, prices: dict[str, float]) -> float:
    px = 1.0 if name == "USDC" else prices.get(name, 0.0)
    return atoms / 10 ** DECIMALS[name] * px


def plan_sweeps(st: dict[str, Any], asset: str, side: int | None, prices: dict[str, float], min_usd: float) -> list[dict[str, Any]]:
    toks = st.get("sweepTokens") or {}
    if "error" in toks:
        return []
    out = []
    names = tokens_for(asset, side)
    # Tie-break so two bots never swap the same USDC at once: priority ETH > BTC > SOL among bots holding a short
    # (SOL shorts are paid in SOL by the keeper anyway; the SOL bot only sweeps USDC when no ETH/BTC short is open).
    ahead = {"ETH": (), "BTC": ("ETH",), "SOL": ("ETH", "BTC")}.get(asset, ())
    if "USDC" in names and any(p.get("market") in ahead and p.get("side") == "short" for p in st.get("allPositions") or []):
        names.remove("USDC")
    for name in names:
        t = toks.get(name) or {}
        atoms = int(t.get("atoms") or 0)
        if atoms > 0 and token_usd(name, atoms, prices) >= min_usd:
            out.append({"name": name, "mint": t["mint"], "atoms": atoms, "usd": round(token_usd(name, atoms, prices), 4)})
    return out


def sweep_args(cfg: Any, mint: str, atoms: int, out_mint: str | None = None) -> dict[str, Any]:
    """Swap args (slippage / impact caps from [withdraw]). out_mint None = old "sweep" to SOL; else the generic "swap"
    direction (own-coin ledger: USDC -> the bot's own coin, coin -> JupSOL, SOL <-> coin; pairs allowlisted in stake.ts)."""
    w = cfg.withdraw
    bps = int(w.unstake_slippage_bps)
    if not 1 <= bps <= w.stake_max_slippage_bps:
        raise ValueError("sweep slippage outside 1..withdraw.stake_max_slippage_bps")
    a = {"direction": "sweep", "inputMint": mint, "amountAtoms": str(int(atoms)), "slippageBps": bps,
         "maxPriceImpactPct": w.stake_max_price_impact_pct, "apiBases": list(w.stake_api_bases),
         "onlyDirectRoutes": w.stake_only_direct_routes}
    if out_mint is not None:
        a.update(direction="swap", outputMint=out_mint)
    return a


def swap_sol_change(d: dict[str, Any]) -> tuple[float, float]:
    """(SOL result, token-account rent) of one confirmed swap from its tx deltas (live/src/stake.ts txDeltas).

    Rent fix 2026-10-07: SOL result = native + wrapped-SOL change + ``rentDeltaLamports``: rent that moved into (or out
    of) the owner's own token accounts (e.g. a re-created wSOL ATA, ~0.0015 SOL) is still the owner's recoverable capital,
    not a swap cost. Older CLI output without that field -> rent 0 (old formula)."""
    rent = int(d.get("rentDeltaLamports") or 0) / LAMPORTS
    sol = d["nativeDeltaLamports"] / LAMPORTS + int(d.get("wsolDeltaAtoms") or 0) / LAMPORTS + rent
    return round(sol, 9), round(rent, 9)


def run_sweeps_detail(cfg: Any, plans: list[dict[str, Any]], send: bool, jup: Jup) -> dict[str, float]:
    """Swap each planned token to SOL. Returns {"sol": SOL result of the confirmed swaps (rent excluded), "rent_sol":
    token-account rent parked (+) / refunded (-) by them} (both 0.0 in dry run / on failure)."""
    total, rent_total = 0.0, 0.0
    for p in plans:
        args = sweep_args(cfg, p["mint"], p["atoms"])
        if send:
            os.environ["JUP_LIVE"] = "1"
        res = jup("swap" if send else "simulate-swap", {**args, "send": send})
        d = res.get("deltas") or {}
        chg, rent = None, 0.0
        if d.get("nativeDeltaLamports") is not None:
            chg, rent = swap_sol_change(d)
        elif send and res.get("ok") and res.get("quotedOut"):
            chg = int(res["quotedOut"]) / LAMPORTS   # tx meta unavailable: quote (logged)
        log.info("SWEEP %s %d atoms (~$%.2f) -> SOL: %s", p["name"], p["atoms"], p["usd"],
                 {k: res.get(k) for k in ("ok", "sig", "err", "error", "quotedOut", "route")} | {"sol_change": chg, "rent_sol": rent})
        if send and res.get("ok") and res.get("sig") and chg is not None:
            total += chg
            rent_total += rent
    return {"sol": round(total, 9), "rent_sol": round(rent_total, 9)}


def run_sweeps(cfg: Any, plans: list[dict[str, Any]], send: bool, jup: Jup) -> float:
    """SOL result of the confirmed swaps (rent excluded); see run_sweeps_detail."""
    return run_sweeps_detail(cfg, plans, send, jup)["sol"]


# ------------------------------------------------------------------ self-healing sweep (2026-10-08)
ROOT = Path(__file__).resolve().parent.parent            # tests redirect this (conftest)
SHARED_DIR = "live"                                       # sweep.lock, sweep_state.json, sweep_px_<TOKEN>.json
BOT_DIRS = {"SOL": "live", "ETH": "live/eth", "BTC": "live/btc"}   # each bot's [bot] state_dir (ledger files)
SWEEPABLE = ("USDC", "ETH", "WBTC")                       # never JupSOL / wSOL
LONG_TOKEN_BOT = {"ETH": "ETH", "WBTC": "BTC"}            # long closes of the ETH / BTC markets pay these
USDC_PRIORITY = ("ETH", "BTC", "SOL")                     # shorts pay USDC (SOL shorts ask the keeper for SOL)
ROUNDS_PER_TICK = 2                                       # in-tick rounds over all API bases
BACKOFF_SEC = 2.0                                         # in-tick wait before round 2, 3, ...: 2 s, 4 s, ...
TICK_BACKOFF_SEC = 60.0                                   # cross-tick backoff after a failed tick: 60, 120, 240 s ...
TICK_BACKOFF_MAX_SEC = 900.0
FOREIGN_GRACE_SEC = 300.0                                 # another bot takes over a booked trade's tokens after this
PRICE_MAX_AGE_SEC = 3600.0                                # ETH / WBTC price cache (for the dust check by other bots)
LOCK_WAIT_SEC = 5.0
_RETRYABLE = re.compile(r"\b(429|5\d\d)\b|timeout|timed out|fetch failed|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|"
                        r"socket hang up|aborted", re.I)


def _shared(name: str) -> Path:
    return ROOT / SHARED_DIR / name


def _utc(ts: float) -> str:
    return str(datetime.fromtimestamp(ts, timezone.utc))


def classify(res: dict[str, Any]) -> str:
    """ok | retry (Jupiter API 429 / 5xx / timeout BEFORE anything was sent: safe to ask again) | unknown (sent or
    possibly sent but unconfirmed: never re-sent this tick) | fatal (e.g. quote rejected, price impact over the cap)."""
    if res.get("ok") and res.get("sig"):
        return "ok"
    if res.get("sent") or res.get("sig"):
        return "unknown"
    err = str(res.get("error") or res.get("err") or "")
    if "jupiter api failed" in err or err.startswith("Error: swap-instructions") or "swap-instructions:" in err:
        return "retry" if _RETRYABLE.search(err) else "fatal"
    if "quote mismatch" in err or "price impact" in err or "not allowed" in err or "slippageBps" in err:
        return "fatal"
    return "unknown"   # e.g. CLI crash / RPC error during send: could have landed -> re-read next tick


def swap_with_backoff(cfg: Any, plan: dict[str, Any], jup: Jup, sleep: Callable[[float], None] = time.sleep,
                      rounds: int = ROUNDS_PER_TICK, out_mint: str | None = None) -> tuple[dict[str, Any], str, list[str]]:
    """One token -> SOL. Each attempt pins ONE API base (api.jup.ag, then lite-api.jup.ag); only 'retry' failures are
    asked again, with exponential backoff between rounds. Returns (last result, kind, attempt log)."""
    bases = list(cfg.withdraw.stake_api_bases)
    log_: list[str] = []
    res: dict[str, Any] = {"ok": False, "error": "no attempt"}
    kind = "fatal"
    for r in range(max(1, rounds)):
        if r:
            wait = BACKOFF_SEC * 2 ** (r - 1) * (1.0 + 0.25 * random.random())
            log_.append(f"wait {wait:.1f}s")
            sleep(wait)
        for base in bases:
            args = {**sweep_args(cfg, plan["mint"], plan["atoms"], out_mint), "apiBases": [base], "send": True}
            os.environ["JUP_LIVE"] = "1"
            try:
                res = jup("swap", args)
            except Exception as e:  # noqa: BLE001 - CLI timeout / crash: outcome unknown
                res = {"ok": False, "error": f"cli exception: {str(e)[:200]}"}
            kind = classify(res)
            log_.append(f"{base.split('//')[-1].split('/')[0]}:{kind}" + ("" if kind == "ok" else f" ({str(res.get('error') or res.get('err'))[:120]})"))
            if kind != "retry":
                return res, kind, log_
    return res, kind, log_


def result_sol(res: dict[str, Any]) -> tuple[float | None, float]:
    """(SOL result, rent) of a confirmed swap; quote as fallback when the tx meta is unavailable."""
    d = res.get("deltas") or {}
    if d.get("nativeDeltaLamports") is not None:
        return swap_sol_change(d)
    if res.get("ok") and res.get("quotedOut"):
        return int(res["quotedOut"]) / LAMPORTS, 0.0
    return None, 0.0


@contextmanager
def sweep_lock(wait_sec: float = LOCK_WAIT_SEC) -> Iterator[bool]:
    """Exclusive cross-process lock (flock on live/sweep.lock). Yields False if another bot holds it after wait_sec."""
    path = _shared("sweep.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        deadline = time.monotonic() + wait_sec
        got = False
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)


def load_state() -> dict[str, Any]:
    try:
        s = json.loads(_shared("sweep_state.json").read_text())
    except (OSError, ValueError):
        s = {}
    s.setdefault("tokens", {})
    s.setdefault("last_sweep", {})
    return s


def save_state(s: dict[str, Any]) -> None:
    p = _shared("sweep_state.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1))
    os.replace(tmp, p)


def write_price(token: str, price: float, now: float) -> None:
    """Each bot publishes its own market price so the others can dust-check its token (atomic replace, no lock)."""
    p = _shared(f"sweep_px_{token}.json")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"price": price, "time": now}))
        os.replace(tmp, p)
    except OSError as e:
        log.debug("price cache write failed: %s", e)


def read_price(token: str, now: float) -> float | None:
    try:
        d = json.loads(_shared(f"sweep_px_{token}.json").read_text())
    except (OSError, ValueError):
        return None
    return float(d["price"]) if now - float(d.get("time") or 0) <= PRICE_MAX_AGE_SEC and d.get("price") else None


def _paths(asset: str, me: str) -> tuple[Path, Path]:
    """(ledger_open.json, trades.csv) of a bot: our own via the configured ledger, the others' via BOT_DIRS."""
    if asset == me:
        return ledger.OPEN, ledger.TRADES
    d = ROOT / BOT_DIRS[asset]
    return d / "ledger_open.json", d / "trades.csv"


def _open_rec(asset: str, me: str) -> dict[str, Any] | None:
    try:
        return json.loads(_paths(asset, me)[0].read_text())
    except (OSError, ValueError):
        return None


def _ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _last_trade(asset: str, me: str, side: str) -> dict[str, Any] | None:
    try:
        with open(_paths(asset, me)[1], newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("side") == side]
    except OSError:
        return None
    return max(rows, key=lambda r: _ts(r.get("close_time"))) if rows else None


def attribute(name: str, me: str, st: dict[str, Any]) -> dict[str, Any]:
    """Which bot's trade produced this token balance (see the module doc)."""
    side = "long" if name in LONG_TOKEN_BOT else "short"
    if name in LONG_TOKEN_BOT:
        owner: str | None = LONG_TOKEN_BOT[name]
    else:
        shorts = {p.get("market") for p in st.get("allPositions") or [] if p.get("side") == "short"}
        owner = next((a for a in USDC_PRIORITY if a in shorts), None)
        if owner is None:   # just closed, not booked yet
            owner = next((a for a in USDC_PRIORITY if (_open_rec(a, me) or {}).get("side") == "short"), None)
        if owner is None:   # most recently closed short trade
            last = [(_ts(t.get("close_time")), a) for a in USDC_PRIORITY if (t := _last_trade(a, me, "short"))]
            owner = max(last)[1] if last else None
    claim: dict[str, Any] = {"owner": owner, "side": side, "open_sig": None, "position": None, "open_time": None}
    if owner is None:
        return claim
    rec = _open_rec(owner, me)
    src = rec if rec and rec.get("side") == side else _last_trade(owner, me, side)
    if src:
        claim.update(open_sig=src.get("open_sig") or None, position=src.get("position") or None,
                     open_time=src.get("open_time") or None)
    return claim


def trade_open_now(claim: dict[str, Any], me: str) -> bool:
    if not claim.get("owner"):
        return False
    rec = _open_rec(claim["owner"], me)
    return bool(rec) and rec.get("side") == claim["side"] and (rec.get("open_sig") or None) == claim.get("open_sig")


def decide(ent: dict[str, Any], me: str, now: float) -> tuple[str | None, str]:
    """(booking mode 'open' | 'late' | None to skip, reason)."""
    c = ent["claim"]
    owner = c.get("owner")
    if owner and trade_open_now(c, me):
        return ("open", "own open trade") if owner == me else (None, f"{owner} trade still open: left to the {owner} bot")
    if owner and owner != me and now - float(ent["first_seen"]) < FOREIGN_GRACE_SEC:
        return None, f"{owner} proceeds, grace {FOREIGN_GRACE_SEC:.0f}s for its own bot"
    return "late", "booked trade" if owner else "unattributed"


def _book(mode: str, me: str, name: str, p: dict[str, Any], claim: dict[str, Any], sol: float, rent: float,
          now: float) -> dict[str, Any]:
    owner = claim.get("owner")
    if mode == "open":
        ledger.update(sweep_sol=sol, rent_sol=rent)
        return {"mode": "open", "owner": me}
    note = (f"late sweep {name} {p['atoms']} atoms (~${p['usd']:.2f}) of {owner} {claim['side']} trade opened "
            f"{claim.get('open_time')} (swept by the {me} bot)") if owner else \
           f"unattributed sweep {name} {p['atoms']} atoms (~${p['usd']:.2f}) (swept by the {me} bot)"
    kw = dict(rent_sol=rent, open_sig=claim.get("open_sig"), position=claim.get("position"))
    if owner and owner != me:
        with ledger.at(ROOT / BOT_DIRS[owner], owner):
            row = ledger.write_sweep_row(_utc(now), sol, note, keep_header=True, **kw)
    else:
        row = ledger.write_sweep_row(_utc(now), sol, note, **kw)
    return {"mode": "late", "owner": owner or me, "row": row}


def pending_tokens(st: dict[str, Any], me: str, min_usd: float, now: float) -> dict[str, dict[str, Any]]:
    """USDC / ETH / WBTC balances above dust (ETH / WBTC priced by their own bot's oracle or the shared cache)."""
    toks = st.get("sweepTokens") or {}
    own_tok = ASSET_TOKEN.get(me)
    own_px = (st.get("oracle") or {}).get("price")
    out = {}
    for name in SWEEPABLE:
        t = toks.get(name) or {}
        atoms = int(t.get("atoms") or 0)
        if atoms <= 0 or not t.get("mint"):
            continue
        px = 1.0 if name == "USDC" else own_px if name == own_tok and own_px else read_price(name, now)
        if px is None:
            log.info("sweep: %s %d atoms, no fresh price here; left to its own bot", name, atoms)
            continue
        usd = atoms / 10 ** DECIMALS[name] * px
        if usd >= min_usd:
            out[name] = {"name": name, "mint": t["mint"], "atoms": atoms, "usd": round(usd, 4)}
    return out


def self_heal(cfg: Any, me: str, st: dict[str, Any], jup: Jup, *, dry_run: bool, min_usd: float,
              clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
              lock_wait: float = LOCK_WAIT_SEC) -> list[dict[str, Any]]:
    """One tick of the self-healing sweep (module doc). Returns the bookings made."""
    toks = st.get("sweepTokens") or {}
    if "error" in toks:
        return []
    now = clock()
    read_at = float(st.get("_read_at") or now)
    own_tok, own_px = ASSET_TOKEN.get(me), (st.get("oracle") or {}).get("price")
    if own_tok and own_px and not dry_run:
        write_price(own_tok, float(own_px), now)
    seen = pending_tokens(st, me, min_usd, now)
    if dry_run:   # simulate only; no shared state, no booking
        for p in seen.values():
            res = jup("simulate-swap", {**sweep_args(cfg, p["mint"], p["atoms"]), "send": False})
            log.info("SWEEP (dry run) %s %d atoms (~$%.2f): %s", p["name"], p["atoms"], p["usd"],
                     {k: res.get(k) for k in ("ok", "err", "error", "quotedOut", "route")})
        return []
    if not seen and not load_state()["tokens"]:
        return []
    booked: list[dict[str, Any]] = []
    with sweep_lock(lock_wait) as got:
        if not got:
            log.info("sweep lock busy (another bot is sweeping); next tick")
            return []
        state = load_state()
        for n in [n for n in state["tokens"] if n not in seen]:   # gone / dust now (only trust a view newer than the entry)
            if float(state["tokens"][n].get("seen_at") or 0) <= read_at:
                del state["tokens"][n]
        for name, p in seen.items():
            last = state["last_sweep"].get(name)
            if last and float(last.get("time") or 0) > read_at:
                log.info("sweep: %s swept by the %s bot after this balance was read; skipping", name, last.get("by"))
                continue
            ent = state["tokens"].get(name)
            if ent is None or p["atoms"] > int(ent.get("atoms") or 0):   # new proceeds: (re)attribute, reset backoff
                ent = {"first_seen": ent["first_seen"] if ent else now, "claim": attribute(name, me, st),
                       "fails": 0, "next_try": 0.0}
            ent.update(atoms=p["atoms"], usd=p["usd"], seen_at=now)
            state["tokens"][name] = ent
            mode, why = decide(ent, me, now)
            if mode is None:
                log.info("sweep: %s %d atoms (~$%.2f) not mine now: %s", name, p["atoms"], p["usd"], why)
                continue
            if now < float(ent.get("next_try") or 0):
                log.info("sweep: %s in backoff until %s (fail #%d: %s)", name, _utc(ent["next_try"]), ent["fails"],
                         ent.get("last_error"))
                continue
            log.warning("non-SOL proceeds in wallet: %s (%s, claim %s) -> swapping to SOL", p, why, ent["claim"])
            res, kind, attempts = swap_with_backoff(cfg, p, jup, sleep)
            sol, rent = result_sol(res) if kind == "ok" else (None, 0.0)
            log.info("SWEEP %s %d atoms (~$%.2f) -> SOL: %s attempts=%s sol_change=%s rent_sol=%s", name, p["atoms"],
                     p["usd"], {k: res.get(k) for k in ("ok", "sig", "err", "error", "quotedOut", "route", "api")},
                     attempts, sol, rent)
            t_end = clock()
            if kind == "ok":
                state["last_sweep"][name] = {"time": t_end, "utc": _utc(t_end), "by": me, "sig": res.get("sig"),
                                             "atoms": p["atoms"], "sol": sol, "claim": ent["claim"]}
                del state["tokens"][name]
                save_state(state)   # before booking: a booking error must never cause a second swap
                if sol is None:
                    log.error("sweep %s confirmed (%s) but its SOL result is unknown: NOT booked", name, res.get("sig"))
                    continue
                b = _book(mode, me, name, p, ent["claim"], sol, rent, t_end)
                log.info("LEDGER sweep booked (%s, %s trade): %+.9f SOL (rent %+.9f SOL capital)", b["mode"], b["owner"], sol, rent)
                booked.append({"name": name, "sol": sol, "rent_sol": rent, **b})
            else:
                ent["fails"] = int(ent.get("fails") or 0) + 1
                delay = min(TICK_BACKOFF_MAX_SEC, TICK_BACKOFF_SEC * 2 ** (ent["fails"] - 1))
                ent.update(next_try=t_end + delay, last_error=f"{kind}: {str(res.get('error') or res.get('err'))[:160]}",
                           last_sig=res.get("sig"))
                (log.error if kind == "unknown" else log.warning)(
                    "sweep %s failed (%s); retry after %.0fs (fail #%d)%s", name, kind, delay, ent["fails"],
                    "; tx may have landed: the next balance read decides, check sig " + str(res.get("sig")) if kind == "unknown" else "")
        save_state(state)
    return booked


# ------------------------------------------------------------------ own-coin sweep (per-bot ledger, 2026-10-08 21:37 MT)
OWN_COIN = {"SOL": ("SOL", "So11111111111111111111111111111111111111112", 9),
            "ETH": ("ETH", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", 8),
            "BTC": ("WBTC", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", 8)}


def native_out(res: dict[str, Any], me: str) -> float | None:
    """Own-coin amount a confirmed swap delivered (tx deltas; quote as fallback)."""
    coin, _, dec = OWN_COIN[me]
    d = res.get("deltas") or {}
    if coin == "SOL":
        return result_sol(res)[0]
    td = (d.get("tokenDeltaAtoms") or {}).get(coin)
    if td is not None:
        return int(td) / 10 ** dec
    if res.get("ok") and res.get("quotedOut"):
        return int(res["quotedOut"]) / 10 ** dec
    return None


def self_heal_own(cfg: Any, me: str, st: dict[str, Any], jup: Jup, *, dry_run: bool, min_usd: float,
                  clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                  lock_wait: float = LOCK_WAIT_SEC) -> list[dict[str, Any]]:
    """Own-coin sweep: ETH / WBTC are the ETH / BTC bots' balances and are NEVER swept. Only USDC (short payouts) is
    swapped, by the bot whose short produced it, into THAT bot's own coin (SOL / ETH / WBTC); unattributed USDC is
    swapped to SOL by the SOL bot and reported as unattributed (the caller books it as an external deposit). Same
    lock / backoff / shared state as self_heal. Returns [{name, native, coin, unattributed, sig, ...}]."""
    toks = st.get("sweepTokens") or {}
    if "error" in toks:
        return []
    now = clock()
    read_at = float(st.get("_read_at") or now)
    seen = {k: v for k, v in pending_tokens(st, me, min_usd, now).items() if k == "USDC"}
    if not seen:
        return []
    out_mint = OWN_COIN[me][1]
    if dry_run:
        for p in seen.values():
            res = jup("simulate-swap", {**sweep_args(cfg, p["mint"], p["atoms"], out_mint), "send": False})
            log.info("SWEEP (dry run) USDC %d atoms -> %s: %s", p["atoms"], OWN_COIN[me][0],
                     {k: res.get(k) for k in ("ok", "err", "error", "quotedOut", "route")})
        return []
    booked: list[dict[str, Any]] = []
    with sweep_lock(lock_wait) as got:
        if not got:
            log.info("sweep lock busy (another bot is sweeping); next tick")
            return []
        state = load_state()
        for name, p in seen.items():
            last = state["last_sweep"].get(name)
            if last and float(last.get("time") or 0) > read_at:
                log.info("sweep: %s swept by the %s bot after this balance was read; skipping", name, last.get("by"))
                continue
            ent = state["tokens"].get(name)
            if ent is None or p["atoms"] > int(ent.get("atoms") or 0):
                ent = {"first_seen": ent["first_seen"] if ent else now, "claim": attribute(name, me, st),
                       "fails": 0, "next_try": 0.0}
            ent.update(atoms=p["atoms"], usd=p["usd"], seen_at=now)
            state["tokens"][name] = ent
            owner = ent["claim"].get("owner")
            unattributed = owner is None
            if (owner or "SOL") != me:
                log.info("sweep: USDC %d atoms (~$%.2f) belongs to the %s bot (its own coin); left to it", p["atoms"], p["usd"],
                         owner or "SOL")
                continue
            if now < float(ent.get("next_try") or 0):
                log.info("sweep: %s in backoff until %s (fail #%d: %s)", name, _utc(ent["next_try"]), ent["fails"],
                         ent.get("last_error"))
                continue
            log.warning("USDC proceeds in wallet: %s (claim %s) -> swapping to own coin %s", p, ent["claim"], OWN_COIN[me][0])
            res, kind, attempts = swap_with_backoff(cfg, p, jup, sleep, out_mint=out_mint)
            t_end = clock()
            if kind == "ok":
                nat = native_out(res, me)
                sol, rent = result_sol(res) if me == "SOL" else (
                    ((res.get("deltas") or {}).get("nativeDeltaLamports") or 0) / LAMPORTS, 0.0)
                state["last_sweep"][name] = {"time": t_end, "utc": _utc(t_end), "by": me, "sig": res.get("sig"),
                                             "atoms": p["atoms"], "native": nat, "coin": OWN_COIN[me][0], "claim": ent["claim"]}
                del state["tokens"][name]
                save_state(state)
                log.info("SWEEP USDC %d atoms (~$%.2f) -> %s %s: sig %s attempts=%s (SOL change %s)", p["atoms"], p["usd"],
                         nat, OWN_COIN[me][0], res.get("sig"), attempts, sol)
                mode = "open" if trade_open_now(ent["claim"], me) else "late"
                try:   # informational trade ledger (SOL-based): the swap's SOL change only
                    _book(mode, me, name, p, ent["claim"], sol or 0.0, rent, t_end)
                except Exception as e:  # noqa: BLE001
                    log.warning("sweep ledger row failed: %s", e)
                booked.append({"name": name, "native": nat, "coin": OWN_COIN[me][0], "unattributed": unattributed,
                               "sig": res.get("sig"), "sol_change": sol, "usd": p["usd"]})
            else:
                ent["fails"] = int(ent.get("fails") or 0) + 1
                delay = min(TICK_BACKOFF_MAX_SEC, TICK_BACKOFF_SEC * 2 ** (ent["fails"] - 1))
                ent.update(next_try=t_end + delay, last_error=f"{kind}: {str(res.get('error') or res.get('err'))[:160]}",
                           last_sig=res.get("sig"))
                (log.error if kind == "unknown" else log.warning)("sweep %s failed (%s); retry after %.0fs (fail #%d)",
                                                                  name, kind, delay, ent["fails"])
        save_state(state)
    return booked
