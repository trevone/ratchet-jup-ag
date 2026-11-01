"""Typed, immutable strategy/backtest configuration.

Every tunable lives here. Layering (lowest to highest precedence):
dataclass defaults < configs/default.toml < preset TOML < ``--set a.b=value`` overrides.
Unknown keys are rejected so typos never silently fall back to defaults.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args, get_origin, get_type_hints

import tomllib

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "configs" / "default.toml"


class Sides(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


@dataclass(frozen=True)
class MarketConfig:
    symbol: str = "SOLUSDT"
    base_tf: str = "1h"                  # interval of the raw kline files
    start: str | None = None             # inclusive; applied after indicator warm-up
    end: str | None = None               # exclusive


@dataclass(frozen=True)
class TrendConfig:
    tf: str = "4h"
    ma_type: Literal["ema", "sma"] = "ema"
    length: int = 200
    warmup_bars: int | None = None       # trend-TF bars masked as NaN; None -> length


@dataclass(frozen=True)
class SignalConfig:
    tf: str = "1h"
    rsi_method: Literal["wilder", "ema", "sma"] = "wilder"
    rsi_length: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    max_flat_minutes: float | None = None  # flat this long -> enter with trend when RSI is on the right side of 50
    flat_rsi_mid: float = 50.0
    # --- entry rule (Trevor 2026-10-29 20:52 MT): "price_ma" = open LONG only when price > 4h EMA200 (trend MA) AND
    # price > signal-TF SMA(entry_sma_length); SHORT only when price < both; disagree / equal = no entry. Price vs line,
    # never slope. RSI and the idle loosening are not used for entries. "rsi" = the old RSI rule (kept for backtests).
    entry_rule: Literal["price_ma", "rsi"] = "price_ma"
    entry_sma_length: int = 10
    # --- idle loosening (Trevor 2026-10-07 20:14 MT): flat with no entry >= idle_after_min -> step the entry RSI
    # thresholds one step every idle_step_min (step 1 at idle_after_min, step 2 at +idle_step_min, ...), capped.
    # Trend filter, PAUSE_UNTIL and KILL still apply. Resets on the next entry.
    idle_enabled: bool = False
    idle_after_min: float = 60.0
    idle_step_min: float = 30.0
    idle_long_rsi_steps: tuple[float, ...] = (48.0, 50.0)    # long threshold after step 1, 2, ... (base = oversold)
    idle_short_rsi_steps: tuple[float, ...] = (52.0, 50.0)   # short threshold after step 1, 2, ... (base = overbought)
    idle_max_long_rsi: float = 50.0      # long threshold never above this
    idle_min_short_rsi: float = 50.0     # short threshold never below this

    def __post_init__(self) -> None:
        if self.idle_after_min < 0 or self.idle_step_min <= 0:
            raise ValueError("signal.idle_after_min must be >= 0 and idle_step_min > 0")
        if self.idle_max_long_rsi > self.idle_min_short_rsi:
            raise ValueError("signal.idle_max_long_rsi must be <= idle_min_short_rsi (no overlapping long/short zones)")


@dataclass(frozen=True)
class SizingConfig:
    mode: Literal["equity_pct", "fixed_usd"] = "equity_pct"
    initial_margin_pct: float = 0.10     # equity_pct: fraction of free cash
    initial_margin_usd: float = 1_000.0  # fixed_usd


@dataclass(frozen=True)
class EntryConfig:
    """First-entry (fresh open) collateral. "fixed" = legacy [sizing] behaviour; "pct" = first_collateral_pct of the free
    collateral (native SOL - reserve, USD) at the moment of the open, floored at first_collateral_min_usd (Jupiter's $10 TP
    minimum), optionally capped at first_collateral_max_usd, and never above free collateral - fee_reserve_usd."""
    first_collateral_mode: Literal["fixed", "pct"] = "fixed"
    first_collateral_pct: float = 0.10
    first_collateral_min_usd: float = 10.50
    first_collateral_max_usd: float | None = None   # None = no cap
    fee_reserve_usd: float = 0.10        # kept free for the open's network/request fee
    # Trevor 2026-10-30: after any position close no new entry until this many seconds have passed AND the trade is booked
    # and the flat HWM raise has run from the chain balance (0 = no time wait; the booking / HWM wait always applies)
    post_close_cooldown_sec: float = 300.0

    def __post_init__(self) -> None:
        if not 0.0 < self.first_collateral_pct <= 1.0:
            raise ValueError("entry.first_collateral_pct must be in (0, 1]")
        if self.post_close_cooldown_sec < 0:
            raise ValueError("entry.post_close_cooldown_sec must be >= 0")
        if self.first_collateral_min_usd < 0 or self.fee_reserve_usd < 0:
            raise ValueError("entry.first_collateral_min_usd / fee_reserve_usd must be >= 0")
        if self.first_collateral_max_usd is not None and self.first_collateral_max_usd < self.first_collateral_min_usd:
            raise ValueError("entry.first_collateral_max_usd must be >= first_collateral_min_usd")


@dataclass(frozen=True)
class DcaConfig:
    max_adds: int = 4
    mode: Literal["fixed", "levels"] = "fixed"   # levels: adds at support/resistance (dca_levels.py)
    levels_path: str = "/workspace/market-analysis/latest.json"
    levels_max_age_min: float = 45.0
    min_gap_atr: float = 0.5
    max_gap_atr: float = 2.0
    target_avg_atr: float = 1.0          # size adds so avg lands this many ATR short of the next opposing level
    min_add_usd: float = 3.0
    add_frontrun_atr: float = 0.1        # Trevor 2026-10-15: resting add placed this many own-1h-ATR before the level (live bots only); 0 = at the level
    min_place_gap_atr: float = 0.0       # Trevor 2026-10-27: resting ladder add PLACED >= this many ladder ATR beyond the add ref (after the
                                         # front-run); > 0 also floors the fixed fallback at min_gap_atr x ladder ATR. 0 = off
    max_add_frac: float = 1.0           # levels mode: cap one add's collateral at this fraction of available
    # --- levels mode: target-resistance guard (root-cause fix 2026-10-07 16:30) ---
    min_target_gap_atr: float = 0.5      # opposing level must sit >= this many ATR beyond the add price (else ignored)
    liq_buffer_atr: float = 0.0          # post-add liq must sit >= this many ATR beyond the next rung
    target_flipped_supports: bool = False  # true: supports above the add price may serve as the sizing target
    min_avg_gap_atr: float = 0.25        # wanted avg must be >= this many ATR beyond the add price (caps the formula)
    fallback_avg_frac: float = 0.5       # target unreachable: want avg = avg - frac * (avg - add price)
    # --- levels mode: ladder budgeting of free collateral across planned rungs ---
    ladder_enabled: bool = True
    ladder_sizing: Literal["min", "budget"] = "budget"  # budget: the rung's ladder budget; min: min(formula, budget)
    ladder_min_rungs: int = 4            # rolling ladder (price past the anchored deep level): N rungs + deep
    ladder_max_rungs: int = 12           # cap on main rungs above the deep level
    ladder_weight_mult: float = 1.5      # rung k weight = mult**k (deeper rungs get more collateral; last = remainder)
    ladder_deep: bool = True             # final deep rung (largest weight, takes the remainder)
    ladder_deep_anchor: Literal["first_entry", "last_entry"] = "first_entry"  # deep level measured from here
    ladder_deep_min_atr: float = 6.0     # deep = deepest analyst support 6..12 ATR below the anchor
    ladder_deep_max_atr: float = 12.0
    ladder_deep_atr: float = 8.0         # ... else anchor - this many ATR
    ladder_weight_mode: Literal["geometric", "accel"] = "geometric"  # accel: ratio starts at weight_mult, +step/rung
    ladder_weight_mult_step: float = 0.10
    ladder_weight_mult_cap: float = 2.5
    ladder_fixed_usd: float = 0.0       # > 0: every rung's budget = this flat USD collateral (weights ignored); 0 = off
    # Trevor 2026-10-29 21:43 MT: "scaled" = infinite scaled ladder. B = first-entry collateral (set once at open, kept in
    # trade state). Rung n: L_n = min(lev_start + n*lev_step, lev_cap), N_n = (lev_start + n*lev_step) * B, C_n = N_n / L_n.
    # No rung count; a rung is skipped only when free collateral (+ fee reserve) can't cover it or the liq-buffer check
    # fails (never shrunk). "legacy" = the budget / fixed_usd ladder above (kept for backtests).
    ladder_mode: Literal["legacy", "scaled"] = "legacy"
    ladder_fee_usd_per_add: float = 0.10 # network/request fee reserve per planned rung, taken off free collateral
    trigger_mode: Literal["price_pct", "price_abs"] = "price_pct"
    trigger_pct: float = 0.04            # price_pct: adverse fraction from reference before an add
    trigger_dist: float = 2.0            # price_abs: adverse USD move from reference before an add
    reference: Literal["avg_entry", "last_entry"] = "avg_entry"
    require_signal: bool = True          # RSI must re-signal for an add
    allow_after_partial: bool = False    # allow adds after a partial take-profit filled
    require_liq_beyond_next_level: bool = True  # skip an add if post-add liq is not beyond the next add level
    margin_mult: float = 1.0             # add k margin = first * margin_mult * margin_growth**(k-1)
    margin_growth: float = 1.0
    add_margin_usd: float | None = None  # fixed USD collateral per add (overrides margin_mult/growth)
    lev_start: float = 2.0
    lev_step: float = 0.5
    lev_cap: float = 4.0


@dataclass(frozen=True)
class TpLevel:
    dist: float          # price_abs: USD; price_pct: fraction of anchor; margin_pct: (pnl-borrow)/margin
    close_pct: float     # CUMULATIVE fraction of the position closed once this level fills (last = 1.0)


@dataclass(frozen=True)
class TakeProfitConfig:
    mode: Literal["price_pct", "price_abs", "margin_pct"] = "price_pct"
    pct: float = 0.08                    # single 100 % level when ``levels`` is empty
    levels: tuple[TpLevel, ...] = ()     # partial-TP ladder, ascending dist
    anchor: Literal["avg_entry", "first_entry"] = "avg_entry"  # avg_entry re-anchors after each add; first_entry never moves
    move_stop_to_breakeven_after_partial: bool = False
    close_remainder_after_min: float | None = None  # market-close the rest this long after TP1 fills
    timer_require_profit: bool = True
    # --- smart TP sanity (Trevor 2026-10-07 20:14 MT); off by default so backtests are unchanged ---
    smart_tp2: bool = False
    tp_min_net_usd: float = 0.05         # each TP's closing chunk must net >= this after 6+6 bps open/close fees
    tp_min_gap_usd: float = 0.05         # TP1 sits at least this far inside TP2
    tp1_fee_mult: float = 3.0            # Trevor 2026-10-13: TP1 portion must net >= this x its share of the trade's total fees
    tp2_fee_mult: float = 4.5            # ... TP2 likewise (fees = all opening fees + est. close fee + accrued borrow)
    tp_min_move: float = 0.05            # periodic check re-places a TP only when it moves >= this (USD)
    tp_market_gap: float = 0.02          # a trigger must sit at least this far beyond market (Jupiter rejects otherwise)
    # --- explicit TP placement (Trevor 2026-10-15, Option A): TP1/TP2 = avg x (1 -/+ pct) (short below / long above avg).
    # When set they replace the levels' dist (the levels still give close_pct); fee floors / ordering / market clamp apply.
    tp1_pct: float | None = None
    tp2_pct: float | None = None
    # (timer_require_profit) timer only closes when net of est. close fee + slippage + borrow > 0

    def __post_init__(self) -> None:
        lv = self.levels
        if lv and (any(b.dist <= a.dist or b.close_pct <= a.close_pct for a, b in zip(lv, lv[1:]))
                   or lv[-1].close_pct != 1.0 or lv[0].close_pct <= 0):
            raise ValueError("tp.levels must ascend in dist and close_pct, ending at close_pct = 1.0")
        if self.mode == "price_abs" and not lv:
            raise ValueError("tp.mode = price_abs needs tp.levels")
        if self.tp_min_move < 0 or self.tp_market_gap < 0 or self.tp_min_gap_usd < 0:
            raise ValueError("tp.tp_min_move / tp_market_gap / tp_min_gap_usd must be >= 0")
        if (self.tp1_pct is None) != (self.tp2_pct is None):
            raise ValueError("tp.tp1_pct and tp.tp2_pct must be set together")
        if self.tp1_pct is not None:
            if not 0.0 < self.tp1_pct < self.tp2_pct:
                raise ValueError("tp.tp1_pct / tp2_pct must satisfy 0 < tp1_pct < tp2_pct")
            if len(self.ladder) != 2:
                raise ValueError("tp.tp1_pct / tp2_pct need a two-level tp.levels ladder")

    @property
    def ladder(self) -> tuple[TpLevel, ...]:
        return self.levels or (TpLevel(self.pct, 1.0),)


@dataclass(frozen=True)
class StopsConfig:
    margin_loss_pct: float | None = None  # exit when pnl - borrow <= -pct * committed margin
    price_stop_pct: float | None = None   # exit on this adverse price move from avg entry
    trend_flip_exit: bool = False
    daily_loss_pct: float | None = None   # block new entries/adds for rest of the period
    daily_period: str = "1D"
    max_hours_in_trade: float | None = None


@dataclass(frozen=True)
class CostsConfig:
    open_fee: float = 0.0006
    close_fee: float = 0.0006
    borrow_per_hour: float = 0.0001
    slippage: float = 0.0002


@dataclass(frozen=True)
class RiskConfig:
    maintenance_ratio: float = 0.01      # liquidated when collateral <= ratio * size (backtests; liq_model "legacy")
    # Trevor 2026-10-29 21:59 MT: "jupiter" = Jupiter Perps' own formula for the ladder liq-buffer check:
    # liq = avg -/+ (collateral - close_fee - borrow_fee - size / max_leverage) * avg / size. max_leverage (500) and the
    # close fee (decreasePositionBps) are refreshed from the on-chain custody each tick; these are the fallbacks.
    liq_model: Literal["legacy", "jupiter"] = "legacy"
    jup_max_leverage: float = 500.0


@dataclass(frozen=True)
class AccountConfig:
    start_equity: float = 10_000.0


@dataclass(frozen=True)
class LiveConfig:
    dry_run: bool = True                 # build + simulateTransaction only; nothing is sent
    max_collateral_usd: float = 100.0    # hard cap on total collateral across entries
    max_position_usd: float = 3000.0     # sanity ceiling on notional
    max_leverage: float = 4.0
    max_daily_loss_usd: float = 10.0     # no new orders after this realized+unrealized loss (UTC day)
    min_collateral_usd: float = 10.0     # Jupiter's documented minimum for opening a position
    kill_switch_path: str = "live/KILL"  # if this file exists: no new orders
    close_all_on_kill: bool = False
    price_slippage_pct: float = 0.01     # worst acceptable fill vs oracle for market requests
    max_oracle_age_sec: int = 60
    request_timeout_sec: int = 60        # keeper fulfillment wait before treating a request as failed
    poll_interval_sec: float = 30.0
    sol_fee_reserve: float = 0.02         # SOL never used as collateral (fees/rent)


@dataclass(frozen=True)
class BotConfig:
    """Multi-asset instance (2026-10-07 20:23 MT, Trevor): SOL / ETH / BTC bots trade from ONE wallet. Each instance has
    its own market, wallet share, state dir (state.json, trades.csv, ledger_*.json, KILL, PAUSE_UNTIL, logs) and process.
    Defaults = the original single SOL bot (asset SOL, share 1.0, state dir live/)."""
    asset: Literal["SOL", "ETH", "BTC"] = "SOL"   # Jupiter Perps market (custody); margin is always native SOL
    share: float = 1.0                   # fraction of the wallet budget (live/budget.py); shares across bots must sum <= 1
    state_dir: str = "live"              # relative to the repo root
    extra_pause_files: tuple[str, ...] = ()   # other PAUSE_UNTIL files this bot also obeys (e.g. the SOL operator's)
    global_kill_path: str = "live/KILL_ALL"   # present -> no new orders on ANY bot (closes still allowed)
    sweep_enabled: bool = True           # SOL in / SOL out: swap USDC / ETH / WBTC leftovers back to SOL (live/sweep.py)
    sweep_min_usd: float = 0.10          # smaller leftovers are dust (left alone)

    def __post_init__(self) -> None:
        if not 0.0 < self.share <= 1.0:
            raise ValueError("bot.share must be in (0, 1]")


@dataclass(frozen=True)
class ScaleConfig:
    """Per-asset price scaling (asset_scale.py). Off = the USD values in [dca]/[tp] are used as written (SOL)."""
    enabled: bool = False
    atr_tf: str = "1h"                   # own ATR: Wilder ATR(atr_len) on this TF from the bot's own klines
    atr_len: int = 14
    trigger_dist_atr: float | None = None    # dca.trigger_dist = this * own ATR (fixed spacing / levels fallback)
    trigger_dist_pct: float | None = None    # ... or this * price (used only if trigger_dist_atr is unset)
    tp_min_gap_pct: float | None = None      # tp.tp_min_gap_usd = this * price
    tp_min_move_pct: float | None = None     # tp.tp_min_move = this * price
    tp_market_gap_pct: float | None = None   # tp.tp_market_gap = this * price
    own_atr_fallback: bool = False       # analyst file missing/stale -> Levels(no S/R, ATR = own ATR) instead of None


WITHDRAW_ADDRESS = "59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp"  # the only allowed destination
JUPSOL_MINT = "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"           # the only allowed stake token
STAKE_HARD_MAX_SLIPPAGE_BPS = 300                                     # also enforced in live/src/stake.ts


@dataclass(frozen=True)
class WithdrawConfig:
    enabled: bool = False
    address: str = WITHDRAW_ADDRESS
    time_mt: str = "00:05"               # daily, America/Denver
    # what the daily run does with the realized profit: "send" = SOL to `address` (old behaviour; legacy value
    # "realized_profit_sol" means the same), "stake_jupsol" = swap SOL -> JupSOL kept in the bot wallet (stake bucket)
    mode: Literal["send", "stake_jupsol", "realized_profit_sol"] = "send"
    amount: Literal["realized_profit_sol"] = "realized_profit_sol"   # ledger realized net SOL since last run + carry
    min_send_sol: float = 0.01           # minimum for send AND stake; smaller (or negative) amounts carry forward
    # stake_jupsol mode only (send keeps min_send_sol): stake ALL realized profit when it is comfortably above the swap
    # cost: amount > min_stake_fee_mult * (stake_fee_est_sol + amount * stake_slippage_bps / 1e4), and >= min_stake_floor_sol
    min_stake_fee_mult: float = 3.0
    min_stake_floor_sol: float = 0.0005
    stake_fee_est_sol: float = 0.00001   # network + priority fee per swap tx (observed ~0.0000053-0.0000058 SOL)
    # stake_jupsol: false = stake whether or not a trade is open; only hard check = native SOL stays >= sol_fee_reserve +
    # stake_fee_buffer_sol (stake what fits, carry the rest). true = old rule (free SOL must also cover the next add, all-or-nothing).
    require_next_add_cover: bool = True
    stake_mint: str = JUPSOL_MINT
    stake_slippage_bps: int = 50         # SOL -> JupSOL
    unstake_slippage_bps: int = 50       # JupSOL -> SOL (live/stake.py unstake)
    stake_max_slippage_bps: int = 100    # cap for CLI --slippage-bps overrides
    stake_max_price_impact_pct: float = 0.5
    stake_only_direct_routes: bool = False
    stake_fee_buffer_sol: float = 0.003  # free SOL kept on top of sol_fee_reserve for the swap fee + one-time token-account rent
    stake_api_bases: tuple[str, ...] = ("https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1")  # tried in order
    # multi-asset: realized profit of every bot's trade file goes into ONE daily stake (run only by the bot with
    # withdraw.enabled = true, i.e. SOL). Per-file cutoffs, so no trade is counted twice or skipped.
    trade_files: tuple[str, ...] = ("live/trades.csv",)

    def __post_init__(self) -> None:
        if self.address != WITHDRAW_ADDRESS:
            raise ValueError(f"withdraw.address must be exactly {WITHDRAW_ADDRESS}")
        if self.stake_mint != JUPSOL_MINT:
            raise ValueError(f"withdraw.stake_mint must be {JUPSOL_MINT}")
        for k in ("stake_slippage_bps", "unstake_slippage_bps"):
            if not 1 <= getattr(self, k) <= self.stake_max_slippage_bps:
                raise ValueError(f"withdraw.{k} must be 1..stake_max_slippage_bps")
        if self.min_stake_fee_mult < 1.0 or self.min_stake_floor_sol < 0 or self.stake_fee_est_sol < 0:
            raise ValueError("withdraw.min_stake_fee_mult must be >= 1; min_stake_floor_sol / stake_fee_est_sol >= 0")
        if not 1 <= self.stake_max_slippage_bps <= STAKE_HARD_MAX_SLIPPAGE_BPS:
            raise ValueError(f"withdraw.stake_max_slippage_bps must be 1..{STAKE_HARD_MAX_SLIPPAGE_BPS}")

    def min_stake_sol(self) -> float:
        """Smallest amount staked (stake_jupsol): solves a > mult * (fee + a * slip) -> a > mult*fee / (1 - mult*slip),
        floored at min_stake_floor_sol. inf if the slippage alone eats it (mult * slip >= 1)."""
        k = self.min_stake_fee_mult * self.stake_slippage_bps / 1e4
        if k >= 1.0:
            return float("inf")
        return max(self.min_stake_floor_sol, self.min_stake_fee_mult * self.stake_fee_est_sol / (1.0 - k))

    def min_amount_sol(self) -> float:
        """Daily minimum for the configured action: stake -> min_stake_sol(); send -> min_send_sol (unchanged)."""
        return self.min_stake_sol() if self.action == "stake_jupsol" else self.min_send_sol

    @property
    def action(self) -> str:
        return "stake_jupsol" if self.mode == "stake_jupsol" else "send"


@dataclass(frozen=True)
class TrimConfig:
    """Time-based per-add trim (Trevor 2026-10-08 13:00 MT; live bot only, OFF by default so backtests / ETH / BTC are
    unchanged). Every entry / add is tracked as its own lot. Once the position has stayed under water for
    ``trim_after_hours`` since the clock reference, every add lot (never the first entry) whose OWN gain at the current
    price covers fees + trim_min_net_usd and is >= trim_min_gain_pct is market-closed for exactly its size; collateral
    stays in the position, its ladder rung re-arms and the TPs are re-placed for the smaller size."""
    enabled: bool = False
    trim_after_hours: float = 4.0
    trim_clock: Literal["last_add", "open"] = "last_add"   # clock reference: most recent add (or the open) | the open
    trim_min_net_usd: float = 0.25       # lot gain must cover 6+6 bps + est. network fee + this
    trim_min_gain_pct: float = 0.005     # ... and be >= this fraction of the lot price (0.005 = 0.5 %)
    trim_network_usd: float = 0.02       # est. network fees per trim (decrease + TP cancel / re-place txs)
    trim_check_min: float = 1.0          # check cadence (minutes; the loop ticks every 60 s)
    trim_max_per_day: int = 0            # max trim actions per UTC day (0 = no limit)
    # trigger (Trevor 2026-10-08 17:06 MT): "time" = the trim_after_hours gate above; "distance_level" = ARM once price is
    # > trim_arm_dist_pct against the avg (sticky until flat), then TRIGGER when price reaches an analyst resistance (long)
    # / support (short) within trim_level_tol_atr x 1h ATR. Stale / missing levels -> no trim.
    trim_trigger: Literal["time", "distance_level"] = "time"
    trim_arm_dist_pct: float = 0.03      # fraction of avg (0.03 = 3 %)
    trim_level_tol_atr: float = 0.1
    # front-run (Trevor 17:16 MT): fire BEFORE the level (long: resistance - offset, short: support + offset);
    # offset = trim_level_front_run_atr x 1h ATR + trim_level_front_run_pct x level
    trim_level_front_run_atr: float = 0.15
    trim_level_front_run_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.trim_clock not in ("last_add", "open"):
            raise ValueError("trim.trim_clock must be last_add | open")
        if self.trim_trigger not in ("time", "distance_level"):
            raise ValueError("trim.trim_trigger must be time | distance_level")
        if min(self.trim_arm_dist_pct, self.trim_level_tol_atr, self.trim_level_front_run_atr, self.trim_level_front_run_pct) < 0:
            raise ValueError("trim.trim_arm_dist_pct / trim_level_tol_atr must be >= 0")
        if self.trim_after_hours < 0 or self.trim_min_net_usd < 0 or self.trim_min_gain_pct < 0 or self.trim_max_per_day < 0:
            raise ValueError("trim.* thresholds must be >= 0")


@dataclass(frozen=True)
class ProfitGuardConfig:
    """Wallet-baseline profit guard (Trevor 2026-10-08 17:11 MT; live only, OFF by default). At each fresh open the
    bot's own SOL baseline (its free budget share, nothing open, in SOL) is stored; the trade must end >= baseline +
    max(min_trade_profit_sol, min_trade_profit_pct x baseline) in SOL, counting every deposit, trim / TP payout, fee and
    network cost. Owner spec 2026-10-08 18:02 MT: it NEVER moves TPs; its action is a MARKET CLOSE of the whole position
    as soon as closing now meets baseline + min (close_when_met), gated by close_requires ("adds_or_trims": only
    positions that had >= 1 add or trim, plain first-entry trades run to their TPs; "any": every position). The TP
    timer still only closes when the close now meets it."""
    enabled: bool = False
    min_trade_profit_sol: float = 0.001
    min_trade_profit_pct: float = 0.0     # fraction of the baseline (0.005 = 0.5 %); min = max(sol, pct x baseline)
    close_network_sol: float = 0.00002    # est. network fees still to pay (final close + TP cancels)
    close_when_met: bool = True           # market-close the whole position once the close now meets baseline + min
    close_requires: str = "adds_or_trims"  # "adds_or_trims" | "any"
    min_profit_pct_trade: float = 0.002   # live guard (Trevor 2026-10-14): close once equity >= trade_start_equity x (1 + this)
    tp_guard_gap_pct: float = 0.003   # Trevor 2026-10-15: after a TP3 fill TP2 sits AT the guard price, TP1 this fraction inside it

    def __post_init__(self) -> None:
        if self.min_trade_profit_sol < 0 or self.min_trade_profit_pct < 0 or self.close_network_sol < 0 or self.min_profit_pct_trade < 0:
            raise ValueError("profit_guard.* must be >= 0")
        if not 0 <= self.tp_guard_gap_pct < 0.05:
            raise ValueError("profit_guard.tp_guard_gap_pct must be in [0, 0.05)")
        if self.close_requires not in ("adds_or_trims", "any"):
            raise ValueError("profit_guard.close_requires must be 'adds_or_trims' or 'any'")


@dataclass(frozen=True)
class LedgerConfig:
    """Per-bot own-coin ledger + HWM (Trevor 2026-10-08 21:37 MT, live/bank.py). Each bot keeps its OWN balance in its OWN
    coin (SOL bot: SOL, ETH bot: Wormhole ETH 7vfCXT.., BTC bot: WBTC 3NZ9JM..) in state.json["bank"]; trades are funded
    from it and pay back into it; the profit guard targets hwm + min_profit_native; a daily job moves
    withdraw_fraction x (HWM gain since the last withdrawal) into JupSOL. OFF by default (backtests / old tests)."""
    enabled: bool = False
    guard_only_underwater: bool = False     # OBSOLETE (2026-10-14: per-trade guard ignores it; key kept so live tomls still load)
    min_profit_native: float = 0.001        # guard target = hwm + this (native units: SOL / ETH / WBTC)
    # Trevor 2026-10-12 00:08/00:13 MT: > 0 replaces min_profit_native: target = hwm x (min_profit_pct_hwm +
    # min_profit_pct_per_partial x partials taken this trade [trims + partial-TP fills])
    min_profit_pct_hwm: float = 0.0
    min_profit_pct_per_partial: float = 0.0
    swap_cost_pct: float = 0.002            # est. USDC -> native swap cost on short payouts (guard close-value estimate)
    close_network_native: float = 0.00002   # est. network cost still to pay for a close (SOL bot; others: in fee_sol)
    ext_min_usd: float = 1.5                # unexplained wallet change >= this (persisting 2 ticks) = external dep/wd
    ext_split: tuple[float, float, float] = (0.5, 0.25, 0.25)   # SOL / ETH / BTC share of an external SOL change
    # Trevor 2026-10-11 19:08 MT: "alert" = an unexplained wallet change only logs WARNING UNEXPLAINED WALLET CHANGE and
    # writes live/EXT_FLOW_ALERT; banks untouched, no split, no coin swaps. "split" = old rule (ext_split capital move).
    # Trevor 2026-10-30 07:15 MT: "chain" = a bot's balance IS its own token on chain (SOL bot: native SOL - sol_fee_reserve;
    # ETH bot: wallet ETH; BTC bot: wallet WBTC), re-read every tick with nothing in flight; no own-token deposit detection.
    # HWM is the only bookkeeping number: flat and balance (net of loans / fee debt) > HWM -> HWM = balance; never lowered
    # except by the daily withdrawal (capital out). USDC deposits (live/deposit.py) unchanged.
    ext_flow_mode: str = "alert"
    window_ticks: int = 3                   # ticks after a payout event during which own-token changes are trade flows
    # Trevor 2026-10-31 14:45 MT (SOL bot): the open-trade payout true-up (sol_pay_trueup) reads the position's txs
    # (position-flows). With the RPCs rate-limited it blocked ~240 s every tick (guard read 250-334 s stale, bars skipped).
    # Its read now times out after pay_trueup_timeout_sec and, after a failed / unavailable read, is retried at most every
    # pay_trueup_backoff_sec (the payout estimate stays booked meanwhile; the flat true-up still corrects it exactly).
    pay_trueup_timeout_sec: float = 30.0
    pay_trueup_backoff_sec: float = 900.0
    # Trevor 2026-10-31 20:48 MT: position-flows is incremental. Fetched tx rows are cached in live/position_flows_cache.json
    # (runtime, not committed; per position + open tx); new signatures are paged with until= the last listed one; uncached
    # txs are fetched flows_batch at a time, retried flows_retries times on 429 with flows_backoff_ms doubling; one call
    # stops after flows_budget_sec (progress saved) and is killed at budget + flows_timeout_margin_sec. A partial read
    # (budget hit, progress made) is retried after flows_partial_retry_sec instead of the full pay_trueup_backoff_sec.
    # The close-time true-up (book_flat) uses the same budget (was the 240 s CLI default).
    flows_cache: bool = True
    flows_budget_sec: float = 20.0
    flows_timeout_margin_sec: float = 10.0
    flows_batch: int = 4
    flows_retries: int = 3
    flows_backoff_ms: int = 1000
    flows_partial_retry_sec: float = 60.0
    # Trevor 2026-10-31: chain mode, ETH / BTC. An own-token change the chain sync sees while a payout is due (payout
    # window / pay_wait / booking pending) is the trade's payout: credited to trade_recv, so a cancelled add's refund
    # (booked against trade_dep) can no longer leave the row at "-deposited, received 0" (ETH 07:49 / BTC 08:45 MT 10-30).
    chain_payout_recv: bool = True
    # Trevor 2026-10-31: chain mode. A rise of the own token on chain while pay_wait is set (per-add partial / TP payout)
    # = payout seen: pay_wait cleared, so the guard-price TP re-place runs on the next tick instead of waiting
    # GUARD_DEFER_MAX ticks (~14 min, BTC 09:32-09:46 / 14:23-14:37 MT 10-30).
    chain_payout_clears_wait: bool = True
    withdraw_enabled: bool = True           # daily HWM-gain withdrawal to JupSOL (replaces the old SOL-bot nightly stake)
    withdraw_time_mt: str = "00:05"
    withdraw_fraction: float = 0.5
    # Trevor 2026-10-11 06:53 MT: no fixed $ minimum; withdraw when amount_usd > withdraw_fee_factor x est. cost
    # (est. cost = amount_usd x withdraw_cost_pct [route fee + impact] + withdraw_network_sol x SOL price), else carry.
    withdraw_cost_pct: float = 0.002
    withdraw_network_sol: float = 0.0001
    withdraw_fee_factor: float = 2.0
    fee_settle_min_sol: float = 0.005       # ETH/BTC: settle SOL network-fee debt (native -> SOL swap) above this
    # Trevor 2026-10-27: automatic JupSOL loans (live/jupsol_loans.py). "off" = no automatic JupSOL lending (manual
    # rescue.sh only); "observe" = log what would be lent, send nothing; "on" = rescue (< 3% from liq, collateral only, max
    # 1/3 of the JupSOL) and next-add loans (exact shortfall + $1, only when no peer bot can cover it). Repayment of an
    # outstanding JupSOL loan (exact JupSOL quantity, on flat) runs in every mode.
    jupsol_loans: str = "off"

    def __post_init__(self) -> None:
        if self.min_profit_native < 0 or not 0.0 <= self.withdraw_fraction <= 1.0 or self.ext_min_usd <= 0:
            raise ValueError("ledger: min_profit_native >= 0, 0 <= withdraw_fraction <= 1, ext_min_usd > 0")
        if self.pay_trueup_timeout_sec <= 0 or self.pay_trueup_backoff_sec < 0:
            raise ValueError("ledger: pay_trueup_timeout_sec > 0 and pay_trueup_backoff_sec >= 0")
        if (self.flows_budget_sec <= 0 or self.flows_timeout_margin_sec <= 0 or self.flows_batch < 1 or self.flows_retries < 0
                or self.flows_backoff_ms < 0 or self.flows_partial_retry_sec < 0):
            raise ValueError("ledger: flows_budget_sec/flows_timeout_margin_sec > 0, flows_batch >= 1, flows_retries/"
                             "flows_backoff_ms/flows_partial_retry_sec >= 0")
        if self.ext_flow_mode not in ("alert", "split", "chain"):
            raise ValueError("ledger.ext_flow_mode must be 'alert', 'split' or 'chain'")
        if abs(sum(self.ext_split) - 1.0) > 1e-9:
            raise ValueError("ledger.ext_split must sum to 1")
        if self.jupsol_loans not in ("off", "observe", "on"):
            raise ValueError("ledger.jupsol_loans must be 'off', 'observe' or 'on'")


@dataclass(frozen=True)
class RestingConfig:
    """Resting on-chain orders (Trevor 2026-10-11 spec, approved 23:00 MT). ALL OFF by default and in every live config:
    with every flag false the bot behaves exactly as before (market adds on the bar, no per-add partial orders).

    adds_enabled: the next ladder add rests as a Jupiter limit-open order (Perps API, live/src/limit.ts) at the rung price
      (same size / leverage as the ladder plans now; the add trigger has no front-run today, so neither does the order).
      Placed orders are never modified; after a fill the next missing rung is placed. Pending add orders are cancelled
      only when the trade closes or the side changes. A refused creation (e.g. utilisation > 80 %, $10 minimum) is logged
      and retried next tick. The market add on the bar is not used while this is on.
    partials_enabled: ONE partial TP (TP3, trigger decrease) per position (Trevor 2026-10-15): target = avg + f x (furthest
      open add - avg), f tapering partial_zone_f_first (rung 1) -> partial_zone_f_deep (rung >= ladder_max_rungs), rung = number
      of open adds; sized to the adds that net > 0 after fees (6+6 bps + trim_network_usd) at the target; none -> no TP3.
      Re-planned (cancel + place) whenever size / price change: new add, position shrank, adds closed by a TP3 fill.
    last_partial_rule: the price of the last partial is recorded; the next add order must lie beyond it in the adverse
      direction (the ladder is planned from the more adverse of the last fill and the last partial price)."""
    adds_enabled: bool = False
    partials_enabled: bool = False
    last_partial_rule: bool = False
    partial_levels_min: int = 1
    partial_levels_max: int = 4
    partial_zone_f_first: float = 0.50   # TP3 target = avg + f x (furthest add - avg); f at rung 1
    partial_zone_f_deep: float = 0.27    # ... tapering linearly to this at rung >= ladder_max_rungs
    # Trevor 2026-10-27: TP3 at least far enough that the furthest add lot nets this fraction of its own notional after
    # open + close fees and network (0.002 = $0.15 on a $75 add); 0 = off (f alone)
    partial_min_net_pct: float = 0.0
    # Trevor 2026-10-31: a resting TP3 is cancelled for "skipped" (no add beyond the reference avg / no lot nets) only after
    # the skip held this many consecutive ticks. The reference (true breakeven) reads the shared wallet's SOL; a peer bot's
    # request cancel / re-place can skew one read (07:38 MT: +0.0037 SOL rent -> breakeven 95.1492 vs 94.84-94.94) and
    # cancelled a valid TP3 that was re-placed at the same price 3 min later. 1 = old behaviour (cancel on the first skip).
    tp3_skip_confirm_ticks: int = 3
    # Trevor 2026-10-27: after a TP3 fill the resting add re-arms at the more adverse of the closed lot's entry and the last
    # partial price + this x ladder (analyst) ATR (short: above / long: below), placed there with no extra front-run;
    # 0 = off (old re-arm: closed lot's entry, then the add front-run)
    readd_gap_atr: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= self.partial_levels_min <= self.partial_levels_max:
            raise ValueError("resting: 1 <= partial_levels_min <= partial_levels_max")
        if self.tp3_skip_confirm_ticks < 1:
            raise ValueError("resting: tp3_skip_confirm_ticks must be >= 1")
        if self.partial_min_net_pct < 0 or self.readd_gap_atr < 0:
            raise ValueError("resting: partial_min_net_pct and readd_gap_atr must be >= 0")

    @property
    def any_on(self) -> bool:
        return self.adds_enabled or self.partials_enabled or self.last_partial_rule


@dataclass(frozen=True)
class Config:
    name: str = "default"
    sides: Sides = Sides.BOTH
    market: MarketConfig = field(default_factory=MarketConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    dca: DcaConfig = field(default_factory=DcaConfig)
    tp: TakeProfitConfig = field(default_factory=TakeProfitConfig)
    stops: StopsConfig = field(default_factory=StopsConfig)
    costs: CostsConfig = field(default_factory=CostsConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    withdraw: WithdrawConfig = field(default_factory=WithdrawConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    scale: ScaleConfig = field(default_factory=ScaleConfig)
    trim: TrimConfig = field(default_factory=TrimConfig)
    profit_guard: ProfitGuardConfig = field(default_factory=ProfitGuardConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    resting: RestingConfig = field(default_factory=RestingConfig)

    @property
    def allow_long(self) -> bool:
        return self.sides in (Sides.LONG, Sides.BOTH)

    @property
    def allow_short(self) -> bool:
        return self.sides in (Sides.SHORT, Sides.BOTH)

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)

    def flat(self) -> dict[str, Any]:
        return flatten(self.to_dict())

    def short_hash(self, n: int = 8) -> str:
        """Content hash of everything that affects results (``name`` excluded)."""
        d = self.to_dict()
        d.pop("name")
        blob = json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:n]


# ------------------------------------------------------------------ (de)serialisation
T = TypeVar("T")


def _to_plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, tuple):
        return [_to_plain(x) for x in obj]
    return obj


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _coerce(tp: Any, value: Any, path: str) -> Any:
    if isinstance(tp, type) and is_dataclass(tp):
        if not isinstance(value, dict):
            raise TypeError(f"{path}: expected a mapping, got {value!r}")
        return from_dict(tp, value, f"{path}.")
    if get_origin(tp) is tuple:
        (inner, _) = get_args(tp)
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"{path}: expected a list, got {value!r}")
        return tuple(_coerce(inner, v, f"{path}[{i}]") for i, v in enumerate(value))
    if get_origin(tp) is Literal:
        if value not in get_args(tp):
            raise ValueError(f"{path}: {value!r} not one of {get_args(tp)}")
        return value
    args = get_args(tp)
    if type(None) in args:  # X | None
        if value is None:
            return None
        (inner,) = (a for a in args if a is not type(None))
        return _coerce(inner, value, path)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if tp is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if type(value) is tp:
        return value
    raise TypeError(f"{path}: expected {getattr(tp, '__name__', tp)}, got {value!r}")


def from_dict(cls: type[T], data: dict[str, Any], path: str = "") -> T:
    """Build a (nested) frozen dataclass from a mapping; missing keys keep defaults."""
    hints = get_type_hints(cls)
    unknown = set(data) - {f.name for f in fields(cls)}  # type: ignore[arg-type]
    if unknown:
        raise KeyError(f"unknown config key(s) under '{path or '<root>'}': {sorted(unknown)}")
    return cls(**{k: _coerce(hints[k], v, f"{path}{k}") for k, v in data.items()})


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def set_path(d: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """Copy of ``d`` with ``a.b.c`` set to ``value``."""
    head, _, rest = dotted.partition(".")
    out = dict(d)
    out[head] = set_path(out.get(head) or {}, rest, value) if rest else value
    return out


def parse_override(expr: str) -> tuple[str, Any]:
    """``"dca.max_adds=3"`` -> ``("dca.max_adds", 3)``. The value is parsed as JSON (null/true/numbers), else kept as a string."""
    key, sep, raw = expr.partition("=")
    if not sep or not key.strip():
        raise ValueError(f"override must be key.path=value, got {expr!r}")
    raw = raw.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw  # bare strings, e.g. sides=long or trend.tf=1d
    return key.strip(), value


def read_toml(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(
    path: str | Path | None = None,
    overrides: list[str] | None = None,
    params: dict[str, Any] | None = None,
) -> Config:
    """Resolve a Config. ``params`` maps dotted keys to values (used by the sweep)."""
    data = read_toml(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else {}
    if path is not None and Path(path).resolve() != DEFAULT_CONFIG:
        data = deep_merge(data, read_toml(path))
    for key, value in (params or {}).items():
        data = set_path(data, key, value)
    for expr in overrides or []:
        data = set_path(data, *parse_override(expr))
    return from_dict(Config, data)


def dump_config(cfg: Config, path: str | Path) -> None:
    with open(path, "w") as f:
        json.dump(cfg.to_dict(), f, indent=2)
