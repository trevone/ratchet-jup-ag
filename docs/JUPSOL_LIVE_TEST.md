# JupSOL loans: live test runbook (branch `jupsol-loans`, written 2026-10-27, for Trevor's review)

Nothing here has been run live. Every number below is a formula or an example from the offline tests, not a live value:
read the live values (bal, JupSOL, prices) first and work the formulas out before each step.

Files: `live/jupsol_loans.py` (logic), `live/bank.py` (loan / park booking), `live/rescue.py` (manual tool, same rules),
`[ledger] jupsol_loans` in each config (all three `"off"` on the branch), shared registry `live/jupsol_loans.json`
(+ `live/jupsol.lock`), test switches `live/IB_BORROW_OFF` and `live/JUPSOL_PARK` (SOL bot; ETH / BTC: `live/eth/`, `live/btc/`).

## 0. Before deploying
- Read-only checks: `bash live/rescue.sh status` (no `LOAN` on any bot), `cat live/ib_loan.json` (status `settled` or no
  file), no `live/EXT_FLOW_ALERT`, no `live/JUPSOL_PARK`, no `IB_BORROW_OFF` anywhere.
- With all three configs on `"off"`, the deploy changes only: (a) repayment of an existing JupSOL loan (none open) uses the
  fixed exact-JupSOL logic, (b) the stake.ts quote allowlist accepts JupSOL as a swap INPUT (output list unchanged),
  (c) the manual `rescue.sh` uses the new rules. Trading, inter-bot loans, deposits, withdrawals: unchanged.
- The TS CLI runs from source (`npx tsx src/cli.ts`), so (b) is live for every CLI call as soon as the files change, before
  any restart. The running Python processes keep the old code until restarted (the old code imports `live/rescue.py` lazily
  only when a JupSOL loan is open; there is none).
- `live/rescue_cfg.json` is tracked but matches the `live/**/*.json` ignore pattern. This branch drops the retired keys
  (collateral_share, repay_margin_pct, repay_profit_share, emergency_min_collat_pct). If a checkout still has them,
  they are harmless: the new code ignores them.

## 1. Deploy (one bot at a time, each right after its own `sweep:` line)
1. On the droplet, after Trevor's review: merge `origin/jupsol-loans` into main the usual way (fast-forward if main has
   not moved since 2653c96; otherwise re-run the tests after the merge:
   `sudo -u ratchet .venv/bin/python -m pytest -q tests` -> 487 passed on the branch).
2. SOL: `tail -f live/logs/bot.log | grep -m1 'sweep:'`, then immediately `sudo systemctl restart ratchet-sol`.
   Wait for the first full tick in the log (no traceback; `JUPSOL` lines are not expected in mode off).
3. ETH: same with `live/eth/logs/bot.log` and `ratchet-eth`. Then BTC: `live/btc/logs/bot.log`, `ratchet-btc`.
4. Never restart two at once. If a bot is mid-swap (deposit leg, inter-bot loan funding/repaying), wait for its next `sweep:`.

## 2. Make SOL short of free balance: park its free SOL in JupSOL (and back)
Why park and not withdraw: a withdrawal + deposit pair would move wd_mark. Park books the SOL as a receivable
(`bank.park`), so equity, hwm, wd_mark and the guard target stay put; only the free balance (bal) drops.

Amount to park, X (SOL): `X = bal - loan - 0.00002 - target_free_usd / sol_px`, with `target_free_usd` below the
$10 rung (e.g. $3, so the next-add shortfall is about $10 - $3 + $1 = $8). Refused if X > bal - loan - 0.00002.

    echo '{"op":"park","native":X}' | sudo -u ratchet tee live/JUPSOL_PARK

Processed on the next SOL tick that has no request in flight, no pending reconcile and no open payout window (any mode).
Result in `live/JUPSOL_PARK.result`; the request file disappears. Exact entries it makes:
- `bank.bal -= spent` (spent = measured SOL out of the wallet incl. the network fee), `bank.park = {native: spent,
  jupsol: got, opened}`, hist row `park` (-spent). `bank.hwm` / `bank.wd_mark` unchanged (asserted in code).
- `day_start_equity -= spent x sol_px` (daily-loss baseline follows the moved capital).
- `live/stake_ledger.csv` row kind `park` (jupsol_delta +got, wallet_sol_change, fee_sol, sig); totals
  `parked_jupsol_total` += got in `live/ledger_summary.json` (`net_jupsol` includes it).
- In a trade: `ledger.update(external_out_sol=spent)` on the open trade so its wallet-delta fallback ignores it.
- `live/jupsol_loans.json` `parked.SOL.jupsol` += got: parked JupSOL is never lent.
- `bank.window` = window_ticks (3), so the reconcile treats the tick's wallet change as accounted.
Log: `JUPSOL PARK SOL: <spent> SOL -> <got> JupSOL sig ... (equity / hwm / wd_mark unchanged)`.

Back (after the test):

    echo '{"op":"unpark"}' | sudo -u ratchet tee live/JUPSOL_PARK

All of `bank.park.jupsol` is swapped back: `bank.bal += got`, `bank.park` removed, hist row `unpark` (+got),
`day_start_equity += got x sol_px`, stake row `unpark` (jupsol_delta -parked), `unparked_jupsol_total` += parked,
registry entry removed. `got - spent` (swap costs both ways + the JupSOL rate move, a few hundredths of a % plus fees) lands
in bal through the log line `difference +/-x SOL through bal (swap cost / JupSOL move, not profit)`; hwm / wd_mark unchanged.

## 3. Disable peer borrowing for SOL (test switch)

    sudo -u ratchet touch live/IB_BORROW_OFF

SOL then never borrows from ETH / BTC (its own pending inter-bot request is withdrawn: `ib_loan.json` -> `settled`), in
any mode, no restart. It still lends to them as before. Remove after the test: `sudo -u ratchet rm live/IB_BORROW_OFF`.

## 4. Switch SOL to observe, then on
1. `configs/live_100.toml` `[ledger] jupsol_loans = "observe"`; restart SOL after its `sweep:` line. Watch for
   `JUPSOL observe: would lend $<need> (rung margin $10.00 - free $<free> + $1) for the next add (peer borrowing switched
   off (IB_BORROW_OFF)); nothing sent`. Check need = 10 - free + 1 (free = bal x sol_px).
2. Same edit with `"on"`; restart after its `sweep:` line. ETH / BTC stay `"off"`.
3. Log `live/changes.log` per the usual rule when this is done for real (not part of this branch).

## 5. What to watch (live/logs/bot.log, grep JUPSOL)
Next-add loan (SOL in a trade whose next resting rung needs $10 and free < $10):
- 1st tick: inter-bot request filed as today, unless IB_BORROW_OFF (then nothing). 2nd consecutive tick: decided.
- Next bank tick: `JUPSOL NEXT-ADD LOAN: <js> JupSOL -> <got> SOL (~$<usd>) sig <sig>; liability <got> SOL, <js> JupSOL owed;
  hwm X / wd_mark Y unchanged`. Expect `got x sol_px ~= need` (ExactOut: the SOL amount is exact, the JupSOL in floats) and
  `js ~= got / solPerJupsol` plus the swap cost (<= 50 bps slippage cap). No `(QUOTE fallback)` suffix (that would mean the
  tx deltas were missing).
- The following ticks: the normal `RESTING ADD` placement for the $10 rung.
- `bash live/rescue.sh status`: `LOAN <got> SOL (owes <js> JupSOL)`.
- `bank`: `loan.principal = got`, `loan.jupsol = js`, `loan.tranches[-1].kind = next_add`; `bal` up by got;
  `hwm`, `wd_mark` unchanged; `stake_ledger.csv` row `loan_out`.
On the flat close (after any inter-bot loan is repaid, the payout window closed, no pend / book_pend / trueup_block):
- `JUPSOL LOAN REPAID: <spent> SOL -> <js> JupSOL (owed <js>) sig ...; liability cleared <got>, cost <spent-got> SOL (through
  bal, not trade P&L); hwm / wd_mark unchanged`. JupSOL back == JupSOL lent (to the lamport, ExactOut). spent - got is the
  JupSOL rate move + swap cost between lend and repay (expect a small fraction of a % of got).
- If the balance can't buy it back: `PART-REPAID ... (carried to the next flat)` and status shows `carried to next flat`.
  Expected only if the trade lost most of the bot's coin.
Rescue (mode on, any bot in trigger): `JUPSOL RESCUE LOAN: ...`, then `JUPSOL RESCUE collateral sent: <coin> ... liq was a% ->
~b% (planned)`; `EMERGENCY` lines mean notify Trevor. `KILL present` -> `JUPSOL RESCUE skipped`.
Bad signs (stop: set `"off"` + restart): `JUPSOL tick error`, `outcome UNKNOWN` without a later booking, an
`EXT_FLOW_ALERT` line within a few ticks of a JUPSOL booking, hwm / wd_mark changing in a JUPSOL line.

## 6. Rollback
- Behaviour: set `jupsol_loans = "off"` (+ restart after `sweep:`), `rm live/IB_BORROW_OFF`, unpark (section 2). With "off"
  no new loan is made; an outstanding loan is still repaid on the next flat (exact JupSOL).
- Code: only after `bank.loan` and `bank.park` are gone on every bot (repaid / unparked), check out the previous main commit
  and restart each bot after its `sweep:`. The old code would repay an open loan as principal x 1.05 (with the partial
  write-off bug) and would not know `bank.park` (its equity would then drop by the parked amount): do not roll the code
  back while either exists.
- Manual unwind of a stuck op: `bank.jl_op` in state.json names the stage and sig; the bot never re-sends it. Check the tx
  (`tx-deltas`), book by hand with the service stopped, delete `bank.jl_op`.
