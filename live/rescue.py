"""JupSOL liquidation rescue, manual tool (Trevor 2026-10-11; reworked 2026-10-27). Dry run unless --send (and then only
via rescue.sh, service stopped). The automatic version runs inside the bots: live/jupsol_loans.py ([ledger] jupsol_loans).

When a bot's position is within trigger_pct of liquidation (accrued borrow fee included): ONE ExactOut JupSOL -> own-coin
swap (no SOL leg left stranded) for min(collateral that moves liq to target_pct away, 1/3 of the usable JupSOL after the
haircut), then ALL of the measured coin goes in as extra collateral (increase with sizeUsd 0; shorts: keeper swaps to USDC
as the bot already does). No bank credit split. The loan is a bank LIABILITY (bank.loan): excluded from HWM / profit guard /
withdrawals. When the bot is flat it buys back exactly the JupSOL quantity lent (no margin, no profit share; live/bot.py
bank_tick -> jupsol_loans.repay_tick) and restores any extended settings.

  rescue.sh status
  rescue.sh lend <sol|eth|btc> <auto|usd> [--send]   (auto = the rule above; usd = that collateral, still max 1/3 of JupSOL)
  rescue.sh extend <bot> [n_rungs] [--send]     (raises dca.ladder_max_rungs; original restored on repay / restore)
  rescue.sh add <bot> <usd> [--lev X] [--send]  (manual add on the bot's behalf, normal increase builder)
  rescue.sh restore <bot> [--send]              (repay now if flat + restore settings)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from live import bank as K

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "live" / "rescue_cfg.json"
# 2026-10-27: collateral_share / repay_margin_pct / repay_profit_share / emergency_min_collat_pct removed (collateral only,
# exact JupSOL repayment, EMERGENCY = push < emergency_min_push_pct); max 1/3 of the JupSOL per rescue (jupsol_loans.RESCUE_SHARE)
DEFAULTS = {"trigger_pct": 3.0, "target_pct": 10.0, "keep_jupsol": 0.0, "haircut_pct": 1.0, "emergency_min_push_pct": 2.0,
            "extend_rungs": 6, "maint": 0.01}
BOTS = {"sol": ("SOL", "configs/live_100.toml", "live/state.json", "ratchet-sol"),
        "eth": ("ETH", "configs/live_eth.toml", "live/eth/state.json", "ratchet-eth"),
        "btc": ("BTC", "configs/live_btc.toml", "live/btc/state.json", "ratchet-btc")}
LAMPORTS = 1_000_000_000
USDC_ATOMS = 1_000_000


def rcfg() -> dict[str, Any]:
    c = dict(DEFAULTS)
    if CFG_PATH.exists():
        c.update(json.loads(CFG_PATH.read_text()))
    return c


# ------------------------------------------------------------------ pure maths (tested)
def liq_price(side: int, size: float, avg: float, margin: float, maint: float = 0.01) -> float:
    """Same formula as quickcheck_core.sh: liq = avg x (1 + side x (maint - margin/size))."""
    return avg * (1 + side * (maint - margin / size))


def dist_pct(side: int, px: float, liq: float) -> float:
    return (px - liq) / px * 100 * side


def collateral_for(side: int, size: float, avg: float, margin: float, px: float, target_pct: float, maint: float = 0.01) -> float:
    """USD collateral that moves liq to target_pct away from px (0 if already there)."""
    liq_t = px * (1 - side * target_pct / 100)
    return max(0.0, size * (maint - side * (liq_t / avg - 1)) - margin)


def plan(side: int, size: float, avg: float, margin: float, px: float, jupsol_usd: float, c: dict[str, Any],
         borrow_usd: float = 0.0) -> dict[str, Any]:
    """Collateral-only rescue plan (jupsol_loans.rescue_plan): min(collateral to target_pct, 1/3 of the JupSOL after the
    haircut); liq from margin - accrued borrow fee; EMERGENCY when that moves liq < emergency_min_push_pct points."""
    from live.jupsol_loans import rescue_plan
    return rescue_plan(side, size, avg, margin, borrow_usd, px, jupsol_usd, c)


def loan_out(b: dict[str, Any]) -> float:
    return K.loan_outstanding(b)


def repay_due(b: dict[str, Any], c: dict[str, Any] | None = None) -> float:
    """JupSOL to give back: exactly the JupSOL quantity still owed (Trevor 2026-10-27: no margin, no profit share)."""
    return round(K.loan_jupsol_owed(b), 9)


# ------------------------------------------------------------------ settings (toml edit with snapshot)
def _toml(bot: str) -> Path:
    return ROOT / BOTS[bot][1]


def get_rungs(bot: str) -> int:
    m = re.search(r"^ladder_max_rungs\s*=\s*(\d+)", _toml(bot).read_text(), re.M)
    return int(m.group(1)) if m else 12


def set_rungs(bot: str, n: int, why: str) -> None:
    p = _toml(bot)
    t = re.sub(r"^ladder_max_rungs\s*=\s*\d+[^\n]*", f"ladder_max_rungs = {n}          # {why}", p.read_text(), count=1, flags=re.M)
    p.write_text(t)


def extend(bot: str, b: dict[str, Any], n: int) -> tuple[int, int]:
    cur = get_rungs(bot)
    snap = b.setdefault("rescue_settings", {})
    snap.setdefault("dca.ladder_max_rungs", cur)   # first extension keeps the true original
    new = cur + n
    set_rungs(bot, new, f"RESCUE extended (orig {snap['dca.ladder_max_rungs']}); auto-restored when flat + loan repaid")
    K.note(b, "rescue_extend", 0.0, f"ladder_max_rungs {cur} -> {new}")
    return cur, new


def restore_settings(bot: str, b: dict[str, Any], cfg: Any = None) -> list[str]:
    snap = b.pop("rescue_settings", None) or {}
    done = []
    if "dca.ladder_max_rungs" in snap:
        o = int(snap["dca.ladder_max_rungs"])
        set_rungs(bot, o, "2026-10-09 15:08 config B (ladder sim): 14 main rungs" if o == 14 else "restored after rescue")
        if cfg is not None:
            try:
                object.__setattr__(cfg.dca, "ladder_max_rungs", o)
            except Exception:   # noqa: BLE001
                pass
        done.append(f"ladder_max_rungs -> {o}")
        K.note(b, "rescue_restore", 0.0, done[-1])
    return done


# ------------------------------------------------------------------ chain helpers (existing code paths)
def _jup(cmd: str, args: dict[str, Any]) -> dict[str, Any]:
    from live.live_broker import jup
    return jup(cmd, args)


def _load(bot: str) -> tuple[Any, Path, dict[str, Any]]:
    from config import load_config
    cfg = load_config(str(ROOT / BOTS[bot][1]), [])
    sp = ROOT / BOTS[bot][2]
    return cfg, sp, json.loads(sp.read_text())


def _save(sp: Path, st: dict[str, Any]) -> None:
    tmp = sp.with_suffix(".rescue.tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(sp)


def snapshot(bot: str) -> dict[str, Any]:
    asset = BOTS[bot][0]
    ch = _jup("state", {"market": asset})
    sol_px = float((ch.get("solOracle") or {}).get("price") or ch["oracle"]["price"])
    px = float(ch["oracle"]["price"])
    sk = ch.get("stake") or {}
    pos = None
    for s, k in ((1, "long"), (-1, "short")):
        p = ch["positions"].get(k) or {}
        if p.get("sizeUsd"):
            pos = {"side": s, "size": float(p["sizeUsd"]), "margin": float(p["collateralUsd"]), "avg": float(p["price"])}
    return {"chain": ch, "px": px, "sol_px": sol_px, "pos": pos, "jupsol": float(sk.get("jupsol") or 0.0),
            "sol_per_jupsol": float(sk.get("solPerJupsol") or 1.0), "jupsol_atoms": int(sk.get("jupsolAtoms") or 0)}


def status(c: dict[str, Any]) -> list[str]:
    out = []
    for bot in BOTS:
        try:
            cfg, sp, st = _load(bot)
            s = snapshot(bot)
        except Exception as e:   # noqa: BLE001
            out.append(f"RESCUE {bot}: ERROR {e}")
            continue
        b = st.get("bank") or {}
        from live import jupsol_loans as JL
        usable = JL.usable_jupsol(s["jupsol"], c["keep_jupsol"], JL.parked_jupsol(JL.reg_read()))
        ju = usable * s["sol_per_jupsol"] * s["sol_px"]
        loan = (f" LOAN {loan_out(b):.9f} {b.get('coin')} (owes {repay_due(b, c):.9f} JupSOL"
                f"{', carried to next flat' if 'carry_flat' in (b.get('loan') or {}) else ''})") if loan_out(b) > 0 else ""
        rungs = f" rungs {max(0, int(st.get('n_entries') or 0) - 1)}/{get_rungs(bot)}"
        mode = getattr(cfg.ledger, "jupsol_loans", "off")
        if not s["pos"]:
            out.append(f"RESCUE {bot}: flat | auto {mode} | JupSOL {s['jupsol']:.6f} usable ~${ju:.2f}{loan}{rungs}")
            continue
        p = s["pos"]
        borrow = float(((s["chain"].get("positions") or {}).get("long" if p["side"] > 0 else "short") or {}).get("borrowFeeUsd") or 0.0)
        pl = plan(p["side"], p["size"], p["avg"], p["margin"], s["px"], ju, c, borrow)
        flag = " EMERGENCY" if pl["emergency"] and pl["in_trigger"] else (" (would be EMERGENCY if triggered)" if pl["emergency"] else "")
        out.append(f"RESCUE {bot}: auto {mode} | {'long' if p['side'] > 0 else 'short'} ${p['size']:.2f} coll ${p['margin']:.2f} "
                   f"(borrow fee ${borrow:.2f}) px {s['px']:.2f} liq {pl['liq']:.2f} ({pl['dist_pct']:.1f}% away)"
                   f"{' IN-TRIGGER' if pl['in_trigger'] else ''} | JupSOL {s['jupsol']:.6f} (usable ~${ju:.2f}): to "
                   f"{c['target_pct']:g}% needs ${pl['need_usd']:.2f}, 1/3 cap ${pl['cap_usd']:.2f} -> ${pl['collateral_usd']:.2f} "
                   f"-> {pl['new_dist_pct']:.1f}% away (+{pl['push_pts']:.1f} pts){loan}{rungs}{flag}")
    return out


def lend(bot: str, amount: str, send: bool, c: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Manual rescue (service stopped): one ExactOut JupSOL -> coin swap, measured coin booked as the loan, all of it
    deposited as collateral. amount = "auto" or a USD collateral (still capped at 1/3 of the usable JupSOL)."""
    asset = BOTS[bot][0]
    cfg, sp, st = _load(bot)
    from live import jupsol_loans as JL
    from live import sweep as W
    s = snapshot(bot)
    if not s["pos"]:
        raise SystemExit(f"{bot}: no open position")
    p, coin = s["pos"], K.COIN[asset]
    r = JL.reg_read()
    usable = JL.usable_jupsol(s["jupsol"], c["keep_jupsol"], JL.parked_jupsol(r))
    rate_usd = s["sol_per_jupsol"] * s["sol_px"]
    borrow = float(((s["chain"].get("positions") or {}).get("long" if p["side"] > 0 else "short") or {}).get("borrowFeeUsd") or 0.0)
    pl = plan(p["side"], p["size"], p["avg"], p["margin"], s["px"], usable * rate_usd, c, borrow)
    if amount != "auto":
        pl["collateral_usd"] = min(float(amount), pl["cap_usd"])
    if send and not pl["in_trigger"] and not force:
        raise SystemExit(f"{bot}: liq {pl['dist_pct']}% away, not within trigger {c['trigger_pct']}%; refusing (--force to override)")
    out: dict[str, Any] = {"bot": bot, "send": send, "plan": pl, "EMERGENCY": pl["emergency"], "steps": []}
    cpx = s["px"] if asset != "SOL" else s["sol_px"]
    dec = K.DECIMALS[coin]
    atoms = int(pl["collateral_usd"] / cpx * 10 ** dec)
    if atoms <= 0:
        return {**out, "EMERGENCY": True, "why": "no usable JupSOL / nothing needed"}
    if send:
        os.environ["JUP_LIVE"] = "1"
    a1 = W.sweep_args(cfg, K.MINT["JUPSOL"], atoms, K.MINT[coin], "ExactOut")
    r1 = _jup("swap" if send else "simulate-swap", {**a1, "send": send})
    out["steps"].append({"jupsol_to_coin": {k: r1.get(k) for k in ("ok", "sig", "error", "err", "inAmount", "quotedOut", "unitsConsumed")}})
    if send and not (r1.get("ok") and r1.get("sig")):
        out["note"] = "swap not confirmed: nothing booked (check the wallet if it has a sig)"
        return out
    got, js, measured = JL.lend_measured(r1, coin) if send else (atoms / 10 ** dec, int(r1.get("inAmount") or 0) / LAMPORTS, False)
    slip = cfg.live.price_slippage_pct
    args: dict[str, Any] = {"side": "long" if p["side"] > 0 else "short", "collateralAtoms": str(int(round(got * 10 ** dec))),
                            "sizeUsd": 0, "market": asset,
                            "priceSlippageUsd": round(s["px"] * (1 + slip) if p["side"] > 0 else s["px"] * (1 - slip), 2)}
    if asset != "SOL":
        args["inputMint"] = K.MINT[coin]
    if p["side"] < 0:
        args["jupiterMinimumOut"] = str(int(got * cpx * (1 - slip) * USDC_ATOMS))
    out["coin_got"], out["jupsol_out"], out["measured"] = round(got, 9), round(js, 9), measured
    if send:
        b = st["bank"]
        K.loan_open(b, got, js, 0.0, f"JupSOL rescue (manual): {js:.9f} JupSOL -> {got:.9f} {coin} sig {r1.get('sig')}",
                    kind="rescue_manual", sig=r1.get("sig"))
        _save(sp, st)
        JL._stake_row("loan_out", f"rescue-manual-{asset}", r1, -js, f"{js:.9f} JupSOL lent -> {got:.9f} {coin}")
    r3 = _jup("increase" if send else "simulate-increase", {**args, "send": send})
    out["steps"].append({"add_collateral": {"args": args, **{k: r3.get(k) for k in ("ok", "sig", "error", "err", "unitsConsumed")}}})
    if send:
        b = st["bank"]
        if r3.get("sig") or r3.get("ok"):
            K.debit(b, got, "collateral", f"rescue collateral {got:.9f} {coin} sig {r3.get('sig')}")
        else:
            out["note"] = "collateral add failed: the borrowed coin stays in bal (repaid on flat); retry with rescue.sh add"
        b["window"] = max(b.get("window", 0), 10)
        _save(sp, st)
    return out


def manual_add(bot: str, usd: float, lev: float | None, send: bool) -> dict[str, Any]:
    asset = BOTS[bot][0]
    cfg, sp, st = _load(bot)
    s = snapshot(bot)
    if not s["pos"]:
        raise SystemExit(f"{bot}: no open position")
    p, coin = s["pos"], K.COIN[asset]
    lv = lev or min(float(st.get("last_leverage") or cfg.dca.lev_start) + cfg.dca.lev_step, cfg.dca.lev_cap)
    cpx = s["px"] if asset != "SOL" else s["sol_px"]
    dec = 9 if asset == "SOL" else K.DECIMALS[coin]
    nat = usd / cpx
    slip = cfg.live.price_slippage_pct
    args: dict[str, Any] = {"side": "long" if p["side"] > 0 else "short", "collateralAtoms": str(int(nat * 10 ** dec)),
                            "sizeUsd": round(usd * lv, 2), "market": asset,
                            "priceSlippageUsd": round(s["px"] * (1 + slip) if p["side"] > 0 else s["px"] * (1 - slip), 2)}
    if asset != "SOL":
        args["inputMint"] = K.MINT[coin]
    if p["side"] < 0:
        args["jupiterMinimumOut"] = str(int(usd * (1 - slip) * USDC_ATOMS))
    r = _jup("increase" if send else "simulate-increase", {**args, "send": send})
    if send and (r.get("sig") or r.get("ok")):
        K.debit(st["bank"], nat, "collateral", f"rescue manual add ${usd:.2f} x{lv:g} sig {r.get('sig')}")
        st["n_entries"] = int(st.get("n_entries") or 0) + 1
        st["last_leverage"], st["last_entry"] = lv, s["px"]
        st["bank"]["window"] = max(st["bank"].get("window", 0), 10)
        _save(sp, st)
        (ROOT / BOTS[bot][2]).parent.joinpath("RETP_NOW").touch()   # TP1/TP2 re-placed for the new average
    return {"bot": bot, "send": send, "args": args, "res": {k: r.get(k) for k in ("ok", "sig", "error", "err")}}


def repay(bot: str, b: dict[str, Any], cfg: Any, c: dict[str, Any], send: bool) -> dict[str, Any]:
    """Manual (service stopped, flat): buy back exactly the JupSOL still owed (ExactOut coin -> JupSOL). Not enough
    balance -> nothing sent (the bot carries it to its next flat). Restores settings once repaid."""
    asset = BOTS[bot][0]
    coin = K.COIN[asset]
    owed = repay_due(b, c)
    if owed <= 0:
        return {"bot": bot, "nothing": True, "restored": restore_settings(bot, b, cfg) if send else []}
    from live import jupsol_loans as JL
    from live import sweep as W
    args = W.sweep_args(cfg, K.MINT[coin], int(round(owed * LAMPORTS)), K.MINT["JUPSOL"], "ExactOut")
    q = _jup("swap-quote", args)
    need = int(q.get("otherAmountThreshold") or q.get("inAmount") or 0) / 10 ** K.DECIMALS[coin]
    if not q.get("ok") or need <= 0 or need > b["bal"]:
        return {"bot": bot, "owed_jupsol": owed, "ok": False, "error": f"quote {q.get('error') or ''} needs {need:.9f} {coin} "
                                                                      f"vs bal {b['bal']:.9f}: not sent (carried to the next flat)"}
    if send:
        os.environ["JUP_LIVE"] = "1"
    r = _jup("swap" if send else "simulate-swap", {**args, "send": send})
    ok = bool(r.get("ok")) and bool(r.get("sig"))
    res = {"bot": bot, "owed_jupsol": owed, "max_in": need, "send": send, "ok": ok or not send, "sig": r.get("sig"), "error": r.get("error")}
    if send and ok:
        spent, got, measured = JL.repay_measured(r, coin)
        res.update(spent=spent, jupsol_got=got, measured=measured,
                   book=K.loan_repay_jupsol(b, spent, got, f"manual repay to JupSOL sig {r.get('sig')}"))
        JL._stake_row("loan_repay", f"rescue-manual-repay-{asset}", r, got, f"{spent:.9f} {coin} -> {got:.9f} JupSOL repaid")
        if res["book"]["closed"]:
            res["restored"] = restore_settings(bot, b, cfg)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rescue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("lend"); p.add_argument("bot", choices=BOTS); p.add_argument("amount"); p.add_argument("--send", action="store_true")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("extend"); p.add_argument("bot", choices=BOTS); p.add_argument("n", nargs="?", type=int); p.add_argument("--send", action="store_true")
    p = sub.add_parser("add"); p.add_argument("bot", choices=BOTS); p.add_argument("usd", type=float); p.add_argument("--lev", type=float); p.add_argument("--send", action="store_true")
    p = sub.add_parser("restore"); p.add_argument("bot", choices=BOTS); p.add_argument("--send", action="store_true")
    a = ap.parse_args(argv)
    c = rcfg()
    if a.cmd == "status":
        lines = status(c)
        print("\n".join(lines))
        return 3 if any(ln.endswith("EMERGENCY") for ln in lines) else 0
    if a.cmd == "lend":
        r = lend(a.bot, a.amount, a.send, c, a.force)
        print(json.dumps(r, indent=1))
        if r.get("EMERGENCY"):
            print(f"EMERGENCY {a.bot}: JupSOL too small for meaningful relief -> notify Trevor now")
            return 3
        return 0
    cfg, sp, st = _load(a.bot)
    if a.cmd == "extend":
        n = a.n or c["extend_rungs"]
        if not a.send:
            print(f"DRY RUN extend {a.bot}: ladder_max_rungs {get_rungs(a.bot)} -> {get_rungs(a.bot) + n} (orig kept in bank.rescue_settings)")
            return 0
        print("extend", a.bot, extend(a.bot, st["bank"], n))
        _save(sp, st)
        return 0
    if a.cmd == "add":
        print(json.dumps(manual_add(a.bot, a.usd, a.lev, a.send), indent=1))
        return 0
    if a.cmd == "restore":
        s = snapshot(a.bot)
        if s["pos"]:
            print(f"{a.bot}: position still open; loan stays, settings kept")
            return 1
        r = repay(a.bot, st["bank"], cfg, c, a.send) if loan_out(st["bank"]) > 0 else {"restored": restore_settings(a.bot, st["bank"], cfg) if a.send else "dry"}
        print(json.dumps(r, indent=1))
        if a.send:
            _save(sp, st)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
