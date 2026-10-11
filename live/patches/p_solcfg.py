"""configs/live_100.toml: SOL bot share 0.5 + withdraw over all bots' trade files. Usage: python p_solcfg.py <root>"""
import sys
from pathlib import Path
p = Path(sys.argv[1]) / "configs" / "live_100.toml"
s = p.read_text()
if "\n[bot]\n" in s:
    raise SystemExit("live_100.toml already has [bot]")
old = 'stake_api_bases = ["https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1"]'
assert s.count(old) == 1, "stake_api_bases line not found"
i = s.index(old)
eol = s.index("\n", i) if "\n" in s[i:] else len(s)
s = s[:eol] + '''
# multi-asset (Trevor 2026-10-07 20:23 MT): this SOL bot runs the ONE daily stake over every bot's realized trades
trade_files = ["live/trades.csv", "live/eth/trades.csv", "live/btc/trades.csv"]''' + s[eol:]
s = s.rstrip("\n") + '''

[bot]
# multi-asset (Trevor approved 2026-10-07 20:23 MT): SOL 50% / ETH 25% / BTC 25% of ONE wallet (live/budget.py).
# free collateral = share x (native SOL - reserve + every bot's open collateral) - own open collateral, capped at native free.
asset = "SOL"
share = 0.5
state_dir = "live"             # unchanged paths: live/state.json, live/trades.csv, live/KILL, live/PAUSE_UNTIL, live/logs/
global_kill_path = "live/KILL_ALL"   # touch to stop new orders on all three bots
'''
p.write_text(s)
print("live_100.toml patched")
