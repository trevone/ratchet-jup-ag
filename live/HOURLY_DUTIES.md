# Grok Bot hourly duties (Trevor, 2026-10-28)

Every hour, Grok Bot works through EVERY point below and writes its decision on EACH point to
`live/hourly_decisions.log` (one block per hour, headed with the time in MT, points numbered 1-16).
No point may be skipped or written as just "ok" / "quiet": each one states what was checked, the numbers, and the decision.

1. **Bots alive** - all three services running, ticking, sweep lines recent; any errors or halts since last hour.
2. **Events since last hour** - every fill, add, partial, guard re-place, close, deposit, withdrawal (listed, none hidden).
3. **Market read (my own)** - per open position: price vs avg, 1h and 4h trend (ADX/DI/RSI), nearest support/resistance with touches, last 1h move, volatility vs normal. My call: is the move with or against us.
4. **Levels script risk call** - every reading since last hour (history.jsonl, eth/, btc/), its reasons, whether I agree and why.
5. **TP3 decision** - per bot: current TP3 price and size; moved or not, old -> new, why; the 0.2% minimum-profit check.
6. **TP2 decision** - only for trades with no partial yet (guard not armed); moved or not and why. Guarded trades: TP1/TP2 sit at the guard price by design.
   - **TP2 for more profit (Trevor, 2026-10-28 20:57 MT):** TP2 placement is my judgment each check, not a formula. Goal: more profit per trade, because thin wins are too small versus losses. Read trend, momentum, volatility and the support map; when the move favors the short, push TP2 further toward a support the move is likely to reach (sensible, not ridiculously deep); when it doesn't, keep TP2 in front of the nearest strong support. Never below the 0.2% net floor. TP1 unchanged. Log MOVE/HOLD with the evidence and my reasoning.
7. **Profit guard** - armed on every trade with adds/partials; TPs on the safe side of the guard target; bank vs target; no re-place loop.
8. **Closed trades** - each close since last hour: overall net profit (coin and $), partials taken, bank vs guard target.
9. **Liquidation distance** - per bot, from the live position; anything within 3% -> rescue decision and alert.
10. **Next add** - per bot: price and size; whether I recommend pausing adds (Trevor decides) and why.
11. **Direction** - each bot's sides vs the 4h trend / EMA200; recommend a change or not (Trevor decides).
12. **Deposits** - new USDC, swaps landed, each bot credited exactly.
13. **Withdrawals / JupSOL** - last nightly withdrawal ran, amounts, errors.
14. **Inter-bot loan** - status, repay errors.
15. **Outside flows and droplet health** - EXT_FLOW_ALERT, reboot required, disk, state backup fresh.
16. **Message to Trevor** - sent or not, and why.

Reserved for Trevor (propose only): pausing adds, closing positions, cancelling orders by hand, marks (HWM / wd_mark), strategy and code changes.
Restarts: one bot at a time, each right after its own sweep: line.

This file is the master reference (Trevor, 2026-10-28: "your Bible"). Every hourly check reads it first and follows it. Any change to the duties is made here first, then committed and pushed to GitHub the same hour, with a line in live/changes.log.

TP DUTIES ARE MANDATORY (Trevor, 2026-10-28 08:43 MT): points 5 (TP3) and 6 (TP2) are done on EVERY check, with no exceptions. Each run logs, per bot, the current TP price and size, then MOVE (old -> new, with the evidence) or HOLD (the exact reason, with the numbers). Credit saving never cuts the TP work or the trading checks; save credits elsewhere.
