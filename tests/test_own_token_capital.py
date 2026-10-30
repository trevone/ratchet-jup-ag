"""Trevor 2026-10-30 07:02 MT: a bot's own token is the truth; a confirmed own-token change is its capital at once."""
import dataclasses

import pytest

import live.bank as K
from config import load_config
from tests.test_bank import _st


def _bot(monkeypatch, tmp_path, path, asset):
    import live.bot as B
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    ov = ["live.dry_run=true"]
    if asset != "SOL":
        monkeypatch.setattr(B, "ROOT", tmp_path.parent)
        ov.append(f"bot.state_dir={tmp_path.name}")
    b = B.Bot(load_config(path, ov))
    b.live = dataclasses.replace(b.live, dry_run=False)
    return b


@pytest.mark.parametrize("p", ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"])
def test_live_configs_use_own(p):
    assert load_config(p).ledger.ext_flow_mode == "own"


def test_sol_deposit_is_capital_after_two_ticks(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.95, 1.174)
    b.local.bank["wd_mark"] = 1.16
    b.local.bank["trade_start_equity"] = 1.17
    b.local.day_start_equity = 100.0
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.97
    b.bank_tick(st, None, 96.0)                            # baseline
    st["wallet"]["sol"] = 0.97 + 0.9375                    # ~$90 SOL deposit
    b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.95)      # one tick: pending only
    b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.95 + 0.9375) and bk["hwm"] == pytest.approx(1.174 + 0.9375)
    assert bk["wd_mark"] == pytest.approx(1.16 + 0.9375) and bk["trade_start_equity"] == pytest.approx(1.17 + 0.9375)
    assert b.local.day_start_equity == pytest.approx(100.0 + 0.9375 * b.sol_price(st))
    assert bk["deposits"][-1]["kind"] == "own_external" and K.load_journal()["events"] == []   # never split
    for _ in range(3):                                     # booked once
        b.bank_tick(st, None, 96.0)
    assert bk["bal"] == pytest.approx(0.95 + 0.9375)


def test_sol_small_change_is_reserve_float(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.95, 1.174)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.97
    b.bank_tick(st, None, 96.0)
    st["wallet"]["sol"] = 0.97 + 0.01                      # ~$0.96 < ext_min_usd 1.5
    for _ in range(3):
        b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.95)


def test_sol_outflow_booked_and_alerted(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.95, 1.174)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.97
    b.bank_tick(st, None, 96.0)
    st["wallet"]["sol"] = 0.77
    for _ in range(3):
        b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.75) and b.local.bank["hwm"] == pytest.approx(0.974)
    assert "own capital out" in K.alert_path().read_text()


def test_eth_token_deposit_is_own_capital(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_eth.toml", "ETH")
    b.local.bank = K.new_bank("ETH", 0.05, 0.05)
    b.local.bank["trade_start_equity"] = 0.05
    st = _st(asset="ETH", size=0)
    coin = b.local.bank["coin"]
    st.setdefault("sweepTokens", {})[coin] = {"atoms": int(0.09 * 10 ** K.DECIMALS[coin])}
    for _ in range(3):
        b.bank_tick(st, None, 2200.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.09) and bk["hwm"] == pytest.approx(0.09) and bk["wd_mark"] == pytest.approx(0.09)
    assert bk["trade_start_equity"] == pytest.approx(0.09) and bk["deposits"][-1]["before"]["bal"] == pytest.approx(0.05)


def test_payout_window_is_not_capital():
    e = K.new_bank("ETH", 0.05, 0.05)
    e["window"] = 2
    assert K.recon_token(e, 0.06, 2200.0, 1.5, False, "own")["action"] == "payout"
    assert e["hwm"] == 0.05


def test_busy_defers():
    e = K.new_bank("ETH", 0.05, 0.05)
    assert K.recon_token(e, 0.09, 2200.0, 1.5, True, "own")["action"] == "wait" and e["bal"] == 0.05
