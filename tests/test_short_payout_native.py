"""2026-10-13: short_payout_native (default OFF) adds desiredMint = market coin to ETH/BTC short decreases."""
import dataclasses

import live.bot as B
from live import bank as bankmod
from tests.test_resting import mkbot


def run(monkeypatch, tmp_path, asset, flag, side="short"):
    b = mkbot(monkeypatch, tmp_path, [])
    b.asset = asset
    b.live = dataclasses.replace(b.live, short_payout_native=flag)
    seen = []
    monkeypatch.setattr(B, "jup", lambda name, a: seen.append(a) or {"ok": True})
    B.Bot.cmd(b, "decrease", {"side": side, "entire": True})
    return seen[0]


def test_off_by_default_and_flag_on(monkeypatch, tmp_path):
    assert "desiredMint" not in run(monkeypatch, tmp_path, "ETH", False)
    assert run(monkeypatch, tmp_path, "ETH", True)["desiredMint"] == bankmod.MINT["ETH"]
    assert run(monkeypatch, tmp_path, "BTC", True)["desiredMint"] == bankmod.MINT["WBTC"]
    assert "desiredMint" not in run(monkeypatch, tmp_path, "SOL", True)
    assert "desiredMint" not in run(monkeypatch, tmp_path, "ETH", True, side="long")
