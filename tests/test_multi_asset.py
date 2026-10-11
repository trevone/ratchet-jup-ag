"""Multi-asset (SOL / ETH / BTC on one wallet): budget isolation, per-asset scaling, sizing floors, ledger/stake
aggregation and deposit detection. Pure functions + temp files only; conftest.py blocks every chain call."""
from types import SimpleNamespace

import pandas as pd
import pytest

from asset_scale import atr_from_klines, levels_with_fallback, scale_config
from dca_levels import Levels
from live.budget import budget_from_state, compute_budget

SOL_PX = 116.0
RESERVE = 0.02
SHARES = {"SOL": 0.5, "ETH": 0.25, "BTC": 0.25}


def state(native_sol: float, positions: list[dict], market: str = "SOL", px: float = SOL_PX) -> dict:
    return {"wallet": {"sol": native_sol}, "oracle": {"price": px}, "solOracle": {"price": SOL_PX},
            "allPositions": positions, "positions": {}}


# ------------------------------------------------------------------ budget isolation
def test_single_bot_share_one_is_legacy():
    b = compute_budget(1.0, 0.91, SOL_PX, RESERVE, 0.0, 0.0)
    assert b.free_usd == pytest.approx((0.91 - RESERVE) * SOL_PX)
    # legacy state without allPositions / solOracle: same numbers as the old ChainAccount
    st = {"wallet": {"sol": 0.91}, "oracle": {"price": SOL_PX}, "positions": {"long": {"open": False}, "short": {"open": False}}}
    assert budget_from_state(st, 1.0, RESERVE, "SOL").free_usd == pytest.approx((0.91 - RESERVE) * SOL_PX)


def test_three_bots_flat_split():
    st = state(0.91, [])
    tot = (0.91 - RESERVE) * SOL_PX
    for m, sh in SHARES.items():
        assert budget_from_state(st, sh, RESERVE, m).free_usd == pytest.approx(sh * tot)


def test_open_position_does_not_change_other_bots_budget():
    tot = (0.91 - RESERVE) * SOL_PX
    eth_coll = 10.50
    native = 0.91 - eth_coll / SOL_PX - 0.0001          # ETH opened: SOL left the wallet as ETH collateral (+ fee)
    st = state(native, [{"market": "ETH", "side": "long", "sizeUsd": 73.5, "collateralUsd": eth_coll}])
    sol = budget_from_state(st, 0.5, RESERVE, "SOL").free_usd
    btc = budget_from_state(st, 0.25, RESERVE, "BTC").free_usd
    eth = budget_from_state(st, 0.25, RESERVE, "ETH").free_usd
    assert sol == pytest.approx(0.5 * tot, abs=0.02)
    assert btc == pytest.approx(0.25 * tot, abs=0.02)
    assert eth == pytest.approx(0.25 * tot - eth_coll, abs=0.02)


def test_bot_never_uses_another_share_even_when_others_flat():
    st = state(0.91 - 26.0 / SOL_PX, [{"market": "BTC", "side": "long", "sizeUsd": 300, "collateralUsd": 26.0}])
    b = budget_from_state(st, 0.25, RESERVE, "BTC")
    assert b.free_usd == pytest.approx(0.0, abs=1e-9)   # BTC used its whole share; SOL/ETH free money is not BTC's


@pytest.mark.parametrize("seq", [("SOL", "ETH", "BTC"), ("BTC", "ETH", "SOL"), ("ETH", "SOL", "BTC")])
def test_all_bots_spending_everything_never_overdraws(seq):
    native, positions = 0.91, []
    for m in seq * 3:                                          # each bot repeatedly spends all its free budget
        st = state(native, positions)
        free = budget_from_state(st, SHARES[m], RESERVE, m).free_usd
        assert free <= (native - RESERVE) * SOL_PX + 1e-9      # capped at what is really in the wallet
        if free > 0:
            native -= free / SOL_PX
            positions.append({"market": m, "side": "long", "sizeUsd": free * 7, "collateralUsd": free})
    assert native >= RESERVE - 1e-12
    for m, sh in SHARES.items():
        used = sum(p["collateralUsd"] for p in positions if p["market"] == m)
        assert used == pytest.approx(sh * (0.91 - RESERVE) * SOL_PX, abs=0.01)


def test_cap_at_native_free_when_collateral_is_large():
    # big open collateral, little native SOL left: the share math may say more, the wallet caps it
    st = state(0.05, [{"market": "SOL", "side": "long", "sizeUsd": 2000, "collateralUsd": 100.0}])
    b = budget_from_state(st, 0.25, RESERVE, "ETH")
    assert b.share_usd > b.native_free_usd and b.free_usd == pytest.approx((0.05 - RESERVE) * SOL_PX)


def test_share_validation():
    with pytest.raises(ValueError):
        compute_budget(0.0, 1.0, SOL_PX, RESERVE, 0, 0)
    with pytest.raises(ValueError):
        compute_budget(1.5, 1.0, SOL_PX, RESERVE, 0, 0)


# ------------------------------------------------------------------ per-asset scaling
def test_atr_from_klines():
    idx = pd.date_range("2026-10-01", periods=15 * 4 * 24, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=idx)
    assert atr_from_klines(df) == pytest.approx(2.0)
    assert atr_from_klines(df.iloc[:20]) is None


def test_levels_fallback():
    lv = Levels((1.0,), (2.0,), 0.5)
    assert levels_with_fallback(lv, 9.0, True) is lv
    assert levels_with_fallback(None, 9.0, False) is None
    f = levels_with_fallback(None, 9.0, True)
    assert f.atr == 9.0 and f.supports == () and f.resistances == ()


# ------------------------------------------------------------------ configs + per-asset sizing floors
from config import load_config  # noqa: E402
from asset_scale import scaled_summary  # noqa: E402
from dca_levels import _SimPos, project_ladder  # noqa: E402
from strategy import DcaStrategy  # noqa: E402

ASSET_CFG = {"ETH": ("configs/live_eth.toml", 2578.0, 13.8), "BTC": ("configs/live_btc.toml", 83100.0, 350.0)}


def test_config_keys_and_isolation():
    sol = load_config("configs/live_100.toml")
    assert sol.bot.asset == "SOL" and sol.bot.state_dir == "live" and sol.live.kill_switch_path == "live/KILL"
    assert set(sol.withdraw.trade_files) >= {"live/trades.csv"}
    shares = sol.bot.share
    for a, (f, _, _) in ASSET_CFG.items():
        c = load_config(f)
        assert c.bot.asset == a and c.bot.share == 0.25 and c.bot.state_dir == f"live/{a.lower()}"
        assert c.live.kill_switch_path == f"live/{a.lower()}/KILL" and not c.withdraw.enabled
        assert c.market.symbol == f"{a}USDT" and c.tp.mode == "price_pct" and c.scale.enabled and c.scale.own_atr_fallback
        assert c.dca.levels_path == f"/workspace/market-analysis/{a.lower()}/latest.json"
        shares += c.bot.share
    assert shares <= 1.0 + 1e-9   # SOL 0.5 (live) or 1.0 (default) + 0.25 + 0.25 -> must be configured to 0.5 when ETH/BTC run


@pytest.mark.parametrize("asset", ["ETH", "BTC"])
def test_scaled_distances(asset):
    f, px, atr = ASSET_CFG[asset]
    c = scale_config(load_config(f), px, atr)
    assert c.dca.trigger_dist == pytest.approx(1.15 * atr)
    assert c.tp.tp_min_move == pytest.approx(0.00043 * px) and c.tp.tp_market_gap == pytest.approx(0.00017 * px)
    assert c.tp.deep_buffer_usd == pytest.approx(0.0013 * px) and c.tp.tp_min_gap_usd == pytest.approx(0.00043 * px)
    c0 = scale_config(load_config(f), px, None)            # no ATR yet -> percent fallback
    assert c0.dca.trigger_dist == pytest.approx(0.0073 * px)
    sol = load_config("configs/live_100.toml")
    assert scale_config(sol, 116.0, 0.7) is sol             # SOL: [scale] off, USD values untouched
    assert "trigger_dist" in scaled_summary(c)


@pytest.mark.parametrize("asset", ["ETH", "BTC"])
@pytest.mark.parametrize("side", [1, -1])
def test_first_entry_floor_and_short_ladder(asset, side):
    f, px, atr = ASSET_CFG[asset]
    c = scale_config(load_config(f), px, atr)
    s = DcaStrategy(c)
    free = budget_from_state(state(0.91, []), 0.25, RESERVE, asset).free_usd      # ~$25.8
    m = s.initial_margin(free)
    assert m == 10.50                                         # 10% of ~$26 = $2.6 -> $10.50 floor (per bot)
    tps = s.tp_targets(_SimPos(side, m * 7, m * 7 / px, m, 0.0, px, 7.0, px)) if False else None
    n = m * c.dca.lev_start
    pos = _SimPos(side, n, n / px, m - n * c.costs.open_fee, 0.0, px, c.dca.lev_start, px)
    rungs = project_ladder(pos, Levels((), (), atr), available_usd=free - m, **s.plan_kwargs())
    assert 1 <= len(rungs) <= 5
    spent = 0.0
    for r in rungs:
        assert r.margin >= c.dca.min_add_usd - 1e-9            # never below $3
        assert side * (r.next_level - r.liq) > c.dca.liq_buffer_atr * atr   # liq >= 2 ATR beyond the next rung
        spent += r.margin + c.dca.ladder_fee_usd_per_add
    assert spent <= free - m + 1e-6                           # ladder stays inside the bot's own budget
    del tps


def test_adds_shrink_to_budget_and_stop_below_min():
    f, px, atr = ASSET_CFG["ETH"]
    c = scale_config(load_config(f), px, atr)
    s = DcaStrategy(c)
    n = 73.5
    pos = _SimPos(1, n, n / px, 10.45, 0.0, px, 7.0, px)
    from dca_levels import plan_add
    p = plan_add(pos, Levels((), (), atr), available_usd=4.0, **s.plan_kwargs())
    assert p is not None and 3.0 <= p.margin <= 4.0
    assert plan_add(pos, Levels((), (), atr), available_usd=2.9, **s.plan_kwargs()) is None


# ------------------------------------------------------------------ bot wiring (dry run, fake chain)
import live.bot as botmod  # noqa: E402


def eth_state(native=0.91, eth_px=2578.0, positions=None, requests=None):
    return {"market": "ETH", "wallet": {"sol": native}, "oracle": {"price": eth_px, "ageSec": 1},
            "solOracle": {"price": SOL_PX}, "allPositions": positions or [],
            "positions": {"long": {"pubkey": "ETHLONGPDA", "open": False}, "short": {"pubkey": "ETHSHORTPDA", "open": False}},
            "openRequests": requests or [], "allOpenRequests": requests or [], "custody": {}}


def make_bot(tmp_path, asset="ETH", extra=()):
    f = ASSET_CFG[asset][0]
    cfg = load_config(f, [f"bot.state_dir={tmp_path}", f"live.kill_switch_path={tmp_path}/KILL",
                          f"bot.global_kill_path={tmp_path}/KILL_ALL", "bot.extra_pause_files=[]",
                          "live.dry_run=true", *extra])   # configs are live now; tests always run the bot in dry-run
    return botmod.Bot(cfg)


def test_open_args_use_sol_price_and_swap_min_out(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "jup", lambda cmd, args=None: calls.append((cmd, args)) or {"ok": True})
    b = make_bot(tmp_path)
    st = eth_state()
    b.budget = budget_from_state(st, 0.25, RESERVE, "ETH")
    from strategy import Action, Intent
    b.open_or_add(Intent(Action.OPEN, 1, 2578.0, 10.50, 7.0), None, st, 100.0)
    cmd, a = calls[-1]
    assert cmd == "simulate-increase" and a["market"] == "ETH" and a["send"] is False
    assert int(a["collateralAtoms"]) == int(10.50 / SOL_PX * 1e9)          # SOL lamports at the SOL price
    assert int(a["jupiterMinimumOut"]) == int(10.50 * 0.99 / 2578.0 * 1e8)  # ETH atoms (8 dec), 1% slippage
    assert a["sizeUsd"] == 73.5 and a["priceSlippageUsd"] == round(2578.0 * 1.01, 2)
    b.open_or_add(Intent(Action.OPEN, -1, 2578.0, 10.50, 7.0), None, st, 100.0)
    cmd, a = calls[-1]
    assert int(a["jupiterMinimumOut"]) == int(10.50 * 0.99 * 1e6)          # short: USDC atoms


def test_guard_blocks_more_than_own_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "jup", lambda *a, **k: {"ok": True})
    b = make_bot(tmp_path)
    st = eth_state(native=0.91 - 26 / SOL_PX, positions=[{"market": "SOL", "side": "long", "sizeUsd": 300, "collateralUsd": 26.0},
                                                       {"market": "ETH", "side": "long", "sizeUsd": 100, "collateralUsd": 20.0}])
    b.budget = budget_from_state(st, 0.25, RESERVE, "ETH", own_pos_collateral=20.0)
    from strategy import Action, Intent
    why = b.guard(Intent(Action.ADD, 1, 2578.0, b.budget.free_usd + 1.0, 9.0), None, st, 100.0)
    assert why and "free budget" in why
    assert b.guard(Intent(Action.ADD, 1, 2578.0, max(b.budget.free_usd - 0.5, 0.0), 9.0), None, st, 100.0) is None


def test_own_kill_pause_and_global_kill(tmp_path, monkeypatch):
    b = make_bot(tmp_path)
    assert not b.killed() and b.entries_paused() is None
    (tmp_path / "PAUSE_UNTIL").write_text("2099-01-01T00:00:00")
    assert "paused" in b.entries_paused()
    (tmp_path / "KILL_ALL").write_text("x")
    assert b.killed()
    assert b.state_path == tmp_path / "state.json"


def test_sol_bot_paths_unchanged():
    b = botmod.Bot(load_config("configs/live_100.toml"))
    assert b.state_path == botmod.STATE and b.kill == botmod.ROOT / "live" / "KILL" and b.asset == "SOL"
    assert b.dir == botmod.ROOT / "live"


def test_levels_provider_falls_back_to_own_atr(tmp_path):
    b = make_bot(tmp_path, extra=(f"dca.levels_path={tmp_path}/missing.json",))
    assert b.strategy.levels_provider(pd.Timestamp.now(tz="UTC")) is None   # no ATR known yet -> old behaviour
    b._own_atr = 13.8
    lv = b.strategy.levels_provider(pd.Timestamp.now(tz="UTC"))
    assert lv.atr == 13.8 and lv.supports == ()


# ------------------------------------------------------------------ ledger / deposit detection / stake aggregation
from live import ledger  # noqa: E402
from live import withdraw as wd  # noqa: E402


def _trade(ledger_mod, t_open, t_close, wallet_before, wallet_after, flows, gross=0.5, position="P"):
    ledger_mod.on_open(t_open, 1, wallet_before, "SIG", 5000, position=position)
    ledger_mod.update(opened_size_usd=73.5, size_usd=73.5, avg_entry=100.0)
    return ledger_mod.on_flat(t_close, wallet_after, SOL_PX, gross, 0.0, flows_net_sol=flows)


def test_deposit_mid_trade_is_not_profit():
    # 1 SOL deposited while the trade was open: wallet delta says +1.003, the position's own txs say +0.003
    row = _trade(ledger, "2026-10-08 01:00:00+00:00", "2026-10-08 02:00:00+00:00", 0.9, 1.903, flows=0.003)
    assert row["net_sol"] == pytest.approx(0.003) and row["net_sol_method"] == "position_flows"
    assert row["asset"] == "SOL" and row["position"] == "P"
    assert wd.trade_net_sol(row) <= 0.003 + 1e-12


def test_other_bot_flows_not_counted_and_fallback_marked():
    # ETH opened during this SOL trade (wallet -0.09): flows unaffected; without flows the wallet delta is marked
    row = _trade(ledger, "2026-10-08 01:00:00+00:00", "2026-10-08 02:00:00+00:00", 0.9, 0.813, flows=0.003)
    assert row["net_sol"] == pytest.approx(0.003)
    row2 = _trade(ledger, "2026-10-08 03:00:00+00:00", "2026-10-08 04:00:00+00:00", 0.9, 0.903, flows=None)
    assert row2["net_sol_method"] == "wallet_delta" and row2["net_sol"] == pytest.approx(0.003)


def test_per_bot_ledgers_and_single_daily_stake(tmp_path, monkeypatch):
    # three bots write their own trade files; the SOL bot's daily run stakes the sum once, per-file cutoffs
    for d, asset, flows in (("live", "SOL", 0.002), ("live/eth", "ETH", 0.001), ("live/btc", "BTC", -0.0005)):
        ledger.configure(tmp_path / d, asset)
        _trade(ledger, "2026-10-08 01:00:00+00:00", "2026-10-08 02:00:00+00:00", 1.0, 5.0, flows=flows, gross=5.0)
    assert (tmp_path / "live/eth/trades.csv").exists() and (tmp_path / "live/btc/trades.csv").exists()
    monkeypatch.setattr(wd, "ROOT_DIR", tmp_path)
    cfg = load_config("configs/live_100.toml", ['withdraw.trade_files=["live/trades.csv","live/eth/trades.csv","live/btc/trades.csv"]'])
    staked = []
    chain = {"wallet": {"sol": 1.0}, "positions": {"long": {}, "short": {}}, "openRequests": [], "allOpenRequests": [], "allPositions": []}
    from datetime import datetime, timezone
    now = datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc)    # 00:10 MT
    st = {}
    rec = wd.run_withdraw(cfg, st, chain, now, stake=lambda a: staked.append(a) or {"ok": True, "sig": "S"})
    assert rec["realized_new"] == pytest.approx(0.0025) and staked == [pytest.approx(0.0025)]
    assert set(rec["realized_per_file"]) == {"live/trades.csv", "live/eth/trades.csv", "live/btc/trades.csv"}
    assert wd.run_withdraw(cfg, st, chain, now, stake=lambda a: staked.append(a)) is None          # once per day
    # next day: one new ETH trade only -> only it is counted (no re-count of yesterday's rows)
    ledger.configure(tmp_path / "live/eth", "ETH")
    _trade(ledger, "2026-10-08 20:00:00+00:00", "2026-10-08 21:00:00+00:00", 1.0, 1.0, flows=0.004, gross=5.0)
    rec2 = wd.run_withdraw(cfg, st, chain, datetime(2026, 10, 9, 6, 10, tzinfo=timezone.utc),
                           stake=lambda a: staked.append(a) or {"ok": True, "sig": "S2"})
    assert rec2["realized_new"] == pytest.approx(0.004) and staked[-1] == pytest.approx(0.004)


def test_withdraw_defers_on_any_bots_market_request(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "ROOT_DIR", tmp_path)
    ledger.configure(tmp_path / "live", "SOL")
    _trade(ledger, "2026-10-08 01:00:00+00:00", "2026-10-08 02:00:00+00:00", 1.0, 1.0, flows=0.002, gross=5.0)
    cfg = load_config("configs/live_100.toml", ['withdraw.trade_files=["live/trades.csv"]'])
    chain = {"wallet": {"sol": 1.0}, "positions": {"long": {}, "short": {}}, "openRequests": [],
             "allOpenRequests": [{"requestType": "market", "position": "ETHPDA"}], "allPositions": []}
    from datetime import datetime, timezone
    rec = wd.run_withdraw(cfg, {}, chain, datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc), stake=lambda a: 1 / 0)
    assert rec["amount"] == 0.0 and rec["carry_after"] == pytest.approx(0.002)


def test_eth_bot_never_stakes():
    cfg = load_config("configs/live_eth.toml")
    from datetime import datetime, timezone
    assert wd.run_withdraw(cfg, {}, {}, datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc)) is None


def test_flows_helper_uses_cli_result():
    from live.flows import position_net_sol
    net, _ = position_net_sol("P", "S", jup=lambda c, a: {"ok": True, "netLamports": 667895, "nTx": 27})
    assert net == pytest.approx(0.000667895)
    assert position_net_sol("P", "S", jup=lambda c, a: {"ok": False})[0] is None
    assert position_net_sol("P", None, jup=lambda c, a: 1 / 0)[0] is None


# ------------------------------------------------------------------ SOL in / SOL out (sweep) + deposits with 3 bots
from live import sweep as sw  # noqa: E402

TOKS = {"USDC": {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "atoms": "2500000"},
        "ETH": {"mint": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "atoms": "400000"},
        "WBTC": {"mint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", "atoms": "50"}}


def test_sweep_attribution_no_double_claims():
    st = {"sweepTokens": TOKS}
    px = {"ETH": 2578.0, "WBTC": 83100.0}
    eth_long = [p["name"] for p in sw.plan_sweeps(st, "ETH", 1, px, 0.10)]
    btc_short = [p["name"] for p in sw.plan_sweeps(st, "BTC", -1, px, 0.10)]
    sol_flat = [p["name"] for p in sw.plan_sweeps(st, "SOL", None, px, 0.10)]
    sol_short = [p["name"] for p in sw.plan_sweeps(st, "SOL", -1, px, 0.10)]
    assert eth_long == ["ETH"] and sol_flat == [] and sol_short == ["USDC"]
    assert btc_short == ["USDC"]          # WBTC 50 atoms = $0.04 < $0.10 -> dust, left alone
    assert sw.plan_sweeps({"sweepTokens": {"error": "x"}}, "ETH", 1, px, 0.1) == []


def test_sweep_args_locked_and_capped():
    cfg = load_config("configs/live_eth.toml")
    a = sw.sweep_args(cfg, TOKS["USDC"]["mint"], 2500000)
    assert a["direction"] == "sweep" and a["slippageBps"] == cfg.withdraw.unstake_slippage_bps <= cfg.withdraw.stake_max_slippage_bps
    assert a["amountAtoms"] == "2500000"


def test_sweep_books_into_trade(tmp_path, monkeypatch):
    calls = []
    fake = lambda cmd, args: calls.append((cmd, args)) or {"ok": True, "sig": "SW", "deltas": {"nativeDeltaLamports": 21_000_000, "wsolDeltaAtoms": "0"}}
    b = make_bot(tmp_path, extra=("live.dry_run=false",))
    monkeypatch.setattr(botmod, "jup", fake)
    monkeypatch.setenv("JUP_LIVE", "0")
    ledger.on_open("2026-10-08 01:00:00+00:00", -1, 1.0, "SIG", 5000, position="ETHSHORTPDA")
    st = eth_state()
    st["sweepTokens"] = TOKS
    b.sweep(st, -1)
    assert [c[0] for c in calls] == ["swap", "swap"] and {c[1]["inputMint"] for c in calls} == {TOKS["USDC"]["mint"], TOKS["ETH"]["mint"]}
    # 2026-10-08 attribution: the USDC is this open short's proceeds -> its open record; ETH can only come from an ETH
    # LONG close, so while short it belongs to an earlier (booked) long trade -> its own late sweep row (same 0.042 total)
    late = wd.read_trades(ledger.TRADES)
    assert len(late) == 1 and late[0]["side"] == "sweep" and float(late[0]["net_sol"]) == pytest.approx(0.021)
    assert "ETH" in late[0]["note"] and "long" in late[0]["note"]
    row = ledger.on_flat("2026-10-08 02:00:00+00:00", 1.0, SOL_PX, 2.0, 0.0, flows_net_sol=-0.05)
    assert row["net_sol"] == pytest.approx(-0.05 + 0.021)   # position flows + the USDC swap's SOL
    assert sum(float(r["net_sol"]) for r in wd.read_trades(ledger.TRADES)) == pytest.approx(-0.05 + 0.042)
    # in-flight market request of ANY bot -> no sweep this tick
    calls.clear()
    st["allOpenRequests"] = [{"requestType": "market"}]
    b.sweep(st, -1)
    assert calls == []


def test_sweep_after_booking_gets_its_own_row(tmp_path, monkeypatch):
    b = make_bot(tmp_path, extra=("live.dry_run=false",))
    monkeypatch.setattr(botmod, "jup", lambda c, a: {"ok": True, "sig": "SW", "deltas": {"nativeDeltaLamports": 3_000_000}})
    monkeypatch.setenv("JUP_LIVE", "0")   # run_sweeps sets it in live mode; keep it scoped to this test
    st = eth_state()
    st["sweepTokens"] = {"ETH": TOKS["ETH"]}
    b.sweep(st, None)
    rows = wd.read_trades(ledger.TRADES)
    assert rows[-1]["side"] == "sweep" and float(rows[-1]["net_sol"]) == pytest.approx(0.003)


def test_dry_run_sweep_only_simulates(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "jup", lambda c, a: calls.append(c) or {"ok": True})
    b = make_bot(tmp_path)
    st = eth_state()
    st["sweepTokens"] = {"ETH": TOKS["ETH"]}
    b.sweep(st, 1)
    assert calls == ["simulate-swap"] and not ledger.OPEN.exists()


def test_deposit_seen_by_all_budgets_once_and_never_profit():
    # 0.5 SOL deposit while ETH has a position open: every bot's share grows from the live wallet, ETH's own collateral
    # is still subtracted only from ETH, and no trade's position flows include the deposit
    pos = [{"market": "ETH", "side": "short", "sizeUsd": 73.5, "collateralUsd": 10.5}]
    before = {m: budget_from_state(state(0.82, pos), sh, RESERVE, m).free_usd for m, sh in SHARES.items()}
    after = {m: budget_from_state(state(1.32, pos), sh, RESERVE, m).free_usd for m, sh in SHARES.items()}
    for m, sh in SHARES.items():
        assert after[m] - before[m] == pytest.approx(sh * 0.5 * SOL_PX)
    assert sum(after.values()) - sum(before.values()) == pytest.approx(0.5 * SOL_PX)   # counted once in total
    row = _trade(ledger, "2026-10-08 01:00:00+00:00", "2026-10-08 02:00:00+00:00", 0.82, 1.32 + 0.002, flows=0.002)
    assert wd.trade_net_sol(row) == pytest.approx(0.002)


def test_usdc_sweep_tiebreak_when_eth_and_btc_both_short():
    from live import sweep as sw
    st = {"sweepTokens": {"USDC": {"atoms": 5_000_000, "mint": "U"}, "WBTC": {"atoms": 0, "mint": "W"}},
          "allPositions": [{"market": "ETH", "side": "short"}, {"market": "BTC", "side": "short"}]}
    assert [p["name"] for p in sw.plan_sweeps(st, "ETH", -1, {"ETH": 2500.0}, 0.1)] == ["USDC"]
    assert sw.plan_sweeps(st, "BTC", -1, {"WBTC": 80000.0}, 0.1) == []
    st["allPositions"] = [{"market": "BTC", "side": "short"}]
    assert [p["name"] for p in sw.plan_sweeps(st, "BTC", -1, {"WBTC": 80000.0}, 0.1)] == ["USDC"]


def test_sol_bot_leaves_usdc_to_eth_btc_shorts():
    from live import sweep as sw
    st = {"sweepTokens": {"USDC": {"atoms": 5_000_000, "mint": "U"}}, "allPositions": [{"market": "BTC", "side": "short"}]}
    assert sw.plan_sweeps(st, "SOL", -1, {}, 0.1) == []
    st["allPositions"] = [{"market": "SOL", "side": "short"}]
    assert [p["name"] for p in sw.plan_sweeps(st, "SOL", -1, {}, 0.1)] == ["USDC"]
