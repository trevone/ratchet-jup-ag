"""Trevor 2026-10-15: [profit_guard] tps_at_guard: TP1/TP2 AT the profit-guard close price (hwm_equity == guard_target),
re-placed on every partial / add; fee-minimum floors still clamp; resting add / TP3 partials never cancelled. Default off."""
from live import bank as bankmod
from strategy import LONG, SHORT
from tests.test_resting import ON, mkbot, req, state

FLAG = "profit_guard.tps_at_guard=true"


def _setup(monkeypatch, tmp_path, flags, short=False, boost=0.01):
    b = mkbot(monkeypatch, tmp_path, flags)
    st = state(size=115.50, reqs=[req("R110.33", 110.33), req("R110.63", 110.63), req("PT", 112.0), req("RA", 100.0, "increase")])
    if short:
        st["positions"] = {"long": {}, "short": dict(st["positions"]["long"], price=106.0)}
        b.local.side = SHORT
    st["positions"]["short" if short else "long"]["borrowFeeUsd"] = 0.0
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", [x["pubkey"] for x in r])) or True
    b.local.partials = [{"pubkey": "PT", "px": 112.0, "size": 10.0}]
    b.local.partials_done = [{"pubkey": "old"}]
    pos = b.chain_position(st)
    b.local.bank = bankmod.new_bank("SOL", 1.0)
    eq = b.hwm_equity(pos, st, 106.6)   # target = equity now x (1 + boost): guard not yet met
    b.local.bank["trade_start_equity"] = eq * (1 + boost) / (1 + b.cfg.profit_guard.min_profit_pct_trade)
    return b, st, pos


def _placed(b):
    return [a["triggerPriceUsd"] for n, a in b.calls if n == "decrease"]


def test_default_off():
    from config import ProfitGuardConfig
    assert ProfitGuardConfig().tps_at_guard is False


def test_guard_price_long_equity_equals_target(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    gp = b.guard_price(pos, st)
    assert gp > 106.6
    assert abs(b.hwm_equity(pos, st, gp) - b.guard_target(st)) < 1e-9


def test_guard_price_short_equity_equals_target(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], short=True)
    assert pos.side == SHORT
    gp = b.guard_price(pos, st)
    assert gp < 106.6
    assert abs(b.hwm_equity(pos, st, gp) - b.guard_target(st)) < 1e-9


def test_guard_price_none_when_met(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], boost=-0.01)
    assert b.guard_price(pos, st) is None


def test_tps_placed_at_guard_price_after_partial_resting_add_kept(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    gp = b.guard_price(pos, st)
    b.local.tp_px = [110.33, 110.63]
    b.resize_tps(pos)
    mins, _ = b.tp_fee_mins(pos, st)
    want = max(gp, mins[0]), max(round(gp, 2) + b.cfg.tp.tp_min_gap_usd, gp, mins[1])
    got = _placed(b)
    assert abs(got[0] - want[0]) < 0.01 and got[1] >= got[0] + b.cfg.tp.tp_min_gap_usd - 1e-9 and got[1] - got[0] < 1.0
    assert b.calls[0] == ("cancel", ["R110.33", "R110.63"])   # never PT (TP3 partial) nor RA (resting add)
    assert not b.local.tp_retry


def test_clamped_to_fee_minimum(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], boost=0.00001)   # guard price barely beyond market
    mins, _ = b.tp_fee_mins(pos, st)
    b.sync_tp_ladder(pos)
    got = _placed(b)
    assert got[0] >= mins[0] - 0.005 and got[1] >= mins[1] - 0.005


def test_guard_met_places_at_fee_floors(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], boost=-0.01)
    mins, _ = b.tp_fee_mins(pos, st)
    b.sync_tp_ladder(pos)
    got = _placed(b)
    assert abs(got[0] - round(mins[0], 2)) < 0.01 and got[1] >= mins[1] - 0.005


def test_flag_off_unchanged(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON)
    b.local.tp_px = [110.33, 110.63]
    b.resize_tps(pos)
    assert b.calls[0] == ("cancel", ["R110.33", "R110.63"])
    assert _placed(b) == [110.33, 110.63]
    assert b.guard_sole_note(pos, st, 106.6, 1.0, 1.1) == ""


def test_exception_sets_tp_retry_no_halt(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])

    def boom(*a):
        raise RuntimeError("boom")
    b.guard_price = boom
    b.resize_tps(pos)
    assert b.local.tp_retry and not b.kill.exists() and b.calls == []   # computed before cancel: nothing cancelled


def test_guard_note_shows_target_and_tp_price(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    n = b.guard_sole_note(pos, st, 106.6, 1.0, b.guard_target(st))
    assert "TP@GUARD" in n and f"{b.guard_price(pos, st):.2f}" in n
"""Trevor 2026-10-15: [profit_guard] tps_at_guard: TP1/TP2 AT the profit-guard close price (hwm_equity == guard_target),
re-placed on every partial / add; fee-minimum floors still clamp; resting add / TP3 partials never cancelled. Default off."""
from live import bank as bankmod
from strategy import LONG, SHORT
from tests.test_resting import ON, mkbot, req, state

FLAG = "profit_guard.tps_at_guard=true"


def _setup(monkeypatch, tmp_path, flags, short=False, boost=0.01):
    b = mkbot(monkeypatch, tmp_path, flags)
    st = state(size=115.50, reqs=[req("R110.33", 110.33), req("R110.63", 110.63), req("PT", 112.0), req("RA", 100.0, "increase")])
    if short:
        st["positions"] = {"long": {}, "short": dict(st["positions"]["long"], price=106.0)}
        b.local.side = SHORT
    st["positions"]["short" if short else "long"]["borrowFeeUsd"] = 0.0
    b.chain = lambda: st
    b.cancel_reqs = lambda s, r: b.calls.append(("cancel", [x["pubkey"] for x in r])) or True
    b.local.partials = [{"pubkey": "PT", "px": 112.0, "size": 10.0}]
    b.local.partials_done = [{"pubkey": "old"}]
    pos = b.chain_position(st)
    b.local.bank = bankmod.new_bank("SOL", 1.0)
    eq = b.hwm_equity(pos, st, 106.6)   # target = equity now x (1 + boost): guard not yet met
    b.local.bank["trade_start_equity"] = eq * (1 + boost) / (1 + b.cfg.profit_guard.min_profit_pct_trade)
    return b, st, pos


def _placed(b):
    return [a["triggerPriceUsd"] for n, a in b.calls if n == "decrease"]


def test_default_off():
    from config import ProfitGuardConfig
    assert ProfitGuardConfig().tps_at_guard is False


def test_guard_price_long_equity_equals_target(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    gp = b.guard_price(pos, st)
    assert gp > 106.6
    assert abs(b.hwm_equity(pos, st, gp) - b.guard_target(st)) < 1e-9


def test_guard_price_short_equity_equals_target(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], short=True)
    assert pos.side == SHORT
    gp = b.guard_price(pos, st)
    assert gp < 106.6
    assert abs(b.hwm_equity(pos, st, gp) - b.guard_target(st)) < 1e-9


def test_guard_price_none_when_met(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], boost=-0.01)
    assert b.guard_price(pos, st) is None


def test_tps_placed_at_guard_price_after_partial_resting_add_kept(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    gp = b.guard_price(pos, st)
    b.local.tp_px = [110.33, 110.63]
    b.resize_tps(pos)
    mins, _ = b.tp_fee_mins(pos, st)
    want = max(gp, mins[0]), max(round(gp, 2) + b.cfg.tp.tp_min_gap_usd, gp, mins[1])
    got = _placed(b)
    assert abs(got[0] - want[0]) < 0.01 and got[1] >= got[0] + b.cfg.tp.tp_min_gap_usd - 1e-9 and got[1] - got[0] < 1.0
    assert b.calls[0] == ("cancel", ["R110.33", "R110.63"])   # never PT (TP3 partial) nor RA (resting add)
    assert not b.local.tp_retry


def test_clamped_to_fee_minimum(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], boost=0.00001)   # guard price barely beyond market
    mins, _ = b.tp_fee_mins(pos, st)
    b.sync_tp_ladder(pos)
    got = _placed(b)
    assert got[0] >= mins[0] - 0.005 and got[1] >= mins[1] - 0.005


def test_guard_met_places_at_fee_floors(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG], boost=-0.01)
    mins, _ = b.tp_fee_mins(pos, st)
    b.sync_tp_ladder(pos)
    got = _placed(b)
    assert abs(got[0] - round(mins[0], 2)) < 0.01 and got[1] >= mins[1] - 0.005


def test_flag_off_unchanged(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON)
    b.local.tp_px = [110.33, 110.63]
    b.resize_tps(pos)
    assert b.calls[0] == ("cancel", ["R110.33", "R110.63"])
    assert _placed(b) == [110.33, 110.63]
    assert b.guard_sole_note(pos, st, 106.6, 1.0, 1.1) == ""


def test_exception_sets_tp_retry_no_halt(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])

    def boom(*a):
        raise RuntimeError("boom")
    b.guard_price = boom
    b.resize_tps(pos)
    assert b.local.tp_retry and not b.kill.exists() and b.calls == []   # computed before cancel: nothing cancelled


def test_guard_note_shows_target_and_tp_price(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    n = b.guard_sole_note(pos, st, 106.6, 1.0, b.guard_target(st))
    assert "TP@GUARD" in n and f"{b.guard_price(pos, st):.2f}" in n


# ---- 2026-10-15: stale-bank deferral (BTC 07:40 incident: TPs computed before the partial's WBTC payout was credited)
def _btc_bank(b, pay_wait=0, wallet=None):
    bk = bankmod.new_bank("BTC", 0.001)
    bk["trade_start_equity"] = b.local.bank["trade_start_equity"]
    bk["pay_wait"] = pay_wait
    b.local.bank = bk
    return bk


def _tok(st, coin_amt):
    st["sweepTokens"] = {"WBTC": {"atoms": int(round(coin_amt * 1e8))}}


def test_defer_while_payout_pending_keeps_tps(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    bk = _btc_bank(b, pay_wait=20)
    _tok(st, bk["bal"])
    b.local.tp_px = [110.33, 110.63]
    b.resize_tps(pos)
    assert b.calls == [] and bk["tp_defer"] == 1 and not b.kill.exists()   # nothing cancelled, nothing placed


def test_defer_when_wallet_differs_from_bank(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    bk = _btc_bank(b)
    _tok(st, bk["bal"] + 0.000154216)
    b.sync_tp_ladder(pos)
    assert b.calls == [] and bk["tp_defer"] == 1


def test_replaced_after_credit_and_defer_cleared(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    bk = _btc_bank(b, pay_wait=20)
    _tok(st, bk["bal"])
    b.sync_tp_ladder(pos)
    assert b.calls == []
    bk["pay_wait"] = 0   # credit landed
    b.GUARD_FAR_PCT = 100.0   # (plausibility is tested separately)
    b.sync_tp_ladder(pos)
    assert b.calls[0][0] == "cancel" and len(_placed(b)) == 2 and "tp_defer" not in bk


def test_no_defer_without_resting_tps(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    bk = _btc_bank(b, pay_wait=20)
    _tok(st, bk["bal"])
    st["openRequests"] = [r for r in st["openRequests"] if r["pubkey"] in ("PT", "RA")]
    b.sync_tp_ladder(pos)
    assert len(_placed(b)) == 2   # never a window without TPs


def test_implausibly_far_tp_deferred_then_placed_after_cap(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])   # fixture guard price ~2x market
    bk = _btc_bank(b)
    _tok(st, bk["bal"])
    b.sync_tp_ladder(pos)
    assert b.calls == [] and bk["tp_defer"] == 1
    bk["tp_defer"] = b.GUARD_DEFER_MAX
    b.sync_tp_ladder(pos)
    assert len(_placed(b)) == 2 and "tp_defer" not in bk


def test_bank_payout_sets_pay_wait_and_recon_clears(monkeypatch, tmp_path):
    b, st, pos = _setup(monkeypatch, tmp_path, ON + [FLAG])
    bk = _btc_bank(b)
    b.asset = "BTC"
    b.bank_payout(10.0, "TP3 partial")
    assert bk["pay_wait"] == b.GUARD_DEFER_MAX and b.guard_bank_stale({"sweepTokens": {"WBTC": {"atoms": 100000}}})
