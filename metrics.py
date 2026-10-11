"""Performance metrics from a trade log and an equity curve."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

YEAR = pd.Timedelta("365.25D")


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp, pd.Timestamp]:
    """(max drawdown as a negative fraction, trough time, preceding peak time)."""
    dd = equity / equity.cummax() - 1
    trough = dd.idxmin()
    return float(dd.min()), trough, equity[:trough].idxmax()


def _annualised_ratio(returns: pd.Series, downside_only: bool, periods_per_year: float) -> float | None:
    dev = returns[returns < 0] if downside_only else returns
    sd = math.sqrt((dev**2).mean()) if downside_only else returns.std()
    if not sd or math.isnan(sd):
        return None
    return float(returns.mean() / sd * math.sqrt(periods_per_year))


def summarize(trades: pd.DataFrame, equity: pd.Series, prices: pd.Series, start_equity: float) -> dict[str, Any]:
    p0, p1 = float(prices.iloc[0]), float(prices.iloc[-1])
    dd, dd_t, dd_peak_t = max_drawdown(equity)
    final = float(equity.iloc[-1])
    years = (equity.index[-1] - equity.index[0]) / YEAR
    bar = equity.index.to_series().diff().median()
    rets = equity.pct_change().dropna()
    out: dict[str, Any] = dict(
        trades=len(trades), final_equity=final, return_pct=final / start_equity - 1,
        cagr=(final / start_equity) ** (1 / years) - 1 if final > 0 else -1.0,
        max_drawdown=dd, dd_peak=str(dd_peak_t), dd_trough=str(dd_t),
        sharpe=_annualised_ratio(rets, False, YEAR / bar),
        sortino=_annualised_ratio(rets, True, YEAR / bar),
        final_sol=final / p1, bh_sol=start_equity / p0, sol_vs_bh=(final / p1) / (start_equity / p0) - 1,
        bh_return_pct=p1 / p0 - 1,
    )
    if len(trades):
        wins, losses = trades.pnl[trades.pnl > 0], trades.pnl[trades.pnl <= 0]
        worst = trades.loc[trades.pnl.idxmin()]
        costs = trades.fees + trades.borrow + trades.slippage
        gross = trades.pnl + costs
        out.update(
            win_rate=len(wins) / len(trades),
            profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
            longs=int((trades.side > 0).sum()), shorts=int((trades.side < 0).sum()),
            liquidations=int((trades.reason == "liquidation").sum()),
            avg_adds=float(trades.adds.mean()), max_adds_used=int(trades.adds.max()),
            exit_reasons={str(k): int(v) for k, v in trades.reason.value_counts().items()},
            adds_hist={str(k): int(v) for k, v in trades.adds.value_counts().sort_index().items()},
            worst_trade_pnl=float(worst.pnl),
            worst_trade_pct_equity=float(worst.pnl / (worst.equity_after - worst.pnl)),
            avg_hours_in_trade=float(trades.hours.mean()), max_hours_in_trade=float(trades.hours.max()),
            worst_unrealized_pct_margin=float((trades.max_adverse / trades.margin).min()),
            total_fees=float(trades.fees.sum()), total_borrow=float(trades.borrow.sum()),
            total_slippage=float(trades.slippage.sum()),
            avg_win=float(wins.mean()) if len(wins) else None,
            avg_loss=float(losses.mean()) if len(losses) else None,
            # share of pre-cost gross profit consumed by fees + borrow + slippage
            cost_share_of_gross=float(costs.sum() / gross[gross > 0].sum()) if (gross > 0).any() else None,
        )
    else:
        out.update(win_rate=None, profit_factor=None, liquidations=0, avg_adds=None)
    yearly = pd.concat([pd.Series([start_equity], index=[equity.index[0]]), equity.resample("YE").last()])
    out["yearly_return"] = {str(k.year): round(float(v), 4) for k, v in yearly.pct_change().dropna().items()}
    return {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in out.items()}
