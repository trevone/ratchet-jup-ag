"""Trevor 2026-10-12 08:52 MT: no double add while an add is unresolved; adopted collateral booked as a deposit."""
from live.bot import Local
from tests.test_resting import ON, mkbot, req, state


def bot(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    b.local.add_pending = {"time": "2026-10-12 14:00:00+00:00", "size": 10.0, "lev": 10, "open": False}
    return b


def test_waits_while_increase_request_on_chain(monkeypatch, tmp_path):
    b = bot(monkeypatch, tmp_path)
    st = state(reqs=[{**req("INC", 0, "increase"), "requestType": "market"}])
    assert b.add_pending_wait(st, b.chain_position(st)) and b.local.add_pending


def test_landed_add_booked_first(monkeypatch, tmp_path):
    b = bot(monkeypatch, tmp_path)
    lots = sum(l["size"] for l in b.local.lots)
    st = state(size=lots + 10.0, reqs=[])
    hit = []
    b.recover_missing_add = lambda *a: hit.append(1) or True
    assert b.add_pending_wait(st, b.chain_position(st)) and hit


def test_absent_clears_marker_and_allows_send(monkeypatch, tmp_path):
    b = bot(monkeypatch, tmp_path)
    lots = sum(l["size"] for l in b.local.lots)
    st = state(size=lots, reqs=[])
    assert not b.add_pending_wait(st, b.chain_position(st)) and b.local.add_pending is None


def test_adopt_books_collateral_deposit_no_hwm(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    monkeypatch.setattr(type(b), "bank_on", property(lambda s: True))
    b.local = Local(bank={"bal": 1.0, "coin": "SOL", "hwm": 2.0, "trade_dep": 0.0, "hist": [], "totals": {}})
    st = state(reqs=[])
    b.chain = lambda: st
    b.sync_tp_ladder = lambda p: None
    pos = b.chain_position(st)
    b.adopt_position(st, pos)
    bk = b.local.bank
    nat = round(pos.margin / b.coin_px(st), 9)
    assert abs(bk["bal"] - (1.0 - nat)) < 1e-9 and abs(bk["trade_dep"] - nat) < 1e-9 and bk["hwm"] == 2.0


def test_adopt_own_late_open_not_double_debited(monkeypatch, tmp_path):
    b = mkbot(monkeypatch, tmp_path, ON)
    monkeypatch.setattr(type(b), "bank_on", property(lambda s: True))
    b.local = Local(bank={"bal": 1.0, "coin": "SOL", "hwm": 2.0, "trade_dep": 0.3, "hist": [], "totals": {}},
                    add_pending={"open": True, "size": 10.0})
    st = state(reqs=[])
    b.chain = lambda: st
    b.sync_tp_ladder = lambda p: None
    b.adopt_position(st, b.chain_position(st))
    assert b.local.bank["bal"] == 1.0 and b.local.bank["trade_dep"] == 0.3
