"""Trevor 2026-10-30 05:51 MT: 'If a tp3 is taken then it should only close with profit guard.'"""
import pandas as pd

import live.bot as B
from tests.test_resting import ON, mkbot


def test_flag_default_off_and_tp1_only_unchanged(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.local.tp_hits, b.local.first_tp_time = 1, str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=60))
    assert b.timer_blocked_tp3() is False


def test_tp3_fill_disables_timer(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.local.partials_done = [{"px": 96.0, "size": 10.0}]
    assert b.timer_blocked_tp3() is True and b.local.tp3_filled is True


def test_flag_survives_restart_and_resets_on_new_trade(tmp_path):
    p = tmp_path / "state.json"
    import json
    from dataclasses import asdict
    L = B.Local(side=-1, tp3_filled=True)
    p.write_text(json.dumps(asdict(L)))
    assert B.load_local(p).tp3_filled is True
    assert B.Local().tp3_filled is False


def test_backtest_mirror():
    from broker import Position
    from config import load_config
    from strategy import DcaStrategy
    import inspect
    assert Position(side=1, opened=pd.Timestamp.now()).tp3_filled is False
    assert 'getattr(pos, "tp3_filled", False)' in inspect.getsource(DcaStrategy.exit_intents)
