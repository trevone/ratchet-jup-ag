"""2026-10-14: no bot expects USDC; a stray balance is logged (WARNING), never swapped. Swap helper fallback kept."""
import logging

from live import sweep as sw

OK = {"ok": True, "sig": "S"}


def test_stray_usdc_logged_only(caplog):
    with caplog.at_level(logging.INFO, logger="sweep"):
        assert sw.stray_usdc({"sweepTokens": {"USDC": {"atoms": "15257304"}}}, 0.10) == 15.257304
        assert sw.stray_usdc({"sweepTokens": {}}, 0.10) == 0.0
    assert "stray USDC $15.26" in caplog.text and "sweep: ok" in caplog.text


class C:
    class withdraw:
        unstake_slippage_bps, stake_max_slippage_bps, stake_max_price_impact_pct = 50, 100, 1.0
        stake_api_bases, stake_only_direct_routes = ["https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1"], False


def test_swap_with_backoff_falls_back_and_never_resends_unknown():
    calls = []
    res = [{"ok": False, "error": "Error: jupiter api failed: https://api.jup.ag/swap/v1 429"}, OK]
    out, kind, _ = sw.swap_with_backoff(C(), {"mint": "m", "atoms": 5}, lambda c, a: calls.append(a) or res.pop(0),
                                        sleep=lambda s: None)
    assert kind == "ok" and [a["apiBases"][0] for a in calls] == C.withdraw.stake_api_bases
    calls.clear()
    out, kind, _ = sw.swap_with_backoff(C(), {"mint": "m", "atoms": 5}, lambda c, a: calls.append(a) or {"ok": False, "sent": True},
                                        sleep=lambda s: None)
    assert kind == "unknown" and len(calls) == 1
