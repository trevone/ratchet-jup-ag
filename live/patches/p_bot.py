"""Integration patch (multi-asset) for live/bot.py. Usage: python p_bot.py <root>"""
import sys
from pathlib import Path
R = Path(sys.argv[1])
F = "live/bot.py"


def rep(old, new, count=1):
    p = R / F
    s = p.read_text()
    n = s.count(old)
    if n != count:
        raise SystemExit(f"{F}: expected {count} occurrence(s), found {n}: {old[:90]!r}")
    p.write_text(s.replace(old, new))


rep('''    .venv/bin/python -m live.bot --config configs/live_100.toml
"""''', '''    .venv/bin/python -m live.bot --config configs/live_100.toml      # SOL (state in live/)
    .venv/bin/python -m live.bot --config configs/live_eth.toml      # ETH (state in live/eth/)
    .venv/bin/python -m live.bot --config configs/live_btc.toml      # BTC (state in live/btc/)

Multi-asset (2026-10-07): one process per market, all on the same wallet. [bot] asset/share/state_dir pick the Jupiter
market, the wallet share (live/budget.py) and the state dir; SOL is the default (paths unchanged). Margin is always
native SOL: every USD <-> SOL conversion uses the SOL oracle, the market oracle only prices the market.
"""''')
rep('''from strategy import LONG, SHORT, Action, DcaStrategy, Intent
from dca_levels import TpFade, load_analyst_levels
''', '''from strategy import LONG, SHORT, Action, DcaStrategy, Intent
from dca_levels import TpFade, load_analyst_levels
from asset_scale import atr_from_klines, levels_with_fallback, scale_config, scaled_summary
from live.budget import budget_from_state
from live.flows import position_net_sol
from live import sweep as sweepmod
''')
rep('''LAMPORTS = 1_000_000_000
USDC_ATOMS = 1_000_000
''', '''LAMPORTS = 1_000_000_000
USDC_ATOMS = 1_000_000
COLLATERAL_DECIMALS = {"ETH": 8, "BTC": 8}   # long collateral token of the ETH / WBTC custodies (verified on-chain)
''')
rep('''class ChainAccount:  # satisfies strategy.AccountView
    def __init__(self, sol: float, price: float, pos: ChainPosition | None, reserve: float) -> None:
        self._cash = max(0.0, (sol - reserve) * price)''', '''class ChainAccount:  # satisfies strategy.AccountView
    def __init__(self, sol: float, price: float, pos: ChainPosition | None, reserve: float, cash: float | None = None) -> None:
        # cash = this bot's free collateral (live/budget.py: share of the shared wallet minus own open collateral);
        # None -> old single-bot rule (native SOL - reserve) * price
        self._cash = max(0.0, (sol - reserve) * price) if cash is None else max(0.0, cash)''')
rep('''        self.strategy = DcaStrategy(cfg)
        if cfg.dca.mode == "levels":   # analyst S/R + 1h ATR, re-read every call; None (-> fixed spacing) if stale
            self.strategy.levels_provider = lambda now: load_analyst_levels(cfg.dca.levels_path, cfg.dca.levels_max_age_min)
        self.local = load_local(STATE)
        self._tp_checked: pd.Timestamp | None = None
        self.kill = ROOT / self.live.kill_switch_path
        self._first_entry_logged = False''', '''        self.base_cfg = cfg                       # [scale] re-derives price distances from this every tick
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
            self._own_atr = a''')
rep('''    def save(self) -> None:
        STATE.write_text(json.dumps(asdict(self.local), indent=1))''', '''    def save(self) -> None:
        self.state_path.write_text(json.dumps(asdict(self.local), indent=1))''')
rep('''        live = not self.live.dry_run
        res = jup(name if live else f"simulate-{name}", {**args, "send": live})''', '''        live = not self.live.dry_run
        res = jup(name if live else f"simulate-{name}", {**args, "market": self.asset, "send": live})''')
rep('''        st = jup("state")
        if "wallet" not in st:
            raise RuntimeError(f"state read failed: {st}")
        return st''', '''        st = jup("state", {"market": self.asset})
        if "wallet" not in st:
            raise RuntimeError(f"state read failed: {st}")
        if st.get("market", "SOL") != self.asset:
            raise RuntimeError(f"state is for market {st.get('market')} not {self.asset}")
        return st

    @staticmethod
    def sol_price(st: dict[str, Any]) -> float:
        """SOL/USD (margin is native SOL for every market)."""
        return float((st.get("solOracle") or {}).get("price") or st["oracle"]["price"])''')
rep('''        row = ledger.on_flat(str(pd.Timestamp.now(tz="UTC")), st["wallet"]["sol"], st["oracle"]["price"],
                             rec.get("realisedPnlUsd"), hourly)
        if row:
            log.info("LEDGER trade booked: net %+.9f SOL (~$%+.4f), exits %s", row["net_sol"], row["net_usd_at_close"], row["exits"])''',
'''        flows = None
        try:   # this position's own txs since its open tx (shared wallet: other bots / deposits / stakes excluded)
            o = json.loads(ledger.OPEN.read_text()) if ledger.OPEN.exists() else {}
            flows, fres = position_net_sol(o.get("position") or rec.get("pubkey"), o.get("open_sig"))
            if flows is not None:
                log.info("LEDGER position flows: %d txs, net %+.9f SOL", fres.get("nTx", 0), flows)
        except Exception as e:  # noqa: BLE001 - fall back to the wallet delta
            log.warning("position flows failed: %s", e)
        row = ledger.on_flat(str(pd.Timestamp.now(tz="UTC")), st["wallet"]["sol"], self.sol_price(st),
                             rec.get("realisedPnlUsd"), hourly, flows_net_sol=flows)
        if row:
            log.info("LEDGER trade booked: net %+.9f SOL (~$%+.4f) [%s], exits %s", row["net_sol"], row["net_usd_at_close"],
                     row.get("net_sol_method"), row["exits"])''')
rep('''        if self.kill.exists():
            return "kill switch present"''', '''        if self.killed():
            return "kill switch present"''')
rep('''        price = st["oracle"]["price"]
        if margin_sol(st) - it.margin / price < lv.sol_fee_reserve:
            return "would breach SOL fee reserve"
        return None''', '''        if margin_sol(st) - it.margin / self.sol_price(st) < lv.sol_fee_reserve:
            return "would breach SOL fee reserve"
        if self.budget is not None and it.margin > self.budget.free_usd + 0.01:
            return f"collateral {it.margin:.2f} > this bot's free budget {self.budget.free_usd:.2f} (share {self.budget.share:.0%})"
        return None''')
rep('''        f = ROOT / "live" / "PAUSE_UNTIL"
        if not f.exists():
            return None
        try:
            until = pd.Timestamp(f.read_text().strip())
            until = until.tz_localize("America/Denver") if until.tzinfo is None else until
        except ValueError:
            return "PAUSE_UNTIL unreadable -> paused"
        return f"entries paused until {until}" if pd.Timestamp.now(tz="UTC") < until else None''', '''        for f in [self.dir / "PAUSE_UNTIL", *(ROOT / x for x in self.cfg.bot.extra_pause_files)]:
            if not f.exists():
                continue
            try:
                until = pd.Timestamp(f.read_text().strip())
                until = until.tz_localize("America/Denver") if until.tzinfo is None else until
            except ValueError:
                return f"{f.name} ({f.parent}) unreadable -> paused"
            if pd.Timestamp.now(tz="UTC") < until:
                return f"entries paused until {until} ({f})"
        return None''')
rep('''        price = st["oracle"]["price"]
        slip = self.live.price_slippage_pct
        side = "long" if it.side == LONG else "short"
        sol = it.margin / price
        args: dict[str, Any] = {"side": side, "collateralAtoms": str(int(sol * LAMPORTS)),
                                "sizeUsd": round(it.margin * it.leverage, 2),
                                "priceSlippageUsd": round(price * (1 + slip) if it.side == LONG else price * (1 - slip), 2)}
        if it.side == SHORT:  # SOL input swapped to USDC collateral by the request
            args["jupiterMinimumOut"] = str(int(sol * price * (1 - slip) * USDC_ATOMS))''', '''        price = st["oracle"]["price"]          # market price (SOL / ETH / BTC)
        slip = self.live.price_slippage_pct
        side = "long" if it.side == LONG else "short"
        sol = it.margin / self.sol_price(st)   # collateral is paid in native SOL
        args: dict[str, Any] = {"side": side, "collateralAtoms": str(int(sol * LAMPORTS)),
                                "sizeUsd": round(it.margin * it.leverage, 2),
                                "priceSlippageUsd": round(price * (1 + slip) if it.side == LONG else price * (1 - slip), 2)}
        if it.side == SHORT:  # SOL input swapped to USDC collateral by the request
            args["jupiterMinimumOut"] = str(int(it.margin * (1 - slip) * USDC_ATOMS))
        elif self.asset != "SOL":  # long ETH/BTC: SOL input swapped to the custody token (ETH / WBTC) by the request
            args["jupiterMinimumOut"] = str(int(it.margin * (1 - slip) / price * 10 ** COLLATERAL_DECIMALS[self.asset]))''')
rep('''            ledger.on_open(str(pd.Timestamp.now(tz="UTC")), it.side, st["wallet"]["sol"], res.get("sig"), res.get("feeLamports"))''',
'''            ledger.on_open(str(pd.Timestamp.now(tz="UTC")), it.side, st["wallet"]["sol"], res.get("sig"), res.get("feeLamports"),
                           position=st["positions"][side].get("pubkey"))''')
# tick: rescale + budget account
rep('''        price = o["price"]
        pos = self.chain_position(st)
        L = self.local
''', '''        price = o["price"]
        sol_px = self.sol_price(st)
        self.rescale(price)
        pos = self.chain_position(st)
        L = self.local
''')
rep('''        account = ChainAccount(margin_sol(st), price, pos, self.live.sol_fee_reserve)   # native SOL only''',
'''        # this bot's free collateral: share of (native SOL - reserve + every bot's open collateral) - own collateral,
        # capped at the native SOL really free (live/budget.py); share 1.0 alone = the old native-SOL rule
        self.budget = budget_from_state(st, self.cfg.bot.share, self.live.sol_fee_reserve, self.asset,
                                        own_pos_collateral=pos.margin if pos else 0.0)
        account = ChainAccount(margin_sol(st), sol_px, pos, self.live.sol_fee_reserve, cash=self.budget.free_usd)''')
rep('''        if self.cfg.withdraw.enabled and not self.kill.exists():''', '''        if self.cfg.withdraw.enabled and not self.killed():''')
rep('''            wrec = run_withdraw(self.cfg, wstate, st, pd.Timestamp.now(tz="UTC").to_pydatetime(), next_add_sol=next_usd / price)
            wpath.write_text(json.dumps(wstate, indent=1))
            if wrec and wrec.get("staked_sol"):   # SOL moved to the stake bucket is not a daily equity loss
                L.day_start_equity -= wrec["staked_sol"] * price''', '''            wrec = run_withdraw(self.cfg, wstate, st, pd.Timestamp.now(tz="UTC").to_pydatetime(), next_add_sol=next_usd / sol_px)
            wpath.write_text(json.dumps(wstate, indent=1))
            if wrec and wrec.get("staked_sol"):   # SOL moved to the stake bucket is not a daily equity loss
                L.day_start_equity -= wrec["staked_sol"] * sol_px''')
rep('''        if self.kill.exists():
            if pos is not None and self.live.close_all_on_kill:''', '''        if self.killed():
            if pos is not None and self.live.close_all_on_kill:''')
rep('''        if not self._first_entry_logged:   # once at start; then on every bar line below
            log.info(self.first_entry_line(account.cash))
            if pos is None:
                self.strategy._flat_since = pd.Timestamp(L.flat_since) if L.flat_since else None
                log.info("entry thresholds now: %s", self.strategy.thresholds_at(pd.Timestamp.now(tz="UTC"))[2])
            self._first_entry_logged = True

        # strategy on each newly closed signal bar
        feats = compute_features(fetch_klines(self.cfg.market.symbol, self.cfg.signal.tf), self.cfg)''', '''        # strategy on each newly closed signal bar
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
        feats = compute_features(kl, self.cfg)''')
rep('''        log.info("bar %s close=%.3f rsi=%.1f trend_up=%s oracle=%.3f equity=$%.2f pos=%s | wallet %.6f SOL native (margin) | %s",
                 bar.time, bar.close, bar.rsi, bar.trend_up, price, equity,
                 pos and f"{pos.side:+d} ${pos.size:.2f} avg {pos.avg_entry:.3f}", margin_sol(st), stake_line(st))
        log.info(self.first_entry_line(account.cash))''', '''        log.info("bar %s %s close=%.3f rsi=%.1f trend_up=%s oracle=%.3f equity=$%.2f pos=%s | wallet %.6f SOL native (margin) | %s",
                 self.asset, bar.time, bar.close, bar.rsi, bar.trend_up, price, equity,
                 pos and f"{pos.side:+d} ${pos.size:.2f} avg {pos.avg_entry:.3f}", margin_sol(st),
                 stake_line(st) if self.cfg.withdraw.enabled else f"SOL {sol_px:.3f}")
        log.info(self.budget.line(self.asset))
        if self.base_cfg.scale.enabled:
            log.info("scaled: own 1h ATR %s | %s", f"{self._own_atr:.3f}" if self._own_atr else "n/a", scaled_summary(self.cfg))
        log.info(self.first_entry_line(account.cash))''')
rep('''    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "live" / "logs" / "bot.log"), logging.StreamHandler()])
    cfg = load_config(a.config, a.overrides)''', '''    cfg = load_config(a.config, a.overrides)
    (ROOT / cfg.bot.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / cfg.bot.state_dir / "logs" / "bot.log"), logging.StreamHandler()])
    if cfg.bot.state_dir != "live":   # ETH / BTC: own trades.csv / ledger_open.json / ledger_summary.json
        ledger.configure(cfg.bot.state_dir, cfg.bot.asset)''')
rep('''    log.info("start %s dry_run=%s hash=%s withdraw.mode=%s", cfg.name, cfg.live.dry_run, cfg.short_hash(), cfg.withdraw.mode)''',
'''    log.info("start %s asset=%s share=%.2f state_dir=%s dry_run=%s hash=%s withdraw.enabled=%s mode=%s", cfg.name, cfg.bot.asset,
             cfg.bot.share, cfg.bot.state_dir, cfg.live.dry_run, cfg.short_hash(), cfg.withdraw.enabled, cfg.withdraw.mode)''')
rep('''    def cancel_all(self, st: dict[str, Any]) -> bool:''', '''    def sweep(self, st: dict[str, Any], side: int | None) -> None:
        """SOL in / SOL out: swap this bot's USDC / ETH / WBTC leftovers back to SOL (live/sweep.py) and book the SOL into
        the open trade record (or, if that trade is already booked, its own ledger row). Skipped while any market
        request is in flight (its tokens may still be moving)."""
        b = self.cfg.bot
        if not b.sweep_enabled:
            return
        if [r for r in st.get("allOpenRequests", st["openRequests"]) if r.get("requestType") != "trigger"]:
            return
        prices = {"ETH": st["oracle"]["price"]} if self.asset == "ETH" else {"WBTC": st["oracle"]["price"]} if self.asset == "BTC" else {}
        plans = sweepmod.plan_sweeps(st, self.asset, side, prices, b.sweep_min_usd)
        if not plans:
            return
        log.warning("non-SOL proceeds in wallet: %s -> swapping to SOL", plans)
        sol = sweepmod.run_sweeps(self.cfg, plans, not self.live.dry_run, jup)
        if not sol:
            return
        if ledger.OPEN.exists():
            ledger.update(sweep_sol=sol)
        else:
            row = ledger.write_sweep_row(str(pd.Timestamp.now(tz="UTC")), sol, f"post-booking sweep {[p['name'] for p in plans]}")
            log.info("LEDGER sweep row booked: %+.9f SOL", row["net_sol"])

    def cancel_all(self, st: dict[str, Any]) -> bool:''')
rep('''            log.info("position gone on-chain (TP2/stop/liquidation/manual); clearing local state")
            ledger.update(exit=f"{'TP2' if L.tp_hits else 'external/TP/liq'}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
            self.book_flat(st, L.side)''', '''            log.info("position gone on-chain (TP2/stop/liquidation/manual); clearing local state")
            ledger.update(exit=f"{'TP2' if L.tp_hits else 'external/TP/liq'}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
            try:
                self.sweep(st, L.side)   # SOL out: before booking, so swapped proceeds land in this trade
            except Exception as e:  # noqa: BLE001 - retried next tick
                log.warning("sweep failed: %s", e)
            self.book_flat(st, L.side)''')
rep('''        if pos is None and not L.flat_since:   # idle clock: since the last booked close, else now''', '''        if pos is not None or L.side == 0:   # partial-TP proceeds / leftovers after booking
            try:
                self.sweep(st, pos.side if pos is not None else None)
            except Exception as e:  # noqa: BLE001 - retried next tick
                log.warning("sweep failed: %s", e)
        if pos is None and not L.flat_since:   # idle clock: since the last booked close, else now''')
rep('''        st2 = self.chain()
        after = self.chain_position(st2)
        if after is not None or not res.get("ok"):
            self.halt(f"close not confirmed on-chain: {res}")
            return
        ledger.update(exit=f"{why.split(' ')[0]}@{pd.Timestamp.now(tz='UTC'):%H:%M}")''', '''        st2 = self.chain()
        after = self.chain_position(st2)
        if after is not None or not res.get("ok"):
            self.halt(f"close not confirmed on-chain: {res}")
            return
        ledger.update(exit=f"{why.split(' ')[0]}@{pd.Timestamp.now(tz='UTC'):%H:%M}")
        try:
            self.sweep(st2, pos.side)   # SOL out before booking
        except Exception as e:  # noqa: BLE001 - retried next tick
            log.warning("sweep failed: %s", e)''')
print("bot.py patched")
