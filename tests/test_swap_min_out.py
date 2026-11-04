"""2026-11-03: short collateral-swap jupiterMinimumOut from a live Jupiter quote (fallback: margin x (1 - slip))."""
import dataclasses

import pytest

import live.bank as K
from config import load_config
from tests.test_chain_balance import _bot

CFGS = ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"]


@pytest.mark.parametrize("p", CFGS)
def test_config_keys_identical(p):
    L = load_config(p).live
    assert (L.swap_min_out_quote, L.swap_min_out_slippage_bps, L.swap_min_out_max_bps) == (True, 200, 300)


def _b(monkeypatch, tmp_path, **kw):
    b = _bot(monkeypatch, tmp_path, "configs/live_btc.toml", "BTC")
    if kw:
        b.live = dataclasses.replace(b.live, **kw)
    return b


def test_quote_based(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(b, "quote_out", lambda m, a: seen.update(m=m, a=a) or 10_493_904)
    assert b.swap_min_out(K.MINT["WBTC"], 13661, 10.5) == int(10_493_904 * 0.98)
    assert seen == {"m": K.MINT["WBTC"], "a": 13661}


def test_quote_failure_falls_back(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    def boom(*a):
        raise OSError("timeout")
    monkeypatch.setattr(b, "quote_out", boom)
    assert b.swap_min_out(K.MINT["WBTC"], 13661, 10.5) == 10_395_000


def test_implausible_quote_falls_back(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path)
    monkeypatch.setattr(b, "quote_out", lambda m, a: 9_000_000)
    assert b.swap_min_out(K.MINT["WBTC"], 13661, 10.5) == 10_395_000


def test_off_uses_old_rule(monkeypatch, tmp_path):
    b = _b(monkeypatch, tmp_path, swap_min_out_quote=False)
    monkeypatch.setattr(b, "quote_out", lambda m, a: pytest.fail("no quote when off"))
    assert b.swap_min_out(K.MINT["WBTC"], 13661, 10.5) == 10_395_000
