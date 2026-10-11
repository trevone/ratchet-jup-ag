#!/usr/bin/env python3
"""Multi-asset (SOL, ETH, BTC) market outlook for Jupiter Perps fixed-$ bots. Real data only; unavailable -> null + note.
Usage: python3 outlook.py [--assets SOL,ETH,BTC] [--out-root DIR] [--no-history]
Outputs (one run does all assets; one asset failing never blocks the others):
  SOL -> latest.json + history.jsonl            (paths unchanged; schema only gains fields; read live by the SOL bot)
  ETH -> eth/latest.json + eth/history.jsonl
  BTC -> btc/latest.json + btc/history.jsonl
Each file: regime, 1h/4h trend, momentum, levels (h1/h4 lists of floats, plus levels.supports/resistances = merged, levels.fine = near-price 15m/1h/pdh/round,
deduplicated, nearest-first objects carrying their timeframe), ATR, Jupiter borrow rate for that custody, 4h EMA200 +
side_flip (bot direction filter), recommendation (side, leverage, TP1/TP2 $, DCA $, close timer, pause_new_entries)
scaled to the asset's own price/ATR, current_bot_config (live TOML, null if none) + comparison, events tagged for the
asset or "all" (events.json, managed by events_update.py), event_pause_active/windows, regime_change_flag (own history).
"""
import json, os, sys, glob, math, datetime, tempfile, traceback, urllib.request, tomllib
D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
import events_update as EV  # events.json helpers (schema normalisation, asset tags, pause windows)

BOT_ROOT = "/workspace/sol-bot"
ASSETS = {
    "SOL": dict(symbol="SOL-USD", kraken="SOLUSD", coinbase="SOL-USD", binance="SOLUSDT",
                mint="So11111111111111111111111111111111111111112", subdir="", borrow_key="jupiter_sol_borrow_rate",
                config_path=f"{BOT_ROOT}/configs/live_100.toml"),
    "ETH": dict(symbol="ETH-USD", kraken="ETHUSD", coinbase="ETH-USD", binance="ETHUSDT",
                mint="7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", subdir="eth", borrow_key="jupiter_eth_borrow_rate",
                config_path=None),
    "BTC": dict(symbol="BTC-USD", kraken="XBTUSD", coinbase="BTC-USD", binance="BTCUSDT",
                mint="3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", subdir="btc", borrow_key="jupiter_btc_borrow_rate",
                config_path=None),
}
# where to look for ETH/BTC bot configs (files whose [market].symbol or file name names the asset)
CONFIG_GLOBS = [f"{BOT_ROOT}/configs/*.toml", "/workspace/*-bot/configs/*.toml", "/workspace/*/configs/*.toml",
                "/workspace/*-bot/*.toml"]
BOT_KLINE_PAGES, BOT_SIGNAL_TF_MIN = 5, 15     # live/bot.py: 5 x 1000 15m Binance klines -> 4h EMA200 (adjust=False)
EMA_LEN = 200

def get(u):
    r = urllib.request.Request(u, headers={"User-Agent": "market-outlook/2.0"})
    return json.load(urllib.request.urlopen(r, timeout=20))

# ---------------------------------------------------------------- indicators (unchanged maths from the SOL version)
def ema(x, n):
    k, out = 2/(n+1), [x[0]]
    for v in x[1:]: out.append(v*k + out[-1]*(1-k))
    return out
def wilder(x, n):
    out = [sum(x[:n])/n]
    for v in x[n:]: out.append((out[-1]*(n-1)+v)/n)
    return out
def tr(c): return [max(c[i]["h"]-c[i]["l"], abs(c[i]["h"]-c[i-1]["c"]), abs(c[i]["l"]-c[i-1]["c"])) for i in range(1, len(c))]
def atr(c, n=14): return wilder(tr(c), n)[-1]
def rsi(cl, n=14):
    d = [cl[i]-cl[i-1] for i in range(1, len(cl))]
    g, l = wilder([max(x,0) for x in d], n)[-1], wilder([max(-x,0) for x in d], n)[-1]
    return 100.0 if l == 0 else 100-100/(1+g/l)
def adx(c, n=14):
    p, m = [], []
    for i in range(1, len(c)):
        up, dn = c[i]["h"]-c[i-1]["h"], c[i-1]["l"]-c[i]["l"]
        p.append(up if up > dn and up > 0 else 0); m.append(dn if dn > up and dn > 0 else 0)
    t, sp, sm = wilder(tr(c), n), wilder(p, n), wilder(m, n)
    dx, pdi, mdi = [], 0, 0
    for a, b, cc in zip(t, sp, sm):
        pdi, mdi = 100*b/a, 100*cc/a
        dx.append(0 if pdi+mdi == 0 else 100*abs(pdi-mdi)/(pdi+mdi))
    return wilder(dx, n)[-1], pdi, mdi
def macd_hist(cl):
    m = [a-b for a, b in zip(ema(cl, 12), ema(cl, 26))]
    return m[-1]-ema(m, 9)[-1]

def analyze(c):
    if not c or len(c) < 60: return None
    cl = [x["c"] for x in c]
    e20, e50 = ema(cl, 20)[-1], ema(cl, 50)[-1]
    a, pdi, mdi = adx(c)
    px = cl[-1]
    if a >= 20 and e20 > e50 and px > e20 and pdi > mdi: reg = "uptrend"
    elif a >= 20 and e20 < e50 and px < e20 and mdi > pdi: reg = "downtrend"
    else: reg = "range"
    return dict(regime=reg, ema20=round(e20,3), ema50=round(e50,3), adx14=round(a,2),
                plus_di=round(pdi,2), minus_di=round(mdi,2), rsi14=round(rsi(cl),2),
                macd_hist=round(macd_hist(cl),4), atr14_usd=round(atr(c),3), last_close=px)

def nice_step(price):
    """$ rounding step on a 1-2-5 ladder at ~0.05% of price: SOL ~$116 -> 0.05, ETH ~$2.6k -> 1, BTC ~$83k -> 20."""
    raw = price*0.0005; e = 10**math.floor(math.log10(raw))
    return max(m*e for m in (1, 2, 5) if m*e <= raw)

# ---------------------------------------------------------------- S/R levels
def pivots(c, price, k=3):
    w = c[-120:]; sup, res = [], []
    for i in range(k, len(w)-k):
        if w[i]["l"] == min(x["l"] for x in w[i-k:i+k+1]): sup.append(w[i]["l"])
        if w[i]["h"] == max(x["h"] for x in w[i-k:i+k+1]): res.append(w[i]["h"])
    def cluster(v):  # pivots within 0.4% of price merge into one level (mean)
        v, out = sorted(v), []
        for x in v:
            if out and abs(x-out[-1][-1]) < price*0.004: out[-1].append(x)
            else: out.append([x])
        return [(round(sum(g)/len(g), 2), len(g)) for g in out]
    return w, cluster(sup), cluster(res)

def levels(c, price, k=3):
    """Per-TF levels (same shape as the SOL schema): supports below price descending (nearest first), resistances above
    ascending (nearest first), deduplicated, top 3 each. Extra fields: support_touches/resistance_touches (pivot count)."""
    w, cs, cr = pivots(c, price, k)
    s = sorted({p: n for p, n in cs if p < price}.items(), key=lambda x: -x[0])[:3]
    r = sorted({p: n for p, n in cr if p > price}.items(), key=lambda x: x[0])[:3]
    return dict(support=[p for p, _ in s], resistance=[p for p, _ in r],
                swing_low_120=round(min(x["l"] for x in w),2), swing_high_120=round(max(x["h"] for x in w),2),
                support_touches=[n for _, n in s], resistance_touches=[n for _, n in r])

def merged_levels(lv, price, atr1h):
    """h1+h4 levels merged into one nearest-first list per side, each tagged with its timeframe. Levels from both TFs
    closer than 0.25 x 1h ATR collapse into one (the 4h price is kept, tf='4h', also_on=['1h'])."""
    if not price: return [], []
    tol = 0.25*atr1h if atr1h else price*0.002
    out = {}
    for side, key in (("support", "support"), ("resistance", "resistance")):
        items = []
        for tf, name in (("4h", "h4"), ("1h", "h1")):
            for p in ((lv.get(name) or {}).get(key) or []):
                hit = next((x for x in items if abs(x["price"]-p) < tol), None)
                if hit: hit.setdefault("also_on", []).append(tf) if tf not in hit.get("also_on", []) else None
                else: items.append(dict(price=p, tf=tf))
        items.sort(key=lambda x: -x["price"] if side == "support" else x["price"])
        for x in items:
            d = abs(price-x["price"]); x["distance_usd"] = round(d, 2)
            x["distance_atr1h"] = round(d/atr1h, 2) if atr1h else None
        out[side] = items
    return out["support"], out["resistance"]

def _pivot_hl(c, k=2):
    """Pivot lows/highs with confirmation window k on each side. Returns (lows, highs) lists."""
    lows, highs = [], []
    if not c or len(c) < 2 * k + 1: return lows, highs
    for i in range(k, len(c) - k):
        win = c[i - k:i + k + 1]
        if c[i]["l"] == min(x["l"] for x in win): lows.append(c[i]["l"])
        if c[i]["h"] == max(x["h"] for x in win): highs.append(c[i]["h"])
    return lows, highs

def _touches_at(c, level, tol):
    """Count candles whose range overlaps level within tol."""
    if not c: return 0
    n = 0
    for x in c:
        if x["l"] - tol <= level <= x["h"] + tol: n += 1
    return n

def _round_candidates(name, price, lo, hi):
    """Sparse round-number levels inside [lo, hi]. SOL $1/$5, ETH $25/$50, BTC $500/$1000."""
    if name == "SOL": steps = (1.0, 5.0)
    elif name == "ETH": steps = (25.0, 50.0)
    else: steps = (500.0, 1000.0)
    out = []
    for step in steps:
        x = math.ceil(lo / step) * step
        while x <= hi + 1e-9:
            if abs(x - price) / price >= 0.0015:  # skip levels essentially at spot
                out.append((x, step))
            x = round(x + step, 10)
    return out

def fine_levels(name, price, atr1h, c15, c1h, notes):
    """Fine-grained S/R near price for tighter ladder spacing. Real structure only; empty if 15m unavailable.
    Schema matches levels.supports/resistances (price, tf, distance_usd, distance_atr1h) plus touches."""
    empty = dict(supports=[], resistances=[])
    if not price:
        notes.append("fine levels: no price"); return empty
    if not c15:
        notes.append("fine levels: 15m candles unavailable -> empty fine lists")
        return empty

    # Windows: ~2 days of 15m (192 bars), ~2 days of 1h (48 bars)
    w15 = c15[-192:] if len(c15) > 192 else list(c15)
    w1h = (c1h[-48:] if len(c1h) > 48 else list(c1h)) if c1h else []
    merge_tol = price * 0.0015          # ~0.15%
    near_hi = price * 0.04              # keep within ~4% of price
    near_lo = price * 0.02              # prefer within ~2%, allow out to 4%
    band_lo, band_hi = price - near_hi, price + near_hi

    # Collect raw candidates: (price, tf, touches)
    raw = []

    lows15, highs15 = _pivot_hl(w15, k=2)
    for p in lows15: raw.append(dict(price=p, tf="15m", touches=1))
    for p in highs15: raw.append(dict(price=p, tf="15m", touches=1))

    if w1h:
        lows1, highs1 = _pivot_hl(w1h, k=2)
        for p in lows1: raw.append(dict(price=p, tf="1h", touches=1))
        for p in highs1: raw.append(dict(price=p, tf="1h", touches=1))

    # Intraday high/low (UTC today) from 15m
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    day0 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [x for x in w15 if x["t"] >= day0]
    if today:
        idl, idh = min(x["l"] for x in today), max(x["h"] for x in today)
        if band_lo <= idl < price:
            raw.append(dict(price=idl, tf="intraday", touches=_touches_at(w15, idl, merge_tol) or 1))
        if price < idh <= band_hi:
            raw.append(dict(price=idh, tf="intraday", touches=_touches_at(w15, idh, merge_tol) or 1))

    # Prior UTC day high/low from 1h (fallback 15m)
    y0 = day0 - 86400
    src_pd = w1h if w1h else w15
    ybars = [x for x in src_pd if y0 <= x["t"] < day0]
    if ybars:
        pdl, pdh = min(x["l"] for x in ybars), max(x["h"] for x in ybars)
        if band_lo <= pdl < price:
            raw.append(dict(price=pdl, tf="pdl", touches=_touches_at(w15, pdl, merge_tol) or 1))
        if price < pdh <= band_hi:
            raw.append(dict(price=pdh, tf="pdh", touches=_touches_at(w15, pdh, merge_tol) or 1))

    # Round numbers: only keep if near other structure (within ~0.3%) or a major mark
    struct_prices = [r["price"] for r in raw]
    major = {"SOL": 5.0, "ETH": 50.0, "BTC": 1000.0}.get(name, 5.0)
    for x, step in _round_candidates(name, price, band_lo, band_hi):
        near_struct = any(abs(x - s) / price <= 0.003 for s in struct_prices)
        is_major = abs(x / major - round(x / major)) < 1e-9
        if not (near_struct or is_major):
            continue
        # still require it to sit near some wick activity, else skip invented emptiness
        t = _touches_at(w15, x, merge_tol)
        if t < 1 and not near_struct:
            continue
        raw.append(dict(price=x, tf="round", touches=max(t, 1)))

    # Filter to band (supports below, resistances above) then cluster within 0.15%
    # Prefer structural tf over round when merging; sum touches; average price weighted by touches
    tf_rank = {"pdl": 0, "pdh": 0, "intraday": 1, "1h": 2, "15m": 3, "round": 4}

    def cluster(side_items):
        side_items = sorted(side_items, key=lambda x: x["price"])
        groups = []
        for it in side_items:
            if groups and abs(it["price"] - groups[-1][-1]["price"]) <= merge_tol:
                groups[-1].append(it)
            else:
                groups.append([it])
        out = []
        for g in groups:
            tw = sum(max(1, x.get("touches") or 1) for x in g)
            px = sum(x["price"] * max(1, x.get("touches") or 1) for x in g) / tw
            # best (most structural) tf wins; if tie, prefer the one closest to mean price
            best = min(g, key=lambda x: (tf_rank.get(x["tf"], 9), abs(x["price"] - px)))
            # recount wick touches at the merged price (avoids inflated sums from confluence)
            t = _touches_at(w15, px, merge_tol)
            out.append(dict(price=round(px, 2),
                            tf=best["tf"],
                            touches=max(t, len(g))))
        return out

    supports_raw = [r for r in raw if band_lo <= r["price"] < price * 0.9985]
    resists_raw = [r for r in raw if price * 1.0015 < r["price"] <= band_hi]
    supports = cluster(supports_raw)
    resists = cluster(resists_raw)

    # Prefer levels inside ~2%; fill out to 4% only if we have fewer than 4
    def pick(items, side):
        # nearest first: supports desc, resistances asc
        items = sorted(items, key=lambda x: -x["price"] if side == "support" else x["price"])
        inner = [x for x in items if abs(price - x["price"]) / price <= 0.02]
        chosen = list(inner)
        if len(chosen) < 4:
            for x in items:
                if x not in chosen and abs(price - x["price"]) / price <= 0.04:
                    chosen.append(x)
                if len(chosen) >= 6: break
        else:
            chosen = chosen[:6]
        # re-sort nearest-first after fill
        chosen = sorted(chosen, key=lambda x: -x["price"] if side == "support" else x["price"])[:6]
        for x in chosen:
            d = abs(price - x["price"])
            x["distance_usd"] = round(d, 2)
            x["distance_atr1h"] = round(d / atr1h, 2) if atr1h else None
        return chosen

    return dict(supports=pick(supports, "support"), resistances=pick(resists, "resistance"))


# ---------------------------------------------------------------- add-ladder supports/resistances (2026-10-09)
LADDER_MIN_RUNGS, LADDER_REACH_PCT, LADDER_MAX_RUNGS = 14, 10.0, 30
LADDER_GAP = (0.75, 1.0)          # allowed gap (x 1h ATR) price->rung1 and rung->rung; target = middle
SRC_RANK = {"pivot": 0, "swing": 1, "volume_node": 2, "fib": 3, "round": 4, "interpolated": 5}
REAL_SOURCES = ("pivot", "swing", "volume_node")

def _swings(c, k):
    lows, highs = [], []
    for i in range(k, len(c)-k):
        w = c[i-k:i+k+1]
        if c[i]["l"] == min(x["l"] for x in w): lows.append(c[i]["l"])
        if c[i]["h"] == max(x["h"] for x in w): highs.append(c[i]["h"])
    return lows, highs

def volume_nodes(cs, price, atr1h, lo, hi):
    """High-volume nodes: candle volume spread evenly over each candle's range into 0.2x1h-ATR bins inside [lo, hi];
    a node = bin that is a local max (+-2 bins) and above the median non-empty bin."""
    bw = 0.2*atr1h; n = int((hi-lo)/bw)+1
    if n < 5: return []
    vb = [0.0]*n
    for c in cs:
        v = c.get("v") or 0
        if v <= 0 or c["h"] < lo or c["l"] > hi: continue
        a, b = max(0, int((c["l"]-lo)/bw)), min(n-1, int((c["h"]-lo)/bw))
        for j in range(a, b+1): vb[j] += v/(b-a+1)
    nz = sorted(x for x in vb if x > 0)
    if not nz: return []
    med = nz[len(nz)//2]
    return [lo+(j+0.5)*bw for j in range(n) if vb[j] > med and vb[j] == max(vb[max(0, j-2):j+3])]

def ladder_candidates(name, price, atr1h, h1, h4, d1, lv):
    """(price, source, tf) candidates from real structure, then fib / round fillers. Real candle data only."""
    cand = []
    for tf, key in (("1h", "h1"), ("4h", "h4")):             # existing pivot levels
        for side in ("support", "resistance"):
            for p in ((lv.get(key) or {}).get(side) or []): cand.append((p, "pivot", tf))
    for tf, c, k in (("1h", h1, 3), ("4h", h4, 3)):          # full-history pivots on the fetched 1h/4h series
        if c:
            lo_, hi_ = _swings(c, k); cand += [(p, "pivot", tf) for p in lo_ + hi_]
    for tf, c, k in (("4h", h4, 6), ("1d", d1, 3)):          # longer-history swing highs/lows
        if c:
            lo_, hi_ = _swings(c, k); cand += [(p, "swing", tf) for p in lo_ + hi_]
    lo, hi = price*0.80, price*1.20
    for src, c in (("1h", h1), ("4h", h4)):
        if c: cand += [(p, "volume_node", src) for p in volume_nodes(c, price, atr1h, lo, hi)]
    sw = (h4 or [])[-540:] or (d1 or [])[-90:]                # major swing: ~90 days of 4h
    if sw:
        H, L = max(x["h"] for x in sw), min(x["l"] for x in sw); R = H-L
        for f in (0.236, 0.382, 0.5, 0.618, 0.786):
            cand += [(H-f*R, "fib", "4h"), (L+f*R, "fib", "4h")]
        for f in (1.272, 1.618, 2.0):
            cand += [(H-f*R, "fib", "4h"), (L+f*R, "fib", "4h")]
    for x, _ in _round_candidates(name, price, lo, hi): cand.append((x, "round", "round"))
    return [(round(p, 2), s, tf) for p, s, tf in cand if p and p > 0]

def build_ladder(side, price, atr1h, cand, h1):
    """Chain rungs nearest-first, every gap in LADDER_GAP x ATR. In each window take the best-ranked source
    (pivot > swing > volume_node > fib > round), tie -> closest to the window middle; empty window -> interpolated."""
    s = -1 if side == "support" else 1
    g0, g1 = LADDER_GAP[0]*atr1h, LADDER_GAP[1]*atr1h; mid = (g0+g1)/2
    ref, out = price, []
    w1h = (h1 or [])[-240:]; tol = 0.1*atr1h
    while len(out) < LADDER_MAX_RUNGS and (len(out) < LADDER_MIN_RUNGS or abs(ref-price)/price*100 < LADDER_REACH_PCT):
        win = [c for c in cand if g0 - 1e-9 <= s*(c[0]-ref) <= g1 + 1e-9]
        if win:
            p, src, tf = min(win, key=lambda c: (SRC_RANK[c[1]], abs(s*(c[0]-ref)-mid)))
            also = sorted({c[1] for c in win if abs(c[0]-p) <= 0.1*atr1h and c[1] != src}, key=SRC_RANK.get)
        else:
            p, src, tf, also = round(ref + s*mid, 2), "interpolated", "1h", []
        if p <= 0: break
        t = sum(1 for x in w1h if x["l"]-tol <= p <= x["h"]+tol)
        it = dict(price=p, tf=tf, source=src)
        if also: it["confluence"] = also
        # touches: real wick touches (last 240 1h bars) for structure; filled rungs = 1 so touch-filtered TP caps skip them
        it["touches"] = max(1, t) if src in REAL_SOURCES else 1
        d = abs(price-p); it["distance_usd"] = round(d, 2); it["distance_atr1h"] = round(d/atr1h, 2)
        out.append(it); ref = p
    return out

def ladder_levels(A, name, price, a1, h1, h4, lv, out_root):
    """levels.supports/resistances = add ladder (>=14 rungs/side, gaps 0.75-1.0x 1h ATR). The bot reads
    levels.h1/h4.support/resistance (float lists, concatenated), so those carry the same rungs (h4 = rungs from
    4h/daily structure, h1 = the rest) with aligned touch counts; the original pivot lists move to
    levels.h1/h4.pivot_support/pivot_resistance. Ladder can't be built -> previous file's levels are kept."""
    A1 = a1 and a1["atr14_usd"]
    try:
        if not (price and A1 and h1 and h4): raise RuntimeError("price/1h ATR/1h/4h candles unavailable")
        d1 = None
        try: d1 = A.kraken(1440)
        except Exception as e: A.notes.append(f"ladder: daily candles failed ({e}); daily swings skipped")
        cand = ladder_candidates(name, price, A1, h1, h4, d1, lv)
        sup = build_ladder("support", price, A1, [c for c in cand if c[0] < price], h1)
        res = build_ladder("resistance", price, A1, [c for c in cand if c[0] > price], h1)
        if len(sup) < LADDER_MIN_RUNGS or len(res) < LADDER_MIN_RUNGS: raise RuntimeError("ladder too short")
    except Exception as e:
        prev = None
        try:
            odir = os.path.join(out_root, A.c["subdir"]) if A.c["subdir"] else out_root
            prev = (json.load(open(os.path.join(odir, "latest.json"))).get("levels") or None)
        except Exception: pass
        if prev and prev.get("supports") and prev.get("resistances"):
            A.notes.append(f"ladder: {e} -> previous levels (h1/h4/supports/resistances) kept from the last run")
            for k in ("h1", "h4", "supports", "resistances", "method", "ladder"):
                if k in prev: lv[k] = prev[k]
            lv["kept_from_previous_run"] = True
        else:
            A.notes.append(f"ladder: {e}; no previous levels -> pivot-only merged levels")
            lv["supports"], lv["resistances"] = merged_levels(lv, price, A1)
        return lv
    for key, tfs in (("h1", ("1h", "round")), ("h4", ("4h", "1d"))):
        d = lv.get(key) or {}
        d["pivot_support"], d["pivot_resistance"] = d.get("support", []), d.get("resistance", [])
        d["pivot_support_touches"], d["pivot_resistance_touches"] = d.get("support_touches", []), d.get("resistance_touches", [])
        pick = lambda items: [x for x in items if (x["tf"] in ("4h", "1d")) == (key == "h4")]
        d["support"] = [x["price"] for x in pick(sup)]; d["support_touches"] = [x["touches"] for x in pick(sup)]
        d["resistance"] = [x["price"] for x in pick(res)]; d["resistance_touches"] = [x["touches"] for x in pick(res)]
        lv[key] = d
    lv["supports"], lv["resistances"] = sup, res
    gaps = lambda it: [round(abs(a-b)/A1, 3) for a, b in zip([price]+[x["price"] for x in it], [x["price"] for x in it])]
    gs, gr = gaps(sup), gaps(res)
    lv["ladder"] = dict(atr1h=A1, gap_atr_target=list(LADDER_GAP), min_rungs=LADDER_MIN_RUNGS, reach_pct_target=LADDER_REACH_PCT,
                        supports_gap_atr=dict(min=min(gs), avg=round(sum(gs)/len(gs), 3), max=max(gs)),
                        resistances_gap_atr=dict(min=min(gr), avg=round(sum(gr)/len(gr), 3), max=max(gr)),
                        supports_real=sum(x["source"] in REAL_SOURCES for x in sup),
                        resistances_real=sum(x["source"] in REAL_SOURCES for x in res))
    lv["method"] = ("add ladder: rungs chained nearest-first from price, each gap 0.75-1.0x 1h ATR; per window the best "
                    "real structure wins (pivot: 1h/4h pivots k=3; swing: 4h k=6 / daily k=3 swing highs-lows; volume_node: "
                    "1h/4h volume-profile HVNs), else fib (~90d 4h major swing retracements/extensions), else round number, "
                    "else interpolated at 0.875x ATR. Extended past 14 rungs until >=10% from price (max 30). "
                    "h1/h4.support/resistance carry the same rungs for the bot (h4 = 4h/1d-sourced); original pivots in "
                    "h1/h4.pivot_*")
    return lv

# ---------------------------------------------------------------- data
class Asset:
    def __init__(self, name, cfg):
        self.name, self.c, self.notes, self.sources = name, cfg, [], []

    def kraken(self, iv):
        u = f"https://api.kraken.com/0/public/OHLC?pair={self.c['kraken']}&interval={iv}"
        j = get(u)
        if j.get("error"): raise RuntimeError(j["error"])
        rows = [v for k, v in j["result"].items() if k != "last"][0][:-1]  # drop in-progress candle
        self.sources.append(u)
        return [dict(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[6])) for r in rows]

    def coinbase(self, g):
        u = f"https://api.exchange.coinbase.com/products/{self.c['coinbase']}/candles?granularity={g}"
        rows = sorted(get(u))[:-1]; self.sources.append(u)
        return [dict(t=r[0], l=r[1], h=r[2], o=r[3], c=r[4], v=r[5]) for r in rows]

    def candles(self, iv):
        try: return self.kraken(iv)
        except Exception as e: self.notes.append(f"Kraken {iv}m failed: {e}")
        # Coinbase granularity fallbacks (seconds): 15m=900, 1h=3600
        cb = {15: 900, 60: 3600, 1440: 86400}.get(iv)
        if cb is not None:
            try: return self.coinbase(cb)
            except Exception as e: self.notes.append(f"Coinbase {iv}m failed: {e}")
        return None

    def price(self):
        try:
            u = f"https://api.exchange.coinbase.com/products/{self.c['coinbase']}/ticker"
            p = float(get(u)["price"]); self.sources.append(u); return p
        except Exception as e:
            self.notes.append(f"Coinbase ticker failed: {e}")
        try:
            u = f"https://api.kraken.com/0/public/Ticker?pair={self.c['kraken']}"
            p = float([v for v in get(u)["result"].values()][0]["c"][0]); self.sources.append(u); return p
        except Exception as e: self.notes.append(f"Kraken ticker failed: {e}")
        return None

    def jupiter(self):
        jup = dict(long_hourly_pct=None, short_hourly_pct=None, utilization_long_pct=None, utilization_short_pct=None,
                   open_fee_pct=None)
        try:
            u = f"https://perps-api.jup.ag/v1/pool-info?mint={self.c['mint']}"
            j = get(u); self.sources.append(u)
            jup = dict(long_hourly_pct=float(j["longBorrowRatePercent"]), short_hourly_pct=float(j["shortBorrowRatePercent"]),
                       utilization_long_pct=float(j["longUtilizationPercent"]), utilization_short_pct=float(j["shortUtilizationPercent"]),
                       open_fee_pct=float(j["openFeePercent"]))
        except Exception as e: self.notes.append(f"Jupiter pool-info failed: {e}")
        return jup

    def binance_4h(self):
        """Closed Binance 4h klines (the SOL bot's data source) as dicts with open-time ms."""
        u = f"https://data-api.binance.vision/api/v3/klines?symbol={self.c['binance']}&interval=4h&limit=1000"
        rows = get(u); self.sources.append(u)
        now_ms = datetime.datetime.now(datetime.timezone.utc).timestamp()*1000
        return [dict(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4])) for r in rows
                if int(r[6]) < now_ms]  # r[6] = close time; keep closed bars only

def ema200_block(A, h4_kraken, price, atr4):
    """4h EMA200 exactly as live/bot.py computes it: Binance <SYM>USDT 15m klines, last 5000 bars, resampled to 4h
    (UTC buckets), ewm(span=200, adjust=False) seeded at the first bucket, trend_up = last CLOSED 4h close > EMA.
    Binance 4h bars == those buckets, so we seed at the 4h bar holding the oldest of the bot's 5000 15m bars."""
    bars, src = None, None
    try: bars, src = A.binance_4h(), f"Binance {A.c['binance']} 4h"
    except Exception as e: A.notes.append(f"Binance 4h klines failed ({e}); EMA200 from Kraken 4h (USD) instead")
    if not bars and h4_kraken: bars, src = h4_kraken, f"Kraken {A.c['kraken']} 4h (fallback)"
    if not bars or len(bars) < EMA_LEN + 2:
        A.notes.append("4h EMA200 unavailable: not enough 4h bars"); return None
    now = datetime.datetime.now(datetime.timezone.utc)
    step = BOT_SIGNAL_TF_MIN*60
    oldest = (int(now.timestamp())//step)*step - (BOT_KLINE_PAGES*1000-1)*step   # bot's oldest 15m bar open (s)
    seed_t = (oldest//14400)*14400*1000
    i0 = next((i for i, b in enumerate(bars) if b["t"] >= seed_t), None)
    botwin = bars[i0:] if (i0 is not None and src.startswith("Binance") and bars[0]["t"] <= seed_t) else None
    cl_full = [b["c"] for b in bars]
    e_full = ema(cl_full, EMA_LEN)
    if botwin and len(botwin) > EMA_LEN:
        cl = [b["c"] for b in botwin]; e = ema(cl, EMA_LEN); method = "bot-equivalent window (5000x15m -> 4h, adjust=False)"
    else:
        cl, e, botwin = cl_full, e_full, bars; method = f"all {len(bars)} bars (bot window not reconstructable)"
    E, C = e[-1], cl[-1]
    sides = ["above" if c > m else "below" for c, m in zip(cl[-12:], e[-12:])]
    run = 1
    while run < len(sides) and sides[-1-run] == sides[-1]: run += 1
    last_t = botwin[-1]["t"]//1000
    tz = EV.MT
    last_close_mt = datetime.datetime.fromtimestamp(last_t+14400, datetime.timezone.utc).astimezone(tz)
    next_close_mt = last_close_mt + datetime.timedelta(hours=4)
    pos = sides[-1]
    return dict(value=round(E, 3), source=src, method=method,
                value_full_history=round(e_full[-1], 3), full_history_bars=len(bars),
                last_closed_4h_close=C, last_closed_4h_close_time_mt=EV.iso(last_close_mt),
                next_4h_close_time_mt=EV.iso(next_close_mt),
                price=price, price_vs_ema200=("above" if price and price > E else "below") if price else None,
                last_close_vs_ema200=pos, consecutive_4h_closes_same_side=run, recent_4h_closes_vs_ema200=sides[-6:],
                distance_usd=round(price-E, 3) if price else None,
                distance_pct=round((price-E)/E*100, 2) if price else None,
                distance_atr4h=round((price-E)/atr4, 2) if (price and atr4) else None,
                ema_slope_per_4h_bar=round(e[-1]-e[-2], 4),
                bot_trend_up=C > E, alpha=round(2/(EMA_LEN+1), 5))

def side_flip_block(eb, cfg_sides):
    if not eb: return None
    E, pos, run = eb["value"], eb["last_close_vs_ema200"], eb["consecutive_4h_closes_same_side"]
    bot_side = "long" if pos == "above" else "short"
    other = "short" if bot_side == "long" else "long"
    word = "below" if bot_side == "long" else "above"
    nxt = eb["next_4h_close_time_mt"][11:16]
    if run >= 3:
        status, flip = f"stable: {run} consecutive 4h closes {pos} the EMA200", False
    elif run == 2:
        status, flip = f"flip just confirmed: last two 4h closes {pos} the EMA200 -> allowed side {bot_side}", True
    else:
        status, flip = (f"unconfirmed: only the latest 4h close is {pos} the EMA200; the bot has already switched to "
                        f"{bot_side}-only mechanically, but we would not treat it as a flip until the {nxt} MT close "
                        f"also closes {pos} ${E:,.2f}"), False
    return dict(
        current_allowed_side=bot_side if cfg_sides in (None, "both") else f"{bot_side} (config sides={cfg_sides})",
        would_flip_now=flip, status=status,
        flip_to=other, trigger_price=round(E, 2), trigger_direction=f"4h close {word} EMA200",
        confirmation_required=(f"two consecutive 4h closes {word} the 4h EMA200 at ${E:,.2f} (Binance 4h bars; next "
                               f"close {nxt} MT, then 4h later). The EMA moves only {eb['alpha']*100:.2f}% of the "
                               f"close-EMA gap per bar, so both closes must be {word} ~${E:,.2f}"),
        distance_to_trigger_usd=eb["distance_usd"], distance_to_trigger_atr4h=eb["distance_atr4h"],
        bot_rule=("the bot itself re-evaluates trend_up = last closed 4h close > EMA200 on every bar with NO "
                  "confirmation: one 4h close beyond the EMA flips which side NEW entries may take; open positions are "
                  "not closed (stops.trend_flip_exit=false)"))

# ---------------------------------------------------------------- bot config
def deep_merge(a, b):
    out = dict(a)
    for k, v in b.items(): out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

def find_config(name, A):
    p = A.c.get("config_path")
    if p:
        if os.path.exists(p): return p
        A.notes.append(f"bot config {p} not found"); return None
    seen, hits = set(), []
    for g in CONFIG_GLOBS:
        for f in sorted(glob.glob(g)):
            if f in seen or ".bak" in f: continue
            seen.add(f)
            try: d = tomllib.load(open(f, "rb"))
            except Exception: continue
            sym = str((d.get("market") or {}).get("symbol") or "").upper()
            asset = str((d.get("bot") or {}).get("asset") or "").upper()
            base = os.path.basename(f).lower()
            if asset == name or sym.startswith(name) or name.lower() in base.replace(".toml", "").split("_") \
                    or base.startswith(name.lower()):
                # rank: [bot].asset match, then live_* files, then the rest
                hits.append((0 if asset == name else 1, 0 if base.startswith("live") else 1, f))
    if len(hits) > 1: A.notes.append(f"several {name} bot configs match, using {sorted(hits)[0][2]}: " + ", ".join(h[2] for h in hits))
    return sorted(hits)[0][2] if hits else None

def bot_config(name, A, price=None, atr1h=None):
    p = find_config(name, A)
    if not p:
        A.notes.append(f"no {name} bot config found (searched {', '.join(CONFIG_GLOBS)}): current_bot_config null")
        return None, None
    try:
        dflt = os.path.join(os.path.dirname(p), "default.toml")
        base = tomllib.load(open(dflt, "rb")) if os.path.exists(dflt) and os.path.abspath(dflt) != os.path.abspath(p) else {}
        raw = tomllib.load(open(p, "rb")); d = deep_merge(base, raw)
    except Exception as e:
        A.notes.append(f"bot config {p} unreadable: {e}"); return None, None
    tp, dca, tr_, live, sig, sc, bot = (d.get(k) or {} for k in ("tp", "dca", "trend", "live", "signal", "scale", "bot"))
    lv = sorted(tp.get("levels") or [], key=lambda x: x.get("dist", 0))
    m = (d.get("market") or {})
    mode = tp.get("mode", "price_abs")
    def tp_usd(i):
        if len(lv) <= i: return None, None
        dist = lv[i]["dist"]
        if mode == "price_abs": return dist, round(dist/price*100, 4) if price else None
        if mode == "price_pct": return (round(dist*price, 4) if price else None), round(dist*100, 4)
        return None, None  # margin_pct: depends on leverage, no fixed $ distance
    (tp1u, tp1p), (tp2u, tp2p) = tp_usd(0), tp_usd(1)
    if mode == "margin_pct": A.notes.append(f"{os.path.basename(p)} tp.mode=margin_pct: TP $ distances depend on leverage -> null")
    fb, fb_basis = dca.get("trigger_dist"), "dca.trigger_dist (USD)"
    if sc.get("enabled") and sc.get("trigger_dist_atr") is not None:
        fb = round(sc["trigger_dist_atr"]*atr1h, 4) if atr1h else None
        fb_basis = f"[scale] trigger_dist_atr {sc['trigger_dist_atr']} x 1h ATR (bot uses its own Binance 1h ATR; ours shown)"
    cfg = dict(
        source_file=p, base_layer=dflt if base else None,
        file_mtime_mt=EV.iso(datetime.datetime.fromtimestamp(os.path.getmtime(p), EV.MT)),
        name=d.get("name"), market_symbol=m.get("symbol"), sides=d.get("sides", "both"),
        trend_filter=dict(tf=tr_.get("tf"), ma_type=tr_.get("ma_type"), length=tr_.get("length")),
        signal=dict(tf=sig.get("tf"), oversold=sig.get("oversold"), overbought=sig.get("overbought")),
        bot=dict(asset=bot.get("asset"), share=bot.get("share"), state_dir=bot.get("state_dir")) if bot else None,
        tp_mode=mode, tp_anchor=tp.get("anchor"),
        tp1_usd=tp1u, tp1_pct=tp1p, tp1_close_pct=round(lv[0]["close_pct"]*100) if lv else None,
        tp2_usd=tp2u, tp2_pct=tp2p,
        tp_usd_basis=("config $ distances" if mode == "price_abs" else
                      "config % of avg entry x current price (actual $ = % x the position's avg entry)" if mode == "price_pct" else None),
        post_first_tp_close_timer_minutes=tp.get("close_remainder_after_min"), smart_tp2=tp.get("smart_tp2"),
        dca_mode=dca.get("mode"), dca_levels_path=dca.get("levels_path"),
        dca_fallback_spacing_usd=fb, dca_fallback_spacing_basis=fb_basis, dca_min_gap_atr=dca.get("min_gap_atr"),
        dca_max_gap_atr=dca.get("max_gap_atr"), dca_reference=dca.get("reference"),
        leverage=dict(first_entry=dca.get("lev_start"), step_per_add=dca.get("lev_step"), cap=dca.get("lev_cap"),
                      max_leverage=live.get("max_leverage")),
        trend_flip_exit=(d.get("stops") or {}).get("trend_flip_exit"), dry_run=live.get("dry_run"))
    return cfg, d

def compare(cfg, rec, a1, eb, price, step):
    if not cfg or not rec: return None
    A1 = a1["atr14_usd"] if a1 else None
    def tp(k):
        cur, new = cfg.get(k), rec.get(k)
        if cur is None or new is None: return dict(current=cur, recommended=new)
        return dict(current=cur, recommended=new, diff_usd=round(new-cur, 4),
                    current_x_atr1h=round(cur/A1, 2) if A1 else None, recommended_x_atr1h=round(new/A1, 2) if A1 else None,
                    verdict="match" if abs(new-cur) < step/2 else ("tighten" if new < cur else "widen"))
    out = dict(tp1_usd=tp("tp1_usd"), tp2_usd=tp("tp2_usd"),
               tp1_close_pct=dict(current=cfg.get("tp1_close_pct"), recommended=rec.get("tp1_close_pct")),
               post_first_tp_close_timer_minutes=dict(current=cfg.get("post_first_tp_close_timer_minutes"),
                                                      recommended=rec.get("post_first_tp_close_timer_minutes")))
    fb = cfg.get("dca_fallback_spacing_usd")
    out["dca"] = dict(mode=cfg.get("dca_mode"), current_fallback_spacing_usd=fb,
                      recommended_spacing_usd=rec.get("dca_spacing_usd"),
                      min_gap_usd=round(cfg["dca_min_gap_atr"]*A1, 3) if (A1 and cfg.get("dca_min_gap_atr")) else None,
                      max_gap_usd=round(cfg["dca_max_gap_atr"]*A1, 3) if (A1 and cfg.get("dca_max_gap_atr")) else None,
                      note=("levels mode: adds go to analyst supports (longs) / resistances (shorts) between min_gap and "
                            "max_gap x 1h ATR from the last fill; trigger_dist is only the fallback when no level fits "
                            "or latest.json is stale") if cfg.get("dca_mode") == "levels" else None)
    lr = rec.get("leverage_range") or ""
    try: lo, hi = (float(x) for x in lr.rstrip("x").split("-"))
    except Exception: lo = hi = None
    fe = (cfg.get("leverage") or {}).get("first_entry")
    out["leverage"] = dict(current_first_entry=fe, recommended_range=lr,
                           verdict=None if (fe is None or lo is None) else ("within" if lo <= fe <= hi else ("above" if fe > hi else "below")))
    bot_side = ("long" if eb["bot_trend_up"] else "short") if eb else None
    out["side"] = dict(bot_allowed_side=bot_side, recommended_preferred_side=rec.get("preferred_side"),
                       conflict=bool(bot_side and rec.get("preferred_side") in ("long", "short") and rec["preferred_side"] != bot_side))
    if out["side"]["conflict"]:
        out["side"]["note"] = (f"4h EMA200 filter allows only {bot_side} entries but 1h/4h regimes favour "
                               f"{rec['preferred_side']}: counter-regime {bot_side}s -> keep TPs tight, leverage at the low end")
    return out

# ---------------------------------------------------------------- events
def load_events(A, now):
    p = os.path.join(D, "events.json")
    if not os.path.exists(p): A.notes.append("events.json missing: events empty, no event pause"); return [], False, []
    try: data = EV.load(p)
    except Exception as e: A.notes.append(f"events.json unreadable: {e}"); return [], False, []
    if data.get("scanned_at"):
        age_h = (now - EV.parse(data["scanned_at"])).total_seconds() / 3600
        if age_h > 26: A.notes.append(f"events.json last full scan {age_h:.0f}h ago (stale)")
    out, nm = [], now.astimezone(EV.MT)
    for e in EV.relevant(data, hours=24, breaking_hours=24, now=nm, asset=A.name):
        try:
            w = EV.pause_window(e)
            tm = EV.parse(e.get("time_mt"))
            out.append(dict(id=e.get("id"), title=e.get("title"), category=e.get("category"), status=e.get("status"),
                            assets=e.get("assets"), time_mt=e.get("time_mt"), published_mt=e.get("published_mt"),
                            minutes_until=round((tm - nm).total_seconds() / 60) if tm else None,
                            impact=e.get("impact"), recommendation=e.get("recommendation"), source_url=e.get("source_url"),
                            pause_window_mt=[EV.iso(w[0]), EV.iso(w[1])] if w else None,
                            pause_active=bool(w and w[0] <= nm <= w[1]), notified=e.get("notified", False)))
        except Exception as ex: A.notes.append(f"event skipped ({e.get('title')}): {ex}")
    out.sort(key=lambda x: x["time_mt"] or x["published_mt"] or "")
    act = [x for x in out if x["pause_active"]]
    return out, bool(act), act

# ---------------------------------------------------------------- one asset
def build(name, now, out_root):
    A = Asset(name, ASSETS[name])
    events, event_pause, pause_evs = load_events(A, now)
    h1, h4 = A.candles(60), A.candles(240)
    if h4 is None and h1:  # aggregate 1h -> 4h
        A.notes.append("4h aggregated from 1h candles")
        h4 = [dict(t=g[0]["t"], o=g[0]["o"], h=max(x["h"] for x in g), l=min(x["l"] for x in g), c=g[-1]["c"])
              for g in (h1[i:i+4] for i in range(len(h1) % 4, len(h1), 4))]
    price = A.price()
    a1, a4 = analyze(h1), analyze(h4)
    if price is None and a1: price = a1["last_close"]; A.notes.append("price = last closed 1h candle (tickers failed)")
    jup = A.jupiter()
    step = nice_step(price) if price else None
    eb = ema200_block(A, h4, price, a4 and a4["atr14_usd"])
    if a4 is not None: a4["ema200"] = eb and eb["value"]
    cfg, _ = bot_config(name, A, price, a1 and a1["atr14_usd"])

    lv = dict(h1=levels(h1, price) if h1 and price else None, h4=levels(h4, price) if h4 and price else None)
    lv = ladder_levels(A, name, price, a1, h1, h4, lv, out_root)
    # Fine-grained near-price S/R (does not alter supports/resistances). 15m failure -> empty fine + note.
    c15 = None
    try:
        c15 = A.candles(15)
    except Exception as e:
        A.notes.append(f"fine levels: 15m fetch raised: {e}")
        c15 = None
    lv["fine"] = fine_levels(name, price, a1 and a1["atr14_usd"], c15, h1, A.notes)

    rec = None
    if a1 and a4 and price:
        A1, A4, r1, r4 = a1["atr14_usd"], a4["atr14_usd"], a1["regime"], a4["regime"]
        why = []
        if r4 == "uptrend" and r1 != "downtrend": side = "long"
        elif r4 == "downtrend" and r1 != "uptrend": side = "short"
        elif r4 == "range" and r1 == "range": side = "both"
        elif r4 == "range": side = "long" if r1 == "uptrend" else "short"
        else: side = "none"
        why.append(f"4h={r4} (ADX {a4['adx14']}), 1h={r1} (ADX {a1['adx14']}) -> {side}")
        if eb:
            why.append(f"4h EMA200 ${eb['value']:,.2f}: last 4h close {eb['last_close_vs_ema200']} "
                       f"({eb['consecutive_4h_closes_same_side']} in a row) -> bot direction filter allows "
                       f"{'longs' if eb['bot_trend_up'] else 'shorts'} only")
        # TP sized to fraction of 1h ATR: TP1 ~0.6 ATR1h, TP2 ~0.9 ATR1h, rounded to the asset's $ step;
        # floors scale with the asset: TP1 >= 4x the open fee x price (2x round-trip fees), TP2 >= TP1 + 0.25x 1h ATR
        fee = (jup.get("open_fee_pct") or 0.06)/100
        rnd = lambda x: round(round(x/step)*step, 6)
        up = lambda x: round(math.ceil(x/step - 1e-9)*step, 6)
        tp1_floor, gap = up(4*fee*price), up(0.25*A1)
        tp1 = max(rnd(0.6*A1), tp1_floor); tp2 = max(rnd(0.9*A1), round(tp1+gap, 6))
        if r1 == "range": tp1 = max(rnd(0.5*A1), tp1_floor); tp2 = max(rnd(0.8*A1), round(tp1_floor+gap, 6), round(tp1+step, 6))
        dca = rnd(max(A1, 0.5*A4))
        why.append(f"1h ATR ${A1:,} -> TP1 ${tp1:,} (~{tp1/A1:.1f}xATR, floor ${tp1_floor:,}), TP2 ${tp2:,}; "
                   f"DCA ${dca:,} = max(1h ATR, 0.5x4h ATR ${A4:,}) (rounded to ${step:g})")
        if cfg and cfg.get("tp1_usd") is not None and cfg.get("tp2_usd") is not None:
            why.append(f"current bot config ({os.path.basename(cfg['source_file'])}): TP1 ${cfg['tp1_usd']} = "
                       f"{cfg['tp1_usd']/A1:.2f}x and TP2 ${cfg['tp2_usd']} = {cfg['tp2_usd']/A1:.2f}x 1h ATR")
        if cfg and cfg.get("dca_mode") == "levels":
            why.append("bot DCA is level-based: adds at the supports/resistances above (nearest first) sized off 1h ATR; "
                       "dca_spacing_usd is the fallback spacing")
        atrpct = A1/price*100
        lev = "3-5x" if atrpct > 1.0 else ("5-8x" if atrpct > 0.6 else "8-10x")
        why.append(f"1h ATR = {atrpct:.2f}% of price -> leverage {lev} (keep DCA ladder well inside liquidation distance)")
        timer = int(min(240, max(30, round(60*tp2/A1/5)*5)))
        why.append(f"close timer ~ time for price to travel TP2 at 1h ATR pace: {timer} min")
        pause = side == "none" or a1["rsi14"] > 78 or a1["rsi14"] < 22 or atrpct > 2.0
        if pause: why.append("pause: conflicting regimes or extreme RSI/volatility")
        if event_pause:
            pause = True
            why.append("pause: high-impact event window active: " + "; ".join(
                f"{x['title']} ({x['pause_window_mt'][0][11:16]}-{x['pause_window_mt'][1][11:16]} MT)" for x in pause_evs))
        rec = dict(preferred_side=side, leverage_range=lev, tp1_usd=tp1, tp1_close_pct=50, tp2_usd=tp2,
                   dca_spacing_usd=dca, post_first_tp_close_timer_minutes=timer, pause_new_entries=pause,
                   usd_rounding_step=step, bot_allowed_side=("long" if eb["bot_trend_up"] else "short") if eb else None,
                   reasoning=why)
    else: A.notes.append("recommendation null: insufficient data")
    cmp_ = compare(cfg, rec, a1, eb, price, step or 0.05)
    sflip = side_flip_block(eb, cfg and cfg.get("sides"))

    odir = os.path.join(out_root, A.c["subdir"]) if A.c["subdir"] else out_root
    os.makedirs(odir, exist_ok=True)
    hp = os.path.join(odir, "history.jsonl")
    prev = None
    if os.path.exists(hp):
        try:
            ls = [l for l in open(hp) if l.strip()]
            if ls: prev = json.loads(ls[-1])
        except Exception as e: A.notes.append(f"history.jsonl unreadable: {e}")
    flag = None
    if prev:
        p1 = (prev.get("trend") or {}).get("1h") or {}; p4 = (prev.get("trend") or {}).get("4h") or {}
        trend_changed = bool((a1 and p1.get("regime") != a1["regime"]) or (a4 and p4.get("regime") != a4["regime"]))
        pe = (prev.get("ema200_4h") or {}).get("last_close_vs_ema200")
        ema_changed = bool(eb and pe and pe != eb["last_close_vs_ema200"])
        pr, cr = prev.get("recommendation") or {}, rec or {}
        rec_changes = {k: dict(prev=pr.get(k), now=cr.get(k))
                       for k in ("pause_new_entries", "tp1_usd", "tp2_usd", "dca_spacing_usd") if pr.get(k) != cr.get(k)}
        flag = dict(changed=trend_changed or ema_changed or bool(rec_changes), trend_changed=trend_changed,
                    ema200_side_changed=ema_changed,
                    changed_fields=(["trend"] if trend_changed else []) + (["ema200_side"] if ema_changed else []) + list(rec_changes),
                    rec_changes=rec_changes,
                    prev_timestamp=prev.get("timestamp"), prev_1h=p1.get("regime"), prev_4h=p4.get("regime"),
                    now_1h=a1 and a1["regime"], now_4h=a4 and a4["regime"],
                    prev_ema200_side=pe, now_ema200_side=eb and eb["last_close_vs_ema200"])
    else: A.notes.append("regime_change_flag null: no prior history for this asset")

    out = dict(timestamp=now.isoformat(timespec="seconds"), symbol=A.c["symbol"], asset=name, price=price,
               trend={"4h": a4, "1h": a1},
               volatility=dict(atr14_1h_usd_per_hour=a1 and a1["atr14_usd"], atr14_4h_usd=a4 and a4["atr14_usd"],
                               atr14_1h_pct=round(a1["atr14_usd"]/price*100, 3) if (a1 and price) else None),
               momentum=dict(rsi14_1h=a1 and a1["rsi14"], rsi14_4h=a4 and a4["rsi14"],
                             macd_hist_1h=a1 and a1["macd_hist"], macd_hist_4h=a4 and a4["macd_hist"]),
               levels=lv,
               ema200_4h=eb, side_flip=sflip)
    out[A.c["borrow_key"]] = jup
    out["jupiter_borrow_rate"] = dict(jup, custody_mint=A.c["mint"],
                                      note="long = this asset's custody; short = USDC custody (shared by all markets)")
    out.update(event_pause_active=event_pause,
               event_pause_windows=[dict(title=x["title"], start_mt=x["pause_window_mt"][0], end_mt=x["pause_window_mt"][1],
                                         source_url=x["source_url"]) for x in pause_evs],
               events=events, recommendation=rec, current_bot_config=cfg, recommendation_vs_bot_config=cmp_,
               regime_change_flag=flag, data_sources=A.sources, notes=A.notes)
    return out, odir, hp

def write_atomic(path, obj):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".latest.", suffix=".json")
    with os.fdopen(fd, "w") as f: json.dump(obj, f, indent=2)
    os.chmod(tmp, 0o644); os.replace(tmp, path)  # the SOL bot reads latest.json live: never expose a partial file

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="SOL,ETH,BTC"); ap.add_argument("--out-root", default=D)
    ap.add_argument("--no-history", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.datetime.now().astimezone()
    results, failed = {}, []
    for name in [x.strip().upper() for x in a.assets.split(",") if x.strip()]:
        try:
            out, odir, hp = build(name, now, a.out_root)
            write_atomic(os.path.join(odir, "latest.json"), out)
            if not a.no_history:
                with open(hp, "a") as f: f.write(json.dumps(out) + "\n")
            results[name] = out
            r = out.get("recommendation") or {}; sf = out.get("side_flip") or {}
            print(f"[{name}] price={out['price']} 4h={(out['trend']['4h'] or {}).get('regime')} "
                  f"1h={(out['trend']['1h'] or {}).get('regime')} side={r.get('preferred_side')} "
                  f"tp1={r.get('tp1_usd')} tp2={r.get('tp2_usd')} dca={r.get('dca_spacing_usd')} "
                  f"ema200={(out.get('ema200_4h') or {}).get('value')} allowed={sf.get('current_allowed_side')} "
                  f"notes={len(out['notes'])}", file=sys.stderr)
        except Exception:
            failed.append(name)
            print(f"[{name}] FAILED (previous latest.json left in place):\n{traceback.format_exc()}", file=sys.stderr)
    if "SOL" in results: print(json.dumps(results["SOL"], indent=2))  # stdout stays the SOL outlook, as before
    if failed and not results: sys.exit(1)

if __name__ == "__main__": main(sys.argv[1:])
