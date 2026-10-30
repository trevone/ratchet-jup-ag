"""USDC deposit fold-in (Trevor 2026-10-26, live/deposit.py): >= $2 USDC in the shared wallet is swapped by the SOL bot
alone into SOL / ETH / WBTC thirds; each bot credits exactly what landed (measured) to its own bank as capital."""
import dataclasses
import json
import logging
import time

import pytest

import live.bot as B
from config import load_config
from live import bank as K
from live import deposit as D
from live import sweep as sweepmod

PX = {"SOL": 110.0, "ETH": 2450.0, "BTC": 80000.0}
CFG = {"SOL": "configs/live_100.toml", "ETH": "configs/live_eth.toml", "BTC": "configs/live_btc.toml"}
MINT2ASSET = {K.MINT["SOL"]: "SOL", K.MINT["ETH"]: "ETH", K.MINT["WBTC"]: "BTC"}
FEE = 5000
ONCHAIN_SLIP = {"InstructionError": [2, {"Custom": 6001}]}   # live 2026-10-26 21:57 MT, tx 2KK5zVRB...


class Chain:
    """Fake shared wallet + TS CLI: swaps land at fixed prices; the quote (quotedOut) is deliberately wrong so a test
    fails if anything credits the quote instead of the measured delta."""

    def __init__(self, usdc=0):
        self.usdc, self.lamports, self.eth, self.wbtc = usdc, 700_000_000, 1_500_000, 60_000
        self.txs, self.bt, self.calls, self.modes, self.n = {}, {}, [], {}, 0

    def out_atoms(self, asset, atoms):
        usd = atoms / 1e6
        return int(usd / PX[asset] * (1e9 if asset == "SOL" else 1e8) * 0.998)   # 0.2 % swap cost

    def land(self, asset, atoms):
        self.n += 1
        sig = f"SIG{self.n}{asset}"
        got = self.out_atoms(asset, atoms)
        self.usdc -= atoms
        self.lamports -= FEE
        td = {"USDC": str(-atoms), "ETH": "0", "WBTC": "0"}
        if asset == "SOL":
            self.lamports += got
            d = {"feeLamports": FEE, "nativeDeltaLamports": got - FEE, "rentDeltaLamports": 0, "wsolDeltaAtoms": "0"}
        else:
            if asset == "ETH":
                self.eth += got
            else:
                self.wbtc += got
            td["ETH" if asset == "ETH" else "WBTC"] = str(got)
            d = {"feeLamports": FEE, "nativeDeltaLamports": -FEE, "rentDeltaLamports": 0, "wsolDeltaAtoms": "0"}
        d.update(jupsolDeltaAtoms="0", tokenDeltaAtoms=td, err=None)
        self.txs[sig], self.bt[sig] = d, time.time()
        return sig, d, got

    def jup(self, cmd, a):
        self.calls.append((cmd, a))
        if cmd == "swap":
            assert a["inputMint"] == K.MINT["USDC"] and a["direction"] == "swap" and a["send"] is True
            asset = MINT2ASSET[a["outputMint"]]
            atoms = int(a["amountAtoms"])
            mode = (self.modes.get(asset) or [None]).pop(0) if self.modes.get(asset) else None
            if mode == "fatal":
                return {"ok": False, "error": "price impact 2.1% over the cap"}
            if mode == "simslip":    # send.ts own simulation before signing (live 2026-10-26 21:51 MT)
                return {"ok": False, "error": 'Error: simulation failed {"InstructionError":[1,{"Custom":6001}]} Program '
                                              'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success | Program JUP6'}
            if mode == "preflight":  # RPC preflight of the first send (live 2026-10-26 21:45 MT): never forwarded
                return {"ok": False, "error": "Error: Simulation failed. \nMessage: Transaction simulation failed: Error "
                                              "processing Instruction 2: custom program error: 0x1771. \nLogs: \n[\n"}
            if mode == "siminsufficient":
                return {"ok": False, "error": 'Error: simulation failed {"InstructionError":[1,{"Custom":6024}]} x'}
            if mode == "lost":   # CLI died mid-send, nothing landed
                raise TimeoutError("cli timeout")
            if mode == "noresult":   # CLI died after the tx landed
                self.land(asset, atoms)
                raise TimeoutError("cli timeout")
            if mode == "onchain_err":
                self.n += 1
                sig = f"SIG{self.n}ERR"
                self.lamports -= FEE
                self.txs[sig], self.bt[sig] = {"feeLamports": FEE, "nativeDeltaLamports": -FEE, "tokenDeltaAtoms":
                                               {"USDC": "0", "ETH": "0", "WBTC": "0"}, "err": ONCHAIN_SLIP}, time.time()
                return {"ok": False, "sent": True, "sig": sig, "err": ONCHAIN_SLIP, "deltas": self.txs[sig]}
            sig, d, got = self.land(asset, atoms)
            if mode == "nodeltas":
                return {"ok": True, "sent": True, "sig": sig, "quotedOut": str(got * 2), "deltas": None}
            return {"ok": True, "sent": True, "sig": sig, "quotedOut": str(got * 2), "feeLamports": FEE, "deltas": d}
        if cmd == "tx-deltas":
            d = self.txs.get(a["sig"])
            return {"ok": True, "found": d is not None, "deltas": d}
        if cmd == "usdc-sigs":
            return {"ok": True, "sigs": [{"signature": s, "blockTime": int(self.bt[s]), "err": self.txs[s].get("err")}
                                         for s in reversed(list(self.txs))]}
        raise AssertionError(f"unexpected CLI call {cmd}")

    def swaps(self, asset=None):
        return [a for c, a in self.calls if c == "swap" and (asset is None or MINT2ASSET[a["outputMint"]] == asset)]

    def st(self, asset, size=0.0, reqs=(), busy=False, read_at=None):
        p = {"pubkey": "P", "sizeUsd": size, "collateralUsd": 10.0, "price": PX[asset] * 1.01, "realisedPnlUsd": 0.0} \
            if size else {}
        rq = [{"pubkey": f"R{x}", "triggerPrice": x, "executed": False, "requestType": "trigger"} for x in reqs]
        if busy:
            rq = rq + [{"pubkey": "M", "requestType": "market", "requestChange": "increase"}]
        return {"market": asset, "openRequests": rq, "allOpenRequests": rq, "oracle": {"price": PX[asset], "ageSec": 1},
                "solOracle": {"price": PX["SOL"]}, "wallet": {"sol": self.lamports / 1e9},
                "positions": {"long": {}, "short": p},
                "sweepTokens": {"USDC": {"mint": K.MINT["USDC"], "atoms": str(self.usdc)},
                                "ETH": {"mint": K.MINT["ETH"], "atoms": str(self.eth)},
                                "WBTC": {"mint": K.MINT["WBTC"], "atoms": str(self.wbtc)}},
                "_read_at": time.time() if read_at is None else read_at}


@pytest.fixture
def env(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    ch = Chain()
    monkeypatch.setattr(B, "jup", ch.jup)
    monkeypatch.setattr(B, "ROOT", tmp_path)
    monkeypatch.setattr(sweepmod, "BACKOFF_SEC", 0.0)

    def mk(asset, bal=None, fresh=False):
        d = asset.lower()
        (tmp_path / d).mkdir(exist_ok=True)
        if asset == "SOL":
            monkeypatch.setattr(B, "STATE", tmp_path / d / "state.json")
        cfg = load_config(CFG[asset], ["live.dry_run=true"] + ([] if asset == "SOL" else [f"bot.state_dir={d}"]))
        b = B.Bot(cfg)
        b.live = dataclasses.replace(b.live, dry_run=False)
        b._sol_px, b._mkt_px = PX["SOL"], PX[asset]
        if not fresh:
            coin = K.COIN[asset]
            w = {"SOL": ch.lamports / 1e9 - 0.02, "ETH": ch.eth / 1e8, "WBTC": ch.wbtc / 1e8}[coin] if bal is None else bal
            b.local.bank = K.new_bank(asset, w, w)
            b.save()
        return b
    return ch, mk, tmp_path


def journal():
    return D.load()


def run(ch, sol_bot, n=2):
    """n SOL-bot sweep steps on fresh state reads (detection needs two reads of the same balance)."""
    for _ in range(n):
        sol_bot.sweep(ch.st("SOL"), None)
        time.sleep(0.002)


def credit_all(ch, bots):
    for a, b in bots.items():
        b.bank_tick(ch.st(a), None, PX[a])


# ------------------------------------------------------------------ split math / dust
def test_split_thirds_remainder_to_sol():
    for n in (2_000_000, 10_000_000, 10_000_001, 10_000_002, 123_456_789, 3):
        s = D.split(n)
        assert s["ETH"] == s["BTC"] == n // 3 and s["SOL"] == n - 2 * (n // 3) and sum(s.values()) == n
        assert 0 <= s["SOL"] - s["ETH"] <= 2
    assert D.split(10_000_000) == {"SOL": 3_333_334, "ETH": 3_333_333, "BTC": 3_333_333}


def test_dust_below_two_dollars_ignored(env, caplog):
    ch, mk, _ = env
    ch.usdc = 1_999_999
    sol = mk("SOL")
    run(ch, sol, 3)
    assert not ch.swaps() and journal()["records"] == []
    assert "sweep: stray USDC" in caplog.text and "deposit" not in caplog.text.lower()


def test_only_sol_bot_swaps_eth_btc_log_only(env, caplog):
    ch, mk, _ = env
    ch.usdc = 9_000_000
    eth, btc = mk("ETH"), mk("BTC")
    for _ in range(3):
        eth.sweep(ch.st("ETH"), None)
        btc.sweep(ch.st("BTC"), None)
    assert not ch.swaps() and journal()["records"] == []
    assert "sweep: USDC $9.00 in the wallet: deposit (the SOL bot swaps it" in caplog.text
    dry = mk("SOL")
    dry.live = dataclasses.replace(dry.live, dry_run=True)   # a dry-run SOL bot never swaps
    run(ch, dry, 3)
    assert not ch.swaps()


# ------------------------------------------------------------------ the happy path
def test_full_flow_measured_credit_hwm_wdmark_equal_and_ledger(env, caplog):
    ch, mk, tmp = env
    bots = {a: mk(a) for a in ("SOL", "ETH", "BTC")}
    before = {a: dict(b.local.bank) for a, b in bots.items()}
    ch.usdc = 10_000_000
    bots["SOL"].sweep(ch.st("SOL"), None)
    assert not ch.swaps(), "first sighting only (two fresh reads needed)"
    time.sleep(0.002)
    bots["SOL"].sweep(ch.st("SOL"), None)
    assert [int(a["amountAtoms"]) for a in ch.swaps()] == [3_333_334, 3_333_333, 3_333_333]
    assert [a["slippageBps"] for a in ch.swaps()] == [bots["SOL"].cfg.withdraw.unstake_slippage_bps] * 3
    assert ch.usdc == 0 and "DEPOSIT USDC: $10.000000 -> SOL" in caplog.text and "sweep: deposit #1 swapped" in caplog.text
    r = journal()["records"][0]
    assert r["status"] == "swapped" and all(lg["status"] == "landed" for lg in r["legs"].values())
    credit_all(ch, bots)
    for a, b in bots.items():
        bk, b0 = b.local.bank, before[a]
        got = r["legs"][a]["got"]
        exp = ch.out_atoms(a, r["legs"][a]["atoms"]) / (1e9 if a == "SOL" else 1e8)   # measured, not the 2x quote
        assert got == pytest.approx(exp, abs=1e-9)
        for k in ("bal", "hwm", "wd_mark"):
            assert bk[k] == pytest.approx(b0[k] + got, abs=1e-9)
        assert bk["totals"]["ext_in"] == pytest.approx(got) and bk["totals"].get("trade_net", 0.0) == 0.0
        assert bk["deposits"][-1]["sig"] == r["legs"][a]["sig"] and bk["fee_sol"] == b0["fee_sol"]
        assert K.withdraw_amount(bk, 0.85, 0.0) == K.withdraw_amount(b0, 0.85, 0.0)   # daily withdrawal unaffected
        assert json.loads(b.state_path.read_text())["bank"]["bal"] == pytest.approx(bk["bal"])
    assert caplog.text.count("BANK DEPOSIT credited #1") == 3
    lines = (tmp / D.LEDGER).read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["usdc_in"] == 10.0 and set(row["out"]) == {"SOL", "ETH", "WBTC"}
    assert row["banks"]["ETH"]["after"]["bal"] == pytest.approx(bots["ETH"].local.bank["bal"])
    assert row["banks"]["SOL"]["before"]["hwm"] == pytest.approx(before["SOL"]["hwm"])
    assert journal()["records"][0]["status"] == "done"
    # nothing more happens: no double credit, no new record, no alert
    for _ in range(3):
        run(ch, bots["SOL"], 1)
        credit_all(ch, bots)
    assert len(ch.swaps()) == 3 and len((tmp / D.LEDGER).read_text().splitlines()) == 1
    for a, b in bots.items():
        assert b.local.bank["bal"] == pytest.approx(before[a]["bal"] + r["legs"][a]["got"], abs=1e-9)
    assert not K.alert_path().exists()


def test_second_sweep_call_same_tick_and_concurrent_run_never_swap_twice(env):
    ch, mk, _ = env
    sol = mk("SOL")
    ch.usdc = 6_000_000
    run(ch, sol, 1)
    st = ch.st("SOL")
    sol.sweep(st, None)            # swaps the three legs
    ch.usdc += 6_000_000           # (even if more USDC were there) the same stale read never drives another swap
    sol.sweep(st, None)            # position-gone tick: sweep runs twice on one state read
    assert len(ch.swaps()) == 3 and len(journal()["records"]) == 1
    with D.run_lock() as got:      # another swapper process holds the run lock: this run does nothing
        assert got
        assert "another swapper run" in D.run_swapper(ch.st("SOL"), sol.cfg, ch.jup)
    assert len(ch.swaps()) == 3


# ------------------------------------------------------------------ crash / restart / partial failure
def test_crash_between_swap_and_credit_credits_once_after_restart(env):
    ch, mk, _ = env
    bots = {a: mk(a) for a in ("SOL", "ETH", "BTC")}
    b0 = {a: b.local.bank["bal"] for a, b in bots.items()}
    ch.usdc = 7_500_000
    run(ch, bots["SOL"])
    assert len(ch.swaps()) == 3
    bots = {a: mk(a, fresh=True) for a in bots}      # all three restart: banks re-read from state.json
    assert all(b.local.bank["bal"] == b0[a] for a, b in bots.items())
    credit_all(ch, bots)
    credit_all(ch, bots)
    bots = {a: mk(a, fresh=True) for a in bots}
    credit_all(ch, bots)
    r = journal()["records"][0]
    for a, b in bots.items():
        assert b.local.bank["bal"] == pytest.approx(b0[a] + r["legs"][a]["got"], abs=1e-9)
        assert len(b.local.bank["deposits"]) == 1


def test_crash_after_state_save_before_journal_mark_never_double_credits(env, monkeypatch):
    ch, mk, _ = env
    eth = mk("ETH")
    b0 = eth.local.bank["bal"]
    ch.usdc = 7_500_000
    run(ch, mk("SOL"))
    real = D.mark_credited
    monkeypatch.setattr(D, "mark_credited", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        eth.deposit_tick(ch.st("ETH"))
    monkeypatch.setattr(D, "mark_credited", real)
    got = journal()["records"][0]["legs"]["ETH"]["got"]
    eth = mk("ETH", fresh=True)                      # restart: the credit is in state.json, the journal mark is not
    assert eth.local.bank["bal"] == pytest.approx(b0 + got)
    assert journal()["records"][0]["credits"]["ETH"] is None
    for _ in range(3):
        eth.bank_tick(ch.st("ETH"), None, PX["ETH"])
    assert eth.local.bank["bal"] == pytest.approx(b0 + got) and journal()["records"][0]["credits"]["ETH"]["sig"]


def test_swapper_crash_mid_send_resolves_from_chain_never_resends(env, monkeypatch, caplog):
    ch, mk, _ = env
    sol = mk("SOL")
    ch.usdc = 9_000_000
    clock = [10_000.0]
    real = sweepmod.swap_with_backoff

    def dies_after_landing(cfg, plan, jup, **k):      # process killed right after the ETH swap landed
        if k["out_mint"] == K.MINT["ETH"]:
            ch.land("ETH", plan["atoms"])
            raise KeyboardInterrupt("SIGKILL")
        return real(cfg, plan, jup, **k)
    monkeypatch.setattr(sweepmod, "swap_with_backoff", dies_after_landing)
    D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
    clock[0] += 30
    with pytest.raises(KeyboardInterrupt):
        D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
    monkeypatch.setattr(sweepmod, "swap_with_backoff", real)
    legs = journal()["records"][0]["legs"]
    assert legs["SOL"]["status"] == "landed" and legs["ETH"]["status"] == "sending" and legs["ETH"]["sig"] is None
    n = len(ch.swaps())
    for _ in range(5):                                # restarted bot, within RESOLVE_AFTER_S: waits, sends nothing
        clock[0] += 30
        note = D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
        assert "waiting" in note
    assert len(ch.swaps()) == n
    ch.bt = {s: clock[0] - 160 for s in ch.bt}        # chain block times on the test clock
    clock[0] += 200                                   # past RESOLVE_AFTER_S: found on the USDC account
    D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
    r = journal()["records"][0]
    assert r["legs"]["ETH"]["status"] == "landed" and r["legs"]["ETH"]["sig"].endswith("ETH")
    assert r["legs"]["BTC"]["status"] == "pending"   # resolution touched the journal: the next leg waits for a newer read
    clock[0] += 30
    D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
    r = journal()["records"][0]
    assert r["status"] == "swapped" and len(ch.swaps("ETH")) == 0 and len(ch.swaps("BTC")) == 1
    assert "found its landed swap" in caplog.text and ch.usdc == 0


def test_lost_send_resent_once_after_resolution(env):
    ch, mk, _ = env
    sol = mk("SOL")
    ch.usdc = 9_000_000
    ch.modes = {"BTC": ["lost"]}                      # CLI died, nothing landed
    clock = [10_000.0]

    def step(dt):
        clock[0] += dt
        return D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
    step(0)
    step(30)
    assert journal()["records"][0]["legs"]["BTC"]["status"] == "sending" and len(ch.swaps("BTC")) == 1
    step(60)
    assert len(ch.swaps("BTC")) == 1                  # unresolved: never re-sent
    ch.bt = {s: 0 for s in ch.bt}                     # the landed SOL / ETH txs are older than the BTC send
    step(RESOLVE := D.RESOLVE_AFTER_S)                # no matching tx -> pending; re-sent on the next fresh read
    assert RESOLVE == 300 and journal()["records"][0]["legs"]["BTC"]["status"] == "pending"
    step(1)
    r = journal()["records"][0]
    assert r["legs"]["BTC"]["status"] == "landed" and len(ch.swaps("BTC")) == 2 and len(ch.swaps()) == 4
    assert ch.usdc == 0 and r["status"] == "swapped"


def test_partial_failure_resumes_missing_legs_only(env, caplog):
    ch, mk, _ = env
    bots = {a: mk(a) for a in ("SOL", "ETH", "BTC")}
    b0 = {a: b.local.bank["bal"] for a, b in bots.items()}
    ch.usdc = 12_000_000
    ch.modes = {"ETH": ["fatal", "onchain_err"]}
    clock = [10_000.0]

    def step(dt):
        clock[0] += dt
        return D.run_swapper(ch.st("SOL", read_at=clock[0]), bots["SOL"].cfg, ch.jup, now=lambda: clock[0])
    step(0)
    step(30)
    L = journal()["records"][0]["legs"]
    assert (L["SOL"]["status"], L["ETH"]["status"], L["BTC"]["status"]) == ("landed", "pending", "landed")
    assert ch.usdc == 4_000_000 and "ETH leg not swapped (fatal" in caplog.text
    credit_all(ch, bots)                              # SOL / BTC credit their shares now; ETH has nothing to credit
    assert bots["ETH"].local.bank["bal"] == b0["ETH"] and bots["BTC"].local.bank["bal"] > b0["BTC"]
    step(10)
    assert len(ch.swaps("ETH")) == 1                  # retry waits RETRY_S
    step(D.RETRY_S)                                   # on-chain failure: USDC not spent -> pending again
    assert journal()["records"][0]["legs"]["ETH"]["status"] == "sending"
    step(5)
    assert journal()["records"][0]["legs"]["ETH"]["status"] == "pending" and ch.usdc == 4_000_000
    step(D.RETRY_S + 1)
    r = journal()["records"][0]
    assert r["legs"]["ETH"]["status"] == "landed" and r["status"] == "swapped" and ch.usdc == 0
    assert len(ch.swaps("SOL")) == 1 and len(ch.swaps("BTC")) == 1 and len(ch.swaps("ETH")) == 3
    assert [x["slippageBps"] for x in ch.swaps("ETH")] == [50, 50, 100]   # fatal: same bps; on-chain 6001: 2x
    credit_all(ch, bots)
    credit_all(ch, bots)
    for a, b in bots.items():
        assert b.local.bank["bal"] == pytest.approx(b0[a] + r["legs"][a]["got"], abs=1e-9)
    assert journal()["records"][0]["status"] == "done"


def test_unmeasured_ok_resolved_from_tx_and_usdc_shortfall_never_oversends(env, caplog):
    ch, mk, _ = env
    sol = mk("SOL")
    ch.usdc = 6_000_000
    ch.modes = {"SOL": ["nodeltas"]}
    clock = [10_000.0]

    def step(dt):
        clock[0] += dt
        return D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup, now=lambda: clock[0])
    step(0)
    step(30)
    L = journal()["records"][0]["legs"]
    assert L["SOL"]["status"] == "sending" and L["SOL"]["sig"] and len(ch.swaps()) == 1   # stops: USDC uncertain
    step(30)
    L = journal()["records"][0]["legs"]
    assert L["SOL"]["status"] == "landed" and L["SOL"]["got"] == pytest.approx(ch.out_atoms("SOL", 2_000_000) / 1e9)
    step(30)
    L = journal()["records"][0]["legs"]
    assert all(L[a]["status"] == "landed" for a in L) and len(ch.swaps()) == 3
    # a journal that believes legs are unswapped while the USDC is gone never sends
    def regress(j):
        for a in ("ETH", "BTC"):
            j["records"][0]["legs"][a].update(status="pending", sig=None)
        j["records"][0]["status"] = "swapping"
    D.update(regress)
    step(30)
    assert len(ch.swaps()) == 3 and "still to swap; nothing sent" in caplog.text


# ------------------------------------------------------------------ crediting rules
def test_credit_while_in_trade_moves_guard_baseline_and_defers_while_busy(env):
    ch, mk, _ = env
    btc = mk("BTC")
    bk = btc.local.bank
    bk["trade_start_equity"] = round(bk["bal"] + 0.000125, 9)   # in a trade: collateral deposited
    bk["bal"] = round(bk["bal"] - 0.000125, 9)
    tse0, bal0, hwm0, wd0 = bk["trade_start_equity"], bk["bal"], bk["hwm"], bk["wd_mark"]
    btc.local.side, btc.local.day_start_equity = -1, 100.0
    ch.usdc = 9_000_000
    run(ch, mk("SOL"))
    got = journal()["records"][0]["legs"]["BTC"]["got"]
    st = ch.st("BTC", size=50.0, reqs=(79000.0,), busy=True)
    assert btc.deposit_tick(st) is True and bk["bal"] == bal0          # order request in flight: deferred
    btc.bank_tick(ch.st("BTC", size=50.0, reqs=(79000.0,)), btc.chain_position(st), PX["BTC"])
    assert bk["bal"] == pytest.approx(bal0 + got) and bk["hwm"] == pytest.approx(hwm0 + got)
    assert bk["wd_mark"] == pytest.approx(wd0 + got) and bk["trade_start_equity"] == pytest.approx(tse0 + got)
    assert btc.local.day_start_equity == pytest.approx(100.0 + got * PX["BTC"])
    assert bk.get("trade_dep", 0.0) == 0.0 and bk.get("trade_recv", 0.0) == 0.0   # trade flows untouched


def test_eth_payout_window_and_inflight_never_book_profit_or_alert(env):
    ch, mk, _ = env
    eth = mk("ETH")
    bk = eth.local.bank
    eth.bank_tick(ch.st("ETH"), None, PX["ETH"])
    bk["window"] = 2                                  # a TP payout window is open when the deposit's ETH lands
    ch.usdc = 9_000_000
    sol = mk("SOL")
    clock = [time.time()]
    D.run_swapper(ch.st("SOL", read_at=clock[0]), sol.cfg, ch.jup)
    ch.modes = {"ETH": ["noresult"]}                  # ETH lands, its outcome unknown for a while
    D.run_swapper(ch.st("SOL"), sol.cfg, ch.jup)
    assert journal()["records"][0]["legs"]["ETH"]["status"] == "sending"
    bal0 = bk["bal"]
    for _ in range(4):                                # wallet ETH is up, bank is not: reconcile held, window held
        eth.bank_tick(ch.st("ETH"), None, PX["ETH"])
    assert bk["bal"] == bal0 and bk["window"] == 2 and not [h for h in bk["hist"] if h.get("kind") == "payout"]
    ch.bt = {s: 0 for s in ch.bt}
    ch.bt[[s for s in ch.txs if s.endswith("ETH")][0]] = time.time()
    D.run_swapper(ch.st("SOL"), sol.cfg, ch.jup, now=lambda: time.time() + D.RESOLVE_AFTER_S + 1)
    got = journal()["records"][0]["legs"]["ETH"]["got"]
    for _ in range(4):
        eth.bank_tick(ch.st("ETH"), None, PX["ETH"])
    assert bk["bal"] == pytest.approx(bal0 + got) and bk["hwm"] >= bal0 + got - 1e-9
    assert bk["totals"]["ext_in"] == pytest.approx(got) and not [h for h in bk["hist"] if h.get("kind") == "payout"]
    assert bk["window"] == 0 and bk.get("pend") is None and not K.alert_path().exists()


def test_sol_reconcile_no_ext_alert_fees_are_reserve_float(env):
    ch, mk, _ = env
    bots = {a: mk(a) for a in ("SOL", "ETH", "BTC")}
    credit_all(ch, bots)                              # reserve baseline
    res0 = bots["SOL"].local.bank["res_last"]
    ch.usdc = 50_000_000                              # $50: each third far above ext_min_usd
    for _ in range(2):
        bots["SOL"].sweep(ch.st("SOL"), None)
        credit_all(ch, bots)
        time.sleep(0.002)
    for _ in range(4):
        credit_all(ch, bots)
    b = bots["SOL"].local.bank
    assert not K.alert_path().exists() and b.get("pend") is None
    assert b["res_last"] == pytest.approx(res0 - 3 * FEE / 1e9, abs=1e-9)   # only the three swap fees, as float
    for a in ("ETH", "BTC"):
        assert bots[a].local.bank.get("ext_alert") is None and bots[a].local.bank.get("pend") is None


def test_without_fold_in_the_same_deposit_would_alert(env):
    """Guard for the test above: the reconcile does catch an unbooked coin change of that size."""
    ch, mk, _ = env
    eth = mk("ETH")
    eth.bank_tick(ch.st("ETH"), None, PX["ETH"])
    ch.land("ETH", 16_000_000)
    for _ in range(3):
        eth.bank_tick(ch.st("ETH"), None, PX["ETH"])
    # ext_flow_mode = "own" (Trevor 2026-10-30): the unbooked own-coin change is caught and booked as own capital (alert mode: alerts)
    assert K.alert_path().exists() or (eth.local.bank.get("deposits") or [{}])[-1].get("kind") == "own_external"


def test_kill_switch_holds_swaps(env):
    ch, mk, tmp = env
    sol = mk("SOL")
    ch.usdc = 9_000_000
    sol.kill.parent.mkdir(parents=True, exist_ok=True)
    sol.kill.write_text("x")
    run(ch, sol, 3)
    assert not ch.swaps()
    sol.kill.unlink()
    run(ch, sol, 2)
    assert len(ch.swaps()) == 3


# ------------------------------------------------------------------ simulation rejections (2026-10-26 deposit #1 BTC leg)
def test_sim_rejected_only_when_provably_not_sent():
    assert D.sim_rejected({"ok": False, "error": 'Error: simulation failed {"InstructionError":[1,{"Custom":6001}]}'})
    assert D.sim_rejected({"ok": False, "error": "Error: Simulation failed. \nMessage: Transaction simulation failed: "
                                                 "Error processing Instruction 2: custom program error: 0x1771."})
    assert not D.sim_rejected({"ok": False, "sig": "S", "error": "Error: simulation failed {x}"})      # has a sig
    assert not D.sim_rejected({"ok": False, "sent": True, "error": "Error: simulation failed {x}"})    # was sent
    assert not D.sim_rejected({"ok": False, "error": "cli exception: Command timed out ... simulation failed {"})
    assert not D.sim_rejected({"ok": False, "error": "Error: expired, not landed"})
    assert not D.sim_rejected({"ok": False, "error": "price impact 2.1% over the cap"})
    for e in (str(ONCHAIN_SLIP), 'simulation failed {"InstructionError":[1,{"Custom":6001}]}', "custom program error: 0x1771."):
        assert D.SLIPPAGE_RE.search(e)
    assert not D.SLIPPAGE_RE.search('{"InstructionError":[1,{"Custom":6024}]}')


def test_sweep_args_slippage_default_unchanged_and_override_capped():
    cfg = load_config(CFG["BTC"])
    w = cfg.withdraw
    assert sweepmod.sweep_args(cfg, K.MINT["USDC"], 5, K.MINT["WBTC"])["slippageBps"] == w.unstake_slippage_bps == 50
    assert sweepmod.sweep_args(cfg, K.MINT["USDC"], 5, K.MINT["WBTC"], slippage_bps=100)["slippageBps"] == 100
    with pytest.raises(ValueError):
        sweepmod.sweep_args(cfg, K.MINT["USDC"], 5, K.MINT["WBTC"], slippage_bps=w.stake_max_slippage_bps + 1)
    assert D.retry_bps(cfg, None) == min(w.stake_max_slippage_bps, 2 * w.unstake_slippage_bps) == 100
    assert D.retry_bps(cfg, 100) == 100


def test_sim_slippage_rejection_retries_in_60s_at_higher_slippage_and_credits_once(env, caplog):
    ch, mk, _ = env
    bots = {a: mk(a) for a in ("SOL", "ETH", "BTC")}
    b0 = {a: dict(b.local.bank) for a, b in bots.items()}
    ch.usdc = 5_000_000
    ch.modes = {"BTC": ["preflight", "simslip"]}
    clock = [10_000.0]

    def step(dt):
        clock[0] += dt
        return D.run_swapper(ch.st("SOL", read_at=clock[0]), bots["SOL"].cfg, ch.jup, now=lambda: clock[0])
    step(0)
    step(30)                                          # detect, then SOL + ETH land, BTC rejected at RPC preflight
    leg = journal()["records"][0]["legs"]["BTC"]
    assert leg["status"] == "pending" and leg["sig"] is None and leg["slip_bps"] == 100
    assert leg["next_try"] == pytest.approx(clock[0] + D.RETRY_S, abs=1)
    assert "BTC leg rejected at simulation" in caplog.text and "nothing sent, retry in 60s at 100 bps" in caplog.text
    assert "outcome unknown" not in caplog.text and ch.usdc == 1_666_666
    assert not D.own_work("BTC", bots["BTC"].local.bank)["inflight"]   # nothing in flight: BTC recon not held
    credit_all(ch, bots)
    step(10)
    assert len(ch.swaps("BTC")) == 1                  # waits RETRY_S, never the 300 s unknown path
    step(D.RETRY_S)                                   # own simulation 6001 again: still nothing sent, stays at 100
    assert len(ch.swaps("BTC")) == 2 and journal()["records"][0]["legs"]["BTC"]["status"] == "pending"
    step(D.RETRY_S + 1)
    r = journal()["records"][0]
    assert r["legs"]["BTC"]["status"] == "landed" and r["status"] == "swapped" and ch.usdc == 0
    assert [x["slippageBps"] for x in ch.swaps("BTC")] == [50, 100, 100]
    assert [x["slippageBps"] for x in ch.swaps("SOL") + ch.swaps("ETH")] == [50, 50]
    credit_all(ch, bots)
    credit_all(ch, bots)
    bb = bots["BTC"].local.bank
    got = r["legs"]["BTC"]["got"]
    for k in ("bal", "hwm", "wd_mark"):
        assert bb[k] == pytest.approx(b0["BTC"][k] + got, abs=1e-12)
    assert len([d for d in bb["deposits"] if d["id"] == r["id"]]) == 1
    assert journal()["records"][0]["status"] == "done"
    assert not K.alert_path().exists() and bb.get("ext_alert") is None


def test_sim_rejection_other_error_retries_without_raising_slippage(env):
    ch, mk, _ = env
    bots = {a: mk(a) for a in ("SOL", "ETH", "BTC")}
    ch.usdc = 5_000_000
    ch.modes = {"SOL": ["siminsufficient"]}
    clock = [10_000.0]

    def step(dt):
        clock[0] += dt
        return D.run_swapper(ch.st("SOL", read_at=clock[0]), bots["SOL"].cfg, ch.jup, now=lambda: clock[0])
    step(0)
    step(30)
    L = journal()["records"][0]["legs"]
    assert L["SOL"]["status"] == "pending" and "slip_bps" not in L["SOL"]
    step(D.RETRY_S + 1)
    step(1)
    step(1)
    L = journal()["records"][0]["legs"]
    assert all(L[a]["status"] == "landed" for a in ("SOL", "ETH", "BTC"))
    assert [x["slippageBps"] for x in ch.swaps()] == [50] * 4 and ch.usdc == 0
