"""USDC attribution by pending-payout claims (Trevor 2026-10-14). Regression for 2026-10-13 04:31 MT: the ETH short
closed (tp_timer) and paid $15.26 USDC while the BTC short was still open; the old rule (open short first, ETH > BTC)
handed it to the BTC bot, which swapped it into WBTC and booked it as its own payout. Every Jupiter call is a fake."""
import dataclasses
import json

import pytest

from config import load_config
from live import bank as K
from live import ledger, sweep as sw

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T0 = 1_791_887_462.0          # 2026-10-13 10:31:02 UTC (04:31:02 MT)
OK_WBTC = {"ok": True, "sig": "SWAPBTC", "deltas": {"nativeDeltaLamports": -6413, "tokenDeltaAtoms": {"WBTC": "18333"}}}
OK_ETH = {"ok": True, "sig": "SWAPETH", "deltas": {"nativeDeltaLamports": -6000, "tokenDeltaAtoms": {"ETH": "608000"}}}


class Fake:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, args):
        self.calls.append((cmd, args))
        return self.results.pop(0) if self.results else {"ok": False, "error": "unexpected"}


def st(usdc, positions=(), px=83000.0, read_at=None):
    return {"sweepTokens": {"USDC": {"mint": USDC, "atoms": str(usdc)}}, "allPositions": list(positions),
            "oracle": {"price": px}, "allOpenRequests": [], "openRequests": [], "_read_at": read_at or T0 - 1}


def heal(me, s, fake, now):
    cfg = load_config({"ETH": "configs/live_eth.toml", "BTC": "configs/live_btc.toml", "SOL": "configs/live_100.toml"}[me])
    return sw.self_heal_own(cfg, me, s, fake, dry_run=False, min_usd=0.10, clock=lambda: now, sleep=lambda x: None,
                            lock_wait=0)


def open_rec(asset, side):
    d = sw.ROOT / sw.BOT_DIRS[asset]
    d.mkdir(parents=True, exist_ok=True)
    (d / "ledger_open.json").write_text(json.dumps({"side": side, "open_sig": f"{asset}OPEN", "position": f"{asset}POS",
                                                    "open_time": "2026-10-13 02:00:32+00:00", "exits": []}))


BTC_SHORT = [{"market": "BTC", "side": "short"}]


def test_oct13_eth_close_usdc_while_btc_short_open_is_never_given_to_btc():
    open_rec("ETH", "short")                  # ETH closed on-chain, not booked yet
    open_rec("BTC", "short")                  # BTC short still open
    sw.post_claim("ETH", None, "decrease sent", now=T0 - 5)
    usdc = st(15_257_304, BTC_SHORT)
    fake = Fake(OK_WBTC)
    assert heal("BTC", usdc, fake, T0) == [] and fake.calls == []       # ambiguous: left unswept, no guess
    assert "USDC" not in sw.load_state()["tokens"]
    sw.post_claim("ETH", 15.2221, "close (tp_timer)", now=T0 + 6)        # the ETH bot's payout event
    assert heal("BTC", {**usdc, "_read_at": T0 + 30}, fake, T0 + 31) == [] and fake.calls == []   # ETH's, left to it
    assert sw.load_state()["tokens"]["USDC"]["claim"]["owner"] == "ETH"
    efake = Fake(OK_ETH)
    out = heal("ETH", {**usdc, "_read_at": T0 + 32, "oracle": {"price": 2501.0}}, efake, T0 + 33)
    assert len(efake.calls) == 1 and out[0]["coin"] == "ETH" and out[0]["native"] == pytest.approx(0.00608)
    assert "ETH" not in sw.load_claims(T0 + 34)                         # claim cleared by the sweep
    # then the BTC short's own close: its claim matches its own USDC
    sw.post_claim("BTC", None, "decrease sent", now=T0 + 140)
    sw.post_claim("BTC", 38.7399, "close (profit_guard)", now=T0 + 150)
    bfake = Fake(OK_WBTC)
    out = heal("BTC", st(38_960_302, (), read_at=T0 + 160), bfake, T0 + 161)
    assert len(bfake.calls) == 1 and out[0]["coin"] == "WBTC"


def test_two_open_shorts_without_claim_is_ambiguous_and_sol_never_sweeps_it():
    s = st(1_000_000, [{"market": "BTC", "side": "short"}, {"market": "ETH", "side": "short"}])
    c = sw.attribute("USDC", "SOL", s, 1.0, T0)
    assert c["owner"] is None and c.get("ambiguous")
    fake = Fake()
    assert heal("SOL", s, fake, T0) == [] and heal("ETH", s, fake, T0) == [] and fake.calls == []


def test_single_candidate_and_claim_mismatch():
    assert sw.attribute("USDC", "SOL", st(1, BTC_SHORT), 5.0, T0)["owner"] == "BTC"
    sw.post_claim("BTC", 25.0, "TP fill", now=T0)
    assert sw.attribute("USDC", "SOL", st(1, BTC_SHORT), 25.4, T0)["owner"] == "BTC"
    assert sw.attribute("USDC", "SOL", st(1, BTC_SHORT), 40.0, T0).get("ambiguous")      # amount does not match
    sw.post_claim("ETH", None, "decrease sent", now=T0)
    assert sw.attribute("USDC", "SOL", st(1, BTC_SHORT), 25.0, T0).get("ambiguous")      # ETH amount still unknown
    sw.post_claim("ETH", 15.0, "close", now=T0)
    assert sw.attribute("USDC", "SOL", st(1, BTC_SHORT), 15.1, T0)["owner"] == "ETH"
    assert sw.attribute("USDC", "SOL", st(1, BTC_SHORT), 40.0, T0).get("ambiguous")      # sum of two: never split/guess


def test_claims_accumulate_and_expire():
    sw.post_claim("ETH", None, "decrease sent", now=T0)
    sw.post_claim("ETH", 5.0, "TP fill", now=T0 + 1)
    sw.post_claim("ETH", 7.5, "close", now=T0 + 2)
    assert sw.load_claims(T0 + 3)["ETH"]["usd"] == pytest.approx(12.5)
    assert sw.load_claims(T0 + 2 + sw.CLAIM_MAX_AGE_SEC + 1) == {}


def test_sol_short_does_not_compete_with_eth_btc():
    s = st(1, [{"market": "SOL", "side": "short"}, {"market": "ETH", "side": "short"}])
    assert sw.attribute("USDC", "SOL", s, 1.0, T0)["owner"] == "ETH"


@pytest.fixture
def ebot(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_eth.toml", ["live.dry_run=true", f"bot.state_dir={tmp_path.name}"])
    monkeypatch.setattr(B, "ROOT", tmp_path.parent)
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False, short_payout_native=False)
    b.local.bank = K.new_bank("ETH", 0.015, 0.0155)
    b._sol_px, b._mkt_px = 110.0, 2450.0
    b.save = lambda: None
    return b


def test_bank_payout_posts_claim_only_for_usdc_paying_short(ebot):
    ebot.local.side = -1
    ebot.bank_payout(15.2221, "close (tp_timer)")
    assert sw.load_claims()["ETH"]["usd"] == pytest.approx(15.2221)
    sw.clear_claim("ETH")
    ebot.live = dataclasses.replace(ebot.live, short_payout_native=True)   # paid in ETH: no USDC, no claim
    ebot.bank_payout(10.0, "TP fill")
    ebot.local.side = 1
    ebot.live = dataclasses.replace(ebot.live, short_payout_native=False)
    ebot.bank_payout(10.0, "TP fill")                                    # long: pays ETH
    assert sw.load_claims() == {}
    assert ebot._usdc_payout_expected("decrease", {"side": "short"})
    assert not ebot._usdc_payout_expected("decrease", {"side": "short", "desiredMint": "x"})
