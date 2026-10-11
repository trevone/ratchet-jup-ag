"""LiveBroker: same interface as broker.SimBroker, routed to Jupiter Perps via the TypeScript CLI.

In ``dry_run`` (the default) every intent is turned into the real Jupiter request transaction and run
through ``simulateTransaction``; nothing is ever sent. A shadow SimBroker keeps the strategy's view of
the position consistent. Live sending is intentionally not implemented yet.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from broker import SimBroker
from config import ROOT, Config
from strategy import LONG, Action, Bar, ExitLevels, Intent

LIVE_DIR = ROOT / "live"
CLI = ["npx", "tsx", "src/cli.ts"]
LAMPORTS_PER_SOL = 1_000_000_000
CLI_TIMEOUT_SEC = 240
log = logging.getLogger("live")


def jup(cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one read-only / simulate command of the TS CLI and return its JSON result."""
    proc = subprocess.run([*CLI, cmd, json.dumps(args or {})], cwd=LIVE_DIR, capture_output=True,
                          text=True, timeout=CLI_TIMEOUT_SEC, env={**__import__("os").environ, "NODE_NO_WARNINGS": "1"})
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1]) if lines else {"ok": False, "error": proc.stderr[-300:]}


class LimitBreach(Exception):
    pass


@dataclass
class LiveBroker:
    cfg: Config
    shadow: SimBroker
    oracle_price: float = 0.0
    journal: list[dict[str, Any]] = field(default_factory=list)
    _tp_requests: list[str] = field(default_factory=list)

    # ---- views delegated to the shadow ledger (strategy-facing AccountView)
    @property
    def cash(self) -> float:
        return self.shadow.cash

    @property
    def position(self):  # noqa: ANN201 - broker.Position
        return self.shadow.position

    def equity(self, price: float) -> float:
        return self.shadow.equity(price)

    # ---- guards
    def _guard(self, it: Intent) -> None:
        lv = self.cfg.live
        if Path(ROOT / lv.kill_switch_path).exists() and it.action is not Action.CLOSE:
            raise LimitBreach("kill switch file present")
        if it.action is Action.CLOSE:
            return
        cur = self.position.size if self.position else 0.0
        if cur + it.margin * it.leverage > lv.max_position_usd:
            raise LimitBreach(f"size {cur + it.margin * it.leverage:.2f} > max_position_usd {lv.max_position_usd}")
        if it.leverage > lv.max_leverage:
            raise LimitBreach(f"leverage {it.leverage} > {lv.max_leverage}")
        if it.action is Action.OPEN and it.margin < lv.min_collateral_usd:
            raise LimitBreach(f"collateral ${it.margin:.2f} < Jupiter min ${lv.min_collateral_usd}")
        if self.cfg.account.start_equity - self.equity(it.price) > lv.max_daily_loss_usd:
            raise LimitBreach("max_daily_loss_usd reached")

    def _request(self, it: Intent) -> dict[str, Any]:
        """Build + simulate the Jupiter request for an intent (dry run never sends)."""
        slip = self.cfg.live.price_slippage_pct
        px = self.oracle_price or it.price
        side = "long" if it.side == LONG else "short"
        if it.action in (Action.OPEN, Action.ADD):
            if side == "short":
                return {"ok": False, "error": "short needs USDC collateral or a Jupiter quote for SOL->USDC swap (TODO)"}
            atoms = int(it.margin / px * LAMPORTS_PER_SOL)
            return jup("simulate-increase", {"side": side, "collateralAtoms": str(atoms),
                                             "sizeUsd": round(it.margin * it.leverage, 2),
                                             "priceSlippageUsd": round(px * (1 + slip), 2)})
        if it.reason is not None and it.reason.value == "tp_timer":
            # live: cancel the remaining TP trigger requests first (simulate-cancel per request id), then market-close
            for req in self._tp_requests:
                jup("simulate-cancel", {"request": req})
            self._tp_requests.clear()
        worst = px * (1 - slip) if side == "long" else px * (1 + slip)
        return jup("simulate-decrease", {"side": side, "entire": True, "priceSlippageUsd": round(worst, 2)})

    # ---- SimBroker interface
    def process_resting(self, bar: Bar, levels: ExitLevels | None) -> None:
        """On-chain TP/SL triggers fill on the keeper side; the shadow mirrors them for the strategy."""
        self.shadow.process_resting(bar, levels)

    def execute(self, intents: list[Intent], time: pd.Timestamp) -> None:
        for it in intents:
            rec: dict[str, Any] = {"time": str(time), "intent": it.action.value, "side": it.side,
                                   "margin": round(it.margin, 2), "lev": it.leverage, "ref_price": it.price}
            try:
                self._guard(it)
            except LimitBreach as e:
                rec.update(blocked=str(e))
                self.journal.append(rec)
                log.warning("blocked %s", rec)
                continue
            rec["simulation"] = self._request(it) if self.cfg.live.dry_run else {"error": "live send not implemented"}
            self.journal.append(rec)
            log.info("would %s: %s", it.action.value, json.dumps(rec)[:400])
            self.shadow.execute([it], time)

    def sync_tp_ladder(self, levels: ExitLevels) -> list[dict[str, Any]]:
        """(Re)place the TP ladder as on-chain trigger decreases after the average entry changed.
        Live: cancel stale requests in ``_tp_requests`` first (simulate-cancel), then create new ones."""
        pos = self.position
        if pos is None:
            return []
        side = "long" if pos.side == LONG else "short"
        out, prev = [], 0.0
        for tgt in levels.take_profits:
            frac = (tgt.close_pct - prev) / (1 - prev) if tgt.close_pct < 1 else 1.0
            args = {"side": side, "triggerPriceUsd": round(tgt.price, 2), "triggerAbove": pos.side == LONG}
            args.update({"entire": True} if tgt.close_pct >= 1 else {"sizeUsd": round(pos.size * tgt.close_pct, 2)})
            out.append({"tp": tgt.price, "close_pct": tgt.close_pct, "sim": jup("simulate-decrease", args)})
            prev = tgt.close_pct
            del frac
        return out
