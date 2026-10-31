"""Trevor 2026-10-31: chain mode. (1) A payout seen on chain is credited to trade_recv (ETH / BTC), so a cancelled add's
refund (booked against trade_dep) never leaves a row at "-deposited, received 0" (ETH 07:49 / BTC 08:45 MT 10-30).
(2) A payout seen on chain clears pay_wait, so guard TPs re-place on the next tick, not ~14 min later (BTC 09:32 / 14:23)."""
import dataclasses

import pytest

import live.bank as K
from config import load_config
from tests.test_bank import _st
from tests.test_chain_balance import _bot

CFGS = {"SOL": "configs/live_100.toml", "ETH": "configs/live_eth.toml", "BTC": "configs/live_btc.toml"}


def _coin_st(asset, coin, amt):
    st = _st(asset=asset, size=0)
    st["sweepTokens"] = {coin: {"mint": K.MINT[coin], "atoms": str(round(amt * 10 ** K.DECIMALS[coin]))}}
    return st


def _mk(monkeypatch, tmp_path, asset, bal):
    import live.bot as B
    b = _bot(monkeypatch, tmp_path, CFGS[asset], asset)
    bk = K.new_bank(asset, bal, 0.06 if asset == "ETH" else 0.0016)
    bk["chain_v"] = B.CHAIN_V
    b.local.bank = bk
    return b


@pytest.mark.parametrize("p", list(CFGS.values()))
def test_flags_on_for_all_bots(p):
    lc = load_config(p).ledger
    assert lc.chain_payout_recv is True and lc.chain_payout_clears_wait is True


def test_eth_0749_replay_refund_not_money_in(monkeypatch, tmp_path):
    """ETH trade 06:01-07:49 MT 10-30, figures from the bot log: entry + resting add debited, TP1 paid 0.00243226,
    resting add cancelled (refund 0.00470863 booked into bal first), TP2 paid 0.00244441, refund then lands on chain."""
    b = _mk(monkeypatch, tmp_path, "ETH", 0.04349582)
    bk = b.local.bank
    bk["trade_dep"], bk["trade_recv"] = 0.00940956, 0.0
    bk["window"] = 3
    b.chain_balance_sync(_coin_st("ETH", "ETH", 0.04592808), None)          # TP1 payout
    bk["window"] = 3
    bk["bal"] = round(bk["bal"] + 0.00470863, 9)                             # _rest_bank_refund
    bk["trade_dep"] = round(bk["trade_dep"] - 0.00470863, 9)
    b.chain_balance_sync(_coin_st("ETH", "ETH", 0.04837249), None)          # TP2 in, refund not yet
    b.chain_balance_sync(_coin_st("ETH", "ETH", 0.05308112), None)          # refund lands
    assert bk["trade_dep"] == pytest.approx(0.00470093)
    assert bk["trade_recv"] == pytest.approx(0.00487667)
    assert bk["trade_recv"] - bk["trade_dep"] == pytest.approx(0.00017574)  # chain: 0.05308112 - 0.05290538


def test_btc_0845_replay(monkeypatch, tmp_path):
    b = _mk(monkeypatch, tmp_path, "BTC", 0.00127292)
    bk = b.local.bank
    bk["trade_dep"], bk["window"] = 0.00027161, 3
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.00134276), None)         # TP1
    bk["bal"] = round(bk["bal"] + 0.00013583, 9)
    bk["trade_dep"] = round(bk["trade_dep"] - 0.00013583, 9)                # resting add refund
    bk["window"] = 3
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.00154824), None)         # close payout
    assert bk["trade_recv"] - bk["trade_dep"] == pytest.approx(0.00000371)


def test_no_credit_outside_payout_phase(monkeypatch, tmp_path):
    b = _mk(monkeypatch, tmp_path, "BTC", 0.0013)
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.0013001), None)
    assert b.local.bank["trade_recv"] == 0.0


def test_capital_not_credited(monkeypatch, tmp_path):
    """An idle own-token deposit stays capital (chain_capital), never trade_recv."""
    b = _mk(monkeypatch, tmp_path, "BTC", 0.0013)
    bk = b.local.bank
    bk["chain_idle"] = True
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.0016), None)
    assert bk["trade_recv"] == 0.0


def test_sol_counters_untouched(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, CFGS["SOL"], "SOL")
    import live.bot as B
    bk = K.new_bank("SOL", 1.0, 1.2)
    bk["chain_v"], bk["window"] = B.CHAIN_V, 3
    b.local.bank = bk
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 1.02 + 0.05
    b.chain_balance_sync(st, None)
    assert bk["trade_recv"] == 0.0                                          # SOL books from its position flows


def test_recv_flag_off(monkeypatch, tmp_path):
    b = _mk(monkeypatch, tmp_path, "BTC", 0.00127292)
    b.cfg = dataclasses.replace(b.cfg, ledger=dataclasses.replace(b.cfg.ledger, chain_payout_recv=False))
    b.local.bank["window"] = 3
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.00134276), None)
    assert b.local.bank["trade_recv"] == 0.0


class _Pos:
    side = -1


def test_partial_payout_clears_pay_wait_and_unblocks_guard(monkeypatch, tmp_path):
    """BTC 09:32 MT: per-add partial paid 0.00016178; pay_wait (20) kept the TPs stale ~14 min."""
    b = _mk(monkeypatch, tmp_path, "BTC", 0.00113988)
    bk = b.local.bank
    b.bank_payout(12.53, "per-add partial $78.75")
    assert bk["pay_wait"] == b.GUARD_DEFER_MAX
    st = _coin_st("BTC", "WBTC", 0.00130166)
    assert b.guard_bank_stale(st) is True
    b.chain_balance_sync(st, _Pos())
    assert bk["pay_wait"] == 0 and b.guard_bank_stale(st) is False
    assert bk["trade_recv"] == pytest.approx(0.00016178)


def test_pay_wait_kept_without_rise_or_flag_off(monkeypatch, tmp_path):
    b = _mk(monkeypatch, tmp_path, "BTC", 0.00113988)
    b.bank_payout(12.53, "partial")
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.00113988), _Pos())      # nothing landed yet
    assert b.local.bank["pay_wait"] == b.GUARD_DEFER_MAX
    b.cfg = dataclasses.replace(b.cfg, ledger=dataclasses.replace(b.cfg.ledger, chain_payout_clears_wait=False))
    b.chain_balance_sync(_coin_st("BTC", "WBTC", 0.00130166), _Pos())
    assert b.local.bank["pay_wait"] == b.GUARD_DEFER_MAX
