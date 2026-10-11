# Ratchet - Jupiter Perps bots (SOL / ETH / BTC)

Ratchet runs three Jupiter Perps bots (SOL, ETH, BTC) from one wallet. Full strategy: [STRATEGY.md](STRATEGY.md). Knobs: [SETTINGS.md](SETTINGS.md). Droplet ops: [live/DROPLET.md](live/DROPLET.md).

## Strategy in brief
- **Per-bot own-coin ledger** (`live/bank.py`): each bot trades only its own coin (SOL / Wormhole ETH / WBTC), no cross-bot borrowing. High-water mark (HWM) only rises while flat.
- **Entries:** 4h EMA200 picks the side; 15m RSI < 45 long / > 55 short (loosens when idle). First entry = 10% of free bank (min $10.50).
- **DCA add ladder** from analyst S/R levels (`/workspace/market-analysis/{,eth/,btc/}latest.json`), 4-14 rungs, accelerating weights, deep level 6-12 ATR; ATR fallback when levels are stale.
- **Leverage:** 5x start, +1x per add, cap 20x.
- **Take profit:** TP1 (50%) and TP2 placed on-chain at open/add; TP1 = 60% of smart TP2 distance; TP2 capped under resistance.
- **Trim:** arms at 0.65% against avg, then closes profitable adds just before the next S/R level.
- **Profit guard:** market-closes the whole position when equity-if-closed >= HWM + `[ledger] min_profit_native` (trades with adds or trims).
- **Daily 00:05 MT:** 50% of HWM gain per bot -> JupSOL.
- No stop-loss; never close to cut a loss.

## Layout
- `config.py`, `strategy.py`, `dca_levels.py`, `broker.py`, `indicators.py`, ... - core strategy / sizing / backtest code
- `configs/` - `live_100.toml` (SOL), `live_eth.toml`, `live_btc.toml`, backtest configs
- `live/` - live runner (`bot.py`, `bank.py`, `ledger.py`, `trim.py`, `withdraw.py`, `stake.py`, ops scripts), `src/` TypeScript Jupiter tx tooling, `patches/` one-off ops scripts
- `market-analysis/` - snapshot of the analyst scripts from `/workspace/market-analysis` (levels/outlook/newsgate; outputs excluded)
- `tests/` - pytest (never touches chain), `research/` - ladder sims

## Droplet deployment
Host `161.35.1.72` (Ubuntu 24.04, TZ America/Denver). User `ratchet` owns `/workspace/sol-bot` and `/workspace/market-analysis`.
Python venv `/workspace/sol-bot/.venv` (`requirements.lock.txt`), Node 20 (`cd live && npm ci`).

Secrets live in `/home/ratchet/.secrets/` (wallet keypair JSON + env file with RPC key and key path), mode 600, loaded via systemd `EnvironmentFile`. Never committed, never printed.

systemd services (Restart=always, enabled at boot):

| Service | Runs | Logs |
|---|---|---|
| ratchet-sol | `python -m live.bot --config configs/live_100.toml` | live/logs/ |
| ratchet-eth | `... configs/live_eth.toml` | live/eth/logs/ |
| ratchet-btc | `... configs/live_btc.toml` | live/btc/logs/ |
| ratchet-levels | `/workspace/market-analysis/refresh_loop.sh` | refresh_loop.log |

```bash
SSH="ssh -i /home/box/.ssh/ratchet_droplet ratchet@161.35.1.72"
$SSH 'systemctl status ratchet-sol ratchet-eth ratchet-btc ratchet-levels --no-pager'
$SSH 'sudo systemctl restart ratchet-eth'
$SSH 'tail -f /workspace/sol-bot/live/logs/bot.log'      # or journalctl -u ratchet-sol -f
$SSH 'touch /workspace/sol-bot/live/KILL_ALL'            # stop new orders on all bots (per bot: live/KILL, live/eth/KILL, live/btc/KILL)
bash /workspace/sol-bot/live/quickcheck.sh               # status from the box
```
Log rotation: `/etc/logrotate.d/ratchet`. Change notes: `live/changes.log`.
