import pytest

from config import load_config
from live import bank as bankmod


def test_min_native_formula():
    m = bankmod.withdraw_min_native(0.003, 0.0001, 2.0, 110.0, 2500.0)
    usd = m * 2500.0
    assert usd == pytest.approx(2.0 * (usd * 0.003 + 0.0001 * 110.0))
    assert bankmod.withdraw_min_native(0.6, 0.0, 2.0, 1, 1) == float("inf")


def test_small_amount_now_sent_tiny_carried():
    b = {"bal": 1.0, "hwm": 1.0, "wd_mark": 0.99, "totals": {}}
    m = bankmod.withdraw_min_native(0.001, 0.00005, 2.0, 110.0, 110.0)
    amt, _ = bankmod.withdraw_amount(b, 0.5, m)           # 0.005 SOL ~ $0.55... and $0.30 now also passes
    assert amt == pytest.approx(0.005)
    b2 = {"bal": 1.0, "hwm": 1.0, "wd_mark": 1.0 - 2 * m * 0.9, "totals": {}}
    assert bankmod.withdraw_amount(b2, 0.5, m)[0] == 0.0


@pytest.mark.parametrize("f", ["live_100", "live_eth", "live_btc"])
def test_configs(f):
    lc = load_config(f"configs/{f}.toml").ledger
    assert lc.withdraw_fee_factor == 2.0 and lc.withdraw_cost_pct > 0 and lc.withdraw_network_sol > 0
