"""Read-only dry run of ladder preset C for SOL/ETH/BTC (live chain state, nothing sent, state not saved)."""
import json, subprocess, sys
import pandas as pd
from config import load_config
import live.bot as B
from live.budget import budget_from_state
from dca_levels import project_ladder
P = [l.strip() for l in open("configs/preset_ladder_c.txt") if l.strip() and not l.startswith("#")]
for path in ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"]:
    cfg = load_config(path, ["live.dry_run=true", *P]); bot = B.Bot(cfg); bot.save = lambda: None
    st = bot.chain(); px = st["oracle"]["price"]; bot._sol_px, bot._mkt_px = bot.sol_price(st), px; bot.rescale(px)
    pos = bot.chain_position(st)
    cash = max(0.0, bot.local.bank["bal"] * bot.coin_px(st)) if bot.bank_on else budget_from_state(
        st, cfg.bot.share, cfg.live.sol_fee_reserve, bot.asset, own_pos_collateral=pos.margin if pos else 0.0).free_usd
    s = bot.strategy
    for label, p, c in ([("current position", pos, cash)] if pos else []) + [("fresh open now", None, cash)]:
        if p is None:
            m = s.initial_margin(c); lev = cfg.dca.lev_start; side = pos.side if pos else 1
            p = B.ChainPosition(side=side, opened=pd.Timestamp.now(tz="UTC"), margin=m, size=m * lev, qty=m * lev / px, borrow=0.0,
                                n_entries=1, last_leverage=lev, first_margin=m, first_entry=px, last_entry=px, tp_hits=0, first_tp_time=None)
            c = c - m
        s.plan_next_add(p, c, pd.Timestamp.now(tz="UTC")); kw = s.plan_kwargs()
        pl = project_ladder(p, s._levels, available_usd=c, max_steps=40, **kw)
        print(f"## {bot.asset} {label}: {'short' if p.side < 0 else 'long'} size ${p.size:.2f} coll ${p.margin:.2f} x{p.last_leverage:g} "
              f"price {px:.2f} | free for adds ${c:.2f} -> {len(pl)} rungs")
        tsz, tco = p.size, p.margin
        for i, a in enumerate(pl, 1):
            tsz += a.margin * a.leverage; tco += a.margin
            print(f"   {i:2d} trigger {a.trigger:10.2f} coll ${a.margin:5.2f} x{a.leverage:4.1f} size ${a.margin*a.leverage:7.2f} "
                  f"-> avg {a.new_avg:10.2f} liq {a.liq:10.2f} eff {tsz/tco:4.1f}x")
        if pl:
            print(f"   after last rung: liq {abs(pl[-1].liq-pl[-1].trigger)/pl[-1].trigger*100:.2f}% beyond it, "
                  f"{abs(pl[-1].liq-px)/px*100:.2f}% from now; capital in trade ${tco:.2f}")
    if pos:
        r = subprocess.run([".venv/bin/python", "-m", "live.resting_preview", "--config", path, *sum([["--set", x] for x in P], [])],
                           capture_output=True, text=True, timeout=200)
        try:
            d = json.loads(r.stdout[r.stdout.index("{"):])
            print("   resting order now:", json.dumps(d.get("next_resting_add") or d.get("rest_add_plan") or {k: d[k] for k in list(d)[:8]}, default=str)[:700])
        except Exception:
            print("   resting preview:", (r.stdout + r.stderr)[-600:])
