"""USDC deposit fold-in (Trevor 2026-10-26).

USDC >= MIN_USD ($2) in the shared wallet is a deposit from Trevor. Exactly ONE process swaps it: the SOL bot, in its
sweep step (live/bot.py sweep; the ``sweep:`` log line is written after this run). It swaps the USDC on Jupiter into
equal thirds of SOL / ETH / WBTC (atoms split in thirds, the remainder to the SOL leg) with the existing [withdraw]
swap slippage / price-impact caps (live/sweep.py swap_with_backoff). Each bot then credits exactly what landed for its
own coin (the measured on-chain tx delta, never the quote) to its own bank as capital: bal, hwm and wd_mark all rise by
it (bank.apply_external). It is never trade profit and never an ext-flow alert: a bot holds its wallet reconcile (and
its payout window) while its own leg is sent-but-unresolved or landed-but-uncredited (Bot.deposit_tick).

Journal live/deposit_journal.json (lock live/deposit.lock; atomic replace). One record per deposit:
  {id, detected, usdc_atoms, status swapping|swapped|done, legs{SOL|ETH|BTC}, credits{SOL|ETH|BTC: rec|None}, ledgered}
Leg status: pending (not sent, or sent and proven not landed) -> sending (saved BEFORE the swap is sent; outcome
unknown until measured) -> landed (sig + measured result). The swapper is the only writer of legs / status /
last_activity / seen; each bot writes only its own credits[asset] (and the bot that credits last, the ledger line).
  * an unresolved ``sending`` leg is settled first, before anything else is sent: from its tx (sig known; not found
    after RESOLVE_AFTER_S = blockhash long expired = not landed) or, sig unknown (crash / CLI timeout mid-send), from the
    USDC account's recent txs after RESOLVE_AFTER_S (a landed swap of exactly that leg's USDC into that leg's coin).
  * a pending leg is (re)sent only on a state read taken after the journal's last swap activity that shows enough USDC
    for every unswapped leg; otherwise it waits (logged). So a crash / restart anywhere never swaps twice, a partial
    failure resumes with only the missing legs, and the USDC is never lost track of.
  * a new deposit record needs the same USDC balance on two fresh state reads (no record from a stale read).
  * credit idempotency: the leg sig, kept in the bot's own bank["deposits"] (saved with state.json BEFORE the journal
    is marked; a crash in between only re-marks the journal next tick).
Network fees of all legs are paid in native SOL and fall in the SOL bot's reserve float (as its other fees do); no
fee debt is touched. live/deposits.jsonl: one line per fully credited deposit (USDC in, each coin out, sigs, fees,
before/after bank values), read by the hourly check.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from live import bank as bankmod
from live import sweep as sweepmod

log = logging.getLogger("deposit")
ROOT = Path(__file__).resolve().parent.parent            # tests redirect this (conftest)
JOURNAL = "live/deposit_journal.json"
LOCK = "live/deposit.lock"                               # journal read-modify-write (held for milliseconds)
RUN_LOCK = "live/deposit_run.lock"                       # one swapper run at a time (non-blocking)
LEDGER = "live/deposits.jsonl"
MIN_USD = 2.0                                            # Trevor: >= $2 of USDC = a deposit; less = dust, ignored
USDC_ATOMS = 1_000_000
USDC_MINT = bankmod.MINT["USDC"]
ASSETS = ("SOL", "ETH", "BTC")                           # leg order; bot asset -> coin = bank.COIN
OUT_MINT = {"SOL": bankmod.MINT["SOL"], "ETH": bankmod.MINT["ETH"], "BTC": bankmod.MINT["WBTC"]}
RESOLVE_AFTER_S = 300                                    # a tx not landed by then never will (blockhash ~60-90 s)
# A swap rejected by simulation never left the box (live/src/send.ts sendBuilt: own simulation before signing, "simulation
# failed ..."; or the RPC preflight of the first send, web3 "Simulation failed. ... Transaction simulation failed", which
# the RPC never forwards). No sig, not sent -> a clean failure: pending again, retried after RETRY_S (not the 300 s
# unknown-outcome path). A Jupiter slippage rejection (6001 = 0x1771 SlippageToleranceExceeded) retries that leg with
# twice the sweep slippage, capped at [withdraw] stake_max_slippage_bps (2026-10-26: deposit #1 WBTC leg, $1.67 ->
# ~2095 WBTC atoms; the Manifest USDC/WBTC route fills ~0.5-0.9% below its quote at that size, 50 bps always fails).
SIM_REJECT_RE = re.compile(r"^(?:Error: )?simulation failed \{|^(?:\w*Error: )?Simulation failed\.\s*\n?\s*Message: Transaction simulation failed")
SLIPPAGE_RE = re.compile(r"""['"]Custom['"]\s*:\s*6001\b|0x1771\b|SlippageToleranceExceeded""")
RETRY_S = 60                                             # a leg not sent (API error / quote rejected): retry after
SCAN_LIMIT = 25                                          # recent USDC-account txs scanned for a sig-less leg
KEEP = 50                                                # done records kept in the journal
LAMPORTS = 1_000_000_000
Jup = Callable[[str, dict[str, Any]], dict[str, Any]]


def split(atoms: int) -> dict[str, int]:
    """USDC atoms -> per-leg atoms: equal thirds, the remainder (0-2 atoms) to the SOL leg."""
    third = int(atoms) // 3
    return {"SOL": int(atoms) - 2 * third, "ETH": third, "BTC": third}


def _p(name: str) -> Path:
    return ROOT / name


@contextlib.contextmanager
def journal_lock() -> Iterator[None]:
    f = _p(LOCK)
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(h, fcntl.LOCK_UN)


@contextlib.contextmanager
def run_lock() -> Iterator[bool]:
    f = _p(RUN_LOCK)
    f.parent.mkdir(parents=True, exist_ok=True)
    h = open(f, "a")
    got = False
    try:
        try:
            fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            fcntl.flock(h, fcntl.LOCK_UN)
        h.close()


def load() -> dict[str, Any]:
    """The journal (empty if none). An unreadable journal raises: callers then do nothing (never guess)."""
    f = _p(JOURNAL)
    j = json.loads(f.read_text()) if f.exists() else {}
    j.setdefault("records", [])
    j.setdefault("last_activity", 0.0)
    j.setdefault("next_id", max([r["id"] for r in j["records"]], default=0) + 1)
    j.setdefault("seen", None)
    return j


def save(j: dict[str, Any]) -> None:
    f = _p(JOURNAL)
    tmp = f.with_name(f.name + ".tmp")
    with open(tmp, "w") as h:
        h.write(json.dumps(j, indent=1))
        h.flush()
        os.fsync(h.fileno())
    os.replace(tmp, f)


def update(fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """Load, modify, save under the journal lock; returns the fresh journal."""
    with journal_lock():
        j = load()
        fn(j)
        save(j)
        return j


def _rec(j: dict[str, Any], rid: int) -> dict[str, Any]:
    return next(r for r in j["records"] if r["id"] == rid)


def active(j: dict[str, Any]) -> dict[str, Any] | None:
    return next((r for r in j["records"] if r["status"] == "swapping"), None)


def usdc_atoms(st: dict[str, Any]) -> int | None:
    tok = st.get("sweepTokens") or {}
    if "error" in tok:
        return None
    return int((tok.get("USDC") or {}).get("atoms") or 0)


def leg_got(asset: str, d: dict[str, Any] | None) -> tuple[int, int] | None:
    """(atoms of the leg's coin received, USDC atoms change) measured from one successful swap tx's deltas; None if the
    tx failed or carries no measurement. SOL: gross swap output = native + wSOL + token-account rent change + the tx fee
    (the fee is paid from native SOL like every other fee: reserve float)."""
    if not d or d.get("err") is not None:
        return None
    td = d.get("tokenDeltaAtoms") or {}
    usdc = int(td.get("USDC") or 0)
    if asset == "SOL":
        if d.get("nativeDeltaLamports") is None or int(td.get("ETH") or 0) or int(td.get("WBTC") or 0):
            return None
        return round(sweepmod.swap_sol_change(d)[0] * LAMPORTS) + int(d.get("feeLamports") or 0), usdc
    return int(td.get(bankmod.COIN[asset]) or 0), usdc


def _call(jup: Jup, cmd: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        r = jup(cmd, args)
        return r if isinstance(r, dict) else {"ok": False, "error": f"bad reply {r!r}"[:200]}
    except Exception as e:  # noqa: BLE001 - read-only call: retried next tick
        return {"ok": False, "error": f"cli exception: {str(e)[:200]}"}


# ------------------------------------------------------------------ the swapper (SOL bot only)
def run_swapper(st: dict[str, Any], cfg: Any, jup: Jup, killed: bool = False,
                now: Callable[[], float] = time.time) -> str:
    """One swapper step on this tick's state read ``st``. Returns a note for the sweep log line ("" = nothing to do)."""
    with run_lock() as got:
        if not got:
            return "deposit: another swapper run is active; skipped"
        return _run(st, cfg, jup, killed, now)


def _leg_set(rid: int, a: str, touch: float | None = None, **kw: Any) -> dict[str, Any]:
    def fn(j: dict[str, Any]) -> None:
        _rec(j, rid)["legs"][a].update(kw)
        if touch is not None:
            j["last_activity"] = max(float(j["last_activity"]), touch)
    return update(fn)


def _landed(rid: int, a: str, sig: str, d: dict[str, Any], m: tuple[int, int], t: float, atoms: int) -> dict[str, Any]:
    coin = bankmod.COIN[a]
    got_atoms, usdc_d = m
    if -usdc_d != atoms:
        log.warning("DEPOSIT #%d %s leg %s spent %d USDC atoms, journal leg is %d", rid, a, sig, -usdc_d, atoms)
    got = round(got_atoms / 10 ** bankmod.DECIMALS[coin], 9)
    log.info("deposit #%d %s leg landed: %d USDC atoms -> %.9f %s (tx %s, fee %s lamports)", rid, a, atoms, got, coin,
             sig, d.get("feeLamports"))
    return _leg_set(rid, a, touch=t, status="landed", sig=sig, got_atoms=got_atoms, got=got, usdc_spent=-usdc_d,
                    fee_lamports=int(d.get("feeLamports") or 0),
                    net_sol=sweepmod.swap_sol_change(d)[0] if d.get("nativeDeltaLamports") is not None else None,
                    landed_at=t)


def _resolve(j: dict[str, Any], rec: dict[str, Any], a: str, jup: Jup, t: float, cfg: Any = None) -> dict[str, Any]:
    """Settle one leg whose send outcome is unknown. Never sends anything."""
    rid, leg = rec["id"], rec["legs"][a]
    age = t - float(leg.get("started") or 0.0)
    if leg.get("sig"):
        r = _call(jup, "tx-deltas", {"sig": leg["sig"]})
        if r.get("ok") and r.get("found"):
            d = r.get("deltas") or {}
            if d.get("err") is not None:
                kw: dict[str, Any] = {}
                if cfg is not None and SLIPPAGE_RE.search(str(d.get("err"))):   # on-chain slippage fail: same rule
                    kw["slip_bps"] = retry_bps(cfg, leg.get("slip_bps"))
                log.warning("DEPOSIT #%d %s leg tx %s failed on-chain (%s): not landed, USDC not spent; retry in %ds%s",
                            rid, a, leg["sig"], d.get("err"), RETRY_S, f" at {kw['slip_bps']} bps slippage" if kw else "")
                return _leg_set(rid, a, touch=t, status="pending", next_try=t + RETRY_S,
                                errors=(leg.get("errors") or [])[-4:] + [f"{leg['sig']}: on-chain err {d.get('err')}"], **kw)
            m = leg_got(a, d)
            if m and m[0] > 0:
                return _landed(rid, a, leg["sig"], d, m, t, int(leg["atoms"]))
            log.error("DEPOSIT #%d %s leg tx %s landed but its result is not measurable (%s); held (check by hand)",
                      rid, a, leg["sig"], json.dumps(d)[:200])
            return j
        if r.get("ok") and age >= RESOLVE_AFTER_S:
            log.warning("DEPOSIT #%d %s leg tx %s not found %ds after sending (blockhash expired): not landed; re-sent",
                        rid, a, leg["sig"], int(age))
            return _leg_set(rid, a, touch=t, status="pending", next_try=0.0,
                            errors=(leg.get("errors") or [])[-4:] + [f"{leg['sig']}: never landed"])
        log.info("deposit #%d %s leg: waiting for tx %s (%ds since send)", rid, a, leg["sig"], int(age))
        return j
    if age < RESOLVE_AFTER_S:
        log.info("deposit #%d %s leg: send outcome unknown (no sig); resolving %ds after the send (%ds so far)", rid, a,
                 RESOLVE_AFTER_S, int(age))
        return j
    r = _call(jup, "usdc-sigs", {"limit": SCAN_LIMIT})
    if not r.get("ok"):
        log.info("deposit #%d %s leg: USDC account scan failed (%s); retry next tick", rid, a, str(r.get("error"))[:120])
        return j
    known = {lg.get("sig") for x in j["records"] for lg in x["legs"].values() if lg.get("sig")}
    started = float(leg.get("started") or 0.0)
    for s in sorted(r.get("sigs") or [], key=lambda x: x.get("blockTime") or 0):
        sig = s.get("signature")
        if not sig or sig in known or s.get("err") is not None or (s.get("blockTime") or 0) < started - 30:
            continue
        dd = _call(jup, "tx-deltas", {"sig": sig})
        if not dd.get("ok"):
            log.info("deposit #%d %s leg: tx %s unreadable (%s); retry next tick", rid, a, sig, str(dd.get("error"))[:120])
            return j
        m = leg_got(a, dd.get("deltas")) if dd.get("found") else None
        if m and m[0] > 0 and -m[1] == int(leg["atoms"]):
            log.warning("DEPOSIT #%d %s leg: found its landed swap %s on the USDC account", rid, a, sig)
            return _landed(rid, a, sig, dd["deltas"], m, t, int(leg["atoms"]))
    log.warning("DEPOSIT #%d %s leg: no landed swap of %d USDC atoms on the USDC account since the send (%ds ago): "
                "not landed; re-sent", rid, a, int(leg["atoms"]), int(age))
    return _leg_set(rid, a, touch=t, status="pending", next_try=0.0,
                    errors=(leg.get("errors") or [])[-4:] + ["send outcome unknown, no matching tx: not landed"])


def _finalize(j: dict[str, Any]) -> dict[str, Any]:
    rec = active(j)
    if not rec or any(lg["status"] != "landed" for lg in rec["legs"].values()):
        return j
    L = rec["legs"]
    log.warning("DEPOSIT USDC: $%.6f -> SOL %.9f / ETH %.9f / WBTC %.9f, sigs %s / %s / %s (#%d; each bot credits its "
                "own share as capital)", rec["usdc_atoms"] / USDC_ATOMS, L["SOL"]["got"], L["ETH"]["got"],
                L["BTC"]["got"], L["SOL"]["sig"], L["ETH"]["sig"], L["BTC"]["sig"], rec["id"])
    return update(lambda jj: _rec(jj, rec["id"]).update(status="swapped"))


def _run(st: dict[str, Any], cfg: Any, jup: Jup, killed: bool, now: Callable[[], float]) -> str:
    read_at = float(st.get("_read_at") or 0.0)
    with journal_lock():
        j = load()
    rec = active(j)
    if rec:
        for a in ASSETS:
            if rec["legs"][a]["status"] == "sending":
                j = _resolve(j, rec, a, jup, now(), cfg)
        j = _finalize(j)
        rec = active(j)
        if rec and any(lg["status"] == "sending" for lg in rec["legs"].values()):
            return f"deposit #{rec['id']}: waiting for a sent swap's outcome (nothing else sent)"
    u = usdc_atoms(st)
    if rec is None:
        if u is None:
            return ""
        if u < MIN_USD * USDC_ATOMS:
            if j.get("seen"):
                update(lambda jj: jj.update(seen=None))
            return ""
        if read_at <= float(j["last_activity"]):
            return f"USDC ${u / USDC_ATOMS:.2f} in the wallet; state read predates the last deposit swap, re-checked next tick"
        seen = j.get("seen")
        if not (seen and int(seen["atoms"]) == u and float(seen["read_at"]) < read_at):
            update(lambda jj: jj.update(seen={"atoms": u, "read_at": read_at}))
            return f"USDC ${u / USDC_ATOMS:.2f} in the wallet: deposit; swapped next tick if the balance is unchanged"
        if killed:
            return f"USDC ${u / USDC_ATOMS:.2f} in the wallet: deposit; kill switch present, not swapped"
        parts = split(u)
        t = now()

        def add(jj: dict[str, Any]) -> None:
            rid = int(jj["next_id"])
            jj["next_id"] = rid + 1
            done = [r for r in jj["records"] if r["status"] == "done"]
            keep = {r["id"] for r in done[-KEEP:]}
            jj["records"] = [r for r in jj["records"] if r["status"] != "done" or r["id"] in keep]
            jj["records"].append({
                "id": rid, "detected": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t)), "detected_ts": t,
                "usdc_atoms": u, "status": "swapping", "ledgered": False,
                "legs": {a: {"coin": bankmod.COIN[a], "atoms": parts[a], "status": "pending", "sig": None, "tries": 0,
                             "next_try": 0.0, "errors": []} for a in ASSETS},
                "credits": {a: None for a in ASSETS}})
            jj["seen"] = None
        j = update(add)
        rec = active(j)
        log.warning("DEPOSIT detected #%d: USDC $%.6f in the wallet -> swapping into thirds (USDC atoms SOL %d / ETH %d / "
                    "WBTC %d)", rec["id"], u / USDC_ATOMS, parts["SOL"], parts["ETH"], parts["BTC"])
    rid = rec["id"]
    if killed:
        return f"deposit #{rid}: kill switch present, swaps held"
    if u is None:
        return f"deposit #{rid}: USDC balance unreadable this tick; swaps held"
    if read_at <= float(j["last_activity"]):
        return f"deposit #{rid}: state read predates the last swap; next legs next tick"
    need = sum(int(lg["atoms"]) for lg in rec["legs"].values() if lg["status"] != "landed")
    if u < need:
        msg = (f"deposit #{rid}: wallet USDC {u} atoms < {need} atoms still to swap; nothing sent (waiting; check by hand "
               f"if this persists)")
        if not rec.get("short_warned"):
            log.warning("DEPOSIT %s", msg)
            update(lambda jj: _rec(jj, rid).update(short_warned=True))
        return msg
    sent = []
    for a in ASSETS:
        leg = rec["legs"][a]
        if leg["status"] != "pending" or float(leg.get("next_try") or 0.0) > now():
            continue
        t0 = now()
        _leg_set(rid, a, touch=t0, status="sending", started=t0, sig=None, tries=int(leg.get("tries") or 0) + 1)
        res, kind, attempts = sweepmod.swap_with_backoff(cfg, {"mint": USDC_MINT, "atoms": int(leg["atoms"])}, jup,
                                                         out_mint=OUT_MINT[a], slippage_bps=leg.get("slip_bps"))
        t1 = now()
        sent.append(f"{a}:{kind}")
        if kind == "ok":
            m = leg_got(a, res.get("deltas"))
            if m and m[0] > 0:
                j = _landed(rid, a, res["sig"], res["deltas"], m, t1, int(leg["atoms"]))
                continue
            _leg_set(rid, a, touch=t1, sig=res.get("sig"))
            log.warning("DEPOSIT #%d %s leg sent (tx %s) but its result is not measured yet; resolved next tick", rid, a,
                        res.get("sig"))
            break
        if kind == "unknown" and sim_rejected(res):
            err = str(res.get("error") or res.get("err"))
            kw: dict[str, Any] = {}
            if SLIPPAGE_RE.search(err):
                kw["slip_bps"] = retry_bps(cfg, leg.get("slip_bps"))
            _leg_set(rid, a, touch=t1, status="pending", next_try=t1 + RETRY_S,
                     errors=(leg.get("errors") or [])[-4:] + [f"simulation rejected: {err[:160]}"], **kw)
            log.warning("DEPOSIT #%d %s leg rejected at simulation (%s; %s): nothing sent, retry in %ds%s", rid, a,
                        err[:160], attempts, RETRY_S, f" at {kw['slip_bps']} bps slippage" if kw else "")
            continue
        if kind == "unknown":
            _leg_set(rid, a, touch=t1, sig=res.get("sig"))
            log.warning("DEPOSIT #%d %s leg send outcome unknown (%s; %s): never re-sent before it is resolved", rid, a,
                        str(res.get("error") or res.get("err"))[:160], attempts)
            break
        _leg_set(rid, a, touch=t1, status="pending", next_try=t1 + RETRY_S,
                 errors=(leg.get("errors") or [])[-4:] + [f"{kind}: {str(res.get('error') or res.get('err'))[:160]}"])
        log.warning("DEPOSIT #%d %s leg not swapped (%s: %s; %s); nothing sent, retry in %ds", rid, a, kind,
                    str(res.get("error") or res.get("err"))[:160], attempts, RETRY_S)
    with journal_lock():
        j = load()
    j = _finalize(j)
    r = next(x for x in j["records"] if x["id"] == rid)
    return f"deposit #{rid} {r['status']}: legs " + " ".join(f"{a}={r['legs'][a]['status']}" for a in ASSETS) + \
        (f" (this run: {', '.join(sent)})" if sent else "")


def sim_rejected(res: dict[str, Any]) -> bool:
    """True only when the swap provably never left: no sig, not sent, and the CLI error is a simulation rejection."""
    if res.get("sig") or res.get("sent") or res.get("ok"):
        return False
    return bool(SIM_REJECT_RE.search(str(res.get("error") or res.get("err") or "")))


def retry_bps(cfg: Any, cur: int | None) -> int:
    """Slippage for the retry after a slippage rejection: 2 x the sweep setting, capped at stake_max_slippage_bps."""
    w = cfg.withdraw
    base = int(w.unstake_slippage_bps)
    return max(int(cur or base), min(int(w.stake_max_slippage_bps), 2 * base))


# ------------------------------------------------------------------ crediting (every bot, its own share only)
def own_work(asset: str, b: dict[str, Any]) -> dict[str, Any]:
    """This bot's share (read-only). credit = own leg landed, not in this bank yet; mark = already in this bank
    (bank["deposits"], keyed by sig) but not marked in the journal (crash between the two writes); inflight = own leg
    sent with its outcome unknown; ledger = a fully credited record not yet written to deposits.jsonl."""
    mine = {d.get("sig"): d for d in b.get("deposits") or []}
    with journal_lock():
        j = load()
    out: dict[str, Any] = {"credit": [], "mark": [], "inflight": False, "ledger": False}
    for r in j["records"]:
        if r["status"] == "done":
            continue
        leg = r["legs"][asset]
        if leg["status"] == "sending":
            out["inflight"] = True
        elif leg["status"] == "landed" and r["credits"].get(asset) is None:
            if leg["sig"] in mine:
                out["mark"].append((r["id"], mine[leg["sig"]]))
            else:
                out["credit"].append((r["id"], dict(leg)))
        if all(r["credits"].get(a) is not None for a in ASSETS) and not r.get("ledgered"):
            out["ledger"] = True
    return out


def mark_credited(rid: int, asset: str, crec: dict[str, Any]) -> None:
    def fn(j: dict[str, Any]) -> None:
        r = _rec(j, rid)
        if r["credits"].get(asset) is None:
            r["credits"][asset] = crec
    update(fn)


def ledger_done() -> list[int]:
    """Append every fully credited, not yet ledgered record to live/deposits.jsonl (once per id), mark it done."""
    out: list[int] = []
    with journal_lock():
        j = load()
        recs = [r for r in j["records"] if r["status"] == "swapped" and not r.get("ledgered")
                and all(r["credits"].get(a) is not None for a in ASSETS)]
        if not recs:
            return out
        f = _p(LEDGER)
        have = set()
        if f.exists():
            for ln in f.read_text().splitlines():
                try:
                    have.add(json.loads(ln).get("id"))
                except ValueError:
                    pass
        for r in recs:
            if r["id"] not in have:
                L, C = r["legs"], r["credits"]
                row = {"id": r["id"], "detected": r["detected"], "done": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                       "usdc_in": r["usdc_atoms"] / USDC_ATOMS,
                       "out": {L[a]["coin"]: L[a]["got"] for a in ASSETS},
                       "legs": {a: {"coin": L[a]["coin"], "usdc": L[a]["atoms"] / USDC_ATOMS, "got": L[a]["got"],
                                    "sig": L[a]["sig"], "fee_lamports": L[a].get("fee_lamports")} for a in ASSETS},
                       "banks": {a: {"before": C[a]["before"], "after": C[a]["after"], "credited_at": C[a]["at"]}
                                 for a in ASSETS}}
                with open(f, "a") as h:
                    h.write(json.dumps(row) + "\n")
                    h.flush()
                    os.fsync(h.fileno())
            r.update(ledgered=True, status="done")
            out.append(r["id"])
        save(j)
    for rid in out:
        log.info("deposit #%d fully credited by all three bots; recorded in %s", rid, LEDGER)
    return out
