"""First-entry sizing scales with the wallet (entry.first_collateral_*), ladder after a bigger first entry, and the
scaled withdraw/stake minimum. Pure functions only; conftest.py blocks every chain call."""
import pytest

from config import load_config
from dca_levels import Levels, _SimPos, ladder_budgets, ladder_rungs, project_ladder
from live.withdraw import run_withdraw
from strategy import DcaStrategy

PRICE = 115.70
RESERVE = 0.02
LV = Levels(supports=(115.44, 112.99, 112.48), resistances=(117.18, 119.05, 119.85), atr=0.835)


def free_usd(wallet_sol: float) -> float:   # bot.ChainAccount.cash: native SOL minus the 0.02 reserve, in USD
    return (wallet_sol - RESERVE) * PRICE


@pytest.fixture(scope="module")
def strat():
    return DcaStrategy(load_config("configs/live_100.toml"))


def test_live_config_keys():
    c = load_config("configs/live_100.toml")
    assert c.entry.first_collateral_mode == "pct" and c.entry.first_collateral_pct == 0.10
    assert c.entry.first_collateral_min_usd == 10.50 and c.entry.first_collateral_max_usd is None
    assert c.withdraw.min_send_sol == 0.01 and c.withdraw.min_stake_fee_mult == 3.0 and c.withdraw.min_stake_floor_sol == 0.0005


def test_floor_at_current_96_wallet(strat):
    cash = free_usd(0.831)                      # ~$96 wallet -> ~$93.8 free -> 10% = $9.38 < floor
    assert 9.0 < 0.10 * cash < 10.5
    assert strat.initial_margin(cash) == 10.50


def test_500_wallet_is_50(strat):
    assert strat.initial_margin(500.0) == pytest.approx(50.0)
    assert strat.initial_margin(free_usd(500 / PRICE)) == pytest.approx(0.10 * free_usd(500 / PRICE), abs=0.01)


def test_cap_and_never_above_free(strat):
    c = load_config("configs/live_100.toml", ["entry.first_collateral_max_usd=40"])
    assert DcaStrategy(c).initial_margin(1000.0) == 40.0
    assert strat.initial_margin(10.30) == pytest.approx(10.20)   # floor clipped to free - fee reserve (guard then skips < $10)
    with pytest.raises(ValueError):
        load_config("configs/live_100.toml", ["entry.first_collateral_max_usd=5"])


def test_fixed_mode_keeps_old_behaviour():
    c = load_config("configs/live_100.toml", ["entry.first_collateral_mode=fixed"])
    s = DcaStrategy(c)
    for cash in (50.0, 94.0, 300.0, 5000.0):
        assert s.initial_margin(cash) == 10.50
    assert DcaStrategy(load_config("configs/default.toml")).initial_margin(1000.0) == 100.0   # backtest default untouched


@pytest.mark.parametrize("wallet_usd", [96.0, 300.0, 500.0, 2000.0])
def test_ladder_after_bigger_first_entry(strat, wallet_usd):
    kw = strat.plan_kwargs()
    cash = free_usd(wallet_usd / PRICE)
    first = strat.initial_margin(cash)
    lev = strat.cfg.dca.lev_start
    open_fee = strat.cfg.entry.fee_reserve_usd   # network/request fee of the open, paid from the wallet
    left = cash - first - open_fee               # what the wallet re-read shows after the open
    pos = _SimPos(side=1, size=first * lev, qty=first * lev / PRICE, margin=first * (1 - lev * 0.0006), borrow=0.0,
                  last_entry=PRICE, last_leverage=lev, first_entry=PRICE)
    lp = kw["ladder"]
    rungs = ladder_rungs(1, PRICE, LV, last_leverage=lev, lev_step=kw["lev_step"], lev_cap=kw["lev_cap"],
                         min_gap_atr=kw["min_gap_atr"], max_gap_atr=kw["max_gap_atr"], fixed_dist=kw["fixed_dist"],
                         lp=lp, first_entry=PRICE)
    budgets = ladder_budgets(rungs, left, lp)
    # ladder budgets from the REMAINING free collateral, net of per-rung fees
    assert sum(r.budget for r in budgets) == pytest.approx(left - lp.fee_usd_per_add * len(rungs))
    assert all(b.budget >= a.budget for a, b in zip(budgets, budgets[1:-1]))   # deeper rungs larger
    plans = project_ladder(pos, LV, available_usd=left, **kw)
    assert plans, "ladder must still produce adds after a bigger first entry"
    spent = first + sum(p.margin for p in plans) + lp.fee_usd_per_add * len(plans) + open_fee
    assert spent <= cash + 1e-6                  # first entry + ladder + fees never exceed free collateral (reserve excluded)
    for p in plans:                              # liquidation buffer check unchanged: liq beyond the next rung
        assert p.next_level - p.liq > kw["liq_buffer_atr"] * LV.atr


def test_bigger_first_entry_shrinks_ladder_by_its_size(strat):
    kw = strat.plan_kwargs()
    cash = free_usd(500 / PRICE)
    lp = kw["ladder"]
    rungs = ladder_rungs(1, PRICE, LV, last_leverage=5, lev_step=2, lev_cap=30, min_gap_atr=0.5, max_gap_atr=2.0,
                         fixed_dist=0.85, lp=lp, first_entry=PRICE)
    old = sum(r.budget for r in ladder_budgets(rungs, cash - 10.5, lp))
    new = sum(r.budget for r in ladder_budgets(rungs, cash - strat.initial_margin(cash), lp))
    assert old - new == pytest.approx(strat.initial_margin(cash) - 10.5)


def test_stake_minimum_from_fee_and_slippage():
    w = load_config("configs/live_100.toml").withdraw
    assert w.action == "stake_jupsol" and w.min_stake_fee_mult == 3.0
    # 3 x (0.00001 + a x 0.5%) -> a > 0.0000305 SOL, so the 0.0005 SOL floor rules (the old 0.01 is gone for staking)
    assert w.min_amount_sol() == pytest.approx(0.0005)
    big_fee = load_config("configs/live_100.toml", ["withdraw.stake_fee_est_sol=0.001"]).withdraw
    assert big_fee.min_amount_sol() == pytest.approx(3 * 0.001 / (1 - 3 * 0.005))   # fee-driven above the floor
    a = big_fee.min_amount_sol()
    assert a == pytest.approx(3 * (0.001 + a * 0.005))                                # exactly 3x the swap cost
    mult5 = load_config("configs/live_100.toml", ["withdraw.stake_fee_est_sol=0.001", "withdraw.min_stake_fee_mult=5"])
    assert mult5.withdraw.min_amount_sol() > a                                        # tweakable
    send = load_config("configs/live_100.toml", ["withdraw.mode=send"]).withdraw
    assert send.min_amount_sol() == 0.01                                              # send mode unchanged
    with pytest.raises(ValueError):
        load_config("configs/live_100.toml", ["withdraw.min_stake_fee_mult=0.5"])


def test_stake_run_stakes_all_small_profit_and_carries_dust():
    from datetime import datetime, timezone
    cfg = load_config("configs/live_100.toml")             # stake_jupsol; stake is stubbed (never the chain)
    chain = {"wallet": {"sol": 0.83}, "positions": {"long": {}, "short": {}}, "openRequests": []}
    now = datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)   # 01:00 MT, after 00:05
    staked: list[float] = []
    stub = lambda a: staked.append(a) or {"ok": True, "sig": None}   # dry: no sig -> not booked as sent
    rows = [{"close_time": "2026-10-07 20:00:00+00:00", "net_sol": "0.0013", "net_sol_est": ""}]
    st: dict = {}
    rec = run_withdraw(cfg, st, chain, now, rows=rows, stake=stub)
    assert rec["min_amount_sol"] == pytest.approx(0.0005) and rec["amount"] == pytest.approx(0.0013)
    assert staked == [pytest.approx(0.0013)]               # the 0.0013 profit is staked (was carried under 0.01)
    dust = [{"close_time": "2026-10-07 20:00:00+00:00", "net_sol": "0.0004", "net_sol_est": ""}]
    st2: dict = {}
    rec2 = run_withdraw(cfg, st2, chain, now, rows=dust, stake=stub)
    assert rec2["amount"] == 0.0 and st2["withdraw_carry_sol"] == pytest.approx(0.0004) and len(staked) == 1


def test_stake_runs_with_open_trade_ignoring_next_add():
    from live.withdraw import withdraw_amount_sol as wa
    # wallet 0.10, reserve 0.02, buffer 0.003 -> fits 0.077; next add 0.05 SOL no longer blocks (require_next_add_cover=false)
    assert wa(0.03, 0.10, 0.02, 0.0005, 0.05, 0, 0.003, require_next_add_cover=False, allow_partial=True) == pytest.approx(0.03)
    assert wa(0.03, 0.10, 0.02, 0.0005, 0.05, 0, 0.003) == 0.0                     # old rule (flag true) still blocks
    # would breach reserve + buffer: stake what fits (0.027), the rest is carried by run_withdraw
    assert wa(0.05, 0.05, 0.02, 0.0005, 0.0, 0, 0.003, require_next_add_cover=False, allow_partial=True) == pytest.approx(0.027)
    assert wa(0.05, 0.0232, 0.02, 0.0005, 0.0, 0, 0.003, require_next_add_cover=False, allow_partial=True) == 0.0  # fits < min
    assert wa(0.05, 1.0, 0.02, 0.0005, 0.0, 1, 0.003, require_next_add_cover=False, allow_partial=True) == 0.0     # request in flight


def test_stake_run_partial_with_position_open_carries_rest():
    from datetime import datetime, timezone
    cfg = load_config("configs/live_100.toml")
    assert cfg.withdraw.require_next_add_cover is False
    chain = {"wallet": {"sol": 0.033}, "positions": {"long": {"sizeUsd": 47.25}, "short": {}}, "openRequests": [
        {"requestType": "trigger"}]}                       # open long with its TP triggers resting
    now = datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)
    staked: list[float] = []
    stub = lambda a: staked.append(a) or {"ok": True, "sig": "K", "jupsol_out": a / 1.2}
    cfg_live = load_config("configs/live_100.toml", ["live.dry_run=false"])
    st: dict = {}
    rows = [{"close_time": "2026-10-07 20:00:00+00:00", "net_sol": "0.02", "net_sol_est": ""}]
    rec = run_withdraw(cfg_live, st, chain, now, next_add_sol=0.05, rows=rows, stake=stub)
    # fits = 0.033 - 0.02 reserve - 0.003 buffer = 0.010; next add (0.05) ignored; 0.01 of 0.02 staked, 0.01 carried
    assert rec["position_open"] and staked == [pytest.approx(0.010)] and rec["staked_sol"] == pytest.approx(0.010)
    assert st["withdraw_carry_sol"] == pytest.approx(0.010)
    # send mode unchanged: all-or-nothing and the next add must be covered
    st2: dict = {}
    sent: list[float] = []
    rec2 = run_withdraw(load_config("configs/live_100.toml", ["withdraw.mode=send", "live.dry_run=false"]), st2,
                        {**chain, "wallet": {"sol": 0.5}}, now, next_add_sol=0.48, rows=rows,
                        send=lambda a: sent.append(a) or {"ok": True})
    assert rec2["amount"] == 0.0 and not sent and st2["withdraw_carry_sol"] == pytest.approx(0.02)
