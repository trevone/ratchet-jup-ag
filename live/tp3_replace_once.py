import logging, os, sys
os.environ["JUP_LIVE"] = "1"
import pandas as pd
from config import load_config, ROOT
import live.bot as B
from live import ledger
cfg = load_config("configs/live_eth.toml")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [tp3-replace] %(message)s",
                    handlers=[logging.FileHandler(ROOT / cfg.bot.state_dir / "logs" / "bot.log"), logging.StreamHandler(sys.stdout)])
ledger.configure(cfg.bot.state_dir, cfg.bot.asset)
b = B.Bot(cfg)
st = b.chain(); px = st["oracle"]["price"]; b._sol_px, b._mkt_px = b.sol_price(st), px; b.rescale(px)
pos = b.chain_position(st); L = b.local
old = L.partials[0]
assert any(r["pubkey"] == old["pubkey"] for r in st["openRequests"]), "old TP3 not on-chain"
lots = [l for l in L.lots if l["time"] in old["lots"]]
r = b.cancel_request(old["pubkey"], pos.size)
print("cancel old TP3", old["pubkey"], old["px"], "->", r)
assert r == "ok"
L.partials = []
b.save()
ok = b._place_partial(b.chain_position(b.chain()), px, lots, "TP3 zone rule re-place (Trevor 00:53 MT)")
print("placed", ok, L.partials)
st2 = b.chain()
print("on-chain", [(q["pubkey"][:6], q["requestChange"], q["triggerPrice"]) for q in st2["openRequests"]])
