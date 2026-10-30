"""Event-driven backtest: features -> DcaStrategy -> SimBroker, one closed bar at a time.

    python backtest.py --config configs/with_stops.toml --set dca.max_adds=3 --set tp.pct=0.06
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from broker import SimBroker
from config import ROOT, Config, dump_config, load_config
from data import load
from indicators import compute_features
from metrics import summarize
from strategy import Bar, DcaStrategy

RUNS_DIR = ROOT / "runs"
HOUR = pd.Timedelta(hours=1)


@dataclass(frozen=True)
class BacktestResult:
    config: Config
    trades: pd.DataFrame
    equity: pd.Series
    prices: pd.Series
    metrics: dict[str, Any]


def iter_bars(features: pd.DataFrame) -> list[Bar]:
    f = features
    nan = pd.Series(float("nan"), index=f.index)
    cols = pd.DataFrame({k: f[k] for k in ("open", "high", "low", "close", "rsi", "trend_up")})
    cols["ma_htf"] = f["ma_htf"] if "ma_htf" in f else nan
    cols["sma"] = f["sma_sig"] if "sma_sig" in f else nan
    return [Bar(t, o, h, l, c, r, None if pd.isna(tu) else bool(tu), float(m), float(sm))
            for t, (o, h, l, c, r, tu, m, sm) in zip(cols.index, cols.itertuples(index=False, name=None))]


def run_backtest(cfg: Config, features: pd.DataFrame) -> BacktestResult:
    """``features`` must come from ``compute_features(base, cfg)`` (or an equivalent cfg)."""
    strategy = DcaStrategy(cfg)
    broker = SimBroker(cfg.costs, cfg.risk, cash=cfg.account.start_equity,
                       bar_hours=pd.Timedelta(cfg.signal.tf) / HOUR, on_fill=strategy.on_fill)
    bars = iter_bars(features)
    equity = np.empty(len(bars))
    for i, bar in enumerate(bars):
        strategy.begin_bar(bar, broker)
        if broker.position is not None:
            broker.process_resting(bar, strategy.exit_levels(broker.position))
        broker.execute(strategy.exit_intents(bar, broker), bar.time)
        broker.execute(strategy.entry_intents(bar, broker), bar.time)
        equity[i] = broker.equity(bar.close)
    eq = pd.Series(equity, index=features.index, name="equity")
    trades = pd.DataFrame(broker.trades)
    metrics = summarize(trades, eq, features["close"], cfg.account.start_equity)
    metrics["open_position_at_end"] = broker.snapshot(float(features["close"].iloc[-1]))
    return BacktestResult(cfg, trades, eq, features["close"], metrics)


def features_for(cfg: Config) -> pd.DataFrame:
    return compute_features(load(cfg.market.symbol, cfg.market.base_tf), cfg)


def plot_equity(res: BacktestResult, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    start = res.config.account.start_equity
    bh = start * res.prices / res.prices.iloc[0]
    fig, ax = plt.subplots(2, 1, figsize=(13, 9), sharex=True, gridspec_kw=dict(height_ratios=[3, 1]))
    ax[0].plot(bh, label=f"Buy & hold {res.config.market.symbol}", color="gray", lw=1)
    ax[0].plot(res.equity, label=res.config.name, lw=1.3)
    ax[0].set_yscale("log")
    ax[0].set_ylabel(f"Equity, USD (log), start ${start:,.0f}")
    m = res.metrics
    ax[0].set_title(f"{res.config.name} [{res.config.short_hash()}]  final ${m['final_equity']:,.2f}  "
                    f"maxDD {m['max_drawdown']:.1%}  trades {m['trades']}")
    ax[0].legend()
    ax[0].grid(alpha=0.3, which="both")
    ax[1].plot(res.equity / res.equity.cummax() - 1, lw=1, label=res.config.name)
    ax[1].plot(bh / bh.cummax() - 1, color="gray", lw=0.8, label="buy & hold")
    ax[1].set_ylabel("Drawdown")
    ax[1].grid(alpha=0.3)
    ax[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def save_run(res: BacktestResult, runs_dir: Path = RUNS_DIR, plot: bool = False) -> Path:
    """Write ``runs/<YYYYmmdd-HHMMSS>_<cfg-hash>/`` with config, metrics, trades, equity, plot."""
    out = runs_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{res.config.short_hash()}"
    out.mkdir(parents=True, exist_ok=True)
    dump_config(res.config, out / "config.json")
    with open(out / "metrics.json", "w") as f:
        json.dump(res.metrics, f, indent=2, default=str)
    res.trades.to_csv(out / "trades.csv", index=False)
    res.equity.to_csv(out / "equity.csv", index_label="time")
    if plot:  # off by default for speed; --plot to enable
        plot_equity(res, out / "equity.png")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="preset TOML merged over configs/default.toml")
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                    help="override any parameter, e.g. --set dca.max_adds=3 (repeatable)")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--plot", action="store_true", help="also write equity.png")
    a = ap.parse_args()

    cfg = load_config(a.config, a.overrides)
    res = run_backtest(cfg, features_for(cfg))
    keys = ["trades", "final_equity", "return_pct", "max_drawdown", "win_rate", "profit_factor",
            "liquidations", "avg_adds", "final_sol", "sharpe", "sortino"]
    print(json.dumps({"config": cfg.name, "hash": cfg.short_hash(), **{k: res.metrics.get(k) for k in keys}},
                     indent=2, default=str))
    if not a.no_save:
        print("saved to", save_run(res, a.runs_dir, plot=a.plot))


if __name__ == "__main__":
    main()
