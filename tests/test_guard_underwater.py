from types import SimpleNamespace as NS

from live import bank as bankmod
from live.bot import Bot


def _bot(flag):
    return NS(cfg=NS(ledger=NS(guard_only_underwater=flag)))


def test_guard_only_underwater_long_and_short():
    long_, short = NS(side=1, avg_entry=100.0), NS(side=-1, avg_entry=100.0)
    g = Bot.guard_allowed
    assert g(_bot(True), long_, 99.9) and not g(_bot(True), long_, 100.0) and not g(_bot(True), long_, 101.0)
    assert g(_bot(True), short, 100.1) and not g(_bot(True), short, 100.0) and not g(_bot(True), short, 99.0)
    assert g(_bot(False), long_, 101.0)


def test_live_configs_guard_only_underwater():
    from config import load_config
    for f in ("live_100", "live_eth", "live_btc"):
        assert load_config(f"configs/{f}.toml").ledger.guard_only_underwater is True


def test_withdrawal_drops_hwm_and_resets_wd_mark():
    b = {"bal": 0.80, "hwm": 0.7879, "wd_mark": 0.7785, "totals": {}}
    bankmod.apply_withdrawal(b, 0.0047, "daily")
    assert b["hwm"] == 0.7832 and b["bal"] == 0.7953 and b["wd_mark"] == 0.7832
    assert b["totals"]["withdrawn"] == 0.0047
