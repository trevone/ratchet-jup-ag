"""Trevor 2026-10-29 ("on-chain truth only; bookkeeping only for loans"): the guard TP price (TP1 = TP2 at the profit-guard
price) is computed from the chain read only (guard_chain_equity: wallet + escrow + position collateral - accrued borrow fee
+ PnL - custody close fee), never from the bookkept bank or payout estimates. Incident 2026-10-29 SOL: the TP sat at 101.09
from bank bal + an estimated close value (no borrow fee, config close fee) and booked 0.0018 SOL short of the target.
When the chain read is failed / stale, resting TPs are kept unchanged and retried on the next fresh read. Fake chain only."""
import time

import pytest

import live.bot as B
from tests.test_guard_onchain import sb, set_target, sol_st  # noqa: F401 - fixtures
from tests import test_rest_add_size_growth_first as R

b = R.b


def _arm(sb):
    sb.local.partials_done = [{"pubkey": "old"}]
    assert sb.guard_tps()


def test_guard_price_is_where_on_chain_equity_meets_target(sb):
    _arm(sb)
    st = sol_st(price=103.31)
    pos = sb.chain_position(st)
    tgt = sb.guard_chain_equity(st, pos, 102.0)[0]
    set_target(sb, tgt)
    gp = sb.guard_price(pos, st)
    assert gp is not None and 102.0 - 1e-6 < gp < 103.31
    assert sb.guard_chain_equity(st, pos, gp)[0] == pytest.approx(sb.guard_target(st), abs=1e-9)


def test_bank_records_never_move_the_guard_price(sb):
    _arm(sb)
    st = sol_st(price=103.31)
    pos = sb.chain_position(st)
    set_target(sb, sb.guard_chain_equity(st, pos, 102.0)[0])
    gp = sb.guard_price(pos, st)
    sb.local.bank["bal"] += 0.0024   # e.g. an un-trued-up payout estimate in bal
    sb.local.rest_add = {"nat": 5.0, "debited": True, "size_usd": 1.0}
    assert sb.guard_price(pos, st) == pytest.approx(gp, abs=1e-9)


def test_accrued_borrow_fee_moves_short_guard_price_further(sb):
    """The bank estimate ignored the accrued borrow fee; on chain it lowers the close value -> short TP lower (safe side)."""
    _arm(sb)
    st0, st1 = sol_st(price=103.31, borrow=0.0), sol_st(price=103.31, borrow=0.25)
    pos = sb.chain_position(st0)
    set_target(sb, sb.guard_chain_equity(st0, pos, 102.0)[0])
    assert sb.guard_price(pos, st1) < sb.guard_price(pos, st0) - 0.05


@pytest.mark.parametrize("breakit", ["old_read", "borrow_none", "escrow_error"])
def test_unreadable_chain_raises_no_estimate(sb, breakit):
    _arm(sb)
    st = sol_st(price=103.31)
    pos = sb.chain_position(st)
    set_target(sb, sb.guard_chain_equity(st, pos, 102.0)[0])
    if breakit == "old_read":
        st["_read_at"] = time.time() - 300
    elif breakit == "borrow_none":
        st["positions"]["short"]["borrowFeeUsd"] = None
    else:
        st["openRequests"][0]["escrowError"] = "429 Too Many Requests"
    with pytest.raises(B.GuardChainUnreadable):
        sb.guard_price(pos, st)


def test_drift_check_keeps_tps_when_chain_unreadable(sb):
    _arm(sb)
    st = sol_st(price=103.31, read_at=time.time() - 300)
    pos = sb.chain_position(st)
    sb.local.tp_px = [99.0, 99.0]
    sb.chain = lambda: st
    called = []
    sb.sync_tp_ladder = lambda p: called.append(p)
    sb._guard_drift_until = 0.0
    assert sb.guard_drift_check(pos, st) is False and called == []


def test_replace_keeps_resting_tps_when_chain_unreadable(sb):
    _arm(sb)
    st = sol_st(price=103.31, read_at=time.time() - 300)
    pos = sb.chain_position(st)
    set_target(sb, 1.0)
    sb.chain = lambda: st
    sent = []
    sb.cancel_reqs = lambda s, r: sent.append(("cancel", r)) or True
    sb.place_tp = lambda *a, **k: sent.append(("place", a)) or True
    before = list(sb.local.tp_px)
    sb._sync_tp_ladder(pos)
    assert sent == [] and sb.local.tp_retry is True and sb.local.tp_px == before
