"""SOL flat true-up always applied + withdrawal block (Trevor 2026-10-27). On 2026-10-26 08:24 MT a SOL trade's position
flows were +0.001155254 SOL but the bank had booked +0.023703325 (6 cancelled resting adds x 0.0037236 request rent
credited that was never debited, + payout-estimate error); the true-up -0.022548071 was skipped by the old 0.02 SOL
limit, inflating bal / hwm / wd_mark and the 10-27 00:05 withdrawal. Fake chain only (conftest)."""
import dataclasses

import pytest

from config import load_config
from live import bank as K
from live import ledger

SOL_PX = 100.0


def _st(reqs=(), wallet=0.6612825):
    rq = [{"pubkey": r, "requestType": "trigger", "requestChange": "increase"} for r in reqs]
    return {"market": "SOL", "openRequests": list(rq), "allOpenRequests": list(rq), "oracle": {"price": SOL_PX, "ageSec": 1},
            "solOracle": {"price": SOL_PX}, "wallet": {"sol": wallet}, "custody": {}, "positions": {"long": {}, "short": {}},
            "sweepTokens": {}}


def _w(bot):   # wallet in step with the book (bal + reserve float): the recon stays quiet, only the booking is tested
    return bot.local.bank["bal"] + bot.local.bank.get("res_last", 0.0)


@pytest.fixture
def sbot(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_100.toml", ["live.dry_run=true", f"bot.state_dir={tmp_path.name}"])
    monkeypatch.setattr(B, "ROOT", tmp_path.parent)
    b = B.Bot(cfg)
    b.live = dataclasses.replace(b.live, dry_run=False)
    b.kill, b.kill_all, b.confirm_wait = tmp_path / "KILL", tmp_path / "KILL_ALL", 0.0
    b.cmd = lambda name, args: {"ok": True, "sig": "S", "feeLamports": 5000}
    b._sol_px = SOL_PX
    bk = K.new_bank("SOL", 0.661282500, 0.639520700)
    bk["trade_dep"], bk["trade_recv"], bk["res_last"], bk["res_v"] = 0.098498742, 0.122202067, 0.0, 3
    b.local.bank = bk
    flows = {"v": 0.001155254}
    b._flows = flows
    monkeypatch.setattr(B, "position_net_sol", lambda pos, sig: (flows["v"], {"ok": True, "nTx": 25, "rentLamports": 0}))
    staked = []
    import live.stake as S
    monkeypatch.setattr(S, "stake_sol", lambda cfg, amt, **k: staked.append(amt) or {"ok": True, "sig": "WD", "jupsol_out": amt * 0.8})
    b._staked = staked
    return b


def test_large_true_up_is_applied_whatever_its_size(sbot):
    b = sbot.local.bank
    sbot.bank_flat(0.001155254)                    # no ledger open record: booked at once, counters reset
    assert b["bal"] == pytest.approx(0.661282500 - 0.022548071, abs=1e-9)
    assert b["res_last"] == pytest.approx(0.022548071, abs=1e-9)   # moved between reserve float and bal
    assert b["hist"][-1]["kind"] == "flat_trueup" and b["hist"][-1]["amt"] == pytest.approx(-0.022548071, abs=1e-9)
    assert "trueup_block" not in b and (b["trade_dep"], b["trade_recv"]) == (0.0, 0.0)
    assert K.update_hwm(b, True) is False          # hwm 0.6395207 stays: the over-booking never reaches it


def test_unappliable_true_up_blocks_hwm_and_withdrawal_until_applied_at_booking(sbot):
    b = sbot.local.bank
    b["hwm"] = b["wd_mark"] = 0.63
    ledger.on_open("2026-10-26 04:45:52+00:00", -1, 0.9, "OPENSIG", 5000, position="POS")
    sbot._flows["v"] = None                        # chain flows unavailable at flat
    sbot.bank_flat(sbot.book_flat(dict(_st(), positions={"long": {}, "short": {"pubkey": "POS"}}), -1))
    assert b["trueup_block"]["trade"] == "OPENSIG" and b["trueup_block"]["booked"] == pytest.approx(0.023703325)
    assert b["bal"] == pytest.approx(0.661282500) and "book_pend" in b
    for _ in range(4):                             # booking re-read still fails: row held, block stays
        sbot.bank_tick(_st(wallet=_w(sbot)), None, SOL_PX)
    assert "book_pend" in b and b["trueup_block"]["trade"] == "OPENSIG"
    assert b["hwm"] == pytest.approx(0.63)  # flat, but the HWM is held while the true-up is blocked
    b["wd_last_date"] = None
    sbot.bank_daily(_st(), None)
    assert sbot._staked == [] and b["wd_last_date"] is None and b["wd_mark"] == pytest.approx(0.63)
    sbot._flows["v"] = 0.001155254                 # flows readable again: the booking applies the true-up
    sbot.bank_tick(_st(wallet=_w(sbot)), None, SOL_PX)
    assert "trueup_block" not in b and "book_pend" not in b
    assert b["bal"] == pytest.approx(0.661282500 - 0.022548071, abs=1e-9)
    sbot.bank_tick(_st(wallet=_w(sbot)), None, SOL_PX)
    assert b["hwm"] == pytest.approx(0.661282500 - 0.022548071, abs=1e-9)
    sbot.bank_daily(_st(), None)                   # unblocked: withdraws the TRUE gain only
    gain = 0.661282500 - 0.022548071 - 0.63
    assert sbot._staked and sbot._staked[0] == pytest.approx(round(sbot.cfg.ledger.withdraw_fraction * gain, 9), abs=1e-9)


def test_block_of_an_older_trade_is_not_cleared_by_another_trade(sbot):
    b = sbot.local.bank
    b["trueup_block"] = {"since": "x", "trade": "OLDSIG", "booked": 0.01}
    ledger.on_open("2026-10-27 04:45:52+00:00", -1, 0.9, "NEWSIG", 5000, position="POS")
    sbot.bank_flat(sbot.book_flat(dict(_st(), positions={"long": {}, "short": {"pubkey": "POS"}}), -1))
    sbot.bank_tick(_st(wallet=_w(sbot)), None, SOL_PX)
    assert b["trueup_block"]["trade"] == "OLDSIG"  # this trade booked and trued up, the old block still stands
    assert b["bal"] == pytest.approx(0.661282500 - 0.022548071, abs=1e-9)


def test_pending_cancel_at_flat_gets_its_rent_back_and_books_exactly(sbot):
    """2026-10-26 11:28 MT shape: the resting add's cancel was still pending at flat, so the flat flows counted its
    collateral + request rent as gone; the later refund returns both, and the booking re-read nets to the chain."""
    b = sbot.local.bank
    nat = 0.098379255
    b["trade_dep"], b["trade_recv"] = 0.098264765 + nat, 0.100031722 - K.REQ_RENT
    ledger.on_open("2026-10-26 15:00:00+00:00", -1, 0.9, "SIG2", 5000, position="POS")
    sbot.local.pending_cancels = [{"pubkey": "GPG", "change": "increase", "ra": {"debited": True, "nat": nat}}]
    sbot._flows["v"] = -0.100335898
    start = b["bal"]
    booked_net = 0.100031722 - K.REQ_RENT - 0.098264765 - nat
    sbot.bank_flat(sbot.book_flat(dict(_st(), positions={"long": {}, "short": {"pubkey": "POS"}}), -1))
    assert sbot.local.pending_cancels[0]["ra"]["rent_out"] is True
    sbot.retry_cancels(_st(), None)               # gone on-chain: refund collateral + rent
    assert b["bal"] == pytest.approx(start + (-0.100335898 - booked_net) + nat + K.REQ_RENT, abs=1e-9)
    sbot._flows["v"] = 0.001756751
    for _ in range(4):
        sbot.bank_tick(_st(wallet=_w(sbot)), None, SOL_PX)
    assert "book_pend" not in b
    assert b["bal"] == pytest.approx(start + (0.001756751 - booked_net), abs=1e-9)   # bank net = chain net


def test_eth_btc_bank_flat_never_blocks(monkeypatch, tmp_path):
    import live.bot as B
    cfg = load_config("configs/live_eth.toml", ["live.dry_run=true", f"bot.state_dir={tmp_path.name}"])
    monkeypatch.setattr(B, "ROOT", tmp_path.parent)
    e = B.Bot(cfg)
    e.live = dataclasses.replace(e.live, dry_run=False)
    e.local.bank = K.new_bank("ETH", 0.02, 0.02)
    e.bank_flat(None)
    assert "trueup_block" not in e.local.bank and e.local.bank["bal"] == 0.02


def test_forced_booking_at_a_new_open_does_not_true_up_again(sbot):
    """The position PDA is shared across trades: at a new open its flows may already hold the new deposit, so the forced
    booking books the row but leaves the bank to the flat true-up already applied."""
    b = sbot.local.bank
    ledger.on_open("2026-10-26 04:45:52+00:00", -1, 0.9, "OPENSIG", 5000, position="POS")
    sbot.bank_flat(sbot.book_flat(dict(_st(), positions={"long": {}, "short": {"pubkey": "POS"}}), -1))
    bal = b["bal"]
    sbot._flows["v"] = -0.1                        # would include the new trade's collateral
    sbot.bank_book_pending(SOL_PX, SOL_PX, "new trade opened")
    assert "book_pend" not in b and b["bal"] == bal
