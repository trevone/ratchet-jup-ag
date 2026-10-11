"""One-off seeding of the per-bot own-coin ledgers (Trevor 2026-10-08 21:37 MT). Run ONLY with all three bots stopped.

  free = native SOL - 0.02 reserve (JupSOL bucket and the open positions are outside "free")
  SOL bot bal = 50 % of free (stays SOL); ETH / BTC bots: 25 % of free each, swapped SOL -> ETH / WBTC for real.
  hwm = bal + the collateral this bot put into its CURRENT open trade, in its own coin at deposit-time prices
        (SOL: pg_dep_sol = exact lamports sent; ETH / BTC: sum(lot coll USD / lot fill price) incl. trimmed lots)
        = the seeded "starting capital before the current trade"; trade_dep = that collateral (SOL flat true-up).
  --dry-run: quotes only, nothing sent, nothing written.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from config import ROOT, load_config  # noqa: E402
from live import bank as K, sweep as S  # noqa: E402
from live.live_broker import jup  # noqa: E402

DRY = "--dry-run" in sys.argv
RESERVE = 0.02
cfg = load_config("configs/live_100.toml")
st = jup("state", {"market": "BTC"})
wallet = float(st["wallet"]["sol"])
sol_px = float(st["solOracle"]["price"])
btc_px = float(st["oracle"]["price"])
eth_px = float(jup("state", {"market": "ETH"})["oracle"]["price"])
free = round(wallet - RESERVE, 9)
sh = {"SOL": round(free * 0.5, 9), "ETH": round(free * 0.25, 9)}
sh["BTC"] = round(free - sh["SOL"] - sh["ETH"], 9)
print(json.dumps({"wallet_sol": wallet, "free": free, "shares_sol": sh, "sol_px": sol_px, "eth_px": eth_px, "btc_px": btc_px}))
if [r for r in st["allOpenRequests"] if r.get("requestType") != "trigger"]:
    raise SystemExit("market request in flight: abort")
got = {}
for a, coin in (("ETH", "ETH"), ("BTC", "WBTC")):
    args = S.sweep_args(cfg, K.MINT["SOL"], int(sh[a] * 1e9), K.MINT[coin])
    if DRY:
        q = jup("swap-quote", args)
        print(a, "quote", q.get("outAmount"), q.get("priceImpactPct"))
        got[a] = {"native": int(q["outAmount"]) / 1e8, "sig": None}
        continue
    os.environ["JUP_LIVE"] = "1"
    res = jup("swap", {**args, "send": True})
    d = res.get("deltas") or {}
    print(a, json.dumps({k: res.get(k) for k in ("ok", "sig", "err", "error", "inAmount", "quotedOut", "route")}),
          json.dumps({k: d.get(k) for k in ("feeLamports", "nativeDeltaLamports", "tokenDeltaAtoms")}))
    if not (res.get("ok") and res.get("sig")):
        raise SystemExit(f"{a} seed swap failed: abort (nothing written)")
    atoms = int((d.get("tokenDeltaAtoms") or {}).get(coin) or res["quotedOut"])
    got[a] = {"native": atoms / 1e8, "sig": res["sig"], "fee_sol": (d.get("feeLamports") or 0) / 1e9}

out = {}
for a, sd in (("SOL", "live"), ("ETH", "live/eth"), ("BTC", "live/btc")):
    p = ROOT / sd / "state.json"
    s = json.loads(p.read_text())
    if a == "SOL":
        bal, dep = sh["SOL"], float(s.get("pg_dep_sol") or 0.0)
        how = "pg_dep_sol (exact lamports sent)"
    else:
        lots = list(s.get("lots") or []) + [l for t in s.get("trims") or [] for l in t.get("lots") or []]
        bal, dep = got[a]["native"], sum(float(l["coll"]) / float(l["price"]) for l in lots)
        how = f"sum(coll/price) over {len(lots)} lots incl. trimmed"
    px = {"SOL": sol_px, "ETH": eth_px, "BTC": btc_px}[a]
    note = (f"seed 2026-10-08: {('50' if a == 'SOL' else '25')}% of free SOL {free:.9f} = {sh[a]:.9f} SOL"
            + (f" -> {bal:.9f} {K.COIN[a]} (swap {got[a]['sig']})" if a != "SOL" else "")
            + f"; hwm = bal + current-trade collateral {dep:.9f} {K.COIN[a]} ({how}); wallet reserve {RESERVE} SOL outside all bots")
    b = K.new_bank(a, bal, round(bal + dep, 9), note)
    b["trade_dep"] = round(dep, 9)
    if a != "SOL":
        b["fee_sol"] = got[a].get("fee_sol", 0.0)
    out[a] = {"bal": bal, "bal_usd": round(bal * px, 2), "hwm": b["hwm"], "hwm_usd": round(b["hwm"] * px, 2), "dep": dep,
              "sig": got.get(a, {}).get("sig")}
    if not DRY:
        s["bank"] = b
        s["day"] = ""            # day_start_equity re-initialised on the new (own-balance) equity basis
        p.write_text(json.dumps(s, indent=1))
print(json.dumps(out, indent=1))
