"""JupSOL rescue loan booking (Trevor 2026-10-11; 2026-10-27: exact JupSOL repayment, collateral-only rescue): HWM
untouched, repayment, settings restore, relief maths."""
import pytest

from live import bank as K
from live import rescue as R


def _b():
    return K.new_bank("SOL", 1.0, hwm=1.2)


def test_loan_does_not_raise_hwm():
    b = _b()
    K.loan_open(b, 0.5, 0.42, 5.0, "t")
    assert b["bal"] == pytest.approx(1.5) and K.loan_outstanding(b) == pytest.approx(0.5)
    assert not K.update_hwm(b, flat=True)          # 1.5 - 0.5 = 1.0 < 1.2
    assert b["hwm"] == 1.2 and b["wd_mark"] == 1.2
    assert K.equity(b, 0.0) == pytest.approx(1.0)  # profit guard sees equity net of the loan


def test_loan_excluded_from_withdrawal():
    b = _b()
    b["wd_mark"] = 0.0
    K.loan_open(b, 5.0, 4.0, 5.0)
    amt, _ = K.withdraw_amount(b, 1.0, 0.0)        # gain 1.2 > own bal 1.0 -> nothing (loan coin not withdrawable)
    assert amt == 0.0


def test_repay_exact_jupsol_and_hwm():
    """2026-10-27 (Trevor): the exact JupSOL quantity lent is repaid; no 5 % margin, no profit share."""
    b = _b()
    K.loan_open(b, 0.5, 0.42, 5.0)
    b["bal"] += 0.3                                # trading profit while the loan was out
    assert R.repay_due(b, {**R.DEFAULTS, "repay_margin_pct": 5.0}) == pytest.approx(0.42)   # JupSOL owed; margin ignored
    K.loan_repay_jupsol(b, 0.5, 0.42)              # same JupSOL price: 0.5 SOL buys the 0.42 JupSOL back
    assert K.loan_outstanding(b) == 0 and b["bal"] == pytest.approx(1.3)
    assert K.update_hwm(b, flat=True) and b["hwm"] == pytest.approx(1.3)
    assert b["loans_done"][-1]["repaid"] == pytest.approx(0.5) and b["loans_done"][-1]["repaid_jupsol"] == pytest.approx(0.42)


def test_no_profit_share():
    b = _b()
    K.loan_open(b, 0.5, 0.42, 5.0)
    b["bal"] += 0.4
    assert R.repay_due(b, {**R.DEFAULTS, "repay_margin_pct": 0.0, "repay_profit_share": 0.5}) == pytest.approx(0.42)


def test_extend_and_restore(tmp_path, monkeypatch):
    cfgdir = tmp_path / "configs"; cfgdir.mkdir()
    (cfgdir / "live_100.toml").write_text("[dca]\nladder_max_rungs = 14          # orig\nx = 1\n")
    monkeypatch.setattr(R, "ROOT", tmp_path)
    b = _b()
    assert R.extend("sol", b, 6) == (14, 20) and R.get_rungs("sol") == 20
    R.extend("sol", b, 2)
    assert b["rescue_settings"]["dca.ladder_max_rungs"] == 14
    assert R.restore_settings("sol", b) == ["ladder_max_rungs -> 14"]
    assert R.get_rungs("sol") == 14 and "rescue_settings" not in b
    assert "x = 1" in (cfgdir / "live_100.toml").read_text()


def test_liq_and_plan():
    c = dict(R.DEFAULTS)
    # short $100 @ 100 with $10 collateral: liq = 100 x (1 - 0.01 + 0.1) = 109
    assert R.liq_price(-1, 100, 100, 10) == pytest.approx(109)
    p = R.plan(-1, 100, 100, 10, 107, 50, c)       # 1.9% away -> in trigger
    assert p["in_trigger"] and not p["emergency"] and p["new_dist_pct"] > p["dist_pct"]
    assert p["collateral_usd"] == pytest.approx(min(p["need_usd"], 50 * 0.99 / 3))   # collateral only, max 1/3 of JupSOL
    e = R.plan(-1, 100, 100, 10, 107, 1.0, c)      # $1 of JupSOL -> EMERGENCY
    assert e["emergency"]
    lg = R.plan(1, 100, 100, 10, 92, 50, c)        # long: liq 91
    assert lg["liq"] == pytest.approx(91) and lg["new_liq"] < 91
