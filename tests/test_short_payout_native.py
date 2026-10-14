"""2026-10-14: ETH / BTC short decreases always pay out the market coin (desiredMint); SOL shorts and longs unchanged."""
import live.bot as B
from live import bank as bankmod
from tests.test_resting import mkbot


def run(monkeypatch, tmp_path, asset, side="short"):
    b = mkbot(monkeypatch, tmp_path, [])
    b.asset = asset
    seen = []
    monkeypatch.setattr(B, "jup", lambda name, a: seen.append(a) or {"ok": True})
    B.Bot.cmd(b, "decrease", {"side": side, "entire": True})
    return seen[0]


def test_eth_btc_shorts_always_paid_in_own_coin(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, "ETH")["desiredMint"] == bankmod.MINT["ETH"]
    assert run(monkeypatch, tmp_path, "BTC")["desiredMint"] == bankmod.MINT["WBTC"]
    assert "desiredMint" not in run(monkeypatch, tmp_path, "SOL")
    assert "desiredMint" not in run(monkeypatch, tmp_path, "ETH", side="long")
