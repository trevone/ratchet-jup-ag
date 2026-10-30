"""Entry rule price_ma (Trevor 2026-10-29 20:52 MT): 4h EMA200 + 15m SMA10, price vs line, both must agree."""
import pandas as pd
import pytest

from config import load_config
from strategy import LONG, SHORT, Action, Bar, DcaStrategy


class Acct:
    cash = 100.0
    position = None

    def equity(self, price):
        return 100.0


def strat(*over):
    return DcaStrategy(load_config("configs/live_100.toml", ["sides=both", *over]))


def go(s, b, acct):
    s.begin_bar(b, acct)
    return s.entry_intents(b, acct)


def bar(px, ema, sma, rsi=50.0):
    return Bar(pd.Timestamp("2026-10-30T03:00Z"), px, px, px, px, rsi, px > ema, ema, sma)


@pytest.mark.parametrize("px,ema,sma,want", [
    (110.0, 100.0, 105.0, LONG),    # above EMA (long) + above SMA (long) -> LONG
    (90.0, 100.0, 95.0, SHORT),     # below EMA (short) + below SMA (short) -> SHORT
    (110.0, 100.0, 115.0, None),    # above EMA (long), below SMA (short) -> disagree, no entry
    (90.0, 100.0, 85.0, None),      # below EMA (short), above SMA (long) -> disagree, no entry
    (100.0, 100.0, 95.0, None),     # price == EMA -> no long-period signal
    (110.0, 100.0, 110.0, None),    # price == SMA -> no short-period signal
    (100.0, 100.0, 100.0, None),    # both equal
])
def test_four_cases_and_equality(px, ema, sma, want):
    s = strat()
    out = go(s, bar(px, ema, sma), Acct())
    if want is None:
        assert out == []
    else:
        assert len(out) == 1 and out[0].action is Action.OPEN and out[0].side == want
    assert "RSI off" in s.entry_thresholds(bar(px, ema, sma))[2]


@pytest.mark.parametrize("rsi", [5.0, 50.0, 95.0])
def test_rsi_ignored(rsi):
    s = strat()
    assert go(s, bar(110.0, 100.0, 105.0, rsi), Acct())[0].side == LONG
    assert go(s, bar(90.0, 100.0, 95.0, rsi), Acct())[0].side == SHORT


def test_sides_bias_still_respected():
    s = strat("sides=short")
    assert go(s, bar(110.0, 100.0, 105.0), Acct()) == []          # both long but longs disabled
    assert go(s, bar(90.0, 100.0, 95.0), Acct())[0].side == SHORT


def test_idle_loosening_does_not_open():
    s = strat()
    s._flat_since = pd.Timestamp("2026-10-29T00:00Z")   # flat for many hours: old idle step would loosen RSI
    assert go(s, bar(110.0, 100.0, 115.0, rsi=10.0), Acct()) == []


def test_warmup_nan_stays_flat():
    s = strat()
    assert go(s, bar(110.0, float("nan"), 105.0), Acct()) == []
    assert go(s, bar(110.0, 100.0, float("nan")), Acct()) == []


def test_live_configs_use_price_ma():
    for p in ("configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"):
        c = load_config(p)
        assert c.signal.entry_rule == "price_ma" and c.signal.entry_sma_length == 10
        assert c.trend.tf == "4h" and c.trend.ma_type == "ema" and c.trend.length == 200 and c.signal.tf == "15m"


def test_features_carry_ema_and_sma():
    from backtest import iter_bars
    from indicators import compute_features
    idx = pd.date_range("2026-01-01", periods=4 * 16 * 260, freq="15min", tz="UTC", name="time")
    close = pd.Series(range(len(idx)), index=idx, dtype=float) * 0.01 + 100
    df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0})
    cfg = load_config("configs/live_100.toml", ["sides=both"])
    b = iter_bars(compute_features(df, cfg))[-1]
    assert b.sma == pytest.approx(close.iloc[-10:].mean())
    assert b.ma_htf == b.ma_htf and b.close > b.ma_htf   # rising series: above EMA and SMA -> LONG
    assert DcaStrategy(cfg).price_ma_side(b)[0] == LONG
