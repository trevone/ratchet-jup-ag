"""Jupiter swap helpers shared by the bots (own-coin ledger: inter-bot loans, fee settlement, daily JupSOL withdrawal,
rescue): swap args with the [withdraw] slippage / impact caps, API-base fallback with backoff, tx-delta SOL result.

2026-10-14 (Trevor, minimalism): every short pays out in its own coin (SOL shorts: SOL; ETH / BTC shorts: desiredMint =
ETH / WBTC, live/bot.py cmd), so the old USDC / ETH / WBTC -> SOL self-healing sweep and its cross-bot USDC attribution
are gone. Stray USDC in the wallet is only logged (stray_usdc), never swapped.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("sweep")
LAMPORTS = 1_000_000_000
Jup = Callable[[str, dict[str, Any]], dict[str, Any]]
ROOT = Path(__file__).resolve().parent.parent            # tests redirect this (conftest)
ROUNDS_PER_TICK = 2                                       # in-tick rounds over all API bases
BACKOFF_SEC = 2.0                                         # in-tick wait before round 2, 3, ...: 2 s, 4 s, ...
_RETRYABLE = re.compile(r"\b(429|5\d\d)\b|timeout|timed out|fetch failed|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|"
                        r"socket hang up|aborted", re.I)


def sweep_args(cfg: Any, mint: str, atoms: int, out_mint: str | None = None, swap_mode: str | None = None) -> dict[str, Any]:
    """Swap args (slippage / impact caps from [withdraw]). out_mint None = old "sweep" to SOL; else the generic "swap"
    direction (own-coin ledger: USDC -> the bot's own coin, coin -> JupSOL, SOL <-> coin; pairs allowlisted in stake.ts)."""
    w = cfg.withdraw
    bps = int(w.unstake_slippage_bps)
    if not 1 <= bps <= w.stake_max_slippage_bps:
        raise ValueError("sweep slippage outside 1..withdraw.stake_max_slippage_bps")
    a = {"direction": "sweep", "inputMint": mint, "amountAtoms": str(int(atoms)), "slippageBps": bps,
         "maxPriceImpactPct": w.stake_max_price_impact_pct, "apiBases": list(w.stake_api_bases),
         "onlyDirectRoutes": w.stake_only_direct_routes}
    if out_mint is not None:
        a.update(direction="swap", outputMint=out_mint)
        if swap_mode is not None:   # "ExactOut" (inter-bot loan repay): atoms = exact output atoms
            a["swapMode"] = swap_mode
    return a


def swap_sol_change(d: dict[str, Any]) -> tuple[float, float]:
    """(SOL result, token-account rent) of one confirmed swap from its tx deltas (live/src/stake.ts txDeltas).

    Rent fix 2026-10-07: SOL result = native + wrapped-SOL change + ``rentDeltaLamports``: rent that moved into (or out
    of) the owner's own token accounts (e.g. a re-created wSOL ATA, ~0.0015 SOL) is still the owner's recoverable capital,
    not a swap cost. Older CLI output without that field -> rent 0 (old formula)."""
    rent = int(d.get("rentDeltaLamports") or 0) / LAMPORTS
    sol = d["nativeDeltaLamports"] / LAMPORTS + int(d.get("wsolDeltaAtoms") or 0) / LAMPORTS + rent
    return round(sol, 9), round(rent, 9)


def classify(res: dict[str, Any]) -> str:
    """ok | retry (Jupiter API 429 / 5xx / timeout BEFORE anything was sent: safe to ask again) | unknown (sent or
    possibly sent but unconfirmed: never re-sent this tick) | fatal (e.g. quote rejected, price impact over the cap)."""
    if res.get("ok") and res.get("sig"):
        return "ok"
    if res.get("sent") or res.get("sig"):
        return "unknown"
    err = str(res.get("error") or res.get("err") or "")
    if "jupiter api failed" in err or err.startswith("Error: swap-instructions") or "swap-instructions:" in err:
        return "retry" if _RETRYABLE.search(err) else "fatal"
    if "quote mismatch" in err or "price impact" in err or "not allowed" in err or "slippageBps" in err:
        return "fatal"
    return "unknown"   # e.g. CLI crash / RPC error during send: could have landed -> re-read next tick


def swap_with_backoff(cfg: Any, plan: dict[str, Any], jup: Jup, sleep: Callable[[float], None] = time.sleep,
                      rounds: int = ROUNDS_PER_TICK, out_mint: str | None = None,
                      swap_mode: str | None = None) -> tuple[dict[str, Any], str, list[str]]:
    """One token -> SOL. Each attempt pins ONE API base (api.jup.ag, then lite-api.jup.ag); only 'retry' failures are
    asked again, with exponential backoff between rounds. Returns (last result, kind, attempt log)."""
    bases = list(cfg.withdraw.stake_api_bases)
    log_: list[str] = []
    res: dict[str, Any] = {"ok": False, "error": "no attempt"}
    kind = "fatal"
    for r in range(max(1, rounds)):
        if r:
            wait = BACKOFF_SEC * 2 ** (r - 1) * (1.0 + 0.25 * random.random())
            log_.append(f"wait {wait:.1f}s")
            sleep(wait)
        for base in bases:
            args = {**sweep_args(cfg, plan["mint"], plan["atoms"], out_mint, swap_mode), "apiBases": [base], "send": True}
            os.environ["JUP_LIVE"] = "1"
            try:
                res = jup("swap", args)
            except Exception as e:  # noqa: BLE001 - CLI timeout / crash: outcome unknown
                res = {"ok": False, "error": f"cli exception: {str(e)[:200]}"}
            kind = classify(res)
            log_.append(f"{base.split('//')[-1].split('/')[0]}:{kind}" + ("" if kind == "ok" else f" ({str(res.get('error') or res.get('err'))[:120]})"))
            if kind != "retry":
                return res, kind, log_
    return res, kind, log_


def stray_usdc(st: dict[str, Any], min_usd: float) -> float:
    """USD of USDC sitting in the wallet (no short pays USDC any more): logged as a WARNING, never swapped."""
    usdc = int(((st.get("sweepTokens") or {}).get("USDC") or {}).get("atoms") or 0) / 1e6
    if usdc >= min_usd:
        log.warning("sweep: stray USDC $%.2f in the wallet (no bot expects USDC; not swapped, not booked)", usdc)
    else:
        log.info("sweep: ok (no stray USDC)")
    return usdc
