"""SOL guard equity uses the real payouts + network fees (Trevor 2026-10-27). On 10-27 SOL closed at its TP@guard
0.00126 SOL below the target: each partial payout was booked as an estimate ~0.0002-0.00025 SOL high and the trade's
network fees (~0.00055 SOL, 51 txs) only reached bal at the flat true-up. Now a payout booked as an estimate while the
position stays open is re-measured from the position's own on-chain flows (as the flat true-up) and bal trued up at once
(open request rent added back: it never left bal), and hwm_equity deducts the fees paid since. The 'position gone'
estimate uses the last seen whole-position avg, not the lots. Fake chain only (conftest)."""
import dataclasses

import pandas as pd
import pytest

from config import load_config
from live import bank as K
from live import ledger

SOL_PX = 100.0


def _st(reqs=("TP1", "TP2"), size=125.0, avg=100.55):
    rq = [{"pubkey": r, "requestType": "trigger", "requestChange": "decrease"} for r in reqs]
    short = {"sizeUsd": size, "collateralUsd": 12.5, "price": avg, "pubkey": "POS"} if size else {}
    return {"market": "SOL", "openRequests": list(rq), "allOpenRequests": list(rq), "oracle": {"price": SOL_PX, "ageSec": 1},
            "solOracle": {"price": SOL_PX}, "wallet": {"sol": 0.7}, "custody": {}, "positions": {"long": {}, "short": short},
            "sweepTokens": {}}


def _pos(size=125.0, avg=100.55):
    import live.bot as B
    return B.ChainPosition(side=-1, opened=pd.Timestamp.now(tz="UTC"), margin=12.5, size=size, qty=size / avg, borrow=0.0,
                           n_entries=2, last_leverage=10, first_margin=5.0, first_entry=100.36, last_entry=101.12, tp_hits=0,
                           first_tp_time=None)


@pytest.fixture
def sbot(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_100.toml", ["live.dry_run=true", f"bot.state_dir={tmp_path.name}"])
    monkeypatch.setattr(B, "ROOT", tmp_path.parent)
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)
    b.kill, b.kill_all, b.confirm_wait = tmp_path / "KILL", tmp_path / "KILL_ALL", 0.0
    b._sol_px = SOL_PX
    bk = K.new_bank("SOL", 0.70, 0.86)
    bk["trade_dep"], bk["trade_recv"], bk["res_last"], bk["res_v"] = 0.40, 0.0, 0.0, 3
    b.local.bank = bk
    b.local.side = -1
    ledger.on_open("2026-10-27 10:45:28+00:00", -1, 0.9, "OPENSIG", 550_000, position="POS")   # 0.00055 SOL of fees so far
    fl = {"v": -0.39, "dec": 1, "calls": 0}
    b._fl = fl

    def flows(pos, sig):
        fl["calls"] += 1
        return fl["v"], {"ok": True, "nTx": 51, "decreases": fl["dec"], "eventErrors": []}
    monkeypatch.setattr(B, "position_net_sol", flows)
    b._chain = {"st": _st()}
    b.chain = lambda: b._chain["st"]
    return b


def test_open_trade_equity_deducts_network_fees_paid_so_far(sbot):
    st, pos = _st(), _pos()
    eq_open = sbot.hwm_equity(pos, st, SOL_PX)
    assert sbot.sol_fees_unbooked(pos) == pytest.approx(0.00055)
    assert sbot.sol_fees_unbooked(None) == 0.0                      # flat: the true-up already has them
    sbot.local.fee_mark_sol = 0.00055
    assert sbot.hwm_equity(pos, st, SOL_PX) == pytest.approx(eq_open + 0.00055, abs=1e-12)
    ledger.update(fee_lamports=20_000)                              # a later tx: only the fees since the mark
    assert sbot.sol_fees_unbooked(pos) == pytest.approx(0.00002)


def test_estimated_partial_payout_is_trued_up_to_the_measured_flows(sbot):
    L, b = sbot.local, sbot.local.bank
    sbot.bank_payout(11.8789, "per-add partial $75.00")             # estimate 0.118789 SOL
    assert L.pay_check is True
    L.partials_done = [{"pubkey": "P1"}]
    # real: position flows -0.40 (collateral) + 0.11855 (payout really received) - 0.00055 (fees) - 2 open TP requests' rent
    sbot._fl["v"] = round(-0.40 + 0.11855 - 0.00055 - 2 * K.REQ_RENT, 9)
    bal0, eq0 = b["bal"], sbot.hwm_equity(_pos(), _st(), SOL_PX)
    sbot.sol_pay_trueup(_st(), _pos())
    adj = (0.11855 - 0.00055) - 0.118789                            # real payout - fees - estimate
    assert b["bal"] == pytest.approx(bal0 + adj, abs=1e-9) and b["hist"][-1]["kind"] == "payout_trueup"
    assert b["res_last"] == pytest.approx(-adj, abs=1e-9)           # moved between reserve float and bal
    assert L.pay_check is False and L.fee_mark_sol == pytest.approx(0.00055)
    # equity: estimate error (0.000239 high) and the fees already deducted before -> only the payout error is new
    assert sbot.hwm_equity(_pos(), _st(), SOL_PX) == pytest.approx(eq0 - (0.118789 - 0.11855), abs=1e-9)
    sbot.bank_flat(sbot._fl["v"] + 2 * K.REQ_RENT)                  # flat: requests refunded, true-up has nothing left
    assert b["hist"][-1]["kind"] == "flat_trueup" and abs(b["hist"][-1]["amt"]) < 1e-9


def test_true_up_waits_for_a_consistent_snapshot(sbot):
    L, b = sbot.local, sbot.local.bank
    sbot.bank_payout(11.8789, "TP fill $62.50")
    L.tp_hits = 1
    L.partials_done = [{"pubkey": "P1"}]
    bal = b["bal"]
    sbot._fl["dec"] = 1                                             # 2 decreases known, the flows show 1: index lagging
    sbot.sol_pay_trueup(_st(), _pos())
    assert L.pay_check and b["bal"] == bal
    sbot._fl["dec"] = 2
    sbot._chain["st"] = _st(reqs=("TP1",))                          # a request went during the read
    sbot.sol_pay_trueup(_st(), _pos())
    assert L.pay_check and b["bal"] == bal
    sbot._chain["st"] = _st()
    L.pending_cancels = [{"pubkey": "X"}]                           # a cancel in flight: not measured
    sbot.sol_pay_trueup(_st(), _pos())
    assert L.pay_check and b["bal"] == bal and sbot._fl["calls"] == 2
    L.pending_cancels = []
    sbot._fl["v"] = None                                            # flows unavailable
    sbot.sol_pay_trueup(_st(), _pos())
    assert L.pay_check and b["bal"] == bal
    sbot._fl["v"] = -0.3
    sbot.sol_pay_trueup(_st(), _pos())
    assert not L.pay_check and b["bal"] != bal


def test_close_at_tp_guard_price_meets_the_target_after_the_true_up(sbot):
    """The guard price is found on hwm_equity: with the real payout + fees it sits further into profit, so a close
    there (real money = the corrected bal + close value) reaches the target."""
    L, b = sbot.local, sbot.local.bank
    st, pos = _st(), _pos()
    sbot.bank_payout(11.8789, "per-add partial $75.00")
    L.partials_done = [{"pubkey": "P1"}]
    pct = sbot.cfg.profit_guard.min_profit_pct_trade
    b["trade_start_equity"] = (sbot.hwm_equity(pos, st, SOL_PX) + 0.002) / (1 + pct)   # target 0.002 SOL above "now"
    gp_est = sbot.guard_price(pos, st)
    sbot._fl["v"] = round(-0.40 + 0.11855 - 0.00055 - 2 * K.REQ_RENT, 9)
    sbot.sol_pay_trueup(st, pos)
    gp = sbot.guard_price(pos, st)
    assert gp is not None and gp_est is not None and gp < gp_est    # short: further into profit
    assert sbot.hwm_equity(pos, st, gp) >= sbot.guard_target(st) - 1e-9


def test_non_sol_bots_unchanged(sbot):
    sbot.asset = "ETH"
    assert sbot.sol_fees_unbooked(_pos()) == 0.0
    sbot.local.pay_check = True
    sbot.sol_pay_trueup(_st(), _pos())
    assert sbot._fl["calls"] == 0


class _Stop(Exception):
    pass


def test_position_gone_estimate_uses_the_whole_position_avg(sbot, monkeypatch):
    L = sbot.local
    L.lots = [{"time": "t0", "price": 100.0531, "size": 50.0, "first": True}]   # only the first lot left after the partials
    L.pg_m_size, L.pg_margin, L.pg_avg, L.tp_px, L.tp_hits = 50.0, 5.0, 100.55, [99.77, 99.77], 0
    seen = []

    def pay(usd, why):
        seen.append(usd)
        raise _Stop
    monkeypatch.setattr(sbot, "bank_payout", pay)
    sbot._chain["st"] = _st(reqs=(), size=0)
    with pytest.raises(_Stop):
        sbot.tick()
    c = sbot.cfg.costs.close_fee
    assert seen[0] == pytest.approx(5.0 + 50.0 * (100.55 - 99.77) / 100.55 - c * 50.0)   # was (100.0531 - 99.77) / 100.0531
