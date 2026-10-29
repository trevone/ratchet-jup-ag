"""Live Jupiter Perps bot loop (SOL). Ticks every ``TICK_SEC``; strategy evaluated on each closed signal bar.

On-chain state is the source of truth: every tick re-reads the position + requests and reconciles the
local state (``live/state.json``). Failed actions are logged (ERROR) and reconciled next tick; the bot never writes the KILL file (manual KILL honoured)
(no new orders; closes still allowed) instead of retrying blindly.

    .venv/bin/python -m live.bot --config configs/live_100.toml      # SOL (state in live/)
    .venv/bin/python -m live.bot --config configs/live_eth.toml      # ETH (state in live/eth/)
    .venv/bin/python -m live.bot --config configs/live_btc.toml      # BTC (state in live/btc/)

Multi-asset (2026-10-07): one process per market, all on the same wallet. [bot] asset/share/state_dir pick the Jupiter
market, the wallet share (live/budget.py) and the state dir; SOL is the default (paths unchanged). Margin is always
native SOL: every USD <-> SOL conversion uses the SOL oracle, the market oracle only prices the market.
"""
from __future__ import annotations

import argparse
import json
import math
import logging
import os
import time
import types
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import pandas as pd

from config import ROOT, Config, load_config
from backtest import iter_bars
from indicators import compute_features
from live.live_broker import jup
from live import ledger
from live.run_dry import KLINES, HTTP_TIMEOUT
from strategy import LONG, SHORT, Action, DcaStrategy, Intent
from dca_levels import level_tf, load_analyst_levels
from asset_scale import atr_from_klines, levels_with_fallback, scale_config, scaled_summary
from live.budget import budget_from_state
from live.flows import flow_extras, position_net_sol
from live import sweep as sweepmod
from live import trim as trimmod
from live import bank as bankmod
from live import deposit as depositmod
from live import resting as restmod
from live import jupsol_loans as jlmod

import urllib.request

STATE = ROOT / "live" / "state.json"
TICK_SEC = 15                     # Trevor 2026-10-11 21:02 MT: tried 5s, hit RPC 429s, so 15s (was 60)
KLINE_PAGES = 5                   # 5000 x 15m bars > 200 x 4h EMA warm-up
LAMPORTS = 1_000_000_000
USDC_ATOMS = 1_000_000
COLLATERAL_DECIMALS = {"ETH": 8, "BTC": 8}   # long collateral token of the ETH / WBTC custodies (verified on-chain)
log = logging.getLogger("bot")


@dataclass
class Local:
    """What the chain can't tell us about the position."""
    side: int = 0
    opened: str = ""
    n_entries: int = 0
    first_margin: float = 0.0
    last_leverage: float = 0.0
    first_entry: float = 0.0
    last_entry: float = 0.0
    last_size: float = 0.0
    tp_hits: int = 0
    first_tp_time: str | None = None
    last_bar: str | None = None
    day: str = ""
    day_start_equity: float = 0.0
    # idle loosening: when the bot went flat (UTC ISO); None while a position is open
    flat_since: str | None = None
    # smart TPs: trigger prices this bot placed per ladder level (0 = none)
    tp_px: list[float] = field(default_factory=list)
    # per-add trim (2026-10-08, [trim]): every entry / add as its own lot (live/trim.py), the trims of this trade,
    # the total size ever opened in it, the last time it was seen net positive and the per-day trim counter
    lots: list[dict] = field(default_factory=list)
    trims: list[dict] = field(default_factory=list)
    trade_opened_usd: float = 0.0
    trim_np_last: str | None = None
    trim_day: str = ""
    trim_day_n: int = 0
    # distance+level trim trigger (2026-10-08 17:06 MT): armed once price was > trim_arm_dist_pct against the avg;
    # sticky until flat (a new Local on flat / open resets it)
    trim_armed: bool = False
    trim_arm_px: float | None = None
    trim_arm_time: str | None = None
    # wallet-baseline profit guard (2026-10-08 17:11 MT, [profit_guard]): this bot's SOL baseline just before the open,
    # SOL deposited as collateral and SOL paid out by trims / TP1 so far (network fees: ledger_open.json); reset on flat
    pg_base_sol: float | None = None
    pg_dep_sol: float = 0.0
    pg_recv_sol: float = 0.0
    # (fix 2026-10-08 18:03 MT) last seen on-chain size / collateral / realised PnL of the open position, so a TP fill can
    # count the collateral Jupiter returns with it (partial trigger decreases return collateral pro rata) + its real PnL;
    # pg_tp_usd = realised net USD of TP fills (simple USD cross-check of the SOL projection)
    pg_m_size: float = 0.0
    pg_margin: float = 0.0
    pg_rpnl: float | None = None
    pg_tp_usd: float = 0.0
    # per-bot own-coin ledger + HWM (2026-10-08 21:37 MT, live/bank.py, [ledger]): persists across flat resets / restarts
    bank: dict | None = None
    # resting orders ([resting], 2026-10-11, default OFF): the resting limit add of this trade (one at a time), the per-add
    # partial TP triggers placed (one per filled add, never modified), the price of the last partial, the last seen avg
    rest_add: dict | None = None
    partials: list[dict] = field(default_factory=list)
    partials_done: list[dict] = field(default_factory=list)
    # Trevor 2026-10-12 08:28 MT: cancels not confirmed -> retried each tick (no KILL); [{pubkey, change, ra, then}]
    pending_cancels: list[dict] = field(default_factory=list)
    tp_retry_n: int = 0   # consecutive failed tp_retry attempts; 3 -> back off (tp_retry_until), the tick carries on
    tp_retry_until: str = ""   # ISO UTC: no tp_retry attempt before this time
    tp_retry: bool = False   # Trevor 2026-10-12 08:46 MT: TP1/TP2 re-placed next tick (placement failed / TPs cancelled)
    last_partial_px: float | None = None
    rest_last_avg: float = 0.0
    add_pending: dict | None = None   # market add sent, fill not saved yet (cleared once its lot is saved)
    rearm: dict | None = None   # 2026-10-15: entry / margin / lev of the add lot a TP3 partial closed; the resting add returns to it
    tp2_override: float | None = None   # TP2_SET (Trevor 2026-10-13 20:57 MT): manual TP2 price, kept until the next add
    # Trevor 2026-10-27: per-trade guard arm latch. Set by the first add fill / partial (TP3) / trim of this trade and never
    # cleared until flat (a new Local on flat / open resets it), so a partial that leaves lots=1, n_entries=1 no longer
    # disarms the guard. Persisted in state.json; a state without the key derives it (trade_guard_armed).
    guard_armed: bool = False
    # Trevor 2026-10-27 (SOL bot): a payout this trade was booked as an estimate -> re-measure the trade's real on-chain
    # flows (position txs, as the flat true-up) and true up bal mid-trade (sol_pay_trueup); fee_mark_sol = this trade's
    # network fees (ledger_open.json) already inside bal after that true-up (hwm_equity deducts only the fees since)
    pay_check: bool = False
    fee_mark_sol: float = 0.0
    pg_avg: float = 0.0   # 2026-10-27: last seen on-chain avg entry of the whole position (the 'position gone' estimate)


def load_local(path: Path) -> Local:
    """state.json -> Local; unknown keys are ignored (forward/backward compatible)."""
    if not path.exists():
        return Local()
    d = json.loads(path.read_text())
    names = {f.name for f in fields(Local)}
    return Local(**{k: v for k, v in d.items() if k in names})


def last_close_time() -> str | None:
    """close_time of the last booked trade in live/trades.csv (seeds the idle clock after a restart)."""
    try:
        import csv
        with open(ledger.TRADES, newline="") as f:
            rows = list(csv.DictReader(f))
        return str(pd.Timestamp(rows[-1]["close_time"])) if rows else None
    except (OSError, KeyError, ValueError):
        return None


@dataclass
class ChainPosition:  # satisfies strategy.PositionView
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
    tp_hits: int
    first_tp_time: pd.Timestamp | None

    @property
    def avg_entry(self) -> float:
        return self.size / self.qty

    def price_for_net_pnl(self, net_pnl: float) -> float:
        return (self.size + self.side * (self.borrow + net_pnl)) / self.qty

    def pnl(self, price: float) -> float:
        return self.side * (self.qty * price - self.size)


def tp_fee_total(size: float, opened_usd: float, open_fee: float, close_fee: float, borrow_usd: float) -> float:
    """Trade's total fees (USD): opening fees of every open/add + est. close fee on the current size + accrued borrow."""
    return open_fee * opened_usd + close_fee * size + max(borrow_usd or 0.0, 0.0)


def tp_fee_min_price(side: int, avg: float, size: float, total_fees: float, mult: float) -> float:
    """Minimum TP price so the TP's portion nets >= mult x its fee share. A portion of c USD carries fees
    F * c / size; it nets c * side * (P - avg) / avg - F * c / size >= mult * F * c / size
    <=> P = avg * (1 + side * (1 + mult) * F / size) (independent of the portion's size). Rounded outward to cents."""
    raw = avg * (1.0 + side * (1.0 + mult) * total_fees / size)
    return (math.ceil(raw * 100 - 1e-6) if side > 0 else math.floor(raw * 100 + 1e-6)) / 100.0


def apply_tp_fee_min(side: int, prices: list[float], pending: list[int], mins: list[float],
                     gap: float) -> tuple[list[float], list[str]]:
    """TP1 (index 0) / TP2 (index 1): keep the analyst/strategy price when it is at or beyond its fee minimum, else
    place it exactly at the minimum; then keep TP2 beyond TP1 by >= gap. Other indices untouched."""
    out, why = list(prices), []
    for i in pending:
        if i > 1 or i >= len(mins):
            continue
        if side * (out[i] - mins[i]) < -1e-9:
            why.append(f"TP{i + 1} {out[i]:.2f} inside fee minimum -> {mins[i]:.2f}")
            out[i] = mins[i]
    if 0 in pending and 1 in pending and len(out) > 1 and side * (out[1] - out[0]) < gap - 1e-9:
        nv = round(out[0] + side * gap, 2)
        why.append(f"TP2 {out[1]:.2f} not beyond TP1 {out[0]:.2f} + {gap:.2f} -> {nv:.2f}")
        out[1] = nv
    return out, why


def margin_sol(st: dict[str, Any]) -> float:
    """Bot margin / ladder budget source: NATIVE SOL lamports of the wallet only (getBalance). The JupSOL stake bucket
    (st["stake"]) is a separate token account and is never margin."""
    return float(st["wallet"]["sol"])


def stake_line(st: dict[str, Any]) -> str:
    try:
        return _stake_line(st)
    except Exception as e:  # informational only
        return f"stake n/a ({e})"


def _stake_line(st: dict[str, Any]) -> str:
    sk = st.get("stake") or {}
    tot = ledger.stake_totals()
    val = sk.get("solValue")
    return (f"stake JupSOL {float(sk.get('jupsol') or 0):.6f} (~{val:.6f} SOL)" if val is not None else
            f"stake JupSOL {float(sk.get('jupsol') or 0):.6f}") + \
        f" | bucket: staked_total {tot['staked_sol_total']:.6f} SOL, unstaked {tot['unstaked_sol_total']:.6f} SOL," \
        f" net {tot['net_jupsol']:.6f} JupSOL (ops {tot['ops']})"


class ChainAccount:  # satisfies strategy.AccountView
    def __init__(self, sol: float, price: float, pos: ChainPosition | None, reserve: float, cash: float | None = None) -> None:
        # cash = this bot's free collateral (live/budget.py: share of the shared wallet minus own open collateral);
        # None -> old single-bot rule (native SOL - reserve) * price
        self._cash = max(0.0, (sol - reserve) * price) if cash is None else max(0.0, cash)
        self._pos, self._price = pos, price

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def position(self) -> ChainPosition | None:
        return self._pos

    def equity(self, price: float) -> float:
        p = self._pos
        return self._cash + (p.margin + p.pnl(price) if p else 0.0)


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.live = cfg.live
        self.base_cfg = cfg                       # [scale] re-derives price distances from this every tick
        self.asset = cfg.bot.asset
        self.dir = ROOT / cfg.bot.state_dir       # live/ (SOL), live/eth/, live/btc/
        self.state_path = self.dir / "state.json" if cfg.bot.state_dir != "live" else STATE
        self.strategy = DcaStrategy(cfg)
        self._own_atr: float | None = None        # own 1h ATR from this bot's klines (scale / analyst fallback)
        if cfg.dca.mode == "levels":   # analyst S/R + 1h ATR, re-read every call; stale -> own-ATR fallback or None
            self.strategy.levels_provider = lambda now: levels_with_fallback(
                load_analyst_levels(cfg.dca.levels_path, cfg.dca.levels_max_age_min), self._own_atr, cfg.scale.own_atr_fallback)
        self.local = load_local(self.state_path)
        self._fee_rechecked = False
        self._guard_drift_until = 0.0   # guard_drift_check cooldown (epoch s; per process)
        self.kill = ROOT / self.live.kill_switch_path
        self.kill_all = ROOT / cfg.bot.global_kill_path
        self._first_entry_logged = False
        self.budget = None
        self._trim_checked: pd.Timestamp | None = None
        self._trim_fails = 0
        self._trim_sent = False
        self._trim_note = ""
        self._trim_lv_note = ""
        self._sol_px = 0.0
        self._mkt_px = 0.0
        self.confirm_tries, self.confirm_wait = 5, 3.0   # increase / trim confirmation: re-read the chain up to 5x 3 s
        self._jl_nopeer = 0      # JupSOL next-add loans: consecutive ticks no peer could cover the shortfall
        self._jl_want: dict | None = None   # JupSOL next-add loan decided by rest_add_plan, sent by the next bank tick

    def killed(self) -> bool:
        """This bot's KILL file or the global KILL_ALL."""
        return self.kill.exists() or self.kill_all.exists()

    def rescale(self, price: float) -> None:
        """Apply [scale] (per-asset price distances) for the current price / own ATR; no-op for SOL."""
        if self.base_cfg.scale.enabled:
            self.cfg = scale_config(self.base_cfg, price, self._own_atr)
            self.strategy.cfg = self.cfg

    def update_atr(self, klines: pd.DataFrame) -> None:
        sc = self.base_cfg.scale
        a = atr_from_klines(klines, sc.atr_tf, sc.atr_len)
        if a:
            self._own_atr = a

    # ------------------------------------------------------------- helpers
    def first_entry_line(self, cash: float) -> str:
        """Status line: planned collateral of the next fresh open (new trades only; an open position is untouched)."""
        e = self.cfg.entry
        m = self.strategy.initial_margin(cash)
        how = (f"pct {e.first_collateral_pct:.1%} of free ${cash:.2f} = ${e.first_collateral_pct * cash:.2f}, "
               f"floor ${e.first_collateral_min_usd:.2f}, cap " +
               (f"${e.first_collateral_max_usd:.2f}" if e.first_collateral_max_usd is not None else "off")) \
            if e.first_collateral_mode == "pct" else f"fixed ({self.cfg.sizing.mode})"
        return (f"next first-entry plan: collateral ${m:.2f} x{self.cfg.dca.lev_start:g} notional "
                f"${m * self.cfg.dca.lev_start:.2f} [{how}]; ladder budget after open ~${max(0.0, cash - m):.2f}")

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")   # atomic: other bots read the bank of this file
        tmp.write_text(json.dumps(asdict(self.local), indent=1))
        tmp.replace(self.state_path)

    def halt(self, why: str) -> None:
        """Trevor 2026-10-12 08:46 MT: the bot never writes KILL itself (a manual KILL file is still honoured). Logs loudly;
        the next tick re-reads the chain and reconciles; TP1/TP2 are re-placed next tick if the position is open."""
        log.error("RECONCILE NEXT TICK (no KILL): %s", why)
        self.local.tp_retry = True

    def cmd(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Send when live, simulate when dry_run (same builders)."""
        live = not self.live.dry_run
        if name == "decrease" and args.get("side") == "short" and self.asset in ("ETH", "BTC"):
            args = {**args, "desiredMint": bankmod.MINT["ETH" if self.asset == "ETH" else "WBTC"]}   # paid in own coin
        res = jup(name if live else f"simulate-{name}", {**args, "market": self.asset, "send": live})
        log.info("%s %s -> %s", name, json.dumps(args), json.dumps({k: res.get(k) for k in
                 ("ok", "sig", "err", "error", "fulfillSec", "unitsConsumed")}))
        if live and res.get("sig"):
            log.info("tx https://solscan.io/tx/%s", res["sig"])
            if name != "increase":   # increase fees are booked by open_or_add (the open creates the record)
                ledger.update(fee_lamports=res.get("feeLamports"))
            self.bank_fee(res.get("feeLamports"))
        return res

    def chain(self) -> dict[str, Any]:
        t0 = time.time()
        st = jup("state", {"market": self.asset})
        if isinstance(st, dict):
            st["_read_at"] = t0   # balance-view time: the sweep skips tokens another bot swept after it (live/sweep.py)
        if "wallet" not in st:
            raise RuntimeError(f"state read failed: {st}")
        if st.get("market", "SOL") != self.asset:
            raise RuntimeError(f"state is for market {st.get('market')} not {self.asset}")
        return st

    @staticmethod
    def sol_price(st: dict[str, Any]) -> float:
        """SOL/USD (margin is native SOL for every market)."""
        return float((st.get("solOracle") or {}).get("price") or st["oracle"]["price"])

    def chain_position(self, st: dict[str, Any]) -> ChainPosition | None:
        live = [(LONG, st["positions"]["long"]), (SHORT, st["positions"]["short"])]
        open_ = [(s, p) for s, p in live if p.get("sizeUsd")]
        if len(open_) > 1:
            raise RuntimeError("both long and short open")
        if not open_:
            return None
        side, p = open_[0]
        L = self.local
        return ChainPosition(side=side, opened=pd.Timestamp(L.opened or pd.Timestamp.now(tz="UTC")),
                             margin=p["collateralUsd"], size=p["sizeUsd"], qty=p["sizeUsd"] / p["price"], borrow=0.0,
                             n_entries=max(L.n_entries, 1), last_leverage=L.last_leverage or self.cfg.dca.lev_start,
                             first_margin=L.first_margin or p["collateralUsd"], first_entry=L.first_entry or p["price"],
                             last_entry=L.last_entry or p["price"], tp_hits=L.tp_hits,
                             first_tp_time=pd.Timestamp(L.first_tp_time) if L.first_tp_time else None)

    def _position_flows(self, rec: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
        """(net SOL, rent, event gross) of this position's own txs since its open tx (shared wallet: other bots / deposits /
        stakes excluded); Nones when unavailable (wallet-delta fallback)."""
        flows, rent, gross = None, None, None
        try:   # this position's own txs since its open tx (shared wallet: other bots / deposits / stakes excluded)
            o = json.loads(ledger.OPEN.read_text()) if ledger.OPEN.exists() else {}
            flows, fres = position_net_sol(o.get("position") or rec.get("pubkey"), o.get("open_sig"))
            if flows is not None:
                rent, gross = flow_extras(fres)   # token-account rent (capital, excluded) + event gross P&L
                log.info("LEDGER position flows: %d txs, net %+.9f SOL (token-account rent %s SOL excluded; legacy %s), "
                         "event gross %s USD", fres.get("nTx", 0), flows, rent, fres.get("legacyNetLamports"), gross)
        except Exception as e:  # noqa: BLE001 - fall back to the wallet delta
            log.warning("position flows failed: %s", e)
        return flows, rent, gross

    def book_flat(self, st: dict[str, Any], side: int) -> float | None:
        """Write the finished trade to live/trades.csv (net SOL = wallet after flat - before open)."""
        rec = st["positions"]["long" if side == LONG else "short"]
        c = st.get("custody", {})
        jr = c.get("jumpRate") or {}
        try:  # jump-rate borrow model below target utilisation (annual bps -> hourly fraction)
            u, tu = c["utilization"], int(jr["targetUtilizationRate"]) / 1e9
            mn, tg = int(jr["minRateBps"]) / 1e4, int(jr["targetRateBps"]) / 1e4
            hourly = (mn + (tg - mn) * min(u / tu, 1.0)) / 8760
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            hourly = self.cfg.costs.borrow_per_hour
        flows, rent, gross = self._position_flows(rec)
        args = {"time": str(pd.Timestamp.now(tz="UTC")), "wallet_sol": st["wallet"]["sol"], "sol_px": self.sol_price(st),
                "realised": rec.get("realisedPnlUsd"), "hourly": hourly, "flows": flows, "rent": rent, "gross": gross}
        if self.bank_on and not self.live.dry_run and ledger.OPEN.exists():
            # the last payout (ETH / BTC own-coin result, 2026-10-09) and the cancelled resting add's collateral refund
            # (all bots, 2026-10-14) land a tick or more after flat -> book once the payout window is over and this
            # market has nothing in flight (bank_book_pending; SOL re-reads its position flows then)
            self.local.bank["book_pend"] = args
            log.info("LEDGER %s trade flat: booking once the payout / refund settles (window %d ticks); position SOL flows "
                     "now %s", self.asset, self.cfg.ledger.window_ticks, flows)
            self.save()
            return flows
        self._book_row(args)
        return flows

    def _book_row(self, a: dict[str, Any], native: dict[str, Any] | None = None) -> dict[str, Any] | None:
        row = ledger.on_flat(a["time"], a["wallet_sol"], a["sol_px"], a["realised"], a["hourly"], flows_net_sol=a["flows"],
                             flows_rent_sol=a["rent"], events_gross_usd=a["gross"], native=native)
        if row and native is not None:
            log.info("LEDGER trade booked: net %+.9f %s (dep %.9f, recv %.9f) = %+.9f SOL (~$%+.4f) [%s], gross $%s [%s], "
                     "SOL network fees %.9f (fee debt), exits %s", row["net_native"], row["native_coin"],
                     row["native_deposited"], row["native_received"], row["net_sol"], row["net_usd_at_close"],
                     row.get("net_sol_method"), row.get("gross_pnl_usd"), row.get("gross_pnl_source"),
                     row.get("network_fee_sol") or 0.0, row["exits"])
        elif row:
            log.info("LEDGER trade booked: net %+.9f SOL (~$%+.4f) [%s], gross $%s [%s], rent parked %+.9f SOL (capital), exits %s",
                     row["net_sol"], row["net_usd_at_close"], row.get("net_sol_method"), row.get("gross_pnl_usd"),
                     row.get("gross_pnl_source"), row.get("rent_sol") or 0.0, row["exits"])
        return row

    def bank_book_pending(self, sol_px: float, cpx: float, why: str = "payout settled") -> None:
        """ETH / BTC: book the deferred trade row with the own-coin result (received - deposited) and reset the
        trade counters (2026-10-09)."""
        b = self.local.bank
        a = b.pop("book_pend", None)
        if not a:
            return
        if why != "payout settled":
            log.warning("LEDGER booking %s trade before its payout window ended (%s): own-coin result may be partial",
                        self.asset, why)
        if not ledger.OPEN.exists():
            log.warning("LEDGER deferred %s booking: no open record left; nothing booked", self.asset)
        else:
            a = {**a, "sol_px": sol_px or a["sol_px"]}
            if self.asset == "SOL":   # re-read: a late resting-add refund is one of the position's txs
                f = self._position_flows({})
                blk = b.get("trueup_block")
                if f[0] is None and why == "payout settled" and blk and blk.get("trade") == self._open_sig():
                    b["book_pend"] = a   # this trade's true-up is still blocked: keep the row, re-read next tick
                    log.warning("LEDGER SOL booking held: position flows still unavailable (true-up blocked)")
                    return
                if f[0] is not None:
                    a.update(flows=f[0], rent=f[1], gross=f[2])
                if f[0] is not None and why == "payout settled":
                    # 2026-10-27: the settled flows (late refunds included) true the bank up once more; this also
                    # applies a flat true-up that could not be applied at flat (block of THIS trade cleared). Not on a
                    # forced booking at a new open: the shared position's flows may already hold the new deposit.
                    self._sol_trueup(f[0], "at booking")
                    blk = b.get("trueup_block")
                    if blk and blk.get("trade") and blk.get("trade") == self._open_sig():
                        b.pop("trueup_block", None)
                        log.warning("BANK true-up applied at booking: SOL withdrawal / HWM block cleared")
                self._book_row(a)
            else:
                self._book_row(a, native={"coin": b["coin"], "deposited": b.get("trade_dep", 0.0),
                                          "received": b.get("trade_recv", 0.0), "coin_px": cpx})
        log.info("BANK trade done: deposited %.9f, received %.9f %s (net %+.9f)", b.get("trade_dep", 0.0),
                 b.get("trade_recv", 0.0), b["coin"], b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0))
        b["trade_dep"], b["trade_recv"] = 0.0, 0.0
        self.save()

    def sweep(self, st: dict[str, Any], side: int | None) -> None:
        """Every short pays out in its own coin (2026-10-14): no proceeds are swapped. USDC >= $2 in the wallet is a deposit
        from Trevor (2026-10-26, live/deposit.py): the SOL bot alone swaps it into SOL / ETH / WBTC thirds here, BEFORE
        its ``sweep:`` log line; each bot credits its own share in bank_tick (deposit_tick). Less than $2 = dust, logged."""
        if not self.cfg.bot.sweep_enabled:
            return
        note = ""
        if self.asset == "SOL" and self.bank_on and not self.live.dry_run:
            try:
                note = depositmod.run_swapper(st, self.cfg, jup, killed=self.killed())
            except Exception as e:  # noqa: BLE001 - journal / CLI trouble: nothing assumed, retried next tick
                log.error("DEPOSIT swapper step failed (retried next tick): %s", e)
                note = "deposit swapper step failed (see ERROR above)"
        usdc = (depositmod.usdc_atoms(st) or 0) / depositmod.USDC_ATOMS
        if note:
            log.info("sweep: %s", note)
        elif usdc >= depositmod.MIN_USD:
            log.info("sweep: USDC $%.2f in the wallet: deposit (the SOL bot swaps it into SOL / ETH / WBTC thirds)", usdc)
        else:
            sweepmod.stray_usdc(st, self.cfg.bot.sweep_min_usd)

    def cancel_all(self, st: dict[str, Any]) -> bool:
        """Cancel this market's requests. A request that is already gone on-chain (cancel failed with 3012
        AccountNotInitialized, e.g. the escalated re-sign of the same cancel landed first) counts as cancelled while the
        position size is unchanged; a size change (a trigger filled meanwhile) or a request still on-chain is a failure."""
        return self.cancel_reqs(st, st["openRequests"])

    def cancel_reqs(self, st: dict[str, Any], reqs: list[dict[str, Any]]) -> bool:
        """Cancel ``reqs``. [resting] on: a resting limit add (increase trigger) is cancelled through the Jupiter Perps API
        (cancel_limit); everything else as before (closePositionRequest2)."""
        ok = True
        size = self._chain_size(st) if reqs else 0.0
        for r in reqs:
            if self.cfg.resting.any_on and r.get("requestChange") == "increase" and r.get("requestType") == "trigger":
                ok &= self.cancel_limit(r["pubkey"])
                continue
            ok &= self.cancel_request(r["pubkey"], size) == "ok"
        return ok

    def ladder_requests(self, st: dict[str, Any]) -> list[dict[str, Any]]:
        """The TP1/TP2 ladder's requests. [resting] on: resting limit adds and this bot's per-add partial triggers are
        excluded (they are never modified by a TP re-place). Off: every request of this market (unchanged)."""
        reqs = st["openRequests"]
        if not self.cfg.resting.any_on:
            return reqs
        mine = {p.get("pubkey") for p in self.local.partials}
        return [r for r in reqs if r.get("requestChange") != "increase" and r.get("pubkey") not in mine]

    def _chain_size(self, st: dict[str, Any]) -> float:
        p = self.chain_position(st)
        return p.size if p is not None else 0.0

    def add_pending_wait(self, st: dict[str, Any], pos: ChainPosition | None) -> bool:
        """Trevor 2026-10-12 08:52 MT: while an add/open-pending marker exists, no new add/open is sent until the chain
        shows the prior one resolved. Pending increase request on-chain -> wait; landed (size above the lots) -> lot
        recovery books it (or the open is adopted) -> wait; absent and no request -> marker cleared, send. True = wait."""
        L = self.local
        ap = L.add_pending
        if not ap:
            return False
        ra = (L.rest_add or {}).get("pubkey")
        reqs = [r.get("pubkey") for r in st.get("openRequests", []) if r.get("requestChange") == "increase"
                and r.get("pubkey") != ra and r.get("requestType") != "trigger"]
        if reqs:
            log.error("ADD PENDING: increase request still on-chain %s; no new add until it resolves", reqs)
            return True
        lots = sum(float(l["size"]) for l in L.lots)
        if pos is not None and (ap.get("open") or pos.size - lots > 0.01):
            log.error("ADD PENDING: prior add/open landed late (chain %.2f vs lots %.2f); booking it first", pos.size, lots)
            if not ap.get("open"):
                self.recover_missing_add(st, pos, st["oracle"]["price"])
            return True
        log.error("ADD PENDING: prior add/open absent on-chain and no request pending; marker cleared")
        L.add_pending = None
        self.save()
        return False

    def adopt_position(self, st: dict[str, Any], pos: ChainPosition) -> None:
        """Trevor 2026-10-12 08:46 MT: a position on-chain the bot did not open (manual / late-landed open) is adopted as a
        single lot (side, size, avg entry, collateral from the chain), TPs placed, then managed normally. No KILL."""
        L = self.local
        own_open = bool(L.add_pending and L.add_pending.get("open"))
        if self.bank_on and not self.live.dry_run and not own_open:   # Trevor 08:52 MT: collateral = trade deposit (no HWM)
            b = L.bank
            nat = round(pos.margin / self.coin_px(st), 9)
            b["trade_start_equity"] = self.bank_equity_flat(st)
            bankmod.debit(b, nat, "collateral", f"adopted position collateral ${pos.margin:.2f} at {self.coin_px(st):.4f}")
            b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
        lev = round(pos.size / pos.margin, 2) if pos.margin > 0 else L.last_leverage
        now = str(pd.Timestamp.now(tz="UTC"))
        lot = {"time": now, "price": round(pos.avg_entry, 6), "size": round(pos.size, 2), "coll": round(pos.margin, 4),
               "lev": lev, "first": True, "sig": None, "adopted": True}
        self.local = Local(side=pos.side, opened=now, n_entries=1, first_margin=pos.margin, last_leverage=lev,
                           first_entry=pos.avg_entry, last_entry=pos.avg_entry, last_size=pos.size, last_bar=L.last_bar,
                           day=L.day, day_start_equity=L.day_start_equity, lots=[lot], trade_opened_usd=lot["size"],
                           trim_day=L.trim_day, trim_day_n=L.trim_day_n, bank=L.bank, pending_cancels=L.pending_cancels)
        log.error("ADOPTED on-chain position not opened by this bot: %s $%.2f avg %.4f coll $%.2f x%g (single lot)",
                  "long" if pos.side == LONG else "short", pos.size, pos.avg_entry, pos.margin, lev)
        self.save()
        self.sync_tp_ladder(pos)
        self.save()

    def cancel_failed(self, reqs: list[dict[str, Any]], then: str | None, why: str) -> None:
        """Trevor 2026-10-12 08:28 MT: a cancel that did not confirm never halts. Requests still on-chain are recorded and
        retried every tick (retry_cancels); ``then`` ("sync" / "resize") re-places TP1/TP2 once all are gone."""
        try:
            live = {r["pubkey"] for r in self.chain().get("openRequests", [])}
        except Exception as e:  # noqa: BLE001 - cannot verify: keep them all pending
            log.warning("cancel check re-read failed: %s", e)
            live = {r["pubkey"] for r in reqs}
        known = {p["pubkey"] for p in self.local.pending_cancels}
        ra = self.local.rest_add
        for r in reqs:
            if r["pubkey"] in live and r["pubkey"] not in known:
                self.local.pending_cancels.append({"pubkey": r["pubkey"], "change": r.get("requestChange"), "then": then,
                                                   "ra": dict(ra) if ra and ra.get("pubkey") == r["pubkey"] else None})
        log.error("CANCEL NOT CONFIRMED (%s): no halt; retried each tick: %s", why,
                  [p["pubkey"] for p in self.local.pending_cancels])
        self.save()

    def retry_cancels(self, st: dict[str, Any], pos: ChainPosition | None) -> bool:
        """Each tick while cancels are pending: still on-chain -> cancel again; gone -> book the resting-add refund (if the
        bot no longer tracks that order itself) and drop it. True = something was done (the tick ends here)."""
        L = self.local
        open_ = {r["pubkey"] for r in st["openRequests"]}
        left, then = [], set()
        for p in L.pending_cancels:
            if p["pubkey"] in open_:
                log.warning("pending cancel %s still on-chain: retrying", p["pubkey"])
                if p.get("change") == "increase" and self.cfg.resting.any_on:
                    self.cmd("cancel-limit-order", {"request": p["pubkey"]})
                else:
                    self.cmd("cancel", {"request": p["pubkey"]})
                left.append(p)
                continue
            if L.rest_add and L.rest_add.get("pubkey") == p["pubkey"]:
                pass   # still tracked: resting_reconcile books fill or refund
            elif p.get("ra") and pos is None:
                self._rest_bank_refund(p["ra"], f"resting add {p['pubkey']} cancelled (pending cancel)")
            log.warning("pending cancel %s: gone on-chain", p["pubkey"])
            if p.get("then"):
                then.add(p["then"])
        L.pending_cancels = left
        if not left and pos is not None and L.side == pos.side:
            if "sync" in then:
                self.sync_tp_ladder(pos)
            elif "resize" in then:
                self.resize_tps(pos)
        self.save()
        return True

    def cancel_request(self, pubkey: str, size_before: float) -> str:
        """Cancel one request. Returns "ok" (cancelled, or already gone with the position unchanged), "filled" (the request
        is gone and the position size changed: it executed; the next tick reconciles) or "failed" (still on-chain).
        StaleOraclePrice (Custom 6003) is retried up to 2x with 5 s waits, like place_tp (a failed tx changes nothing)."""
        res: dict[str, Any] = {}
        for attempt in range(3):
            res = self.cmd("cancel", {"request": pubkey})
            if res.get("ok"):
                return "ok"
            if attempt < 2 and any(k in json.dumps([res.get("err"), res.get("error")]) for k in ("6003", "0x1773")):
                log.warning("cancel %s hit StaleOraclePrice (6003); retrying in 5s (%d/2)", pubkey, attempt + 1)
                time.sleep(5)
                continue
            break
        err = json.dumps(res.get("err") or res.get("error"))
        try:   # failed: did the request already go away (3012 AccountNotInitialized = account closed)?
            st2 = self.chain()
        except Exception as e:  # noqa: BLE001 - cannot verify -> treat as a real failure
            log.warning("cancel %s failed (%s) and the chain re-read failed: %s", pubkey, err, e)
            return "failed"
        if any(r.get("pubkey") == pubkey for r in st2.get("openRequests", [])):
            return "failed"
        size_now = self._chain_size(st2)
        why = "3012 AccountNotInitialized" if "3012" in err else err
        if abs(size_now - size_before) > 0.01:
            log.warning("cancel %s failed (%s): request gone and size %.2f -> %.2f (filled); next tick reconciles",
                        pubkey, why, size_before, size_now)
            return "filled"
        log.warning("cancel %s failed (%s) but the request is already gone on-chain and size is unchanged (%.2f): "
                    "treated as cancelled", pubkey, why, size_now)
        return "ok"

    def close(self, pos: ChainPosition, price: float, why: str) -> None:
        log.warning("CLOSE %s (%s)", "long" if pos.side == LONG else "short", why)
        st = self.chain()
        if not self.cancel_all(st):
            self.cancel_failed(st["openRequests"], None, "cancel before close")
        slip = self.live.price_slippage_pct
        worst = price * (1 - slip) if pos.side == LONG else price * (1 + slip)
        res = self.cmd("decrease", {"side": "long" if pos.side == LONG else "short", "entire": True,
                                    "priceSlippageUsd": round(worst, 2)})
        if self.live.dry_run:
            return
        st2 = self.chain()
        after = self.chain_position(st2)
        if after is not None or not res.get("ok"):
            self.halt(f"close not confirmed on-chain: {res}")
            return
        ledger.update(exit=f"{why.split(' ')[0]}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
        self.bank_payout(pos.margin + pos.pnl(price) - self.cfg.costs.close_fee * pos.size, f"close ({why[:60]})")
        try:
            self.sweep(st2, pos.side)   # SOL out before booking
        except Exception as e:  # noqa: BLE001 - retried next tick
            log.warning("sweep failed: %s", e)
        flows = self.book_flat(st2, pos.side)
        self.bank_flat(flows)
        self.local = Local(last_bar=self.local.last_bar, day=self.local.day, day_start_equity=self.local.day_start_equity,
                           flat_since=str(pd.Timestamp.now(tz="UTC")), trim_day=self.local.trim_day,
                           trim_day_n=self.local.trim_day_n, bank=self.local.bank,
                           pending_cancels=self.local.pending_cancels)

    def guard_tps(self) -> bool:
        """Trevor 2026-10-15 (all bots): TP1/TP2 sit at the profit-guard price only once a TP3 partial has filled on this
        trade (partials_done is reset on flat); before that tp1_pct / tp2_pct from the average."""
        return bool(self.local.partials_done and self.bank_on and self.local.bank)

    def guard_price(self, pos: ChainPosition, st: dict[str, Any], extra: float = 0.0) -> float | None:
        """Trevor 2026-10-15: price at which hwm_equity() == guard_target() (bisection on the existing functions; equity is
        monotone in price: up for a long, down for a short). None when already met at market or unreachable.
        ``extra`` (own coin, >= 0; 2026-10-28) raises the target: used for the guard TP fee buffer."""
        price, tgt = st["oracle"]["price"], self.guard_target(st) + max(extra, 0.0)
        if self.hwm_equity(pos, st, price) >= tgt:
            return None
        far = price * (2.0 if pos.side == LONG else 0.01)
        if self.hwm_equity(pos, st, far) < tgt:
            return None
        lo, hi = price, far   # lo: below target, hi: at/above target
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self.hwm_equity(pos, st, mid) < tgt else (lo, mid)
        return hi

    PRICE_TICK = 0.01   # trigger price precision the bot places at (place_tp / move_trigger: cents; same for SOL, ETH, BTC)

    def guard_round(self, side: int, px: float) -> float:
        """Trevor 2026-10-27 14:55 MT: the guard TP price rounded against itself to the price tick: short floor (lower),
        long ceil (higher), so a fill at the placed price never lands below the guard target (plain round() could be 1 tick short)."""
        n = px / self.PRICE_TICK
        n = math.floor(n + 1e-6) if side == SHORT else math.ceil(n - 1e-6)
        return round(n * self.PRICE_TICK, 2)

    # Trevor 2026-10-28 09:36 MT: guard TPs sit this much SOL of network fees beyond the guard target (own coin = this x SOL px /
    # coin px; SOL bot: as is). One guard re-place (2 cancels + 2 places) costs ~0.000024 SOL (ETH 2026-10-28 03:35-06:35 MT),
    # which moved the ETH guard price ~1.4-1.7 cents and left the just-placed TPs 1 tick on the wrong side -> re-place loop every
    # 10 min. 0.0001 SOL ~ 4 re-places: a re-place's own fees no longer cross the resting TPs; a real drift past them still does.
    GUARD_TP_BUFFER_SOL = 0.0001

    def guard_tp_buffer(self, st: dict[str, Any]) -> float:
        """GUARD_TP_BUFFER_SOL in the bot's own coin (the unit of hwm_equity / guard_target)."""
        if self.asset == "SOL":
            return self.GUARD_TP_BUFFER_SOL
        cpx = self.coin_px(st)
        return self.GUARD_TP_BUFFER_SOL * self.sol_price(st) / cpx if cpx > 0 else 0.0

    def guard_tp_px(self, pos: ChainPosition, st: dict[str, Any], verbose: bool = True) -> tuple[float, float | None]:
        """(TP1 = TP2 price, raw guard price or None). Guard price + fee buffer (target raised by guard_tp_buffer, i.e. further
        on the safe side), rounded conservatively, then clamped by the stricter fee floor. Raw guard None (met at market):
        fee floors as before; buffered price unreachable: the raw guard price."""
        gp = self.guard_price(pos, st)
        gpb = self.guard_price(pos, st, self.guard_tp_buffer(st)) if gp is not None else None
        if gpb is None or pos.side * (gpb - gp) < 0:
            gpb = gp
        px = st["oracle"]["price"] if gp is None else gpb
        if verbose:
            log.info("TP at guard: close price %s", f"{gp:.6f} (+ fee buffer {self.GUARD_TP_BUFFER_SOL:g} SOL -> {gpb:.6f})"
                     if gp else "n/a (guard met at market) -> fee floors")
        # Trevor 2026-10-16: TP1 and TP2 at the SAME price: guard price clamped by the stricter of the two fee floors
        # (long: higher, short: lower = beyond both); no tp_guard_gap_pct spacing, no tp_min_gap_usd.
        mins, _ = self.tp_fee_mins(pos, st)
        floor = max(mins[:2]) if pos.side == LONG else min(mins[:2])
        px = self.guard_round(pos.side, px) if gp is not None else round(px, 2)
        if pos.side * (px - floor) < 0:
            if verbose:
                log.info("TP at guard: %.2f inside the stricter fee floor -> %.2f", px, floor)
            px = round(floor, 2)
        return px, gp

    def guard_tp_targets(self, pos: ChainPosition, st: dict[str, Any], targets: tuple) -> tuple:
        """guard_tps: TP1 = TP2 = the guard close price (rounded against itself) clamped by the stricter fee floor (no gap).
        Guard already met at market (None): both at market, i.e. clamped to the fee floors."""
        from strategy import TpTarget
        px, _ = self.guard_tp_px(pos, st)
        return tuple(TpTarget(px, t.close_pct) if i < 2 else t for i, t in enumerate(targets))

    GUARD_DRIFT_COOLDOWN_S = 600   # after a guard-drift re-place: no further drift re-place for 10 min (its own network fees move the guard)

    def guard_drift_check(self, pos: ChainPosition, st: dict[str, Any]) -> bool:
        """Trevor 2026-10-27 14:55 MT: guard_tps owns TP1/TP2 and fees accrue (borrow, network fees) after they were placed:
        when a pending resting TP is now on the wrong side of the current conservative guard price (before the fee-floor clamp; a fill there would
        land below the guard target; short: TP above it, long: below), cancel and re-place TP1/TP2 via the normal
        sync_tp_ladder path (guard_defer, cancel_failed, tp_retry all apply). A TP merely further out than needed is kept.
        Hysteresis: compared on the rounded tick (>= 1 tick wrong), at most once per GUARD_DRIFT_COOLDOWN_S, skipped while a
        tp_retry / guard deferral / pending cancel / un-credited payout is being handled. Returns True when it re-placed.
        2026-10-28: compared with the UN-buffered guard price while placement adds GUARD_TP_BUFFER_SOL, so a drift smaller
        than the buffer (e.g. a re-place's own network fees) never re-places; a drift past the resting TPs still does."""
        L, n = self.local, len(self.cfg.tp.ladder)
        if not self.guard_tps() or L.tp_retry or L.pending_cancels or (L.bank or {}).get("tp_defer"):
            return False
        pend = list(range(pos.tp_hits, n))
        if not pend or len(L.tp_px) < n or any(not L.tp_px[i] for i in pend) or not self.tp_requests(st):
            return False
        now = time.time()
        if now < self._guard_drift_until or self.guard_bank_stale(st):
            return False
        gp = self.guard_price(pos, st)
        if gp is None:   # guard met at market: the tick's market-close guard handles it
            return False
        px = self.guard_round(pos.side, gp)   # the guard side only: a fee-floor move is fee_min_recheck's job (no churn on borrow accrual)
        wrong = [i for i in pend if pos.side * (px - L.tp_px[i]) > self.PRICE_TICK / 2]
        if not wrong:
            return False
        self._guard_drift_until = now + self.GUARD_DRIFT_COOLDOWN_S
        log.warning("TP at guard: guard price moved to %.6f (TP %.2f); resting %s on the wrong side -> re-placing TP1/TP2",
                    gp, px, [L.tp_px[i] for i in wrong])
        self.sync_tp_ladder(pos)
        return True

    GUARD_DEFER_MAX = 20       # ticks the guard-price TP re-place may wait for a payout credit / a plausible price
    GUARD_FAR_PCT = 0.03       # TP1 more than this beyond max(market, TP2 fee floor) counts as implausible

    def guard_bank_stale(self, st: dict[str, Any]) -> bool:
        """ETH / BTC: a payout is on its way (pay_wait, set by bank_payout, cleared when recon credits it) or the wallet
        token differs from the bank balance: the guard target would be computed from a stale bank."""
        b = self.local.bank
        if not self.bank_on or self.live.dry_run or not b or b.get("coin") not in bankmod.DUST or b["coin"] == "SOL":
            return False
        if b.get("pay_wait", 0) > 0:
            return True
        tok = st.get("sweepTokens") or {}
        if "error" in tok:
            return False
        atoms = int((tok.get(b["coin"]) or {}).get("atoms") or 0)
        return abs(atoms / 10 ** bankmod.DECIMALS[b["coin"]] - b["bal"]) > bankmod.DUST[b["coin"]]

    def guard_defer(self, pos: ChainPosition, st: dict[str, Any], tp1: float | None) -> bool:
        """guard_tps (2026-10-15): True = do not re-place now. Only while TPs are resting (never a window without TPs),
        at most GUARD_DEFER_MAX ticks. tp1 None: stale-bank check (before computing); else plausibility of the computed TP1."""
        L = self.local
        if not L.bank or not self.tp_requests(st) or L.bank.get("tp_defer", 0) >= self.GUARD_DEFER_MAX:
            return False
        if tp1 is None:
            why = "payout not credited to the bank yet" if self.guard_bank_stale(st) else ""
        else:
            price = st["oracle"]["price"]
            ref = max(price, self.tp_fee_mins(pos, st)[0][1]) if pos.side == LONG else min(price, self.tp_fee_mins(pos, st)[0][1])
            far = pos.side * (tp1 - ref) / ref > self.GUARD_FAR_PCT
            why = f"TP1 {tp1:.2f} implausibly far from market {price:.2f} / fee floor {ref:.2f}" if far else ""
        if not why:
            return False
        L.bank["tp_defer"] = L.bank.get("tp_defer", 0) + 1
        log.warning("TP at guard: re-place deferred (%s); resting TPs kept, retry next tick (%d/%d)", why, L.bank["tp_defer"],
                    self.GUARD_DEFER_MAX)
        self.save()
        return True

    def sync_tp_ladder(self, pos: ChainPosition) -> None:
        """Any exception (targets, cancel, placement) never halts: it sets tp_retry so the next tick re-places the TPs."""
        try:
            self._sync_tp_ladder(pos)
        except Exception as e:  # noqa: BLE001 - never halt; reconcile next tick
            log.error("TP ladder sync raised %r; tp_retry set (no halt)", e)
            self.local.tp_retry = True

    def _sync_tp_ladder(self, pos: ChainPosition) -> None:
        """Cancel existing TP triggers and place the full ladder on the whole position (strategy.tp_targets:
        tp1_pct / tp2_pct from the average (else the levels' distances), fee floors / never
        past market when tp.smart_tp2). Resets the momentum-fade tracking. Targets are computed BEFORE the old TPs
        are cancelled, so an exception there leaves the existing TPs in place."""
        st = self.chain()
        price = st["oracle"]["price"]
        L = self.local
        n = len(self.cfg.tp.ladder)
        targets = self.trim_tp_guard(pos, self.strategy.tp_targets(pos, price=price), "placement")
        at_guard = self.guard_tps()
        if at_guard and self.guard_defer(pos, st, None):   # payout not credited yet: keep the resting TPs, retry
            return
        targets = self.guard_tp_targets(pos, st, targets) if at_guard else self.fee_min_targets(pos, targets, st, "placement")
        if at_guard and self.guard_defer(pos, st, targets[0].price):   # implausibly far from market: keep, retry
            return
        if at_guard:
            L.bank.pop("tp_defer", None)
        if not self.cancel_reqs(st, self.ladder_requests(st)):
            self.cancel_failed(self.ladder_requests(st), "sync", "TP cancel")
            return
        side = "long" if pos.side == LONG else "short"
        L.tp_px = [0.0] * n
        if L.tp2_override and len(targets) > 1 and not at_guard:   # TP2_SET override survives re-places until the next add
            from strategy import TpTarget
            targets = tuple(list(targets[:1]) + [TpTarget(self.tp2_clamp(pos, st, L.tp2_override, targets[0].price),
                                                          targets[1].close_pct)] + list(targets[2:]))
        self.log_tp_reasons("placement")
        log.info("TP ladder for avg %.3f: %s", pos.avg_entry, ", ".join(f"{t.price:.2f} ({t.close_pct:.0%})" for t in targets))
        prev = targets[pos.tp_hits - 1].close_pct if pos.tp_hits else 0.0
        for i in range(pos.tp_hits, n):
            lv = targets[i]
            px = lv.price
            if pos.side * (px - price) < self.cfg.tp.tp_market_gap - 1e-9:   # at/through the market: Jupiter rejects it
                log.warning("TP %.2f not beyond market %.3f; skipped", px, price)
                continue
            if not self.place_tp(pos, side, i, px, prev):
                self.halt("TP placement failed")
                self.save()
                return
            prev = lv.close_pct
        self.save()

    TP_RETRY_MAX = 3          # consecutive failed attempts, then back off
    TP_RETRY_BACKOFF_S = 300  # a few minutes; the rest of the tick runs meanwhile and after every failed attempt

    def tp_retry_step(self, pos: ChainPosition) -> bool:
        """One tp_retry attempt. Returns True when the tick should end (TPs re-placed, as before); False when it is in
        back-off or the attempt failed again, so a persistent TP placement failure never freezes the rest of the tick."""
        L = self.local
        now = pd.Timestamp.now(tz="UTC")
        if L.tp_retry_until and now < pd.Timestamp(L.tp_retry_until):
            return False
        log.error("TP retry: re-placing TP1/TP2 on the open position (previous placement failed / not confirmed)")
        L.tp_retry = False
        self.sync_tp_ladder(pos)
        if L.tp_retry:   # failed again
            L.tp_retry_n += 1
            if L.tp_retry_n >= self.TP_RETRY_MAX:
                L.tp_retry_n = 0
                L.tp_retry_until = str(now + pd.Timedelta(seconds=self.TP_RETRY_BACKOFF_S))
                log.error("TP retry failed %d times in a row: backing off %d s (tick continues, no halt)",
                          self.TP_RETRY_MAX, self.TP_RETRY_BACKOFF_S)
            self.save()
            return False
        L.tp_retry_n, L.tp_retry_until = 0, ""
        self.save()
        return True

    def _accrued_borrow_usd(self, pos: ChainPosition, st: dict[str, Any] | None) -> float:
        """Accrued borrow fee of the open position: on-chain (jup.ts borrowFeeUsd), else est. hourly rate x size x hours."""
        try:
            v = ((st or {}).get("positions") or {}).get("long" if pos.side == LONG else "short", {}).get("borrowFeeUsd")
            if v is not None:
                return max(float(v), 0.0)
        except (AttributeError, TypeError, ValueError):
            pass
        hrs = max((pd.Timestamp.now(tz="UTC") - pos.opened).total_seconds() / 3600.0, 0.0)
        return self.cfg.costs.borrow_per_hour * pos.size * hrs

    def tp_fee_mins(self, pos: ChainPosition, st: dict[str, Any] | None) -> tuple[list[float], float]:
        """Fee-based minimum TP1 / TP2 prices (Trevor 2026-10-13) and the trade's total fees used."""
        c, t, L = self.cfg.costs, self.cfg.tp, self.local
        # Trevor 2026-10-13 20:37 MT: only fees of the currently open size (TP3 partials covered their own): opening
        # fee pro-rated = open_fee x size; borrow: on-chain accrual is already on the current size (Jupiter settles
        # borrow into collateral on every increase / decrease), so it is used as is.
        fees = tp_fee_total(pos.size, pos.size, c.open_fee, c.close_fee, self._accrued_borrow_usd(pos, st))
        mults = [t.tp1_fee_mult, t.tp2_fee_mult]
        return [tp_fee_min_price(pos.side, pos.avg_entry, pos.size, fees, m) for m in mults], fees

    def fee_min_prices(self, pos: ChainPosition, st: dict[str, Any] | None, prices: list[float], ctx: str) -> list[float]:
        pend = list(range(pos.tp_hits, len(prices)))
        mins, fees = self.tp_fee_mins(pos, st)
        out, why = apply_tp_fee_min(pos.side, [round(p, 2) for p in prices], pend, mins,
                                     0.0 if self.guard_tps() else self.cfg.tp.tp_min_gap_usd)   # guard: TP1 == TP2 allowed
        log.info("TP fee minimum (%s): total fees $%.4f -> TP1 min %.2f, TP2 min %.2f%s", ctx, fees, mins[0], mins[1],
                 "; " + "; ".join(why) if why else " (levels kept)")
        return out

    def fee_min_targets(self, pos: ChainPosition, targets: tuple, st: dict[str, Any] | None, ctx: str) -> tuple:
        if not targets:
            return targets
        from strategy import TpTarget
        px = self.fee_min_prices(pos, st, [t.price for t in targets], ctx)
        return tuple(TpTarget(p, t.close_pct) if abs(p - t.price) > 1e-9 else t for p, t in zip(px, targets))

    def fee_min_recheck(self, pos: ChainPosition, st: dict[str, Any]) -> bool:
        """Once per process with an open position: if a recorded pending TP1/TP2 sits inside its fee minimum,
        cancel/re-place via resize_tps (same prices otherwise). Returns True when it re-placed."""
        L, n = self.local, len(self.cfg.tp.ladder)
        pend = list(range(pos.tp_hits, n))
        if not pend or len(L.tp_px) < n or any(not L.tp_px[i] for i in pend):
            return False
        new = self.fee_min_prices(pos, st, list(L.tp_px[:n]), "recheck")
        if all(abs(new[i] - L.tp_px[i]) < 0.005 for i in pend):
            return False
        log.warning("TP fee minimum recheck: %s -> %s; re-placing", [L.tp_px[i] for i in pend], [new[i] for i in pend])
        self.resize_tps(pos)
        return True

    def tp2_clamp(self, pos: ChainPosition, st: dict[str, Any] | None, px: float, tp1: float | None) -> float:
        """TP2_SET price clamped to >= the TP2 fee minimum and beyond TP1 + tp_min_gap_usd (TP1 still pending)."""
        mins, _ = self.tp_fee_mins(pos, st)
        out = round(px, 2)
        if pos.side * (out - mins[1]) < -1e-9:
            out = mins[1]
        if tp1 and pos.tp_hits == 0 and pos.side * (out - tp1) < self.cfg.tp.tp_min_gap_usd - 1e-9:
            out = round(tp1 + pos.side * self.cfg.tp.tp_min_gap_usd, 2)
        return out

    @staticmethod
    def set_file_price(f: Path) -> float | None:
        """TP2_SET / TP3_SET: read the one price and delete the file; None (warning logged) when invalid."""
        try:
            raw = f.read_text().strip()
        except OSError as e:
            raw = f"<unreadable: {e}>"
        f.unlink(missing_ok=True)
        try:
            want = float(raw)
            if not math.isfinite(want) or want <= 0:
                raise ValueError("not a positive price")
            return want
        except ValueError as e:
            log.warning("%s: invalid content %r (%s); file deleted, nothing changed", f.name, raw[:80], e)
            return None

    def move_trigger(self, pubkey: str, px: float) -> bool:
        """Move a resting TP trigger in place (Jupiter updateDecreasePositionRequest2; same size, new price)."""
        return bool(self.cmd("update-trigger", {"request": pubkey, "triggerPriceUsd": round(px, 2)}).get("ok"))

    def tp2_set(self, pos: ChainPosition, st: dict[str, Any], f: Path) -> None:
        """One-shot manual TP2 move (file TP2_SET with a price; Trevor 2026-10-13 20:57 MT): the resting TP2 trigger is
        moved in place to that price (clamped; Trevor 2026-10-14); recorded in local.tp2_override. Never halts."""
        L, t = self.local, self.cfg.tp
        want = self.set_file_price(f)
        if want is None:
            return
        if self.guard_tps():
            log.warning("TP2_SET %.2f: ignored (guard_tps); file deleted, nothing changed", want)
            return
        n = len(t.ladder)
        if n < 2 or pos.tp_hits > 1:
            log.warning("TP2_SET %.2f: no pending TP2 (tp_hits %d); file deleted, nothing changed", want, pos.tp_hits)
            return
        if len(L.tp_px) < n:
            L.tp_px = (L.tp_px + [0.0] * n)[:n]
        old = L.tp_px[1]
        tp1 = L.tp_px[0] if pos.tp_hits == 0 else None
        px = self.tp2_clamp(pos, st, want, tp1)
        price = st["oracle"]["price"]
        if pos.side * (px - price) < t.tp_market_gap - 1e-9:
            log.warning("TP2_SET %.2f (clamped %.2f) not beyond market %.3f; file deleted, nothing changed", want, px, price)
            return
        reqs = self.tp_requests(st)
        mine = [r for r in reqs if old and abs(r["triggerPrice"] - old) < 0.006]
        if not mine and old == 0.0 and len(reqs) == n - pos.tp_hits:   # no record: the farthest trigger is TP2
            mine = sorted(reqs, key=lambda r: pos.side * r["triggerPrice"])[-1:]
        if len(mine) != 1:
            log.warning("TP2_SET %.2f: resting TP2 (%.2f) not identified among %s; nothing changed", want, old,
                        [r["triggerPrice"] for r in reqs])
            return
        if not self.move_trigger(mine[0]["pubkey"], px):
            log.error("TP2_SET %.2f: in-place update failed; TP2 unchanged at %.2f (no halt)", px, old)
            return
        L.tp_px[1] = L.tp2_override = px
        log.warning("TP2_SET: TP2 %.2f -> %.2f in place (requested %.2f%s)", old, px, want,
                    "" if abs(px - want) < 0.005 else ", clamped to fee minimum / TP1 + gap")
        self.save()

    def tp3_set(self, pos: ChainPosition, st: dict[str, Any], f: Path) -> None:
        """One-shot manual TP3 move (file TP3_SET with a price; Trevor 2026-10-14): the resting TP3 (partial) trigger is
        moved in place (no fee clamp; must be beyond market); marked manual so partial_sync keeps it until the next add /
        resize (size change) re-plans TP3. Never halts."""
        want = self.set_file_price(f)
        if want is None:
            return
        if not self.local.partials or self.local.partials[0].get("booked"):
            log.warning("TP3_SET %.2f: no resting TP3; file deleted, nothing changed", want)
            return
        p = self.local.partials[0]
        px = round(want, 2)
        price = st["oracle"]["price"]
        if pos.side * (px - price) < self.cfg.tp.tp_market_gap - 1e-9:
            log.warning("TP3_SET %.2f not beyond market %.3f; file deleted, nothing changed", want, price)
            return
        if not self.move_trigger(p["pubkey"], px):
            log.error("TP3_SET %.2f: in-place update failed; TP3 unchanged at %.2f (no halt)", px, p["px"])
            return
        log.warning("TP3_SET: TP3 %.2f -> %.2f in place", p["px"], px)
        p["px"] = px
        p["manual"] = True   # 2026-10-29: partial_sync holds this price until the next add / resize (size change)
        self.save()

    def resize_tps(self, pos: ChainPosition) -> None:
        """Any exception never halts: it sets tp_retry so the next tick re-places the TPs."""
        try:
            self._resize_tps(pos)
        except Exception as e:  # noqa: BLE001 - never halt; reconcile next tick
            log.error("TP resize raised %r; tp_retry set (no halt)", e)
            self.local.tp_retry = True

    def _resize_tps(self, pos: ChainPosition) -> None:
        """(targets are computed before the old TPs are cancelled.)
        Trevor 2026-10-12 06:07 MT: after a TP3 (per-add partial) fill, cancel and re-place the pending TP1/TP2
        triggers at the SAME prices (local.tp_px) sized on the remaining position with the ladder's % split. No
        recorded prices (e.g. after a restart): falls back to the normal full re-place (as RETP_NOW)."""
        n = len(self.cfg.tp.ladder)
        if self.guard_tps():   # guard price changes with every partial / add: full re-place at the new price
            self._sync_tp_ladder(pos)
            return
        pend = list(range(pos.tp_hits, n))
        if not pend or any(i >= len(self.local.tp_px) or not self.local.tp_px[i] for i in pend):
            log.warning("TP resize after TP3: no recorded TP prices; full TP re-place instead")
            self.sync_tp_ladder(pos)
            return
        st = self.chain()
        price = st["oracle"]["price"]
        pxs = list(self.local.tp_px)
        pxs[:n] = self.fee_min_prices(pos, st, pxs[:n], "resize")
        if not self.cancel_reqs(st, self.ladder_requests(st)):
            self.cancel_failed(self.ladder_requests(st), "resize", "TP cancel (resize after TP3)")
            return
        side = "long" if pos.side == LONG else "short"
        prev = self.cfg.tp.ladder[pos.tp_hits - 1].close_pct if pos.tp_hits else 0.0
        for i in pend:
            px = pxs[i]
            if pos.side * (px - price) < self.cfg.tp.tp_market_gap - 1e-9:
                log.warning("TP%d %.2f not beyond market %.3f; skipped (resize)", i + 1, px, price)
                continue
            if not self.place_tp(pos, side, i, px, prev):
                self.halt("TP placement failed (resize after TP3)")
                self.save()
                return
            prev = self.cfg.tp.ladder[i].close_pct
        log.warning("TP resize after TP3: size %.2f, TPs %s", pos.size, [pxs[i] for i in pend])
        self.save()

    def place_tp(self, pos: ChainPosition, side: str, i: int, px: float, prev: float) -> bool:
        lv = self.cfg.tp.ladder[i]
        args: dict[str, Any] = {"side": side, "triggerPriceUsd": round(px, 2), "triggerAbove": pos.side == LONG}
        if lv.close_pct >= 1.0:
            args["entire"] = True
        else:
            args["sizeUsd"] = round(pos.size * (lv.close_pct - prev), 2)
        for attempt in range(3):   # StaleOraclePrice (Custom 6003) is transient: the failed tx creates no request
            res = self.cmd("decrease", args)
            if res.get("ok"):
                break
            if attempt < 2 and any(k in json.dumps([res.get("err"), res.get("error")]) for k in ("6003", "0x1773")):
                log.warning("TP%d placement hit StaleOraclePrice (6003); retrying in 5s (%d/2)", i + 1, attempt + 1)
                time.sleep(5)
                continue
            return False
        if len(self.local.tp_px) < len(self.cfg.tp.ladder):
            self.local.tp_px = (self.local.tp_px + [0.0] * len(self.cfg.tp.ladder))[:len(self.cfg.tp.ladder)]
        self.local.tp_px[i] = round(px, 2)
        return True

    def log_tp_reasons(self, ctx: str) -> None:
        r = self.strategy.last_tp
        if r is not None:
            for why in r.reasons:
                log.info("TP adjust (%s): %s", ctx, why)

    def tp_requests(self, st: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in self.ladder_requests(st) if r.get("triggerPrice") and not r.get("executed")]

    # ------------------------------------------------------------- confirmation / per-add trim
    def confirm_size(self, size_before: float, direction: int, tries: int | None = None,
                     min_delta: float = 0.01) -> tuple[dict[str, Any], ChainPosition | None] | None:
        """Re-read the chain until the position size moved by > ``min_delta`` in ``direction`` (+1 grew, -1 shrank),
        up to ``tries`` reads ``confirm_wait`` s apart (a keeper fill can lag the request tx). Returns (state, position)
        or None if the size never moved."""
        n = tries or self.confirm_tries
        for k in range(n):
            try:
                st = self.chain()
                p = self.chain_position(st)
                size = p.size if p is not None else 0.0
                if direction * (size - size_before) > min_delta:
                    if k:
                        log.info("confirmation: size %.2f -> %.2f seen on read %d/%d", size_before, size, k + 1, n)
                    return st, p
            except Exception as e:  # noqa: BLE001 - a failed read counts as "not yet"
                log.warning("confirmation read %d/%d failed: %s", k + 1, n, e)
            if k < n - 1:
                time.sleep(self.confirm_wait)
        return None

    def _trim_fixed_usd(self, pos: ChainPosition) -> float:
        """Realized money of this trade's trims minus every fee of the whole trade (6+6 bps on everything opened,
        network fees so far + one more est.): the fixed part of the overall-net guard."""
        L, c = self.local, self.cfg.costs
        realized = sum(float(t.get("onchain_pnl_usd") or 0.0) for t in L.trims)
        opened = max(L.trade_opened_usd, sum(l["size"] for l in L.lots), pos.size)
        net_fee = 0.0
        try:
            rec = json.loads(ledger.OPEN.read_text()) if ledger.OPEN.exists() else {}
            net_fee = float(rec.get("network_fee_lamports") or 0) / LAMPORTS * (self._sol_px or 0.0)
        except (OSError, ValueError):
            pass
        return realized - (c.open_fee + c.close_fee) * opened - net_fee - self.cfg.trim.trim_network_usd

    def trim_tp_guard(self, pos: ChainPosition, targets: tuple, ctx: str) -> tuple:
        """Overall-net-positive rule: once this trade has trims, the pending TPs (as placed) must leave the WHOLE trade
        (trims included, real on-chain money, all fees) >= trim_min_net_usd; else TP2 / TP1 are raised and logged.
        No trims (or trim off) -> targets unchanged. Then the wallet-baseline profit guard ([profit_guard])."""
        # Trevor 2026-10-08 18:24 MT: no automatic TP moving by guards; TPs are set at open/add, only Grok Bot moves TP2.
        if (not getattr(self.cfg.trim, "trim_tp_raise", False) or not self.cfg.trim.enabled or not self.local.trims
                or not targets):
            return self.pg_tp_guard(pos, targets, ctx)
        from strategy import TpTarget
        pend, chunks = self._tp_chunks(pos)
        g = trimmod.tp_guard(pos.side, pos.avg_entry, chunks, [targets[i].price for i in pend], self._trim_fixed_usd(pos),
                             self.cfg.trim.trim_min_net_usd, self.cfg.tp.tp_min_gap_usd)
        if not g.raised:
            return self.pg_tp_guard(pos, targets, ctx)
        log.warning("TP adjust (%s): %s", ctx, g.reason)
        out = list(targets)
        for i, px in zip(pend, g.prices):
            out[i] = TpTarget(px, targets[i].close_pct)
        return self.pg_tp_guard(pos, tuple(out), ctx)

    def _tp_chunks(self, pos: ChainPosition) -> tuple[list[int], list[float]]:
        """Pending ladder indices and their closing sizes (USD) for the current position."""
        lad = self.cfg.tp.ladder
        pend = list(range(pos.tp_hits, len(lad)))
        prev = [lad[i - 1].close_pct if i else 0.0 for i in pend]
        base = pos.size / (1.0 - (lad[pos.tp_hits - 1].close_pct if pos.tp_hits else 0.0))   # size the ladder % refer to
        chunks = [base * (lad[i].close_pct - pv) for i, pv in zip(pend, prev)]
        chunks[-1] = pos.size - sum(chunks[:-1])
        return pend, chunks

    # ------------------------------------------------------------- wallet-baseline profit guard ([profit_guard])
    def _net_fee_sol(self) -> float:
        try:
            rec = json.loads(ledger.OPEN.read_text()) if ledger.OPEN.exists() else {}
            return float(rec.get("network_fee_lamports") or 0) / LAMPORTS
        except (OSError, ValueError):
            return 0.0

    def pg_active(self) -> bool:
        return self.cfg.profit_guard.enabled and self.local.pg_base_sol is not None and self.local.pg_dep_sol > 0

    def pg_min(self) -> float:
        g = self.cfg.profit_guard
        return max(g.min_trade_profit_sol, g.min_trade_profit_pct * (self.local.pg_base_sol or 0.0))

    def pg_project(self, pos: ChainPosition, chunks: list[float], prices: list[float]) -> float:
        """Projected SOL result of the whole trade with the pending ``chunks`` closing at ``prices``."""
        L = self.local
        sunk = L.pg_recv_sol - L.pg_dep_sol - self._net_fee_sol() - self.cfg.profit_guard.close_network_sol
        conv = None if self.asset == "SOL" else (self._sol_px or None)
        return trimmod.trade_sol(pos.side, pos.avg_entry, pos.margin, pos.borrow, chunks, prices, self.cfg.costs.close_fee,
                                 sunk, conv)

    def pg_tp_payout_usd(self, pos: ChainPosition, price: float, rp_now: Any) -> tuple[float, str]:
        """USD paid out by the partial TP fill just detected (pos = position after it): collateral returned (last seen
        collateral - now; pro rata if the last snapshot is not of the pre-fill size) + realised PnL (on-chain realisedPnlUsd
        delta, else at the TP price) - close fee."""
        L = self.local
        cl = L.last_size - pos.size
        if L.pg_margin > 0 and abs(L.pg_m_size - L.last_size) <= 0.01:
            old_m, src = L.pg_margin, "snapshot"
        else:
            old_m, src = pos.margin * L.last_size / pos.size if pos.size > 0 else pos.margin, "pro-rata"
        ret = max(old_m - pos.margin, 0.0)
        tpx = (L.tp_px[L.tp_hits] if L.tp_hits < len(L.tp_px or []) and L.tp_px[L.tp_hits] else price)
        pnl, psrc = pos.side * cl * (tpx - pos.avg_entry) / pos.avg_entry, "tp px"
        if rp_now is not None and L.pg_rpnl is not None and abs(float(rp_now) - L.pg_rpnl) > 1e-9:
            d = float(rp_now) - L.pg_rpnl
            if abs(d - pnl) <= max(0.5, 0.02 * cl):    # sanity: realised delta close to the TP-price estimate
                pnl, psrc = d, "realisedPnl"
        net = pnl - self.cfg.costs.close_fee * cl
        L.pg_tp_usd = round(L.pg_tp_usd + net, 6)
        return ret + net, (f"${cl:.2f} closed: collateral returned ${ret:.4f} ({src}) + pnl ${pnl:+.4f} ({psrc}) "
                           f"- fee ${self.cfg.costs.close_fee * cl:.4f}")

    def pg_usd_estimate(self, pos: ChainPosition, chunks: list[float], prices: list[float]) -> float:
        """Simple USD result of the whole trade with ``chunks`` closing at ``prices`` (trims + TP fills realised, all fees)."""
        return (self._trim_fixed_usd(pos) + self.local.pg_tp_usd
                + sum(pos.side * c * (p - pos.avg_entry) / pos.avg_entry for c, p in zip(chunks, prices)))

    def pg_tp_guard(self, pos: ChainPosition, targets: tuple, ctx: str) -> tuple:
        """TRACKING ONLY (owner rule 2026-10-08 18:02 MT: the profit guard must NEVER move TPs). Returns ``targets``
        unchanged; when the projected trade end at the TPs is below baseline + min, logs it (once per distinct value),
        with a simple USD cross-check. Its actions: market close once the close now meets it (pg_close_armed), TP-timer gate."""
        if not targets or not self.pg_active():
            return targets
        pend, chunks = self._tp_chunks(pos)
        old = [targets[i].price for i in pend]
        if len(old) != len(chunks):
            return targets
        r = self.pg_project(pos, chunks, old)
        if r < self.pg_min() - 1e-12:
            note = (f"baseline guard (tracking only, TPs NOT moved): trade would end {r:+.6f} SOL at TPs "
                    f"{[round(p, 2) for p in old]} (< +{self.pg_min():.6f}); usd est {self.pg_usd_estimate(pos, chunks, old):+.4f}")
            if note != getattr(self, "_pg_note", None):
                log.info("%s (%s)", note, ctx)
                self._pg_note = note
        return targets

    def trade_guard_armed(self) -> bool:
        """Trevor 2026-10-27: this trade had an add fill or a partial (TP3 / trim) since it opened -> the guard stays armed
        until flat. Latched in local.guard_armed; derived from the trade's state (n_entries > 1, > 1 lot, trims, partials_done:
        all reset only at a new trade) so a state written before the latch existed (restart) arms correctly."""
        L = self.local
        if not L.guard_armed and (L.n_entries > 1 or len(L.lots) > 1 or bool(L.trims) or bool(L.partials_done)):
            L.guard_armed = True
            log.warning("GUARD ARMED for the rest of this trade (adds %d, lots %d, trims %d, partials done %d); stays armed until flat",
                        max(L.n_entries - 1, 0), len(L.lots), len(L.trims), len(L.partials_done))
            self.save()
        return L.guard_armed

    def pg_close_armed(self) -> bool:
        """Profit-guard market close applies: guard active, close_when_met, and (close_requires "adds_or_trims") the
        position had at least one add or trim."""
        g, L = self.cfg.profit_guard, self.local
        if not (self.pg_active() and g.close_when_met):
            return False
        return g.close_requires == "any" or self.trade_guard_armed()

    def pg_close_ok(self, pos: ChainPosition, price: float) -> tuple[bool, str]:
        """Early close (TP timer) allowed only when closing now ends the trade >= baseline + min profit (SOL)."""
        if not self.pg_active():
            return True, ""
        fill = price * (1 - pos.side * self.cfg.costs.slippage)
        r = self.pg_project(pos, [pos.size], [fill])
        return r >= self.pg_min() - 1e-12, f"baseline guard: close now {r:+.6f} SOL vs min +{self.pg_min():.6f}"

    def pg_status(self, pos: ChainPosition, price: float) -> str:
        L = self.local
        if not self.pg_active():
            return "profit guard: no baseline for this position (opened before the guard) -> inactive"
        pend, chunks = self._tp_chunks(pos)
        tps = [p for p in (L.tp_px or [])[pos.tp_hits:] if p] if L.tp_px else []
        at_tp = self.pg_project(pos, chunks, tps) if len(tps) == len(chunks) else None
        now = self.pg_project(pos, [pos.size], [price * (1 - pos.side * self.cfg.costs.slippage)])
        b = L.pg_base_sol or 0.0
        usd = self.pg_usd_estimate(pos, chunks, tps) if len(tps) == len(chunks) else None
        return (f"profit guard: baseline {b:.6f} SOL, need end >= {b + self.pg_min():.6f} (+{self.pg_min():.6f}) | "
                f"projected end at TPs {tps}: " + (f"{b + at_tp:.6f} ({at_tp:+.6f})" if at_tp is not None else "n/a")
                + (f" [usd est {usd:+.4f}]" if usd is not None else "")
                + f" | close now: {b + now:.6f} ({now:+.6f}) | deposited {L.pg_dep_sol:.6f}, paid out {L.pg_recv_sol:.6f}, "
                f"network {self._net_fee_sol():.6f} SOL")

    def trim_status(self, pos: ChainPosition, price: float) -> str:
        L, t, c = self.local, self.cfg.trim, self.cfg.costs
        if t.trim_trigger == "distance_level":
            arm = trimmod.arm_price(pos.side, pos.avg_entry, t.trim_arm_dist_pct)
            lv = self.strategy.levels_provider(pd.Timestamp.now(tz="UTC")) if self.strategy.levels_provider else None
            lvl, hit = trimmod.trigger_level(pos.side, price, lv, t.trim_level_tol_atr, t.trim_level_front_run_atr,
                                              t.trim_level_front_run_pct)
            kind = "res" if pos.side > 0 else "sup"
            nxt = ("levels stale/missing -> no trim" if lvl is None else
                   f"{'AT' if hit else 'next'} {kind} {lvl:.3f} (fires {'>=' if pos.side > 0 else '<='} "
                   f"{trimmod.trigger_price(pos.side, lvl, lv.atr, t.trim_level_tol_atr, t.trim_level_front_run_atr, t.trim_level_front_run_pct):.3f})")
            idx = trimmod.select_lots(L.lots, pos.side, price, t, c.open_fee, c.close_fee) if L.lots else []
            adds = [i for i, l in enumerate(L.lots) if not (l.get("first") or i == 0)]
            thr = [trimmod.lot_threshold(L.lots[i], pos.side, t, c.open_fee, c.close_fee) for i in adds if i not in idx]
            near = (min(thr) if pos.side > 0 else max(thr)) if thr else None
            return (f"trim status: {'ARMED' if L.trim_armed else 'not armed'}"
                    + (f" @ {L.trim_arm_px:.3f}" if L.trim_armed and L.trim_arm_px else "")
                    + f" (arm {'<=' if pos.side > 0 else '>='} {arm:.3f} = avg {pos.avg_entry:.3f} "
                    f"{'-' if pos.side > 0 else '+'}{t.trim_arm_dist_pct * 100:.1f}%) | {nxt} | eligible "
                    f"[{','.join(f'#{i + 1}' for i in idx)}] of {len(adds)} adds"
                    + (f" (next lot at {near:.2f})" if near is not None else "")
                    + f" | trims this trade {len(L.trims)}{self._trim_note}")
        ea = trimmod.eligible_at(L.lots, L.opened, t.trim_clock, t.trim_after_hours, L.trim_np_last)
        lots = ", ".join(f"#{i + 1} {l['price']:.3f} ${l['size']:.2f}" + (" first" if l.get("first") or i == 0 else
                         f" >= {trimmod.lot_threshold(l, pos.side, t, c.open_fee, c.close_fee):.2f}")
                         for i, l in enumerate(L.lots))
        return (f"trim status: clock {t.trim_clock} -> eligible at {ea and ea.tz_convert('America/Denver'):%Y-%m-%d %H:%M} MT"
                if ea is not None else f"trim status: clock {t.trim_clock} -> no reference") + \
            f", lots [{lots}], trims this trade {len(L.trims)}{self._trim_note}"

    def maybe_trim(self, pos: ChainPosition, st: dict[str, Any], price: float) -> bool:
        """Per-add trim check ([trim], every trim_check_min). True when a trim was executed (TPs re-placed)."""
        t, L, c = self.cfg.trim, self.local, self.cfg.costs
        now = pd.Timestamp.now(tz="UTC")
        dist_mode = t.trim_trigger == "distance_level"
        if dist_mode and not L.trim_armed and trimmod.arm_hit(pos.side, pos.avg_entry, price, t.trim_arm_dist_pct):
            L.trim_armed, L.trim_arm_px, L.trim_arm_time = True, round(price, 4), str(now)
            log.info("trim ARMED: price %.3f is >= %.1f%% against avg %.3f (arm %.3f); armed until flat",
                     price, t.trim_arm_dist_pct * 100, pos.avg_entry, trimmod.arm_price(pos.side, pos.avg_entry, t.trim_arm_dist_pct))
            self.save()
        if self.strategy.net_close_pnl(pos, price) > 0:   # net positive: the TPs handle it; restarts the wait
            L.trim_np_last = str(now)
            return False
        if self._trim_checked is not None and now - self._trim_checked < pd.Timedelta(minutes=t.trim_check_min) - pd.Timedelta(seconds=5):
            return False
        self._trim_checked = now
        if not L.lots:   # restart without lot records: one protected lot = the whole position (nothing trimmable)
            L.lots = [{"time": L.opened or str(now), "price": round(pos.avg_entry, 6), "size": round(pos.size, 2),
                       "coll": round(pos.margin, 4), "lev": L.last_leverage, "first": True, "seeded": True}]
            L.trade_opened_usd = max(L.trade_opened_usd, pos.size)
            log.warning("trim: no lot records for the open position; seeded one protected lot (nothing trimmable)")
            self.save()
            return False
        if pos.tp_hits:
            return False
        if not trimmod.lots_match(L.lots, pos.size):
            msg = f"trim skipped: lots total ${sum(l['size'] for l in L.lots):.2f} != on-chain size ${pos.size:.2f}"
            if self._trim_note != f" ({msg})":
                log.warning(msg)
                self._trim_note = f" ({msg})"
            return False
        self._trim_note = ""
        lvl = None
        if dist_mode:
            if not L.trim_armed:
                return False
            lv = self.strategy.levels_provider(now) if self.strategy.levels_provider else None
            lvl, hit = trimmod.trigger_level(pos.side, price, lv, t.trim_level_tol_atr, t.trim_level_front_run_atr,
                                              t.trim_level_front_run_pct)
            if lvl is None:
                msg = "trim armed but analyst levels stale/missing (none on the trigger side) -> no trim"
                if self._trim_lv_note != msg:
                    log.warning(msg)
                    self._trim_lv_note = msg
                return False
            self._trim_lv_note = ""
            if not hit:
                return False
        else:
            ea = trimmod.eligible_at(L.lots, L.opened, t.trim_clock, t.trim_after_hours, L.trim_np_last)
            if ea is None or now < ea:
                return False
        if [r for r in st.get("allOpenRequests", st["openRequests"]) if r.get("requestType") != "trigger"]:
            log.info("trim: a request is in flight; skipped")
            return False
        today = str(now.date())
        if L.trim_day != today:
            L.trim_day, L.trim_day_n = today, 0
        if t.trim_max_per_day and L.trim_day_n >= t.trim_max_per_day:
            return False
        idx = trimmod.select_lots(L.lots, pos.side, price, t, c.open_fee, c.close_fee)
        if not idx:
            return False
        if self.cfg.resting.partials_enabled:   # Trevor 2026-10-12 00:27 MT: TP3 (partial TP order) is the only partial
            msg = f"trim: TP3 mode, market trim not executed (lots {[i + 1 for i in idx]} qualify)"
            if getattr(self, "_tp3_trim_note", None) != msg:
                log.info(msg)
                self._tp3_trim_note = msg
            return False
        if lvl is not None:
            log.info("trim TRIGGER: armed, price %.3f reached %s %.3f -> closing lots %s",
                     price, "resistance" if pos.side > 0 else "support", lvl, [i + 1 for i in idx])
        return self.execute_trim(pos, st, price, idx)

    def execute_trim(self, pos: ChainPosition, st: dict[str, Any], price: float, idx: list[int]) -> bool:
        t, L, c = self.cfg.trim, self.local, self.cfg.costs
        lots = [L.lots[i] for i in idx]
        size = round(sum(l["size"] for l in lots), 2)
        if size >= pos.size - 0.01 or any(l.get("first") or i == 0 for l, i in zip(lots, idx)):
            log.error("trim refused: would close the first entry / whole position (%s)", idx)
            return False
        side = "long" if pos.side == LONG else "short"
        slip = self.live.price_slippage_pct
        worst = price * (1 - slip) if pos.side == LONG else price * (1 + slip)
        desc = ", ".join(f"#{i + 1} {l['time'][:16]}Z @{l['price']:.3f} ${l['size']:.2f}" for i, l in zip(idx, lots))
        log.warning("TRIM start: %s lots [%s] = $%.2f at market ~%.3f (avg %.3f, collateral stays)", side, desc, size,
                    price, pos.avg_entry)
        self._trim_sent = True
        res = self.cmd("decrease", {"side": side, "sizeUsd": size, "priceSlippageUsd": round(worst, 2)})
        if self.live.dry_run:
            return False
        conf = self.confirm_size(pos.size, -1, tries=self.confirm_tries if res.get("ok") else 2, min_delta=0.5 * size)
        if conf is None:
            if res.get("ok"):
                self.halt(f"trim not confirmed on-chain: {res}")
                return False
            self._trim_fails += 1
            log.warning("TRIM failed (%s), position unchanged (%d/3)", res.get("err") or res.get("error"), self._trim_fails)
            if self._trim_fails >= 3:
                self.halt(f"trim failed 3x: {res}")
            return False
        self._trim_fails = 0
        st2, new = conf
        assert new is not None
        now = pd.Timestamp.now(tz="UTC")
        rec = st2["positions"][side]
        rp0 = float(st["positions"][side].get("realisedPnlUsd") or 0.0)
        rp1 = float(rec.get("realisedPnlUsd") or 0.0)
        closed = round(pos.size - new.size, 2)
        exit_px, src = price, "oracle"
        if rp1 != rp0:   # exit implied by the position's realized P&L change (Jupiter books against the avg)
            imp = pos.avg_entry * (1 + pos.side * (rp1 - rp0) / closed)
            if abs(imp / price - 1) < 0.01:
                exit_px, src = imp, "realisedPnl"
        onchain = trimmod.jup_realized(pos.side, closed, exit_px, pos.avg_entry) if src == "oracle" else rp1 - rp0
        net_usd = (res.get("feeLamports") or 0) / LAMPORTS * self._sol_px
        lot_gross = sum(pos.side * l["size"] * (exit_px - l["price"]) / l["price"] for l in lots)
        fees = (c.open_fee + c.close_fee) * closed + net_usd
        lot_gain = lot_gross - fees
        onchain_net = onchain - c.close_fee * closed - net_usd
        rec_t = {"time": str(now), "lots": lots, "size": closed, "exit": round(exit_px, 4), "exit_src": src,
                 "avg": round(pos.avg_entry, 4), "onchain_pnl_usd": round(onchain, 4), "onchain_net_usd": round(onchain_net, 4),
                 "lot_gain_usd": round(lot_gain, 4), "fees_usd": round(fees, 4), "sig": res.get("sig")}
        L.trims.append(rec_t)
        L.guard_armed = True   # 2026-10-27: a trim arms the guard until flat
        if self.cfg.resting.last_partial_rule:   # [resting] rule 3: a trim is a partial -> its exit is the last partial price
            L.last_partial_px = round(exit_px, 6)
        if L.pg_base_sol is not None:   # paid out = collateral returned + PnL - fee (a loss off collateral nets to ~0)
            pay = (pos.margin - new.margin) + onchain - c.close_fee * closed
            L.pg_recv_sol = round(L.pg_recv_sol + max(pay, 0.0) / (self._sol_px or price), 9)
        self.bank_payout((pos.margin - new.margin) + onchain - c.close_fee * closed, f"trim ${closed:.2f}")
        L.lots = [l for i, l in enumerate(L.lots) if i not in idx]
        L.n_entries = len(L.lots)
        L.last_entry, L.last_leverage = L.lots[-1]["price"], L.lots[-1].get("lev") or L.last_leverage   # rungs re-arm
        L.last_size = new.size
        L.trim_day_n += 1
        self.save()
        so_far_chain = sum(x["onchain_net_usd"] for x in L.trims)
        so_far_lot = sum(x["lot_gain_usd"] for x in L.trims)
        log.warning("TRIM done: closed $%.2f of %s lots [%s] at %.3f (%s), avg %.3f unchanged, size %.2f -> %.2f, "
                    "collateral %.2f -> %.2f | on-chain realized %+.4f USD (net of close fee %+.4f), lot-based true gain "
                    "%+.4f USD (%+.6f SOL) | trade so far: trims on-chain %+.4f, lot-based %+.4f | rungs re-armed: "
                    "n_entries %d, last_entry %.3f", closed, side, desc, exit_px, src, pos.avg_entry, pos.size, new.size,
                    pos.margin, new.margin, onchain, onchain_net, lot_gain, lot_gain / (self._sol_px or 1), so_far_chain,
                    so_far_lot, L.n_entries, L.last_entry)
        try:
            ledger.write_trim_row(rec_t, self._sol_px, asset=self.asset)
        except Exception as e:  # noqa: BLE001 - informational row; the trade row still carries the totals
            log.warning("trim ledger row failed: %s", e)
        self.sync_tp_ladder(self.chain_position(self.chain()) or new)
        return True

    def guard(self, it: Intent, pos: ChainPosition | None, st: dict[str, Any], equity: float) -> str | None:
        lv = self.live
        if self.killed():
            return "kill switch present"
        coll_after = (pos.margin if pos else 0.0) + it.margin
        if coll_after > lv.max_collateral_usd:
            return f"collateral after {coll_after:.2f} > max_collateral_usd {lv.max_collateral_usd}"
        size_after = (pos.size if pos else 0.0) + it.margin * it.leverage
        if size_after > lv.max_position_usd:
            return f"size after {size_after:.2f} > max_position_usd {lv.max_position_usd}"
        if it.leverage > lv.max_leverage:
            return f"leverage {it.leverage} > {lv.max_leverage}"
        if it.action is Action.OPEN and it.margin < lv.min_collateral_usd:  # $10 min is for opens only; adds may be smaller
            return f"collateral {it.margin:.2f} < min {lv.min_collateral_usd}"
        if self.local.day_start_equity - equity > lv.max_daily_loss_usd:
            return "max_daily_loss_usd reached"
        if self.bank_on:   # own-coin ledger: funded ONLY from this bot's own balance (the guard itself never borrows; inter-bot /
            # JupSOL loans are booked into bal beforehand by bank_tick)
            need = it.margin / self.coin_px(st)
            if need > self.local.bank["bal"] + 1e-12:
                return (f"insufficient own funds: needs {need:.9f} {self.local.bank['coin']} (${it.margin:.2f}), own balance "
                        f"{self.local.bank['bal']:.9f} -> skipped (never borrows from other bots)")
            if self.asset == "SOL" and margin_sol(st) - need < lv.sol_fee_reserve:
                return "would breach SOL fee reserve"
            if self.asset != "SOL" and margin_sol(st) < 0.005:
                return "wallet native SOL < 0.005 (network fees)"
            return None
        if margin_sol(st) - it.margin / self.sol_price(st) < lv.sol_fee_reserve:
            return "would breach SOL fee reserve"
        if self.budget is not None and it.margin > self.budget.free_usd + 0.01:
            return f"collateral {it.margin:.2f} > this bot's free budget {self.budget.free_usd:.2f} (share {self.budget.share:.0%})"
        return None

    def entries_paused(self) -> str | None:
        """live/PAUSE_UNTIL holds an ISO time (tz-aware, or naive = America/Denver); fresh opens blocked until then."""
        ln = (self.local.bank or {}).get("ib_loan") if self.bank_on else None
        if ln and ln.get("role") == "lender":   # Trevor 2026-10-12 18:13 MT: a lender opens nothing until repaid
            return f"lent {ln.get('sol')} {ln.get('lender_coin') or 'SOL'} to the {ln.get('peer')} bot; opens disabled until repaid"
        for f in [self.dir / "PAUSE_UNTIL", *(ROOT / x for x in self.cfg.bot.extra_pause_files)]:
            if not f.exists():
                continue
            try:
                until = pd.Timestamp(f.read_text().strip())
                until = until.tz_localize("America/Denver") if until.tzinfo is None else until
            except ValueError:
                return f"{f.name} ({f.parent}) unreadable -> paused"
            if pd.Timestamp.now(tz="UTC") < until:
                return f"entries paused until {until} ({f})"
        return None

    def open_or_add(self, it: Intent, pos: ChainPosition | None, st: dict[str, Any], equity: float) -> None:
        paused = self.entries_paused() if pos is None else None   # adds to an open position are not paused
        if paused:
            log.info("skip open: %s", paused)
            return
        why = self.guard(it, pos, st, equity)
        if why:
            log.warning("skip %s: %s", it.action.value, why)
            return
        price = st["oracle"]["price"]          # market price (SOL / ETH / BTC)
        slip = self.live.price_slippage_pct
        side = "long" if it.side == LONG else "short"
        sol = it.margin / self.sol_price(st)   # collateral is paid in native SOL
        args: dict[str, Any] = {"side": side, "collateralAtoms": str(int(sol * LAMPORTS)),
                                "sizeUsd": round(it.margin * it.leverage, 2),
                                "priceSlippageUsd": round(price * (1 + slip) if it.side == LONG else price * (1 - slip), 2)}
        nat = 0.0
        if self.bank_on and self.asset != "SOL":   # own coin (ETH / WBTC) funds the request
            coin = self.local.bank["coin"]
            atoms = int(it.margin / self.coin_px(st) * 10 ** bankmod.DECIMALS[coin])
            nat = atoms / 10 ** bankmod.DECIMALS[coin]
            args.update(collateralAtoms=str(atoms), inputMint=bankmod.MINT[coin])
            if it.side == SHORT:   # keeper swaps ETH / WBTC -> USDC collateral inside the request
                args["jupiterMinimumOut"] = str(int(it.margin * (1 - slip) * USDC_ATOMS))
        elif self.bank_on:
            nat = int(sol * LAMPORTS) / LAMPORTS
            if it.side == SHORT:
                args["jupiterMinimumOut"] = str(int(it.margin * (1 - slip) * USDC_ATOMS))
        elif it.side == SHORT:  # SOL input swapped to USDC collateral by the request
            args["jupiterMinimumOut"] = str(int(it.margin * (1 - slip) * USDC_ATOMS))
        elif self.asset != "SOL":  # long ETH/BTC: SOL input swapped to the custody token (ETH / WBTC) by the request
            args["jupiterMinimumOut"] = str(int(it.margin * (1 - slip) / price * 10 ** COLLATERAL_DECIMALS[self.asset]))
        if self.add_pending_wait(st, pos):   # Trevor 2026-10-12 08:52 MT: never a second add on an unresolved one
            return
        if not self.live.dry_run:   # 2026-10-12: persist "add pending" before sending (a restart
            # between the fill and the lot save lost a lot); recover_missing_add() rebuilds it from the chain
            self.local.add_pending = {"time": str(pd.Timestamp.now(tz="UTC")), "size": round(it.margin * it.leverage, 2),
                                      "lev": it.leverage, "margin": it.margin, "price": price,
                                      "size0": pos.size if pos is not None else 0.0, "sig": None, "open": pos is None,
                                      "nat": nat}
            self.save()
        res = self.cmd("increase", args)
        if not self.live.dry_run and self.local.add_pending is not None:
            self.local.add_pending["sig"] = res.get("sig")
            self.save()
        if self.bank_on and not self.live.dry_run and (res.get("ok") or res.get("sig")):
            b = self.local.bank
            if pos is None and b.get("book_pend"):   # previous trade not booked yet (payout window still open)
                self.bank_book_pending(self.sol_price(st), self.coin_px(st), "new trade opened")
            if pos is None:
                b["trade_dep"], b["trade_recv"] = 0.0, 0.0
                b["trade_start_equity"] = self.bank_equity_flat(st)   # per-trade guard baseline, before the opening debit
            bankmod.debit(b, nat, "collateral", f"{it.action.value} {side} ${it.margin:.2f} x{it.leverage:g} sig {res.get('sig')}")
            b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)   # a rejected request refunds the coin
            self.save()
        if self.live.dry_run:
            return
        # tolerant confirmation (2026-10-08: the 09:30 SOL add filled but the immediate read lagged the keeper -> halt):
        # re-read up to confirm_tries x confirm_wait s; a failed result is re-checked too (an escalated copy may have
        # landed). Halt only when the size never grew.
        size0 = pos.size if pos is not None else 0.0
        conf = self.confirm_size(size0, +1, tries=self.confirm_tries if res.get("ok") else 2)
        if conf is None:
            # Trevor 2026-10-12 08:46 MT: add_pending kept: a late fill is rebuilt by recover_missing_add (a late open
            # is adopted); absent on the next chain read -> normal logic re-places
            if self.bank_on:
                self.local.bank["window"] = 10   # refund of the collateral (if any) is this bot's flow, not external
                self.save()
            err = str(res.get("error", "")) if not res.get("ok") else ""
            if "Simulation failed" in err or "Blockhash not found" in err:
                # 2026-10-10: tx failed pre-flight simulation, so nothing was sent and size is unchanged.
                # Transient (RPC/blockhash); retry on a later tick instead of halting so the bot can run unattended.
                log.warning("%s not sent (simulation failed, size unchanged); will retry: %s", it.action.value, err[:200])
                return
            self.halt(f"{it.action.value} not confirmed on-chain: {res}")
            return
        new = conf[1]
        if not res.get("ok"):
            log.warning("%s reported %s but the position grew %.2f -> %.2f: treated as filled", it.action.value,
                        res.get("err") or res.get("error"), size0, new.size)
        L = self.local
        size_delta = it.margin * it.leverage
        fill = (trimmod.fill_price(pos.size, pos.avg_entry, new.size, new.avg_entry) if pos is not None else new.avg_entry) \
            or price
        lot = {"time": str(pd.Timestamp.now(tz="UTC")), "price": round(fill, 6), "size": round(new.size - size0, 2),
               "coll": round(new.margin - (pos.margin if pos is not None else 0.0), 4), "lev": it.leverage,
               "first": pos is None, "sig": res.get("sig")}
        if self.cfg.resting.partials_enabled and pos is not None:   # [resting] rule 2: this add gets its own partial TP
            lot.update(partial_due=True, rung=L.n_entries)          # rung = add number (n_entries before this add)
        if pos is None:
            ledger.on_open(str(pd.Timestamp.now(tz="UTC")), it.side, st["wallet"]["sol"], res.get("sig"), res.get("feeLamports"),
                           position=st["positions"][side].get("pubkey"))
            ledger.update(opened_size_usd=size_delta, size_usd=new.size, avg_entry=new.avg_entry)
        else:
            ledger.update(fee_lamports=res.get("feeLamports"), add_entry=True, opened_size_usd=size_delta,
                          size_usd=new.size, avg_entry=new.avg_entry)
        if pos is None:
            self.local = Local(side=it.side, opened=str(pd.Timestamp.now(tz="UTC")), n_entries=1,
                               first_margin=it.margin, last_leverage=it.leverage, first_entry=new.avg_entry,
                               last_entry=new.avg_entry, last_size=new.size, last_bar=L.last_bar, day=L.day,
                               day_start_equity=L.day_start_equity, lots=[lot], trade_opened_usd=lot["size"],
                               trim_day=L.trim_day, trim_day_n=L.trim_day_n, bank=L.bank, pending_cancels=L.pending_cancels,
                               pg_base_sol=round(self.budget.free_usd / self.sol_price(st), 9) if self.budget else 0.0,
                               pg_dep_sol=round(sol, 9))
            log.info("profit guard: baseline %.6f SOL (own free budget just before the open), deposited %.6f SOL",
                     self.local.pg_base_sol, sol)
        else:
            L.pg_dep_sol = round(L.pg_dep_sol + sol, 9)
            L.n_entries += 1
            L.guard_armed = True   # 2026-10-27: an add fill arms the guard until flat
            L.last_leverage, L.last_entry, L.last_size = it.leverage, price, new.size
            L.tp_hits, L.first_tp_time = 0, None   # fresh TP1 50% / TP2 100% ladder on the whole position
            L.lots.append(lot)
            L.trade_opened_usd += lot["size"]
            L.tp2_override = None   # a new add re-places TPs normally (TP2_SET dropped)
            L.rearm = None
        self.local.add_pending = None
        log.info("LOT added #%d: %s fill %.4f size $%.2f coll $%.2f x%g", len(self.local.lots), lot["time"][:19],
                 lot["price"], lot["size"], lot["coll"], lot["lev"])
        self.save()
        new = self.chain_position(self.chain())
        assert new is not None
        self.sync_tp_ladder(new)
        if pos is not None and self.cfg.resting.partials_enabled:   # [resting]: the single partial TP covers the new add
            self.partial_sync(new, price)

    # ------------------------------------------------------------- resting orders ([resting], 2026-10-11, default OFF)
    def _rest_note(self, key: str, msg: str, level: int = logging.INFO) -> None:
        """Log ``msg`` once per distinct message for ``key`` (a refused / skipped placement is retried every tick)."""
        notes = self.__dict__.setdefault("_rest_notes", {})
        if notes.get(key) != msg:
            notes[key] = msg
            log.log(level, "resting: %s", msg)

    def _rest_bank_refund(self, ra: dict[str, Any], why: str) -> None:
        """Reverse the collateral debit of a resting add that was cancelled / never filled (own-coin ledger)."""
        if not self.bank_on or self.live.dry_run or not ra.get("debited") or ra.get("booked"):
            return   # 2026-10-28: a booked (filled) add is never refunded
        b, amt = self.local.bank, float(ra.get("nat") or 0.0)
        b["bal"] = round(b["bal"] + amt, 9)
        b["trade_dep"] = round(b.get("trade_dep", 0.0) - amt, 9)
        bankmod.note(b, "refund", amt, why)
        # 2026-10-27: the placement never debits the request rent from bal (it sits in rent_held, outside bal), so the
        # cancel returns it to the reserve float, NOT to bal. Only when the SOL flat true-up already ran while this
        # request was still open (its position flows then counted the rent as gone: rent_out) does the refund credit it.
        if b.get("coin") == "SOL" and ra.get("rent_out"):
            bankmod.credit(b, bankmod.REQ_RENT, "refund_rent", why)
            amt = round(amt + bankmod.REQ_RENT, 9)
        b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
        log.info("BANK resting add refund %+.9f %s (%s)", amt, b["coin"], why)

    def _rest_bank_debit(self, ra: dict[str, Any]) -> None:
        if not self.bank_on or self.live.dry_run or ra.get("debited"):
            return
        b = self.local.bank
        bankmod.debit(b, float(ra.get("nat") or 0.0), "collateral",
                      f"resting add {ra.get('side')} ${ra.get('margin', 0):.2f} x{ra.get('lev', 0):g} @ {ra.get('trigger')} "
                      f"req {ra.get('pubkey')}")
        b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
        ra["debited"] = True

    def cancel_limit(self, pubkey: str) -> bool:
        """Cancel a resting limit add via the Jupiter Perps API (build -> verify -> owner signs -> /transaction/execute).
        True when cancelled or already gone; the bank refund is booked here when the record is this bot's."""
        res = self.cmd("cancel-limit-order", {"request": pubkey})
        if self.live.dry_run:
            return bool(res.get("ok"))
        gone = bool(res.get("ok"))
        if not gone:
            try:
                gone = not any(r.get("pubkey") == pubkey for r in self.chain().get("openRequests", []))
            except Exception as e:  # noqa: BLE001 - cannot verify -> failure
                log.warning("resting add cancel %s failed (%s) and the chain re-read failed: %s", pubkey, res.get("error"), e)
                return False
            if gone:   # gone without our cancel: filled or closed elsewhere -> the next reconcile decides (record kept)
                log.warning("resting add cancel %s failed (%s) but the request is gone; next tick reconciles", pubkey,
                            res.get("error") or res.get("err"))
                return True
            log.warning("resting add cancel %s failed: %s", pubkey, res.get("error") or res.get("err"))
            return False
        ra = self.local.rest_add
        if ra and ra.get("pubkey") == pubkey:
            self._rest_bank_refund(ra, f"resting add {pubkey} cancelled")
            self.local.rest_add = None
            self.save()
        return True

    def recover_missing_add(self, st: dict[str, Any], pos: ChainPosition, price: float) -> bool:
        """2026-10-12: on-chain size exceeds the lots total by a whole add whose lot was never saved (restart between
        the fill and the save) -> rebuild that lot from the chain's IncreasePositionEvent (fill price, size, collateral)
        and book it like a normal add. Only an exact single-fill match is rebuilt; otherwise nothing changes (trims
        keep skipping on the mismatch, as before). Resting add fills are left to resting_reconcile. True = lot added."""
        L = self.local
        gap = round(pos.size - sum(float(l["size"]) for l in L.lots), 2)
        if gap <= 0.01:
            return False
        ra = L.rest_add
        if ra and self.cfg.resting.any_on and abs(float(ra["size_usd"]) - gap) <= 0.01:
            return False   # a resting add fill: resting_reconcile books it
        key = (round(pos.size, 2), gap)
        if getattr(self, "_recover_tried", None) == key:
            return False   # one chain lookup per distinct mismatch
        self._recover_tried = key
        pubkey = st["positions"]["long" if pos.side == LONG else "short"].get("pubkey")
        try:
            res = jup("position-increases", {"position": pubkey, "limit": 40})
        except Exception as e:  # noqa: BLE001 - retried next tick
            log.warning("lot recovery: reading increase events failed: %s", e)
            self._recover_tried = None
            return False
        opened = pd.Timestamp(L.opened) if L.opened else None
        lot_times = [pd.Timestamp(l["time"]) for l in L.lots]
        cands = []
        for ev in res.get("increases") or []:
            t = pd.Timestamp(int(ev["blockTime"]), unit="s", tz="UTC")
            if opened is not None and t < opened - pd.Timedelta(minutes=2):
                continue
            if abs(float(ev["sizeUsdDelta"]) - gap) > 0.01:
                continue
            if any(abs((t - lt).total_seconds()) <= 180 for lt in lot_times):
                continue   # this fill already has a lot
            cands.append((t, ev))
        if not cands:
            log.warning("lot recovery: on-chain size %.2f exceeds lots total by $%.2f but no single unrecorded add fill "
                        "of that size was found on-chain; lots unchanged", pos.size, gap)
            return False
        t, ev = max(cands, key=lambda x: x[0])
        pend = L.add_pending
        coll_usd = float(ev["collateralUsdDelta"])
        if pend and abs(float(pend["size"]) - gap) <= 0.01:
            lev = float(pend["lev"])
        else:
            lev = round(float(ev["sizeUsdDelta"]) / coll_usd * 2) / 2 if coll_usd > 0 else L.last_leverage
        lot = {"time": str(t), "price": round(float(ev["price"]), 6), "size": round(float(ev["sizeUsdDelta"]), 2),
               "coll": round(coll_usd - float(ev["feeUsd"]), 4), "lev": lev, "first": False,
               "sig": (pend or {}).get("sig") or ev["sig"], "recovered": ev["sig"]}
        if self.cfg.resting.partials_enabled:
            lot.update(partial_due=True, rung=L.n_entries)
        newest = all(t > lt for lt in lot_times)
        L.lots.append(lot)
        L.lots.sort(key=lambda x: pd.Timestamp(x["time"]))
        L.rearm = None
        L.n_entries += 1
        L.guard_armed = True   # 2026-10-27: an add fill (recovered) arms the guard until flat
        L.trade_opened_usd += lot["size"]
        L.tp2_override = None   # a new add re-places TPs normally (TP2_SET dropped)
        if newest:
            L.last_leverage = lev
            L.last_entry = float(pend["price"]) if pend and pend.get("price") else lot["price"]
            L.last_size = pos.size
            L.tp_hits, L.first_tp_time = 0, None
        ledger.update(add_entry=True, opened_size_usd=lot["size"], size_usd=pos.size, avg_entry=pos.avg_entry)
        L.add_pending = None
        self.save()
        log.warning("LOT RECOVERED from chain: %s fill %.4f size $%.2f coll $%.2f x%g (keeper tx %s), n_entries %d, "
                    "lots total now $%.2f = on-chain $%.2f", lot["time"][:19], lot["price"], lot["size"], lot["coll"],
                    lot["lev"], ev["sig"], L.n_entries, sum(x["size"] for x in L.lots), pos.size)
        return True

    def resting_reconcile(self, st: dict[str, Any], pos: ChainPosition, price: float) -> tuple[ChainPosition, bool]:
        """Detect fills of this bot's resting add order and per-add partial triggers (their request accounts are gone).
        Books them (lots, leverage step, ledger, bank, profit-guard deposit / payout, last partial price) and moves
        L.last_size so the existing TP1/TP2 fill detection only sees what is left. Returns (position, TP re-sync needed)."""
        L, R, c = self.local, self.cfg.resting, self.cfg.costs
        reqs = st["openRequests"]
        on_chain = {r.get("pubkey") for r in reqs}
        ra = L.rest_add
        if ra and ra.get("unconfirmed"):
            if ra.get("pubkey") in on_chain:   # the submitted order did land
                ra.pop("unconfirmed", None)
                self._rest_bank_debit(ra)
                log.info("resting add %s confirmed on-chain", ra.get("pubkey"))
                self.save()
            elif pd.Timestamp.now(tz="UTC") - pd.Timestamp(ra["placed"]) < pd.Timedelta(minutes=2):
                return pos, False   # may still land: wait before judging
        gone_p = restmod.unmatched(L.partials, reqs)
        # 2026-10-28: a TP3 record already booked from its size drop (early_partial_fill) whose request account has now
        # gone: the fill is booked once already -> record removed quietly (no "records dropped", no second booking)
        stale = [p for p in gone_p if p.get("booked")]
        if stale:
            log.info("per-add partial(s) %s: stale order record gone after its fill was booked from the size drop; "
                     "record removed (already booked)", [p["pubkey"] for p in stale])
            L.partials = [p for p in L.partials if not p.get("booked") or p["pubkey"] in on_chain]
            self.save()
        gone_p = [p for p in gone_p if not p.get("booked")]
        add_gone = bool(ra) and ra.get("pubkey") not in on_chain
        if ra and ra.get("booked"):   # 2026-10-28: resting add already booked from its size growth (early_add_fill)
            if add_gone:   # its stale order record has now gone: removed quietly (no second booking, no refund)
                log.info("resting add %s: stale order record gone after its fill was booked from the size growth; "
                         "record removed (already booked)", ra.get("pubkey"))
                L.rest_add = None
                self.save()
            ra, add_gone = None, False
        delta = pos.size - L.last_size
        p_size = sum(float(p["size"]) for p in gone_p)
        resync = False
        ra_early = bool(ra) and not add_gone and self.early_add_fill(pos, price, ra, delta + p_size)
        if (add_gone or ra_early) and ra is not None:
            add_size = float(ra["size_usd"])
            if ra_early or delta + p_size > 0.5 * add_size:   # filled (also a submitted-but-unconfirmed order that landed and filled)
                fill = float(ra["trigger"])
                src = "order trigger"
                if not gone_p and L.rest_last_avg > 0:   # exact fill from the avg change (same as a market add)
                    f = trimmod.fill_price(L.last_size, L.rest_last_avg, pos.size, pos.avg_entry)
                    if f:
                        fill, src = f, "avg change"
                now = str(pd.Timestamp.now(tz="UTC"))
                lot = {"time": now, "price": round(fill, 6), "size": round(add_size, 2), "coll": round(float(ra["margin"]), 4),
                       "lev": ra["lev"], "first": False, "sig": ra.get("sig"), "via": "limit", "req": ra.get("pubkey")}
                if R.partials_enabled:
                    lot.update(partial_due=True, rung=L.n_entries)
                ledger.update(add_entry=True, opened_size_usd=add_size, size_usd=pos.size, avg_entry=pos.avg_entry)
                self._rest_bank_debit(ra)   # normally booked at placement; no-op then
                if L.pg_base_sol is not None:
                    L.pg_dep_sol = round(L.pg_dep_sol + float(ra.get("sol") or 0.0), 9)
                L.n_entries += 1
                L.guard_armed = True   # 2026-10-27: a resting add fill arms the guard until flat
                L.last_leverage, L.last_entry = ra["lev"], round(fill, 6)
                L.last_size = round(L.last_size + add_size, 2)
                L.tp_hits, L.first_tp_time = 0, None   # fresh TP1 / TP2 ladder on the whole position (as after a market add)
                L.lots.append(lot)
                L.trade_opened_usd += add_size
                L.tp2_override = None   # a new add re-places TPs normally (TP2_SET dropped)
                if ra_early:   # request account still listed: record kept, flagged booked, until it disappears (never
                    ra["booked"] = now   # booked twice, never refunded, not in hwm_equity's resting term, not re-planned)
                    L.rest_add = ra
                else:
                    L.rest_add = None
                L.rearm = None
                resync = True
                self.save()   # 2026-10-12: persist the booked fill at once (a restart must not lose the lot)
                log.warning("RESTING ADD FILLED%s: %s $%.2f x%g at %.4f (%s), n_entries %d, size now %.2f avg %.4f",
                            " (size growth before its order record disappeared)" if ra_early else "",
                            "long" if pos.side == LONG else "short", add_size, ra["lev"], fill, src, L.n_entries, pos.size,
                            pos.avg_entry)
            else:   # gone without a fill: cancelled / expired / never landed
                log.warning("resting add %s gone without a fill (size %.2f -> %.2f): record dropped, refund booked",
                            ra.get("pubkey"), L.last_size, pos.size)
                self._rest_bank_refund(ra, f"resting add {ra.get('pubkey')} gone unfilled")
                L.rest_add = None
        early = [] if gone_p or resync or pos.size >= L.last_size - 0.01 else self.early_partial_fill(pos, price)
        fills = gone_p or early
        if fills:
            if pos.size < L.last_size - 0.01:   # partial trigger(s) executed
                gone_p = fills
                closed = round(L.last_size - pos.size, 2)
                old_m = L.pg_margin if L.pg_margin > 0 and abs(L.pg_m_size - L.last_size) <= 0.01 and not resync \
                    else pos.margin * L.last_size / pos.size
                rp = st["positions"]["long" if pos.side == LONG else "short"].get("realisedPnlUsd")
                d_rp = (float(rp) - L.pg_rpnl) if (rp is not None and L.pg_rpnl is not None and not resync) else \
                    sum(pos.side * float(p["size"]) * (float(p["px"]) - pos.avg_entry) / pos.avg_entry for p in gone_p)
                usd = max(old_m - pos.margin, 0.0) + d_rp - c.close_fee * closed
                if L.pg_base_sol is not None:
                    L.pg_recv_sol = round(L.pg_recv_sol + max(usd, 0.0) / (self._sol_px or price), 9)
                self.bank_payout(usd, f"per-add partial ${closed:.2f}")
                times = {x for p in gone_p for x in (p.get("lots") or [p.get("lot_time")])}
                shut = [l for l in L.lots if l["time"] in times and not l.get("first")]
                last = max(gone_p, key=lambda p: pos.side * float(p["px"]))
                if shut:   # the resting add goes back to the closed lot's entry (long: highest, short: lowest), same size / lev
                    top = max(shut, key=lambda l: pos.side * float(l["price"]))
                    lev = float(top.get("lev") or L.last_leverage)
                    left = [l for l in L.lots if l["time"] not in times]
                    L.rearm = {"px": self.rearm_px(pos, float(top["price"]), float(last["px"]),
                                                   float(left[-1]["price"]) if left else None), "lev": lev,
                               "margin": round(float(top["size"]) / lev, 4)}
                L.lots = [l for l in L.lots if l["time"] not in times]
                if L.lots:
                    L.n_entries = len(L.lots)
                    L.last_entry, L.last_leverage = L.lots[-1]["price"], L.lots[-1].get("lev") or L.last_leverage
                if R.last_partial_rule or R.partials_enabled:
                    L.last_partial_px = float(last["px"])
                L.partials_done += [{**p, "filled": str(pd.Timestamp.now(tz="UTC"))} for p in gone_p]
                L.guard_armed = True   # 2026-10-27: a partial (TP3) arms the guard until flat, even with lots / n_entries back to 1
                L.last_size = pos.size
                self._tp_resize = True   # Trevor 2026-10-12 06:07 MT: re-size TP1/TP2 to the remaining position
                ledger.update(exit=f"P{len(L.partials_done)}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
                log.warning("PER-ADD PARTIAL FILLED%s: %s closed $%.2f at ~%s; payout est $%.4f; last partial price %s; "
                            "lots left %d, n_entries %d, last_entry %.4f",
                            " (size drop before its order record disappeared)" if early else "", [p["pubkey"] for p in gone_p],
                            closed, [p["px"] for p in gone_p], usd, L.last_partial_px, len(L.lots), L.n_entries, L.last_entry)
            else:
                log.warning("per-add partial(s) %s gone without a size drop: records dropped", [p["pubkey"] for p in gone_p])
            gone = {p["pubkey"] for p in gone_p}
            if early:   # request account still on-chain: keep the record flagged booked until it disappears (never re-booked,
                for p in L.partials:   # never cancelled / re-planned by partial_sync, excluded from the TP1/TP2 ladder)
                    if p["pubkey"] in gone:
                        p["booked"] = str(pd.Timestamp.now(tz="UTC"))
            else:
                L.partials = [p for p in L.partials if p["pubkey"] not in gone]
        if add_gone or ra_early or gone_p or early:
            self.save()
            pos = self.chain_position(st) or pos
        return pos, resync

    def early_add_fill(self, pos: ChainPosition, price: float, ra: dict[str, Any], grow: float) -> bool:
        """Trevor 2026-10-28 14:15 MT (SOL 14:01 MT): the chain can show a resting add fill as position size growth BEFORE
        its request account disappears. Size growth that matches the resting add (grown by its size, or the market reached
        its trigger and the position grew by more than half of it, never more than the add) is that add's fill. Mirror of
        early_partial_fill."""
        add = float(ra["size_usd"])
        tol = max(0.05, 0.005 * add)
        if grow <= 0.5 * add or grow > add + tol:
            return False
        trig = float(ra["trigger"])
        reached = pos.side * (price - trig) <= max(self.cfg.tp.tp_market_gap, 0.0005 * trig)   # short: price >= trigger - tol
        return abs(grow - add) <= tol or reached

    def early_partial_fill(self, pos: ChainPosition, price: float) -> list[dict[str, Any]]:
        """Trevor 2026-10-28 (BTC 12:46 MT): the chain can show a TP3 (per-add partial) fill as a size drop BEFORE its
        request account disappears. A size drop that matches the resting TP3 (closed size = its size, or the market reached
        its trigger and no pending TP1/TP2 trigger, closing at most its size) is that TP3 fill, not a TP1/TP2 hit.
        Returns the matching (not yet booked) partial record(s); [] = an ordinary TP fill."""
        L = self.local
        closed = L.last_size - pos.size
        out = []
        for p in L.partials:
            if p.get("booked"):
                continue
            psz = min(float(p["size"]), L.last_size)
            size_ok = abs(closed - psz) <= max(0.05, 0.005 * psz)
            tol = max(self.cfg.tp.tp_market_gap, 0.0005 * float(p["px"]))
            reached = pos.side * (float(p["px"]) - price) <= tol   # long: price >= TP3 - tol; short: price <= TP3 + tol
            tps = [x for x in (L.tp_px or [])[L.tp_hits:] if x]
            tp_reached = any(pos.side * (float(x) - price) <= 0 for x in tps)
            if tp_reached and not reached:   # a TP1/TP2 trigger was crossed but not the TP3 one: an ordinary TP fill
                continue
            if size_ok or (reached and not tp_reached and closed <= psz + max(0.05, 0.005 * psz)):
                out.append(p)
        return out[:1]

    def resting_tick(self, pos: ChainPosition, st: dict[str, Any], price: float, account: Any, equity: float) -> None:
        """Every tick with an open position: place what is missing (per-add partials, then the next resting add)."""
        R = self.cfg.resting
        self._tp3_st = st
        if R.partials_enabled:
            self.partial_sync(pos, price)
        ra = self.local.rest_add
        if R.adds_enabled and ra is None:
            self.place_rest_add(pos, st, price, account, equity)
            return
        if ra.get("booked"):
            self._rest_note("add", "resting add booked from its size growth; waiting for its stale order record to disappear")
            return
        if not R.adds_enabled or ra.get("unconfirmed") or self.live.dry_run:
            return
        if self.local.rearm and abs(ra["trigger"] - self.local.rearm["px"]) > self.add_front() + 0.011 \
                and self.rearm_add(pos, st, price, account, equity, ra):
            return
        self.replan_add(pos, st, price, account, equity, ra)

    def rearm_px(self, pos: ChainPosition, entry: float, partial_px: float, rest_last: float | None = None) -> float:
        """Re-arm price after a TP3 fill (set once, at fill time). readd_gap_atr = 0: the closed lot's entry (old rule; the
        order then gets the usual add front-run). Trevor 2026-10-27, readd_gap_atr > 0: the more adverse of the closed lot's
        entry and partial_px + readd_gap_atr x ladder ATR (short: higher / long: lower), rounded to the adverse cent, so the
        re-add is never inside the closed lot's entry; it is placed exactly there (rest_add_args: no front-run, so the
        re-arms no longer creep 0.1 ATR toward the market each cycle). 2026-10-27 13:50 MT: with dca.min_place_gap_atr > 0
        it is also never inside min_place_gap_atr x ladder ATR beyond rest_last (the remaining lots' last entry)."""
        gap_atr = self.cfg.resting.readd_gap_atr
        if gap_atr <= 0:
            return entry
        lv = self.strategy._levels
        atr = float(lv.atr) if lv is not None and lv.atr else (self.front_atr() or 0.0)
        lvl = partial_px - pos.side * gap_atr * atr
        px = lvl if pos.side * (entry - lvl) > 0 else entry
        # Trevor 2026-10-27 13:50 MT: also >= dca.min_place_gap_atr x ATR beyond the REMAINING last entry (rest_last)
        mgap = self.cfg.dca.min_place_gap_atr
        mlvl = rest_last - pos.side * mgap * atr if (mgap > 0 and rest_last is not None and atr > 0) else None
        if mlvl is not None and pos.side * (px - mlvl) > 0:
            px = mlvl
        px = math.floor(round(px * 100, 6)) / 100 if pos.side > 0 else math.ceil(round(px * 100, 6)) / 100
        log.warning("RESTING ADD re-arm price %.2f = most adverse of closed lot entry %.4f, last partial %.4f %s %.2f x ATR "
                    "%.4f (= %.4f)%s", px, entry, partial_px, "+" if pos.side < 0 else "-", gap_atr, atr, lvl,
                    "" if mlvl is None else " and remaining last entry %.4f %s %.2f x ATR (= %.4f)" % (
                        rest_last, "+" if pos.side < 0 else "-", mgap, mlvl))
        return px

    def rearm_add(self, pos: ChainPosition, st: dict[str, Any], price: float, account: Any, equity: float,
                  ra: dict[str, Any]) -> bool:
        """2026-10-15: a TP3 partial closed add lot(s) -> the single resting add moves to the closed lot's entry. No in-place
        edit (keeper co-signed): cancel, and ONLY on a confirmed cancel place the new one (never two resting adds).
        True when a cancel was attempted (the re-plan check is then skipped this tick)."""
        acc = types.SimpleNamespace(cash=account.cash + float(ra["margin"]))   # the cancel frees the old add's collateral
        plan, why = self.rest_add_plan(pos, acc.cash, price, st=st)
        if plan is None or getattr(plan, "source", "") != "rearm":
            self._rest_note("rearm", f"resting add stays at {ra['trigger']}: {why or 'market not beyond the re-arm price'}")
            return False
        blocked = self._replace_guard(pos, st, equity, ra, plan)
        if blocked:
            self._rest_note("rearm", f"resting add stays at {ra['trigger']}: re-arm blocked: {blocked}")
            return False
        log.warning("RESTING ADD re-arm: cancelling %s at %s to re-place at the re-arm price %.2f ($%.2f x%g)",
                    ra["pubkey"], ra["trigger"], plan.trigger, plan.margin, plan.leverage)
        self._cancel_then_place(pos, st, price, acc, equity, ra, "re-arm")
        return True

    def _replace_guard(self, pos: ChainPosition, st: dict[str, Any], equity: float, ra: dict[str, Any], plan: Any) -> str | None:
        """guard() for the replacement add, with the old add's bank debit counted as already refunded."""
        b = self.local.bank if self.bank_on and ra.get("debited") else None
        if b:
            b["bal"] += float(ra.get("nat") or 0.0)   # funds check as if already refunded
        try:
            return self.guard(Intent(Action.ADD, pos.side, plan.trigger, plan.margin, plan.leverage), pos, st, equity)
        finally:
            if b:
                b["bal"] -= float(ra.get("nat") or 0.0)

    def _cancel_then_place(self, pos: ChainPosition, st: dict[str, Any], price: float, acc: Any, equity: float,
                           ra: dict[str, Any], what: str) -> None:
        """Cancel the resting add; ONLY on a confirmed cancel (record cleared, refund booked) place the replacement. A failed
        cancel leaves the old add on-chain and recorded (never zero, never two); a failed placement after a confirmed
        cancel is retried by the normal 'no resting add -> place' path next tick."""
        if not self.cancel_limit(ra["pubkey"]) or self.local.rest_add is not None:
            log.warning("resting add %s: cancel not confirmed; nothing re-placed, retry next tick", what)
            return
        left = dict(st, openRequests=[r for r in st["openRequests"] if r.get("pubkey") != ra["pubkey"]])
        self.place_rest_add(pos, left, price, acc, equity)

    def replan_add(self, pos: ChainPosition, st: dict[str, Any], price: float, account: Any, equity: float,
                   ra: dict[str, Any]) -> bool:
        """2026-10-24 (Trevor): the analyst levels moved -> a confirmed resting add whose current plan is a "levels" rung is
        re-placed at add_px(new level) when it is off by more than max(add_front(), 0.25 x ATR) (the analyst ATR the
        ladder gaps use). Same confirmed cancel-then-place as the re-arm; size from the normal ladder plan. True = re-placed."""
        acc = types.SimpleNamespace(cash=account.cash + float(ra["margin"]))   # the cancel frees the old add's collateral
        plan, _ = self.rest_add_plan(pos, acc.cash, price, borrow=False)
        if plan is None or getattr(plan, "source", "") != "levels":
            return False
        lv = self.strategy._levels
        atr = float(lv.atr) if lv and lv.atr else 0.0
        if atr <= 0 or self.front_atr() is None:
            return False
        new, old = self.add_px(pos, plan.trigger), float(ra["trigger"])
        thr = max(self.add_front(), 0.25 * atr)
        if abs(new - old) <= thr:
            return False
        blocked = self._replace_guard(pos, st, equity, ra, plan)
        if blocked:
            self._rest_note("replan", f"resting add stays at {old}: re-plan to {new} blocked: {blocked}")
            return False
        log.warning("RESTING ADD re-planned: %.2f -> %.2f (level %.2f src %s, |diff| %.2f > max(front %.2f, 0.25 x ATR %.2f)) "
                    "cancelling %s; new $%.2f x%g", old, new, plan.trigger, level_tf(lv, plan.trigger), abs(new - old),
                    self.add_front(), atr, ra["pubkey"], plan.margin, plan.leverage)
        self._cancel_then_place(pos, st, price, acc, equity, ra, "re-plan")
        return True

    def add_reserve_short_usd(self, st: dict[str, Any] | None, margin: float) -> float:
        """2026-10-28: USD by which an add of ``margin`` would breach the native SOL fee reserve (guard: native wallet SOL
        - need >= sol_fee_reserve), SOL bot on the own-coin ledger only; 0 for ETH / BTC, without a state, or when the
        reserve holds. The JupSOL next-add loan covers this gap too (live/jupsol_loans.next_add_need_usd)."""
        if st is None or not self.bank_on or self.asset != "SOL":
            return 0.0
        px = self.coin_px(st)
        if px <= 0:
            return 0.0
        return max(0.0, float(margin) / px + self.live.sol_fee_reserve - margin_sol(st)) * px

    def rest_add_plan(self, pos: ChainPosition, cash: float, price: float | None = None,
                      borrow: bool = True, st: dict[str, Any] | None = None) -> tuple[Any, str]:
        """(AddPlan | None, why-not) for the next resting add: the ladder's next rung, planned from the last fill (rule 3:
        from the more adverse of the last fill and the last partial price) with this bot's free collateral."""
        R, L = self.cfg.resting, self.local
        if self.cfg.dca.mode != "levels":
            return None, "dca.mode is not levels (no ladder)"
        rr = L.rearm
        if rr and price is not None and pos.side * (price - rr["px"]) > 0:   # re-arm at the closed add lot's entry (TP3 partial)
            if not self.strategy._adds_left(pos):
                return None, "no adds left (max_adds / allow_after_partial)"
            if rr["margin"] > cash:
                return None, f"re-arm margin {rr['margin']:.2f} > free collateral {cash:.2f}"
            return types.SimpleNamespace(trigger=rr["px"], margin=rr["margin"], leverage=rr["lev"], source="rearm"), ""
        p = pos
        if R.last_partial_rule and L.last_partial_px:
            import dataclasses as _dc
            p = _dc.replace(pos, last_entry=restmod.add_ref(pos.side, pos.last_entry, L.last_partial_px))
        plan = self.strategy.plan_next_add(p, cash, pd.Timestamp.now(tz="UTC"))
        if plan is None:
            # 2026-10-25: probe the next rung with a SANE budget, never cash + 1e9: the
            # rung collateral is capped by the budget, so a 1e9 probe asked for $180M and no lender could ever fund it.
            # Smallest probe first: free cash + 1 rung, + 2 rungs, ... (IB_PROBE_STEPS is a search bound, not a loan cap);
            # the first plan found is the real next rung and its shortfall is requested in full.
            rung, big, k = self.ib_rung_usd(), None, 1
            while borrow and big is None and rung > 0 and k <= bankmod.IB_PROBE_STEPS:
                big = self.strategy.plan_next_add(p, max(cash, 0.0) + k * rung, pd.Timestamp.now(tz="UTC"))
                k += 1
            if borrow and big is not None and big.margin > cash:   # Trevor 18:13 MT: short of equity -> borrow from a flat bot (no cap)
                # 2026-10-27: peers first exactly as today; JupSOL lends the exact shortfall + $1 only when no peer can
                # cover it ([ledger] jupsol_loans = "on"; live/jupsol_loans.py). True = no inter-bot request this tick.
                if not jlmod.next_add_shortfall(self, p, cash, big, self.add_reserve_short_usd(st, big.margin)):
                    self.ib_request(bankmod.ib_need_usd(big.margin, cash))   # shortfall + $1, min $2
            return None, "no ladder plan (budget / liq rule)"
        if not self.strategy._adds_left(pos):
            return None, "no adds left (max_adds / allow_after_partial)"
        if R.last_partial_rule and not restmod.beyond_last_partial(pos.side, plan.trigger, L.last_partial_px):
            return None, f"next rung {plan.trigger:.4f} is not beyond the last partial price {L.last_partial_px}"
        return plan, ""

    def _reserve_loan(self, pos: ChainPosition, st: dict[str, Any], cash: float, plan: Any) -> None:
        """2026-10-28: the rung fits in the free balance but its add would breach the native SOL fee reserve (SOL bot):
        the JupSOL next-add loan (jupsol_loans on / observe) covers the reserve gap + $1. No inter-bot request here
        (a peer that can cover it only makes this bot wait, as for a shortfall); jupsol_loans "off": nothing."""
        if not (self.bank_on and self.asset == "SOL") or jlmod.mode(self.cfg) == "off":
            return
        p = pos
        if self.cfg.resting.last_partial_rule and self.local.last_partial_px:
            import dataclasses as _dc
            p = _dc.replace(pos, last_entry=restmod.add_ref(pos.side, pos.last_entry, self.local.last_partial_px))
        jlmod.next_add_shortfall(self, p, cash, plan, self.add_reserve_short_usd(st, plan.margin))

    def front_atr(self) -> float | None:
        """ATR for the add front-run: own 1h ATR; 2026-10-24: before the first kline fetch (first tick after a restart)
        the analyst / ladder ATR (strategy._levels.atr); None when neither is known."""
        if self._own_atr:
            return float(self._own_atr)
        lv = self.strategy._levels
        return float(lv.atr) if lv is not None and lv.atr else None

    def add_front(self) -> float:
        """Front-run distance (USD) before the add level: dca.add_frontrun_atr x front_atr() (0 without an ATR)."""
        return max(self.cfg.dca.add_frontrun_atr, 0.0) * (self.front_atr() or 0.0)

    def add_px(self, pos: ChainPosition, trigger: float) -> float:
        """Resting add price: the level moved add_front() toward the market (long up, short down).
        Trevor 2026-10-27: dca.min_place_gap_atr > 0 -> the front-run never pulls the placed price inside min_place_gap_atr x
        ladder (analyst) ATR beyond the add ref (the more adverse of the last entry and the last partial, as the plan uses);
        clamped to that distance, rounded to the adverse cent. Re-arms (TP3 refill at their own price) do not come here."""
        px = round(trigger + pos.side * self.add_front(), 2)
        gap = self.cfg.dca.min_place_gap_atr
        lv = self.strategy._levels
        atr = float(lv.atr) if lv is not None and lv.atr else 0.0
        if gap <= 0 or atr <= 0:
            return px
        ref = pos.last_entry
        if self.cfg.resting.last_partial_rule and self.local.last_partial_px:
            ref = restmod.add_ref(pos.side, pos.last_entry, self.local.last_partial_px)
        floor = ref - pos.side * gap * atr
        if pos.side * (px - floor) > 0:   # inside the minimum (short: below it / long: above it)
            px = math.floor(round(floor * 100, 6)) / 100 if pos.side > 0 else math.ceil(round(floor * 100, 6)) / 100
        return px

    def rest_add_args(self, pos: ChainPosition, st: dict[str, Any], plan: Any) -> tuple[dict[str, Any], float, float]:
        """CLI args for the limit add (same collateral funding as a market add) + (native units debited, SOL deposited)."""
        sol = plan.margin / self.sol_price(st)
        side = "long" if pos.side == LONG else "short"
        if self.bank_on and self.asset != "SOL":
            coin = self.local.bank["coin"]
            atoms = int(plan.margin / self.coin_px(st) * 10 ** bankmod.DECIMALS[coin])
            nat, mint = atoms / 10 ** bankmod.DECIMALS[coin], bankmod.MINT[coin]
        else:
            atoms = int(sol * LAMPORTS)
            nat, mint = atoms / LAMPORTS, bankmod.MINT["SOL"]
        exact = getattr(plan, "source", "") == "rearm" and self.cfg.resting.readd_gap_atr > 0   # 2026-10-27: no front-run creep
        args = {"side": side, "inputMint": mint, "inputAtoms": str(atoms), "sizeUsd": round(plan.margin * plan.leverage, 2),
                "triggerPriceUsd": round(plan.trigger, 2) if exact else self.add_px(pos, plan.trigger)}
        return args, nat, sol

    def place_rest_add(self, pos: ChainPosition, st: dict[str, Any], price: float, account: Any, equity: float) -> None:
        L = self.local
        mine = {p.get("pubkey") for p in L.partials}
        untracked = [r for r in st["openRequests"] if r.get("requestChange") == "increase" and r.get("pubkey") not in mine]
        if untracked:
            self._rest_note("add", f"an increase request is already on-chain {[r.get('pubkey') for r in untracked]} "
                                   "(not tracked by this bot): no new resting add placed", logging.WARNING)
            return
        plan, why = self.rest_add_plan(pos, account.cash, price, st=st)
        if plan is None:
            self._rest_note("add", f"no resting add placed: {why}")
            return
        self.ib_cancel_request("next rung affordable without a loan")   # 2026-10-25: an own stale request is withdrawn
        if self.cfg.dca.add_frontrun_atr > 0 and self.front_atr() is None:   # 2026-10-24: never placed at the bare level
            self._rest_note("add", "no resting add placed this tick: no ATR yet for the front-run (own 1h ATR unset, no "
                                   "analyst ATR); retry next tick", logging.WARNING)
            return
        if self.add_pending_wait(st, pos):
            return
        it = Intent(Action.ADD, pos.side, plan.trigger, plan.margin, plan.leverage)
        why = self.guard(it, pos, st, equity)
        if why:
            if why == "would breach SOL fee reserve":
                self._reserve_loan(pos, st, account.cash, plan)
            self._rest_note("add", f"resting add skipped: {why}")
            return
        args, nat, sol = self.rest_add_args(pos, st, plan)
        res = self.cmd("create-limit-order", args)
        if self.live.dry_run:
            self._rest_note("add", f"DRY RUN resting add {args} -> ok {res.get('ok')} {res.get('error') or ''}")
            return
        if res.get("sent") and res.get("sig") and res.get("positionRequest"):
            L.rest_add = {"pubkey": res["positionRequest"], "side": args["side"], "trigger": args["triggerPriceUsd"],
                          "margin": round(plan.margin, 4), "lev": plan.leverage, "size_usd": args["sizeUsd"], "nat": nat,
                          "sol": round(sol, 9), "placed": str(pd.Timestamp.now(tz="UTC")), "sig": res.get("sig")}
            if res.get("ok"):
                self._rest_bank_debit(L.rest_add)
            else:   # sent but not confirmed: the next ticks decide whether it landed
                L.rest_add["unconfirmed"] = True
            self.save()
            self.__dict__.setdefault("_rest_notes", {}).pop("add", None)
            log.warning("RESTING ADD placed: %s $%.2f coll x%g = $%.2f at %.4f (req %s, sig %s%s)", args["side"], plan.margin,
                        plan.leverage, args["sizeUsd"], args["triggerPriceUsd"], res["positionRequest"], res.get("sig"),
                        "" if res.get("ok") else ", NOT YET CONFIRMED")
            return
        self._rest_note("add", f"resting add creation refused / failed (retry next tick): "
                               f"{res.get('code') or ''} {res.get('error') or res.get('err')} args {args}", logging.WARNING)

    def breakeven_price(self, pos: ChainPosition, st: dict[str, Any]) -> float | None:
        """Price at which the WHOLE position nets zero: hwm_equity (own-coin bank incl. banked partial payouts, fee debt, close
        fee / slippage) == trade_start_equity (the guard target without its min-profit margin). Bisection on the existing
        hwm_equity (monotone in price). None when the bank has no baseline or no root in [1%, 3x] of market."""
        b = self.local.bank
        if not self.bank_on or not b or b.get("trade_start_equity") is None:
            return None
        tgt, price = float(b["trade_start_equity"]), float(st["oracle"]["price"])
        f = lambda p: self.hwm_equity(pos, st, p) - tgt   # noqa: E731
        lo, hi = price * 0.01, price * 3.0
        flo, fhi = f(lo), f(hi)
        if flo == 0 or flo * fhi > 0:
            return None
        for _ in range(80):
            mid = (lo + hi) / 2
            fm = f(mid)
            if (fm > 0) == (flo > 0):
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def tp3_ref_avg(self, pos: ChainPosition) -> float:
        """TP3 reference (Trevor 2026-10-16): the position's TRUE breakeven price (whole position nets zero incl. banked partial
        payouts and fees; breakeven_price), NOT Jupiter's reported avg. Falls back to the bot's lot-weighted avg entry, then
        Jupiter's avg, when the bank has no baseline."""
        st = getattr(self, "_tp3_st", None)
        be = None
        if st is not None:
            try:
                be = self.breakeven_price(pos, st)
            except Exception as e:   # never let the reference break the tick
                log.warning("TP3 breakeven failed (%s); falling back to lot avg", e)
        self._tp3_ref = (be, "breakeven") if be else (restmod.lots_avg(self.local.lots) or pos.avg_entry, "lot avg")
        return self._tp3_ref[0]

    def partial_plan(self, pos: ChainPosition) -> dict[str, Any] | None:
        """The ONE TP3 (Trevor 2026-10-15): restmod.tp3_plan over the open add lots (rung = their count; a closed lot leaves
        L.lots, so the rung drops), size capped at the position."""
        R, t, c = self.cfg.resting, self.cfg.trim, self.cfg.costs
        adds = [l for i, l in enumerate(self.local.lots) if not (l.get("first") or i == 0)]
        tgt = restmod.tp3_plan(pos.side, self.tp3_ref_avg(pos), adds, self.cfg.dca.ladder_max_rungs, R.partial_zone_f_first,
                               R.partial_zone_f_deep, open_fee=c.open_fee, close_fee=c.close_fee, network_usd=t.trim_network_usd,
                               min_net_pct=R.partial_min_net_pct)
        if tgt is not None:
            tgt["size"] = round(min(tgt["size"], pos.size), 2)
        return tgt

    def _place_partial(self, pos: ChainPosition, price: float, tgt: dict[str, Any], why: str) -> bool:
        L = self.local
        if pos.side * (tgt["px"] - price) < self.cfg.tp.tp_market_gap - 1e-9:
            self._rest_note("partial", f"TP3 target {tgt['px']} not beyond market {price:.4f}; retry")
            return False
        side = "long" if pos.side == LONG else "short"
        args = {"side": side, "sizeUsd": tgt["size"], "triggerPriceUsd": tgt["px"], "triggerAbove": pos.side == LONG}
        res = self.cmd("decrease", args)
        if self.live.dry_run:
            self._rest_note("partial", f"DRY RUN partial {args} ({why}; f {tgt['f']}, rung {tgt['rung']})")
            return False
        if not res.get("ok"):
            self._rest_note("partial", f"partial placement failed (retry next tick): {res.get('err') or res.get('error')}",
                            logging.WARNING)
            return False
        L.partials = [{"pubkey": res.get("positionRequest"), "lots": tgt["lots"], "lot_time": tgt["lots"][-1],
                       "size": args["sizeUsd"], "px": tgt["px"], "level": None, "n": 0, "rung": tgt["rung"],
                       "entry": tgt["entry"], "placed": str(pd.Timestamp.now(tz="UTC")), "sig": res.get("sig"), "why": why}]
        self.save()
        self.__dict__.setdefault("_rest_notes", {}).pop("partial", None)
        log.warning("TP3 reference %s = %.4f", getattr(self, "_tp3_ref", (0, "?"))[1], tgt["avg"])
        log.warning("PARTIAL TP (TP3) placed (%s): adds %s = $%.2f, entry %.4f avg %.4f furthest add %.4f (rung %d, f %.3f) -> "
                    "trigger %.4f (net est $%.4f), req %s", why, tgt["lots"], tgt["size"], tgt["entry"], tgt["avg"], tgt["far"],
                    tgt["rung"], tgt["f"], tgt["px"], tgt["net_usd"], res.get("positionRequest"))
        return True

    def partial_sync(self, pos: ChainPosition, price: float) -> None:
        """Keep the single TP3 equal to partial_plan (every tick; right after an add): placed when missing, cancelled +
        re-placed when its size / price differ (new add, rung or avg change, position shrank), cancelled when no add nets a
        profit any more. A changed target not beyond the market leaves the old order untouched (retry next tick)."""
        L = self.local
        if not self.cfg.resting.partials_enabled:
            return
        if any(p.get("booked") for p in L.partials):   # 2026-10-28: a booked TP3's stale record is still on-chain:
            self._rest_note("partial", "TP3 booked from its size drop; waiting for its stale order record to disappear")
            return   # nothing cancelled / re-placed until it is gone (resting_reconcile removes it quietly)
        tgt, old = self.partial_plan(pos), (L.partials[0] if L.partials else None)
        if tgt is None:
            R, t, c = self.cfg.resting, self.cfg.trim, self.cfg.costs
            adds = [l for i, l in enumerate(L.lots) if not (l.get("first") or i == 0)]
            if adds:
                self._rest_note("tp3_skip", "TP3 skipped: " + restmod.tp3_skip_why(
                    pos.side, self.tp3_ref_avg(pos), adds, self.cfg.dca.ladder_max_rungs, R.partial_zone_f_first, R.partial_zone_f_deep,
                    open_fee=c.open_fee, close_fee=c.close_fee, network_usd=t.trim_network_usd,
                    min_net_pct=R.partial_min_net_pct), logging.WARNING)
        else:
            self.__dict__.setdefault("_rest_notes", {}).pop("tp3_skip", None)
        if old and old.get("manual") and tgt and abs(float(old["size"]) - tgt["size"]) < 0.01:   # 2026-10-29: TP3_SET
            return   # price held until the next add / resize changes the TP3 size, then normal planning resumes
        if old and tgt and abs(float(old["size"]) - tgt["size"]) < 0.01 and abs(float(old["px"]) - tgt["px"]) < max(0.005, 0.0003 * price):   # hysteresis: breakeven drifts with the coin / SOL price
            return
        if tgt and pos.side * (tgt["px"] - price) < self.cfg.tp.tp_market_gap - 1e-9:
            self._rest_note("partial", f"TP3 target {tgt['px']} not beyond market {price:.4f}; retry")
            return
        if old:
            c = self.cancel_request(old["pubkey"], pos.size)
            if c != "ok":
                log.warning("TP3 update: cancel of %s returned %s; not replaced (next tick reconciles)", old["pubkey"], c)
                return
            L.partials = []
            self.save()
        if tgt:
            self._place_partial(pos, price, tgt, "re-planned" if old else "placed")

    def resting_status(self, pos: ChainPosition) -> str:
        L, R = self.local, self.cfg.resting
        ra = L.rest_add
        return (f"resting: adds {'on' if R.adds_enabled else 'off'} partials {'on' if R.partials_enabled else 'off'} "
                f"last-partial rule {'on' if R.last_partial_rule else 'off'} | add order "
                + (f"{ra['side']} ${ra['size_usd']} @ {ra['trigger']} ({ra['pubkey']})" if ra else "none")
                + f" | partial orders {[(p['px'], p['size']) for p in L.partials]} | last partial price {L.last_partial_px}")

    # ------------------------------------------------------------- per-bot own-coin ledger + HWM (live/bank.py)
    @property
    def bank_on(self) -> bool:
        return bool(self.cfg.ledger.enabled and self.local.bank)

    def coin_px(self, st: dict[str, Any]) -> float:
        """USD price of this bot's own coin: SOL oracle (SOL bot), market oracle (ETH ~ Wormhole ETH, BTC ~ WBTC)."""
        return self.sol_price(st) if self.asset == "SOL" else float(st["oracle"]["price"])

    def _coin_px_now(self) -> float:
        return self._sol_px if self.asset == "SOL" else (self._mkt_px or 0.0)

    def bank_fee(self, fee_lamports: Any) -> None:
        """ETH / BTC bots: their SOL network fees become a debt against their own coin (settled daily)."""
        if self.bank_on and self.asset != "SOL" and fee_lamports and not self.live.dry_run:
            b = self.local.bank
            f = int(fee_lamports) / LAMPORTS
            b["fee_sol"] = round(b.get("fee_sol", 0.0) + f, 9)
            b["totals"]["fees_sol"] = round(b["totals"].get("fees_sol", 0.0) + f, 9)

    def bank_payout(self, usd: float, why: str) -> None:
        """A payout event (TP fill, trim, close, position gone). SOL bot: credit the estimate (collateral returned + PnL -
        close fee at the SOL oracle; trued up on flat). ETH / BTC: open the payout window; the real own-token change
        (or the USDC -> coin sweep) is booked by the reconciliation."""
        if not self.bank_on or self.live.dry_run:
            return
        b = self.local.bank
        b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
        if self.asset == "SOL" and self._sol_px:
            amt = max(usd, 0.0) / self._sol_px
            bankmod.credit(b, amt, "payout_est", f"{why}: ${usd:.4f} at SOL {self._sol_px:.3f}")
            log.info("BANK payout (est) %s: %+.9f SOL (bal %.9f)", why, amt, b["bal"])
            self.local.pay_check = True   # 2026-10-27: replaced by the real on-chain amount (sol_pay_trueup) while open
        else:
            b["pay_wait"] = self.GUARD_DEFER_MAX   # guard_tps: no guard-price TP re-place until the credit lands
            log.info("BANK payout event %s (~$%.4f): window %d ticks for the own-coin change", why, usd, b["window"])
        self.save()

    @staticmethod
    def _open_sig() -> str | None:
        """Open tx sig of the trade in the ledger's open record (identifies the trade a true-up block belongs to)."""
        try:
            return json.loads(ledger.OPEN.read_text()).get("open_sig") if ledger.OPEN.exists() else None
        except Exception:  # noqa: BLE001 - unreadable record: no id (the block then needs a manual check)
            return None

    def _sol_trueup(self, flows: float, why: str) -> float:
        """SOL bot: book the difference between the position's own on-chain SOL flows and what the bank booked for this
        trade (estimates). Always applied, whatever its size (2026-10-27, Trevor: the old 0.02 SOL skip let a +0.022548
        over-booking reach bal / HWM / the 10-27 withdrawal). A resting-add request still open (pending cancel) is in these
        flows as gone incl. its request rent -> marked rent_out so its refund credits the rent."""
        b = self.local.bank
        booked = round(b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0), 9)
        adj = round(flows - booked, 9)
        bankmod.credit(b, adj, "flat_trueup", f"{why}: position flows {flows:+.9f} vs booked {booked:+.9f}")
        if b.get("res_last") is not None:
            b["res_last"] = round(b["res_last"] - adj, 9)   # moved between reserve float and bal
        for ra in [self.local.rest_add] + [p.get("ra") for p in self.local.pending_cancels]:
            if ra and ra.get("debited"):
                ra["rent_out"] = True
        log.info("BANK flat true-up %+.9f SOL (position flows %+.9f, %s)", adj, flows, why)
        return adj

    def bank_flat(self, flows: float | None) -> None:
        """Trade over. SOL bot: true-up with the position's own on-chain SOL flows (exact). Reset the trade counters."""
        if not self.bank_on or self.live.dry_run:
            return
        b = self.local.bank
        if self.asset == "SOL":
            if flows is not None:
                self._sol_trueup(flows, "flat")
            else:   # 2026-10-27: no chain flows -> the true-up cannot be applied: block withdrawal + HWM until it is
                b["trueup_block"] = {"since": str(pd.Timestamp.now(tz="UTC")), "trade": self._open_sig(),
                                     "booked": round(b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0), 9)}
                log.error("BANK flat true-up NOT applied (position flows unavailable): SOL withdrawal and HWM blocked "
                          "until it is (retried when the trade row is booked)")
        if b.get("book_pend"):   # ETH / BTC: counters kept until the deferred own-coin booking (bank_book_pending)
            b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
            self.save()
            return
        log.info("BANK trade done: deposited %.9f, received %.9f %s (net %+.9f)", b.get("trade_dep", 0.0),
                 b.get("trade_recv", 0.0), b["coin"], b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0))
        b["trade_dep"], b["trade_recv"] = 0.0, 0.0
        b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
        self.save()

    def _busy(self, st: dict[str, Any], all_markets: bool) -> bool:
        reqs = st.get("allOpenRequests", st["openRequests"]) if all_markets else st["openRequests"]
        if [r for r in reqs if r.get("requestType") != "trigger"]:
            return True
        return False

    def bank_tick(self, st: dict[str, Any], pos: ChainPosition | None, price: float) -> None:
        """Reconcile this bot's balance with the wallet, settle external-event shares, raise the HWM when flat."""
        b, lc = self.local.bank, self.cfg.ledger
        self._jl_just_lent = False   # set by jupsol_loans._book_lend when a JupSOL loan is booked this tick
        if self.live.dry_run:
            return
        sol_px, cpx = self.sol_price(st), self.coin_px(st)
        mid_dep = self.deposit_tick(st)   # USDC deposit share (live/deposit.py): True = own leg in flight / just credited
        mid_loan = self.ib_tick(st, pos)
        # JupSOL loans (live/jupsol_loans.py): rescue / next-add loan / park request / journalled op; True = wallet moved
        mid_jl = False if (mid_loan or mid_dep) else jlmod.tick(self, st, pos, price)
        if mid_loan or mid_dep or mid_jl:
            pass
        elif self.asset == "SOL":
            j = bankmod.load_journal()
            r = bankmod.recon_sol(b, float(st["wallet"]["sol"]), bankmod.pending_others(j), sol_px, lc.ext_min_usd,
                                  self._busy(st, True), bankmod.rent_held(st))
            if r["action"] == "external" and lc.ext_flow_mode == "alert":
                b["res_last"] = r["implied"]   # new reserve baseline so it alerts once; bal / hwm untouched
                line = bankmod.write_alert("SOL", f"native SOL {r['diff']:+.9f} (~${r['diff'] * sol_px:+.2f}); banks "
                                           f"untouched (ext_flow_mode=alert); SOL bal {b['bal']:.9f} hwm {b['hwm']:.9f}")
                log.warning("BANK %s", line)
            elif r["action"] == "external":
                ev = bankmod.post_external(r["diff"], sol_px, tuple(lc.ext_split),
                                           f"unexplained native SOL change {r['diff']:+.9f} (~${r['diff'] * sol_px:+.2f})")
                bankmod.apply_external(b, ev["shares"]["SOL"], f"ext #{ev['id']}: SOL share of {r['diff']:+.9f}")
                log.warning("BANK EXTERNAL %s of %+.9f SOL (~$%+.2f) detected (not profit/loss): event #%d split %s; "
                            "SOL bot bal %.9f hwm %.9f", "deposit" if r["diff"] > 0 else "withdrawal", r["diff"],
                            r["diff"] * sol_px, ev["id"], ev["shares"], b["bal"], b["hwm"])
            elif r["action"] == "pending":
                log.info("BANK: unexplained native SOL change %+.9f (~$%+.2f); re-checking next tick", r["diff"], r["diff"] * sol_px)
        else:
            coin = b["coin"]
            atoms = int(((st.get("sweepTokens") or {}).get(coin) or {}).get("atoms") or 0)
            if "error" not in (st.get("sweepTokens") or {}):
                r = bankmod.recon_token(b, atoms / 10 ** bankmod.DECIMALS[coin], cpx, lc.ext_min_usd, self._busy(st, False),
                                        lc.ext_flow_mode)
                if r["action"] in ("payout", "adjust", "external"):
                    b["pay_wait"] = 0
                if r["action"] in ("payout", "adjust"):
                    log.info("BANK %s %+.9f %s (bal %.9f)", r["action"], r["diff"], coin, b["bal"])
                elif r["action"] == "alert":
                    line = bankmod.write_alert(self.asset, f"{coin} {r['diff']:+.9f} (~${r['diff'] * cpx:+.2f}); bank "
                                               f"untouched (ext_flow_mode=alert); bal {b['bal']:.9f} hwm {b['hwm']:.9f}")
                    log.warning("BANK %s", line)
                elif r["action"] == "external":
                    log.warning("BANK EXTERNAL %s change %+.9f (~$%+.2f) -> this bot's capital + hwm (not profit)", coin,
                                r["diff"], r["diff"] * cpx)
                elif r["action"] == "pending":
                    log.info("BANK: unexplained %s change %+.9f; re-checking next tick", coin, r["diff"])
            if not self._busy(st, False) and lc.ext_flow_mode != "alert":   # alert mode: never swap for ext events
                self.bank_settle_external(st, sol_px, cpx)
        if not mid_dep:   # deposit in flight / credited this tick: the reconcile was held, so the payout window is too
            if b.get("window", 0) > 0:
                b["window"] -= 1
            if b.get("pay_wait", 0) > 0:
                b["pay_wait"] = 0 if self.asset == "SOL" or pos is None else b["pay_wait"] - 1
        flat = pos is None and b.get("window", 0) == 0 and not self._busy(st, self.asset == "SOL")
        if pos is None and b.get("window", 0) == 0 and not self._busy(st, False) and b.get("book_pend") and not b.get("pend"):
            self.bank_book_pending(sol_px, cpx)
        if flat and b.get("ib_loan", {}).get("role") == "borrower" and not b.get("pend") and not b.get("book_pend"):
            try:
                self.ib_borrower_repay(sol_px, cpx)
            except Exception as e:  # noqa: BLE001 - retried next tick
                log.error("INTER-BOT LOAN repay failed (retry next tick): %s", e)
        # 2026-10-27: JupSOL loan repaid AFTER the peer (exact JupSOL quantity; not while a reconcile / own-coin booking /
        # SOL true-up is pending or a payout window is open; a carried rest waits for the next flat)
        if flat and not mid_jl and jlmod.repay_due_now(self):
            try:
                jlmod.repay_tick(self, sol_px, cpx)
            except Exception as e:   # noqa: BLE001 - retried next tick
                log.error("JUPSOL repay failed (retry next tick): %s", e)
        if not b.get("trueup_block") and bankmod.update_hwm(b, flat, bankmod.fee_debt_native(b, sol_px, cpx)):
            log.info("BANK HWM raised (flat): %s", bankmod.status_line(b, cpx, sol_px))
        self.save()

    def deposit_tick(self, st: dict[str, Any]) -> bool:
        """USDC deposit fold-in (Trevor 2026-10-26, live/deposit.py): credit this bot's own landed share (the measured
        on-chain amount of its own coin) to its own bank as capital: bal, hwm and wd_mark all rise by it
        (bank.apply_external; never trade profit, no ext-flow alert). Applied atomically on this bot's own tick, keyed by
        the leg's sig in bank["deposits"] (state.json saved first, then the journal marked). Deferred while an order
        request is in flight. The per-trade guard baseline and the daily-loss baseline move by the same amount (capital,
        not P&L). True = own leg in flight, landed-uncredited or credited this tick: hold the wallet reconcile."""
        b = self.local.bank
        try:
            w = depositmod.own_work(self.asset, b)
        except Exception as e:  # noqa: BLE001 - unreadable journal: never guess, hold the reconcile
            log.error("DEPOSIT journal unreadable (%s): %s wallet reconcile held this tick", e, self.asset)
            return True
        for rid, crec in w["mark"]:   # credited in state.json, journal mark lost (crash): mark only, never re-credit
            depositmod.mark_credited(rid, self.asset, crec)
            log.info("deposit #%d: %s credit already in the bank (%s); journal marked", rid, self.asset, crec.get("sig"))
        if w["credit"] and self._busy(st, self.asset == "SOL"):
            log.info("deposit: %s credit deferred (an order request is in flight); next tick", self.asset)
            return True
        cpx = self.coin_px(st)
        for rid, leg in w["credit"]:
            got = float(leg["got"])
            at = str(pd.Timestamp.now(tz="UTC"))
            before = {k: b[k] for k in ("bal", "hwm", "wd_mark")}
            # one in-memory step (nothing here can raise), then state.json, then the journal mark
            bankmod.apply_external(b, got, f"USDC deposit #{rid}: {leg['atoms'] / depositmod.USDC_ATOMS:.6f} USDC -> "
                                           f"{got:.9f} {b['coin']} (tx {leg['sig']})")
            if b.get("trade_start_equity") is not None:   # per-trade guard baseline: capital in, not trade profit
                b["trade_start_equity"] = round(b["trade_start_equity"] + got, 9)
            crec = {"id": rid, "sig": leg["sig"], "coin": b["coin"], "got": got,
                    "usdc": leg["atoms"] / depositmod.USDC_ATOMS, "coin_px": cpx, "before": before,
                    "after": {k: b[k] for k in ("bal", "hwm", "wd_mark")}, "at": at}
            b["deposits"] = (b.get("deposits") or [])[-19:] + [crec]
            self.local.day_start_equity += got * cpx      # daily-loss baseline: a deposit is not a gain
            self.save()
            log.warning("BANK DEPOSIT credited #%d: +%.9f %s (~$%.2f, from %.6f USDC, tx %s) as capital, not profit: bal "
                        "%.9f -> %.9f, hwm %.9f -> %.9f, wd_mark %.9f -> %.9f", rid, got, b["coin"], got * cpx,
                        crec["usdc"], leg["sig"], before["bal"], b["bal"], before["hwm"], b["hwm"], before["wd_mark"],
                        b["wd_mark"])
            if self.asset == "SOL" and leg.get("net_sol"):
                try:
                    ledger.update(external_out_sol=-float(leg["net_sol"]))   # open trade only: not trade P&L
                except Exception as e:  # noqa: BLE001 - trades.csv wallet-delta fallback note only
                    log.warning("deposit #%d: open-trade ledger note failed: %s", rid, e)
            depositmod.mark_credited(rid, self.asset, crec)
        if w["mark"] or w["credit"] or w["ledger"]:
            try:
                depositmod.ledger_done()
            except Exception as e:  # noqa: BLE001 - retried next tick (once per id)
                log.error("DEPOSIT ledger write failed (retried next tick): %s", e)
        return bool(w["inflight"] or w["credit"])

    def _ib_file(self) -> Path:
        return ROOT / bankmod.IB_FILE

    def _ib_read(self) -> dict[str, Any]:
        f = self._ib_file()
        try:
            return json.loads(f.read_text()) if f.exists() else {}
        except ValueError:
            return {}

    def _ib_write(self, rec: dict[str, Any]) -> None:
        self._ib_file().write_text(json.dumps(rec, indent=1))

    def _swap_got(self, res: dict[str, Any], coin: str) -> float:
        d = res.get("deltas") or {}
        if coin == "SOL":
            return sweepmod.swap_sol_change(d)[0] if d.get("nativeDeltaLamports") is not None else \
                int(res.get("quotedOut") or 0) / LAMPORTS
        return int((d.get("tokenDeltaAtoms") or {}).get(coin) or res.get("quotedOut") or 0) / 10 ** bankmod.DECIMALS[coin]

    def _coin_usd(self, coin: str, st: dict[str, Any] | None = None) -> float:
        if coin == "SOL":
            return self._sol_px or self.sol_price(st or self.chain())
        if coin == self.local.bank.get("coin"):
            return self._mkt_px or float((st or self.chain())["oracle"]["price"])
        mkt = {"ETH": "ETH", "WBTC": "BTC"}[coin]
        return float(jup("state", {"market": mkt})["oracle"]["price"])

    def ib_tick(self, st: dict[str, Any], pos: ChainPosition | None) -> bool:
        """Inter-bot loans (Trevor 2026-10-12 18:09 / 18:13 MT), one at a time via live/ib_loan.json. Runs first in
        bank_tick. Borrower: books a funded loan. Lender: settles a repaid loan. Flat bot without a loan: funds a
        request. True = this bot's wallet is mid-loan-transfer (skip the wallet reconcile this tick). Never raises."""
        try:
            b = self.local.bank
            me, coin = self.asset, b["coin"]
            with bankmod.journal_lock():
                rec = self._ib_read()
                s = rec.get("status")
                mine = me in (rec.get("borrower"), rec.get("lender"))
                if s == "funding" and rec.get("topup") and mine and self._ib_age(rec, "funding_at") > bankmod.IB_TOPUP_TIMEOUT_S:
                    # stuck funding (crash mid-swap): marks / banks untouched, record back to open, wallet diff gets alerted
                    self._ib_topup_revert(rec, "stuck funding")
                    bankmod.write_alert(me, f"inter-bot top-up funding stuck > {bankmod.IB_TOPUP_TIMEOUT_S // 60} min and reverted; "
                                            f"check the lender wallet vs bank (a swap may have landed)")
                    return False
                if s == "funding" and rec.get("borrower") == me:
                    return True
                if s == "topup_requested" and rec.get("borrower") == me and \
                        (pos is None or self._ib_age(rec, "topup_requested") > bankmod.IB_TOPUP_TIMEOUT_S):
                    self._ib_topup_revert(rec, "borrower flat" if pos is None else "timeout")   # then repay proceeds if flat
                    return False
                if s == "requested" and rec.get("borrower") == me and pos is None:   # 2026-10-25: flat -> no longer needed
                    self._ib_write({"status": "settled", "note": f"request by {me} cancelled (borrower flat)",
                                    "cancelled": str(pd.Timestamp.now(tz="UTC")), "cancelled_need_usd": rec.get("need_usd")})
                    log.error("INTER-BOT LOAN request by %s cancelled (borrower flat; was $%s)", me, rec.get("need_usd"))
                    return False
                my_ln = b.get("ib_loan") or {}
                if s == "funded" and rec.get("borrower") == me and rec.get("topup") and my_ln.get("role") == "borrower":
                    got, spent = float(rec["topup_got"]), float(rec["topup_spent"])   # delta only; marks untouched
                    b["bal"] = round(b["bal"] + got, 9)
                    my_ln["sol"] = round(float(my_ln["sol"]) + spent, 9)
                    my_ln["coin"] = round(float(my_ln["coin"]) + got, 9)
                    bankmod.note(b, "ib_borrow", got, f"top-up from {rec.get('lender')}: +{spent} {rec.get('lender_coin')} "
                                                      f"-> +{got} {coin} (total lent {my_ln['sol']}) sig {rec.get('topup_sig')}")
                    for k in ("topup", "topup_requested", "topup_prev", "funding_at", "topup_spent", "topup_got", "topup_sig"):
                        rec.pop(k, None)
                    rec["status"] = "open"
                    self._ib_write(rec)
                    self.save()
                    log.error("INTER-BOT LOAN top-up received from %s: +%.9f %s (payable total lent %s; HWM unchanged)",
                              rec.get("lender"), got, coin, my_ln["sol"])
                    return False
                if s == "funded" and rec.get("borrower") == me and not b.get("ib_loan"):
                    got = float(rec["coin"])
                    b["bal"] = round(b["bal"] + got, 9)
                    b["ib_loan"] = {k: rec.get(k) for k in ("sol", "coin", "lender_coin", "sig", "opened")} | \
                        {"role": "borrower", "peer": rec.get("lender")}
                    bankmod.note(b, "ib_borrow", got, f"loan from {rec.get('lender')}: {rec['sol']} {rec.get('lender_coin')} "
                                                      f"-> {got} {coin} sig {rec.get('sig')}")
                    rec["status"] = "open"
                    self._ib_write(rec)
                    self.save()
                    log.error("INTER-BOT LOAN received from %s: +%.9f %s (payable; HWM unchanged)", rec.get("lender"), got, coin)
                    return False
                if s == "repaid" and rec.get("lender") == me and (b.get("ib_loan") or {}).get("role") == "lender":
                    got = float(rec["repaid_got"])
                    ln = b.pop("ib_loan")
                    b["bal"] = round(b["bal"] + got, 9)
                    bankmod.note(b, "ib_repaid", got, f"loan repaid by {rec.get('borrower')}: {got:.9f} {coin} "
                                                      f"(lent {ln['sol']}) sig {rec.get('repay_sig')}")
                    b["ib_loans_done"] = (b.get("ib_loans_done") or [])[-9:] + [{**ln, "repaid_got": got}]
                    rec["status"] = "settled"
                    self._ib_write(rec)
                    self.save()
                    log.error("INTER-BOT LOAN settled: +%.9f %s back from %s; opens re-enabled", got, coin, rec.get("borrower"))
                    return False
                topup = s == "topup_requested" and rec.get("lender") == me and my_ln.get("role") == "lender" \
                    and my_ln.get("peer") == rec.get("borrower")   # same lender / borrower pair only
                if (topup or (s == "requested" and rec.get("borrower") != me and not b.get("ib_loan"))) \
                        and pos is None and not self._busy(st, me == "SOL") and b.get("window", 0) == 0:
                    px = self.coin_px(st)
                    need = float(rec["need_usd"]) / px
                    if (b["bal"] - bankmod.loan_outstanding(b)) * px < float(rec["need_usd"]) + 5.0:
                        return False
                    rec.update(status="funding", lender=me, lender_coin=coin)
                    if topup:
                        rec.update(funding_at=str(pd.Timestamp.now(tz="UTC")))   # topup_prev was saved by the borrower's request
                    self._ib_write(rec)
                    res, kind, att = sweepmod.swap_with_backoff(
                        self.cfg, {"mint": bankmod.MINT[coin], "atoms": int(need * 10 ** bankmod.DECIMALS[coin])}, jup,
                        out_mint=bankmod.MINT[rec["coin_name"]])
                    self.bank_fee((res.get("deltas") or {}).get("feeLamports") or res.get("feeLamports"))
                    if kind != "ok":
                        if topup:
                            prev = rec.get("topup_prev") or {}   # sol / coin as before the attempt; need_usd stays for the retry
                            rec.update(sol=prev.get("sol", rec.get("sol")), coin=prev.get("coin", rec.get("coin")), status="topup_requested")
                            rec.pop("funding_at", None)
                        else:
                            rec.update(status="requested", lender=None, lender_coin=None)
                        self._ib_write(rec)
                        log.error("INTER-BOT LOAN funding swap failed (%s): %s; request left open", kind, att)
                        return False
                    spent = int(need * 10 ** bankmod.DECIMALS[coin]) / 10 ** bankmod.DECIMALS[coin]
                    got = self._swap_got(res, rec["coin_name"])
                    now = str(pd.Timestamp.now(tz="UTC"))
                    if topup:   # cumulative lent quantity in lender coin; lender equity unchanged (bal down, ib_adj up)
                        b["bal"] = round(b["bal"] - spent, 9)
                        my_ln["sol"] = round(float(my_ln["sol"]) + spent, 9)
                        my_ln["coin"] = round(float(my_ln["coin"]) + got, 9)
                        bankmod.note(b, "ib_lend", -spent, f"top-up to {rec['borrower']}: {spent} {coin} -> {got} "
                                                           f"{rec['coin_name']} (total lent {my_ln['sol']}) sig {res.get('sig')}")
                        rec.update(status="funded", sol=my_ln["sol"], coin=my_ln["coin"], topup_spent=spent, topup_got=got,
                                   topup_sig=res.get("sig"))
                        self._ib_write(rec)
                        self.save()
                        log.error("INTER-BOT LOAN top-up funded for %s: %.9f %s -> %.9f %s (total lent %s) sig %s",
                                  rec["borrower"], spent, coin, got, rec["coin_name"], my_ln["sol"], res.get("sig"))
                        return False
                    b["bal"] = round(b["bal"] - spent, 9)
                    b["ib_loan"] = {"role": "lender", "peer": rec["borrower"], "sol": spent, "coin": got,
                                    "lender_coin": coin, "sig": res.get("sig"), "opened": now}
                    bankmod.note(b, "ib_lend", -spent, f"loan to {rec['borrower']}: {spent} {coin} -> {got} "
                                                       f"{rec['coin_name']} sig {res.get('sig')}")
                    rec.update(status="funded", sol=spent, coin=got, sig=res.get("sig"), opened=now)
                    self._ib_write(rec)
                    self.save()
                    log.error("INTER-BOT LOAN funded for %s: %.9f %s -> %.9f %s sig %s (this bot's opens disabled until "
                              "repaid)", rec["borrower"], spent, coin, got, rec["coin_name"], res.get("sig"))
                    return False
        except Exception as e:  # noqa: BLE001 - loans never stop the bot
            log.error("INTER-BOT LOAN tick error (retry next tick): %s", e)
        return False

    def _ib_age(self, rec: dict[str, Any], key: str) -> float:
        """Seconds since rec[key] (0 when missing / unparsable, so nothing is cancelled on bad data)."""
        try:
            return (pd.Timestamp.now(tz="UTC") - pd.Timestamp(rec[key])).total_seconds()
        except Exception:  # noqa: BLE001
            return 0.0

    def _ib_topup_revert(self, rec: dict[str, Any], why: str) -> None:
        """Pending / stuck top-up -> back to the plain open loan (sol / coin / need_usd restored from topup_prev)."""
        rec.update(rec.get("topup_prev") or {})
        for k in ("topup", "topup_requested", "topup_prev", "funding_at", "topup_spent", "topup_got", "topup_sig"):
            rec.pop(k, None)
        rec["status"] = "open"
        self._ib_write(rec)
        log.error("INTER-BOT LOAN top-up cancelled (%s); loan stays open", why)

    def ib_rung_usd(self) -> float:
        """2026-10-25: probe step for the next-rung search: max(dca.min_add_usd, dca.ladder_fixed_usd) ($10 live)."""
        d = self.cfg.dca
        return max(float(d.min_add_usd), float(getattr(d, "ladder_fixed_usd", 0.0) or 0.0))

    def ib_cancel_request(self, why: str) -> None:
        """2026-10-25: withdraw this bot's own outstanding request (status requested -> settled; a pending top-up ->
        back to open). Funding / funded / open records are never touched. Never raises."""
        try:
            with bankmod.journal_lock():
                rec = self._ib_read()
                if rec.get("borrower") != self.asset:
                    return
                if rec.get("status") == "requested":
                    self._ib_write({"status": "settled", "note": f"request by {self.asset} cancelled ({why})",
                                    "cancelled": str(pd.Timestamp.now(tz="UTC")), "cancelled_need_usd": rec.get("need_usd")})
                    log.error("INTER-BOT LOAN request by %s cancelled (%s; was $%s)", self.asset, why, rec.get("need_usd"))
                elif rec.get("status") == "topup_requested":
                    self._ib_topup_revert(rec, why)
        except Exception as e:  # noqa: BLE001
            log.error("INTER-BOT LOAN request cancel failed: %s", e)

    def ib_request(self, need_usd: float) -> None:
        """Borrower side: not enough free for the next rung -> ask a flat bot for ``need_usd`` (no cap; min $2). With an open
        loan of its own it asks its lender for a top-up of that loan instead (Trevor 2026-10-15)."""
        b = self.local.bank
        ln = b.get("ib_loan")
        if (ln and ln.get("role") != "borrower") or not self.bank_on or self.live.dry_run:
            return
        try:
            need_usd = max(bankmod.IB_MIN_USD, need_usd)
            now = str(pd.Timestamp.now(tz="UTC"))
            with bankmod.journal_lock():
                rec = self._ib_read()
                if ln:
                    if rec.get("status") != "open" or rec.get("borrower") != self.asset:
                        return
                    rec.update(status="topup_requested", topup=True, need_usd=round(need_usd, 2), topup_requested=now,
                               topup_prev={k: rec.get(k) for k in ("sol", "coin", "need_usd")})
                    self._ib_write(rec)
                else:
                    if rec.get("status") not in (None, "settled"):
                        return
                    self._ib_write({"status": "requested", "borrower": self.asset, "coin_name": b["coin"],
                                    "need_usd": round(need_usd, 2), "requested": now})
            log.error("INTER-BOT LOAN %s: %s needs $%.2f for its next rung", "top-up requested" if ln else "requested",
                      self.asset, need_usd)
        except Exception as e:  # noqa: BLE001
            log.error("INTER-BOT LOAN request failed: %s", e)

    def ib_borrower_repay(self, sol_px: float, cpx: float) -> None:
        """Flat after the trade -> Jupiter ExactOut swap of own coin for exactly the lender quantity still owed (swap cost
        on this bot), clear the payable and flag the repayment for the lender (live/ib_loan.json). Trevor 2026-10-13:
        the received amount is verified from the tx balance deltas; if it is still short the loan stays open with the
        remaining shortfall (ib_loan.repaid_got accumulates) and is topped up next tick before it is marked repaid."""
        b = self.local.bank
        ln = b["ib_loan"]
        with bankmod.journal_lock():
            rec = self._ib_read()
            if rec.get("status") != "open" or rec.get("borrower") != self.asset:
                return
            lc = ln.get("lender_coin") or "SOL"
            coin, ldec = b["coin"], bankmod.DECIMALS[lc]
            owed_atoms = int(round(float(ln["sol"]) * 10 ** ldec))
            out_atoms = owed_atoms - int(round(float(ln.get("repaid_got") or 0) * 10 ** ldec))
            if out_atoms > 0:
                res, kind, att = sweepmod.swap_with_backoff(self.cfg, {"mint": bankmod.MINT[coin], "atoms": out_atoms}, jup,
                                                            out_mint=bankmod.MINT[lc], swap_mode="ExactOut")
                fee = (res.get("deltas") or {}).get("feeLamports") or res.get("feeLamports")
                self.bank_fee(fee)
                if kind != "ok":
                    log.error("INTER-BOT LOAN repay swap failed (%s): %s; retry next tick", kind, att)
                    return
                spent, got = self._ib_repay_amounts(res, coin, lc, fee)
                b["bal"] = round(b["bal"] - spent, 9)
                ln["repaid_coin"] = round(float(ln.get("repaid_coin") or 0) + spent, 9)
                ln["repaid_got"] = round(float(ln.get("repaid_got") or 0) + got, 9)
                ln["repay_sigs"] = (ln.get("repay_sigs") or []) + [res.get("sig")]
                bankmod.note(b, "ib_repay", -spent, f"loan repay: {spent:.9f} {coin} -> {got:.9f} {lc} (ExactOut "
                                                    f"{out_atoms / 10 ** ldec:.9f}, lent {ln['sol']}) sig {res.get('sig')}")
                short = owed_atoms - int(round(ln["repaid_got"] * 10 ** ldec))
                if short > 0:
                    self.save()
                    log.error("INTER-BOT LOAN repay short by %.9f %s (got %.9f of %.9f); loan stays open, top-up next tick",
                              short / 10 ** ldec, lc, ln["repaid_got"], float(ln["sol"]))
                    return
            spent, got = float(ln.get("repaid_coin") or 0), float(ln.get("repaid_got") or 0)
            sigs = ln.get("repay_sigs") or []
            sig = sigs[-1] if sigs else None
            b["ib_loans_done"] = (b.get("ib_loans_done") or [])[-9:] + [{**ln, "sig_repay": sig}]
            b.pop("ib_loan", None)
            b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
            rec.update(status="repaid", repaid_got=round(got, 9), sol_got=round(got, 9), repay_sig=sig,
                       repaid=str(pd.Timestamp.now(tz="UTC")))
            self._ib_write(rec)
            self.save()
        log.error("INTER-BOT LOAN REPAID: %.9f %s -> %.9f %s to %s (lent %.9f) sig %s", spent, coin, got, lc,
                  ln.get("peer"), float(ln["sol"]), sig)

    def _ib_repay_amounts(self, res: dict[str, Any], coin: str, lc: str, fee: Any) -> tuple[float, float]:
        """(own coin spent, lender coin received) of a confirmed repay swap from its chain balance deltas (quote as
        fallback). SOL received excludes the network fee (that is booked on this bot via bank_fee): the swap output."""
        d = res.get("deltas") or {}
        if coin == "SOL":
            spent = -sweepmod.swap_sol_change(d)[0] if d.get("nativeDeltaLamports") is not None else \
                int(res.get("inAmount") or 0) / LAMPORTS
        else:
            ta = (d.get("tokenDeltaAtoms") or {}).get(coin)
            spent = (-int(ta) if ta is not None else int(res.get("inAmount") or 0)) / 10 ** bankmod.DECIMALS[coin]
        got = self._swap_got(res, lc)
        if lc == "SOL" and d.get("nativeDeltaLamports") is not None:
            got = round(got + int(fee or 0) / LAMPORTS, 9)
        return round(spent, 9), round(got, 9)

    def bank_settle_external(self, st: dict[str, Any], sol_px: float, cpx: float) -> None:
        """ETH / BTC: swap this bot's share of an external SOL deposit SOL -> own coin (withdrawal: coin -> SOL)."""
        b = self.local.bank
        if time.time() < float(b.get("ext_next_try") or 0):
            return
        j = bankmod.load_journal()
        coin, dec = b["coin"], bankmod.DECIMALS[b["coin"]]
        for ev_id, s in bankmod.open_shares(j, self.asset, 0.25 / sol_px):
            if s > 0:
                args = sweepmod.sweep_args(self.cfg, bankmod.MINT["SOL"], int(s * LAMPORTS), bankmod.MINT[coin])
            else:
                want = min(-s * sol_px / cpx, b["bal"])
                if want * cpx < 0.25:
                    log.warning("BANK ext #%d: withdrawal share %.9f SOL but own free balance %.9f %s; waiting", ev_id, s,
                                b["bal"], coin)
                    continue
                args = sweepmod.sweep_args(self.cfg, bankmod.MINT[coin], int(want * 10 ** dec), bankmod.MINT["SOL"])
            res, kind, att = sweepmod.swap_with_backoff(self.cfg, {"mint": args["inputMint"], "atoms": int(args["amountAtoms"])},
                                                        jup, out_mint=args["outputMint"])
            d = res.get("deltas") or {}
            self.bank_fee(d.get("feeLamports") or res.get("feeLamports"))
            if kind != "ok":
                b["ext_next_try"] = time.time() + 300
                log.warning("BANK ext #%d settle swap failed (%s): %s; retry in 5 min", ev_id, kind, att)
                self.save()
                return
            if s > 0:
                got = int((d.get("tokenDeltaAtoms") or {}).get(coin) or res.get("quotedOut") or 0) / 10 ** dec
                bankmod.apply_external(b, got, f"ext #{ev_id}: {s:.9f} SOL -> {got:.9f} {coin} sig {res.get('sig')}")
                bankmod.settle(self.asset, ev_id, s, f"swapped to {got:.9f} {coin} {res.get('sig')}")
            else:
                spent = int(args["amountAtoms"]) / 10 ** dec
                sol_got = (int(d.get("nativeDeltaLamports") or 0) + int(d.get("wsolDeltaAtoms") or 0)) / LAMPORTS \
                    if d.get("nativeDeltaLamports") is not None else int(res.get("quotedOut") or 0) / LAMPORTS
                bankmod.apply_external(b, -spent, f"ext #{ev_id}: {spent:.9f} {coin} -> {sol_got:.9f} SOL sig {res.get('sig')}")
                bankmod.settle(self.asset, ev_id, -max(sol_got, 0.0), f"{spent:.9f} {coin} swapped {res.get('sig')}")
            b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
            log.warning("BANK ext #%d settled for %s: %s", ev_id, self.asset, bankmod.status_line(b, cpx, sol_px))
            self.save()

    def bank_daily(self, st: dict[str, Any], pos: ChainPosition | None) -> None:
        """Once a day (ledger.withdraw_time_mt): withdraw_fraction x HWM gain since the last withdrawal -> JupSOL
        (SOL: stake; ETH / WBTC: direct coin -> JupSOL swap). Reduces bal and hwm. ETH / BTC also settle their SOL fee
        debt when >= fee_settle_min_sol. Logs every bot's balance in native + USD."""
        from datetime import datetime, timezone
        b, lc = self.local.bank, self.cfg.ledger
        now = datetime.now(timezone.utc)
        if not bankmod.due(now, lc.withdraw_time_mt, b.get("wd_last_date")):
            return
        if self.live.dry_run or self._busy(st, self.asset == "SOL") or b.get("window", 0) > 0:
            return   # retried next tick
        if b.get("trueup_block"):   # 2026-10-27: a confirmed correction is not applied yet -> no withdrawal (re-checked each tick)
            day = str(now.astimezone(bankmod.MT).date())
            if self.__dict__.get("_wd_block_day") != day:
                self._wd_block_day = day
                log.error("BANK daily withdrawal %s BLOCKED: true-up not applied since %s (booked %+.9f); bal %.9f hwm %.9f "
                          "wd_mark %.9f kept", self.asset, b["trueup_block"].get("since"), b["trueup_block"].get("booked", 0.0),
                          b["bal"], b["hwm"], b["wd_mark"])
            return
        sol_px, cpx = self.sol_price(st), self.coin_px(st)
        coin, dec = b["coin"], bankmod.DECIMALS[b["coin"]]
        amt, gain = bankmod.withdraw_amount(b, lc.withdraw_fraction, bankmod.withdraw_min_native(lc.withdraw_cost_pct, lc.withdraw_network_sol, lc.withdraw_fee_factor, sol_px, cpx))
        rec: dict[str, Any] = {"date": str(now.astimezone(bankmod.MT).date()), "gain": round(gain, 9), "amount": amt}
        if amt > 0 and self.asset == "SOL" and float(st["wallet"]["sol"]) - amt < self.live.sol_fee_reserve + self.cfg.withdraw.stake_fee_buffer_sol:
            rec["skip"], amt = "SOL reserve", 0.0
        if amt > 0:
            if self.asset == "SOL":
                from live.stake import stake_sol
                res = stake_sol(self.cfg, amt, source=f"daily-bank-{self.asset}", send=True)
                ok = bool(res.get("ok")) and bool(res.get("sig"))
                out = res.get("jupsol_out")
            else:
                args = sweepmod.sweep_args(self.cfg, bankmod.MINT[coin], int(amt * 10 ** dec), bankmod.MINT["JUPSOL"])
                os.environ["JUP_LIVE"] = "1"
                res = jup("swap", {**args, "send": True})
                ok = bool(res.get("ok")) and bool(res.get("sig"))
                d = res.get("deltas") or {}
                self.bank_fee(d.get("feeLamports") or res.get("feeLamports"))
                out = int(d.get("jupsolDeltaAtoms") or res.get("quotedOut") or 0) / LAMPORTS if ok else None
                if ok:
                    try:
                        with ledger.at(ROOT / "live", "SOL"):
                            ledger.record_stake({"time": str(pd.Timestamp.now(tz="UTC")), "kind": "stake",
                                                 "source": f"daily-bank-{self.asset}", "jupsol_delta": out,
                                                 "wallet_sol_change": (d.get("nativeDeltaLamports") or 0) / LAMPORTS,
                                                 "fee_sol": (d.get("feeLamports") or 0) / LAMPORTS, "sig": res.get("sig"),
                                                 "solscan": f"https://solscan.io/tx/{res.get('sig')}",
                                                 "note": f"{amt:.9f} {coin} -> JupSOL (bank withdrawal)"})
                    except Exception as e:  # noqa: BLE001
                        log.warning("stake ledger row failed: %s", e)
            rec.update(sig=res.get("sig"), jupsol_out=out, ok=ok, err=res.get("error") or res.get("err"))
            if ok:
                bankmod.apply_withdrawal(b, amt, f"daily: {lc.withdraw_fraction:.0%} of hwm gain {gain:.9f} -> {out} JupSOL "
                                         f"sig {res.get('sig')}")
                b["window"] = max(b.get("window", 0), lc.window_ticks)
                log.warning("BANK DAILY WITHDRAWAL %s: %.9f %s (~$%.2f) -> %s JupSOL https://solscan.io/tx/%s", self.asset, amt,
                            coin, amt * cpx, out, res.get("sig"))
            else:
                log.warning("BANK daily withdrawal %s failed: %s; retry next tick", self.asset, rec)
                self.save()
                return
        elif gain > 0:
            rec.setdefault("skip", f"not worth {lc.withdraw_fee_factor:g}x est. swap/network cost (carried)")
        if self.asset != "SOL" and b.get("fee_sol", 0.0) >= lc.fee_settle_min_sol:
            want = min(b["fee_sol"] * sol_px / cpx, b["bal"])
            args = sweepmod.sweep_args(self.cfg, bankmod.MINT[coin], int(want * 10 ** dec), bankmod.MINT["SOL"])
            os.environ["JUP_LIVE"] = "1"
            res = jup("swap", {**args, "send": True})
            d = res.get("deltas") or {}
            if res.get("ok") and res.get("sig"):
                got = (int(d.get("nativeDeltaLamports") or 0) + int(d.get("wsolDeltaAtoms") or 0)) / LAMPORTS
                bankmod.debit(b, want, "fee_settle", f"{want:.9f} {coin} -> {got:.9f} SOL (fee debt) {res.get('sig')}")
                b["fee_sol"] = round(max(0.0, b["fee_sol"] - max(got, 0.0)), 9)
                b["window"] = max(b.get("window", 0), lc.window_ticks)
                rec["fee_settle"] = {"coin_in": want, "sol_out": got, "sig": res.get("sig")}
        b["wd_last_date"] = rec["date"]
        rec["bal"], rec["bal_usd"], rec["hwm"] = b["bal"], round(b["bal"] * cpx, 2), b["hwm"]
        b.setdefault("daily", []).append(rec)
        b["daily"] = b["daily"][-30:]
        log.warning("BANK DAILY %s %s: bal %.9f %s (= $%.2f) | hwm %.9f (= $%.2f) | gain since last wd %.9f | %s", self.asset,
                    rec["date"], b["bal"], coin, b["bal"] * cpx, b["hwm"], b["hwm"] * cpx, gain, json.dumps(rec, default=str)[:300])
        self.save()

    def hwm_close_armed(self) -> bool:
        g, L = self.cfg.profit_guard, self.local
        if not (g.enabled and g.close_when_met and self.bank_on):
            return False
        return g.close_requires == "any" or self.trade_guard_armed()

    def hwm_equity(self, pos: ChainPosition | None, st: dict[str, Any], price: float) -> float:
        b, c, lc = self.local.bank, self.cfg.costs, self.cfg.ledger
        sol_px, cpx = self.sol_price(st), self.coin_px(st)
        if price > 0:
            cpx = price   # coin price follows the evaluated price (guard_price bisection), not the oracle held fixed; at market identical
        cv = 0.0
        if pos is not None:
            fill = price * (1 - pos.side * c.slippage)
            swap = lc.swap_cost_pct if (pos.side == SHORT and self.asset != "SOL") else 0.0
            net = lc.close_network_native if self.asset == "SOL" else 0.0
            cv = bankmod.close_value_native(pos.side, pos.size, pos.avg_entry, pos.margin, fill, c.close_fee, cpx, swap, net)
        ra = self.local.rest_add   # a resting add's collateral left bal at placement and is neither refunded nor margin yet
        rest = float(ra.get("nat") or 0.0) if ra and ra.get("debited") and not ra.get("booked") else 0.0
        add = float(ra.get("size_usd") or 0.0) if ra else 0.0
        if rest and add > 0 and pos is not None and self.local.last_size > 0 and pos.size - self.local.last_size > 0.5 * add:
            rest = 0.0   # 2026-10-28: its fill is already in the position size (close value) -> never counted twice
        return bankmod.equity(b, cv, bankmod.fee_debt_native(b, sol_px, cpx)) + rest - self.sol_fees_unbooked(pos)

    def sol_fees_unbooked(self, pos: ChainPosition | None) -> float:
        """Trevor 2026-10-27 (SOL bot, open position): network fees this trade has paid so far (ledger_open.json, every tx
        the bot sent) that are not in bal yet (bal gets them at the flat true-up or a mid-trade sol_pay_trueup). ETH / BTC
        already carry theirs as fee_sol debt; flat: 0 (the true-up has them)."""
        if pos is None or self.asset != "SOL" or not self.bank_on or self.live.dry_run:
            return 0.0
        return max(self._net_fee_sol() - self.local.fee_mark_sol, 0.0)

    def sol_pay_trueup(self, st: dict[str, Any], pos: ChainPosition | None) -> None:
        """Trevor 2026-10-27 (SOL bot): after a payout booked as an estimate (TP / TP3 partial / trim) while the position
        stays open, re-measure this trade's own on-chain SOL flows (position txs since the open, exactly as the flat true-up)
        and credit the difference to bal now, so the guard equity and the TP@guard price use the amount really received
        and the network fees paid so far. Open request accounts' rent is in the flows as gone but never left bal (it sits
        in rent_held), so it is added back. Measured only on a consistent snapshot (every known decrease in the flows, no
        pending cancel, same open requests and size after the read); otherwise retried next tick."""
        L = self.local
        if not (L.pay_check and pos is not None and self.asset == "SOL" and self.bank_on and not self.live.dry_run):
            return
        if L.pending_cancels:
            return
        try:
            o = json.loads(ledger.OPEN.read_text()) if ledger.OPEN.exists() else {}
            reqs = sorted(str(r.get("pubkey")) for r in st["openRequests"])
            flows, fres = position_net_sol(o.get("position"), o.get("open_sig"))
            need = len(L.partials_done) + L.tp_hits + len(L.trims)
            if flows is None or fres.get("eventErrors") or int(fres.get("decreases") or 0) < need:
                log.warning("BANK payout true-up deferred: position flows %s, decreases %s of %d, errors %s", flows,
                            fres.get("decreases"), need, fres.get("eventErrors"))
                return
            st2 = self.chain()
            pos2 = self.chain_position(st2)
            if (sorted(str(r.get("pubkey")) for r in st2["openRequests"]) != reqs or pos2 is None
                    or abs(pos2.size - pos.size) > 0.01):
                log.warning("BANK payout true-up deferred: requests / size changed during the read")
                return
        except Exception as e:  # noqa: BLE001 - accounting must never stop the trading loop; retried next tick
            log.warning("BANK payout true-up failed: %s", e)
            return
        b = L.bank
        booked = round(b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0), 9)
        rent = round(bankmod.REQ_RENT * len(reqs), 9)
        adj = round(flows + rent - booked, 9)
        bankmod.credit(b, adj, "payout_trueup", f"position flows {flows:+.9f} + open request rent {rent:.9f} vs booked {booked:+.9f}")
        if b.get("res_last") is not None:
            b["res_last"] = round(b["res_last"] - adj, 9)   # moved between reserve float and bal
        L.fee_mark_sol = float(o.get("network_fee_lamports") or 0) / LAMPORTS
        L.pay_check = False
        log.warning("BANK payout true-up %+.9f SOL (position flows %+.9f, %d txs, + %d open request rent %.9f, booked %+.9f; "
                    "network fees %.9f now in bal) bal %.9f", adj, flows, fres.get("nTx", 0), len(reqs), rent, booked,
                    L.fee_mark_sol, b["bal"])
        self.save()

    def bank_equity_flat(self, st: dict[str, Any]) -> float:
        """Own-coin equity with nothing in a position (= the hwm_equity formula without the close value)."""
        b = self.local.bank
        return round(bankmod.equity(b, 0.0, bankmod.fee_debt_native(b, self.sol_price(st), self.coin_px(st))), 9)

    def guard_target(self, st: dict[str, Any]) -> float:
        """Per-trade profit guard (Trevor 2026-10-14): target = trade_start_equity x (1 + min_profit_pct_trade). The baseline
        is saved when the trade opens (before the opening debit). A position already open without one (deploy / adopt) gets
        the bank HWM, which equals the flat equity at its open (HWM only rises while flat): one-off, logged, never HWM-written."""
        b = self.local.bank
        if b.get("trade_start_equity") is None:
            b["trade_start_equity"] = b["hwm"]
            log.warning("MIGRATION: trade_start_equity unset on an open position -> %.9f (= hwm, the flat equity at its open); hwm/wd_mark untouched", b["hwm"])
            self.save()
        return b["trade_start_equity"] * (1 + self.cfg.profit_guard.min_profit_pct_trade)

    def guard_check(self, pos: ChainPosition, st: dict[str, Any], price: float) -> tuple[bool, str]:
        eq, tgt = self.hwm_equity(pos, st, price), self.guard_target(st)
        return eq >= tgt - 1e-12, (f"trade guard: equity if closed now {eq:.9f} vs start {self.local.bank['trade_start_equity']:.9f} x (1+{self.cfg.profit_guard.min_profit_pct_trade:g}) = {tgt:.9f}"
                                   + self.guard_sole_note(pos, st, price, eq, tgt))

    GUARD_CHAIN_MAX_AGE_S = 60.0   # a chain read older than this is stale for the guard's close decision

    def guard_chain_equity(self, st: dict[str, Any], pos: ChainPosition | None) -> tuple[float | None, str]:
        """Trevor 2026-10-28 14:42/14:49 MT ("only one truth: what is on the table"): this bot's guard equity in its own coin
        from the chain read ``st`` only -- no lot / order / payout records, no fee estimates from records:
          wallet    ETH / BTC: the bot's own token in the wallet ATA (sweepTokens atoms). SOL: native SOL + the rent locked
                    in the wallet's open request accounts (allOpenRequests x REQ_RENT, refundable lamports) - sol_fee_reserve
                    (the 0.02 SOL fee reserve, never in bal / HWM).
          + escrow  own-coin tokens held on chain in this market's open request accounts (escrowAtoms of each request's
                    escrow ATA: a resting add's collateral until it fills or is cancelled).
          + close   the position account: collateralUsd - accrued borrowFeeUsd + PnL at the on-chain oracle - close fee at
                    the custody's on-chain decreasePositionBps, in own coin at the oracle, less the same execution haircuts
                    as hwm_equity (slippage, ETH/BTC short swap cost, SOL close tx).
          - loans   the only bookkeeping used: JupSOL loan / inter-bot loan / SOL fee debt between the bots (ETH / BTC owe
                    the SOL their network fees took from the shared wallet; the SOL bot is owed it), exactly as HWM counts them.
        Like-for-like with HWM / trade_start_equity: both are bal - loans (bal excludes the fee reserve and request rent).
        (None, why) when any read failed or is stale: the guard never closes on it."""
        b, lv = self.local.bank, self.live
        coin = b["coin"]
        dec = bankmod.DECIMALS[coin]
        age = time.time() - float(st.get("_read_at") or 0.0)
        if age > self.GUARD_CHAIN_MAX_AGE_S:
            return None, f"chain read {age:.0f}s old (> {self.GUARD_CHAIN_MAX_AGE_S:g}s)"
        o, so = st.get("oracle") or {}, st.get("solOracle") or st.get("oracle") or {}
        for nm, x in (("oracle", o), ("SOL oracle", so)):
            if not x.get("price") or float(x.get("ageSec", 1e9)) > lv.max_oracle_age_sec:
                return None, f"{nm} stale / missing {x}"
        px, sol_px = float(o["price"]), float(so["price"])
        if b.get("park"):
            return None, "JupSOL park open (its JupSOL value is not read here)"
        if self.asset == "SOL":
            wallet = float(st["wallet"]["sol"]) + bankmod.rent_held(st) - lv.sol_fee_reserve
        else:
            tok = st.get("sweepTokens") or {}
            if "error" in tok or coin not in tok or tok[coin].get("atoms") is None:
                return None, f"{coin} wallet balance unreadable: {str(tok)[:120]}"
            wallet = int(tok[coin]["atoms"]) / 10 ** dec
        esc = 0
        for r in st.get("openRequests") or []:
            if r.get("escrowError") or r.get("escrowAtoms") is None:
                return None, f"request {r.get('pubkey')} escrow unreadable: {r.get('escrowError')}"
            a = int(r["escrowAtoms"])
            if a and r.get("mint") != bankmod.MINT[coin]:
                return None, f"request {r.get('pubkey')} escrow holds {r.get('mint')}, not {coin}"
            esc += a
        escrow = esc / 10 ** dec
        cv = 0.0
        if pos is not None:
            p = st["positions"]["long" if pos.side == LONG else "short"]
            bps = (st.get("custody") or {}).get("decreasePositionBps")
            if p.get("borrowFeeUsd") is None or bps is None or not p.get("sizeUsd"):
                return None, f"position / fee read incomplete (borrowFeeUsd {p.get('borrowFeeUsd')}, decreasePositionBps {bps})"
            c, lc = self.cfg.costs, self.cfg.ledger
            fill = px * (1 - pos.side * c.slippage)
            swap = lc.swap_cost_pct if (pos.side == SHORT and self.asset != "SOL") else 0.0
            net = lc.close_network_native if self.asset == "SOL" else 0.0
            cv = bankmod.close_value_native(pos.side, float(p["sizeUsd"]), float(p["price"]),
                                            float(p["collateralUsd"]) - float(p["borrowFeeUsd"]), fill, float(bps) / 1e4,
                                            px, swap, net)
        if self.asset == "SOL":
            fees = -sum(self._peer_fee_sol())   # receivable: SOL the ETH / BTC bots' fees took from this wallet
        else:
            fees = bankmod.fee_debt_native(b, sol_px, px)
        loans = bankmod.loan_outstanding(b) - bankmod.ib_adj(b)
        eq = wallet + escrow + cv - fees - loans
        return eq, (f"on-chain {wallet:.9f} wallet + {escrow:.9f} escrow + {cv:.9f} close - {fees:.9f} fee debt "
                    f"- {loans:.9f} loans = {eq:.9f} {coin}")

    def _peer_fee_sol(self) -> list[float]:
        """ETH / BTC bots' unsettled SOL network-fee debt (their bank fee_sol, owed to the shared SOL wallet). Raises if unreadable."""
        out = []
        for a, rel in jlmod.STATE_FILES.items():
            if a != "SOL":
                out.append(float((json.loads((ROOT / rel).read_text()).get("bank") or {}).get("fee_sol") or 0.0))
        return out

    def guard_check_chain(self, st: dict[str, Any], pos: ChainPosition | None) -> tuple[bool, str]:
        """The profit guard's close decision (2026-10-28): on-chain equity >= guard_target; any failed / stale read: no close."""
        try:
            eq, why = self.guard_chain_equity(st, pos)
        except Exception as e:  # noqa: BLE001 - a failed read never closes
            eq, why = None, f"read failed: {e!r}"
        if eq is None:
            return False, f"on-chain guard: NO CLOSE this tick ({why})"
        tgt = self.guard_target(st)
        return eq >= tgt - 1e-12, (f"trade guard (on chain): {why} vs start {self.local.bank['trade_start_equity']:.9f} x "
                                   f"(1+{self.cfg.profit_guard.min_profit_pct_trade:g}) = {tgt:.9f}")

    def guard_recheck_chain(self, pos: ChainPosition) -> tuple[bool, str, ChainPosition | None, float]:
        """Right before a guard close: a fresh chain read; same side still open and on-chain equity >= target, else abort."""
        try:
            st2 = self.chain()
            pos2 = self.chain_position(st2)
        except Exception as e:  # noqa: BLE001
            return False, f"re-read failed: {e!r}", None, 0.0
        if pos2 is None or pos2.side != pos.side:
            return False, "position changed on chain during the check", None, 0.0
        ok, note = self.guard_check_chain(st2, pos2)
        return ok, note, pos2, float(st2["oracle"]["price"])

    def guard_sole_note(self, pos: ChainPosition, st: dict[str, Any], price: float, eq: float, tgt: float) -> str:
        """guard_tps: target (coin + USD) and the guard close price where TP1/TP2 sit. Empty when the flag is off."""
        if not self.guard_tps():
            return ""
        try:
            px, gp = self.guard_tp_px(pos, st, verbose=False)
            return (f" | TP@GUARD target {tgt:.9f} {self.local.bank['coin']} = ${tgt * self.coin_px(st):.2f}, TP price "
                    + (f"{px:.2f} (guard {gp:.2f} + fee buffer)" if gp else "n/a (met at market)") + f", placed {self.local.tp_px}")
        except Exception as e:  # noqa: BLE001 - log text only
            return f" | TP@GUARD (estimate failed: {e!r})"

    def hwm_check(self, pos: ChainPosition, st: dict[str, Any], price: float, min_native: float) -> tuple[bool, str]:
        """Equity if closed now (own coin) >= hwm + min_native? (guard: min_profit_native; TP timer: 0 = not below HWM)."""
        b = self.local.bank
        eq = self.hwm_equity(pos, st, price)
        tgt = b["hwm"] + min_native
        return eq >= tgt - 1e-12, f"HWM guard: equity if closed now {eq:.9f} {b['coin']} vs hwm {b['hwm']:.9f} + {min_native:g} = {tgt:.9f}"

    def bank_status(self, st: dict[str, Any], pos: ChainPosition | None, price: float) -> str:
        b = self.local.bank
        eq = self.hwm_equity(pos, st, price)
        tgt = self.guard_target(st) if pos is not None else None
        arm = (" | guard " + ("ARMED (adds/partials this trade)" if self.hwm_close_armed() else "not armed (first entry: TPs only)")) if pos else ""
        sole = self.guard_sole_note(pos, st, price, eq, tgt) if (pos is not None and self.hwm_close_armed()) else ""
        return bankmod.status_line(b, self.coin_px(st), self.sol_price(st), eq, tgt) + arm + sole

    # ------------------------------------------------------------- one tick
    def tick(self) -> None:
        st = self.chain()
        o = st["oracle"]
        if not o.get("price") or o.get("ageSec", 1e9) > self.live.max_oracle_age_sec:
            log.warning("oracle stale %s; skipping tick", o)
            return
        price = o["price"]
        sol_px = self._sol_px = self.sol_price(st)
        self._mkt_px = price
        self.rescale(price)
        pos = self.chain_position(st)
        L = self.local
        if L.pending_cancels and self.retry_cancels(st, pos):
            return

        # reconcile with chain
        if pos is None and L.side:
            log.info("position gone on-chain (TP2/stop/liquidation/manual); clearing local state")
            rest_cancelled = False
            if self.cfg.resting.any_on and st["openRequests"]:   # [resting]: leftover add / partial orders cancelled first
                log.info("resting: cancelling leftover requests of the closed trade %s",
                         [(r.get("requestChange"), r.get("triggerPrice")) for r in st["openRequests"]])
                rest_cancelled = True
                if not self.cancel_all(st):
                    self.cancel_failed(st["openRequests"], None, "leftover orders after the position closed")
            ledger.update(exit=f"{'TP2' if L.tp_hits else 'external/TP/liq'}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
            if self.bank_on and L.pg_m_size > 0:   # last seen size / collateral / whole-position avg (2026-10-27; lots: old state)
                avg = L.pg_avg if L.pg_avg > 0 else \
                    (sum(l["size"] for l in L.lots) / sum(l["size"] / l["price"] for l in L.lots)) if L.lots else price
                tpx = next((p for p in (L.tp_px or [])[L.tp_hits:] if p), price)
                self.bank_payout(L.pg_margin + L.side * L.pg_m_size * (tpx - avg) / avg - self.cfg.costs.close_fee * L.pg_m_size,
                                 "position gone (TP2 / external)")
            try:
                self.sweep(st, L.side)   # SOL out: before booking, so swapped proceeds land in this trade
            except Exception as e:  # noqa: BLE001 - retried next tick
                log.warning("sweep failed: %s", e)
            self.bank_flat(self.book_flat(st, L.side))
            if st["openRequests"] and not rest_cancelled:
                self.cancel_all(st)
            self.local = L = Local(last_bar=L.last_bar, day=L.day, day_start_equity=L.day_start_equity,
                                   flat_since=str(pd.Timestamp.now(tz="UTC")), trim_day=L.trim_day, trim_day_n=L.trim_day_n,
                                   bank=L.bank, pending_cancels=L.pending_cancels)
        elif pos is not None and not L.side:
            self.adopt_position(st, pos)
            return
        if pos is not None and L.side == pos.side and L.tp_retry and self.tp_retry_step(pos):
            return
        if pos is not None and L.side == pos.side and (L.bank or {}).get("tp_defer") and not self.guard_bank_stale(st):
            log.info("TP at guard: payout credited / retry after deferral; re-placing TPs at the guard price")
            self.sync_tp_ladder(self.chain_position(self.chain()) or pos)
            if not (L.bank or {}).get("tp_defer"):
                return
        rest_resync = False
        if self.cfg.resting.any_on and pos is not None and L.side == pos.side:
            pos, rest_resync = self.resting_reconcile(st, pos, price)
        if pos is not None and L.side == pos.side and L.lots and self.recover_missing_add(st, pos, price):
            self.sync_tp_ladder(self.chain_position(self.chain()) or pos)   # fresh TP1/TP2, exactly as after an add
        if pos is None:
            pass
        elif pos is not None and pos.size < L.last_size - 0.01:
            if L.tp_hits == 0:
                L.first_tp_time = str(pd.Timestamp.now(tz="UTC"))
            if L.pg_base_sol is not None:   # partial TP pays out returned collateral + PnL (SOL / swept USDC / ETH / WBTC)
                rp = st["positions"]["long" if pos.side == LONG else "short"].get("realisedPnlUsd")
                usd, note = self.pg_tp_payout_usd(pos, price, rp)
                L.pg_recv_sol = round(L.pg_recv_sol + max(usd, 0.0) / sol_px, 9)
                log.info("profit guard: TP payout %s -> +%.6f SOL (at SOL %.3f)", note, max(usd, 0.0) / sol_px, sol_px)
            if self.bank_on:
                rp = st["positions"]["long" if pos.side == LONG else "short"].get("realisedPnlUsd")
                cl = L.last_size - pos.size
                old_m = L.pg_margin if L.pg_margin > 0 and abs(L.pg_m_size - L.last_size) <= 0.01 else pos.margin * L.last_size / pos.size
                d_rp = float(rp) - L.pg_rpnl if rp is not None and L.pg_rpnl is not None else pos.side * cl * (price - pos.avg_entry) / pos.avg_entry
                self.bank_payout(max(old_m - pos.margin, 0.0) + d_rp - self.cfg.costs.close_fee * cl, f"TP fill ${cl:.2f}")
            L.tp_hits += 1
            L.last_size = pos.size
            log.info("TP fill detected: size now %.2f, tp_hits=%d", pos.size, L.tp_hits)
            ledger.update(exit=f"TP{L.tp_hits}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
            pos = self.chain_position(st)

        if pos is not None and L.pay_check:   # 2026-10-27: real payout before any TP@guard re-place / guard check
            self.sol_pay_trueup(st, pos)
        if pos is not None:   # baseline guard: remember size / collateral / realised PnL for the next TP fill
            rp = st["positions"]["long" if pos.side == LONG else "short"].get("realisedPnlUsd")
            L.pg_m_size, L.pg_margin, L.pg_avg = pos.size, pos.margin, pos.avg_entry
            L.pg_rpnl = float(rp) if rp is not None else None
            if self.cfg.resting.any_on:
                L.rest_last_avg = pos.avg_entry
        if getattr(self, "_tp_resize", False) and pos is not None and not rest_resync:
            self._tp_resize = False
            self.resize_tps(self.chain_position(self.chain()) or pos)
        if rest_resync and pos is not None:   # an add filled via its order: fresh TP1/TP2 ladder
            self.sync_tp_ladder(self.chain_position(self.chain()) or pos)   # (exactly as after a market add)
            self.partial_sync(pos, price)
        if pos is not None or L.side == 0:   # partial-TP proceeds / leftovers after booking
            try:
                self.sweep(st, pos.side if pos is not None else None)
            except Exception as e:  # noqa: BLE001 - retried next tick
                log.warning("sweep failed: %s", e)
        if pos is None and not L.flat_since:   # idle clock: since the last booked close, else now
            lc = last_close_time()
            L.flat_since = lc if lc and pd.Timestamp(lc) <= pd.Timestamp.now(tz="UTC") else str(pd.Timestamp.now(tz="UTC"))
            log.info("idle clock seeded: flat since %s", L.flat_since)
        # this bot's free collateral: share of (native SOL - reserve + every bot's open collateral) - own collateral,
        # capped at the native SOL really free (live/budget.py); share 1.0 alone = the old native-SOL rule
        self.budget = budget_from_state(st, self.cfg.bot.share, self.live.sol_fee_reserve, self.asset,
                                        own_pos_collateral=pos.margin if pos else 0.0)
        if self.bank_on:
            try:
                self.bank_tick(st, pos, price)
            except Exception as e:  # noqa: BLE001 - accounting must never stop the trading loop
                log.exception("bank tick failed: %s", e)
            import dataclasses as _dc
            self.budget = _dc.replace(self.budget, free_usd=max(0.0, self.local.bank["bal"] * self.coin_px(st)))
        account = ChainAccount(margin_sol(st), sol_px, pos, self.live.sol_fee_reserve, cash=self.budget.free_usd)
        equity = account.equity(price)
        today = str(pd.Timestamp.now(tz="UTC").date())
        if L.day != today:
            L.day, L.day_start_equity = today, equity
        self.save()

        if self.bank_on and self.cfg.ledger.withdraw_enabled and not self.killed():
            try:
                self.bank_daily(st, pos)
            except Exception as e:  # noqa: BLE001
                log.exception("bank daily withdrawal failed: %s", e)
        elif self.cfg.withdraw.enabled and not self.killed() and not self.bank_on:
            from live.withdraw import run_withdraw
            wpath = ROOT / "live" / "withdraw_state.json"
            wstate = json.loads(wpath.read_text()) if wpath.exists() else {}
            if pos is not None:
                plan = self.strategy.plan_next_add(pos, account.cash, pd.Timestamp.now(tz="UTC")) \
                    if self.cfg.dca.mode == "levels" else None
                next_usd = plan.margin if plan else (self.cfg.dca.add_margin_usd or 0.0)
            else:
                next_usd = self.strategy.initial_margin(account.cash)   # entry.first_collateral_* on current free collateral
            wrec = run_withdraw(self.cfg, wstate, st, pd.Timestamp.now(tz="UTC").to_pydatetime(), next_add_sol=next_usd / sol_px)
            wpath.write_text(json.dumps(wstate, indent=1))
            if wrec and wrec.get("staked_sol"):   # SOL moved to the stake bucket is not a daily equity loss
                L.day_start_equity -= wrec["staked_sol"] * sol_px
                self.save()

        if self.killed():
            if pos is not None and self.live.close_all_on_kill:
                self.close(pos, price, "kill switch")
            log.info("KILL present: no new orders (price %.3f, pos %s)", price, pos and asdict(pos)["size"])
            return

        # every tick: margin stop (no on-chain SL) and post-TP1 timer, on the live oracle price
        if pos is not None:
            lev = self.strategy.exit_levels(pos)
            if lev.stop is not None and pos.side * (price - lev.stop.price) <= 0:
                self.close(pos, price, f"stop {lev.stop.reason.value} @ {lev.stop.price:.3f}")
                return
            if self.bank_on:   # per-trade profit guard (Trevor 2026-10-14): close all once equity >= trade_start_equity x 1.002 (never moves TPs)
                if self.hwm_close_armed():
                    ok, note = self.guard_check_chain(st, pos)   # 2026-10-28: on-chain equity only
                    if ok:
                        ok, note, pos2, px2 = self.guard_recheck_chain(pos)   # fresh chain read right before the close
                        if ok:
                            self.close(pos2, px2, f"profit_guard ({note})")
                            return
                        log.warning("profit_guard: pre-close re-check -> NO close: %s", note)
                    elif note.startswith("on-chain guard: NO CLOSE"):
                        log.warning("%s", note)
            elif self.pg_close_armed():   # profit guard action (owner 18:02 MT): market-close once the close now meets it
                pg_ok, pg_note = self.pg_close_ok(pos, price)
                if pg_ok:
                    self.close(pos, price, f"profit_guard ({pg_note})")
                    return
            timer = self.cfg.tp.close_remainder_after_min
            if timer is not None and pos.first_tp_time is not None:
                expired = pd.Timestamp.now(tz="UTC") - pos.first_tp_time >= pd.Timedelta(minutes=timer)
                net = self.strategy.net_close_pnl(pos, price)
                pg_ok, pg_note = ((self.hwm_check(pos, st, price, 0.0) if self.bank_on else self.pg_close_ok(pos, price))
                                  if expired else (True, ""))
                if expired and (not self.cfg.tp.timer_require_profit or net > 0) and pg_ok:
                    self.close(pos, price, f"tp_timer (net est {net:+.3f}{'; ' + pg_note if pg_note else ''})")
                    return
                if expired:
                    log.info("timer expired but remainder at net %+.3f%s; holding (TPs stay, DCA active)", net,
                             f" / {pg_note}" if pg_note else "")
            if not self._fee_rechecked:   # fee-based TP minimum (Trevor 2026-10-13): once per process on the open position
                self._fee_rechecked = True
                if self.fee_min_recheck(pos, st):
                    return
            try:   # guard_tps: re-place TP1/TP2 when the live guard price moved past them (Trevor 2026-10-27 14:55 MT)
                if self.guard_drift_check(pos, st):
                    return
            except Exception as e:  # noqa: BLE001 - never halt; checked again next tick
                log.error("guard drift check failed: %r (no halt)", e)
            for name, fn in (("TP2_SET", self.tp2_set), ("TP3_SET", self.tp3_set)):   # one-shot manual TP moves (Grok Bot)
                tpf = self.dir / name
                if tpf.exists():
                    try:
                        fn(pos, st, tpf)
                    except Exception as e:  # noqa: BLE001 - never halt on a manual TP move
                        tpf.unlink(missing_ok=True)
                        log.error("%s failed: %s (file deleted; no halt)", name, e)
                    return
            retp = self.dir / "RETP_NOW"   # one-shot manual TP re-place (Grok Bot / owner), via the normal placement path
            if retp.exists():
                retp.unlink(missing_ok=True)
                log.warning("RETP_NOW: re-placing TP ladder once on the open position (manual request)")
                self.sync_tp_ladder(pos)
                return
            if self.cfg.trim.enabled:
                self._trim_sent = False
                try:
                    if self.maybe_trim(pos, st, price):
                        return   # trimmed + TPs re-placed: the next tick re-reads everything
                except Exception as e:  # noqa: BLE001 - before the decrease: skipped; after it: halt (state unknown)
                    log.exception("trim check failed: %s", e)
                    if self._trim_sent:
                        self.halt(f"trim error after the decrease was sent: {e}")
                        self.save()
                        return
            if self.cfg.resting.any_on:
                try:
                    self.resting_tick(pos, st, price, account, equity)
                except Exception as e:  # noqa: BLE001 - nothing placed this tick; retried next tick
                    log.exception("resting orders tick failed: %s", e)

        # strategy on each newly closed signal bar
        kl = fetch_klines(self.cfg.market.symbol, self.cfg.signal.tf)
        self.update_atr(kl)
        self.rescale(price)
        if not self._first_entry_logged:   # once at start; then on every bar line below
            log.info(self.budget.line(self.asset))
            if self.base_cfg.scale.enabled:
                log.info("scaled (%s price %.2f, own 1h ATR %s): %s", self.asset, price,
                         f"{self._own_atr:.3f}" if self._own_atr else "n/a", scaled_summary(self.cfg))
            log.info(self.first_entry_line(account.cash))
            if pos is None:
                self.strategy._flat_since = pd.Timestamp(L.flat_since) if L.flat_since else None
                log.info("entry thresholds now: %s", self.strategy.thresholds_at(pd.Timestamp.now(tz="UTC"))[2])
            self._first_entry_logged = True
        feats = compute_features(kl, self.cfg)
        bars = iter_bars(feats)
        if not bars or str(bars[-1].time) == L.last_bar:
            return
        bar = bars[-1]
        L.last_bar = str(bar.time)
        self.save()
        self.strategy.begin_bar(bar, account)
        log.info("bar %s %s close=%.3f rsi=%.1f trend_up=%s oracle=%.3f equity=$%.2f pos=%s | wallet %.6f SOL native (margin) | %s",
                 self.asset, bar.time, bar.close, bar.rsi, bar.trend_up, price, equity,
                 pos and f"{pos.side:+d} ${pos.size:.2f} avg {pos.avg_entry:.3f}", margin_sol(st),
                 stake_line(st) if self.cfg.withdraw.enabled else f"SOL {sol_px:.3f}")
        log.info(self.budget.line(self.asset))
        if self.base_cfg.scale.enabled:
            log.info("scaled: own 1h ATR %s | %s", f"{self._own_atr:.3f}" if self._own_atr else "n/a", scaled_summary(self.cfg))
        log.info(self.first_entry_line(account.cash))
        if pos is None:
            self.strategy._flat_since = pd.Timestamp(L.flat_since) if L.flat_since else None
            log.info("entry thresholds: %s", self.strategy.entry_thresholds(bar)[2])
        if pos is not None and self.cfg.trim.enabled:
            log.info(self.trim_status(pos, price))
        if self.bank_on:
            log.info(self.bank_status(st, pos, price))
        elif pos is not None and self.cfg.profit_guard.enabled:
            log.info(self.pg_status(pos, price))
        for it in self.strategy.exit_intents(bar, account):
            if pos is not None:
                self.close(pos, price, it.reason.value if it.reason else "exit")
            return
        intents = self.strategy.entry_intents(bar, account)
        if pos is not None and self.cfg.dca.mode == "levels":
            p = self.strategy.plan_next_add(pos, account.cash, pd.Timestamp.now(tz="UTC"))
            lv, fa = self.strategy._levels, self.front_atr()
            log.info("next add plan: %s", p and (
                f"{p.source} trigger {p.trigger:.2f} (level src {level_tf(lv, p.trigger) if p.source == 'levels' else p.source}) "
                f"add price {self.add_px(pos, p.trigger):.2f} (front {self.add_front():.2f} = "
                f"{self.cfg.dca.add_frontrun_atr:g} x ATR {fa if fa is not None else float('nan'):.3f} "
                f"{'own' if self._own_atr else 'analyst'}; ladder ATR {lv.atr if lv else float('nan'):.3f}) "
                f"margin ${p.margin:.2f} x{p.leverage:g} "
                f"-> avg {p.new_avg:.3f} liq {p.liq:.2f} next level {p.next_level:.2f} target {p.target_level:.2f}"))
            try:   # projected ladder: each rung fills at its trigger, re-planned with what is left
                for i, r in enumerate(self.strategy.project_ladder(pos, account.cash), 1):
                    log.info("ladder rung %d: %.2f coll $%.2f x%g notional $%.2f -> avg %.3f liq %.2f (next %.2f)",
                             i, r.trigger, r.margin, r.leverage, r.margin * r.leverage, r.new_avg, r.liq, r.next_level)
            except Exception as e:  # projection is informational only
                log.warning("ladder projection failed: %s", e)
        if pos is not None and self.cfg.resting.any_on:
            log.info(self.resting_status(pos))
        for it in intents:
            if self.cfg.resting.adds_enabled and it.action is Action.ADD:   # [resting]: adds rest as limit orders instead
                log.info("market add skipped (resting adds on): the ladder add rests as a limit order")
                continue
            self.open_or_add(it, pos, st, equity)
        for msg in self.strategy.skip_log:
            log.info(msg)
        self.strategy.skip_log.clear()


def fetch_klines(symbol: str, tf: str) -> pd.DataFrame:
    rows: list[list[Any]] = []
    end = int(time.time() * 1000)
    for _ in range(KLINE_PAGES):
        with urllib.request.urlopen(KLINES.format(sym=symbol, tf=tf, end=end), timeout=HTTP_TIMEOUT) as r:
            page = json.load(r)
        rows = page + rows
        end = page[0][0] - 1
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.set_index("time").astype(float)
    return df[df.index + pd.Timedelta(tf) <= pd.Timestamp.now(tz="UTC")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/live_100.toml")
    ap.add_argument("--set", dest="overrides", action="append", default=[])
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config, a.overrides)
    (ROOT / cfg.bot.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / cfg.bot.state_dir / "logs" / "bot.log"), logging.StreamHandler()])
    if cfg.bot.state_dir != "live":   # ETH / BTC: own trades.csv / ledger_open.json / ledger_summary.json
        ledger.configure(cfg.bot.state_dir, cfg.bot.asset)
    import os
    if not cfg.live.dry_run:
        os.environ["JUP_LIVE"] = "1"   # lets the TS CLI send; dry_run never sets it
    log.info("start %s asset=%s share=%.2f state_dir=%s dry_run=%s hash=%s withdraw.enabled=%s mode=%s", cfg.name, cfg.bot.asset,
             cfg.bot.share, cfg.bot.state_dir, cfg.live.dry_run, cfg.short_hash(), cfg.withdraw.enabled, cfg.withdraw.mode)
    bot = Bot(cfg)
    while True:
        try:
            bot.tick()
        except Exception as e:  # noqa: BLE001 - keep looping; state is re-read from chain next tick
            log.exception("tick error: %s", e)
        if a.once:
            break
        time.sleep(TICK_SEC)


if __name__ == "__main__":
    main()
