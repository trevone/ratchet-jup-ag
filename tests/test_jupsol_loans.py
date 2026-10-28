"""Automatic JupSOL loans (Trevor 2026-10-27): decisions 1-5, the 5 bug fixes, modes off / observe / on, the peer-borrow
test switch, the park tool, and the invariant: a loan / repayment leaves equity, hwm, wd_mark, the guard target and the
withdrawable amount unchanged. Every swap / CLI call is faked (conftest blocks the chain)."""
import json
import time

import pytest

from config import load_config
from live import bank as K
from live import jupsol_loans as JL
from live import ledger
from live import rescue as R
from live import sweep as sweepmod
import live.bot as B
from tests.test_resting import ON, mkbot, state

LIVE_RUNG = ["dca.min_add_usd=10", "dca.ladder_fixed_usd=10"]
PX = 106.6                         # SOL price of the fixture state
LAM = 1_000_000_000
C = dict(R.DEFAULTS)


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def env(monkeypatch, tmp_path):
    f = tmp_path / "ib.json"
    monkeypatch.setattr(B.Bot, "_ib_file", lambda self: f)
    monkeypatch.setattr(K, "journal_lock", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(B.Bot, "bank_on", property(lambda s: True))
    monkeypatch.setattr(JL, "rcfg", lambda: dict(R.DEFAULTS))
    peers = {}
    monkeypatch.setattr(JL, "_peer_local", lambda a: peers.get(a))
    calls = {"swap": [], "jup": []}
    jres = {"stake-state": {"ok": True, "jupsol": 1.0, "jupsolAtoms": str(LAM), "solPerJupsol": 1.25}}

    def fake_jup(cmd, args):
        calls["jup"].append((cmd, args))
        r = jres.get(cmd)
        return r(args) if callable(r) else (r or {"ok": False, "error": "not faked"})
    monkeypatch.setattr(B, "jup", fake_jup)

    def mk(asset="SOL", bal=0.02, mode="on", flags=()):
        b = mkbot(monkeypatch, tmp_path, ON + LIVE_RUNG + [f"ledger.jupsol_loans={mode}", *flags])
        b.asset = asset
        b.dir = tmp_path / asset.lower()
        b.dir.mkdir(exist_ok=True)
        b.kill_all = tmp_path / "KILL_ALL"
        b.local.bank = K.new_bank(asset, bal, bal)
        b.local.last_leverage = 5.0
        b.local.day_start_equity = 100.0
        b.local.flat_since = None
        fees = []
        b.fees = fees
        b.bank_fee = lambda x: fees.append(x)
        b._busy = lambda *a: False
        b._sol_px, b._mkt_px = PX, PX
        return b
    return {"ib": f, "mk": mk, "peers": peers, "calls": calls, "jres": jres, "mp": monkeypatch}


def fake_swap(env, res_fn):
    def sw(cfg, plan, jup, out_mint=None, swap_mode=None, **k):
        env["calls"]["swap"].append({"in": plan["mint"], "atoms": plan["atoms"], "out": out_mint, "mode": swap_mode})
        r = res_fn(plan, out_mint, swap_mode)
        return r, sweepmod.classify(r), ["api.jup.ag:" + sweepmod.classify(r)]
    env["mp"].setattr(sweepmod, "swap_with_backoff", sw)


def st_with(price=PX, jupsol=1.0, borrow=0.0, **kw):
    s = state(price=price, **kw)
    s["solOracle"] = {"price": price}
    s["stake"] = {"jupsol": jupsol, "jupsolAtoms": str(int(jupsol * LAM)), "solPerJupsol": 1.25}
    if s["positions"]["long"]:
        s["positions"]["long"]["borrowFeeUsd"] = borrow
    return s


def lend_res(sol_got, js_spent, fee=5000, sig="LEND"):
    """Confirmed JupSOL -> SOL swap with measured tx deltas (nativeDelta is net of the network fee)."""
    return {"ok": True, "sig": sig, "inAmount": str(int(js_spent * LAM * 0.9)), "quotedOut": "1",
            "deltas": {"feeLamports": fee, "nativeDeltaLamports": int(round(sol_got * LAM)), "rentDeltaLamports": 0,
                       "wsolDeltaAtoms": 0, "jupsolDeltaAtoms": str(-int(round(js_spent * LAM))), "tokenDeltaAtoms": {}}}


def repay_res(sol_spent, js_got, fee=5000, sig="REPAY"):
    return {"ok": True, "sig": sig, "inAmount": "1", "quotedOut": "1",
            "deltas": {"feeLamports": fee, "nativeDeltaLamports": -int(round(sol_spent * LAM)), "rentDeltaLamports": 0,
                       "wsolDeltaAtoms": 0, "jupsolDeltaAtoms": str(int(round(js_got * LAM))), "tokenDeltaAtoms": {}}}


def inv(b, close=0.3, frac=0.5):
    """equity, hwm, wd_mark, guard target (bank.equity at the guard's close value) and the withdrawable amount."""
    return (round(K.equity(b, close), 9), b["hwm"], b["wd_mark"], round(K.equity(b, 0.0), 9),
            K.withdraw_amount({**b, "wd_mark": b["wd_mark"] - 0.01}, frac, 0.0))


# ------------------------------------------------------------------ config flag
def test_config_default_off_everywhere_and_validated():
    from config import LedgerConfig
    assert LedgerConfig().jupsol_loans == "off"
    for p in ("configs/live_eth.toml", "configs/live_btc.toml"):
        assert load_config(p).ledger.jupsol_loans == "off"
    assert load_config("configs/live_100.toml").ledger.jupsol_loans in ("off", "observe", "on")   # SOL: live test 2026-10-28
    with pytest.raises(Exception):
        load_config("configs/live_100.toml", ["ledger.jupsol_loans=yes"])


def test_rescue_cfg_has_no_removed_keys():
    c = R.rcfg()
    for k in ("collateral_share", "repay_margin_pct", "repay_profit_share", "emergency_min_collat_pct"):
        assert k not in c and k not in R.DEFAULTS
    assert (c["trigger_pct"], c["target_pct"], c["haircut_pct"], c["emergency_min_push_pct"]) == (3.0, 10.0, 1.0, 2.0)


# ------------------------------------------------------------------ decision 2: rescue maths
def test_rescue_plan_collateral_only_capped_at_one_third_after_haircut():
    # long $581 @ 109.88 with $44.72 collateral at 105.0: liq ~102.4 -> 2.4 % away (in trigger)
    p = JL.rescue_plan(1, 581.0, 109.882318, 44.719152, 0.0, 105.0, 30.0, C)
    assert p["in_trigger"] and p["dist_pct"] < 3.0
    assert p["cap_usd"] == pytest.approx(30.0 * 0.99 / 3)                    # 1/3 of the JupSOL after the 1 % haircut
    assert p["capped"] and p["collateral_usd"] == pytest.approx(p["cap_usd"])  # needs more than the cap
    assert "credit_usd" not in p and "loan_usd" not in p                      # no bank credit split
    big = JL.rescue_plan(1, 581.0, 109.882318, 44.719152, 0.0, 105.0, 10_000.0, C)
    assert not big["capped"] and big["collateral_usd"] == pytest.approx(big["need_usd"])
    assert big["new_dist_pct"] == pytest.approx(10.0, abs=1e-3)             # exactly to the 10 % target


def test_rescue_plan_borrow_fee_counts_and_emergency():
    a = JL.rescue_plan(1, 581.0, 109.882318, 44.719152, 0.0, 106.0, 300.0, C)
    b = JL.rescue_plan(1, 581.0, 109.882318, 44.719152, 6.0, 106.0, 300.0, C)
    assert not a["in_trigger"] and b["in_trigger"]                          # the accrued borrow fee pushes it inside 3 %
    assert b["eff_margin"] == pytest.approx(44.719152 - 6.0)
    e = JL.rescue_plan(1, 581.0, 109.882318, 44.719152, 0.0, 105.0, 3.0, C)  # $3 of JupSOL: $0.99 moves liq < 2 pts
    assert e["emergency"] and e["push_pts"] < C["emergency_min_push_pct"]
    assert R.plan(1, 581.0, 109.882318, 44.719152, 105.0, 3.0, C)["emergency"]   # manual tool: same maths
    s = JL.rescue_plan(-1, 100.0, 100.0, 10.0, 0.0, 107.0, 50.0, C)          # short: liq 109
    assert s["liq"] == pytest.approx(109.0) and s["new_liq"] > 109.0 and s["in_trigger"]


def test_next_add_need_exact_shortfall_plus_one_dollar():
    assert JL.next_add_need_usd(10.0, 2.0) == pytest.approx(9.0)
    assert JL.next_add_need_usd(10.0, 9.9) == pytest.approx(1.1)            # no $2 minimum (inter-bot keeps its own)
    assert JL.next_add_need_usd(500.0, 0.0) == pytest.approx(501.0)          # no cap


def test_usable_jupsol_excludes_keep_and_parked():
    assert JL.usable_jupsol(1.0, 0.1, 0.3) == pytest.approx(0.6)
    assert JL.usable_jupsol(0.2, 0.1, 0.3) == 0.0


# ------------------------------------------------------------------ decision 1 + bug 1 (partial write-off)
def test_exact_jupsol_repay_and_invariants():
    b = K.new_bank("SOL", 1.0, 1.2)
    b["wd_mark"] = 1.0
    before = inv(b)
    K.loan_open(b, 0.5, 0.42, why="t", kind="rescue", sig="S")
    assert inv(b) == before                                   # borrow: nothing moves
    assert K.loan_jupsol_owed(b) == pytest.approx(0.42) and b["loan"]["tranches"][-1]["sig"] == "S"
    r = K.loan_repay_jupsol(b, 0.5, 0.42, "back")            # same price: costs exactly what came in
    assert r["closed"] and r["cost"] == pytest.approx(0.0) and "loan" not in b
    assert inv(b) == before                                   # repay: nothing moves either
    assert b["loans_done"][-1]["repaid_jupsol"] == pytest.approx(0.42)   # exactly the JupSOL lent, no 5 % extra


def test_partial_repay_keeps_the_rest_owed_no_write_off():
    b = K.new_bank("SOL", 1.0, 1.2)
    K.loan_open(b, 0.5, 0.40)
    r = K.loan_repay_jupsol(b, 0.25, 0.10)
    assert not r["closed"] and r["owed_jupsol"] == pytest.approx(0.30)
    assert K.loan_outstanding(b) == pytest.approx(0.375) and K.loan_jupsol_owed(b) == pytest.approx(0.30)
    K.loan_repay(b, 0.1)                                      # legacy coin amount: pro rata, still no write-off
    assert K.loan_outstanding(b) == pytest.approx(0.275) and K.loan_jupsol_owed(b) == pytest.approx(0.30 * 0.275 / 0.375)
    assert R.repay_due(b) == pytest.approx(K.loan_jupsol_owed(b))


# ------------------------------------------------------------------ bug 2: measured amounts, not quotes
def test_measured_amounts_from_tx_deltas_and_quote_fallback():
    got, js, m = JL.lend_measured(lend_res(0.08, 0.07), "SOL")
    assert (got, js, m) == (pytest.approx(0.08), pytest.approx(0.07), True)
    q = {"ok": True, "sig": "X", "inAmount": str(70_000_000), "quotedOut": str(81_000_000)}
    got, js, m = JL.lend_measured(q, "SOL")
    assert (got, js, m) == (pytest.approx(0.081), pytest.approx(0.07), False)
    eth = {"ok": True, "sig": "E", "deltas": {"jupsolDeltaAtoms": "-5000000", "tokenDeltaAtoms": {"ETH": "123456"}}}
    assert JL.lend_measured(eth, "ETH")[:2] == (pytest.approx(123456 / 10 ** K.DECIMALS["ETH"]), pytest.approx(0.005))
    sp, jg, m = JL.repay_measured(repay_res(0.081, 0.07), "SOL")
    assert (sp, jg, m) == (pytest.approx(0.081), pytest.approx(0.07), True)


# ------------------------------------------------------------------ bug 4: stake-ledger rows for the bucket
def test_stake_ledger_new_kinds_and_net():
    for kind, jd in (("loan_out", -0.07), ("loan_repay", 0.07), ("park", 0.2), ("unpark", -0.2)):
        tot = ledger.record_stake({"time": "t", "kind": kind, "source": "x", "jupsol_delta": jd})
    assert tot["lent_jupsol_total"] == pytest.approx(0.07) and tot["repaid_jupsol_total"] == pytest.approx(0.07)
    assert tot["parked_jupsol_total"] == pytest.approx(0.2) and tot["unparked_jupsol_total"] == pytest.approx(0.2)
    assert tot["net_jupsol"] == pytest.approx(0.0) and len(ledger.read_stake_rows()) == 4


# ------------------------------------------------------------------ decision 5 + modes + peer switch
def peer(bal, side=0, window=0, ib=None, coin="SOL"):
    b = {"coin": coin, "bal": bal, "hwm": bal, "wd_mark": bal, "window": window}
    if ib:
        b["ib_loan"] = ib
    return {"side": side, "bank": b}


def test_peers_can_cover_rules(env):
    bot = env["mk"]("SOL")
    bot._coin_usd = lambda coin, st=None: 2500.0
    env["peers"].update(ETH=peer(0.001, coin="ETH"), BTC=peer(0.0, coin="WBTC"))
    assert JL.peers_can_cover(bot, 9.0)[0] is False                         # $2.50 free < $9 + $5
    env["peers"]["ETH"] = peer(0.01, coin="ETH")                             # $25 free
    assert JL.peers_can_cover(bot, 9.0)[0] is True
    env["peers"]["ETH"] = peer(0.01, side=1, coin="ETH")                     # in a trade: cannot lend
    assert JL.peers_can_cover(bot, 9.0)[0] is False
    env["peers"]["ETH"] = peer(0.001, window=3, coin="ETH")                  # settling a payout: wait (counts as can)
    assert JL.peers_can_cover(bot, 9.0)[0] is True
    (bot.dir / JL.PEER_OFF_FILE).touch()
    assert JL.peers_can_cover(bot, 9.0) == (False, "peer borrowing switched off (IB_BORROW_OFF)")


def test_mode_off_files_the_interbot_request_exactly_as_today(env):
    bot = env["mk"]("SOL", mode="off")
    plan, why = bot.rest_add_plan(bot.chain_position(st_with()), 2.0)
    assert plan is None and json.loads(env["ib"].read_text())["need_usd"] == 9.0   # unchanged: $10 - $2 + $1
    assert bot._jl_want is None


def test_peer_switch_works_in_mode_off_and_cancels_own_request(env):
    bot = env["mk"]("SOL", mode="off")
    bot.rest_add_plan(bot.chain_position(st_with()), 2.0)
    assert json.loads(env["ib"].read_text())["status"] == "requested"
    (bot.dir / JL.PEER_OFF_FILE).touch()
    bot.rest_add_plan(bot.chain_position(st_with()), 2.0)
    assert json.loads(env["ib"].read_text()).get("status") != "requested"   # withdrawn, never re-filed
    assert bot._jl_want is None                                             # off: JupSOL never lends


def test_mode_on_peer_that_can_cover_goes_first(env):
    bot = env["mk"]("SOL", mode="on")
    bot._coin_usd = lambda coin, st=None: 2500.0
    env["peers"]["ETH"] = peer(0.01, coin="ETH")
    for _ in range(3):
        bot.rest_add_plan(bot.chain_position(st_with()), 2.0)
    assert json.loads(env["ib"].read_text())["status"] == "requested" and bot._jl_want is None


def test_mode_on_no_peer_two_ticks_then_jupsol(env):
    bot = env["mk"]("SOL", mode="on")
    pos = bot.chain_position(st_with())
    bot.rest_add_plan(pos, 2.0)
    assert bot._jl_want is None and json.loads(env["ib"].read_text())["status"] == "requested"   # 1st tick: as today
    bot.rest_add_plan(pos, 2.0)
    assert bot._jl_want["need_usd"] == pytest.approx(9.0)                    # 2nd tick: JupSOL will lend $10 - $2 + $1


def test_mode_observe_logs_only(env, caplog):
    bot = env["mk"]("SOL", mode="observe")
    (bot.dir / JL.PEER_OFF_FILE).touch()
    pos = bot.chain_position(st_with())
    for _ in range(3):
        bot.rest_add_plan(pos, 2.0)
    assert bot._jl_want is None and "JUPSOL observe: would lend $9.00" in caplog.text
    fake_swap(env, lambda *a: pytest.fail("observe must never swap"))
    st = st_with(price=105.0)
    caplog.clear()
    assert JL.tick(bot, st, bot.chain_position(st), 105.0) is False
    assert "would RESCUE" in caplog.text and env["calls"]["swap"] == [] and "loan" not in bot.local.bank


# ------------------------------------------------------------------ decision 4: rescue before next add
def test_rescue_wanted_blocks_next_add_loans(env):
    bot = env["mk"]("SOL", mode="on")
    (bot.dir / JL.PEER_OFF_FILE).touch()
    pos = bot.chain_position(st_with())
    bot.rest_add_plan(pos, 2.0); bot.rest_add_plan(pos, 2.0)
    assert bot._jl_want
    with JL.lock():
        r = JL.reg_read(); r["rescue_want"]["ETH"] = time.time(); JL.reg_write(r)
    fake_swap(env, lambda *a: pytest.fail("no next-add loan while a rescue is wanted"))
    assert JL.tick(bot, st_with(), pos, PX) is False and "loan" not in bot.local.bank


def test_kill_skips_rescue_and_logs(env, caplog):
    bot = env["mk"]("SOL", mode="on")
    bot.kill.touch()
    fake_swap(env, lambda *a: pytest.fail("KILL: no rescue"))
    st = st_with(price=105.0)
    JL.tick(bot, st, bot.chain_position(st), 105.0)
    assert "RESCUE skipped: KILL present" in caplog.text and "loan" not in bot.local.bank


# ------------------------------------------------------------------ bug 3: repay waits for pend / book_pend / trueup_block
@pytest.mark.parametrize("key,val", [("pend", {"d": 1}), ("book_pend", {"x": 1}), ("trueup_block", True), ("window", 2),
                                     ("jl_op", {"stage": "x"})])
def test_repay_gates(env, key, val):
    bot = env["mk"]("SOL", bal=0.5)
    K.loan_open(bot.local.bank, 0.1, 0.08)
    assert JL.repay_due_now(bot)
    bot.local.bank[key] = val
    assert not JL.repay_due_now(bot)


def test_repay_after_peer_loan(env):
    bot = env["mk"]("SOL", bal=0.5)
    K.loan_open(bot.local.bank, 0.1, 0.08)
    bot.local.bank["ib_loan"] = {"role": "borrower", "coin": 0.05, "sol": 0.05, "peer": "ETH"}
    assert not JL.repay_due_now(bot)                                          # peer first


def test_full_repay_exact_jupsol_exactout(env):
    bot = env["mk"]("SOL", bal=0.5)
    K.loan_open(bot.local.bank, 0.1, 0.08)
    before = inv(bot.local.bank)
    env["jres"]["swap-quote"] = {"ok": True, "inAmount": str(100_000_000), "otherAmountThreshold": str(101_000_000)}
    fake_swap(env, lambda plan, out, md: repay_res(0.1, plan["atoms"] / LAM))
    JL.repay_tick(bot, PX, PX)
    sw = env["calls"]["swap"][-1]
    assert sw["mode"] == "ExactOut" and sw["atoms"] == 80_000_000 and sw["out"] == K.MINT["JUPSOL"]   # exactly 0.08 JupSOL
    b = bot.local.bank
    assert "loan" not in b and b["bal"] == pytest.approx(0.5) and inv(b) == before
    assert b["window"] > 0 and bot.fees == [5000]                             # window + bank_fee booked
    row = ledger.read_stake_rows()[-1]
    assert row["kind"] == "loan_repay" and float(row["jupsol_delta"]) == pytest.approx(0.08)


def test_partial_repay_carried_to_next_flat_not_paid_by_deposits(env):
    bot = env["mk"]("SOL", bal=0.0)
    bot.local.flat_since = "F1"
    K.loan_open(bot.local.bank, 0.1, 0.08)
    bot.local.bank["bal"] = 0.06 + bot.cfg.ledger.close_network_native       # the trade lost: only 0.06 free
    env["jres"]["swap-quote"] = {"ok": True, "inAmount": str(100_000_000), "otherAmountThreshold": str(101_000_000)}
    fake_swap(env, lambda plan, out, md: repay_res(plan["atoms"] / LAM, 0.048))
    JL.repay_tick(bot, PX, PX)
    sw = env["calls"]["swap"][-1]
    assert sw["mode"] is None and sw["atoms"] == 60_000_000                   # ExactIn of what there is
    b = bot.local.bank
    assert K.loan_jupsol_owed(b) == pytest.approx(0.032) and b["loan"]["carry_flat"] == "F1"   # rest carried, not written off
    b["window"] = 0
    K.apply_external(b, 0.5, "deposit")                                       # a deposit while still flat
    assert not JL.repay_due_now(bot)                                          # does not pay the carried rest
    bot.local.flat_since = "F2"                                               # next flat
    assert JL.repay_due_now(bot)


def test_bank_tick_runs_repay_and_never_auto_repay(env, monkeypatch):
    bot = env["mk"]("SOL", bal=0.5)
    K.loan_open(bot.local.bank, 0.1, 0.08)
    seen = []
    monkeypatch.setattr(JL, "repay_tick", lambda bot, s, c: seen.append((s, c)))
    st = st_with(size=0, reqs=[])
    st["wallet"]["sol"] = 0.6
    bot.local.side = 0
    bot.bank_tick(st, None, PX)
    assert seen == [(PX, PX)] and not hasattr(R, "auto_repay")


# ------------------------------------------------------------------ E2E sim 1: next add funded from JupSOL
def test_e2e_sim1_sol_low_equity_peers_off_jupsol_lends_add_placed_flat_exact_repay(env):
    bot = env["mk"]("SOL", bal=0.02, mode="on")                               # $2.13 free, $10 rung
    (bot.dir / JL.PEER_OFF_FILE).touch()                                      # test switch: no peer lending
    st = st_with(jupsol=1.0)
    pos = bot.chain_position(st)
    b = bot.local.bank
    cash = b["bal"] * PX
    before = inv(b)
    ds0 = bot.local.day_start_equity
    for _ in range(2):
        plan, _ = bot.rest_add_plan(pos, cash)
    assert plan is None and bot._jl_want["need_usd"] == pytest.approx(10.0 - cash + 1.0)
    assert not env["ib"].exists() or json.loads(env["ib"].read_text()).get("status") != "requested"
    need_sol = bot._jl_want["need_usd"] / PX
    fake_swap(env, lambda plan, out, md: lend_res(plan["atoms"] / LAM, 0.0666, sig="LEND1"))
    assert JL.tick(bot, st, pos, PX) is True                                   # wallet moved: recon held this tick
    sw = env["calls"]["swap"][-1]
    assert (sw["in"], sw["out"], sw["mode"]) == (K.MINT["JUPSOL"], K.MINT["SOL"], "ExactOut")   # ONE ExactOut swap
    assert sw["atoms"] == int(need_sol * LAM)
    assert K.loan_outstanding(b) == pytest.approx(need_sol, abs=1e-9) and K.loan_jupsol_owed(b) == pytest.approx(0.0666)
    assert inv(b) == before and bot.local.day_start_equity == pytest.approx(ds0 + need_sol * PX, abs=1e-6)
    assert b["loan"]["tranches"][-1]["kind"] == "next_add" and "jl_op" not in b
    assert ledger.read_stake_rows()[-1]["kind"] == "loan_out"
    # the add now fits in the bot's own free balance and is placed by the normal resting-add path
    cash2 = b["bal"] * PX
    plan, why = bot.rest_add_plan(pos, cash2)
    assert plan is not None and plan.margin <= cash2, why
    import types
    bal_before_add = b["bal"]
    bot.resting_tick(pos, st, PX, types.SimpleNamespace(cash=cash2), 100.0)
    assert any(n == "create-limit-order" for n, _ in bot.calls)
    # ... the add fills, the trade closes flat: collateral back + 0.05 SOL profit (the bank's normal close booking)
    b["bal"] = round(bal_before_add + 0.05, 9)
    b["window"] = 0
    bot.local.flat_since = "F1"
    assert JL.repay_due_now(bot)
    env["jres"]["swap-quote"] = {"ok": True, "inAmount": "60000000", "otherAmountThreshold": "61000000"}
    fake_swap(env, lambda plan, out, md: repay_res(0.0835, plan["atoms"] / LAM, sig="REPAY1"))
    JL.repay_tick(bot, PX, PX)
    sw = env["calls"]["swap"][-1]
    assert sw["mode"] == "ExactOut" and sw["atoms"] == 66_600_000              # exactly the 0.0666 JupSOL lent
    assert "loan" not in b and b["loans_done"][-1]["repaid_jupsol"] == pytest.approx(0.0666)
    assert b["hwm"] == before[1] and b["wd_mark"] == before[2]                 # loan never profit / deposit


# ------------------------------------------------------------------ E2E sim 2: rescue under 3 %, capped at 1/3
def test_e2e_sim2_rescue_capped_at_one_third(env, caplog):
    bot = env["mk"]("SOL", bal=0.3, mode="on")
    st = st_with(price=105.0, jupsol=0.1, borrow=0.5)                          # tiny bucket: the 1/3 cap binds
    env["jres"]["stake-state"] = {"ok": True, "jupsol": 0.1, "jupsolAtoms": str(LAM // 10), "solPerJupsol": 1.25}
    pos = bot.chain_position(st)
    b = bot.local.bank
    before = inv(b)
    usable_usd = 0.1 * 1.25 * 105.0
    cap_usd = usable_usd * 0.99 / 3
    fake_swap(env, lambda plan, out, md: lend_res(plan["atoms"] / LAM, plan["atoms"] / LAM / 1.25, sig="RES1"))
    assert JL.tick(bot, st, pos, 105.0) is True
    sw = env["calls"]["swap"][-1]
    assert (sw["in"], sw["mode"]) == (K.MINT["JUPSOL"], "ExactOut") and sw["atoms"] == int(cap_usd / 105.0 * LAM)
    inc = [a for n, a in bot.calls if n == "increase"]
    assert inc and inc[-1]["sizeUsd"] == 0 and int(inc[-1]["collateralAtoms"]) == sw["atoms"]   # all of it as collateral
    got = sw["atoms"] / LAM
    assert K.loan_outstanding(b) == pytest.approx(got) and b["trade_dep"] == pytest.approx(got)
    assert b["bal"] == pytest.approx(0.3)                                       # in as loan, straight out as collateral
    assert b["hwm"] == before[1] and b["wd_mark"] == before[2] and "jl_op" not in b and b["window"] > 0
    assert "EMERGENCY" in caplog.text                                         # $1.44 moves liq < 2 points
    assert "SOL" in JL.reg_read()["rescue_want"]


def test_rescue_add_failure_retries_without_second_loan(env):
    bot = env["mk"]("SOL", bal=0.3, mode="on")
    st = st_with(price=105.0, jupsol=0.1)
    env["jres"]["stake-state"] = {"ok": True, "jupsol": 0.1, "jupsolAtoms": str(LAM // 10), "solPerJupsol": 1.25}
    pos = bot.chain_position(st)
    fake_swap(env, lambda plan, out, md: lend_res(plan["atoms"] / LAM, 0.01))
    real = bot.cmd
    bot.cmd = lambda n, a: {"ok": False, "error": "sim fail"} if n == "increase" else real(n, a)
    JL.tick(bot, st, pos, 105.0)
    assert bot.local.bank["jl_op"]["stage"] == "add_retry" and len(env["calls"]["swap"]) == 1
    bot.cmd = real
    JL.tick(bot, st, pos, 105.0)
    assert "jl_op" not in bot.local.bank and len(env["calls"]["swap"]) == 1   # added, never borrowed twice


# ------------------------------------------------------------------ unknown outcomes: resolved from the tx, never re-sent
def test_unknown_lend_resolved_from_tx_deltas(env):
    bot = env["mk"]("SOL", bal=0.02, mode="on")
    (bot.dir / JL.PEER_OFF_FILE).touch()
    pos = bot.chain_position(st_with())
    bot.rest_add_plan(pos, 2.0); bot.rest_add_plan(pos, 2.0)
    fake_swap(env, lambda *a: {"ok": False, "sig": "UNK", "error": "confirm timeout"})
    assert JL.tick(bot, st_with(), pos, PX) is True
    assert bot.local.bank["jl_op"]["stage"] == "lend_unknown" and "loan" not in bot.local.bank
    env["jres"]["tx-deltas"] = {"ok": True, "found": True, "deltas": lend_res(0.08, 0.066)["deltas"]}
    fake_swap(env, lambda *a: pytest.fail("never re-sent"))
    assert JL.tick(bot, st_with(), pos, PX) is True
    assert K.loan_outstanding(bot.local.bank) == pytest.approx(0.08) and "jl_op" not in bot.local.bank


def test_tick_never_raises(env, monkeypatch):
    bot = env["mk"]("SOL", mode="on")
    monkeypatch.setattr(JL, "_tick", lambda *a: 1 / 0)
    assert JL.tick(bot, st_with(), None, PX) is True


# ------------------------------------------------------------------ park tool (live test): equity / hwm / wd_mark unchanged
def test_park_and_unpark(env):
    bot = env["mk"]("SOL", bal=0.5, mode="off")                               # works in any mode
    b = bot.local.bank
    before = inv(b)
    (bot.dir / JL.PARK_FILE).write_text(json.dumps({"op": "park", "native": 0.4}))
    fake_swap(env, lambda plan, out, md: repay_res(plan["atoms"] / LAM, 0.32, sig="PARK"))
    assert JL.tick(bot, st_with(size=0, reqs=[]), None, PX) is True
    assert b["bal"] == pytest.approx(0.1) and b["park"]["jupsol"] == pytest.approx(0.32)
    assert inv(b)[:4] == before[:4]                                            # equity / hwm / wd_mark / guard target
    assert JL.parked_jupsol(JL.reg_read()) == pytest.approx(0.32)              # never lent out
    assert not (bot.dir / JL.PARK_FILE).exists() and "park done" in (bot.dir / (JL.PARK_FILE + ".result")).read_text()
    b["window"] = 0
    (bot.dir / JL.PARK_FILE).write_text(json.dumps({"op": "unpark"}))
    fake_swap(env, lambda plan, out, md: lend_res(0.399, plan["atoms"] / LAM, sig="UNPARK"))
    JL.tick(bot, st_with(size=0, reqs=[]), None, PX)
    assert "park" not in b and b["bal"] == pytest.approx(0.499) and b["hwm"] == before[1] and b["wd_mark"] == before[2]
    assert JL.parked_jupsol(JL.reg_read()) == 0.0
    kinds = [r["kind"] for r in ledger.read_stake_rows()]
    assert kinds[-2:] == ["park", "unpark"]


def test_park_refuses_more_than_free(env):
    bot = env["mk"]("SOL", bal=0.1, mode="off")
    (bot.dir / JL.PARK_FILE).write_text(json.dumps({"op": "park", "native": 5}))
    fake_swap(env, lambda *a: pytest.fail("refused"))
    JL.tick(bot, st_with(size=0, reqs=[]), None, PX)
    assert "refused" in (bot.dir / (JL.PARK_FILE + ".result")).read_text() and "park" not in bot.local.bank


# ------------------------------------------------------------------ bug 5: one ExactOut swap, JupSOL as input only
def test_stake_ts_allowlist_jupsol_input_only():
    src = open("live/src/stake.ts").read()
    assert "JupSOL is allowed as INPUT only" in src or "INPUT only" in src


# ------------------------------------------------------------------ 2026-10-28: next-add loan covers the SOL fee reserve
def test_next_add_need_covers_the_larger_of_bank_shortfall_and_fee_reserve_gap():
    assert JL.next_add_need_usd(10.0, 10.99, 0.41) == pytest.approx(1.41)    # cash covers the rung: reserve gap + $1
    assert JL.next_add_need_usd(10.0, 2.0, 0.5) == pytest.approx(9.0)        # bank shortfall binds: unchanged
    assert JL.next_add_need_usd(10.0, 5.05, 6.0) == pytest.approx(7.0)       # reserve gap binds
    assert JL.next_add_need_usd(10.0, 2.0) == pytest.approx(9.0)             # default: exactly as before


def test_add_reserve_short_usd_sol_only(env):
    bot = env["mk"]("SOL")
    st = st_with()
    st["wallet"]["sol"] = 0.105
    need = 10.0 / PX
    assert bot.add_reserve_short_usd(st, 10.0) == pytest.approx((need + bot.live.sol_fee_reserve - 0.105) * PX)
    st["wallet"]["sol"] = 1.2
    assert bot.add_reserve_short_usd(st, 10.0) == 0.0                         # reserve holds
    assert bot.add_reserve_short_usd(None, 10.0) == 0.0
    eth = env["mk"]("ETH")
    st["wallet"]["sol"] = 0.01
    assert eth.add_reserve_short_usd(st, 10.0) == 0.0                        # ETH / BTC: never


def _reserve_case(env, mode="on"):
    """Live 2026-10-28 11:09 MT: bank cash covers the $10 rung, but native SOL - need < sol_fee_reserve; a first
    next-add loan is already outstanding."""
    import types
    bot = env["mk"]("SOL", bal=0.108929717, mode=mode)
    (bot.dir / JL.PEER_OFF_FILE).touch()
    del bot.guard                                                             # the real guard (fee-reserve check)
    K.loan_open(bot.local.bank, 0.058942093, 0.048497176, kind="next_add", sig="LEND1")
    st = st_with(jupsol=1.0)
    st["wallet"]["sol"] = 0.105
    pos = bot.chain_position(st)
    acc = types.SimpleNamespace(cash=bot.local.bank["bal"] * PX)
    return bot, st, pos, acc


def test_fee_reserve_only_block_triggers_jupsol_loan_then_add_placed_and_total_repaid(env):
    bot, st, pos, acc = _reserve_case(env)
    b = bot.local.bank
    assert acc.cash >= 10.0 and bot.guard(B.Intent(B.Action.ADD, pos.side, 100.0, 10.0, 5.0), pos, st, 100.0) \
        == "would breach SOL fee reserve"
    short = bot.add_reserve_short_usd(st, 10.0)
    assert short > 0
    before = inv(b)
    for _ in range(2):
        bot._jl_just_lent = False
        bot.resting_tick(pos, st, PX, acc, 100.0)
    assert not any(n == "create-limit-order" for n, _ in bot.calls)
    assert bot._jl_want["need_usd"] == pytest.approx(short + 1.0) and bot._jl_want["reserve_short_usd"] > 0
    assert not env["ib"].exists() or json.loads(env["ib"].read_text()).get("status") != "requested"   # no peer request
    need_sol = bot._jl_want["need_usd"] / PX
    fake_swap(env, lambda plan, out, md: lend_res(plan["atoms"] / LAM, 0.0115, sig="LEND2"))
    assert JL.tick(bot, st, pos, PX) is True
    assert env["calls"]["swap"][-1]["atoms"] == int(need_sol * LAM)
    # the second loan adds to the outstanding one; equity / hwm / wd_mark unchanged
    assert K.loan_outstanding(b) == pytest.approx(0.058942093 + need_sol, abs=1e-9)
    assert K.loan_jupsol_owed(b) == pytest.approx(0.048497176 + 0.0115, abs=1e-9)
    assert [t["sig"] for t in b["loan"]["tranches"]] == ["LEND1", "LEND2"] and b["loan"]["n"] == 2
    assert inv(b) == before
    # same tick, stale wallet snapshot: no second want (never a double loan)
    acc.cash = b["bal"] * PX
    bot.resting_tick(pos, st, PX, acc, 100.0)
    assert bot._jl_want is None and not any(n == "create-limit-order" for n, _ in bot.calls)
    # next tick: the wallet shows the lent SOL -> the reserve holds -> the add is placed
    bot._jl_just_lent = False
    st["wallet"]["sol"] = round(0.105 + need_sol, 9)
    assert bot.add_reserve_short_usd(st, 10.0) == 0.0
    bot.resting_tick(pos, st, PX, acc, 100.0)
    assert any(n == "create-limit-order" for n, _ in bot.calls)
    # flat later: ONE ExactOut repay of the total JupSOL owed (both tranches)
    b["bal"], b["window"], bot.local.flat_since = 0.5, 0, "F1"
    b.pop("pend", None)
    assert JL.repay_due_now(bot)
    env["jres"]["swap-quote"] = {"ok": True, "inAmount": "75000000", "otherAmountThreshold": "76000000"}
    fake_swap(env, lambda plan, out, md: repay_res(0.075, plan["atoms"] / LAM, sig="REPAY"))
    JL.repay_tick(bot, PX, PX)
    sw = env["calls"]["swap"][-1]
    assert sw["mode"] == "ExactOut" and sw["atoms"] == int(round((0.048497176 + 0.0115) * LAM))
    assert "loan" not in b and b["loans_done"][-1]["repaid_jupsol"] == pytest.approx(0.059997176)


def test_fee_reserve_block_mode_off_or_eth_unchanged(env):
    bot, st, pos, acc = _reserve_case(env, mode="off")
    for _ in range(3):
        bot.resting_tick(pos, st, PX, acc, 100.0)
    assert bot._jl_want is None and not env["calls"]["swap"]
    assert not env["ib"].exists() or json.loads(env["ib"].read_text()).get("status") != "requested"
    eth = env["mk"]("ETH", mode="on")
    eth._reserve_loan(pos, st, 50.0, __import__("types").SimpleNamespace(margin=10.0))
    assert getattr(eth, "_jl_want", None) is None


def test_shortfall_path_includes_fee_reserve_gap(env):
    bot = env["mk"]("SOL", bal=0.0474, mode="on")                              # ~$5.05 free, $10 rung
    (bot.dir / JL.PEER_OFF_FILE).touch()
    st = st_with()
    st["wallet"]["sol"] = 0.0535                                              # native barely above bal (live shape)
    pos = bot.chain_position(st)
    cash = bot.local.bank["bal"] * PX
    for _ in range(2):
        plan, _ = bot.rest_add_plan(pos, cash, st=st)
    short = bot.add_reserve_short_usd(st, 10.0)
    assert plan is None and short > 10.0 - cash
    assert bot._jl_want["need_usd"] == pytest.approx(short + 1.0)
