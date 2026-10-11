#!/usr/bin/env python3
"""newsgate.py - FREE (stdlib-only, no LLM) gate for the hourly news/analysis job.
First line: QUIET  or  TRIP: <reason; reason; ...>.  Exit 0 = QUIET, 10 = TRIP.
State: newsgate_state.json (last check, prices, risk levels, sides, seen headline hashes).
Env: NEWSGATE_STATE / NEWSGATE_DIR override paths (for testing); NEWSGATE_NO_FEEDS=1 skips feeds.
"""
import json, os, re, sys, time, hashlib, html
import urllib.request, concurrent.futures as cf
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ===================== CONSTANTS =====================
BASE = os.environ.get("NEWSGATE_DIR", "/workspace/market-analysis")
STATE = os.environ.get("NEWSGATE_STATE", os.path.join(BASE, "newsgate_state.json"))
ASSET_FILES = {"SOL": "latest.json", "ETH": "eth/latest.json", "BTC": "btc/latest.json"}
EVENTS_FILE = "events.json"

FEEDS = {  # verified 2026-10-10 (Reuters feeds.reuters.com dead, AP RSS 403 -> dropped)
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock": "https://www.theblock.co/rss.xml",
    "decrypt": "https://decrypt.co/feed",
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "gnews_macro": "https://news.google.com/rss/search?q=Fed+OR+FOMC+OR+CPI+OR+Hormuz+OR+missile+OR+sanctions+when:1d&hl=en-US&gl=US&ceid=US:en",
    "gnews_crypto": "https://news.google.com/rss/search?q=crypto+OR+bitcoin+OR+solana+OR+ethereum+when:1d&hl=en-US&gl=US&ceid=US:en",
}
FEED_TIMEOUT = 8          # seconds per feed (fetched in parallel)
HEADLINE_MAX_AGE_MIN = 90
SEEN_KEEP_HOURS = 48

# (label, regex) - case-insensitive, word-boundary anchored
KEYWORDS = [
    ("solana-incident", r"\bsolana\b.{0,80}\b(outage|halt(?:ed|s)?|exploit(?:ed)?|hack(?:ed)?)\b|\b(outage|halt(?:ed|s)?|exploit(?:ed)?|hack(?:ed)?)\b.{0,80}\bsolana\b"),
    ("exchange-incident", r"\b(exchange|Binance|Coinbase|Kraken|OKX|Bybit|Bitfinex|KuCoin|Gate\.io|HTX|Bitget|MEXC|Upbit|Hyperliquid|Jupiter|Drift)\b.{0,80}\b(hack(?:ed)?|exploit(?:ed)?|halt(?:ed|s)?|insolven(?:t|cy))\b|\b(hack(?:ed)?|exploit(?:ed)?|insolven(?:t|cy))\b.{0,80}\bexchange\b|\bwithdrawals?\s+(paused|halted|suspended|frozen)\b|\b(pauses?|halts?|suspends?|freezes?)\s+withdrawals?\b"),
    ("etf", r"\bETFs?\b.{0,60}\b(approv(?:al|ed|es)|reject(?:ion|ed|s)|denied|record\s+(?:inflows?|outflows?))\b|\b(approv(?:al|ed|es)|reject(?:ion|ed|s)|record\s+(?:inflows?|outflows?))\b.{0,60}\bETFs?\b"),
    ("fed-macro", r"\b(FOMC|rate\s+(?:hike|cut)s?|(?:hikes?|cuts?)\s+(?:interest\s+)?rates?|CPI(?!\s+(?:Property|Group|Inc|Ltd|Corp))|PPI|emergency\s+(?:rate|meeting|cut))\b|\bFed\b.{0,40}\b(hike|cut|emergency)\b"),
    ("geopolitics", r"\bHormuz\b|\b(declares?\s+war|war\s+(?:breaks\s+out|escalat\w*)|act\s+of\s+war)\b|\b(missiles?|airstrikes?|air\s+strikes?|drones?|rockets?)\b.{0,50}\b(strikes?|struck|attacks?|attacked|launch(?:ed|es)?|fired|hits?|intercept(?:ed)?)\b|\b(strikes?|attacks?)\s+on\s+(?:Iran|Israel|Saudi|oil|tanker|Taiwan)\w*|\b(new|sweeping|fresh|imposes?|announces?)\s+(?:\w+\s+){0,2}sanctions\s+on\s+(?:Russia|Iran|China)\b"),
    ("liquidation", r"\bliquidat\w*\b.{0,80}(\$\s?(?:[5-9]\d\d(?:\.\d+)?\s?(?:M|mn|million)|\d+(?:\.\d+)?\s?(?:B|bn|billion))\b|\bcascad\w*)|(\$\s?(?:[5-9]\d\d(?:\.\d+)?\s?(?:M|mn|million)|\d+(?:\.\d+)?\s?(?:B|bn|billion))\b|\bcascad\w*).{0,60}\bliquidat\w*"),
    ("depeg", r"\bde-?peg(?:ged|s|ging)?\b"),
    ("sec-action", r"\bSEC\b.{0,60}\b(su(?:es|ed|ing)|lawsuit|charges?|charged)\b"),
]
KW = [(l, re.compile(p, re.I | re.S)) for l, p in KEYWORDS]

# risk / price / vol / staleness / events
RISK_ORDER = {"green": 0, "amber": 1, "red": 2}
MOVE_ATR_MULT = 1.5       # |price - last| >= 1.5 x 1h ATR
MOVE_PCT = 2.0            # or >= 2%
VOL_SPIKE_RATIO = 1.8     # any risk.metrics.volatility_spike *_ratio >= 1.8
STALE_MIN = 45
EVENT_LOOKAHEAD_H = 2
# =====================================================

now = datetime.now(timezone.utc)


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def fetch(name, url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (newsgate)"})
    with urllib.request.urlopen(req, timeout=FEED_TIMEOUT) as r:
        data = r.read(3_000_000)
    root = ET.fromstring(data)
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        g = {c.tag.split("}")[-1]: (c.text or "") for c in it}
        title = html.unescape(g.get("title", "")).strip()
        summ = re.sub(r"<[^>]+>", " ", html.unescape(g.get("description") or g.get("summary") or ""))
        link = g.get("link") or ""
        if not link:
            for c in it:
                if c.tag.split("}")[-1] == "link":
                    link = c.get("href", "")
        ds = g.get("pubDate") or g.get("published") or g.get("updated") or ""
        pub = None
        try:
            pub = parsedate_to_datetime(ds)
        except Exception:
            try:
                pub = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            except Exception:
                pass
        if pub and pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        hid = hashlib.sha1((g.get("guid") or link or title).encode()).hexdigest()[:16]
        items.append({"id": hid, "title": title, "summary": summ[:600], "link": link, "pub": pub, "feed": name})
    return items


def main():
    trips, notes = [], []
    headline_hits, reacting = [], False
    st = load(STATE, None)
    first = st is None
    st = st or {}
    prev_assets = st.get("assets", {})
    seen = dict(st.get("seen", {}))
    new_state = {"last_check": now.isoformat(), "assets": {}, "seen": {}, "feeds_failed": []}

    # ---------- 1. headlines ----------
    if os.environ.get("NEWSGATE_NO_FEEDS") != "1":
        cutoff = now - timedelta(minutes=HEADLINE_MAX_AGE_MIN)
        hits, ok = [], []
        with cf.ThreadPoolExecutor(len(FEEDS)) as ex:
            futs = {ex.submit(fetch, n, u): n for n, u in FEEDS.items()}
            for fu in cf.as_completed(futs, timeout=FEED_TIMEOUT * 3):
                n = futs[fu]
                try:
                    items = fu.result()
                    ok.append(f"{n}({len(items)})")
                except Exception as e:
                    new_state["feeds_failed"].append(n)
                    notes.append(f"feed {n} failed: {type(e).__name__}: {str(e)[:80]}")
                    continue
                for it in items:
                    if it["id"] in seen:
                        continue
                    seen[it["id"]] = now.isoformat()
                    if not it["pub"] or it["pub"] < cutoff:
                        continue
                    text = it["title"] + " \n " + it["summary"]
                    labels = [l for l, rx in KW if rx.search(text)]
                    if labels:
                        hits.append((labels, it))
        notes.append("feeds ok: " + ", ".join(sorted(ok)))
        dedup = {}
        for labels, it in hits:
            dedup.setdefault(it["title"].lower(), (labels, it))
        for labels, it in dedup.values():
            headline_hits.append((labels, it))
            notes.append(f"  headline {it['pub'].astimezone().strftime('%H:%M')} {it['feed']}: {it['title']} {it['link']}")
    keep = now - timedelta(hours=SEEN_KEEP_HOURS)
    new_state["seen"] = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= keep}

    # ---------- 2-4, 6. per-asset ----------
    for a, rel in ASSET_FILES.items():
        p = os.path.join(BASE, rel)
        d = load(p)
        if not d:
            trips.append(f"{a} latest file unreadable")
            continue
        age = (time.time() - os.path.getmtime(p)) / 60
        try:
            ts_age = (now - datetime.fromisoformat(d["timestamp"])).total_seconds() / 60
            age = max(age, ts_age)
        except Exception:
            pass
        if age > STALE_MIN:
            trips.append(f"{a} latest stale {age:.0f}min (refresher broken?)")
        risk = d.get("risk") or {}
        lvl = risk.get("level")
        price = d.get("price")
        atr = (d.get("volatility") or {}).get("atr14_1h_usd_per_hour")
        side = (d.get("side_flip") or {}).get("current_allowed_side") or (d.get("recommendation") or {}).get("bot_allowed_side")
        new_state["assets"][a] = {"price": price, "risk": lvl, "side": side, "atr1h": atr, "ts": d.get("timestamp")}
        pv = prev_assets.get(a) or {}
        notes.append(f"{a} px={price} atr1h={atr} risk={lvl} side={side} age={age:.0f}m"
                     + (f" | prev px={pv.get('price')} risk={pv.get('risk')} side={pv.get('side')}" if pv else ""))
        # risk
        r_now = RISK_ORDER.get(lvl, -1)
        r_prev = RISK_ORDER.get(pv.get("risk"), -1)
        if lvl == "red":
            trips.append(f"{a} risk RED ({'; '.join(risk.get('reasons', []))[:100]})")
        elif pv and r_prev >= 0 and r_now > r_prev:
            trips.append(f"{a} risk escalated {pv.get('risk')}->{lvl}")
        elif d.get("risk_changed") and pv and r_prev >= 0 and r_now > r_prev:
            trips.append(f"{a} risk_changed escalation")
        # price move
        if pv.get("price") and price:
            mv = price - pv["price"]
            pct = mv / pv["price"] * 100
            ref_atr = atr or pv.get("atr1h")
            if abs(pct) >= MOVE_PCT or (ref_atr and abs(mv) >= MOVE_ATR_MULT * ref_atr):
                reacting = True
                trips.append(f"{a} moved {pct:+.2f}% ({mv:+.4g}, {abs(mv)/ref_atr:.1f}x 1h ATR) since last check" if ref_atr
                             else f"{a} moved {pct:+.2f}% since last check")
        # vol spike
        vs = ((risk.get("metrics") or {}).get("volatility_spike")) or {}
        rat = {k: v for k, v in vs.items() if k.endswith("_ratio") and isinstance(v, (int, float))}
        spikes = {k: v for k, v in rat.items() if v >= VOL_SPIKE_RATIO}
        if spikes:
            reacting = True
            trips.append(f"{a} vol spike " + ",".join(f"{k}={v}" for k, v in spikes.items()))
        # side
        if pv.get("side") and side and pv["side"] != side:
            trips.append(f"{a} EMA200 allowed side changed {pv['side']}->{side}")

    # ---------- 5. events ----------
    ev = load(os.path.join(BASE, EVENTS_FILE), {}) or {}
    for e in ev.get("events", []):
        if e.get("impact") != "high" or e.get("notified") or not e.get("time_mt"):
            continue
        try:
            t = datetime.fromisoformat(e["time_mt"])
        except Exception:
            continue
        if now <= t <= now + timedelta(hours=EVENT_LOOKAHEAD_H):
            trips.append(f"event in {(t-now).total_seconds()/60:.0f}min: {e.get('title','')[:90]} [{e.get('assets')}]")

    # headlines only trip if topic is NEW (not seen in 24h) AND crypto is reacting
    topics = dict(st.get("topics", {}))
    tkeep = now - timedelta(hours=24)
    topics = {k: v for k, v in topics.items() if datetime.fromisoformat(v) >= tkeep}
    new_topic_hits = []
    for labels, it in headline_hits:
        fresh = [l for l in labels if l not in topics]
        if fresh:
            new_topic_hits.append((fresh, it))
        for l in labels:
            topics[l] = now.isoformat()
    new_state["topics"] = topics
    if new_topic_hits and reacting:
        for fresh, it in new_topic_hits[:6]:
            trips.append(f"new-topic headline[{'/'.join(fresh)}] {it['title'][:120]} ({it['feed']})")
    elif headline_hits:
        notes.append(f"{len(headline_hits)} headline hit(s) not tripping (topic seen <24h or crypto not reacting)")
    if first:
        notes.insert(0, "first run: baseline recorded (price/risk/side checks QUIET)")
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_state, f, indent=1)
    os.replace(tmp, STATE)

    print(("TRIP: " + "; ".join(trips)) if trips else "QUIET")
    for n in notes:
        print(n)
    return 10 if trips else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # never crash silently: a broken gate should trip
        print(f"TRIP: newsgate error {type(e).__name__}: {e}")
        sys.exit(10)
