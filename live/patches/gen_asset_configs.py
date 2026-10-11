"""Generate configs/live_eth.toml and configs/live_btc.toml from configs/live_100.toml (SOL), so every strategy key
stays identical to SOL except the per-asset ones below. Usage: python gen_asset_configs.py <root> [--live]"""
import re
import sys
from pathlib import Path

R = Path(sys.argv[1])
LIVE = "--live" in sys.argv
src = (R / "configs" / "live_100.toml").read_text()
# drop SOL-only multi-asset keys: its [bot] section (share 0.5) and the stake's trade_files list
src = re.sub(r"(?ms)^\[bot\]\n.*?(?=^\[|\Z)", "", src)
src = re.sub(r"(?m)^# multi-asset \(Trevor 2026-10-07 20:23 MT\): this SOL bot runs.*\n", "", src)
src = re.sub(r"(?m)^trade_files = .*\n", "", src)
src = re.sub(r"(?m)^# multi-asset \(Trevor approved 2026-10-07 20:23 MT\).*\n", "", src)

# SOL reference (2026-10-07 ~$116, own 1h ATR ~0.73): TP1 +0.50 = 0.43%, TP2 +0.75 = 0.65%, fixed spacing 0.85 = 1.16 ATR,
# smart-TP gaps 0.05 / 0.05 / 0.02 and deep buffer 0.15 USD -> fractions of price below.
ASSETS = {
    "eth": dict(asset="ETH", symbol="ETHUSDT", share=0.25, start_equity=26.0),
    "btc": dict(asset="BTC", symbol="BTCUSDT", share=0.25, start_equity=26.0),
}


def sub(s, pattern, repl, section):
    """Replace one `key = ...` line inside [section]."""
    m = re.search(rf"(?ms)^\[{re.escape(section)}\]\n(.*?)(?=^\[|\Z)", s)
    if not m:
        raise SystemExit(f"section [{section}] not found")
    body = m.group(1)
    new, n = re.subn(rf"(?m)^{pattern}[^\n]*$", repl, body, count=1)
    if n != 1:
        raise SystemExit(f"[{section}] {pattern!r} not found")
    return s[:m.start(1)] + new + s[m.end(1):]


def ensure_absent(s, key, section):
    m = re.search(rf"(?ms)^\[{re.escape(section)}\]\n(.*?)(?=^\[|\Z)", s)
    if m and re.search(rf"(?m)^{key}\s*=", m.group(1)):
        raise SystemExit(f"[{section}] already has {key}")


for d, a in ASSETS.items():
    s = src
    s = sub(s, r'name = ', f'name = "live_{d}"', "") if False else s.replace('name = "live_100"', f'name = "live_{d}"', 1)
    header = (f"# {a['asset']} perps bot (multi-asset, Trevor approved 2026-10-07 20:23 MT: wallet split SOL 50% / ETH 25% / BTC 25%).\n"
              f"# GENERATED from configs/live_100.toml by live/patches/gen_asset_configs.py: every strategy key is SOL's except\n"
              f"# [bot], [scale], market.symbol, [tp] mode/levels (percent of avg), live.kill_switch_path / dry_run, dca.levels_path,\n"
              f"# withdraw.enabled (only the SOL bot runs the one daily stake over all bots' trade files).\n")
    s = header + s
    s = s.replace("[market]\n", f'[market]\nsymbol = "{a["symbol"]}"          # Binance klines (same source as SOL): 15m signals, 4h EMA200\n', 1)
    s = sub(s, r"start_equity = ", f"start_equity = {a['start_equity']}", "account")
    s = sub(s, r"levels = ", "mode = \"price_pct\"            # TP distances as a fraction of the average entry (SOL: +$0.50/+$0.75 at ~$116)\n"
                             "levels = [{ dist = 0.0045, close_pct = 0.5 }, { dist = 0.0075, close_pct = 1.0 }]   # TP1 = 60% of TP2 (ratio mode sets TP1 live)\n"
                             "tp_res_min_touches = 2         # TP2 cap only at analyst levels with >= 2 touches (h1+h4 merged); unknown = strong", "tp")
    s = sub(s, r"dry_run = ", f"dry_run = {'false' if LIVE else 'true'}", "live")
    s = sub(s, r'kill_switch_path = ', f'kill_switch_path = "live/{d}/KILL"', "live")
    s = sub(s, r'levels_path = ', f'levels_path = "/workspace/market-analysis/{d}/latest.json"   # analyst per asset; stale/missing -> own-ATR fallback', "dca")
    s = sub(s, r"trigger_dist = ", "trigger_dist = 0.85            # overridden every tick by [scale] trigger_dist_atr x own 1h ATR", "dca")
    s = sub(s, r"enabled = true", "enabled = false                # the SOL bot runs the single daily stake (its withdraw.trade_files include this bot's trades.csv)", "withdraw")
    s += f"""
[bot]
asset = "{a['asset']}"                    # Jupiter Perps market (custody); long collateral = {a['asset'] if a['asset'] == 'ETH' else 'WBTC'} (SOL swapped by the request), short = USDC
share = {a['share']}                    # 25% of the shared wallet budget (live/budget.py)
state_dir = "live/{d}"            # state.json, trades.csv, ledger_*.json, KILL, PAUSE_UNTIL, logs/
extra_pause_files = ["live/PAUSE_UNTIL"]   # also obey the SOL operator's event pause (fresh opens only)
global_kill_path = "live/KILL_ALL"

[scale]
# per-asset price distances re-derived every tick (asset_scale.py); SOL equivalents at ~$116 / ATR ~0.73 in brackets
enabled = true
atr_tf = "1h"
atr_len = 14
trigger_dist_atr = 1.15        # fixed rung spacing = 1.15 x own 1h ATR   [0.85]
trigger_dist_pct = 0.0073      # ... only until the first klines give an ATR [0.85 / 116]
deep_buffer_pct = 0.0013       # above-water TP1 buffer after deep adds  [$0.15]
tp_min_gap_pct = 0.00043       # TP1 at least this inside TP2            [$0.05]
tp_min_move_pct = 0.00043      # periodic TP re-place threshold          [$0.05]
tp_market_gap_pct = 0.00017    # trigger at least this beyond market     [$0.02]
own_atr_fallback = true        # analyst file missing/stale -> ladder on own ATR (no S/R), not trigger_dist as ATR
"""
    (R / "configs" / f"live_{d}.toml").write_text(s)
    print("wrote", f"configs/live_{d}.toml", "dry_run" if not LIVE else "LIVE")
