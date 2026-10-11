"""Shared-wallet budget split for the SOL / ETH / BTC bots (one wallet, native SOL margin).

    total_usd  = (native SOL - reserve) * SOL price + collateral (USD) of EVERY open position of the wallet (all markets)
    free_usd   = share * total_usd - this bot's own open collateral
    free_usd   = clamp(free_usd, 0, (native SOL - reserve) * SOL price)       # never more than is really in the wallet

With shares summing to <= 1, the bots can never spend more together than the wallet holds and a bot can never use
another bot's share: whatever bot A has open is subtracted only from A's own share, and B's cap does not grow when A
opens (A's collateral moves from "native" to "open collateral", total stays the same). share = 1.0 with a single bot
gives exactly the old behaviour (free = native SOL - reserve). Pure functions; unit-tested in tests/test_multi_asset.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Budget:
    share: float
    native_free_usd: float      # (native SOL - reserve) * SOL price, >= 0
    open_collateral_usd: float  # all bots
    own_collateral_usd: float   # this bot
    total_usd: float
    share_usd: float            # share * total
    free_usd: float             # what this bot may still commit (first entry / adds)

    def line(self, asset: str) -> str:
        return (f"budget {asset}: share {self.share:.0%} of total ${self.total_usd:.2f} (native free ${self.native_free_usd:.2f} + "
                f"open collateral ${self.open_collateral_usd:.2f}) = ${self.share_usd:.2f}; own used ${self.own_collateral_usd:.2f}"
                f" -> free ${self.free_usd:.2f}")


def open_collateral(all_positions: Iterable[dict[str, Any]]) -> float:
    return float(sum(float(p.get("collateralUsd") or 0.0) for p in all_positions if float(p.get("sizeUsd") or 0.0) > 0))


def own_collateral(all_positions: Iterable[dict[str, Any]], market: str) -> float:
    return float(sum(float(p.get("collateralUsd") or 0.0) for p in all_positions
                     if p.get("market") == market and float(p.get("sizeUsd") or 0.0) > 0))


def compute_budget(share: float, native_sol: float, sol_price: float, reserve_sol: float,
                   all_collateral_usd: float, own_collateral_usd: float) -> Budget:
    if not 0.0 < share <= 1.0:
        raise ValueError("share must be in (0, 1]")
    native_free = max(0.0, (native_sol - reserve_sol) * sol_price)
    total = native_free + max(0.0, all_collateral_usd)
    share_usd = share * total
    free = min(max(0.0, share_usd - max(0.0, own_collateral_usd)), native_free)
    return Budget(share, native_free, all_collateral_usd, own_collateral_usd, total, share_usd, free)


def budget_from_state(st: dict[str, Any], share: float, reserve_sol: float, market: str,
                      own_pos_collateral: float | None = None) -> Budget:
    """From a TS ``state`` result. Old single-market states (no allPositions / solOracle) fall back to this market's own
    position and its oracle, i.e. the original single-bot numbers."""
    sol_px = float((st.get("solOracle") or {}).get("price") or st["oracle"]["price"])
    allp = st.get("allPositions")
    if allp is None:   # legacy state: only this market's positions are known
        allp = [{"market": market, "sizeUsd": p.get("sizeUsd") or 0, "collateralUsd": p.get("collateralUsd") or 0}
                for p in st["positions"].values()]
    own = own_collateral(allp, market) if own_pos_collateral is None else own_pos_collateral
    return compute_budget(share, float(st["wallet"]["sol"]), sol_px, reserve_sol, open_collateral(allp), own)
