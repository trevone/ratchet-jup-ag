"""Per-position SOL flows (shared-wallet ledger). See live/src/jup.ts positionFlows.

On one wallet shared by the SOL / ETH / BTC bots, "wallet SOL after flat - wallet SOL before open" mixes every bot's
opens/closes, deposits and stake swaps into each trade. Instead each bot books the owner's SOL change of the
transactions that touched ITS position account (its own open/add/TP/cancel txs + the keepers' executions of them) since
the trade's open tx. Deposits, stake swaps and other markets never touch that account, so they can't be read as this
bot's profit or loss, and nothing is counted twice across bots.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("flows")
LAMPORTS = 1_000_000_000


def position_net_sol(position: str, open_sig: str | None, jup: Callable[..., dict[str, Any]] | None = None,
                     timeout: float | None = None, opts: dict[str, Any] | None = None) -> tuple[float | None, dict[str, Any]]:
    """(net SOL of the trade from its own position txs, raw result); (None, res) if unavailable."""
    if not position or not open_sig:
        return None, {"error": "no position / open_sig"}
    if jup is None:
        from live.live_broker import jup as _j
        jup = _j
    a = {"position": position, "sinceSig": open_sig, **(opts or {})}   # opts: incremental cache / budget (2026-10-31)
    res = jup("position-flows", a, timeout=timeout) if timeout else jup("position-flows", a)
    if not res.get("ok") or res.get("netLamports") is None:
        log.warning("position flows unavailable: %s", {k: res.get(k) for k in ("ok", "error", "missing", "nTx")})
        return None, res
    return int(res["netLamports"]) / LAMPORTS, res


def flow_extras(res: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """(token-account rent parked during the trade in SOL, realized gross P&L USD from position events) of a
    position-flows result; None for fields an older CLI did not return or when no decrease/liquidation event was seen."""
    if not res or not res.get("ok"):
        return None, None
    rent = int(res["rentLamports"]) / LAMPORTS if res.get("rentLamports") is not None else None
    gross = None
    if res.get("pnlUsd") is not None and (res.get("decreases") or res.get("liquidations")) and not res.get("eventErrors"):
        gross = round(float(res["pnlUsd"]), 6)
    return rent, gross


def flows_opts(lc: Any, cache_path: Any) -> tuple[float, dict[str, Any]]:
    """(subprocess hard timeout s, CLI options) for position-flows from [ledger] flows_* (2026-10-31: incremental cached read
    with a short time budget, paged with until=, small batches, 429 backoff)."""
    opts = {"budgetMs": int(lc.flows_budget_sec * 1000), "batch": int(lc.flows_batch), "retries": int(lc.flows_retries),
            "backoffMs": int(lc.flows_backoff_ms)}
    if lc.flows_cache:
        opts["cachePath"] = str(cache_path)
    return lc.flows_budget_sec + lc.flows_timeout_margin_sec, opts
