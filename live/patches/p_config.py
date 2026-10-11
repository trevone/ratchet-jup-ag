"""Integration patch (multi-asset) for config.py. Usage: python p_config.py <root>"""
import sys
from pathlib import Path
R = Path(sys.argv[1])


def rep(rel, old, new, count=1):
    p = R / rel
    s = p.read_text()
    n = s.count(old)
    if n != count:
        raise SystemExit(f"{rel}: expected {count} occurrence(s), found {n}: {old[:80]!r}")
    p.write_text(s.replace(old, new))


rep("config.py", '''WITHDRAW_ADDRESS = "59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp"  # the only allowed destination''',
'''@dataclass(frozen=True)
class BotConfig:
    """Multi-asset instance (2026-10-07 20:23 MT, Trevor): SOL / ETH / BTC bots trade from ONE wallet. Each instance has
    its own market, wallet share, state dir (state.json, trades.csv, ledger_*.json, KILL, PAUSE_UNTIL, logs) and process.
    Defaults = the original single SOL bot (asset SOL, share 1.0, state dir live/)."""
    asset: Literal["SOL", "ETH", "BTC"] = "SOL"   # Jupiter Perps market (custody); margin is always native SOL
    share: float = 1.0                   # fraction of the wallet budget (live/budget.py); shares across bots must sum <= 1
    state_dir: str = "live"              # relative to the repo root
    extra_pause_files: tuple[str, ...] = ()   # other PAUSE_UNTIL files this bot also obeys (e.g. the SOL operator's)
    global_kill_path: str = "live/KILL_ALL"   # present -> no new orders on ANY bot (closes still allowed)
    sweep_enabled: bool = True           # SOL in / SOL out: swap USDC / ETH / WBTC leftovers back to SOL (live/sweep.py)
    sweep_min_usd: float = 0.10          # smaller leftovers are dust (left alone)

    def __post_init__(self) -> None:
        if not 0.0 < self.share <= 1.0:
            raise ValueError("bot.share must be in (0, 1]")


@dataclass(frozen=True)
class ScaleConfig:
    """Per-asset price scaling (asset_scale.py). Off = the USD values in [dca]/[tp] are used as written (SOL)."""
    enabled: bool = False
    atr_tf: str = "1h"                   # own ATR: Wilder ATR(atr_len) on this TF from the bot's own klines
    atr_len: int = 14
    trigger_dist_atr: float | None = None    # dca.trigger_dist = this * own ATR (fixed spacing / levels fallback)
    trigger_dist_pct: float | None = None    # ... or this * price (used only if trigger_dist_atr is unset)
    deep_buffer_pct: float | None = None     # tp.deep_buffer_usd = this * price
    tp_min_gap_pct: float | None = None      # tp.tp_min_gap_usd = this * price
    tp_min_move_pct: float | None = None     # tp.tp_min_move = this * price
    tp_market_gap_pct: float | None = None   # tp.tp_market_gap = this * price
    own_atr_fallback: bool = False       # analyst file missing/stale -> Levels(no S/R, ATR = own ATR) instead of None


WITHDRAW_ADDRESS = "59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp"  # the only allowed destination''')

rep("config.py", '''    stake_api_bases: tuple[str, ...] = ("https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1")  # tried in order
''', '''    stake_api_bases: tuple[str, ...] = ("https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1")  # tried in order
    # multi-asset: realized profit of every bot's trade file goes into ONE daily stake (run only by the bot with
    # withdraw.enabled = true, i.e. SOL). Per-file cutoffs, so no trade is counted twice or skipped.
    trade_files: tuple[str, ...] = ("live/trades.csv",)
''')

rep("config.py", '''    withdraw: WithdrawConfig = field(default_factory=WithdrawConfig)

    @property''', '''    withdraw: WithdrawConfig = field(default_factory=WithdrawConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    scale: ScaleConfig = field(default_factory=ScaleConfig)

    @property''')
print("config.py patched")
