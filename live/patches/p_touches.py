"""tp_res_min_touches: smart-TP2 resistance/support cap ignores analyst levels with fewer touches (Trevor 20:44/20:46 MT)."""
import sys
from pathlib import Path
root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
def sub(p, old, new):
    f = root / p; s = f.read_text(); assert s.count(old) == 1, (p, old[:60]); f.write_text(s.replace(old, new))

sub("dca_levels.py", '''    resistances: tuple[float, ...]
    atr: float                        # 1h ATR, USD
''', '''    resistances: tuple[float, ...]
    atr: float                        # 1h ATR, USD
    # analyst touch counts aligned with supports / resistances (None = unknown -> treated as strong); () = all unknown.
    # Counts already include touches of the same level on the other TF (analyst merge rule: within 0.25 x 1h ATR).
    sup_touches: tuple[int | None, ...] = ()
    res_touches: tuple[int | None, ...] = ()
''')
sub("dca_levels.py", '''        sup = tuple(float(x) for tf in ("h1", "h4") for x in lv.get(tf, {}).get("support", []))
        res = tuple(float(x) for tf in ("h1", "h4") for x in lv.get(tf, {}).get("resistance", []))
        atr = float(d["volatility"]["atr14_1h_usd_per_hour"])
        return Levels(sup, res, atr) if atr > 0 else None''', '''        sup = tuple(float(x) for tf in ("h1", "h4") for x in lv.get(tf, {}).get("support", []))
        res = tuple(float(x) for tf in ("h1", "h4") for x in lv.get(tf, {}).get("resistance", []))
        atr = float(d["volatility"]["atr14_1h_usd_per_hour"])
        if atr <= 0:
            return None
        return Levels(sup, res, atr, sup_touches=_touches(lv, "support", atr), res_touches=_touches(lv, "resistance", atr))''')
sub("dca_levels.py", '''def liq_price(side: int, size: float''', '''def _touches(lv: dict, kind: str, atr: float, merge_atr: float = 0.25) -> tuple[int | None, ...]:
    """Touch count per level in h1+h4 order (same order as Levels.supports / resistances). A level's count adds the
    touches of other-TF levels within ``merge_atr`` x ATR (the analyst's h1/h4 merge rule). Missing / mismatched
    ``<kind>_touches`` lists -> None (unknown = strong)."""
    raw: list[tuple[float, str, int | None]] = []
    for tf in ("h1", "h4"):
        px = lv.get(tf, {}).get(kind, [])
        t = lv.get(tf, {}).get(f"{kind}_touches")
        ok = isinstance(t, list) and len(t) == len(px)
        for i, x in enumerate(px):
            n = t[i] if ok else None
            raw.append((float(x), tf, int(n) if isinstance(n, (int, float)) else None))
    out: list[int | None] = []
    for x, tf, n in raw:
        if n is None:
            out.append(None)
            continue
        extra = [m for y, tf2, m in raw if tf2 != tf and abs(y - x) <= merge_atr * atr]
        out.append(None if any(m is None for m in extra) else n + sum(extra))
    return tuple(out)


def liq_price(side: int, size: float''')
sub("dca_levels.py", '''    swing_buffer_atr: float = 0.05
    open_fee: float = 0.0006''', '''    swing_buffer_atr: float = 0.05
    res_min_touches: int = 0          # >= 2: TP2 cap ignores levels with fewer analyst touches (unknown = strong)
    open_fee: float = 0.0006''')
sub("dca_levels.py", '''def nearest_opposing_tp_level(side: int, avg: float, lv: Levels | None) -> float | None:
    """Nearest analyst resistance above ``avg`` (longs) / support below it (shorts)."""
    if lv is None:
        return None
    pool = lv.resistances if side == LONG else lv.supports
    c = [x for x in pool if side * (x - avg) > 0]''', '''def nearest_opposing_tp_level(side: int, avg: float, lv: Levels | None, min_touches: int = 0) -> float | None:
    """Nearest analyst resistance above ``avg`` (longs) / support below it (shorts). ``min_touches`` > 1 skips levels
    known to have fewer touches (unknown touch count = strong, kept)."""
    if lv is None:
        return None
    pool = lv.resistances if side == LONG else lv.supports
    tch = lv.res_touches if side == LONG else lv.sup_touches
    strong = lambda i: (min_touches <= 1 or len(tch) != len(pool) or tch[i] is None   # noqa: E731
                        or tch[i] >= min_touches)
    c = [x for i, x in enumerate(pool) if side * (x - avg) > 0 and strong(i)]''')
sub("dca_levels.py", '''    r = nearest_opposing_tp_level(s, avg, lv)''', '''    r = nearest_opposing_tp_level(s, avg, lv, p.res_min_touches)''')
sub("dca_levels.py", '''            reasons.append(f"{lvl(last)} {prices[last]:.3f}->{cap:.3f}: under {name_lv} {r:.2f} - "
                           f"{p.res_buffer_atr:g} ATR ({p.res_buffer_atr * lv.atr:.3f})")''', '''            reasons.append(f"{lvl(last)} {prices[last]:.3f}->{cap:.3f}: under {name_lv} {r:.2f} - "
                           f"{p.res_buffer_atr:g} ATR ({p.res_buffer_atr * lv.atr:.3f})"
                           + (f" [levels with < {p.res_min_touches} touches ignored]" if p.res_min_touches > 1 else ""))''')
sub("config.py", '''    tp_swing_buffer_atr: float = 0.05    # faded TP2 = max(fee floor, best price - this many ATR)
''', '''    tp_swing_buffer_atr: float = 0.05    # faded TP2 = max(fee floor, best price - this many ATR)
    tp_res_min_touches: int = 0          # >= 2: TP2 cap skips analyst levels with fewer touches (0/1 = every level caps)
''')
sub("config.py", '''        if not 0.0 < self.tp_near_miss_frac <= 1.0:''', '''        if self.tp_res_min_touches < 0:
            raise ValueError("tp.tp_res_min_touches must be >= 0")
        if not 0.0 < self.tp_near_miss_frac <= 1.0:''')
sub("strategy.py", '''                             fade_rsi_drop=t.tp_fade_rsi_drop, swing_buffer_atr=t.tp_swing_buffer_atr,
                             open_fee=c.open_fee''', '''                             fade_rsi_drop=t.tp_fade_rsi_drop, swing_buffer_atr=t.tp_swing_buffer_atr,
                             res_min_touches=t.tp_res_min_touches, open_fee=c.open_fee''')
print("ok")
