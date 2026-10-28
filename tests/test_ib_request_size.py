"""2026-10-25: inter-bot loan request sized from the next rung's normal collateral (never a cash + 1e9 probe), capped at
NO cap (Trevor 2026-10-15); an own stale request is withdrawn when the borrower goes flat or the next rung is affordable."""
import json

import pandas as pd
import pytest

from live import bank as bankmod
import live.bot as B
from tests.test_resting import ON, mkbot, state

LIVE_RUNG = ["dca.min_add_usd=10", "dca.ladder_fixed_usd=10"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    f = tmp_path / "ib.json"
    monkeypatch.setattr(B.Bot, "_ib_file", lambda self: f)
    monkeypatch.setattr(bankmod, "journal_lock", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(B.Bot, "bank_on", property(lambda s: True))

    def mk(asset="SOL", flags=LIVE_RUNG):
        b = mkbot(monkeypatch, tmp_path, ON + flags)
        b.asset = asset
        b.dir = tmp_path   # never the real live/ dir (a live/IB_BORROW_OFF test switch would leak in)
        b.local.bank = {"coin": "SOL", "bal": 0.1, "hwm": 0.1, "wd_mark": 0.1, "hist": [], "totals": {}}
        b.bank_fee = lambda *a: None
        b._busy = lambda *a: False
        b.local.last_leverage = 5.0   # next rung 6x: the fixture ladder passes the liq rule with a $10 rung
        return b
    return f, mk


def test_bank_helpers_sane():
    assert bankmod.ib_need_usd(10.0, 1.5) == 9.5           # shortfall + $1
    assert bankmod.ib_need_usd(10.0, 9.8) == bankmod.IB_MIN_USD
    assert bankmod.ib_need_usd(500.0, 0.0) == 501.0        # no cap
    assert not hasattr(bankmod, "IB_MAX_RUNGS") and not hasattr(bankmod, "ib_cap_usd") and not hasattr(B.Bot, "ib_cap_usd")


@pytest.mark.parametrize("cash", [0.0, 2.0, 6.5, 9.0])
def test_request_from_next_rung_margin_not_millions(env, cash):
    f, mk = env
    bot = mk()   # live sizing: flat $10 rungs
    seen = []
    real = bot.strategy.plan_next_add
    bot.strategy.plan_next_add = lambda p, avail, now: (seen.append(avail), real(p, avail, now))[1]
    plan, why = bot.rest_add_plan(bot.chain_position(state()), cash)
    assert plan is None and "no ladder plan" in why
    assert max(seen) <= cash + bankmod.IB_PROBE_STEPS * bot.ib_rung_usd() + 1e-9 < 1e6   # never a 1e9 probe
    rec = json.loads(f.read_text())
    assert rec["status"] == "requested" and rec["borrower"] == "SOL"
    assert rec["need_usd"] == round(max(bankmod.IB_MIN_USD, 10.0 - cash + 1.0), 2)   # $10 rung - cash + $1


def test_budget_scaled_ladder_old_probe_absurd_new_request_sane(env):
    f, mk = env
    bot = mk(flags=[])   # pre-C accel ladder: rung collateral scales with the budget ($3 floor)
    pos = bot.chain_position(state())
    assert bot.strategy.plan_next_add(pos, 1.0 + 1e9, pd.Timestamp.now(tz="UTC")).margin > 1e8   # the old probe
    plan, _ = bot.rest_add_plan(pos, 1.0)
    assert plan is None
    rec = json.loads(f.read_text())
    assert bankmod.IB_MIN_USD <= rec["need_usd"] < 10.0   # smallest probe with a plan ($6.90), not 1e9


def test_no_cap_request_in_full(env):
    f, mk = env
    bot = mk()
    bot.ib_request(500.0)
    assert json.loads(f.read_text())["need_usd"] == 500.0


@pytest.mark.parametrize("rung", [100.0, 250.0])
def test_large_real_rung_shortfall_requested_in_full(env, rung):
    f, mk = env
    bot = mk(flags=[f"dca.min_add_usd={rung}", f"dca.ladder_fixed_usd={rung}"])
    plan, _ = bot.rest_add_plan(bot.chain_position(state()), 2.0)
    assert plan is None
    rec = json.loads(f.read_text())
    assert rec["need_usd"] == round(rung - 2.0 + 1.0, 2)   # whole rung shortfall, no cap


def test_rung_found_beyond_first_steps(env, monkeypatch):
    f, mk = env
    bot = mk()
    real = bot.strategy.plan_next_add
    # a real next rung that only plans with >= $120 free (12 probe steps of $10): found and requested in full
    bot.strategy.plan_next_add = lambda p, avail, now: real(p, avail, now) if avail >= 120.0 else None
    plan, _ = bot.rest_add_plan(bot.chain_position(state()), 2.0)
    assert plan is None and json.loads(f.read_text())["need_usd"] == 9.0


def test_borrow_false_never_requests(env):
    f, mk = env
    bot = mk()
    bot.rest_add_plan(bot.chain_position(state()), 2.0, borrow=False)
    assert not f.exists()


def test_flat_borrower_cancels_own_request_only(env):
    f, mk = env
    btc = mk("BTC")
    f.write_text(json.dumps({"status": "requested", "borrower": "BTC", "coin_name": "WBTC", "need_usd": 180893111.76}))
    btc.ib_tick(state(), btc.chain_position(state()))   # in a position: request stays
    assert json.loads(f.read_text())["status"] == "requested"
    btc.ib_tick(state(size=0, reqs=[]), None)            # flat: withdrawn
    r = json.loads(f.read_text())
    assert r["status"] == "settled" and r["cancelled_need_usd"] == 180893111.76


def test_cancel_request_when_affordable(env):
    f, mk = env
    sol = mk("SOL")
    f.write_text(json.dumps({"status": "requested", "borrower": "ETH", "coin_name": "ETH", "need_usd": 9.0}))
    sol.ib_cancel_request("x")                        # another bot's request: untouched
    assert json.loads(f.read_text())["status"] == "requested"
    f.write_text(json.dumps({"status": "requested", "borrower": "SOL", "coin_name": "SOL", "need_usd": 9.0}))
    sol.ib_cancel_request("next rung affordable without a loan")
    assert json.loads(f.read_text())["status"] == "settled"
    for s in ("funding", "funded", "open"):           # in-flight / open loans are never touched
        f.write_text(json.dumps({"status": s, "borrower": "SOL", "coin_name": "SOL", "need_usd": 9.0}))
        sol.ib_cancel_request("x")
        assert json.loads(f.read_text())["status"] == s
    f.write_text(json.dumps({"status": "topup_requested", "borrower": "SOL", "topup": True, "need_usd": 9.0,
                             "topup_prev": {"sol": 0.02, "coin": 0.02, "need_usd": 2.13}}))
    sol.ib_cancel_request("x")
    r = json.loads(f.read_text())
    assert r["status"] == "open" and r["need_usd"] == 2.13 and "topup" not in r


def test_place_rest_add_with_plan_withdraws_request(env):
    f, mk = env
    sol = mk("SOL")
    f.write_text(json.dumps({"status": "requested", "borrower": "SOL", "coin_name": "SOL", "need_usd": 9.0}))
    st = state()
    pos = sol.chain_position(st)
    plan, why = sol.rest_add_plan(pos, 60.0)
    assert plan is not None, why
    sol._own_atr = 0.9
    import types
    sol.place_rest_add(pos, st, 106.6, types.SimpleNamespace(cash=60.0), 100.0)
    assert json.loads(f.read_text())["status"] == "settled"
