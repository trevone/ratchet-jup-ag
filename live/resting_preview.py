"""Dry-run preview of the [resting] orders for the current live position (nothing is signed or sent).

    .venv/bin/python -m live.resting_preview --config configs/live_100.toml   # SOL (also live_eth.toml / live_btc.toml)

Reads the chain (read-only `state`), turns the three [resting] flags ON in memory only (dry_run forced) and shows: the
next resting limit add (built by the Jupiter Perps API, decoded + checked, simulated with sigVerify off; never signed),
and the single partial TP the position would get now (armed + trim-eligible adds; trigger decrease simulated).
Never writes state.json / ledgers (save disabled)."""
from __future__ import annotations

import argparse
import dataclasses
import json

import pandas as pd

from config import load_config
from live import trim as trimmod
from live.budget import budget_from_state
from live.live_broker import jup

ON = ["live.dry_run=true", "resting.adds_enabled=true", "resting.partials_enabled=true", "resting.last_partial_rule=true"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--set", action="append", default=[], help="config override k=v (e.g. a preset), repeatable")
    a = ap.parse_args()
    import live.bot as B
    from strategy import Action, Intent
    cfg = load_config(a.config, [*ON, *a.set])
    bot = B.Bot(cfg)
    bot.save = lambda: None
    st = bot.chain()
    price = st["oracle"]["price"]
    bot._sol_px, bot._mkt_px = bot.sol_price(st), price
    bot.rescale(price)
    cfg = bot.cfg
    pos = bot.chain_position(st)
    out: dict = {"market": bot.asset, "price": price, "time_utc": str(pd.Timestamp.now(tz="UTC"))}
    if pos is None:
        out.update(position=None, note="flat: no resting orders (they exist only while a position is open)")
        print(json.dumps(out, indent=1, default=str))
        return
    L = bot.local
    out["position"] = {"side": "long" if pos.side > 0 else "short", "size": pos.size, "avg": round(pos.avg_entry, 4),
                       "collateral": pos.margin, "n_entries": L.n_entries, "last_entry": L.last_entry,
                       "last_leverage": L.last_leverage, "last_partial_px": L.last_partial_px}
    budget = budget_from_state(st, cfg.bot.share, cfg.live.sol_fee_reserve, bot.asset, own_pos_collateral=pos.margin)
    cash = max(0.0, L.bank["bal"] * bot.coin_px(st)) if bot.bank_on else budget.free_usd
    bot.budget = dataclasses.replace(budget, free_usd=cash)
    out["free_collateral_usd"] = round(cash, 4)
    lv = bot.strategy.levels_provider(pd.Timestamp.now(tz="UTC")) if bot.strategy.levels_provider else None
    plan, why = bot.rest_add_plan(pos, cash)
    if plan is None:
        out["resting_add"] = {"would_place": None, "why": why}
    else:
        g = bot.guard(Intent(Action.ADD, pos.side, plan.trigger, plan.margin, plan.leverage), pos, st, 0.0)
        args, nat, _ = bot.rest_add_args(pos, st, plan)
        res = jup("simulate-create-limit-order", {**args, "market": bot.asset})
        out["resting_add"] = {"would_place": args, "collateral_usd": round(plan.margin, 4), "leverage": plan.leverage,
                              "plan_source": plan.source, "guard": g or "ok", "native_debit": nat,
                              "api": {k: res.get(k) for k in ("ok", "refused", "code", "error", "positionRequest",
                                                               "requireKeeperSignature", "simOk", "simErr", "quote")},
                              "verify_problems": (res.get("verify") or {}).get("problems")}
        lot = {"time": "projected", "price": plan.trigger, "size": args["sizeUsd"], "rung": L.n_entries}
        out["partial_if_that_add_alone"] = bot.partial_plan(pos, [lot], lv)
    t, c = cfg.trim, cfg.costs
    armed = L.trim_armed or trimmod.arm_hit(pos.side, pos.avg_entry, price, t.trim_arm_dist_pct)
    elig = set(trimmod.select_lots(L.lots, pos.side, price, t, c.open_fee, c.close_fee))
    due = [l for i, l in enumerate(L.lots) if i in elig and not (l.get("first") or i == 0)]
    tgt = bot.partial_plan(pos, due, lv) if due else None
    sp = {"armed": armed, "arm_price": round(trimmod.arm_price(pos.side, pos.avg_entry, t.trim_arm_dist_pct), 4),
          "eligible_adds": [f"@{l['price']:.4f} ${l['size']}" for l in due], "target": tgt,
          "would_place_now": bool(armed and tgt and pos.side * (tgt["px"] - price) >= cfg.tp.tp_market_gap)}
    if sp["would_place_now"]:
        d = {"side": out["position"]["side"], "sizeUsd": tgt["size"], "triggerPriceUsd": tgt["px"], "triggerAbove": pos.side > 0}
        r3 = jup("simulate-decrease", {**d, "market": bot.asset})
        sp.update(would_place=d, sim={k: r3.get(k) for k in ("ok", "err", "label")})
    out["single_partial_now"] = sp
    out["limit_orders_api"] = jup("limit-orders", {})
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
