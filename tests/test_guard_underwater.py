from live import bank as bankmod


def _bot(flag):
    return NS(cfg=NS(ledger=NS(guard_only_underwater=flag)))


def test_withdrawal_drops_hwm_and_resets_wd_mark():
    b = {"bal": 0.80, "hwm": 0.7879, "wd_mark": 0.7785, "totals": {}}
    bankmod.apply_withdrawal(b, 0.0047, "daily")
    assert b["hwm"] == 0.7832 and b["bal"] == 0.7953 and b["wd_mark"] == 0.7832
    assert b["totals"]["withdrawn"] == 0.0047
