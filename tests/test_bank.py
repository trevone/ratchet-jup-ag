"""Per-bot own-coin ledger + HWM (Trevor 2026-10-08 21:37 MT, live/bank.py). Fake chain only (conftest blocks the CLI)."""
import dataclasses
from datetime import datetime, timezone

import pytest

from config import load_config
from live import bank as K
from strategy import LONG, SHORT, Action, Intent


# ------------------------------------------------------------------ HWM / accounting
def test_hwm_only_rises_when_flat():
    b = K.new_bank("SOL", 0.70, 0.78)
    K.credit(b, 0.10, "payout")                      # bal 0.80 > hwm while a position is open
    assert K.update_hwm(b, flat=False) is False and b["hwm"] == 0.78
    assert K.update_hwm(b, flat=True) is True and b["hwm"] == pytest.approx(0.80)
    K.debit(b, 0.05, "collateral")                    # lower balance never lowers the hwm
    assert K.update_hwm(b, flat=True) is False and b["hwm"] == pytest.approx(0.80)
    e = K.new_bank("ETH", 0.015, 0.015)              # ETH: unsettled SOL fee debt counts against it
    K.credit(e, 0.0001)
    e["fee_sol"] = 0.002
    debt = K.fee_debt_native(e, 110.0, 2500.0)
    assert debt == pytest.approx(0.002 * 110 / 2500)
    K.update_hwm(e, True, debt)
    assert e["hwm"] == pytest.approx(0.0151 - debt)


def test_external_deposit_withdrawal_is_not_profit():
    b = K.new_bank("SOL", 0.68, 0.78)
    K.apply_external(b, 0.5, "deposit")
    assert (b["bal"], b["hwm"], b["wd_mark"]) == pytest.approx((1.18, 1.28, 1.28))
    assert K.withdraw_amount(b, 0.5, 0.0) == (0.0, 0.0)       # nothing to withdraw: capital, not profit
    K.apply_external(b, -0.2, "withdrawal")
    assert (b["bal"], b["hwm"]) == pytest.approx((0.98, 1.08)) and K.withdraw_amount(b, 0.5, 0.0)[1] == 0.0
    # SOL wallet: a persisting jump >= ext_min_usd is external (2 ticks), small jumps are reserve float
    s = K.new_bank("SOL", 0.68)
    assert K.recon_sol(s, 0.70, 0.0, 110.0, 1.5, False)["action"] == "init"
    assert K.recon_sol(s, 0.70003, 0.0, 110.0, 1.5, False)["action"] == "float"   # fee / rent noise
    assert K.recon_sol(s, 1.70003, 0.0, 110.0, 1.5, False)["action"] == "pending"
    r = K.recon_sol(s, 1.70003, 0.0, 110.0, 1.5, False)
    assert r["action"] == "external" and r["diff"] == pytest.approx(1.0)
    sh = K.split_shares(r["diff"], (0.5, 0.25, 0.25))
    assert sh == {"SOL": 0.5, "ETH": 0.25, "BTC": 0.25}
    K.apply_external(s, sh["SOL"])
    ev = K.post_external(r["diff"], 110.0, (0.5, 0.25, 0.25), "test")
    j = K.load_journal()
    assert K.pending_others(j) == pytest.approx(0.5)
    assert K.recon_sol(s, 1.70003, K.pending_others(j), 110.0, 1.5, False)["action"] == "float"   # not re-detected
    assert K.open_shares(j, "ETH", 0.001) == [(ev["id"], 0.25)]
    K.settle("ETH", ev["id"], 0.25)
    assert K.pending_others(K.load_journal()) == pytest.approx(0.25)
    # ETH token: unexplained change outside a payout window -> external for the ETH bot only
    e = K.new_bank("ETH", 0.015)
    assert K.recon_token(e, 0.017, 2500.0, 1.5, False, "split")["action"] == "pending"
    assert K.recon_token(e, 0.017, 2500.0, 1.5, False, "split")["action"] == "external"
    assert (e["bal"], e["hwm"]) == pytest.approx((0.017, 0.017))
    assert e["totals"]["trade_net"] == 0.0


def test_payout_window_books_trade_flow_not_external():
    e = K.new_bank("ETH", 0.015, 0.0152)
    e["window"] = 3
    assert K.recon_token(e, 0.0205, 2500.0, 1.5, False)["action"] == "payout"
    assert e["bal"] == pytest.approx(0.0205) and e["hwm"] == 0.0152 and e["totals"]["trade_net"] == pytest.approx(0.0055)
    assert K.recon_token(e, 0.0, 2500.0, 1.5, True)["action"] == "wait"        # request in flight / USDC pending


def test_daily_withdrawal_math_reduces_balance_and_hwm():
    b = K.new_bank("SOL", 0.70, 0.70)
    K.credit(b, 0.02)
    K.update_hwm(b, True)                             # realized while flat: hwm 0.72
    amt, gain = K.withdraw_amount(b, 0.5, 0.0045)
    assert gain == pytest.approx(0.02) and amt == pytest.approx(0.01)
    K.apply_withdrawal(b, amt)
    assert (b["bal"], b["hwm"], b["wd_mark"]) == pytest.approx((0.71, 0.71, 0.71))
    assert K.withdraw_amount(b, 0.5, 0.0045) == (0.0, 0.0)
    K.credit(b, 0.004)
    K.update_hwm(b, True)
    assert K.withdraw_amount(b, 0.5, 0.0045) == (0.0, pytest.approx(0.004))   # dust: carried, mark kept
    K.credit(b, 0.01)
    K.update_hwm(b, True)
    assert K.withdraw_amount(b, 0.5, 0.0045)[0] == pytest.approx(0.007)        # accumulated gain withdrawn later
    assert K.due(datetime(2026, 10, 9, 6, 6, tzinfo=timezone.utc), "00:05", None)
    assert not K.due(datetime(2026, 10, 9, 6, 6, tzinfo=timezone.utc), "00:05", "2026-10-09")
    assert not K.due(datetime(2026, 10, 9, 6, 4, tzinfo=timezone.utc), "00:05", "2026-10-08")


def test_close_value_native():
    v = K.close_value_native(SHORT, 70.5, 2472.67, 13.43, 2450.0, 0.0006, 2450.0, 0.002)
    usd = 13.43 + 70.5 * (2472.67 - 2450.0) / 2472.67 - 0.0006 * 70.5
    assert v == pytest.approx(usd / 2450.0 * 0.998)
    assert K.close_value_native(LONG, 0.0, 1, 1, 1, 0, 1) == 0.0


# ------------------------------------------------------------------ bot glue
def _st(asset="ETH", side="short", size=70.5, coll=13.43, avg=2472.67, price=2450.0, eth_atoms=1500000, usdc=0, reqs=()):
    p = {"pubkey": "P", "sizeUsd": size, "collateralUsd": coll, "price": avg, "realisedPnlUsd": 0.0} if size else {}
    rq = [{"pubkey": f"R{x}", "triggerPrice": x, "executed": False, "requestType": "trigger"} for x in reqs]
    return {"market": asset, "openRequests": rq, "allOpenRequests": rq, "oracle": {"price": price, "ageSec": 1},
            "solOracle": {"price": 110.0}, "wallet": {"sol": 0.70},
            "positions": {"long": p if side == "long" else {}, "short": p if side == "short" else {}},
            "sweepTokens": {"ETH": {"mint": K.MINT["ETH"], "atoms": str(eth_atoms)},
                            "USDC": {"mint": K.MINT["USDC"], "atoms": str(usdc)}}}


@pytest.fixture
def ebot(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_eth.toml", ["live.dry_run=true", f"bot.state_dir={tmp_path.name}"])
    monkeypatch.setattr(B, "ROOT", tmp_path.parent)
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)
    b.kill = tmp_path / "KILL"
    b.kill_all = tmp_path / "KILL_ALL"
    b.confirm_wait = 0.0
    b.calls = []

    def fake_cmd(name, args):
        b.calls.append((name, args))
        return {"ok": True, "sig": "S", "feeLamports": 5000}
    b.cmd = fake_cmd
    b.local.bank = K.new_bank("ETH", 0.015, 0.0155)
    b._sol_px, b._mkt_px = 110.0, 2450.0
    return b


def test_insufficient_own_funds_skips_add_never_borrows(ebot):
    st = _st()
    pos = ebot.chain_position(st) if ebot.local.side else None
    ebot.local.side, ebot.local.n_entries = SHORT, 1
    pos = ebot.chain_position(st)
    it = Intent(Action.ADD, SHORT, 2450.0, margin=40.0, leverage=6)          # 40 / 2450 = 0.0163 ETH > 0.015
    why = ebot.guard(it, pos, st, 100.0)
    assert why and "insufficient own funds" in why and "never borrows" in why
    ebot.open_or_add(it, pos, st, 100.0)
    assert not [n for n, _ in ebot.calls if n == "increase"] and ebot.local.bank["bal"] == 0.015
    assert ebot.guard(Intent(Action.ADD, SHORT, 2450.0, margin=30.0, leverage=6), pos, st, 100.0) is None


def test_short_open_funds_from_own_coin_via_keeper_swap(ebot):
    st0 = _st(size=0)
    st1 = _st(size=52.5, coll=10.45, avg=2450.0, eth_atoms=1071429)
    seq = [st1, st1, st1, st1]
    ebot.chain = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
    ebot.sync_tp_ladder = lambda pos: None
    ebot.open_or_add(Intent(Action.OPEN, SHORT, 2450.0, margin=10.5, leverage=5), None, st0, 100.0)
    a = [x for n, x in ebot.calls if n == "increase"][0]
    assert a["inputMint"] == K.MINT["ETH"] and a["collateralAtoms"] == str(int(10.5 / 2450.0 * 1e8))
    assert int(a["jupiterMinimumOut"]) == int(10.5 * (1 - ebot.live.price_slippage_pct) * 1e6)   # ETH -> USDC by keeper
    b = ebot.local.bank
    assert b["bal"] == pytest.approx(0.015 - int(10.5 / 2450.0 * 1e8) / 1e8)
    assert b["trade_dep"] == pytest.approx(int(10.5 / 2450.0 * 1e8) / 1e8) and ebot.local.bank is b
    assert b["fee_sol"] == 0.0   # fake cmd bypasses Bot.cmd; fees are booked there


def test_long_eth_open_needs_no_swap(ebot):
    st1 = _st(side="long", size=52.5, coll=10.45, avg=2450.0)
    seq = [st1]
    ebot.chain = lambda: seq[0]
    ebot.sync_tp_ladder = lambda pos: None
    ebot.open_or_add(Intent(Action.OPEN, LONG, 2450.0, margin=10.5, leverage=5), None, _st(side="long", size=0), 100.0)
    a = [x for n, x in ebot.calls if n == "increase"][0]
    assert a["inputMint"] == K.MINT["ETH"] and "jupiterMinimumOut" not in a


def test_guard_closes_at_hwm_plus_min_and_never_moves_tps(ebot):
    b = ebot.local.bank
    ebot.local.side, ebot.local.n_entries, ebot.local.lots = SHORT, 2, [{"size": 52.5, "price": 2469.5}, {"size": 18.0, "price": 2481.8}]
    ebot.local.tp_px = [2461.54, 2454.12]
    b["bal"], b["hwm"] = 0.0100, 0.0155
    assert ebot.hwm_close_armed()
    hold = _st(price=2472.0)                              # equity < hwm + min: hold
    pos = ebot.chain_position(hold)
    ok, note = ebot.hwm_check(pos, hold, 2472.0, ebot.cfg.ledger.min_profit_native)
    assert not ok and "HWM guard" in note
    win = _st(price=2440.0)
    pos = ebot.chain_position(win)
    eq = ebot.hwm_equity(pos, win, 2440.0)
    assert eq >= b["hwm"] + ebot.cfg.ledger.min_profit_native
    assert ebot.hwm_check(pos, win, 2440.0, ebot.cfg.ledger.min_profit_native)[0]
    # the guard never re-places TPs: trim/pg TP guards return targets unchanged
    targets = ebot.strategy.tp_targets(pos, price=2440.0)
    assert ebot.trim_tp_guard(pos, targets, "periodic") == targets
    # its action = the existing close path: cancel triggers, one entire market decrease, no trigger placement
    ebot.sweep = lambda *a, **k: None
    ebot.book_flat = lambda *a, **k: None
    states = [_st(price=2440.0, reqs=(2461.54, 2454.12)), _st(size=0)]
    ebot.chain = lambda: states.pop(0) if len(states) > 1 else states[0]
    ebot.close(pos, 2440.0, f"profit_guard ({note})")
    dec = [a for n, a in ebot.calls if n == "decrease"]
    assert len(dec) == 1 and dec[0]["entire"] is True and "triggerPriceUsd" not in dec[0]
    assert [n for n, _ in ebot.calls].count("cancel") == 2
    assert ebot.local.bank is b and ebot.local.side == 0 and b["window"] == 3   # bank survives the flat reset
    # first-entry only: guard not armed (TPs run)
    ebot.local.n_entries, ebot.local.lots, ebot.local.trims = 1, [{"size": 52.5, "price": 2469.5}], []
    assert not ebot.hwm_close_armed()


def test_tp_timer_not_below_hwm(ebot):
    b = ebot.local.bank
    ebot.local.side, ebot.local.n_entries = SHORT, 1
    b["bal"], b["hwm"] = 0.0100, 0.0155
    st = _st(price=2468.0)
    assert ebot.hwm_check(ebot.chain_position(st), st, 2468.0, 0.0)[0] is False


def test_bank_persists_in_state_json(ebot):
    import live.bot as B
    ebot.save()
    assert B.load_local(ebot.state_path).bank["hwm"] == 0.0155


def test_live_configs_ledger():
    for f, m, wf in (("live_100", 0.0015, 0.85), ("live_eth", 0.000066, 0.85), ("live_btc", 0.00000195, 0.85)):
        lc = load_config(f"configs/{f}.toml").ledger
        assert lc.enabled and lc.min_profit_native == m and lc.withdraw_fraction == wf and lc.ext_split == (0.5, 0.25, 0.25)
        assert lc.ext_flow_mode == "alert"
    assert load_config("configs/default.toml").ledger.enabled is False


def test_bank_tick_and_daily_withdrawal_eth(ebot, monkeypatch):
    import live.bot as B
    b = ebot.local.bank
    b["bal"], b["hwm"], b["wd_mark"] = 0.0150, 0.0150, 0.0150
    st = _st(size=0, eth_atoms=1500000)
    ebot.bank_tick(st, None, 2450.0)                       # in sync: nothing
    b["window"] = 2
    st2 = _st(size=0, eth_atoms=1700000)                   # payout lands in the window
    ebot.bank_tick(st2, None, 2450.0)
    assert b["bal"] == pytest.approx(0.017) and b["hwm"] == 0.015   # window still open: hwm not yet
    ebot.bank_tick(st2, None, 2450.0)
    assert b["hwm"] == pytest.approx(0.017)                # flat + window over: hwm raised
    calls = []

    def fake_jup(cmd, args):
        calls.append((cmd, args))
        return {"ok": True, "sig": "WD", "deltas": {"feeLamports": 7000, "nativeDeltaLamports": -7000,
                                                   "jupsolDeltaAtoms": "18000000"}}
    monkeypatch.setattr(B, "jup", fake_jup)
    monkeypatch.setattr(B.ledger, "record_stake", lambda row: {})
    b["wd_last_date"] = None
    ebot.bank_daily(st2, None)
    sw = [a for c, a in calls if c == "swap"][0]
    wf = ebot.cfg.ledger.withdraw_fraction                 # live_eth: 0.75 (2026-10-15 20:30 MT)
    amt = round(wf * 0.002, 9)                             # gain since last wd 0.017 - 0.015
    left = 0.017 - amt
    assert sw["inputMint"] == K.MINT["ETH"] and sw["outputMint"] == K.MINT["JUPSOL"] and sw["amountAtoms"] == str(round(amt * 1e8))
    assert (b["bal"], b["hwm"], b["wd_mark"]) == pytest.approx((left, left, left))
    assert b["fee_sol"] == pytest.approx(0.000007) and b["wd_last_date"] and b["daily"][-1]["amount"] == pytest.approx(amt)
    n = len(calls)
    ebot.bank_daily(st2, None)                             # once a day
    assert len(calls) == n


def test_recon_token_alert_mode_leaves_bank_untouched():
    e = K.new_bank("ETH", 0.02, 0.02)
    assert K.recon_token(e, 0.017, 2500.0, 1.5, False)["action"] == "pending"
    assert K.recon_token(e, 0.017, 2500.0, 1.5, False)["action"] == "alert"
    assert K.recon_token(e, 0.017, 2500.0, 1.5, False)["action"] == "alert_known"   # alerts once
    assert e["bal"] == 0.02 and e["hwm"] == 0.02 and e["wd_mark"] == 0.02 and e["totals"]["ext_out"] == 0.0
    assert K.recon_token(e, 0.02, 2500.0, 1.5, False)["action"] == "none" and "ext_alert" not in e


def test_bank_tick_sol_unexplained_alert_only(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_100.toml", ["live.dry_run=true"])
    assert cfg.ledger.ext_flow_mode == "alert"
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)
    b.local.bank = K.new_bank("SOL", 0.68, 0.78)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.70
    b.bank_tick(st, None, 110.0)
    st["wallet"]["sol"] = 0.49                             # e.g. a stray 0.21 SOL stake out of the wallet
    for _ in range(4):
        b.bank_tick(st, None, 110.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(0.68) and bk["hwm"] == pytest.approx(0.78) and bk["wd_mark"] == pytest.approx(0.78)
    assert bk["totals"]["ext_out"] == 0.0 and K.load_journal()["events"] == []
    txt = K.alert_path().read_text()
    assert txt.count("UNEXPLAINED WALLET CHANGE") == 1 and "-0.21" in txt


def test_bank_tick_sol_external_split(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_100.toml", ["live.dry_run=true", "ledger.ext_flow_mode=split"])
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)
    b.local.bank = K.new_bank("SOL", 0.68, 0.78)
    st = _st(asset="SOL", size=0)
    st["wallet"]["sol"] = 0.70
    b.bank_tick(st, None, 110.0)
    st["wallet"]["sol"] = 1.70                             # Trevor deposits 1 SOL without telling
    b.bank_tick(st, None, 110.0)
    assert b.local.bank["bal"] == pytest.approx(0.68) or b.local.bank["bal"] == pytest.approx(0.68)
    b.bank_tick(st, None, 110.0)
    bk = b.local.bank
    assert bk["bal"] == pytest.approx(1.18) and bk["wd_mark"] == pytest.approx(1.28)
    assert bk["totals"]["ext_in"] == pytest.approx(0.5) and bk["totals"]["trade_net"] == 0.0
    assert K.pending_others(K.load_journal()) == pytest.approx(0.5)
    b.bank_tick(st, None, 110.0)                           # no double count
    assert bk["bal"] == pytest.approx(1.18)


# ------------------------------------------------------------------ per-trade profit guard (Trevor 2026-10-14)
def _arm(ebot, side=LONG):
    ebot.local.side, ebot.local.n_entries, ebot.local.lots = side, 2, [{"size": 52.5, "price": 2450.0}, {"size": 18.0, "price": 2440.0}]


def test_trade_guard_fires_on_equity_vs_baseline_not_price_vs_avg(ebot):
    b = ebot.local.bank
    _arm(ebot)
    b["bal"], b["hwm"], b["trade_start_equity"] = 0.0100, 0.0155, 0.0150     # hwm must not matter
    assert ebot.guard_target(_st()) == pytest.approx(0.0150 * 1.002)
    lo = _st(side="long", price=2400.0)
    assert not ebot.guard_check(ebot.chain_position(lo), lo, 2400.0)[0]
    hi = _st(side="long", price=2600.0)                                          # far above avg: still fires
    pos = hi_pos = ebot.chain_position(hi)
    assert pos.side * (2600.0 - pos.avg_entry) > 0 and ebot.guard_check(hi_pos, hi, 2600.0)[0]
    b["trade_start_equity"] = 1.0                                                # hwm 0.0155 is ignored; baseline decides
    assert not ebot.guard_check(hi_pos, hi, 2600.0)[0]


def test_trade_guard_target_boundary(ebot):
    b = ebot.local.bank
    _arm(ebot)
    st = _st(side="long", price=2500.0)
    pos = ebot.chain_position(st)
    eq = ebot.hwm_equity(pos, st, 2500.0)
    b["trade_start_equity"] = eq / 1.002 + 1e-9
    assert not ebot.guard_check(pos, st, 2500.0)[0]
    b["trade_start_equity"] = eq / 1.002 - 1e-9
    assert ebot.guard_check(pos, st, 2500.0)[0]


def test_baseline_set_on_flat_open_persisted_and_reset_next_trade(ebot):
    b = ebot.local.bank
    b["trade_start_equity"] = 123.0                                              # stale from a previous trade
    b["fee_sol"], b["bal"] = 0.0, 0.015
    st1 = _st(side="long", size=52.5, coll=10.45, avg=2450.0)
    ebot.chain = lambda: st1
    ebot.sync_tp_ladder = lambda pos: None
    ebot.open_or_add(Intent(Action.OPEN, LONG, 2450.0, margin=10.5, leverage=5), None, _st(size=0), 100.0)
    assert b["trade_start_equity"] == pytest.approx(0.015)                      # before the opening debit
    assert b["bal"] < 0.015


def test_missing_baseline_migrates_once_to_hwm_without_touching_hwm(ebot):
    b = ebot.local.bank
    b.pop("trade_start_equity", None)
    wd = b["wd_mark"]
    assert ebot.guard_target(_st()) == pytest.approx(b["hwm"] * 1.002) and b["trade_start_equity"] == b["hwm"] and b["wd_mark"] == wd


def test_sol_rent_churn_across_bots_does_not_alert_real_outflow_does():
    """Shared wallet: each Perps request (any bot) moves 0.0037236 SOL from the wallet into the request account (refunded on
    execute / cancel); position accounts persist and move nothing. Live evidence 2026-10-15 01:45-01:47 MT: ETH open + TP1 +
    TP2 + resting add left the wallet -0.0149 / -0.0224 while the old (wrong-sign) netting alerted."""
    px = 106.0
    def st(nreq, npos=0):
        return {"allOpenRequests": [{}] * nreq, "allPositions": [{}] * npos}
    assert K.rent_held(st(3, 1)) == pytest.approx(3 * 0.0037236)
    s = K.new_bank("SOL", 0.60)
    free = 0.03                                         # reserve float
    rent0 = K.rent_held(st(1, 1))
    assert K.recon_sol(s, 0.60 + free - rent0, 0.0, px, 1.5, False, rent0)["action"] == "init"
    # requests come and go (wallet = bal + free - rent locked - ~fee noise): never an alert
    for nreq, npos in ((3, 1), (4, 2), (6, 2), (8, 3), (1, 1), (1, 0), (0, 0), (2, 1)):
        rent = K.rent_held(st(nreq, npos))
        r = K.recon_sol(s, 0.60 + free - rent - 0.00002, 0.0, px, 1.5, False, rent)
        assert r["action"] == "float", (nreq, npos, r)
        assert s["pend"] is None
    # a genuine 0.02 SOL transfer out still alerts (2 ticks), rent unchanged
    rent = K.rent_held(st(2, 1))
    assert K.recon_sol(s, 0.60 + free - rent - 0.02, 0.0, px, 1.5, False, rent)["action"] == "pending"
    r = K.recon_sol(s, 0.60 + free - rent - 0.02, 0.0, px, 1.5, False, rent)
    assert r["action"] == "external" and r["diff"] == pytest.approx(-0.02, abs=1e-4)
    # old baseline (res_v != 3) is re-initialised once instead of mis-reading it
    o = K.new_bank("SOL", 0.60)
    o["res_last"] = 0.05
    o["res_v"] = 2
    assert K.recon_sol(o, 0.7, 0.0, px, 1.5, False, 0.0112)["action"] == "init" and o["res_v"] == 3


# ------------------------------------------------- hwm_equity: coin price follows the evaluated price (Trevor 2026-10-15)
def _gp_setup(ebot, side):
    b = ebot.local.bank
    _arm(ebot, SHORT if side == "short" else LONG)
    b["bal"], b["fee_sol"], b["hwm"], b["trade_start_equity"] = 0.0100, 0.02, 0.0155, 0.0150
    ebot.local.rest_add = {"nat": 0.0004, "debited": True}
    st = _st(side=side, size=70.5, coll=13.43, avg=2472.67, price=2450.0 if side == "short" else 2480.0)
    return st, ebot.chain_position(st)


def _with_oracle(st, px):
    import copy
    s2 = copy.deepcopy(st)
    s2["oracle"]["price"] = px
    return s2


@pytest.mark.parametrize("side", ["long", "short"])
def test_guard_price_independent_of_oracle(ebot, side):
    st, pos = _gp_setup(ebot, side)
    ebot.local.bank["trade_start_equity"] = ebot.hwm_equity(pos, st, st["oracle"]["price"]) * (1.03 if side == "long" else 1.03) / 1.002
    base = ebot.guard_price(pos, st)
    assert base is not None
    for f in (0.98, 0.99, 1.01, 1.02):
        o = st["oracle"]["price"] * f
        s2 = _with_oracle(st, o)
        assert ebot.guard_price(pos, s2) == pytest.approx(base, rel=1e-9) or ebot.guard_price(pos, s2) is None


@pytest.mark.parametrize("side", ["long", "short"])
def test_guard_price_sol_oracle_bounded_sensitivity(ebot, side):
    st, pos = _gp_setup(ebot, side)
    ebot.local.bank["trade_start_equity"] = ebot.hwm_equity(pos, st, st["oracle"]["price"]) * 1.03 / 1.002
    base = ebot.guard_price(pos, st)
    s2 = dict(st, solOracle={"price": 110.0 * 1.02})
    g2 = ebot.guard_price(pos, s2)
    assert abs(g2 / base - 1) < 0.01          # 2% SOL move (only fee debt / rest value scale with it): < 1% on the guard price


@pytest.mark.parametrize("side", ["long", "short"])
def test_hwm_equity_closed_form(ebot, side):
    st, pos = _gp_setup(ebot, side)
    b, c, lc = ebot.local.bank, ebot.cfg.costs, ebot.cfg.ledger
    for p in (2300.0, 2470.0, 2650.0):
        fill = p * (1 - pos.side * c.slippage)
        usd = pos.margin + pos.side * pos.size * (fill - pos.avg_entry) / pos.avg_entry - c.close_fee * pos.size
        swap = lc.swap_cost_pct if pos.side == SHORT else 0.0
        want = b["bal"] + 0.0004 - b["fee_sol"] * 110.0 / p + max(usd, 0.0) / p * (1 - swap)
        assert ebot.hwm_equity(pos, st, p) == pytest.approx(want, rel=1e-12)


@pytest.mark.parametrize("side", ["long", "short"])
def test_guard_check_flips_at_guard_price_with_other_oracle(ebot, side):
    st, pos = _gp_setup(ebot, side)
    ebot.local.bank["trade_start_equity"] = ebot.hwm_equity(pos, st, st["oracle"]["price"]) * 1.03 / 1.002
    gp = ebot.guard_price(pos, st)
    other = _with_oracle(st, st["oracle"]["price"] * 1.015)
    eps = gp * 1e-6
    up, dn = (gp + eps, gp - eps)
    assert ebot.guard_check(pos, other, up)[0] is (side == "long")
    assert ebot.guard_check(pos, other, dn)[0] is (side == "short")


def test_regression_btc_0815_guard_price_no_longer_follows_oracle(ebot):
    # synthetic BTC-like short (coin price = candidate price): old code held cpx at the oracle -> a different guard price
    st, pos = _gp_setup(ebot, "short")
    ebot.local.bank["trade_start_equity"] = ebot.hwm_equity(pos, st, 2450.0) * 1.03 / 1.002
    gp_a = ebot.guard_price(pos, st)
    gp_b = ebot.guard_price(pos, _with_oracle(st, 2450.0 * 1.02))
    assert gp_a == pytest.approx(gp_b, rel=1e-9)
    flat = ebot.bank_equity_flat(st)
    assert flat == pytest.approx(0.0100 - 0.02 * 110.0 / 2450.0, abs=1e-9)   # flat equity still at the live oracle
