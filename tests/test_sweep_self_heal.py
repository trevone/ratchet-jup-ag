"""Self-healing sweep (2026-10-08, after the 01:59 MT ETH close whose $5.22 USDC swap-back hit Jupiter 429s and was never
retried once the bot was flat). Every Jupiter call here is a fake; conftest blocks the CLI and redirects the ledger and
the shared sweep state / lock (sweep.ROOT) to a temp dir."""
import csv
import fcntl
import json

import pytest

from config import load_config
from live import bot as botmod
from live import ledger, sweep as sw, withdraw as wd

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ETH = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
WBTC = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
JUPSOL = "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"
WSOL = "So11111111111111111111111111111111111111112"
API, LITE = "https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1"
E429 = {"ok": False, "error": f"Error: jupiter api failed: {API} 429; {API} 429"}
E503 = {"ok": False, "error": f"Error: jupiter api failed: {LITE} 503"}
OK = {"ok": True, "sig": "SWAPSIG", "sent": True, "deltas": {"nativeDeltaLamports": 45_404_512, "wsolDeltaAtoms": "0"}}
T0 = 1_791_446_400.0          # 2026-10-08 08:00 UTC


@pytest.fixture
def cfg():
    return load_config("configs/live_eth.toml")


class Fake:
    """Scripted Jupiter CLI: pops one result per swap call; records (cmd, args)."""
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, args):
        self.calls.append((cmd, args))
        return self.results.pop(0) if self.results else OK


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def st_with(usdc=0, eth=0, wbtc=0, positions=(), eth_px=2560.0, read_at=None, extra=None):
    toks = {"USDC": {"mint": USDC, "atoms": str(usdc)}, "ETH": {"mint": ETH, "atoms": str(eth)},
            "WBTC": {"mint": WBTC, "atoms": str(wbtc)}, "WSOL": {"mint": WSOL, "atoms": "0"}, **(extra or {})}
    s = {"sweepTokens": toks, "allPositions": list(positions), "oracle": {"price": eth_px}, "allOpenRequests": [],
         "openRequests": []}
    if read_at is not None:
        s["_read_at"] = read_at
    return s


def short_trade(close="2026-10-08 07:59:46+00:00", sig="H4yhr9u4", pos="JChss"):
    return {"open_time": "2026-10-08 05:30:59+00:00", "close_time": close, "side": "short", "entries": 1, "adds": 0,
            "net_sol": -0.044329675, "net_sol_est": 0.000896025, "open_sig": sig, "position": pos, "asset": "ETH"}


def heal(cfg, me, st, jup, clock, sleeps=None):
    return sw.self_heal(cfg, me, st, jup, dry_run=False, min_usd=0.10, clock=clock,
                        sleep=(sleeps.append if sleeps is not None else (lambda s: None)), lock_wait=0)


# ------------------------------------------------------------------ the 01:59 incident, end to end
def test_flat_bot_keeps_retrying_its_booked_shorts_usdc_with_backoff(cfg):
    ledger.write_row(short_trade())                     # ETH short already booked (swap-back failed at close)
    clock, sleeps = Clock(), []
    fake = Fake(E429, E429, E429, E429)
    st = st_with(usdc=5_222_517)
    assert heal(cfg, "ETH", st, fake, clock, sleeps) == []
    # 2 rounds x (api.jup.ag, lite-api.jup.ag), exponential wait between rounds, slippage cap on every attempt
    assert [c[1]["apiBases"] for c in fake.calls] == [[API], [LITE], [API], [LITE]]
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.5
    assert all(c[0] == "swap" and c[1]["slippageBps"] == cfg.withdraw.unstake_slippage_bps
               and c[1]["maxPriceImpactPct"] == cfg.withdraw.stake_max_price_impact_pct and c[1]["inputMint"] == USDC
               for c in fake.calls)
    ent = sw.load_state()["tokens"]["USDC"]
    assert ent["fails"] == 1 and ent["next_try"] == pytest.approx(T0 + 60) and ent["claim"]["open_sig"] == "H4yhr9u4"
    # flat ticks keep checking: inside the backoff nothing is sent ...
    clock.t = T0 + 30
    fake.calls.clear()
    assert heal(cfg, "ETH", st_with(usdc=5_222_517), fake, clock) == [] and fake.calls == []
    # ... after it, the next tick sweeps and books a late row onto the short that produced the USDC
    clock.t = T0 + 61
    out = heal(cfg, "ETH", st_with(usdc=5_222_517), fake, clock)
    assert [o["mode"] for o in out] == ["late"] and out[0]["sol"] == pytest.approx(0.045404512)
    rows = wd.read_trades(ledger.TRADES)
    assert rows[-1]["side"] == "sweep" and rows[-1]["open_sig"] == "H4yhr9u4" and rows[-1]["position"] == "JChss"
    assert float(rows[-1]["net_sol"]) == pytest.approx(0.045404512) and "short trade" in rows[-1]["note"]
    assert wd.trade_net_sol(rows[-1]) == pytest.approx(0.045404512)          # the daily stake counts it (once)
    s = sw.load_state()
    assert "USDC" not in s["tokens"] and s["last_sweep"]["USDC"]["sig"] == "SWAPSIG"


def test_cross_tick_backoff_doubles_and_caps(cfg):
    clock = Clock()
    for n, expect in enumerate([60, 120, 240, 480, 900, 900], start=1):
        heal(cfg, "ETH", st_with(usdc=5_000_000), Fake(*[E429] * 4), clock)
        ent = sw.load_state()["tokens"]["USDC"]
        assert ent["fails"] == n and ent["next_try"] - clock.t == pytest.approx(expect)
        clock.t = ent["next_try"] + 1


def test_falls_back_to_second_api_base(cfg):
    fake = Fake(E429, OK)
    out = heal(cfg, "ETH", st_with(usdc=5_000_000), fake, Clock())
    assert [c[1]["apiBases"] for c in fake.calls] == [[API], [LITE]] and out[0]["sol"] == pytest.approx(0.045404512)
    assert sw.classify(E503) == "retry" and sw.classify(E429) == "retry"
    fake = Fake(E503, OK)   # 5xx falls back too
    out = heal(cfg, "ETH", st_with(usdc=4_000_000, read_at=T0 + 100), fake, Clock(T0 + 100))
    assert [c[1]["apiBases"] for c in fake.calls] == [[API], [LITE]] and len(out) == 1


def test_sent_but_unconfirmed_is_never_resent_in_the_same_tick(cfg):
    clock = Clock()
    fake = Fake({"ok": False, "sent": True, "sig": "MAYBE", "err": "not confirmed"})
    assert heal(cfg, "ETH", st_with(usdc=5_000_000), fake, clock) == [] and len(fake.calls) == 1
    ent = sw.load_state()["tokens"]["USDC"]
    assert ent["last_sig"] == "MAYBE" and ent["next_try"] == pytest.approx(T0 + 60)
    # the tx had landed: the next fresh balance read shows no USDC -> the pending entry is cleared, nothing re-sent
    clock.t = T0 + 61
    fake.calls.clear()
    heal(cfg, "ETH", st_with(usdc=0, read_at=T0 + 61), fake, clock)
    assert fake.calls == [] and sw.load_state()["tokens"] == {}
    # CLI crash / unknown errors are not retried in-tick either; quote / impact rejections are fatal (no base loop)
    assert sw.classify({"ok": False, "error": "Error: price impact 1.2% > 0.5%"}) == "fatal"
    assert sw.classify({"ok": False, "error": "Error: jupiter api failed: https://api.jup.ag/swap/v1 400"}) == "fatal"
    assert sw.classify({"ok": False, "error": "some stderr"}) == "unknown"


def test_fatal_error_stops_after_one_call_and_backs_off(cfg):
    fake = Fake({"ok": False, "error": "Error: price impact 1.2% > 0.5%"})
    heal(cfg, "ETH", st_with(usdc=5_000_000), fake, Clock())
    assert len(fake.calls) == 1 and sw.load_state()["tokens"]["USDC"]["fails"] == 1


def test_cli_exception_is_unknown_not_retried(cfg):
    calls = []

    def boom(cmd, args):
        calls.append(cmd)
        raise TimeoutError("cli timed out")
    heal(cfg, "ETH", st_with(usdc=5_000_000), boom, Clock())
    assert calls == ["swap"] and sw.load_state()["tokens"]["USDC"]["last_error"].startswith("unknown")


# ------------------------------------------------------------------ what gets swept
def test_never_jupsol_or_wsol_and_dust_left_alone(cfg):
    extra = {"WSOL": {"mint": WSOL, "atoms": "5000000000"}, "JUPSOL": {"mint": JUPSOL, "atoms": "3843000"}}
    fake = Fake()
    out = heal(cfg, "ETH", st_with(usdc=50_000, eth=1_000, extra=extra), fake, Clock())   # $0.05 USDC, $0.03 ETH
    assert out == [] and fake.calls == []
    assert sw.SWEEPABLE == ("USDC", "ETH", "WBTC")


def test_every_token_above_dust_swept_in_position_or_flat(cfg):
    # ETH bot in a SHORT: USDC (its own open short) + ETH (an earlier long) both go back to SOL in one tick
    ledger.on_open("2026-10-08 05:30:59+00:00", -1, 1.55, "OPENSIG", 5000, position="ETHSHORT")
    fake = Fake(OK, OK)
    out = heal(cfg, "ETH", st_with(usdc=2_500_000, eth=400_000, positions=[{"market": "ETH", "side": "short"}]),
               fake, Clock())
    assert {c[1]["inputMint"] for c in fake.calls} == {USDC, ETH}
    assert {o["name"]: o["mode"] for o in out} == {"USDC": "open", "ETH": "late"}
    assert json.loads(ledger.OPEN.read_text())["sweep_sol"] == pytest.approx(0.045404512)


def test_foreign_token_needs_a_fresh_price_from_its_own_bot(cfg):
    # the SOL bot has no WBTC oracle: it dust-checks WBTC with the price the BTC bot publishes each tick
    btc_cfg = load_config("configs/live_btc.toml")
    clock = Clock()
    fake = Fake()
    assert heal(cfg, "SOL", st_with(wbtc=20_000, eth_px=116.0), fake, clock) == [] and fake.calls == []
    heal(btc_cfg, "BTC", st_with(eth_px=83000.0), Fake(), clock)            # BTC bot publishes its price
    assert sw.read_price("WBTC", clock.t) == 83000.0
    assert sw.pending_tokens(st_with(wbtc=20_000, eth_px=116.0), "SOL", 0.10, clock.t)["WBTC"]["usd"] == pytest.approx(16.6)
    assert sw.read_price("WBTC", clock.t + sw.PRICE_MAX_AGE_SEC + 1) is None      # stale price -> left to the BTC bot


# ------------------------------------------------------------------ one sweeper at a time
def test_lock_held_by_another_bot_skips_this_tick(cfg):
    path = sw.ROOT / sw.SHARED_DIR / "sweep.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as other:                    # another process's flock (separate open file description)
        fcntl.flock(other, fcntl.LOCK_EX)
        fake = Fake()
        assert heal(cfg, "ETH", st_with(usdc=5_000_000), fake, Clock()) == [] and fake.calls == []
        fcntl.flock(other, fcntl.LOCK_UN)
    fake = Fake()
    assert len(heal(cfg, "ETH", st_with(usdc=5_000_000), fake, Clock())) == 1 and len(fake.calls) == 1


def test_balance_read_before_another_bots_sweep_is_not_swapped_again(cfg):
    clock = Clock()
    heal(cfg, "ETH", st_with(usdc=5_000_000, read_at=T0), Fake(OK), clock)       # swept at T0
    fake = Fake()
    clock.t = T0 + 5
    out = heal(cfg, "BTC", st_with(usdc=5_000_000, read_at=T0 - 10), fake, clock)  # BTC's view predates that sweep
    assert out == [] and fake.calls == []


# ------------------------------------------------------------------ attribution across bots
def eth_dir():
    return sw.ROOT / "live/eth"


def test_other_bot_takes_over_after_grace_and_books_into_the_producing_trade(cfg):
    sol_cfg = load_config("configs/live_100.toml")
    eth_dir().mkdir(parents=True)
    old = ["open_time", "close_time", "side", "entries", "adds", "net_sol", "open_sig", "note", "net_sol_est"]
    with open(eth_dir() / "trades.csv", "w", newline="") as f:   # an older column layout (running old code)
        w = csv.DictWriter(f, fieldnames=old)
        w.writeheader()
        w.writerow({k: short_trade().get(k) for k in old})
    clock = Clock()
    fake = Fake()
    assert heal(sol_cfg, "SOL", st_with(usdc=5_222_517, eth_px=116.0), fake, clock) == [] and fake.calls == []
    ent = sw.load_state()["tokens"]["USDC"]
    assert ent["claim"]["owner"] == "ETH" and ent["claim"]["open_sig"] == "H4yhr9u4"
    clock.t = T0 + sw.FOREIGN_GRACE_SEC + 1                   # the ETH bot never swept it (old code / down)
    out = heal(sol_cfg, "SOL", st_with(usdc=5_222_517, eth_px=116.0), Fake(OK), clock)
    assert out[0]["owner"] == "ETH" and out[0]["mode"] == "late"
    with open(eth_dir() / "trades.csv", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        assert r.fieldnames == old                            # header untouched (no migration under another process)
    assert rows[-1]["side"] == "sweep" and rows[-1]["open_sig"] == "H4yhr9u4"
    assert float(rows[-1]["net_sol"]) == pytest.approx(0.045404512) and "swept by the SOL bot" in rows[-1]["note"]
    assert json.loads((eth_dir() / "ledger_summary.json").read_text())["net_sol"] == pytest.approx(0.045404512)
    assert not ledger.TRADES.exists()                         # nothing booked into the SOL bot's own ledger
    assert ledger.TRADES.name == "trades.csv" and ledger.ASSET == "SOL"   # ledger.at restored the globals


def test_open_trade_tokens_are_left_to_their_own_bot(cfg):
    btc_cfg = load_config("configs/live_btc.toml")
    d = sw.ROOT / "live/btc"
    d.mkdir(parents=True)
    (d / "ledger_open.json").write_text(json.dumps({"side": "short", "open_sig": "BTCOPEN", "position": "BTCSHORT",
                                                    "open_time": "x", "exits": []}))
    st = st_with(usdc=3_000_000, positions=[{"market": "BTC", "side": "short"}])
    clock = Clock(T0 + 10_000)
    fake = Fake()
    assert heal(cfg, "ETH", st, fake, clock) == [] and fake.calls == []     # even long after the grace period
    clock.t += sw.FOREIGN_GRACE_SEC * 3
    assert heal(cfg, "ETH", st, fake, clock) == [] and fake.calls == []
    # the BTC bot itself books into its open record
    ledger.OPEN.write_text((d / "ledger_open.json").read_text())
    with ledger.at(d, "BTC"):
        out = heal(btc_cfg, "BTC", {**st, "oracle": {"price": 83000.0}}, Fake(OK), clock)
        assert out[0]["mode"] == "open" and json.loads(ledger.OPEN.read_text())["sweep_sol"] == pytest.approx(0.045404512)


def test_usdc_attribution_rules():
    # open shorts: ETH > BTC > SOL (approximate when two are short at once, as before)
    st = st_with(usdc=1, positions=[{"market": "BTC", "side": "short"}, {"market": "ETH", "side": "short"}])
    assert sw.attribute("USDC", "SOL", st)["owner"] == "ETH"
    assert sw.attribute("USDC", "SOL", st_with(usdc=1, positions=[{"market": "BTC", "side": "short"}]))["owner"] == "BTC"
    # nobody short: most recently closed short trade
    for a, close in (("ETH", "2026-10-08 07:59:46+00:00"), ("BTC", "2026-10-08 08:30:00+00:00")):
        with ledger.at(sw.ROOT / sw.BOT_DIRS[a], a):
            ledger.write_row({**short_trade(close=close, sig=f"{a}SIG"), "asset": a})
    c = sw.attribute("USDC", "SOL", st_with(usdc=1))
    assert (c["owner"], c["open_sig"], c["side"]) == ("BTC", "BTCSIG", "short")
    assert sw.attribute("ETH", "SOL", st_with(eth=1))["owner"] == "ETH" and sw.attribute("WBTC", "SOL", st_with())["side"] == "long"


def test_new_proceeds_reattribute_and_reset_backoff(cfg):
    clock = Clock()
    heal(cfg, "ETH", st_with(usdc=5_000_000), Fake(*[E429] * 4), clock)
    assert sw.load_state()["tokens"]["USDC"]["fails"] == 1
    clock.t = T0 + 10                                     # inside the backoff, but a new payout lands -> try now
    fake = Fake(OK)
    out = heal(cfg, "ETH", st_with(usdc=10_000_000), fake, clock)
    assert len(fake.calls) == 1 and fake.calls[0][1]["amountAtoms"] == "10000000" and out[0]["sol"] > 0


def test_dry_run_only_simulates_and_writes_no_shared_state(cfg):
    fake = Fake({"ok": True})
    assert sw.self_heal(cfg, "ETH", st_with(usdc=5_000_000), fake, dry_run=True, min_usd=0.10) == []
    assert [c[0] for c in fake.calls] == ["simulate-swap"] and fake.calls[0][1]["send"] is False
    assert not (sw.ROOT / sw.SHARED_DIR / "sweep_state.json").exists()


def test_bot_sweep_flat_bot_sweeps_usdc(tmp_path, monkeypatch):
    # regression through the Bot API: a flat ETH bot (side None) now sweeps leftover USDC (it used to ignore it)
    cfg = load_config("configs/live_eth.toml", [f"bot.state_dir={tmp_path}", f"live.kill_switch_path={tmp_path}/KILL",
                                                f"bot.global_kill_path={tmp_path}/KILL_ALL", "bot.extra_pause_files=[]",
                                                "live.dry_run=false"])
    b = botmod.Bot(cfg)
    fake = Fake(OK)
    monkeypatch.setattr(botmod, "jup", fake)
    monkeypatch.setenv("JUP_LIVE", "0")
    b.sweep(st_with(usdc=5_222_517), None)
    row = wd.read_trades(ledger.TRADES)[-1]
    assert [c[0] for c in fake.calls] == ["swap"] and row["side"] == "sweep"
    assert row["note"].startswith("unattributed")        # no short trade anywhere -> still swept, booked by the sweeper


def test_ledger_at_restores_globals_on_error(tmp_path):
    before = (ledger.TRADES, ledger.OPEN, ledger.ASSET)
    with pytest.raises(RuntimeError):
        with ledger.at(tmp_path / "x", "BTC"):
            assert ledger.ASSET == "BTC" and ledger.TRADES == tmp_path / "x" / "trades.csv"
            raise RuntimeError
    assert (ledger.TRADES, ledger.OPEN, ledger.ASSET) == before
