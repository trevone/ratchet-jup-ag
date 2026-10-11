"""JupSOL liquidation rescue (Trevor 2026-10-11 08:23 MT). Dry run unless --send (and then only via rescue.sh, service stopped).

When a bot's position is within trigger_pct of liquidation: lend JupSOL -> the bot's own coin (JupSOL -> SOL ->
ETH / WBTC with the bot's own swap paths), deposit part as extra collateral (increase with sizeUsd 0; shorts: keeper swaps
to USDC as the bot already does) and credit the rest to the bot's balance. The loan is a bank LIABILITY (bank.loan):
excluded from HWM / profit guard / withdrawals. When the bot is flat it repays principal x (1 + repay_margin_pct) (+ optional
profit share) to JupSOL and restores any extended settings (live/bot.py bank_tick hook -> auto_repay).

  rescue.sh status
  rescue.sh lend <sol|eth|btc> <jupsol_amount|auto> [--send]
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
DEFAULTS = {"trigger_pct": 3.0, "target_pct": 10.0, "collateral_share": 0.6, "keep_jupsol": 0.0, "haircut_pct": 1.0,
            "repay_margin_pct": 5.0, "repay_profit_share": 0.0, "emergency_min_push_pct": 2.0,
            "emergency_min_collat_pct": 10.0, "extend_rungs": 6, "maint": 0.01}
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
         amount_usd: float | None = None) -> dict[str, Any]:
    """Split a loan (default: all usable JupSOL) into collateral (up to what reaches target_pct, max collateral_share
    of the loan unless more is needed to reach trigger_pct) and bank credit. EMERGENCY when the whole JupSOL as
    collateral can't push liq out by emergency_min_push_pct points or is < emergency_min_collat_pct of margin."""
    m = c["maint"]
    liq0 = liq_price(side, size, avg, margin, m)
    d0 = dist_pct(side, px, liq0)
    avail = jupsol_usd * (1 - c["haircut_pct"] / 100)
    loan = min(avail, amount_usd) if amount_usd is not None else avail
    need_t = collateral_for(side, size, avg, margin, px, c["target_pct"], m)
    need_trig = collateral_for(side, size, avg, margin, px, c["trigger_pct"] + 0.5, m)
    coll = min(loan, max(min(need_t, loan * c["collateral_share"]), need_trig))
    full_push = dist_pct(side, px, liq_price(side, size, avg, margin + avail, m)) - d0
    emergency = full_push < c["emergency_min_push_pct"] or avail < margin * c["emergency_min_collat_pct"] / 100
    liq1 = liq_price(side, size, avg, margin + coll, m)
    return {"liq": round(liq0, 4), "dist_pct": round(d0, 2), "loan_usd": round(loan, 2), "collateral_usd": round(coll, 2),
            "credit_usd": round(loan - coll, 2), "new_liq": round(liq1, 4), "new_dist_pct": round(dist_pct(side, px, liq1), 2),
            "max_push_pts": round(full_push, 2), "need_usd_target": round(need_t, 2),
            "need_usd_at_trigger": round(size * px / avg * (c["target_pct"] - c["trigger_pct"]) / 100, 2), "emergency": emergency,
            "in_trigger": d0 < c["trigger_pct"]}


def loan_out(b: dict[str, Any]) -> float:
    return K.loan_outstanding(b)


def repay_due(b: dict[str, Any], c: dict[str, Any]) -> float:
    """Native coin to send back to JupSOL: principal x (1 + margin%) + profit_share x profit over the loan-time HWM."""
    ln = b.get("loan") or {}
    p = float(ln.get("principal", 0.0))
    if p <= 0:
        return 0.0
    prof = max(0.0, b["bal"] - p - float(ln.get("hwm_at_open", b["hwm"])))
    return round(p * (1 + c["repay_margin_pct"] / 100) + c["repay_profit_share"] * prof, 9)


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
        usable = max(0.0, s["jupsol"] - c["keep_jupsol"])
        ju = usable * s["sol_per_jupsol"] * s["sol_px"]
        loan = f" LOAN {loan_out(b):.9f} {b.get('coin')} (repay {repay_due(b, c):.9f})" if loan_out(b) > 0 else ""
        rungs = f" rungs {max(0, int(st.get('n_entries') or 0) - 1)}/{get_rungs(bot)}"
        if not s["pos"]:
            out.append(f"RESCUE {bot}: flat | JupSOL {s['jupsol']:.6f} (~${ju:.2f}){loan}{rungs}")
            continue
        p = s["pos"]
        pl = plan(p["side"], p["size"], p["avg"], p["margin"], s["px"], ju, c)
        flag = " EMERGENCY" if pl["emergency"] and pl["in_trigger"] else (" (would be EMERGENCY if triggered)" if pl["emergency"] else "")
        out.append(f"RESCUE {bot}: {'long' if p['side'] > 0 else 'short'} ${p['size']:.2f} coll ${p['margin']:.2f} px {s['px']:.2f} "
                   f"liq {pl['liq']:.2f} ({pl['dist_pct']:.1f}% away){' IN-TRIGGER' if pl['in_trigger'] else ''} | JupSOL {s['jupsol']:.6f} "
                   f"(~${ju:.2f}) all-in collateral -> +{pl['max_push_pts']:.1f} pts; to {c['target_pct']:g}% needs ${pl['need_usd_target']:.2f} "
                   f"(at {c['trigger_pct']:g}% trigger: ${pl['need_usd_at_trigger']:.2f})"
                   f"{loan}{rungs}{flag}")
    return out


def lend(bot: str, amount: str, send: bool, c: dict[str, Any], coll_override: float | None = None,
         force: bool = False) -> dict[str, Any]:
    asset = BOTS[bot][0]
    cfg, sp, st = _load(bot)
    from live import stake as S
    from live import sweep as W
    s = snapshot(bot)
    if not s["pos"]:
        raise SystemExit(f"{bot}: no open position")
    usable = max(0.0, s["jupsol"] - c["keep_jupsol"])
    js = usable if amount == "auto" else min(float(amount), usable)
    if js <= 0:
        return {"bot": bot, "EMERGENCY": True, "why": "no JupSOL available"}
    rate_usd = s["sol_per_jupsol"] * s["sol_px"]
    p = s["pos"]
    pl = plan(p["side"], p["size"], p["avg"], p["margin"], s["px"], usable * rate_usd, c, js * rate_usd * (1 - c["haircut_pct"] / 100))
    if coll_override is not None:
        pl["collateral_usd"] = coll_override
    if send and not pl["in_trigger"] and not force:
        raise SystemExit(f"{bot}: liq {pl['dist_pct']}% away, not within trigger {c['trigger_pct']}%; refusing (--force to override)")
    out: dict[str, Any] = {"bot": bot, "send": send, "jupsol_in": round(js, 9), "plan": pl, "EMERGENCY": pl["emergency"], "steps": []}
    # 1) JupSOL -> SOL (stake CLI "unstake" path)
    a1 = S.swap_args(cfg, "unstake", int(js * LAMPORTS))
    r1 = _jup("swap" if send else "simulate-swap", {**a1, "send": send})
    out["steps"].append({"unstake": {k: r1.get(k) for k in ("ok", "sig", "error", "err", "quotedOut", "unitsConsumed")}})
    if send and not r1.get("sig"):
        return out
    sol_got = int(r1.get("quotedOut") or 0) / LAMPORTS
    d = r1.get("deltas") or {}
    if send and d.get("solDeltaLamports") is not None:
        sol_got = int(d["solDeltaLamports"]) / LAMPORTS
    coin = K.COIN[asset]
    got = sol_got
    if asset != "SOL":   # 2) SOL -> ETH / WBTC (own-coin swap path)
        a2 = W.sweep_args(cfg, K.MINT["SOL"], int(sol_got * 0.995 * LAMPORTS), K.MINT[coin])
        r2 = _jup("swap" if send else "simulate-swap", {**a2, "send": send})
        out["steps"].append({"sol_to_coin": {k: r2.get(k) for k in ("ok", "sig", "error", "err", "quotedOut")}})
        if send and not r2.get("sig"):
            out["note"] = "JupSOL already unstaked to SOL; SOL->coin failed: SOL sits in wallet (book manually)"
            return out
        got = int(r2.get("quotedOut") or 0) / 10 ** K.DECIMALS[coin]
    cpx = s["px"] if asset != "SOL" else s["sol_px"]
    got_usd = got * cpx
    coll_usd = min(pl["collateral_usd"], got_usd)
    coll_nat = coll_usd / cpx
    # 3) collateral deposit = increase with sizeUsd 0 (same builder / args as open_or_add)
    slip = cfg.live.price_slippage_pct
    side = "long" if p["side"] > 0 else "short"
    dec = 9 if asset == "SOL" else K.DECIMALS[coin]
    args: dict[str, Any] = {"side": side, "collateralAtoms": str(int(coll_nat * 10 ** dec)), "sizeUsd": 0,
                            "priceSlippageUsd": round(s["px"] * (1 + slip) if p["side"] > 0 else s["px"] * (1 - slip), 2),
                            "market": asset}
    if asset != "SOL":
        args["inputMint"] = K.MINT[coin]
    if p["side"] < 0:
        args["jupiterMinimumOut"] = str(int(coll_usd * (1 - slip) * USDC_ATOMS))
    elif asset != "SOL" and "inputMint" not in args:
        pass
    r3 = (_jup("increase" if send else "simulate-increase", {**args, "send": send}) if coll_nat > 0
          else {"ok": True, "error": "skipped: no collateral needed (liq already beyond target); all credited to bal"})
    out["steps"].append({"add_collateral": {"args": args, **{k: r3.get(k) for k in ("ok", "sig", "error", "err", "unitsConsumed", "logs")
                                                             if k != "logs"}}})
    out["coin_got"], out["collateral_native"], out["credit_native"] = round(got, 9), round(coll_nat, 9), round(got - coll_nat, 9)
    if send:
        os.environ["JUP_LIVE"] = "1"
        b = st["bank"]
        K.loan_open(b, got, js, c["repay_margin_pct"], f"JupSOL rescue: {js:.9f} JupSOL -> {got:.9f} {coin}")
        if r3.get("sig") or r3.get("ok"):
            K.debit(b, coll_nat, "collateral", f"rescue collateral ${coll_usd:.2f} sig {r3.get('sig')}")
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
    """Flat + open loan: coin -> JupSOL for repay_due (capped at bal), book it, restore settings."""
    asset = BOTS[bot][0]
    coin = K.COIN[asset]
    due = min(repay_due(b, c), b["bal"])
    if due <= 0:
        return {"bot": bot, "nothing": True, "restored": restore_settings(bot, b, cfg) if send else []}
    if asset == "SOL":
        from live.stake import stake_sol
        r = stake_sol(cfg, due, source="rescue-repay-SOL", send=send)
    else:
        from live import sweep as W
        if send:
            os.environ["JUP_LIVE"] = "1"
        r = _jup("swap" if send else "simulate-swap",
                 {**W.sweep_args(cfg, K.MINT[coin], int(due * 10 ** K.DECIMALS[coin]), K.MINT["JUPSOL"]), "send": send})
    ok = bool(r.get("ok")) and bool(r.get("sig"))
    res = {"bot": bot, "due": due, "send": send, "ok": ok or not send, "sig": r.get("sig"), "error": r.get("error")}
    if send and ok:
        K.loan_repay(b, due, f"repay to JupSOL sig {r.get('sig')}")
        res["restored"] = restore_settings(bot, b, cfg)
    return res


def auto_repay(botobj: Any) -> None:
    """Hook from live/bot.py bank_tick (flat, settled window, loan open). Errors are logged, retried next tick."""
    import logging
    log = logging.getLogger("live")
    bot = {"SOL": "sol", "ETH": "eth", "BTC": "btc"}[botobj.asset]
    b = botobj.local.bank
    r = repay(bot, b, botobj.cfg, rcfg(), send=True)
    log.warning("RESCUE auto-repay %s", json.dumps(r))
    if r.get("ok") and r.get("sig"):
        b["window"] = max(b.get("window", 0), botobj.cfg.ledger.window_ticks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rescue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("lend"); p.add_argument("bot", choices=BOTS); p.add_argument("amount"); p.add_argument("--send", action="store_true")
    p.add_argument("--collateral-usd", type=float, help="override collateral part (testing / manual)"); p.add_argument("--force", action="store_true")
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
        r = lend(a.bot, a.amount, a.send, c, a.collateral_usd, a.force)
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
