"""ETH / BTC trade booking in the bot's own coin (2026-10-09). The ETH / BTC bots trade from ETH / WBTC, so the position's
SOL flows are only network fees; the old booking (net_sol = position SOL flows) made this morning's BTC long (05:38 MT,
HWM guard close, really +0.00000157 WBTC) read -0.172687841 SOL. Now the row is booked once the payout window is over,
from the bank's own-coin result (received - deposited), with its SOL / USD value. Fake chain only (conftest)."""
import dataclasses

import pytest

from config import load_config
from live import bank as K
from live import ledger
from live import withdraw as wd
from strategy import LONG, Action, Intent


@pytest.fixture(autouse=True)
def _legacy_ledger_mode(monkeypatch):
    """These tests cover the stored-bank reconcile (ext_flow_mode alert); live configs use "chain" since 2026-10-30."""
    import config as C
    orig = C.load_config

    def lc(path, overrides=None, *a, **k):
        return orig(path, list(overrides or []) + ["ledger.ext_flow_mode=alert"], *a, **k)
    monkeypatch.setattr(C, "load_config", lc)
    if "load_config" in globals():
        monkeypatch.setitem(globals(), "load_config", lc)


SOL_PX, BTC_PX = 110.6318, 82700.0


def _st(wbtc_atoms, size=0.0, usdc=0, price=BTC_PX):
    p = {"pubkey": "P", "sizeUsd": size, "collateralUsd": 10.0, "price": price, "realisedPnlUsd": 0.0} if size else {}
    return {"market": "BTC", "openRequests": [], "allOpenRequests": [], "oracle": {"price": price, "ageSec": 1},
            "solOracle": {"price": SOL_PX}, "wallet": {"sol": 0.813033426}, "custody": {},
            "positions": {"long": p, "short": {}},
            "sweepTokens": {"WBTC": {"mint": K.MINT["WBTC"], "atoms": str(wbtc_atoms)},
                            "USDC": {"mint": K.MINT["USDC"], "atoms": str(usdc)}}}


@pytest.fixture
def bbot(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_btc.toml", ["live.dry_run=true", f"bot.state_dir={tmp_path.name}"])
    monkeypatch.setattr(B, "ROOT", tmp_path.parent)
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)
    b.kill, b.kill_all, b.confirm_wait = tmp_path / "KILL", tmp_path / "KILL_ALL", 0.0
    b.cmd = lambda name, args: {"ok": True, "sig": "S", "feeLamports": 5000}
    b._sol_px, b._mkt_px = SOL_PX, BTC_PX
    # this morning's BTC long just before the 05:38 MT guard close: seeded trade collateral 0.000235920 WBTC, TP1 paid
    # 0.000117680 WBTC at 05:32
    bk = K.new_bank("BTC", 0.00057447, 0.00069271)
    bk["trade_dep"], bk["trade_recv"] = 0.00023592, 0.00011768
    b.local.bank = bk
    b.local.side = LONG
    monkeypatch.setattr(B, "position_net_sol", lambda pos, sig: (-0.172687841, {"ok": True, "nTx": 65, "rentLamports": 0,
                                                                                 "pnlUsd": 0.493901, "decreases": 3,
                                                                                 "eventErrors": []}))
    ledger.on_open("2026-10-08 02:42:34+00:00", LONG, 1.807310633, "4Fch", 5000, position="H4H6")
    ledger.update(opened_size_usd=172.5, size_usd=172.5, avg_entry=82331.74643)
    return b


def test_btc_trade_booked_in_own_coin_after_payout_settles(bbot):
    b = bbot.local.bank
    st = _st(57447)                                       # payout not in the wallet yet
    bbot.bank_payout(9.93, "close (profit_guard)")
    bbot.bank_flat(bbot.book_flat(st, LONG))
    assert not ledger.TRADES.exists() and b["book_pend"]["flows"] == pytest.approx(-0.172687841)
    assert (b["trade_dep"], b["trade_recv"]) == (0.00023592, 0.00011768)   # counters kept until the booking
    st2 = _st(69428)                                      # 05:39: the close payout 0.000119810 WBTC lands
    bbot.bank_tick(st2, None, BTC_PX)
    assert b["bal"] == pytest.approx(0.00069428) and not ledger.TRADES.exists()   # window still open
    bbot.bank_tick(st2, None, BTC_PX)
    bbot.bank_tick(st2, None, BTC_PX)                     # window over, nothing in flight -> booked
    rows = wd.read_trades(ledger.TRADES)
    assert len(rows) == 1 and "book_pend" not in b and (b["trade_dep"], b["trade_recv"]) == (0.0, 0.0)
    r = rows[0]
    assert r["net_sol_method"] == "bank_native" and r["native_coin"] == "WBTC"
    assert float(r["net_native"]) == pytest.approx(0.00000157, abs=1e-12)
    assert float(r["native_deposited"]) == pytest.approx(0.00023592) and float(r["native_received"]) == pytest.approx(0.00023749)
    assert float(r["net_sol"]) == pytest.approx(0.00000157 * BTC_PX / SOL_PX, abs=1e-9)          # ~ +0.00117 SOL
    assert float(r["net_usd_at_close"]) == pytest.approx(0.00000157 * BTC_PX, abs=1e-3)          # ~ +$0.13
    assert float(r["flows_net_sol"]) == pytest.approx(-0.172687841)                               # kept for info only
    assert r["net_sol_est"] == "" and wd.trade_net_sol(r) == pytest.approx(float(r["net_sol"]))   # no fee-model cap
    s = ledger._load(ledger.SUMMARY, {})
    assert s["trades"] == 1 and s["net_native"] == pytest.approx(0.00000157, abs=1e-12) and s["net_sol"] == pytest.approx(float(r["net_sol"]))
    bbot.bank_tick(st2, None, BTC_PX)                     # booked once
    assert len(wd.read_trades(ledger.TRADES)) == 1


def test_new_open_inside_the_window_books_the_previous_trade_first(bbot, monkeypatch):
    b = bbot.local.bank
    bbot.bank_flat(bbot.book_flat(_st(57447), LONG))
    bbot.local = type(bbot.local)(bank=b)                 # flat (as close() leaves it)
    b["trade_recv"] = 0.00023749                          # payout already reconciled, window not over yet
    b["bal"] = 0.00069428
    st1 = _st(57447 + 11981, size=52.5, price=82493.7)
    bbot.chain = lambda: st1
    bbot.sync_tp_ladder = lambda pos: None
    bbot.open_or_add(Intent(Action.OPEN, LONG, 82493.7, margin=10.5, leverage=5), None, _st(69428), 100.0)
    rows = wd.read_trades(ledger.TRADES)
    assert len(rows) == 1 and float(rows[0]["net_native"]) == pytest.approx(0.00000157, abs=1e-12)
    assert "book_pend" not in b and b["trade_recv"] == 0.0 and b["trade_dep"] == pytest.approx(int(10.5 / BTC_PX * 1e8) / 1e8)   # at the oracle


def test_sol_bot_and_bank_off_book_immediately_from_position_flows(bbot):
    bbot.local.bank = None                                # bank off (old configs / backtests): unchanged path
    bbot.book_flat(_st(57447), LONG)
    r = wd.read_trades(ledger.TRADES)[-1]
    assert r["net_sol_method"] == "position_flows" and float(r["net_sol"]) == pytest.approx(-0.172687841)


def test_on_flat_native_row_math():
    ledger.on_open("2026-10-09 13:46:15+00:00", LONG, 0.815, "2XpJ", 5000, position="H4H6")
    ledger.update(opened_size_usd=52.5, size_usd=52.5, avg_entry=82493.7)
    ledger.update(sweep_sol=-0.000006)                    # a sweep's SOL change is fees only: not in the own-coin result
    r = ledger.on_flat("2026-10-09 14:21:58+00:00", 0.815, 110.0, 0.0, 0.0, flows_net_sol=-0.000018232, events_gross_usd=0.315,
                       native={"coin": "WBTC", "deposited": 0.00012727, "received": 0.0001294, "coin_px": 82800.0})
    assert r["net_native"] == pytest.approx(0.00000213) and r["net_sol"] == pytest.approx(0.00000213 * 82800 / 110, abs=1e-9)
    assert r["true_result_sol"] == r["net_sol"] and "net_sol_est" not in r and r["flows_net_sol"] == -0.000018232


def test_sol_row_waits_for_the_cancelled_add_refund(bbot, monkeypatch):
    """2026-10-14: the SOL row was booked at flat (-0.095138230), a minute before the cancelled resting add's refund
    (+0.095831358) landed -> true net +0.000693128. The row now waits for the window / nothing in flight and re-reads."""
    import live.bot as B
    bbot.asset, bbot.local.bank["coin"] = "SOL", "SOL"
    flows = {"v": -0.095138230}
    monkeypatch.setattr(B, "position_net_sol", lambda pos, sig: (flows["v"], {"ok": True, "nTx": 5, "rentLamports": 0}))
    b = bbot.local.bank
    bbot.bank_flat(bbot.book_flat(_st(0), LONG))
    assert not ledger.TRADES.exists() and "book_pend" in b
    st = _st(0)
    st["openRequests"] = [{"pubkey": "R", "requestType": "market", "requestChange": "increase"}]
    for _ in range(4):                                        # window over, but the add is still in flight
        bbot.bank_tick(st, None, BTC_PX)
    assert not ledger.TRADES.exists()
    flows["v"] = 0.000693128                                  # the refund lands
    st["openRequests"] = []
    bbot.bank_tick(st, None, BTC_PX)
    r = wd.read_trades(ledger.TRADES)
    assert len(r) == 1 and float(r[0]["net_sol"]) == pytest.approx(0.000693128) and "book_pend" not in b
