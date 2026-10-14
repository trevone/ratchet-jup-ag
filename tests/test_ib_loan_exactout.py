"""Trevor 2026-10-13: inter-bot loan repayment = Jupiter ExactOut for exactly the owed lender amount; a short receipt
(chain delta) keeps the loan open with the shortfall and is topped up next tick before it is marked repaid."""
import json

from live import bank as bankmod
from live import sweep as sweepmod
from tests.test_ib_loan import bk, env  # noqa: F401  (fixture)


def _open(f, mk):
    f.write_text(json.dumps({"status": "open", "lender": "SOL", "borrower": "BTC", "sol": 0.478070233, "lender_coin": "SOL",
                             "coin_name": "WBTC"}))
    return mk("BTC", bk("WBTC", 0.0012, 0.0006, {"role": "borrower", "peer": "SOL", "sol": 0.478070233, "coin": 0.00061423,
                                                  "lender_coin": "SOL"}))


def test_exactout_args_and_full_repay(env, monkeypatch):  # noqa: F811
    f, mk = env
    btc = _open(f, mk)
    calls = []

    def sw(cfg, plan, jup, **k):
        calls.append((plan, k))
        return ({"ok": True, "sig": "R1", "inAmount": "61800", "deltas": {"feeLamports": 5000,
                 "nativeDeltaLamports": 478_070_233 - 5000, "tokenDeltaAtoms": {"WBTC": "-61800"}}}, "ok", [])
    monkeypatch.setattr(sweepmod, "swap_with_backoff", sw)
    btc.ib_borrower_repay(100.0, 80000.0)
    plan, k = calls[0]
    assert plan == {"mint": bankmod.MINT["WBTC"], "atoms": 478070233}
    assert k["swap_mode"] == "ExactOut" and k["out_mint"] == bankmod.MINT["SOL"]
    rec = json.loads(f.read_text())
    assert rec["status"] == "repaid" and rec["repaid_got"] == 0.478070233 and rec["repay_sig"] == "R1"
    assert "ib_loan" not in btc.local.bank and abs(btc.local.bank["bal"] - (0.0012 - 0.000618)) < 1e-12


def test_args_carry_swapmode():
    class W:
        unstake_slippage_bps, stake_max_slippage_bps, stake_max_price_impact_pct = 50, 100, 1.0
        stake_api_bases, stake_only_direct_routes = ["https://api.jup.ag/swap/v1"], False

    class C:
        withdraw = W()
    a = sweepmod.sweep_args(C(), "m", 5, "o", "ExactOut")
    assert a["swapMode"] == "ExactOut" and a["direction"] == "swap" and a["amountAtoms"] == "5"
    assert "swapMode" not in sweepmod.sweep_args(C(), "m", 5, "o") and a["slippageBps"] == 50


def test_short_keeps_loan_open_then_topup(env, monkeypatch):  # noqa: F811
    f, mk = env
    btc = _open(f, mk)
    calls, outs = [], iter([(478_070_233 - 964_000, "-61700", "S1"), (964_000, "-130", "S2")])

    def sw(cfg, plan, jup, **k):
        calls.append(plan["atoms"])
        got, spent, sig = next(outs)
        return ({"ok": True, "sig": sig, "deltas": {"nativeDeltaLamports": got, "tokenDeltaAtoms": {"WBTC": spent}}}, "ok", [])
    monkeypatch.setattr(sweepmod, "swap_with_backoff", sw)
    btc.ib_borrower_repay(100.0, 80000.0)
    ln = btc.local.bank["ib_loan"]
    assert json.loads(f.read_text())["status"] == "open" and abs(ln["repaid_got"] - 0.477106233) < 1e-12
    btc.ib_borrower_repay(100.0, 80000.0)
    assert calls == [478070233, 964000]
    rec = json.loads(f.read_text())
    assert rec["status"] == "repaid" and rec["repaid_got"] == 0.478070233 and rec["repay_sig"] == "S2"
    done = btc.local.bank["ib_loans_done"][-1]
    assert done["repay_sigs"] == ["S1", "S2"] and abs(done["repaid_coin"] - 0.0006183) < 1e-12


def test_failed_swap_retries_never_raises(env, monkeypatch):  # noqa: F811
    f, mk = env
    btc = _open(f, mk)
    monkeypatch.setattr(sweepmod, "swap_with_backoff", lambda *a, **k: ({"ok": False, "error": "x"}, "fatal", ["x"]))
    btc.ib_borrower_repay(100.0, 80000.0)
    assert json.loads(f.read_text())["status"] == "open" and btc.local.bank["ib_loan"].get("repaid_got") is None
    assert btc.local.bank["bal"] == 0.0012
