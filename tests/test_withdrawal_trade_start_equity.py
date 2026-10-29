from live import bank as bankmod


def test_withdrawal_lowers_open_trade_start_equity():
    b = {"bal": 0.0160, "hwm": 0.0155, "wd_mark": 0.0150, "trade_start_equity": 0.0155, "totals": {}}
    bankmod.apply_withdrawal(b, 0.000543, "daily")
    assert b["trade_start_equity"] == round(0.0155 - 0.000543, 9)
    assert b["hwm"] == b["trade_start_equity"]


def test_withdrawal_without_trade_start_equity_leaves_it_unset():
    b = {"bal": 0.80, "hwm": 0.78, "wd_mark": 0.77, "totals": {}}
    bankmod.apply_withdrawal(b, 0.004, "daily")
    assert "trade_start_equity" not in b
