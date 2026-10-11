"""Live Jupiter Perps bot loop (SOL). Ticks every ``TICK_SEC``; strategy evaluated on each closed signal bar.

On-chain state is the source of truth: every tick re-reads the position + requests and reconciles the
local state (``live/state.json``). Any unexpected inconsistency or failed action writes the KILL file
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

import urllib.request

STATE = ROOT / "live" / "state.json"
TICK_SEC = 60                     # timer / stop / reconciliation cadence
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
        log.error("HALT (KILL written): %s", why)
        self.kill.write_text(f"{pd.Timestamp.now(tz='UTC')} {why}\n")

    def cmd(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Send when live, simulate when dry_run (same builders)."""
        live = not self.live.dry_run
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
        ok = True
        size = self._chain_size(st) if st["openRequests"] else 0.0
        for r in st["openRequests"]:
            ok &= self.cancel_request(r["pubkey"], size) == "ok"
        return ok

    def _chain_size(self, st: dict[str, Any]) -> float:
        p = self.chain_position(st)
        return p.size if p is not None else 0.0

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
            self.halt("cancel before close failed")
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
                           trim_day_n=self.local.trim_day_n, bank=self.local.bank)

    def sync_tp_ladder(self, pos: ChainPosition) -> None:
        """Cancel existing TP triggers and place the full ladder on the whole position (strategy.tp_targets:
        normal ladder on the average, above-water TPs after a deep levels-mode add, smart TP2 / fee floors / never
        past market when tp.smart_tp2). Resets the momentum-fade tracking."""
        st = self.chain()
        if not self.cancel_all(st):
            self.halt("TP cancel failed")
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
        return [r for r in st["openRequests"] if r.get("triggerPrice") and not r.get("executed")]

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
                self.halt(f"TP{i + 1} cancel failed")
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
        res = self.cmd("increase", args)
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
                               trim_day=L.trim_day, trim_day_n=L.trim_day_n, bank=L.bank,
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
        log.info("LOT added #%d: %s fill %.4f size $%.2f coll $%.2f x%g", len(self.local.lots), lot["time"][:19],
                 lot["price"], lot["size"], lot["coll"], lot["lev"])
        self.save()
        new = self.chain_position(self.chain())
        assert new is not None
        self.sync_tp_ladder(new)

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
            if bk.get("unattributed"):   # USDC nobody's short produced -> external deposit, split 50/25/25
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
        if self.asset == "SOL":
            j = bankmod.load_journal()
            r = bankmod.recon_sol(b, float(st["wallet"]["sol"]), bankmod.pending_others(j), sol_px, lc.ext_min_usd,
                                  self._busy(st, True))
            if r["action"] == "external":
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
                r = bankmod.recon_token(b, atoms / 10 ** bankmod.DECIMALS[coin], cpx, lc.ext_min_usd, self._busy(st, False))
                if r["action"] in ("payout", "adjust"):
                    log.info("BANK %s %+.9f %s (bal %.9f)", r["action"], r["diff"], coin, b["bal"])
                elif r["action"] == "external":
                    log.warning("BANK EXTERNAL %s change %+.9f (~$%+.2f) -> this bot's capital + hwm (not profit)", coin,
                                r["diff"], r["diff"] * cpx)
                elif r["action"] == "pending":
                    log.info("BANK: unexplained %s change %+.9f; re-checking next tick", coin, r["diff"])
            if not self._busy(st, False):
                self.bank_settle_external(st, sol_px, cpx)
        if b.get("window", 0) > 0:
            b["window"] -= 1
        flat = pos is None and b.get("window", 0) == 0 and not self._busy(st, self.asset == "SOL")
        if flat and b.get("book_pend") and not b.get("pend"):
            self.bank_book_pending(sol_px, cpx)
        if bankmod.update_hwm(b, flat, bankmod.fee_debt_native(b, sol_px, cpx)):
            log.info("BANK HWM raised (flat): %s", bankmod.status_line(b, cpx, sol_px))
        self.save()

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
        amt, gain = bankmod.withdraw_amount(b, lc.withdraw_fraction, lc.withdraw_min_usd / cpx)
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
            rec.setdefault("skip", f"below min ${lc.withdraw_min_usd:.2f} (carried)")
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

    def hwm_check(self, pos: ChainPosition, st: dict[str, Any], price: float, min_native: float) -> tuple[bool, str]:
        """Equity if closed now (own coin) >= hwm + min_native? (guard: min_profit_native; TP timer: 0 = not below HWM)."""
        b = self.local.bank
        eq = self.hwm_equity(pos, st, price)
        tgt = b["hwm"] + min_native
        return eq >= tgt - 1e-12, f"HWM guard: equity if closed now {eq:.9f} {b['coin']} vs hwm {b['hwm']:.9f} + {min_native:g} = {tgt:.9f}"

    def bank_status(self, st: dict[str, Any], pos: ChainPosition | None, price: float) -> str:
        b = self.local.bank
        eq = self.hwm_equity(pos, st, price)
        tgt = b["hwm"] + self.cfg.ledger.min_profit_native if pos is not None else None
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

        # reconcile with chain
        if pos is None and L.side:
            log.info("position gone on-chain (TP2/stop/liquidation/manual); clearing local state")
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
            if st["openRequests"]:
                self.cancel_all(st)
            self.local = L = Local(last_bar=L.last_bar, day=L.day, day_start_equity=L.day_start_equity,
                                   flat_since=str(pd.Timestamp.now(tz="UTC")), trim_day=L.trim_day, trim_day_n=L.trim_day_n,
                                   bank=L.bank)
        elif pos is not None and not L.side:
            self.halt("on-chain position not opened by this bot")
            return
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
                    ok, note = self.hwm_check(pos, st, price, self.cfg.ledger.min_profit_native)
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
        for it in intents:
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
