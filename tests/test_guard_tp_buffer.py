"""Trevor 2026-10-28 09:36 MT: guard TP re-place loop (ETH 2026-10-28 03:35-06:35 MT). Every guard-drift re-place paid ~0.000024
SOL of network fees into the fee debt, which moved the short guard price ~1.6 cents lower and left the just-placed TPs one tick on
the wrong side -> re-placed every 10 min (2340.18, 2340.16, ... 2339.99). Fix: guard TPs sit GUARD_TP_BUFFER_SOL of fees beyond
the guard target; the drift check still compares with the un-buffered guard price. Real equity model (ETH bot), only the fee
floors / payout check / re-place itself are faked."""
import pytest

from strategy import LONG, SHORT
from tests.test_bank import _arm, _st, ebot  # noqa: F401 - fixture

REPLACE_FEE_SOL = 0.000024   # one guard re-place (2 cancels + 2 places), ETH 2026-10-28 03:30-04:30 MT fee-debt steps


def _setup(ebot, monkeypatch, side):
    b = ebot.local.bank
    _arm(ebot, SHORT if side == "short" else LONG)
    price = 2369.4 if side == "short" else 2351.7
    st = _st(side=side, size=350.0, coll=27.68, avg=2360.557, price=price, reqs=())
    st["solOracle"] = {"price": 102.689}
    pos = ebot.chain_position(st)
    b["bal"], b["fee_sol"], b["hwm"] = 0.0356, 0.001318, 0.0525
    ebot.local.partials_done = [{"pubkey": "old"}]
    # target = equity at the live-like guard price (ETH short: 2340.18; long mirrored)
    tp = 2340.18 if side == "short" else 2380.93
    b["trade_start_equity"] = ebot.hwm_equity(pos, st, tp) / (1 + ebot.cfg.profit_guard.min_profit_pct_trade)
    monkeypatch.setattr(ebot, "guard_tps", lambda: True)
    monkeypatch.setattr(ebot, "guard_bank_stale", lambda st: False)
    far = 1e9 if side == "short" else 1.0      # fee floors never bind: the guard price decides
    monkeypatch.setattr(ebot, "tp_fee_mins", lambda pos, st: ([far, far], 0.0))
    ebot.replaced = []

    def fake_sync(p):   # the real placement price; then the re-place's own network fees land in the fee debt
        px, _ = ebot.guard_tp_px(p, st, verbose=False)
        ebot.local.tp_px = [px, px]
        st["openRequests"] = [{"pubkey": f"R{i}", "triggerPrice": px, "executed": False, "requestType": "trigger",
                               "requestChange": "decrease"} for i in (1, 2)]
        b["fee_sol"] = round(b["fee_sol"] + REPLACE_FEE_SOL, 9)
        ebot.replaced.append(px)
    monkeypatch.setattr(ebot, "sync_tp_ladder", fake_sync)
    return st, pos


def _run(ebot, st, pos, ticks):
    for _ in range(ticks):
        ebot._guard_drift_until = 0.0   # every check past the 10-min cooldown
        ebot.guard_drift_check(pos, st)


@pytest.mark.usefixtures("guard_eq_from_bank")
@pytest.mark.parametrize("side", ["short", "long"])
def test_old_behaviour_loops_without_buffer(ebot, monkeypatch, side):
    """Reproduction: with no buffer every re-place's own fee pushes the guard past the TPs it just placed."""
    st, pos = _setup(ebot, monkeypatch, side)
    monkeypatch.setattr(ebot, "GUARD_TP_BUFFER_SOL", 0.0)
    ebot.sync_tp_ladder(pos)
    _run(ebot, st, pos, 10)
    assert len(ebot.replaced) >= 6
    steps = [pos.side * (b - a) for a, b in zip(ebot.replaced, ebot.replaced[1:])]
    assert all(0 < s <= 0.03 for s in steps)   # cent-sized, fee-only moves


@pytest.mark.usefixtures("guard_eq_from_bank")
@pytest.mark.parametrize("side", ["short", "long"])
def test_fee_only_drift_no_longer_replaces(ebot, monkeypatch, side):
    st, pos = _setup(ebot, monkeypatch, side)
    ebot.sync_tp_ladder(pos)                    # initial guard placement (TP3 fill)
    _run(ebot, st, pos, 20)
    assert len(ebot.replaced) == 1              # its own fees never trigger the next re-place
    # profit safety: a fill at the resting TP still meets the guard target after the re-place fees were booked
    px = ebot.local.tp_px[0]
    assert ebot.hwm_equity(pos, st, px) >= ebot.guard_target(st)
    gp = ebot.guard_price(pos, st)
    assert pos.side * (px - gp) > 0             # on the safe side of the live guard price (short below, long above)


@pytest.mark.usefixtures("guard_eq_from_bank")
@pytest.mark.parametrize("side", ["short", "long"])
def test_buffer_on_safe_side_and_rounded_against_itself(ebot, monkeypatch, side):
    st, pos = _setup(ebot, monkeypatch, side)
    px, gp = ebot.guard_tp_px(pos, st, verbose=False)
    gpb = ebot.guard_price(pos, st, ebot.guard_tp_buffer(st))
    assert ebot.guard_tp_buffer(st) == pytest.approx(0.0001 * 102.689 / st["oracle"]["price"])
    assert pos.side * (gpb - gp) > 0            # buffer moves the TP further out, never closer
    assert px == ebot.guard_round(pos.side, gpb) and pos.side * (px - gpb) >= -1e-9
    assert ebot.hwm_equity(pos, st, px) >= ebot.guard_target(st) + ebot.guard_tp_buffer(st) - 1e-12
    assert 0.03 < abs(px - gp) < 0.15           # ETH ($350 short): a few re-places' fees, a few cents


@pytest.mark.usefixtures("guard_eq_from_bank")
@pytest.mark.parametrize("side", ["short", "long"])
def test_real_drift_still_replaces(ebot, monkeypatch, side):
    st, pos = _setup(ebot, monkeypatch, side)
    ebot.sync_tp_ladder(pos)
    first = ebot.local.tp_px[0]
    _run(ebot, st, pos, 3)
    assert len(ebot.replaced) == 1
    ebot.local.bank["fee_sol"] += 0.0005        # drift well past the buffer (e.g. borrow / fees accrued over hours)
    assert pos.side * (ebot.guard_round(pos.side, ebot.guard_price(pos, st)) - first) > 0.005
    _run(ebot, st, pos, 1)
    assert len(ebot.replaced) == 2
    px = ebot.local.tp_px[0]
    assert pos.side * (px - first) > 0          # followed the guard further out (short lower, long higher)
    assert ebot.hwm_equity(pos, st, px) >= ebot.guard_target(st)
    _run(ebot, st, pos, 10)
    assert len(ebot.replaced) == 2              # and no loop after it
