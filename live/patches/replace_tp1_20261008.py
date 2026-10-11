"""One-off (2026-10-08): re-place one bot's TP1 at an exact price (cancel old, place new), reusing the bot's own
cancel_request / place_tp (6003 retry, 3012 gone-check, fee booking in that bot's ledger). DRY RUN by default
(simulate-cancel + simulate-decrease only). --live sends; refuses unless the bot's KILL file is present (so the running
bot is not touching triggers) and the chain matches exactly (position size, 2 triggers, old TP1 price).
Usage: .venv/bin/python live/patches/replace_tp1_20261008.py <config> <old_tp1> <new_tp1> [--live]"""
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from config import load_config  # noqa: E402
from live import ledger  # noqa: E402
from live.live_broker import jup  # noqa: E402

cfg_path, old_px, new_px = sys.argv[1], float(sys.argv[2]), round(float(sys.argv[3]), 2)
LIVE = "--live" in sys.argv
cfg = load_config(cfg_path)
if cfg.bot.state_dir != "live":
    ledger.configure(cfg.bot.state_dir, cfg.bot.asset)
logf = ROOT / cfg.bot.state_dir / "logs" / "manual_tp1_20261008.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s MANUAL %(message)s",
                    handlers=[logging.FileHandler(logf), logging.StreamHandler()])
log = logging.getLogger("manual")
if LIVE:
    os.environ["JUP_LIVE"] = "1"
import live.bot as B  # noqa: E402

bot = B.Bot(cfg)
st = bot.chain()
pos = bot.chain_position(st)
reqs = bot.tp_requests(st)
log.info("%s position %s, triggers %s, oracle %.3f", cfg.bot.asset, pos and f"{pos.side:+d} ${pos.size:.2f} avg {pos.avg_entry:.3f}",
         [(r["triggerPrice"], r.get("sizeUsd"), r.get("entire")) for r in reqs], st["oracle"]["price"])
assert pos is not None and pos.tp_hits == 0, "no position"
assert len(reqs) == 2, f"expected 2 triggers, got {len(reqs)}"
old = [r for r in reqs if abs(r["triggerPrice"] - old_px) < 0.006]
assert len(old) == 1, f"old TP1 {old_px} not found exactly once"
other = [r for r in reqs if r is not old[0]][0]
assert pos.side * (other["triggerPrice"] - new_px) > 0, "new TP1 must stay inside TP2"
assert pos.side * (new_px - st["oracle"]["price"]) > bot.cfg.tp.tp_market_gap, "new TP1 not beyond market"
lv = cfg.tp.ladder[0]
side = "long" if pos.side == B.LONG else "short"
size_usd = round(pos.size * lv.close_pct, 2)
dec = {"side": side, "triggerPriceUsd": new_px, "triggerAbove": pos.side == B.LONG, "sizeUsd": size_usd, "market": cfg.bot.asset}
sim_c = jup("simulate-cancel", {"request": old[0]["pubkey"], "market": cfg.bot.asset})
sim_d = jup("simulate-decrease", dec)
log.info("dry run: cancel %s -> ok=%s err=%s; place TP1 %.2f $%.2f -> ok=%s err=%s", old[0]["pubkey"], sim_c.get("ok"),
         sim_c.get("err") or sim_c.get("error"), new_px, size_usd, sim_d.get("ok"), sim_d.get("err") or sim_d.get("error"))
if not LIVE:
    sys.exit(0)
assert sim_c.get("ok") and sim_d.get("ok"), "simulation failed; nothing sent"
assert bot.killed(), "KILL file must be present while replacing (running bot paused)"
bot.live = bot.live.__class__(**{**bot.live.__dict__, "dry_run": False}) if bot.live.dry_run else bot.live
c = bot.cancel_request(old[0]["pubkey"], pos.size)
log.info("cancel result: %s", c)
if c != "ok":
    log.error("cancel not ok (%s): NOT placing; check chain", c)
    sys.exit(2)
ok = bot.place_tp(pos, side, 0, new_px, 0.0)
log.info("place TP1 %.2f: %s", new_px, ok)
st2 = bot.chain()
log.info("after: triggers %s, size %s", [r["triggerPrice"] for r in bot.tp_requests(st2)],
         bot.chain_position(st2) and bot.chain_position(st2).size)
sys.exit(0 if ok else 3)
