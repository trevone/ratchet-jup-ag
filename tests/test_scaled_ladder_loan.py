"""2026-10-29 scaled ladder: the inter-bot / JupSOL next-add request is sized from the scaled rung's collateral C_n."""
import json

import pytest

from live import bank as bankmod
from live import jupsol_loans as jl
import live.bot as B
from tests.test_resting import ON, mkbot, state

SCALED = ["dca.ladder_mode=scaled", "dca.min_add_usd=10", "dca.lev_start=5", "dca.lev_step=2.5", "dca.lev_cap=20",
          "dca.ladder_fixed_usd=10"]


@pytest.fixture
def bot(monkeypatch, tmp_path):
    f = tmp_path / "ib.json"
    monkeypatch.setattr(B.Bot, "_ib_file", lambda self: f)
    monkeypatch.setattr(bankmod, "journal_lock", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(B.Bot, "bank_on", property(lambda s: True))
    b = mkbot(monkeypatch, tmp_path, ON + SCALED)
    b.asset, b.dir = "SOL", tmp_path
    b.local.bank = {"coin": "SOL", "bal": 0.1, "hwm": 0.1, "wd_mark": 0.1, "hist": [], "totals": {}}
    b.bank_fee = lambda *a: None
    b._busy = lambda *a: False
    b.local.first_margin, b.local.n_entries, b.local.last_leverage = 12.0, 1, 5.0
    b._f = f
    return b


def test_probe_step_is_scaled_rung(bot):
    pos = bot.chain_position(state())
    assert bot.ib_rung_usd(pos) == pytest.approx(12.0 + 0.10)       # rung 1 at B=12: $12 x7.5
    bot.local.n_entries = 8
    assert bot.ib_rung_usd(bot.chain_position(state())) == pytest.approx(15.0 + 0.10)   # rung 8: 25*12/20


@pytest.mark.parametrize("cash", [0.0, 4.0, 11.0])
def test_request_uses_scaled_rung_margin(bot, cash, monkeypatch):
    seen = {}
    monkeypatch.setattr(jl, "next_add_shortfall", lambda b, p, c, big, r=0.0: (seen.setdefault("m", big.margin), False)[1])
    plan, why = bot.rest_add_plan(bot.chain_position(state()), cash)
    assert plan is None and "no ladder plan" in why
    assert seen["m"] == pytest.approx(12.0)                          # JupSOL path sees the scaled rung margin
    rec = json.loads(bot._f.read_text())
    assert rec["need_usd"] == round(max(bankmod.IB_MIN_USD, 12.0 - cash + 1.0), 2)
