"""Trevor 2026-10-27: once a trade had an add fill or a partial (TP3 / trim), the guard (market close + TP@GUARD status) stays
armed until flat, even when a TP3 partial leaves lots=1, n_entries=1, no trims. Reset only by a new trade. Persisted
(local.guard_armed); a state written before the latch derives it from n_entries / lots / trims / partials_done.
Reproduces the SOL 2026-10-27 04:45-08:02 MT trade: first entry, then 3 x (resting add fill -> TP3 partial)."""
import json

import pytest

from live import bank as bankmod
from live.bot import Local, load_local
from strategy import LONG
from tests.test_resting import ACC, ON, mkbot, req, state

E0, S0, C0 = 115.8, 73.5, 10.5   # first entry (fixture lot 1)


def first_entry_bot(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    L = b.local
    first = dict(L.lots[0])
    L.lots, L.n_entries, L.last_entry, L.last_size, L.trade_opened_usd = [first], 1, E0, S0, S0
    L.last_leverage, L.rest_last_avg = 7.0, E0
    L.bank = bankmod.new_bank("SOL", 1.0)
    L.bank["trade_start_equity"] = 1.0
    return b


def st_of(size, coll, avg, price, reqs):
    return state(size=size, coll=coll, avg=avg, price=price, reqs=reqs)


def add_fill(b, price=106.6):
    """Place the resting add at this tick, then the chain shows it filled. Returns the post-fill state."""
    L = b.local
    st = st_of(L.last_size, C0, E0, price, [])
    pos = b.chain_position(st)
    b.resting_tick(pos, st, price, ACC, 100.0)
    ra = dict(L.rest_add)
    new_avg = (L.last_size + ra["size_usd"]) / (L.last_size / E0 + ra["size_usd"] / ra["trigger"])
    st2 = st_of(L.last_size + ra["size_usd"], C0 + ra["margin"], new_avg, ra["trigger"] - 0.05, [])
    b.resting_reconcile(st2, b.chain_position(st2), st2["oracle"]["price"])
    return st2


def partial_fill(b, n):
    """The TP3 on the add lot fills: the add lot closes, the first lot remains (lots=1, n_entries=1, no trims)."""
    L = b.local
    lot = L.lots[-1]
    assert not lot.get("first")
    L.partials = [{"pubkey": f"PT{n}", "lots": [lot["time"]], "lot_time": lot["time"], "size": lot["size"], "px": 106.8,
                   "level": 106.8, "n": 1, "rung": 1}]
    st = st_of(S0, C0, E0, 106.7, [req("R1", 110.33), req("R2", 110.63)])
    b.resting_reconcile(st, b.chain_position(st), 106.7)
    return st


def test_plain_first_entry_not_armed(monkeypatch, tmp_path):
    b = first_entry_bot(monkeypatch, tmp_path)
    assert not b.hwm_close_armed() and not b.local.guard_armed and not b.guard_tps()


def test_add_partial_cycles_keep_guard_armed_until_flat(monkeypatch, tmp_path):
    b = first_entry_bot(monkeypatch, tmp_path)
    L = b.local
    tgt0 = b.guard_target(st_of(S0, C0, E0, 106.6, []))
    for n in (1, 2, 3):
        add_fill(b)
        assert L.n_entries == 2 and len(L.lots) == 2 and L.guard_armed and b.hwm_close_armed()
        st = partial_fill(b, n)
        # the pre-fix test (n_entries > 1 or lots > 1 or trims) is False here: this was the disarm
        assert L.n_entries == 1 and len(L.lots) == 1 and not L.trims and len(L.partials_done) == n
        assert b.hwm_close_armed(), f"guard disarmed after partial {n}"
        assert b.guard_tps()                         # TP1/TP2 stay at the TP@GUARD price
        line = b.bank_status(st, b.chain_position(st), 106.7)
        assert "guard ARMED" in line and "TP@GUARD target" in line and "not armed" not in line
        assert b.guard_target(st) == pytest.approx(tgt0)   # target unchanged by the latch (trade_start_equity based)
    # persisted: survives a restart
    saved = json.loads(b.state_path.read_text())
    assert saved["guard_armed"] is True
    assert load_local(b.state_path).guard_armed is True
    # flat -> a new trade starts unarmed
    b.local = Local(bank=L.bank)
    assert not b.hwm_close_armed()


def test_restart_with_old_state_derives_armed_from_partials_done(monkeypatch, tmp_path):
    b = first_entry_bot(monkeypatch, tmp_path)
    L = b.local
    L.partials_done = [{"pubkey": "old", "px": 106.8, "size": 75.0, "filled": "2026-10-27 13:09:00+00:00"}]
    d = json.loads(json.dumps(__import__("dataclasses").asdict(L)))
    d.pop("guard_armed")                                  # state.json written by the pre-fix code
    b.state_path.write_text(json.dumps(d))
    b.local = load_local(b.state_path)
    assert b.local.guard_armed is False and len(b.local.lots) == 1 and b.local.n_entries == 1
    assert b.hwm_close_armed()                            # derived from partials_done, then latched + saved
    assert json.loads(b.state_path.read_text())["guard_armed"] is True


def test_restart_with_old_state_plain_first_entry_stays_unarmed(monkeypatch, tmp_path):
    b = first_entry_bot(monkeypatch, tmp_path)
    d = __import__("dataclasses").asdict(b.local)
    d.pop("guard_armed")
    b.state_path.write_text(json.dumps(d))
    b.local = load_local(b.state_path)
    assert not b.hwm_close_armed() and not b.local.guard_armed


def test_trim_and_market_add_latch(monkeypatch, tmp_path):
    b = first_entry_bot(monkeypatch, tmp_path)
    L = b.local
    L.trims = [{"onchain_pnl_usd": 0.1}]
    assert b.hwm_close_armed()
    L.trims = []                                          # still armed for the rest of the trade
    assert b.hwm_close_armed() and L.guard_armed


def test_market_close_guard_fires_after_partial_with_one_lot(monkeypatch, tmp_path):
    """After add -> partial (lots=1) the tick's market-close guard runs: closes once equity >= target."""
    b = first_entry_bot(monkeypatch, tmp_path)
    add_fill(b)
    st = partial_fill(b, 1)
    pos = b.chain_position(st)
    b.local.bank["trade_start_equity"] = b.hwm_equity(pos, st, 106.7) * 0.99   # target met at market
    assert b.hwm_close_armed() and b.guard_check(pos, st, 106.7)[0]
