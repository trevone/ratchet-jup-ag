"""Analyst null/missing/empty h1/h4 levels -> 'no levels' (same as a stale file), never crash (analyst 20:48 MT)."""
import sys
from pathlib import Path
root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
def sub(p, old, new):
    f = root / p; s = f.read_text(); assert s.count(old) == 1, (p, old[:60]); f.write_text(s.replace(old, new))

sub("dca_levels.py", '''        lv = d["levels"]
        sup = tuple(float(x) for tf in ("h1", "h4") for x in lv.get(tf, {}).get("support", []))
        res = tuple(float(x) for tf in ("h1", "h4") for x in lv.get(tf, {}).get("resistance", []))
        atr = float(d["volatility"]["atr14_1h_usd_per_hour"])
        if atr <= 0:
            return None''', '''        # analyst writes h1/h4 = null when its candle fetch failed: null / missing / empty -> no levels (= stale file)
        lv = d.get("levels") if isinstance(d.get("levels"), dict) else {}
        lv = {tf: (lv.get(tf) if isinstance(lv.get(tf), dict) else {}) for tf in ("h1", "h4")}
        sup = tuple(float(x) for tf in ("h1", "h4") for x in (lv[tf].get("support") or []) if x is not None)
        res = tuple(float(x) for tf in ("h1", "h4") for x in (lv[tf].get("resistance") or []) if x is not None)
        if not sup and not res:
            _warn_once(str(path), "analyst file has no h1/h4 levels (null/empty) -> treated as no levels "
                                  "(stale-file behaviour: own-ATR spacing if enabled, no S/R TP2 cap)")
            return None
        _LEVEL_WARNED.pop(str(path), None)
        atr = float(d["volatility"]["atr14_1h_usd_per_hour"])
        if atr <= 0:
            return None''')
sub("dca_levels.py", '''    except (OSError, KeyError, ValueError, TypeError):
        return None


def _touches(''', '''    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None


_LEVEL_WARNED: dict[str, str] = {}


def _warn_once(key: str, msg: str) -> None:
    if _LEVEL_WARNED.get(key) != msg:
        _LEVEL_WARNED[key] = msg
        logging.getLogger("bot").warning("%s: %s", key, msg)


def _touches(''')
sub("dca_levels.py", '''    for tf in ("h1", "h4"):
        px = lv.get(tf, {}).get(kind, [])
        t = lv.get(tf, {}).get(f"{kind}_touches")''', '''    for tf in ("h1", "h4"):
        d = lv.get(tf) if isinstance(lv.get(tf), dict) else {}
        px = [x for x in (d.get(kind) or []) if x is not None]
        t = d.get(f"{kind}_touches")''')
f = root / "dca_levels.py"; s = f.read_text()
if "\nimport logging\n" not in s:
    s = s.replace("\nimport json\n", "\nimport json\nimport logging\n", 1); assert "\nimport logging\n" in s; f.write_text(s)
print("ok")
