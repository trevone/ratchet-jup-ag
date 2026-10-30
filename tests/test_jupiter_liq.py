"""Trevor 2026-10-29 21:59 MT: ladder liq-buffer check uses Jupiter Perps' liquidation formula
liq = avg -/+ (collateral - close_fee - borrow_fee - size/max_lev) * avg / size  (max_lev 500, close fee 6 bps)."""
import pytest

from config import load_config
from dca_levels import _SimPos, liq_price, plan_add
from strategy import DcaStrategy

LIVE = ("configs/live_100.toml", "configs/live_eth.toml", "configs/live_btc.toml")


def jup_doc(side, avg, coll, size, close_fee=0.0006, borrow=0.0, max_lev=500.0):
    return avg - side * (coll - close_fee * size - borrow - size / max_lev) * avg / size


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("borrow", [0.0, 0.37])
def test_liq_price_matches_jupiter_docs(side, borrow):
    size, avg, coll = 918.75, 102.32, 72.95
    got = liq_price(side, size, size / avg, coll, borrow, 1 / 500 + 0.0006)
    assert got == pytest.approx(jup_doc(side, avg, coll, size, borrow=borrow), rel=1e-12)


@pytest.mark.parametrize("path", LIVE)
def test_live_uses_jupiter_model(path):
    s = DcaStrategy(load_config(path))
    assert s.cfg.risk.liq_model == "jupiter" and s.liq_maint() == pytest.approx(0.0026)
    assert s.plan_kwargs()["maint"] == pytest.approx(0.0026)
    s.liq_max_lev, s.liq_close_fee = 250.0, 0.0008          # custody values override the fallbacks
    assert s.liq_maint() == pytest.approx(1 / 250 + 0.0008)


def test_legacy_model_unchanged():
    s = DcaStrategy(load_config("configs/live_100.toml", ["risk.liq_model=legacy"]))
    assert s.liq_maint() == 0.01


def test_sol_short_hand_check_step_by_step():
    """Dry-projection hand check (21:54 MT): SOL short B $10.50 rungs 0..6, cumulative size / collateral net of the
    6 bps open fee / new avg -> Jupiter liq 110.18 after rung 6 (old 1% model 109.43)."""
    fills = [97.04, 98.45, 99.86, 101.26, 102.67, 104.08, 105.49]
    levs = [5, 7.5, 10, 12.5, 15, 17.5, 20]
    S = q = C = 0.0
    for f, l in zip(fills, levs):
        n = 10.5 * l
        S, q, C = S + n, q + n / f, C + 10.5 - n * 0.0006
    assert liq_price(-1, S, q, C, 0.0, 0.0026) == pytest.approx(110.18, abs=0.01)
    assert liq_price(-1, S, q, C, 0.0, 0.01) == pytest.approx(109.43, abs=0.01)


def test_borrow_fee_moves_liq_closer_in_the_check():
    s = DcaStrategy(load_config("configs/live_100.toml"))
    kw = s.plan_kwargs()
    kw.update(fixed_dist=1.0, liq_buffer_atr=0.0)
    p0 = _SimPos(-1, 52.5, 52.5 / 100, 10.47, 0.0, 100.0, 5.0, 100.0, 1, 10.5, 0.0)
    p1 = _SimPos(-1, 52.5, 52.5 / 100, 10.47, 0.0, 100.0, 5.0, 100.0, 1, 10.5, 2.0)
    a0, a1 = plan_add(p0, None, available_usd=1e3, **kw), plan_add(p1, None, available_usd=1e3, **kw)
    assert a1.liq < a0.liq                                    # short: accrued borrow pulls liq down toward price


def test_bot_reads_custody_and_borrow(monkeypatch, tmp_path):
    from tests.test_resting import ON, mkbot, state
    b = mkbot(monkeypatch, tmp_path, ON)
    st = state()
    st["custody"] = {"maxLeverage": 500, "decreasePositionBps": 6}
    side = "long" if st["positions"]["long"].get("sizeUsd") else "short"
    st["positions"][side]["borrowFeeUsd"] = 0.42
    pos = b.chain_position(st)
    assert pos.borrow_fee == pytest.approx(0.42) and pos.borrow == 0.0
    assert b.strategy.liq_max_lev == 500 and b.strategy.liq_close_fee == pytest.approx(0.0006)
