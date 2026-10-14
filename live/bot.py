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
import logging
import os
import time
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
from dca_levels import TpFade, load_analyst_levels
from asset_scale import atr_from_klines, levels_with_fallback, scale_config, scaled_summary
from live.budget import budget_from_state
from live.flows import flow_extras, position_net_sol
from live import sweep as sweepmod
from live import trim as trimmod
from live import bank as bankmod
from live import resting as restmod

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
    # smart TPs: trigger prices this bot placed per ladder level (0 = none), best price / 15m RSI extreme since placement
    tp_px: list[float] = field(default_factory=list)
    tp_since: str | None = None
    tp_peak: float = 0.0
    tp_rsi_ext: float | None = None
    tp_rsi_now: float | None = None
    tp_faded: bool = False
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
    tp_retry: bool = False   # Trevor 2026-10-12 08:46 MT: TP1/TP2 re-placed next tick (placement failed / TPs cancelled)
    last_partial_px: float | None = None
    rest_last_avg: float = 0.0
    add_pending: dict | None = None   # market add sent, fill not saved yet (cleared once its lot is saved)


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
        self._tp_checked: pd.Timestamp | None = None
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
        if name == "decrease" and args.get("side") == "short" and self.asset in ("ETH", "BTC") \
                and getattr(self.live, "short_payout_native", False):   # Trevor 2026-10-13: payout in own coin
            args = {**args, "desiredMint": bankmod.MINT["ETH" if self.asset == "ETH" else "WBTC"]}
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
        args = {"time": str(pd.Timestamp.now(tz="UTC")), "wallet_sol": st["wallet"]["sol"], "sol_px": self.sol_price(st),
                "realised": rec.get("realisedPnlUsd"), "hourly": hourly, "flows": flows, "rent": rent, "gross": gross}
        if self.bank_on and self.asset != "SOL" and not self.live.dry_run and ledger.OPEN.exists():
            # own-coin ledger (2026-10-09): the ETH / BTC bot's result is its own-coin balance change, and the last payout
            # lands in the wallet a tick later -> book once the payout window is over (bank_book_pending)
            self.local.bank["book_pend"] = args
            log.info("LEDGER %s trade flat: booking in own coin (%s) once the payout settles (window %d ticks); position "
                     "SOL flows %s (fees only, not the result)", self.asset, self.local.bank["coin"],
                     self.cfg.ledger.window_ticks, flows)
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
            self._book_row(a, native={"coin": b["coin"], "deposited": b.get("trade_dep", 0.0),
                                      "received": b.get("trade_recv", 0.0), "coin_px": cpx})
        log.info("BANK trade done: deposited %.9f, received %.9f %s (net %+.9f)", b.get("trade_dep", 0.0),
                 b.get("trade_recv", 0.0), b["coin"], b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0))
        b["trade_dep"], b["trade_recv"] = 0.0, 0.0
        self.save()

    def sweep(self, st: dict[str, Any], side: int | None) -> None:
        """SOL in / SOL out, self-healing (2026-10-08): every tick, flat or in a position, swap any USDC / ETH / WBTC above
        dust back to SOL with backoff + API-base fallback, one bot at a time (file lock), booked into the trade that
        produced it (live/sweep.py self_heal). Skipped while any market request is in flight (its tokens may still be
        moving). ``side`` is kept for the call sites; attribution comes from the chain + the bots' ledgers."""
        b = self.cfg.bot
        if not b.sweep_enabled:
            return
        if [r for r in st.get("allOpenRequests", st["openRequests"]) if r.get("requestType") != "trigger"]:
            return
        if self.bank_on:   # own-coin ledger: only USDC is swept, into the producing bot's own coin
            for bk in sweepmod.self_heal_own(self.cfg, self.asset, st, jup, dry_run=self.live.dry_run, min_usd=b.sweep_min_usd):
                self.bank_sweep_booked(bk, st)
            return
        sweepmod.self_heal(self.cfg, self.asset, st, jup, dry_run=self.live.dry_run, min_usd=b.sweep_min_usd)

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
            if attempt < 2 and "6003" in json.dumps(res.get("err")):
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

    def sync_tp_ladder(self, pos: ChainPosition) -> None:
        """Cancel existing TP triggers and place the full ladder on the whole position (strategy.tp_targets:
        normal ladder on the average, above-water TPs after a deep levels-mode add, smart TP2 / fee floors / never
        past market when tp.smart_tp2). Resets the momentum-fade tracking."""
        st = self.chain()
        if not self.cancel_reqs(st, self.ladder_requests(st)):
            self.cancel_failed(self.ladder_requests(st), "sync", "TP cancel")
            return
        side = "long" if pos.side == LONG else "short"
        price = st["oracle"]["price"]
        L = self.local
        n = len(self.cfg.tp.ladder)
        L.tp_px, L.tp_since, L.tp_peak, L.tp_faded = [0.0] * n, str(pd.Timestamp.now(tz="UTC")), price, False
        L.tp_rsi_ext = L.tp_rsi_now
        targets = self.trim_tp_guard(pos, self.strategy.tp_targets(pos, price=price), "placement")
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

    def resize_tps(self, pos: ChainPosition) -> None:
        """Trevor 2026-10-12 06:07 MT: after a TP3 (per-add partial) fill, cancel and re-place the pending TP1/TP2
        triggers at the SAME prices (local.tp_px) sized on the remaining position with the ladder's % split. No
        recorded prices (e.g. after a restart): falls back to the normal full re-place (as RETP_NOW)."""
        n = len(self.cfg.tp.ladder)
        pend = list(range(pos.tp_hits, n))
        if not pend or any(i >= len(self.local.tp_px) or not self.local.tp_px[i] for i in pend):
            log.warning("TP resize after TP3: no recorded TP prices; full TP re-place instead")
            self.sync_tp_ladder(pos)
            return
        st = self.chain()
        if not self.cancel_reqs(st, self.ladder_requests(st)):
            self.cancel_failed(self.ladder_requests(st), "resize", "TP cancel (resize after TP3)")
            return
        side = "long" if pos.side == LONG else "short"
        price = st["oracle"]["price"]
        pxs = list(self.local.tp_px)
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
            if attempt < 2 and "6003" in json.dumps(res.get("err")):
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

    def check_tps(self, pos: ChainPosition, st: dict[str, Any], price: float) -> None:
        """Periodic smart-TP check (tp.smart_tp2): track the best price, detect a momentum fade, recompute the ladder
        and re-place only pending TPs that moved >= tp_min_move and still sit beyond market. Only triggers that match
        what this bot placed (or, after a restart without that record, exactly one trigger per pending level) are
        touched; anything else is logged and left alone."""
        t, L = self.cfg.tp, self.local
        s = pos.side
        n = len(t.ladder)
        L.tp_peak = price if not L.tp_peak else (max(L.tp_peak, price) if s == LONG else min(L.tp_peak, price))
        now = pd.Timestamp.now(tz="UTC")
        if self._tp_checked is not None and now - self._tp_checked < pd.Timedelta(minutes=t.tp_check_min) - pd.Timedelta(seconds=5):
            return
        self._tp_checked = now
        pending = list(range(pos.tp_hits, n))
        reqs = self.tp_requests(st)
        if not pending or not reqs:
            return
        if len(L.tp_px) != n:
            L.tp_px = (L.tp_px + [0.0] * n)[:n]
        by_level: dict[int, dict[str, Any]] = {}
        for i in pending:
            m = [r for r in reqs if L.tp_px[i] and abs(r["triggerPrice"] - L.tp_px[i]) < 0.006]
            if len(m) == 1:
                by_level[i] = m[0]
        if len(by_level) != len(pending):
            if len(reqs) == len(pending):   # restart without a record: adopt by price order (nearest = lowest level)
                srt = sorted(reqs, key=lambda r: s * r["triggerPrice"])
                for i, r in zip(pending, srt):
                    by_level[i] = r
                    L.tp_px[i] = round(r["triggerPrice"], 2)
                log.info("TP check: adopted on-chain triggers %s", [L.tp_px[i] for i in pending])
                if not L.tp_since:
                    L.tp_since = str(now)
                self.save()
            else:
                log.warning("TP check skipped: %d on-chain triggers %s vs %d pending levels %s",
                            len(reqs), [r["triggerPrice"] for r in reqs], len(pending), [L.tp_px[i] for i in pending])
                return
        fade = TpFade(L.tp_peak, price, L.tp_rsi_ext, L.tp_rsi_now)
        targets = self.strategy.tp_targets(pos, price=price, fade=fade, prev_faded=L.tp_faded)
        res = self.strategy.last_tp
        if res is None:
            return
        targets = self.trim_tp_guard(pos, targets, "periodic")
        if res.faded and not L.tp_faded:
            L.tp_faded = True
            self.log_tp_reasons("fade")
            self.save()
        side = "long" if s == LONG else "short"
        logged = False
        for i in pending:
            new, old = round(targets[i].price, 2), L.tp_px[i]
            if abs(new - old) < t.tp_min_move - 1e-9:
                continue
            if res.past_market[i]:
                log.info("TP%d check: wanted %.2f is not beyond market %.3f + %.2f; keeping %.2f", i + 1,
                         self.strategy.last_tp_raw(i), price, t.tp_market_gap, old)
                continue
            if not logged:
                self.log_tp_reasons("periodic")
                logged = True
            log.info("TP%d re-place %.2f -> %.2f (move %+.2f >= %.2f)", i + 1, old, new, new - old, t.tp_min_move)
            c = self.cancel_request(by_level[i]["pubkey"], pos.size)
            if c == "failed":
                self.cancel_failed([by_level[i]], "sync", f"TP{i + 1} cancel")
                self.save()
                return
            L.tp_px[i] = 0.0
            if c == "filled":   # a trigger executed meanwhile: place nothing on a stale size; next tick sees the fill
                self.save()
                return
            prev = t.ladder[i - 1].close_pct if i else 0.0
            if not self.place_tp(pos, side, i, new, prev):
                self.halt(f"TP{i + 1} re-place failed")
                self.save()
                return
            self.save()

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
                             self.cfg.trim.trim_min_net_usd, self.strategy.tp1_ratio_for(pos), self.cfg.tp.tp_min_gap_usd)
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

    def pg_close_armed(self) -> bool:
        """Profit-guard market close applies: guard active, close_when_met, and (close_requires "adds_or_trims") the
        position had at least one add or trim."""
        g, L = self.cfg.profit_guard, self.local
        if not (self.pg_active() and g.close_when_met):
            return False
        return g.close_requires == "any" or L.n_entries > 1 or len(L.lots) > 1 or bool(L.trims)

    def guard_allowed(self, pos: ChainPosition, price: float) -> bool:
        """[ledger] guard_only_underwater: the profit-guard market close may only fire on the losing side of avg."""
        if not getattr(self.cfg.ledger, "guard_only_underwater", False):
            return True
        return pos.side * (price - pos.avg_entry) < 0

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
        if self.bank_on:   # own-coin ledger: funded ONLY from this bot's own balance, never borrowed from other bots
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
            L.last_leverage, L.last_entry, L.last_size = it.leverage, price, new.size
            L.tp_hits, L.first_tp_time = 0, None   # fresh TP1 50% / TP2 100% ladder on the whole position
            L.lots.append(lot)
            L.trade_opened_usd += lot["size"]
        self.local.add_pending = None
        log.info("LOT added #%d: %s fill %.4f size $%.2f coll $%.2f x%g", len(self.local.lots), lot["time"][:19],
                 lot["price"], lot["size"], lot["coll"], lot["lev"])
        self.save()
        new = self.chain_position(self.chain())
        assert new is not None
        self.sync_tp_ladder(new)
        if pos is not None and self.cfg.resting.partials_enabled:   # [resting]: the single partial TP covers the new add
            self.partial_on_add(new, price, self.local.lots[-1])

    # ------------------------------------------------------------- resting orders ([resting], 2026-10-11, default OFF)
    def _rest_note(self, key: str, msg: str, level: int = logging.INFO) -> None:
        """Log ``msg`` once per distinct message for ``key`` (a refused / skipped placement is retried every tick)."""
        notes = self.__dict__.setdefault("_rest_notes", {})
        if notes.get(key) != msg:
            notes[key] = msg
            log.log(level, "resting: %s", msg)

    def _rest_bank_refund(self, ra: dict[str, Any], why: str) -> None:
        """Reverse the collateral debit of a resting add that was cancelled / never filled (own-coin ledger)."""
        if not self.bank_on or self.live.dry_run or not ra.get("debited"):
            return
        b, amt = self.local.bank, float(ra.get("nat") or 0.0)
        b["bal"] = round(b["bal"] + amt, 9)
        b["trade_dep"] = round(b.get("trade_dep", 0.0) - amt, 9)
        bankmod.note(b, "refund", amt, why)
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
        L.n_entries += 1
        L.trade_opened_usd += lot["size"]
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
        add_gone = bool(ra) and ra.get("pubkey") not in on_chain
        delta = pos.size - L.last_size
        p_size = sum(float(p["size"]) for p in gone_p)
        resync = False
        if add_gone and ra is not None:
            add_size = float(ra["size_usd"])
            if delta + p_size > 0.5 * add_size:   # filled (also a submitted-but-unconfirmed order that landed and filled)
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
                L.last_leverage, L.last_entry = ra["lev"], round(fill, 6)
                L.last_size = round(L.last_size + add_size, 2)
                L.tp_hits, L.first_tp_time = 0, None   # fresh TP1 / TP2 ladder on the whole position (as after a market add)
                L.lots.append(lot)
                L.trade_opened_usd += add_size
                L.rest_add = None
                resync = True
                self.save()   # 2026-10-12: persist the booked fill at once (a restart must not lose the lot)
                log.warning("RESTING ADD FILLED: %s $%.2f x%g at %.4f (%s), n_entries %d, size now %.2f avg %.4f",
                            "long" if pos.side == LONG else "short", add_size, ra["lev"], fill, src, L.n_entries, pos.size,
                            pos.avg_entry)
            else:   # gone without a fill: cancelled / expired / never landed
                log.warning("resting add %s gone without a fill (size %.2f -> %.2f): record dropped, refund booked",
                            ra.get("pubkey"), L.last_size, pos.size)
                self._rest_bank_refund(ra, f"resting add {ra.get('pubkey')} gone unfilled")
                L.rest_add = None
        if gone_p:
            if pos.size < L.last_size - 0.01:   # partial trigger(s) executed
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
                L.lots = [l for l in L.lots if l["time"] not in times]
                if L.lots:
                    L.n_entries = len(L.lots)
                    L.last_entry, L.last_leverage = L.lots[-1]["price"], L.lots[-1].get("lev") or L.last_leverage
                last = max(gone_p, key=lambda p: pos.side * float(p["px"]))
                if R.last_partial_rule or R.partials_enabled:
                    L.last_partial_px = float(last["px"])
                L.partials_done += [{**p, "filled": str(pd.Timestamp.now(tz="UTC"))} for p in gone_p]
                L.last_size = pos.size
                self._tp_resize = True   # Trevor 2026-10-12 06:07 MT: re-size TP1/TP2 to the remaining position
                ledger.update(exit=f"P{len(L.partials_done)}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
                log.warning("PER-ADD PARTIAL FILLED: %s closed $%.2f at ~%s; payout est $%.4f; last partial price %s; "
                            "lots left %d, n_entries %d, last_entry %.4f", [p["pubkey"] for p in gone_p], closed,
                            [p["px"] for p in gone_p], usd, L.last_partial_px, len(L.lots), L.n_entries, L.last_entry)
            else:
                log.warning("per-add partial(s) %s gone without a size drop: records dropped", [p["pubkey"] for p in gone_p])
            gone = {p["pubkey"] for p in gone_p}
            L.partials = [p for p in L.partials if p["pubkey"] not in gone]
        if add_gone or gone_p:
            self.save()
            pos = self.chain_position(st) or pos
        return pos, resync

    def resting_tick(self, pos: ChainPosition, st: dict[str, Any], price: float, account: Any, equity: float) -> None:
        """Every tick with an open position: place what is missing (per-add partials, then the next resting add)."""
        R = self.cfg.resting
        if R.partials_enabled:
            self.place_missing_partials(pos, st, price)
        if R.adds_enabled and self.local.rest_add is None:
            self.place_rest_add(pos, st, price, account, equity)

    def rest_add_plan(self, pos: ChainPosition, cash: float) -> tuple[Any, str]:
        """(AddPlan | None, why-not) for the next resting add: the ladder's next rung, planned from the last fill (rule 3:
        from the more adverse of the last fill and the last partial price) with this bot's free collateral."""
        R, L = self.cfg.resting, self.local
        if self.cfg.dca.mode != "levels":
            return None, "dca.mode is not levels (no ladder)"
        p = pos
        if R.last_partial_rule and L.last_partial_px:
            import dataclasses as _dc
            p = _dc.replace(pos, last_entry=restmod.add_ref(pos.side, pos.last_entry, L.last_partial_px))
        plan = self.strategy.plan_next_add(p, cash, pd.Timestamp.now(tz="UTC"))
        if plan is None:
            big = self.strategy.plan_next_add(p, cash + bankmod.IB_MAX_USD, pd.Timestamp.now(tz="UTC"))
            if big is not None and big.margin > cash:   # Trevor 18:13 MT: short of equity -> borrow from a flat bot
                self.ib_request(min(big.margin - cash + 1.0, bankmod.IB_MAX_USD))   # what the rung needs, <= cap
            return None, "no ladder plan (budget / liq rule)"
        if not self.strategy._adds_left(pos):
            return None, "no adds left (max_adds / allow_after_partial)"
        if R.last_partial_rule and not restmod.beyond_last_partial(pos.side, plan.trigger, L.last_partial_px):
            return None, f"next rung {plan.trigger:.4f} is not beyond the last partial price {L.last_partial_px}"
        return plan, ""

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
        args = {"side": side, "inputMint": mint, "inputAtoms": str(atoms), "sizeUsd": round(plan.margin * plan.leverage, 2),
                "triggerPriceUsd": round(plan.trigger, 2)}
        return args, nat, sol

    def place_rest_add(self, pos: ChainPosition, st: dict[str, Any], price: float, account: Any, equity: float) -> None:
        L = self.local
        mine = {p.get("pubkey") for p in L.partials}
        untracked = [r for r in st["openRequests"] if r.get("requestChange") == "increase" and r.get("pubkey") not in mine]
        if untracked:
            self._rest_note("add", f"an increase request is already on-chain {[r.get('pubkey') for r in untracked]} "
                                   "(not tracked by this bot): no new resting add placed", logging.WARNING)
            return
        plan, why = self.rest_add_plan(pos, account.cash)
        if plan is None:
            self._rest_note("add", f"no resting add placed: {why}")
            return
        if self.add_pending_wait(st, pos):
            return
        it = Intent(Action.ADD, pos.side, plan.trigger, plan.margin, plan.leverage)
        why = self.guard(it, pos, st, equity)
        if why:
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
                        plan.leverage, args["sizeUsd"], plan.trigger, res["positionRequest"], res.get("sig"),
                        "" if res.get("ok") else ", NOT YET CONFIRMED")
            return
        self._rest_note("add", f"resting add creation refused / failed (retry next tick): "
                               f"{res.get('code') or ''} {res.get('error') or res.get('err')} args {args}", logging.WARNING)

    def partial_plan(self, pos: ChainPosition, lots: list[dict[str, Any]], lv: Any) -> dict[str, Any] | None:
        """Target of the ONE per-position partial TP covering ``lots`` (Trevor 23:10 MT): size = their total, entry = their
        size-weighted entry (Jupiter-style avg), N from the highest rung among them; spec rule otherwise (N-th trim level
        beyond the entry, tol before it, must cover fees)."""
        R, t, c = self.cfg.resting, self.cfg.trim, self.cfg.costs
        if not lots:
            return None
        idx = {l["time"]: i for i, l in enumerate(self.local.lots)}
        rung = max(int(l.get("rung") or max(1, idx.get(l.get("time"), 1))) for l in lots)
        size = sum(float(l["size"]) for l in lots)
        entry = size / sum(float(l["size"]) / float(l["price"]) for l in lots)
        # 2026-10-12 00:53 MT: target between the covered entry and the trade avg (zone rule), replaces N levels beyond
        tgt = restmod.partial_target_zone(pos.side, entry, pos.avg_entry, size, lv, rung, self.cfg.dca.ladder_max_rungs,
                                          R.partial_zone_f_max, tol_atr=t.trim_level_tol_atr,
                                          front_atr=t.trim_level_front_run_atr, front_pct=t.trim_level_front_run_pct,
                                          open_fee=c.open_fee, close_fee=c.close_fee, network_usd=t.trim_network_usd)
        if tgt.get("px") is None:
            self._rest_note("partial_zone", f"TP3 for adds (entry {entry:.4f}, rung {rung}): {tgt.get('why')}", logging.WARNING)
            return None
        tgt.update(rung=rung, entry=round(entry, 6), size=round(size, 2), n=0)
        return tgt

    def _place_partial(self, pos: ChainPosition, price: float, lots: list[dict[str, Any]], why: str) -> bool:
        L = self.local
        lv = self.strategy.levels_provider(pd.Timestamp.now(tz="UTC")) if self.strategy.levels_provider else None
        tgt = self.partial_plan(pos, lots, lv)
        desc = [f"@{float(l['price']):.4f} ${l['size']}" for l in lots]
        if tgt is None:
            self._rest_note("partial", f"partial for adds {desc}: no target between entry and avg; retry next tick")
            return False
        if pos.side * (tgt["px"] - price) < self.cfg.tp.tp_market_gap - 1e-9:
            self._rest_note("partial", f"partial for adds {desc}: target {tgt['px']} not beyond market {price:.4f}; retry")
            return False
        side = "long" if pos.side == LONG else "short"
        args = {"side": side, "sizeUsd": tgt["size"], "triggerPriceUsd": tgt["px"], "triggerAbove": pos.side == LONG}
        res = self.cmd("decrease", args)
        if self.live.dry_run:
            self._rest_note("partial", f"DRY RUN partial {args} ({why}; {tgt['mode']} {tgt['level']}, f {tgt['f']}, rung {tgt['rung']})")
            return False
        if not res.get("ok"):
            self._rest_note("partial", f"partial placement failed (retry next tick): {res.get('err') or res.get('error')}",
                            logging.WARNING)
            return False
        L.partials = [{"pubkey": res.get("positionRequest"), "lots": [l["time"] for l in lots], "lot_time": lots[-1]["time"],
                       "size": args["sizeUsd"], "px": tgt["px"], "level": tgt["level"], "n": tgt["n"], "rung": tgt["rung"],
                       "entry": tgt["entry"], "placed": str(pd.Timestamp.now(tz="UTC")), "sig": res.get("sig"), "why": why}]
        self.save()
        self.__dict__.setdefault("_rest_notes", {}).pop("partial", None)
        log.warning("PARTIAL TP (TP3) placed (%s): adds %s = $%.2f, entry %.4f avg %.4f (rung %d, f %.3f, zone to %.4f) -> %s "
                    "%s, trigger %.4f (net est $%.4f), req %s", why, desc, tgt["size"], tgt["entry"], tgt["avg"], tgt["rung"],
                    tgt["f"], tgt["zone_end"], tgt["mode"], tgt["level"], tgt["px"], tgt["net_usd"], res.get("positionRequest"))
        return True

    def place_missing_partials(self, pos: ChainPosition, st: dict[str, Any], price: float) -> None:
        """ONE partial TP per position (Trevor 23:09 / 23:10 MT): placed when the trim is armed (existing arm rule, sticky
        until flat) and >= 1 add is trim-eligible (trim.select_lots at the current price; never the first entry); it covers
        the eligible adds. Otherwise never touched here (no toggling with the arm state); a new add updates it
        (partial_on_add)."""
        L, t, c = self.local, self.cfg.trim, self.cfg.costs
        if L.partials:
            return
        if not (L.trim_armed or trimmod.arm_hit(pos.side, pos.avg_entry, price, t.trim_arm_dist_pct)):
            return
        elig = set(trimmod.select_lots(L.lots, pos.side, price, t, c.open_fee, c.close_fee))
        due = [l for i, l in enumerate(L.lots) if i in elig and not (l.get("first") or i == 0)]
        if due:
            self._place_partial(pos, price, due, "armed + eligible adds")

    def partial_on_add(self, pos: ChainPosition, price: float, new_lot: dict[str, Any]) -> None:
        """A new add filled while the single partial TP exists: cancel + replace it so it also covers the new add (the
        adds it covered that are still open + the new one), re-targeted. No partial yet -> nothing (placed when armed)."""
        L = self.local
        if not (self.cfg.resting.partials_enabled and L.partials):
            return
        old = L.partials[0]
        c = self.cancel_request(old["pubkey"], pos.size)
        if c != "ok":
            log.warning("partial update on add: cancel of %s returned %s; not replaced (next tick reconciles)", old["pubkey"], c)
            return
        L.partials = []
        self.save()
        keep = set(old.get("lots") or [old.get("lot_time")])
        lots = [l for l in L.lots if l["time"] in keep or l["time"] == new_lot["time"]]
        self._place_partial(pos, price, lots, "updated for new add")

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
        else:
            log.info("BANK payout event %s (~$%.4f): window %d ticks for the own-coin change", why, usd, b["window"])
        self.save()

    def bank_flat(self, flows: float | None) -> None:
        """Trade over. SOL bot: true-up with the position's own on-chain SOL flows (exact). Reset the trade counters."""
        if not self.bank_on or self.live.dry_run:
            return
        b = self.local.bank
        if self.asset == "SOL" and flows is not None:
            adj = round(flows - (b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0)), 9)
            if abs(adj) <= 0.02:
                bankmod.credit(b, adj, "flat_trueup", f"position flows {flows:+.9f} vs booked "
                               f"{b.get('trade_recv', 0.0) - b.get('trade_dep', 0.0):+.9f}")
                if b.get("res_last") is not None:
                    b["res_last"] = round(b["res_last"] - adj, 9)   # moved between reserve float and bal
                log.info("BANK flat true-up %+.9f SOL (position flows %+.9f)", adj, flows)
            else:
                log.warning("BANK flat true-up %+.9f SOL too large (> 0.02); not applied (estimate kept)", adj)
        if b.get("book_pend"):   # ETH / BTC: counters kept until the deferred own-coin booking (bank_book_pending)
            b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
            self.save()
            return
        log.info("BANK trade done: deposited %.9f, received %.9f %s (net %+.9f)", b.get("trade_dep", 0.0),
                 b.get("trade_recv", 0.0), b["coin"], b.get("trade_recv", 0.0) - b.get("trade_dep", 0.0))
        b["trade_dep"], b["trade_recv"] = 0.0, 0.0
        b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)
        self.save()

    def bank_sweep_booked(self, bk: dict[str, Any], st: dict[str, Any]) -> None:
        if not self.bank_on:
            return
        b = self.local.bank
        nat = bk.get("native")
        b["window"] = max(b.get("window", 0), self.cfg.ledger.window_ticks)   # ETH / BTC: the coin lands next read
        if self.asset == "SOL" and nat is not None:
            if bk.get("unattributed") and self.cfg.ledger.ext_flow_mode == "alert":
                line = bankmod.write_alert("SOL", f"unattributed USDC {bk.get('usd')} swept to {nat:.9f} SOL; banks untouched "
                                           "(ext_flow_mode=alert)")
                log.warning("BANK %s", line)
            elif bk.get("unattributed"):   # USDC nobody's short produced -> external deposit, split 50/25/25
                ev = bankmod.post_external(nat, self.sol_price(st), tuple(self.cfg.ledger.ext_split),
                                           f"unattributed USDC {bk.get('usd')} swept to SOL")
                bankmod.apply_external(b, ev["shares"]["SOL"], f"ext #{ev['id']} (unattributed USDC)")
                log.warning("BANK external (unattributed USDC -> %.9f SOL): event #%d shares %s", nat, ev["id"], ev["shares"])
            else:
                bankmod.credit(b, nat, "payout", f"USDC -> SOL sweep {bk.get('sig')}")
        self.save()

    def _busy(self, st: dict[str, Any], all_markets: bool) -> bool:
        reqs = st.get("allOpenRequests", st["openRequests"]) if all_markets else st["openRequests"]
        if [r for r in reqs if r.get("requestType") != "trigger"]:
            return True
        usdc = int(((st.get("sweepTokens") or {}).get("USDC") or {}).get("atoms") or 0)
        return usdc / 1e6 >= self.cfg.bot.sweep_min_usd

    def bank_tick(self, st: dict[str, Any], pos: ChainPosition | None, price: float) -> None:
        """Reconcile this bot's balance with the wallet, settle external-event shares, raise the HWM when flat."""
        b, lc = self.local.bank, self.cfg.ledger
        if self.live.dry_run:
            return
        sol_px, cpx = self.sol_price(st), self.coin_px(st)
        mid_loan = self.ib_tick(st, pos)
        if mid_loan:
            pass
        elif self.asset == "SOL":
            j = bankmod.load_journal()
            r = bankmod.recon_sol(b, float(st["wallet"]["sol"]), bankmod.pending_others(j), sol_px, lc.ext_min_usd,
                                  self._busy(st, True))
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
        if b.get("window", 0) > 0:
            b["window"] -= 1
        flat = pos is None and b.get("window", 0) == 0 and not self._busy(st, self.asset == "SOL")
        if flat and b.get("book_pend") and not b.get("pend"):
            self.bank_book_pending(sol_px, cpx)
        if flat and bankmod.loan_outstanding(b) > 0 and not b.get("pend"):   # JupSOL rescue: repay + restore settings
            try:
                from live import rescue
                rescue.auto_repay(self)
            except Exception as e:   # noqa: BLE001 - retried next tick
                log.warning("RESCUE auto-repay failed (retry next tick): %s", e)
        if flat and b.get("ib_loan", {}).get("role") == "borrower" and not b.get("pend") and not b.get("book_pend"):
            try:
                self.ib_borrower_repay(sol_px, cpx)
            except Exception as e:  # noqa: BLE001 - retried next tick
                log.error("INTER-BOT LOAN repay failed (retry next tick): %s", e)
        if bankmod.update_hwm(b, flat, bankmod.fee_debt_native(b, sol_px, cpx)):
            log.info("BANK HWM raised (flat): %s", bankmod.status_line(b, cpx, sol_px))
        self.save()

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
                if s == "funding" and rec.get("borrower") == me:
                    return True
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
                if s == "requested" and rec.get("borrower") != me and pos is None and not b.get("ib_loan") \
                        and not self._busy(st, me == "SOL") and b.get("window", 0) == 0:
                    px = self.coin_px(st)
                    need = float(rec["need_usd"]) / px
                    if (b["bal"] - bankmod.loan_outstanding(b)) * px < float(rec["need_usd"]) + 5.0:
                        return False
                    rec.update(status="funding", lender=me, lender_coin=coin)
                    self._ib_write(rec)
                    res, kind, att = sweepmod.swap_with_backoff(
                        self.cfg, {"mint": bankmod.MINT[coin], "atoms": int(need * 10 ** bankmod.DECIMALS[coin])}, jup,
                        out_mint=bankmod.MINT[rec["coin_name"]])
                    self.bank_fee((res.get("deltas") or {}).get("feeLamports") or res.get("feeLamports"))
                    if kind != "ok":
                        rec.update(status="requested", lender=None, lender_coin=None)
                        self._ib_write(rec)
                        log.error("INTER-BOT LOAN funding swap failed (%s): %s; request left open", kind, att)
                        return False
                    spent = int(need * 10 ** bankmod.DECIMALS[coin]) / 10 ** bankmod.DECIMALS[coin]
                    got = self._swap_got(res, rec["coin_name"])
                    now = str(pd.Timestamp.now(tz="UTC"))
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

    def ib_request(self, need_usd: float) -> None:
        """Borrower side: not enough free for the next rung -> ask a flat bot for ``need_usd`` (one loan at a time)."""
        b = self.local.bank
        if b.get("ib_loan") or not self.bank_on or self.live.dry_run:
            return
        try:
            with bankmod.journal_lock():
                rec = self._ib_read()
                if rec.get("status") not in (None, "settled"):
                    return
                self._ib_write({"status": "requested", "borrower": self.asset, "coin_name": b["coin"],
                                "need_usd": round(need_usd, 2), "requested": str(pd.Timestamp.now(tz="UTC"))})
            log.error("INTER-BOT LOAN requested: %s needs $%.2f for its next rung", self.asset, need_usd)
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
        return g.close_requires == "any" or L.n_entries > 1 or len(L.lots) > 1 or bool(L.trims)

    def hwm_equity(self, pos: ChainPosition | None, st: dict[str, Any], price: float) -> float:
        b, c, lc = self.local.bank, self.cfg.costs, self.cfg.ledger
        sol_px, cpx = self.sol_price(st), self.coin_px(st)
        cv = 0.0
        if pos is not None:
            fill = price * (1 - pos.side * c.slippage)
            swap = lc.swap_cost_pct if (pos.side == SHORT and self.asset != "SOL") else 0.0
            net = lc.close_network_native if self.asset == "SOL" else 0.0
            cv = bankmod.close_value_native(pos.side, pos.size, pos.avg_entry, pos.margin, fill, c.close_fee, cpx, swap, net)
        return bankmod.equity(b, cv, bankmod.fee_debt_native(b, sol_px, cpx))

    def guard_min_native(self) -> float:
        """Profit-guard margin above HWM (native). pct mode (2026-10-12): hwm x (pct + per_partial x partials this trade)."""
        g = self.cfg.ledger
        if g.min_profit_pct_hwm > 0:
            n = len(self.local.trims) + len(self.local.partials_done)
            return self.local.bank["hwm"] * (g.min_profit_pct_hwm + g.min_profit_pct_per_partial * n)
        return g.min_profit_native

    def hwm_check(self, pos: ChainPosition, st: dict[str, Any], price: float, min_native: float) -> tuple[bool, str]:
        """Equity if closed now (own coin) >= hwm + min_native? (guard: min_profit_native; TP timer: 0 = not below HWM)."""
        b = self.local.bank
        eq = self.hwm_equity(pos, st, price)
        tgt = b["hwm"] + min_native
        return eq >= tgt - 1e-12, f"HWM guard: equity if closed now {eq:.9f} {b['coin']} vs hwm {b['hwm']:.9f} + {min_native:g} = {tgt:.9f}"

    def bank_status(self, st: dict[str, Any], pos: ChainPosition | None, price: float) -> str:
        b = self.local.bank
        eq = self.hwm_equity(pos, st, price)
        tgt = b["hwm"] + self.guard_min_native() if pos is not None else None
        arm = (" | guard " + ("ARMED (adds/trims)" if self.hwm_close_armed() else "not armed (first entry: TPs only)")) if pos else ""
        return bankmod.status_line(b, self.coin_px(st), self.sol_price(st), eq, tgt) + arm

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
            if self.bank_on and L.pg_m_size > 0:   # last seen size / collateral; avg from the lots
                avg = (sum(l["size"] for l in L.lots) / sum(l["size"] / l["price"] for l in L.lots)) if L.lots else price
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
        if pos is not None and L.side == pos.side and L.tp_retry:
            log.error("TP retry: re-placing TP1/TP2 on the open position (previous placement failed / not confirmed)")
            L.tp_retry = False
            self.sync_tp_ladder(pos)
            self.save()
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

        if pos is not None:   # baseline guard: remember size / collateral / realised PnL for the next TP fill
            rp = st["positions"]["long" if pos.side == LONG else "short"].get("realisedPnlUsd")
            L.pg_m_size, L.pg_margin = pos.size, pos.margin
            L.pg_rpnl = float(rp) if rp is not None else None
            if self.cfg.resting.any_on:
                L.rest_last_avg = pos.avg_entry
        if getattr(self, "_tp_resize", False) and pos is not None and not rest_resync:
            self._tp_resize = False
            self.resize_tps(self.chain_position(self.chain()) or pos)
        if rest_resync and pos is not None:   # an add filled via its order: fresh TP1/TP2 ladder
            self.sync_tp_ladder(self.chain_position(self.chain()) or pos)   # (exactly as after a market add)
            if L.lots:
                self.partial_on_add(pos, price, L.lots[-1])
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
            # Trevor 2026-10-11 06:31 MT: the guard only fires while underwater (long price < avg, short price > avg);
            # at/through avg on the profit side TP1/TP2 (and Grok Bot's TP2 moves) decide the exit.
            guard_ok = self.guard_allowed(pos, price)
            if not guard_ok:
                pass
            elif self.bank_on:   # HWM profit guard (2026-10-08 21:37 MT): close all once equity >= hwm + min (never moves TPs)
                if self.hwm_close_armed():
                    ok, note = self.hwm_check(pos, st, price, self.guard_min_native())
                    if ok:
                        self.close(pos, price, f"profit_guard ({note})")
                        return
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
            retp = self.dir / "RETP_NOW"   # one-shot manual TP re-place (Grok Bot / owner), via the normal placement path
            if retp.exists():
                retp.unlink(missing_ok=True)
                log.warning("RETP_NOW: re-placing TP ladder once on the open position (manual request)")
                self.sync_tp_ladder(pos)
                return
            if self.cfg.tp.smart_tp2 and getattr(self.cfg.tp, "tp_periodic", True):
                try:
                    self.check_tps(pos, st, price)
                except Exception as e:  # noqa: BLE001 - a failed check leaves the placed TPs as they are
                    log.warning("TP check failed: %s", e)
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
        if pos is not None and self.cfg.tp.smart_tp2:   # momentum tracking for the TP2 fade (bars after placement)
            L.tp_rsi_now = float(bar.rsi)
            L.tp_rsi_ext = bar.rsi if L.tp_rsi_ext is None else (max(L.tp_rsi_ext, bar.rsi) if pos.side == LONG
                                                                 else min(L.tp_rsi_ext, bar.rsi))
            if L.tp_since and bar.time >= pd.Timestamp(L.tp_since):   # bars opened after placement only
                hi = bar.high if pos.side == LONG else bar.low
                L.tp_peak = hi if not L.tp_peak else (max(L.tp_peak, hi) if pos.side == LONG else min(L.tp_peak, hi))
            log.info("TP state: placed %s, best %.3f, rsi now %.1f ext %.1f, faded %s", L.tp_px, L.tp_peak,
                     L.tp_rsi_now, L.tp_rsi_ext, L.tp_faded)
            self.save()
        for it in self.strategy.exit_intents(bar, account):
            if pos is not None:
                self.close(pos, price, it.reason.value if it.reason else "exit")
            return
        intents = self.strategy.entry_intents(bar, account)
        if pos is not None and self.cfg.dca.mode == "levels":
            p = self.strategy.plan_next_add(pos, account.cash, pd.Timestamp.now(tz="UTC"))
            log.info("next add plan: %s", p and (f"{p.source} trigger {p.trigger:.2f} margin ${p.margin:.2f} x{p.leverage:g} "
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
