"""Manual market-close of the whole open position (cancel TPs, then close). Run via live/close_now.sh,
which stops the bot loop first so the two never act at the same time, then restarts it."""
from __future__ import annotations

import logging
import os
import sys

from config import load_config
from live.bot import Bot, ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(ROOT / "live" / "logs" / "bot.log"), logging.StreamHandler(sys.stdout)])
cfg = load_config("configs/live_100.toml")
if cfg.live.dry_run:
    sys.exit("dry_run config: refusing")
os.environ["JUP_LIVE"] = "1"
bot = Bot(cfg)
st = bot.chain()
pos = bot.chain_position(st)
if pos is None:
    print("no open position; nothing to close")
    if st["openRequests"]:
        bot.cancel_all(st)
    sys.exit(0)
bot.close(pos, st["oracle"]["price"], "manual close_now")
bot.save()
after = bot.chain()
print("after:", after["positions"], "requests:", len(after["openRequests"]), "wallet SOL:", after["wallet"]["sol"])
