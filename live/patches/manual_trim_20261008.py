"""One-off manual trim (Trevor 2026-10-08 13:31 MT: "lock in those partials for both sol and btc").

Runs the bot's OWN trim path (Bot.execute_trim: one market decrease of the qualifying lots, collateral stays, lots /
trims / ledger trim row / rung re-arm / TP re-placement with the overall-net guard) once, bypassing only the time gate
(trim_after_hours). Lot selection = live/trim.select_lots at the live oracle read immediately before the send.
The bot process for the market MUST be stopped while this runs.

    .venv/bin/python -m live.patches.manual_trim_20261008 --config configs/live_100.toml [--send] [--set k=v ...]
Without --send: dry run (simulate-decrease, no state change).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from config import ROOT, load_config
from live import bot as B
from live import ledger
from live import trim as T

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--set", dest="overrides", action="append", default=[])
ap.add_argument("--send", action="store_true")
a = ap.parse_args()
ov = list(a.overrides) + ([] if a.send else ["live.dry_run=true"])
cfg = load_config(a.config, ov)
handlers = [logging.StreamHandler()]
if a.send:
    handlers.append(logging.FileHandler(ROOT / cfg.bot.state_dir / "logs" / "bot.log"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [manual-trim] %(message)s", handlers=handlers)
log = logging.getLogger("bot")
if cfg.bot.state_dir != "live":
    ledger.configure(cfg.bot.state_dir, cfg.bot.asset)
if not cfg.live.dry_run:
    os.environ["JUP_LIVE"] = "1"
if a.send and cfg.resting.partials_enabled:   # 2026-10-12: keep TP3 consistent - refuse while a TP3 order is tracked
    import json as _j
    _st = ROOT / cfg.bot.state_dir / "state.json"
    _p = _j.loads(_st.read_text()).get("partials") if _st.exists() else None
    if _p:
        sys.exit(f"manual trim refused: TP3 partial order(s) live {[x.get('pubkey') for x in _p]} ([resting] partials_enabled); "
                 "cancel TP3 first or run without --send for a dry run")
if not cfg.trim.enabled:
    sys.exit("trim not enabled in cfg (pass --set trim.enabled=true)")
b = B.Bot(cfg)
if b.killed():
    sys.exit("KILL present: refusing")
st = b.chain()
o = st["oracle"]
if not o.get("price") or o.get("ageSec", 1e9) > cfg.live.max_oracle_age_sec:
    sys.exit(f"oracle stale {o}")
price = o["price"]
b._sol_px = b.sol_price(st)
b.rescale(price)
pos = b.chain_position(st)
L, t, c = b.local, b.cfg.trim, b.cfg.costs
if pos is None or not L.side:
    sys.exit("no position / no local side")
if abs(pos.size - L.last_size) > 0.01 or pos.tp_hits:
    sys.exit(f"state mismatch: chain {pos.size} vs last_size {L.last_size}, tp_hits {pos.tp_hits}")
if not T.lots_match(L.lots, pos.size):
    sys.exit(f"lots total {sum(l['size'] for l in L.lots)} != chain size {pos.size}")
if [r for r in st.get("allOpenRequests", st["openRequests"]) if r.get("requestType") != "trigger"]:
    sys.exit("a market request is in flight")
for i, l in enumerate(L.lots):
    log.info("lot #%d %s @%.4f $%.2f thr %.3f gain@%.3f %+.4f", i + 1, l["time"][:19], l["price"], l["size"],
             T.lot_threshold(l, pos.side, t, c.open_fee, c.close_fee), price,
             T.lot_gain(l, pos.side, price, c.open_fee, c.close_fee, t.trim_network_usd))
idx = T.select_lots(L.lots, pos.side, price, t, c.open_fee, c.close_fee)
log.info("manual trim (%s): oracle %.4f, chain size %.2f avg %.4f coll %.4f -> qualifying lots %s",
         "SEND" if a.send else "DRY", price, pos.size, pos.avg_entry, pos.margin, [i + 1 for i in idx])
if not idx:
    sys.exit(0)
ok = b.execute_trim(pos, st, price, idx)
log.info("manual trim result: %s", ok)
