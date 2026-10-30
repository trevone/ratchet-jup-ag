"""Trevor 2026-10-30 07:15 MT: balance = own token on chain; HWM only bookkeeping: flat and token > HWM -> HWM = token."""
import dataclasses

import pytest

import live.bank as K
from config import load_config
from tests.test_bank import _st


def _bot(monkeypatch, tmp_path, path, asset):
    import live.bot as B
    monkeypatch.setattr(B.Bot, "_peer_fee_sol", lambda self: [0.0, 0.0])   # no ETH/BTC state files in tests
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    ov = ["live.dry_run=true"]
    if asset != "SOL":
        monkeypatch.setattr(B, "ROOT", tmp_path.parent)
        ov.append(f"bot.state_dir={tmp_path.name}")
    b = B.Bot(load_config(path, ov))
    b.live = dataclasses.replace(b.live, dry_run=False)
    return b


@pytest.mark.parametrize("p", ["configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml"])
def test_live_configs_chain(p):
    assert load_config(p).ledger.ext_flow_mode == "chain"


def test_sol_deposit_usable_next_tick_and_hwm_follows_when_flat(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.957, 1.174)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.977 + 0.9375                   # ~$90 SOL arrives
    b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.957 + 0.9375)        # wallet - 0.02 reserve, at once
    assert bk["hwm"] == pytest.approx(0.957 + 0.9375)        # flat and token > HWM -> HWM = token
    assert K.load_journal()["events"] == []                  # never split


def test_hwm_never_moves_down(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.957, 1.174)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.80
    b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.78) and b.local.bank["hwm"] == pytest.approx(1.174)


def test_no_sync_while_request_in_flight(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.957, 1.174)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.50
    st["openRequests"] = st["allOpenRequests"] = [{"requestType": "market"}]
    b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.957)


def test_hwm_not_raised_in_a_trade(monkeypatch, tmp_path):
    import live.bank as KK
    bk = K.new_bank("SOL", 2.0, 1.174)
    assert KK.update_hwm(bk, False) is False and bk["hwm"] == 1.174


def test_eth_balance_is_wallet_eth(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_eth.toml", "ETH")
    b.local.bank = K.new_bank("ETH", 0.05, 0.0528)
    st = _st(asset="ETH", size=0)
    coin = b.local.bank["coin"]
    st.setdefault("sweepTokens", {})[coin] = {"atoms": int(0.09 * 10 ** K.DECIMALS[coin])}
    b.bank_tick(st, None, 2200.0)
    assert b.local.bank["bal"] == pytest.approx(0.09) and b.local.bank["hwm"] >= 0.09 - 1e-9



def _flat_sol(monkeypatch, tmp_path):
    b = _bot(monkeypatch, tmp_path, "configs/live_100.toml", "SOL")
    b.local.bank = K.new_bank("SOL", 0.957, 1.174)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.977
    return b, st


def test_first_sync_after_deploy_is_baseline_only(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    st["wallet"]["sol"] = 0.977 + 0.5                      # gap already present at deploy
    b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["wd_mark"] == pytest.approx(1.174)           # never bumped by the pre-existing gap
    assert bk["chain_idle"] is True


def test_own_token_deposit_while_idle_raises_wd_mark_and_hwm(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    b.local.bank["hwm"] = b.local.bank["wd_mark"] = 0.957
    b.bank_tick(st, None, 96.0)                            # baseline
    st["wallet"]["sol"] = 0.977 + 0.9375                    # ~$90 arrives
    b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.957 + 0.9375)
    assert bk["hwm"] == pytest.approx(0.957 + 0.9375)
    assert bk["wd_mark"] == pytest.approx(0.957 + 0.9375)
    assert K.withdraw_amount(bk, 0.9, 0.0)[0] == pytest.approx(0.0, abs=1e-9)   # not withdrawn tonight


def test_payout_after_close_is_profit_not_deposit(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    b.local.bank["hwm"] = b.local.bank["wd_mark"] = 0.957
    b.bank_tick(st, None, 96.0)                            # idle baseline
    b.local.bank["window"] = 3                             # close happened: payout window open
    st["wallet"]["sol"] = 0.977 + 0.05                     # profit lands
    for _ in range(4):
        b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["wd_mark"] == pytest.approx(0.957)           # profit stays withdrawable
    assert bk["hwm"] == pytest.approx(1.007)


def test_small_increase_is_float(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    b.local.bank["hwm"] = b.local.bank["wd_mark"] = 0.957
    b.bank_tick(st, None, 96.0)
    st["wallet"]["sol"] = 0.977 + 0.005                    # ~$0.48 < ext_min_usd
    b.bank_tick(st, None, 96.0)
    assert b.local.bank["wd_mark"] == pytest.approx(0.957)


def test_in_position_increase_never_bumps(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    b.bank_tick(st, None, 96.0)
    st["wallet"]["sol"] = 0.977 + 0.5
    b.local.bank["window"] = 3                            # payout of the trade (window open)
    b.bank_tick(st, object(), 96.0)                        # a position is open
    assert b.local.bank["wd_mark"] == pytest.approx(1.174)


def test_sol_other_bots_request_rent_not_counted_as_loss(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    st["allOpenRequests"] = [{"requestType": "trigger"}] * 4          # ETH/BTC TPs hold rent from the shared wallet
    st["openRequests"] = []
    st["wallet"]["sol"] = 0.977 - 4 * K.REQ_RENT
    b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.957)


def test_sol_peer_fee_debt_is_own_funds_and_repay_is_not_a_deposit(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    b.local.bank["hwm"] = b.local.bank["wd_mark"] = 0.957
    debt = [0.01, 0.005]
    monkeypatch.setattr(b, "_peer_fee_sol", lambda: list(debt))
    b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.972)
    b.bank_tick(st, None, 96.0)
    debt[:] = [0.0, 0.0]                                   # ETH / BTC repay: wallet up, debt down
    st["wallet"]["sol"] = 0.977 + 0.015
    b.bank_tick(st, None, 96.0)
    assert bk["bal"] == pytest.approx(0.972)
    assert bk["wd_mark"] == pytest.approx(0.957)


def test_formula_change_rebaselines_once(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    b.local.bank["hwm"] = b.local.bank["wd_mark"] = 0.957
    b.local.bank["chain_idle"] = True                      # state from the previous version (no chain_v)
    monkeypatch.setattr(b, "_peer_fee_sol", lambda: [0.3, 0.0])
    b.bank_tick(st, None, 96.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(1.257) and bk["wd_mark"] == pytest.approx(0.957)


def test_peer_fee_unreadable_skips_sync(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    def boom():
        raise OSError("x")
    monkeypatch.setattr(b, "_peer_fee_sol", boom)
    st["wallet"]["sol"] = 0.5
    b.bank_tick(st, None, 96.0)
    assert b.local.bank["bal"] == pytest.approx(0.957)



def test_midtrade_deposit_is_capital_not_profit(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["mig_dep_20261030"] = True
    bk["hwm"], bk["wd_mark"], bk["trade_start_equity"] = 1.174, 1.1678, 1.1755
    pos = object()
    b.bank_tick(st, pos, 96.0)                             # quiet baseline in the trade
    st["wallet"]["sol"] = 0.977 + 0.959212932
    b.bank_tick(st, pos, 96.0)
    assert bk["hwm"] == pytest.approx(1.174 + 0.959212932)
    assert bk["wd_mark"] == pytest.approx(1.1678 + 0.959212932)
    assert bk["trade_start_equity"] == pytest.approx(1.1755 + 0.959212932)


def test_midtrade_payout_window_is_not_a_deposit(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["mig_dep_20261030"] = True
    pos = object()
    b.bank_tick(st, pos, 96.0)
    bk["window"] = 3
    st["wallet"]["sol"] = 0.977 + 0.06
    b.bank_tick(st, pos, 96.0)
    assert bk["hwm"] == pytest.approx(1.174) and bk["wd_mark"] == pytest.approx(1.174)


def test_one_off_migration_applies_once(monkeypatch, tmp_path):
    b, st = _flat_sol(monkeypatch, tmp_path)
    bk = b.local.bank
    bk["hwm"], bk["wd_mark"], bk["trade_start_equity"] = 1.174001999, 1.167854843, 1.1755
    pos = object()
    b.bank_tick(st, pos, 96.0)
    b.bank_tick(st, pos, 96.0)
    assert bk["hwm"] == pytest.approx(1.174001999 + 0.959212932)
    assert bk["wd_mark"] == pytest.approx(1.167854843 + 0.959212932)
    assert bk["trade_start_equity"] == pytest.approx(1.1755 + 0.959212932)
