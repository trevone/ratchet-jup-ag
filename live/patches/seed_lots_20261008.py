"""One-off (2026-10-08): write the current SOL long's lots into live/state.json (bot must be stopped).
Fills / sizes / collateral from the on-chain IncreasePositionEvents (live/patches/lot_fills_20261008.ts), matching the
bot.log "increase" ok:true lines and ledger_open.json (7 entries, $581.00)."""
import json
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "state.json"
EV = [  # blockTime UTC, fill price, sizeUsdDelta, collateralUsdDelta, feeUsd, leverage, sig prefix
    ("2026-10-08T03:15:20+00:00", 115.806777, 73.5, 10.49979, 0.05145, 7, "STuWPLeTuk"),
    ("2026-10-08T04:45:20+00:00", 114.52679, 27.0, 3.000164, 0.020758, 9, "4ZgQS5pQQD"),
    ("2026-10-08T12:30:24+00:00", 112.694296, 33.0, 2.997425, 0.036112, 11, "3JvVwxqA5D"),
    ("2026-10-08T13:45:36+00:00", 111.518876, 51.33, 3.948735, 0.0387, 13, "3GWSZH6179"),
    ("2026-10-08T15:30:05+00:00", 108.87995, 241.63, 16.104993, 0.174468, 15, "2AqTTsaCAs"),
    ("2026-10-08T17:00:55+00:00", 107.516917, 76.49, 4.502816, 0.063651, 17, "4wL4URKk3q"),
    ("2026-10-08T18:00:55+00:00", 106.439585, 78.05, 4.112916, 0.062548, 19, "5XY2iEsaws"),
]
d = json.loads(P.read_text())
assert d["side"] == 1 and d["n_entries"] == 7 and abs(d["last_size"] - 581) < 0.01, d
d["lots"] = [{"time": t, "price": p, "size": s, "coll": round(c - f, 6), "lev": lev, "first": i == 0, "sig": sig,
              "source": "rebuilt 2026-10-08 from on-chain events + bot.log"} for i, (t, p, s, c, f, lev, sig) in enumerate(EV)]
d["trade_opened_usd"] = round(sum(x[2] for x in EV), 2)
d.setdefault("trims", [])
P.write_text(json.dumps(d, indent=1))
print("lots", len(d["lots"]), "size", d["trade_opened_usd"], "coll", round(sum(l["coll"] for l in d["lots"]), 4))
