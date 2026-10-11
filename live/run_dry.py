"""Dry run: live candles -> strategy -> LiveBroker (build + simulate only). Never sends a transaction.

    .venv/bin/python -m live.run_dry --config configs/live_100.toml --iterations 3
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.request

import pandas as pd

from broker import SimBroker
from config import load_config
from backtest import iter_bars
from indicators import compute_features
from live.live_broker import LiveBroker, jup
from strategy import DcaStrategy

KLINES = "https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={tf}&limit=1000&endTime={end}"
WARMUP_PAGES = 2   # 2000 1h bars > 200 x 4h EMA warm-up
HTTP_TIMEOUT = 8
log = logging.getLogger("live")


def fetch_klines(symbol: str, tf: str) -> pd.DataFrame:
    rows, end = [], int(time.time() * 1000)
    for _ in range(WARMUP_PAGES):
        with urllib.request.urlopen(KLINES.format(sym=symbol, tf=tf, end=end), timeout=HTTP_TIMEOUT) as r:
            page = json.load(r)
        rows = page + rows
        end = page[0][0] - 1
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.set_index("time").astype(float)
    closed = df.index + pd.Timedelta(tf) <= pd.Timestamp.now(tz="UTC")
    return df[closed]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/live_100.toml")
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--replay-bars", type=int, default=48, help="recent closed bars replayed to seed state")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    assert cfg.live.dry_run, "this runner only supports dry_run = true"

    state = jup("state")
    log.info("on-chain state: %s", json.dumps(state)[:600])
    strat = DcaStrategy(cfg)
    broker = LiveBroker(cfg, SimBroker(cfg.costs, cfg.risk, cash=cfg.account.start_equity, bar_hours=1.0,
                                       on_fill=strat.on_fill), oracle_price=state.get("oracle", {}).get("price", 0.0))
    seen = None
    for i in range(a.iterations):
        feats = compute_features(fetch_klines(cfg.market.symbol, cfg.signal.tf), cfg)
        bars = iter_bars(feats)
        bars = bars[-a.replay_bars:] if seen is None else [b for b in bars if b.time > seen]
        for bar in bars:
            strat.begin_bar(bar, broker)
            if broker.position is not None:
                broker.process_resting(bar, strat.exit_levels(broker.position))
            broker.execute(strat.exit_intents(bar, broker), bar.time)
            broker.execute(strat.entry_intents(bar, broker), bar.time)
        seen = bars[-1].time if bars else seen
        last = bars[-1] if bars else None
        log.info("iter %d: %d new bars, last %s close=%s rsi=%.1f trend_up=%s pos=%s journal=%d", i, len(bars),
                 last and last.time, last and last.close, last.rsi if last else float("nan"),
                 last and last.trend_up, broker.shadow.snapshot(last.close) if last else None, len(broker.journal))
        if i + 1 < a.iterations:
            time.sleep(cfg.live.poll_interval_sec)
    print(json.dumps(broker.journal, default=str, indent=1))


if __name__ == "__main__":
    main()
