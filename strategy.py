"""Trend-filtered RSI mean reversion with DCA averaging-in (SOL, Jupiter Perps).

The strategy is pure decision logic: it consumes closed signal-TF bars plus a read-only
view of the account and emits :class:`Intent` objects (open / add / close) and resting
:class:`ExitLevels` (take profit, stops). It never touches cash, fills or fees; those belong
to an executor (``broker.SimBroker`` for backtests, later a paper or Jupiter executor).

Per-bar protocol (identical for backtest, paper and live)::

    strategy.begin_bar(bar, account)                    # period bookkeeping at the bar open
    executor.process_resting(bar, strategy.exit_levels(pos))   # TP/stop/liq hit intrabar
    executor.execute(strategy.exit_intents(bar, account))      # bar-close exits
    executor.execute(strategy.entry_intents(bar, account))     # bar-close entries/adds

Every fill is reported back through :meth:`DcaStrategy.on_fill`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import pandas as pd

from typing import Callable

from config import Config
from dca_levels import AddPlan, LadderParams, Levels, SmartTpParams, SmartTpResult, plan_add, project_ladder, smart_tps

LONG, SHORT = 1, -1


class Action(str, Enum):
    OPEN = "open"
    ADD = "add"
    CLOSE = "close"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    PARTIAL_TP = "partial_tp"
    MARGIN_STOP = "hard_stop"          # name kept for continuity with v1 outputs
    PRICE_STOP = "price_stop"
    BREAKEVEN = "breakeven_stop"
    TREND_FLIP = "trend_flip"
    MAX_TIME = "max_time"
    TP_TIMER = "tp_timer"
    LIQUIDATION = "liquidation"


@dataclass(frozen=True, slots=True)
class Bar:
    """A closed signal-TF candle with its features."""
    time: pd.Timestamp                 # open time (UTC)
    open: float
    high: float
    low: float
    close: float
    rsi: float
    trend_up: bool | None              # None while the trend filter warms up
    ma_htf: float = float("nan")       # trend MA value (4h EMA200) known at this bar's close
    sma: float = float("nan")          # signal-TF SMA (15m SMA10) at this bar's close


@dataclass(frozen=True, slots=True)
class Intent:
    action: Action
    side: int
    price: float                       # reference price (bar close); executor adds slippage
    margin: float = 0.0                # open/add only, USD collateral
    leverage: float = 0.0              # open/add only
    reason: ExitReason | None = None   # close only


@dataclass(frozen=True, slots=True)
class StopLevel:
    price: float
    reason: ExitReason


@dataclass(frozen=True, slots=True)
class TpTarget:
    price: float
    close_pct: float                   # cumulative fraction of the position closed after this fill


@dataclass(frozen=True, slots=True)
class ExitLevels:
    take_profits: tuple[TpTarget, ...]  # remaining ladder, nearest first
    stop: StopLevel | None              # the nearest active protective stop


class PositionView(Protocol):
    side: int
    opened: pd.Timestamp
    margin: float
    size: float
    qty: float
    borrow: float
    n_entries: int
    last_leverage: float
    first_margin: float
    first_entry: float
    last_entry: float
    tp_hits: int                       # partial TP levels already filled
    first_tp_time: pd.Timestamp | None  # bar time when the first partial TP filled

    @property
    def avg_entry(self) -> float: ...

    def price_for_net_pnl(self, net_pnl: float) -> float: ...


class AccountView(Protocol):
    @property
    def cash(self) -> float: ...

    @property
    def position(self) -> PositionView | None: ...

    def equity(self, price: float) -> float: ...


class DcaStrategy:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._period: pd.Timestamp | None = None
        self._period_start_equity = cfg.account.start_equity
        self._blocked_period: pd.Timestamp | None = None
        self._exited_at: pd.Timestamp | None = None
        self.skip_log: list[str] = []
        self._flat_since: pd.Timestamp | None = None
        self.levels_provider: Callable[[pd.Timestamp], Levels | None] | None = None
        self._levels: Levels | None = None
        self.last_tp: SmartTpResult | None = None   # smart-TP result of the last tp_targets call (reasons for the log)

    # ----------------------------------------------------------------- callbacks
    def on_fill(self, time: pd.Timestamp, action: Action) -> None:
        if action is Action.CLOSE:
            self._exited_at = time

    # ----------------------------------------------------------------- phase 1
    def begin_bar(self, bar: Bar, account: AccountView) -> None:
        period = bar.time.floor(self.cfg.stops.daily_period)
        if period != self._period:
            self._period = period
            self._period_start_equity = account.equity(bar.open)

    # ----------------------------------------------------------------- phase 2
    def exit_levels(self, pos: PositionView) -> ExitLevels:
        """Resting take-profit and nearest stop for the open position."""
        tp_cfg, st = self.cfg.tp, self.cfg.stops
        s = pos.side
        targets = self.tp_targets(pos)[pos.tp_hits:]

        stops: list[StopLevel] = []
        if st.margin_loss_pct is not None:
            stops.append(StopLevel(pos.price_for_net_pnl(-st.margin_loss_pct * pos.margin),
                                   ExitReason.MARGIN_STOP))
        if st.price_stop_pct is not None:
            stops.append(StopLevel(pos.avg_entry * (1 - s * st.price_stop_pct), ExitReason.PRICE_STOP))
        if tp_cfg.move_stop_to_breakeven_after_partial and pos.tp_hits > 0:
            stops.append(StopLevel(pos.price_for_net_pnl(0.0), ExitReason.BREAKEVEN))
        # nearest stop = the one hit first on an adverse move
        nearest = max(stops, key=lambda x: s * x.price) if stops else None
        return ExitLevels(take_profits=targets, stop=nearest)

    def tp_targets(self, pos: PositionView, price: float | None = None) -> tuple[TpTarget, ...]:
        """Full ladder on the current position. tp.tp1_pct / tp2_pct set: TP1 / TP2 = avg x (1 +/- pct) (long above,
        short below the average), else the tp.levels distances.

        tp.smart_tp2: fee floors (a floor beyond the pct price wins), TP1 inside TP2 and, when ``price`` is given, no
        trigger at/through market. Result details land in ``self.last_tp``."""
        tp_cfg = self.cfg.tp
        anchor = pos.avg_entry if tp_cfg.anchor == "avg_entry" else pos.first_entry
        prices = [self._tp_price(pos, anchor, lv.dist) for lv in tp_cfg.ladder]
        if tp_cfg.tp1_pct is not None:
            prices = [pos.avg_entry * (1 + pos.side * tp_cfg.tp1_pct), pos.avg_entry * (1 + pos.side * tp_cfg.tp2_pct)]
        self.last_tp = None
        if tp_cfg.smart_tp2:
            res = smart_tps(pos.side, pos.avg_entry, pos.size, prices, [x.close_pct for x in tp_cfg.ladder],
                            pos.tp_hits, self.smart_tp_params(), market=price)
            self.last_tp = res
            prices = list(res.prices)
        return tuple(TpTarget(p, lv_.close_pct) for p, lv_ in zip(prices, tp_cfg.ladder))

    def last_tp_raw(self, i: int) -> float:
        r = self.last_tp
        return r.unclamped[i] if r is not None and r.unclamped else float("nan")

    def smart_tp_params(self) -> SmartTpParams:
        t, c = self.cfg.tp, self.cfg.costs
        return SmartTpParams(min_net_usd=t.tp_min_net_usd, min_gap_usd=t.tp_min_gap_usd, market_gap=t.tp_market_gap,
                             open_fee=c.open_fee, close_fee=c.close_fee)

    def _tp_price(self, pos: PositionView, anchor: float, dist: float) -> float:
        mode = self.cfg.tp.mode
        if mode == "price_pct":
            return anchor * (1 + pos.side * dist)
        if mode == "price_abs":
            return anchor + pos.side * dist
        return pos.price_for_net_pnl(dist * pos.margin)

    # ----------------------------------------------------------------- phase 3
    def exit_intents(self, bar: Bar, account: AccountView) -> list[Intent]:
        pos, st = account.position, self.cfg.stops
        if pos is None:
            return []
        timer = self.cfg.tp.close_remainder_after_min
        if timer is not None and pos.first_tp_time is not None:
            # bar granularity: evaluated at bar close, so the effective delay is rounded up to a bar
            if (bar.time + self._bar_duration - pos.first_tp_time >= pd.Timedelta(minutes=timer)
                    and (not self.cfg.tp.timer_require_profit or self.net_close_pnl(pos, bar.close) > 0)):
                return [Intent(Action.CLOSE, pos.side, bar.close, reason=ExitReason.TP_TIMER)]
        if st.max_hours_in_trade is not None:
            held = (bar.time + self._bar_duration - pos.opened) / pd.Timedelta(hours=1)
            if held >= st.max_hours_in_trade:
                return [Intent(Action.CLOSE, pos.side, bar.close, reason=ExitReason.MAX_TIME)]
        if st.trend_flip_exit and bar.trend_up is not None:
            if (pos.side == LONG) != bar.trend_up:
                return [Intent(Action.CLOSE, pos.side, bar.close, reason=ExitReason.TREND_FLIP)]
        return []

    # ----------------------------------------------------------------- phase 4
    def entry_intents(self, bar: Bar, account: AccountView) -> list[Intent]:
        if bar.trend_up is None or pd.isna(bar.rsi):
            return []
        if self._daily_limit_hit(bar, account) or self._exited_at == bar.time:
            return []
        sig = self.cfg.signal
        pos = account.position
        if pos is None and self._flat_since is None:
            self._flat_since = bar.time
        long_th, short_th, _ = self.entry_thresholds(bar) if pos is None else (sig.oversold, sig.overbought, "")
        oversold, overbought = bar.rsi < long_th, bar.rsi > short_th
        if pos is None and sig.entry_rule == "price_ma":
            side, _ = self.price_ma_side(bar)
            if side is None:
                return []
            return [Intent(Action.OPEN, side, bar.close, self.initial_margin(account.cash),
                           self.cfg.dca.lev_start)]
        if pos is None:
            if self._flat_too_long(bar):  # relaxed entry: RSI merely on the trend-friendly side of the midline
                oversold, overbought = bar.rsi < sig.flat_rsi_mid, bar.rsi > sig.flat_rsi_mid
            side = self._entry_side(bar.trend_up, oversold, overbought)
            if side is None:
                return []
            return [Intent(Action.OPEN, side, bar.close, self.initial_margin(account.cash),
                           self.cfg.dca.lev_start)]
        self._flat_since = None
        if self.cfg.dca.mode == "levels":
            plan = self.plan_next_add(pos, account.cash, bar.time)
            if plan is not None and pos.side * (bar.close - plan.trigger) <= 0 and self._adds_left(pos):
                return [Intent(Action.ADD, pos.side, bar.close, plan.margin, plan.leverage)]
            return []
        if self._add_due(pos, bar.close, oversold, overbought):
            return [Intent(Action.ADD, pos.side, bar.close, self.add_margin(pos),
                           self.add_leverage(pos))]
        return []

    @staticmethod
    def _vs(px: float, line: float) -> int:
        """+1 price above the line (long signal), -1 below (short signal), 0 equal (no signal)."""
        return 1 if px > line else -1 if px < line else 0

    def price_ma_side(self, bar: Bar) -> tuple[int | None, str]:
        """Entry rule price_ma: (LONG / SHORT / None, log text). Price = the bar close; lines = 4h EMA200, 15m SMA10."""
        t, sg = self.cfg.trend, self.cfg.signal
        px, ema, sma = bar.close, bar.ma_htf, bar.sma
        names = (f"{t.tf} {t.ma_type.upper()}{t.length}", f"{sg.tf} SMA{sg.entry_sma_length}")
        if pd.isna(ema) or pd.isna(sma):
            return None, f"entry check: price {px:.4f}; {names[0]} / {names[1]} warming up -> stay flat"
        a, b = self._vs(px, ema), self._vs(px, sma)
        word = {1: "long", -1: "short", 0: "none (equal)"}
        side = None
        if a == b == 1 and self.cfg.allow_long:
            side = LONG
        elif a == b == -1 and self.cfg.allow_short:
            side = SHORT
        why = ("OPEN LONG" if side == LONG else "OPEN SHORT" if side == SHORT
               else "stay flat (" + ("side disabled" if a == b != 0 else "signals disagree / equal") + ")")
        return side, (f"entry check: price {px:.4f} vs {names[0]} {ema:.4f} -> {word[a]}; vs {names[1]} {sma:.4f} -> "
                      f"{word[b]} => {why} (RSI off)")

    def idle_step(self, now: pd.Timestamp) -> tuple[int, float]:
        """(loosening step, idle minutes) for a flat bot at ``now`` (bar close); step 0 = normal thresholds."""
        sig = self.cfg.signal
        if self._flat_since is None:
            return 0, 0.0
        idle = (now - self._flat_since) / pd.Timedelta(minutes=1)
        if not sig.idle_enabled or idle < sig.idle_after_min:
            return 0, idle
        n = max(len(sig.idle_long_rsi_steps), len(sig.idle_short_rsi_steps))
        return min(n, 1 + int((idle - sig.idle_after_min) // sig.idle_step_min)), idle

    def entry_thresholds(self, bar: Bar) -> tuple[float, float, str]:
        """Effective (long RSI <, short RSI >, description) for a fresh entry on ``bar`` (idle loosening applied).
        Entry rule price_ma: (nan, nan, the price-vs-EMA/SMA check)."""
        if self.cfg.signal.entry_rule == "price_ma":
            return float("nan"), float("nan"), self.price_ma_side(bar)[1]
        return self.thresholds_at(bar.time + self._bar_duration)

    def thresholds_at(self, now: pd.Timestamp) -> tuple[float, float, str]:
        if self.cfg.signal.entry_rule == "price_ma":
            t, sg = self.cfg.trend, self.cfg.signal
            return float("nan"), float("nan"), (f"entry rule price_ma: LONG when price > {t.tf} {t.ma_type.upper()}{t.length} "
                    f"AND > {sg.tf} SMA{sg.entry_sma_length}; SHORT when below both; else flat (RSI / idle loosening off)")
        sig = self.cfg.signal
        step, idle = self.idle_step(now)
        lo, hi = sig.oversold, sig.overbought
        if step:
            ls, ss = sig.idle_long_rsi_steps, sig.idle_short_rsi_steps
            if ls:
                lo = max(lo, min(ls[min(step, len(ls)) - 1], sig.idle_max_long_rsi))
            if ss:
                hi = min(hi, max(ss[min(step, len(ss)) - 1], sig.idle_min_short_rsi))
        n = max(len(sig.idle_long_rsi_steps), len(sig.idle_short_rsi_steps))
        desc = (f"long RSI < {lo:g} / short RSI > {hi:g} (base {sig.oversold:g}/{sig.overbought:g}; idle {idle:.0f} min, "
                f"step {step}/{n}" + ("" if sig.idle_enabled else ", idle loosening off") + ")")
        return lo, hi, desc

    # ----------------------------------------------------------------- sizing
    def initial_margin(self, cash: float) -> float:
        """First-entry collateral (USD) from the free collateral ``cash`` at the moment of the open.

        entry.first_collateral_mode = "fixed": legacy [sizing] (fixed_usd / equity_pct).
        "pct": clamp(pct * cash, min_usd, max_usd), then never above cash - fee_reserve_usd, so first entry + the
        ladder (which budgets from the free collateral left after the open) can't exceed free collateral. If that
        clamp drops it under the floor the live guard (min_collateral_usd / SOL reserve) skips the open."""
        e = self.cfg.entry
        if e.first_collateral_mode == "fixed":
            sz = self.cfg.sizing
            return sz.initial_margin_pct * cash if sz.mode == "equity_pct" else sz.initial_margin_usd
        m = max(e.first_collateral_pct * max(cash, 0.0), e.first_collateral_min_usd)
        if e.first_collateral_max_usd is not None:
            m = min(m, e.first_collateral_max_usd)
        return round(max(0.0, min(m, cash - e.fee_reserve_usd)), 2)

    def add_margin(self, pos: PositionView) -> float:
        d = self.cfg.dca
        if d.add_margin_usd is not None:
            return d.add_margin_usd
        k = pos.n_entries  # number of the add about to be made (1-based)
        return pos.first_margin * d.margin_mult * d.margin_growth ** (k - 1)

    def add_leverage(self, pos: PositionView) -> float:
        d = self.cfg.dca
        return min(d.lev_cap, pos.last_leverage + d.lev_step)

    # ----------------------------------------------------------------- helpers
    @property
    def _bar_duration(self) -> pd.Timedelta:
        return pd.Timedelta(self.cfg.signal.tf)

    def liq_maint(self) -> float:
        """Maintenance fraction of size for the liq-buffer check. "jupiter": 1/max_leverage + close fee (custody values
        when the bot has read them, else [risk] jup_max_leverage / [costs] close_fee); "legacy": maintenance_ratio."""
        r = self.cfg.risk
        if r.liq_model != "jupiter":
            return r.maintenance_ratio
        ml = getattr(self, "liq_max_lev", None) or r.jup_max_leverage
        cf = getattr(self, "liq_close_fee", None)
        return 1.0 / ml + (self.cfg.costs.close_fee if cf is None else cf)

    def plan_kwargs(self) -> dict:
        """All levels-mode sizing inputs (every one a config key under [dca])."""
        d = self.cfg.dca
        lp = LadderParams(enabled=d.ladder_enabled, sizing=d.ladder_sizing, min_rungs=d.ladder_min_rungs,
                          max_rungs=d.ladder_max_rungs, weight_mult=d.ladder_weight_mult, deep=d.ladder_deep,
                          deep_anchor=d.ladder_deep_anchor, deep_min_atr=d.ladder_deep_min_atr, deep_max_atr=d.ladder_deep_max_atr, deep_atr=d.ladder_deep_atr,
                          fee_usd_per_add=d.ladder_fee_usd_per_add, weight_mode=d.ladder_weight_mode,
                          weight_mult_step=d.ladder_weight_mult_step, weight_mult_cap=d.ladder_weight_mult_cap,
                          min_usd=d.min_add_usd, fixed_usd=d.ladder_fixed_usd,
                          scaled=d.ladder_mode == "scaled", lev_start=d.lev_start)
        return dict(lev_step=d.lev_step, lev_cap=d.lev_cap, min_add_usd=d.min_add_usd, min_gap_atr=d.min_gap_atr,
                    max_gap_atr=d.max_gap_atr, target_avg_atr=d.target_avg_atr, fixed_dist=d.trigger_dist,
                    maint=self.liq_maint(), max_add_frac=d.max_add_frac,
                    min_target_gap_atr=d.min_target_gap_atr, min_avg_gap_atr=d.min_avg_gap_atr,
                    fallback_avg_frac=d.fallback_avg_frac, open_fee=self.cfg.costs.open_fee,
                    target_flipped_supports=d.target_flipped_supports, liq_buffer_atr=d.liq_buffer_atr, ladder=lp,
                    fixed_floor_atr=d.min_gap_atr if d.min_place_gap_atr > 0 else 0.0)   # 2026-10-27 uniform rungs

    def plan_next_add(self, pos: PositionView, available_usd: float, now: pd.Timestamp) -> AddPlan | None:
        self._levels = self.levels_provider(now) if self.levels_provider else None
        return plan_add(pos, self._levels, available_usd=available_usd, **self.plan_kwargs())

    def project_ladder(self, pos: PositionView, available_usd: float) -> list[AddPlan]:
        """Projected successive adds with the levels from the last plan_next_add call."""
        return project_ladder(pos, self._levels, available_usd=available_usd, **self.plan_kwargs())

    def _adds_left(self, pos: PositionView) -> bool:
        d = self.cfg.dca
        return pos.n_entries - 1 < d.max_adds and (pos.tp_hits == 0 or d.allow_after_partial)

    def _flat_too_long(self, bar: Bar) -> bool:
        limit = self.cfg.signal.max_flat_minutes
        return (limit is not None and self._flat_since is not None
                and bar.time + self._bar_duration - self._flat_since >= pd.Timedelta(minutes=limit))

    def _entry_side(self, trend_up: bool, oversold: bool, overbought: bool) -> int | None:
        if trend_up and oversold and self.cfg.allow_long:
            return LONG
        if not trend_up and overbought and self.cfg.allow_short:
            return SHORT
        return None

    def _add_due(self, pos: PositionView, price: float, oversold: bool, overbought: bool) -> bool:
        d = self.cfg.dca
        if pos.n_entries - 1 >= d.max_adds or (pos.tp_hits > 0 and not d.allow_after_partial):
            return False
        ref = pos.avg_entry if d.reference == "avg_entry" else pos.last_entry
        if d.trigger_mode == "price_abs":
            trigger = ref - pos.side * d.trigger_dist
        else:
            trigger = ref * (1 - pos.side * d.trigger_pct)
        far = price <= trigger if pos.side == LONG else price >= trigger
        signal = (oversold if pos.side == LONG else overbought) if d.require_signal else True
        if not (far and signal):
            return False
        if d.require_liq_beyond_next_level:
            liq, nxt = self.post_add_liq_and_next_trigger(pos, price)
            if pos.side * (liq - nxt) >= 0:   # liq at or inside the next DCA level
                self.skip_log.append(f"add skipped: post-add liq {liq:.2f} not beyond next DCA level {nxt:.2f}")
                return False
        return True

    def net_close_pnl(self, pos: PositionView, price: float) -> float:
        """Estimated pnl of closing now: after accrued borrow, close fee and slippage (open fees are sunk)."""
        c = self.cfg.costs
        fill = price * (1 - pos.side * c.slippage)
        return pos.side * (pos.qty * fill - pos.size) - pos.borrow - pos.size * c.close_fee

    def post_add_liq_and_next_trigger(self, pos: PositionView, price: float) -> tuple[float, float]:
        """Liquidation price after a hypothetical add at ``price`` and the next add trigger after it."""
        d, s = self.cfg.dca, pos.side
        margin_add = self.add_margin(pos)
        size_add = margin_add * self.add_leverage(pos)
        size, qty, margin = pos.size + size_add, pos.qty + size_add / price, pos.margin + margin_add
        ratio = self.cfg.risk.maintenance_ratio
        liq = (size + s * (pos.borrow + ratio * size - margin)) / qty
        avg = size / qty
        ref = avg if d.reference == "avg_entry" else price
        nxt = ref - s * d.trigger_dist if d.trigger_mode == "price_abs" else ref * (1 - s * d.trigger_pct)
        return liq, nxt

    def _daily_limit_hit(self, bar: Bar, account: AccountView) -> bool:
        limit = self.cfg.stops.daily_loss_pct
        if limit is not None and account.equity(bar.close) < self._period_start_equity * (1 - limit):
            self._blocked_period = self._period
        return self._blocked_period == self._period
