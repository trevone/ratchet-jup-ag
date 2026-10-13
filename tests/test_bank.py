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


def test_short_close_usdc_swept_back_to_own_coin(ebot, monkeypatch):
    from live import sweep as S
    ebot.local.side = SHORT
    st = _st(size=0, usdc=13_800_000, eth_atoms=1071429)
    calls = []

    def fake_jup(cmd, args):
        calls.append((cmd, args))
        return {"ok": True, "sig": "SW", "deltas": {"nativeDeltaLamports": -6000, "feeLamports": 6000,
                                                   "tokenDeltaAtoms": {"ETH": "560000", "USDC": "-13800000"}}}
    monkeypatch.setattr(S, "attribute", lambda name, me, st: {"owner": "ETH", "side": "short", "open_sig": None,
                                                               "position": None, "open_time": None})
    monkeypatch.setattr("live.bot.jup", fake_jup)
    ebot.sweep(st, None)
    swap = [a for c, a in calls if c == "swap"][0]
    assert swap["direction"] == "swap" and swap["inputMint"] == K.MINT["USDC"] and swap["outputMint"] == K.MINT["ETH"]
    b = ebot.local.bank
    assert b["window"] == 3
    r = K.recon_token(b, 0.015 + 0.0056, 2450.0, 1.5, False)     # next read: the swapped ETH (fees included) = payout
    assert r["action"] == "payout" and r["diff"] == pytest.approx(0.0056) and b["hwm"] == 0.0155
    # BTC / ETH tokens themselves are never swept any more (they are the bots' balances)
    assert S.self_heal_own(ebot.cfg, "ETH", _st(size=0, usdc=0, eth_atoms=99_000_000), fake_jup, dry_run=False,
                           min_usd=0.1) == []


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
    for f, m in (("live_100", 0.0015), ("live_eth", 0.000066), ("live_btc", 0.00000195)):
        lc = load_config(f"configs/{f}.toml").ledger
        assert lc.enabled and lc.min_profit_native == m and lc.withdraw_fraction == 0.60 and lc.ext_split == (0.5, 0.25, 0.25)
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
    assert sw["inputMint"] == K.MINT["ETH"] and sw["outputMint"] == K.MINT["JUPSOL"] and sw["amountAtoms"] == "119999"
    assert (b["bal"], b["hwm"], b["wd_mark"]) == pytest.approx((0.0158, 0.0158, 0.0158))
    assert b["fee_sol"] == pytest.approx(0.000007) and b["wd_last_date"] and b["daily"][-1]["amount"] == pytest.approx(0.0012)
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
