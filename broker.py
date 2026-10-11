"""Simulated Jupiter-Perps-style executor: fills, fees, borrow, liquidation, trade log.

Conventions: sizes are USD notional, ``size = margin * leverage``; ``qty`` is SOL
(sum of size_i / fill_i), so ``avg_entry = size / qty`` is the size-weighted harmonic mean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from config import CostsConfig, RiskConfig
from strategy import LONG, Action, Bar, ExitLevels, ExitReason, Intent, TpTarget

FillCallback = Callable[[pd.Timestamp, Action], None]


@dataclass(slots=True)
class Position:
    side: int
    opened: pd.Timestamp
    margin: float = 0.0
    size: float = 0.0
    qty: float = 0.0
    borrow: float = 0.0            # accrued borrow fees, USD
    fees: float = 0.0              # open fees paid, USD
    n_entries: int = 0
    last_leverage: float = 0.0
    first_margin: float = 0.0
    first_entry: float = 0.0
    last_entry: float = 0.0
    base_qty: float = 0.0          # all qty ever opened; partial-TP close_pct refers to this
    closed_qty: float = 0.0
    tp_hits: int = 0
    first_tp_time: pd.Timestamp | None = None
    committed_margin: float = 0.0  # all margin ever posted
    realized: float = 0.0          # net pnl of partial closes (after their share of all costs)
    close_fees: float = 0.0
    realized_borrow: float = 0.0
    realized_open_fees: float = 0.0
    slippage: float = 0.0          # USD cost of slippage on all fills
    max_adverse: float = 0.0       # worst close-based net unrealized pnl, USD

    @property
    def avg_entry(self) -> float:
        return self.size / self.qty

    @property
    def eff_leverage(self) -> float:
        return self.size / self.margin

    def pnl(self, price: float) -> float:
        return self.side * (self.qty * price - self.size)

    def price_for_net_pnl(self, net_pnl: float) -> float:
        """Price at which ``pnl(price) - borrow == net_pnl``."""
        return (self.size + self.side * (self.borrow + net_pnl)) / self.qty

    def liquidation_price(self, maintenance_ratio: float) -> float:
        """Price where remaining collateral ``margin + pnl - borrow`` equals ``ratio * size``."""
        return self.price_for_net_pnl(maintenance_ratio * self.size - self.margin)


@dataclass
class SimBroker:
    costs: CostsConfig
    risk: RiskConfig
    cash: float
    bar_hours: float
    on_fill: FillCallback | None = None
    position: Position | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    # ----------------------------------------------------------------- views
    def equity(self, price: float) -> float:
        pos = self.position
        if pos is None:
            return self.cash
        return self.cash + pos.margin + pos.pnl(price) - pos.borrow

    def liquidation_price(self) -> float | None:
        return None if self.position is None else self.position.liquidation_price(self.risk.maintenance_ratio)

    # ----------------------------------------------------------------- intrabar
    def process_resting(self, bar: Bar, levels: ExitLevels | None) -> None:
        """Check liquidation/stop/TP inside the bar, conservatively: gap at the open first,
        then the adverse extreme, then the favourable one. Accrues borrow if still open."""
        pos = self.position
        if pos is None or levels is None:
            return
        s = pos.side

        def worse(a: float, b: float) -> bool:   # a at/beyond b in the losing direction
            return a <= b if s == LONG else a >= b

        def better(a: float, b: float) -> bool:  # a at/beyond b in the winning direction
            return a >= b if s == LONG else a <= b

        adverse = bar.low if s == LONG else bar.high
        favourable = bar.high if s == LONG else bar.low
        liq = pos.liquidation_price(self.risk.maintenance_ratio)
        stop = levels.stop
        stop_first = stop is not None and not worse(stop.price, liq)

        if worse(bar.open, liq):
            self._close(bar.time, bar.open, ExitReason.LIQUIDATION, slip=False)
        elif stop is not None and stop_first and worse(bar.open, stop.price):
            self._close(bar.time, bar.open, stop.reason)
        elif stop is not None and stop_first and worse(adverse, stop.price):
            self._close(bar.time, stop.price, stop.reason)
        elif worse(adverse, liq):
            self._close(bar.time, liq, ExitReason.LIQUIDATION, slip=False)
        else:
            self._take_profits(bar, levels.take_profits, better, favourable)
            if self.position is None:
                return
            pos.borrow += pos.size * self.costs.borrow_per_hour * self.bar_hours
            pos.max_adverse = min(pos.max_adverse, pos.pnl(bar.close) - pos.borrow)

    def _take_profits(self, bar: Bar, targets: tuple[TpTarget, ...],
                      better: Callable[[float, float], bool], favourable: float) -> None:
        for tgt in targets:
            if better(bar.open, tgt.price):
                px = bar.open                      # gapped through the level
            elif better(favourable, tgt.price):
                px = tgt.price
            else:
                return
            if tgt.close_pct >= 1.0:
                self._close(bar.time, px, ExitReason.TAKE_PROFIT)
                return
            self._partial_close(bar.time, px, tgt.close_pct)

    def _partial_close(self, t: pd.Timestamp, price: float, close_pct: float) -> None:
        """Close down to ``close_pct`` of ``base_qty`` closed; costs pro-rata, close fee on closed size."""
        pos = self.position
        assert pos is not None
        pos.tp_hits += 1
        if pos.first_tp_time is None:
            pos.first_tp_time = t
        qty = close_pct * pos.base_qty - pos.closed_qty
        if qty <= 0:
            return
        frac = qty / pos.qty
        fill = self._slipped(price, pos.side, opening=False)
        margin, size, borrow, open_fees = (frac * x for x in (pos.margin, pos.size, pos.borrow, pos.fees))
        close_fee = size * self.costs.close_fee
        net = pos.side * (qty * fill - size) - borrow - close_fee
        self.cash += margin + net
        pos.realized += net - open_fees
        pos.margin -= margin
        pos.size -= size
        pos.qty -= qty
        pos.borrow -= borrow
        pos.fees -= open_fees
        pos.closed_qty += qty
        pos.close_fees += close_fee
        pos.slippage += abs(fill - price) * qty
        pos.realized_borrow += borrow
        pos.realized_open_fees += open_fees
        self.events.append(dict(time=t, type="partial_close", reason=ExitReason.PARTIAL_TP.value,
                                price=fill, qty=qty, pnl=net - open_fees))

    # ----------------------------------------------------------------- intents
    def execute(self, intents: list[Intent], time: pd.Timestamp) -> None:
        for it in intents:
            if it.action is Action.CLOSE:
                if self.position is not None:
                    assert it.reason is not None
                    self._close(time, it.price, it.reason)
            else:
                self._open_or_add(time, it)

    def _slipped(self, price: float, side: int, opening: bool) -> float:
        pays_up = (side == LONG) == opening   # buying -> fill above reference
        s = self.costs.slippage
        return price * (1 + s) if pays_up else price * (1 - s)

    def _open_or_add(self, t: pd.Timestamp, it: Intent) -> None:
        size = it.margin * it.leverage
        fee = size * self.costs.open_fee
        if it.margin <= 0 or it.margin + fee > self.cash:
            return  # insufficient free collateral: intent rejected
        fill = self._slipped(it.price, it.side, opening=True)
        pos = self.position
        if pos is None:
            pos = self.position = Position(side=it.side, opened=t, first_margin=it.margin, first_entry=fill)
        self.cash -= it.margin + fee
        pos.margin += it.margin
        pos.size += size
        pos.qty += size / fill
        pos.base_qty += size / fill
        pos.committed_margin += it.margin
        pos.slippage += abs(fill - it.price) * size / fill
        pos.fees += fee
        pos.n_entries += 1
        pos.last_leverage = it.leverage
        pos.last_entry = fill
        if pos.n_entries > 1 and pos.tp_hits:   # add after a partial: fresh ladder on the whole position
            pos.tp_hits, pos.first_tp_time, pos.closed_qty, pos.base_qty = 0, None, 0.0, pos.qty
        action = Action.OPEN if pos.n_entries == 1 else Action.ADD
        self.events.append(dict(time=t, type=action.value, side=it.side, price=fill, margin=it.margin,
                                lev=it.leverage, size=size, avg=pos.avg_entry, eff_lev=pos.eff_leverage,
                                liq=self.liquidation_price()))
        if self.on_fill:
            self.on_fill(t, action)

    def _close(self, t: pd.Timestamp, price: float, reason: ExitReason, slip: bool = True) -> None:
        pos = self.position
        assert pos is not None
        fill = self._slipped(price, pos.side, opening=False) if slip else price
        if reason is ExitReason.LIQUIDATION:
            close_fee = 0.0
            pnl_net = -pos.margin              # all collateral lost
        else:
            close_fee = pos.size * self.costs.close_fee
            gross = pos.pnl(fill)
            pnl_net = gross - pos.borrow - close_fee
            self.cash += pos.margin + pnl_net
        if slip:
            pos.slippage += abs(fill - price) * pos.qty
        pnl_all = pnl_net - pos.fees + pos.realized
        fees = pos.fees + pos.realized_open_fees + pos.close_fees + close_fee
        borrow = pos.borrow + pos.realized_borrow
        self.trades.append(dict(
            opened=pos.opened, closed=t, side=pos.side, reason=reason.value, exit_price=fill,
            first_entry=pos.first_entry, avg_entry=pos.avg_entry, entries=pos.n_entries,
            adds=pos.n_entries - 1, tp_hits=pos.tp_hits, margin=pos.committed_margin,
            size=pos.size, eff_lev=pos.eff_leverage, borrow=borrow, fees=fees, slippage=pos.slippage,
            pnl=pnl_all, ret_on_margin=pnl_all / pos.committed_margin,
            equity_after=self.cash, max_adverse=pos.max_adverse,
            hours=(t - pos.opened) / pd.Timedelta(hours=1)))
        self.events.append(dict(time=t, type=Action.CLOSE.value, reason=reason.value, price=fill, pnl=pnl_all))
        self.position = None
        if self.on_fill:
            self.on_fill(t, Action.CLOSE)

    def snapshot(self, price: float) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        return dict(side=pos.side, entries=pos.n_entries, margin=pos.margin, size=pos.size,
                    avg=pos.avg_entry, eff_lev=pos.eff_leverage, liq=self.liquidation_price(),
                    upnl=pos.pnl(price) - pos.borrow)
