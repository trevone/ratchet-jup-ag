import logging, os, sys
os.environ["JUP_LIVE"] = "1"
from config import load_config, ROOT
import live.bot as B
from live import ledger
path = sys.argv[1]
cfg = load_config(path)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [tp-resync] %(message)s",
                    handlers=[logging.FileHandler(ROOT / cfg.bot.state_dir / "logs" / "bot.log"), logging.StreamHandler(sys.stdout)])
if cfg.bot.state_dir != "live":
    ledger.configure(cfg.bot.state_dir, cfg.bot.asset)
b = B.Bot(cfg)
st = b.chain(); b._sol_px, b._mkt_px = b.sol_price(st), st["oracle"]["price"]; b.rescale(st["oracle"]["price"])
pos = b.chain_position(st)
print("before", [(r["pubkey"][:6], r["requestChange"], r["triggerPrice"]) for r in st["openRequests"]])
b.sync_tp_ladder(pos)
b.save()
st2 = b.chain()
print("after", [(r["pubkey"][:6], r["requestChange"], r["triggerPrice"]) for r in st2["openRequests"]], "tp_px", b.local.tp_px)
