"""Trevor 2026-10-28 14:42 / 14:48 / 14:49 MT: "There's only one truth, and that is what is on the table." The profit guard
decides closes ONLY from the chain: this bot's own token in the wallet (SOL: native + request rent - the 0.02 fee reserve)
+ the position account's close value (collateral - accrued borrow + PnL - on-chain close fee) + the own-coin collateral
held in this market's open request escrow accounts - loans / inter-bot fee debt (the only bookkeeping). No lot / order /
payout records. A failed or stale read never closes; right before a guard close the chain is re-read and the close is
aborted below target. Fake chain only."""
import time

import pytest

import live.bot as B
from live import bank as K
from strategy import SHORT
from tests import test_rest_add_size_growth_first as R
from tests.test_bank import _st as eth_st, ebot  # noqa: F401 - fixture

b = R.b   # SOL-like short fixture (resting add record LIM9, debited, not booked)
SOL = K.MINT["SOL"]


def sol_st(price=103.31, wallet=1.0, size=350.0, coll=R.COLL2, avg=R.AVG2, escrow=0, reqs=None, borrow=0.01, age=1, read_at=None):
    rq = [dict(r, mint=SOL, collateralDelta="0", escrowAtoms="0") for r in (reqs if reqs is not None else R.LADDER)]
    if escrow:
        rq.append(dict(R.LIM, mint=SOL, collateralDelta=str(escrow), escrowAtoms=str(escrow)))
    p = {"pubkey": "P", "sizeUsd": size, "collateralUsd": coll, "price": avg, "realisedPnlUsd": 0.0, "borrowFeeUsd": borrow}
    return {"market": "SOL", "openRequests": rq, "allOpenRequests": list(rq), "oracle": {"price": price, "ageSec": age},
            "solOracle": {"price": price, "ageSec": age}, "positions": {"long": {}, "short": p if size else {}},
            "wallet": {"sol": wallet}, "custody": {"decreasePositionBps": 6}, "sweepTokens": {},
            "_read_at": time.time() if read_at is None else read_at}


def chain_eq(b, st):
    """Expected on-chain equity, written out from the chain fields."""
    p, px = st["positions"]["short"], st["oracle"]["price"]
    fill = px * (1 + b.cfg.costs.slippage)
    usd = p["collateralUsd"] - p["borrowFeeUsd"] - p["sizeUsd"] * (fill - p["price"]) / p["price"] - 0.0006 * p["sizeUsd"]
    esc = sum(int(r["escrowAtoms"]) for r in st["openRequests"]) / 1e9
    return (st["wallet"]["sol"] + K.REQ_RENT * len(st["allOpenRequests"]) - b.live.sol_fee_reserve + esc
            + usd / px - b.cfg.ledger.close_network_native)


@pytest.fixture
def sb(b, monkeypatch):
    R.bank_on(b, monkeypatch)
    b.local.last_size = 350.0
    b._peer_fee_sol = lambda: [0.0, 0.0]
    return b


def set_target(b, tgt):
    b.local.bank["trade_start_equity"] = tgt / (1 + b.cfg.profit_guard.min_profit_pct_trade)


def test_equity_is_the_chain_formula_and_ignores_bot_records(sb):
    st = sol_st(escrow=50_000_000)
    pos = sb.chain_position(st)
    eq, why = sb.guard_chain_equity(st, pos)
    assert eq == pytest.approx(chain_eq(sb, st), abs=1e-12) and "on-chain" in why
    for rest in (None, {"nat": 5.0, "debited": True, "size_usd": 1.0}):   # records never move it
        sb.local.rest_add = rest
        sb.local.lots, sb.local.bank["bal"] = [], 123.0
        assert sb.guard_chain_equity(st, pos)[0] == pytest.approx(eq, abs=1e-12)


def test_sol_1401_stale_add_record_on_chain_below_target_no_close(sb):
    """14:01 MT: the add filled on chain (size 350, escrow empty) but its record was still listed -> hwm_equity's
    resting-add term was +NAT too high. Target between the two: the record-based number would close, the chain does not."""
    st = sol_st(escrow=0)
    pos = sb.chain_position(st)
    eq = chain_eq(sb, st)
    set_target(sb, eq + R.NAT / 2)
    ok, note = sb.guard_check_chain(st, pos)
    assert not ok and "on chain" in note
    assert eq + R.NAT >= sb.guard_target(st)   # the double-counted (record) equity would have met it


def test_genuine_profit_closes(sb):
    st = sol_st(price=101.0, escrow=0)
    pos = sb.chain_position(st)
    set_target(sb, chain_eq(sb, st) - 1e-6)
    assert sb.guard_check_chain(st, pos)[0]
    sb.chain = lambda: sol_st(price=101.0, escrow=0)
    ok, note, pos2, px2 = sb.guard_recheck_chain(pos)
    assert ok and pos2.size == 350.0 and px2 == 101.0


def test_escrow_collateral_counts_wrong_mint_fails(sb):
    st0, st1 = sol_st(escrow=0), sol_st(escrow=int(R.NAT * 1e9))
    pos = sb.chain_position(st0)
    assert sb.guard_chain_equity(st1, pos)[0] - sb.guard_chain_equity(st0, pos)[0] == pytest.approx(int(R.NAT * 1e9) / 1e9 + K.REQ_RENT)
    st1["openRequests"][-1]["mint"] = K.MINT["USDC"]
    assert sb.guard_chain_equity(st1, pos)[0] is None


@pytest.mark.parametrize("breakit", ["old_read", "oracle_stale", "sol_oracle_stale", "escrow_error", "escrow_missing",
                                     "borrow_none", "bps_none", "peer_unreadable", "park"])
def test_failed_or_stale_read_never_closes(sb, breakit):
    st = sol_st(price=101.0)
    pos = sb.chain_position(st)
    set_target(sb, 0.0)   # would close on any number
    assert sb.guard_check_chain(st, pos)[0]
    if breakit == "old_read":
        st["_read_at"] = time.time() - 120
    elif breakit == "oracle_stale":
        st["oracle"]["ageSec"] = 10_000
    elif breakit == "sol_oracle_stale":
        st["solOracle"] = {"price": None}
    elif breakit == "escrow_error":
        st["openRequests"][0]["escrowError"] = "429"
    elif breakit == "escrow_missing":
        del st["openRequests"][0]["escrowAtoms"]
    elif breakit == "borrow_none":
        st["positions"]["short"]["borrowFeeUsd"] = None
    elif breakit == "bps_none":
        st["custody"] = {}
    elif breakit == "peer_unreadable":
        def boom():
            raise OSError("peer state unreadable")
        sb._peer_fee_sol = boom
    elif breakit == "park":
        sb.local.bank["park"] = {"native": 0.1}
    ok, note = sb.guard_check_chain(st, pos)
    assert not ok and note.startswith("on-chain guard: NO CLOSE")


def test_pre_close_recheck_aborts_below_target_or_on_failure(sb):
    st = sol_st(price=101.0)
    pos = sb.chain_position(st)
    set_target(sb, chain_eq(sb, st) - 1e-6)
    assert sb.guard_check_chain(st, pos)[0]
    sb.chain = lambda: sol_st(price=101.2)          # moved against the short before the send: below target
    assert not sb.guard_recheck_chain(pos)[0]
    sb.chain = lambda: sol_st(price=101.0, size=0)  # position gone
    assert not sb.guard_recheck_chain(pos)[0]

    def fail():
        raise RuntimeError("state read failed: 429")
    sb.chain = fail
    assert not sb.guard_recheck_chain(pos)[0]


def test_peer_fee_debt_and_loans_are_the_only_bookkeeping(sb):
    st = sol_st(price=101.0)
    pos = sb.chain_position(st)
    e0 = sb.guard_chain_equity(st, pos)[0]
    sb._peer_fee_sol = lambda: [0.002, 0.003]   # owed back to the SOL wallet by the ETH / BTC bots
    assert sb.guard_chain_equity(st, pos)[0] == pytest.approx(e0 + 0.005)
    sb.local.bank["loan"] = {"principal": 0.05}
    assert sb.guard_chain_equity(st, pos)[0] == pytest.approx(e0 + 0.005 - 0.05)


def _tick_to_guard(b, st, monkeypatch):
    """Real tick() through the guard block; everything after it (klines) stops the tick."""
    b.chain = lambda: st
    b.sweep = lambda *a, **k: None
    b.recover_missing_add = lambda *a, **k: False
    b.sync_tp_ladder = lambda pos: None
    b.partial_sync = lambda pos, price: None
    b.resize_tps = lambda pos: None
    b.bank_tick = lambda *a, **k: None
    b.resting_reconcile = lambda st, pos, price: (pos, False)
    b.resting_tick = lambda *a, **k: None
    b.guard_drift_check = lambda *a, **k: False
    b._fee_rechecked = True
    b.closed = []
    b.close = lambda pos, price, why: b.closed.append((pos.size, price, why))
    monkeypatch.setattr(B, "fetch_klines", lambda *a, **k: (_ for _ in ()).throw(R._Stop()))
    try:
        b.tick()
    except R._Stop:
        pass


def test_tick_closes_only_on_chain_equity_and_rechecks(sb, monkeypatch, caplog):
    sb.local.guard_armed = True
    st = sol_st(price=101.0)
    set_target(sb, chain_eq(sb, st) + 1e-6)          # just below on chain; the records say more (stale add)
    sb.hwm_equity = lambda *a, **k: 99.0
    _tick_to_guard(sb, st, monkeypatch)
    assert sb.closed == []
    set_target(sb, chain_eq(sb, st) - 1e-6)
    _tick_to_guard(sb, st, monkeypatch)
    assert len(sb.closed) == 1 and sb.closed[0][2].startswith("profit_guard (trade guard (on chain)")
    reads = [sol_st(price=101.0), sol_st(price=101.2)]   # tick read meets it, the pre-close re-read does not
    sb.closed = []
    _tick_to_guard(sb, reads[0], monkeypatch)
    sb.closed.clear()
    it = iter(reads)
    sb.chain = lambda: next(it)
    caplog.set_level("WARNING")
    sb.close = lambda pos, price, why: sb.closed.append(why)
    monkeypatch.setattr(B, "fetch_klines", lambda *a, **k: (_ for _ in ()).throw(R._Stop()))
    try:
        sb.tick()
    except R._Stop:
        pass
    assert sb.closed == [] and "pre-close re-check -> NO close" in caplog.text and next(it, None) is None


def test_eth_flat_like_for_like_with_bank(ebot):  # noqa: F811
    """ETH / BTC: wallet token == bal (they own their token) -> on-chain equity == bal - fee debt, the HWM formula."""
    bk = ebot.local.bank
    bk["bal"], bk["fee_sol"] = 0.015, 0.002
    st = dict(eth_st(size=0, eth_atoms=1_500_000), custody={"decreasePositionBps": 6}, _read_at=time.time())
    st["solOracle"]["ageSec"] = 1
    assert ebot.guard_chain_equity(st, None)[0] == pytest.approx(ebot.bank_equity_flat(st), abs=1e-9)   # bank_equity_flat rounds to 9 dp
    st["sweepTokens"] = {"error": "429"}
    assert ebot.guard_chain_equity(st, None)[0] is None


def test_eth_short_close_value_from_position_account(ebot):  # noqa: F811
    ebot.local.side = SHORT
    st = dict(eth_st(size=70.5, coll=13.43, avg=2472.67, price=2450.0, eth_atoms=1_000_000),
              custody={"decreasePositionBps": 6}, _read_at=time.time())
    st["positions"]["short"]["borrowFeeUsd"] = 0.02
    st["solOracle"]["ageSec"] = 1
    pos = ebot.chain_position(st)
    c, lc = ebot.cfg.costs, ebot.cfg.ledger
    fill = 2450.0 * (1 + c.slippage)
    usd = 13.43 - 0.02 - 70.5 * (fill - 2472.67) / 2472.67 - 0.0006 * 70.5
    want = 0.01 + usd / 2450.0 * (1 - lc.swap_cost_pct) - K.fee_debt_native(ebot.local.bank, 110.0, 2450.0)
    assert ebot.guard_chain_equity(st, pos)[0] == pytest.approx(want, abs=1e-12)
