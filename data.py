"""Binance spot klines from https://data.binance.vision (public, no API key).

``python data.py`` (re)builds ``data/<SYMBOL>_<TF>.csv`` from ``data/raw/<SYMBOL>-<TF>-YYYY-MM.zip``;
``python data.py --download 2020-08 2026-09`` fetches the monthly zips first.
"""
from __future__ import annotations

import argparse
import io
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
RAW_DIR = DATA_DIR / "raw"
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_vol", "trades", "tb_base", "tb_quote", "ignore"]
MICROSECOND_THRESHOLD = 10**14  # Binance spot files use microseconds from 2025 on
MICROS_PER_MILLI = 1000


def csv_path(symbol: str, tf: str) -> Path:
    return DATA_DIR / f"{symbol}_{tf}.csv"


def download(symbol: str, tf: str, start: str, end: str) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for month in pd.period_range(start, end, freq="M"):
        name = f"{symbol}-{tf}-{month}.zip"
        dest = RAW_DIR / name
        if not dest.exists():
            urllib.request.urlretrieve(f"{BASE_URL}/{symbol}/{tf}/{name}", dest)


def build_csv(symbol: str = "SOLUSDT", tf: str = "1h") -> pd.DataFrame:
    frames = []
    for f in sorted(RAW_DIR.glob(f"{symbol}-{tf}-*.zip")):
        with zipfile.ZipFile(f) as z:
            df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None, names=COLS)
        ts = df["open_time"].astype("int64")
        ts = ts.where(ts < MICROSECOND_THRESHOLD, ts // MICROS_PER_MILLI)
        df["time"] = pd.to_datetime(ts, unit="ms", utc=True)
        frames.append(df[["time", "open", "high", "low", "close", "volume"]])
    if not frames:
        raise FileNotFoundError(f"no {symbol}-{tf}-*.zip files in {RAW_DIR}")
    out = pd.concat(frames).drop_duplicates("time").sort_values("time")
    out.to_csv(csv_path(symbol, tf), index=False)
    return out


def load(symbol: str = "SOLUSDT", tf: str = "1h") -> pd.DataFrame:
    """Load candles, preferring a fast pickle cache next to the CSV (rebuilt if stale)."""
    path = csv_path(symbol, tf)
    if not path.exists():
        build_csv(symbol, tf)
    cache = path.with_suffix(".pkl")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        return pd.read_pickle(cache)
    df = pd.read_csv(path, parse_dates=["time"]).set_index("time")
    df.to_pickle(cache)
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--download", nargs=2, metavar=("START_YYYY-MM", "END_YYYY-MM"))
    a = ap.parse_args()
    if a.download:
        download(a.symbol, a.tf, *a.download)
    d = build_csv(a.symbol, a.tf)
    gaps = d["time"].diff().dropna()
    print(len(d), d["time"].min(), d["time"].max(),
          "| gaps > tf:", int((gaps > pd.Timedelta(a.tf)).sum()), "max gap:", gaps.max())


if __name__ == "__main__":
    main()
