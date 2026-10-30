"""Indicator math and multi-timeframe feature building (pure functions, no state)."""
from __future__ import annotations

import pandas as pd

from config import Config, SignalConfig, TrendConfig

OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
RSI_MAX = 100.0


def rsi(close: pd.Series, length: int, method: str = "wilder") -> pd.Series:
    """Relative Strength Index. ``wilder`` = RMA smoothing (alpha = 1/length, TradingView default)."""
    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    if method == "wilder":
        avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    elif method == "ema":
        avg_gain = gain.ewm(span=length, adjust=False).mean()
        avg_loss = loss.ewm(span=length, adjust=False).mean()
    elif method == "sma":
        avg_gain = gain.rolling(length).mean()
        avg_loss = loss.rolling(length).mean()
    else:
        raise ValueError(f"unknown RSI method {method!r}")
    return RSI_MAX - RSI_MAX / (1 + avg_gain / avg_loss)


def moving_average(series: pd.Series, length: int, ma_type: str) -> pd.Series:
    if ma_type == "ema":
        return series.ewm(span=length, adjust=False).mean()
    if ma_type == "sma":
        return series.rolling(length).mean()
    raise ValueError(f"unknown moving-average type {ma_type!r}")


def resample_ohlc(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate OHLC(V) bars labelled by open time; drops empty buckets."""
    agg = {k: v for k, v in OHLCV_AGG.items() if k in df.columns}
    return df.resample(tf, label="left", closed="left").agg(agg).dropna(subset=["close"])


def trend_frame(base: pd.DataFrame, cfg: TrendConfig) -> pd.DataFrame:
    """Higher-TF close and MA, keyed by the bar's *close* time (when it becomes known)."""
    htf = resample_ohlc(base, cfg.tf)
    htf["ma_htf"] = moving_average(htf["close"], cfg.length, cfg.ma_type)
    warmup = cfg.length if cfg.warmup_bars is None else cfg.warmup_bars
    htf.loc[htf.index[:warmup], "ma_htf"] = float("nan")
    htf["close_time"] = htf.index + pd.Timedelta(cfg.tf)
    return htf[["close_time", "close", "ma_htf"]].rename(columns={"close": "close_htf"})


def signal_frame(base: pd.DataFrame, base_tf: str, cfg: SignalConfig) -> pd.DataFrame:
    sig = base.copy() if pd.Timedelta(cfg.tf) == pd.Timedelta(base_tf) else resample_ohlc(base, cfg.tf)
    sig["rsi"] = rsi(sig["close"], cfg.rsi_length, cfg.rsi_method)
    sig["sma_sig"] = moving_average(sig["close"], cfg.entry_sma_length, "sma")
    sig["close_time"] = sig.index + pd.Timedelta(cfg.tf)
    return sig


def compute_features(base: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Signal-TF bars with ``rsi`` and ``trend_up``; only *closed* trend bars are used (no look-ahead).

    Rows still in indicator warm-up are dropped, then ``market.start``/``market.end`` are applied.
    """
    sig = signal_frame(base, cfg.market.base_tf, cfg.signal)
    htf = trend_frame(base, cfg.trend)
    m = pd.merge_asof(sig.reset_index().sort_values("close_time"), htf,
                      on="close_time", direction="backward").set_index("time")
    m["trend_up"] = (m["close_htf"] > m["ma_htf"]).where(m["ma_htf"].notna())
    m = m[m["trend_up"].notna() & m["rsi"].notna()]
    if cfg.market.start is not None:
        m = m[m.index >= pd.Timestamp(cfg.market.start, tz="UTC")]
    if cfg.market.end is not None:
        m = m[m.index < pd.Timestamp(cfg.market.end, tz="UTC")]
    return m
