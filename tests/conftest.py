"""Test isolation: tests must never reach the chain. Any TS CLI / subprocess call fails, JUP_LIVE is unset, and the
ledger files, the shared sweep lock / state (live/sweep.py ROOT) are redirected to a temp dir (so the live ledger and
sweep state are never written by a test)."""
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_chain(monkeypatch, tmp_path):
    monkeypatch.delenv("JUP_LIVE", raising=False)

    def blocked(*a, **k):
        raise RuntimeError("tests must not call the TS CLI / subprocess (would touch the chain)")
    import live.live_broker as lb
    monkeypatch.setattr(lb, "jup", blocked)
    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    from live import ledger
    for name in ("TRADES", "SUMMARY", "OPEN", "STAKE"):
        monkeypatch.setattr(ledger, name, tmp_path / getattr(ledger, name).name)
    monkeypatch.setattr(ledger, "LIVE", tmp_path)
    from live import sweep   # shared sweep lock / state / price cache + the other bots' ledger dirs -> temp dir too
    monkeypatch.setattr(sweep, "ROOT", tmp_path)
    from live import bank   # per-bot ledger: shared external-event journal / lock -> temp dir
    monkeypatch.setattr(bank, "ROOT", tmp_path)
