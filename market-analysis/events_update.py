#!/usr/bin/env python3
"""Manage /workspace/market-analysis/events.json (no web access here; an agent does the scanning).

File shape: {"scanned_at": ISO, "events": [{id, title, category, time_mt, published_mt, status, impact,
             recommendation, source_url, notified, first_seen, last_seen, assets, ...optional pause_before_min/
             pause_after_min/pause_until_mt}]}
assets: which outlooks an event applies to: "all" or a list from ["SOL", "ETH", "BTC"]. Untagged events default by
        category: solana -> ["SOL"], ethereum -> ["ETH"], bitcoin -> ["BTC"], macro/crypto -> "all".
        Re-merging an event without "assets" keeps the existing tag.

CLI:
  python3 events_update.py merge NEW.json [--scan]   # NEW.json = list or {"events": [...]}; '-' = stdin
                                                     # dedupe: scheduled = title+MT date, news = source_url;
                                                     # include an existing "id" to update/retime that event
  python3 events_update.py mark-notified ID_OR_TEXT [...] | --due MIN   # by id / title substring / due within MIN
  python3 events_update.py prune [--breaking-hours 24] [--grace-min 60]
  python3 events_update.py list [--hours 24] [--asset ETH]   # upcoming within N hours + recent breaking
  python3 events_update.py pause-status [--asset BTC]  # is a high-impact pause window active now?
  python3 events_update.py tag ID ASSETS [ID ASSETS ...]  # set assets, e.g. tag abc123 ETH,BTC / tag abc123 all
  python3 events_update.py migrate                     # write default asset tags into untagged events
  python3 events_update.py remove ID [...]             # delete events by id (superseded/false items)
Library: load(), save(), merge(), mark_notified(), prune(), relevant(), pause_window(), active_pauses(),
         norm_assets(), applies_to()
"""
import json, os, re, sys, hashlib, tempfile, datetime as dt
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")
D = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(D, "events.json")
CATS, STATUSES, IMPACTS = {"macro", "solana", "crypto", "ethereum", "bitcoin"}, {"scheduled", "breaking", "ongoing"}, {"low", "med", "high"}
ASSETS = ("SOL", "ETH", "BTC")
ASSET_ALIASES = {"SOLANA": "SOL", "ETHEREUM": "ETH", "ETHER": "ETH", "BITCOIN": "BTC", "XBT": "BTC"}
CAT_DEFAULT_ASSETS = {"solana": ["SOL"], "ethereum": ["ETH"], "bitcoin": ["BTC"]}  # anything else -> "all"
DEFAULT_PAUSE_MIN = 30

def now_mt(): return dt.datetime.now(MT)
def iso(t): return t.astimezone(MT).isoformat(timespec="seconds") if t else None
def parse(s):
    """ISO string -> aware datetime in MT (naive strings are assumed to be MT). None-safe."""
    if not s: return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return (t.replace(tzinfo=MT) if t.tzinfo is None else t).astimezone(MT)

def norm_assets(v, category=None):
    """'all' | list of ASSETS (canonical order). None/'' -> default from category. Accepts 'ETH,BTC', ['eth'], 'Solana'."""
    if v is None or v == "" or v == []:
        return list(CAT_DEFAULT_ASSETS[category]) if category in CAT_DEFAULT_ASSETS else "all"
    if isinstance(v, str):
        if v.strip().lower() in ("all", "*"): return "all"
        v = [x for x in re.split(r"[,\s/+]+", v) if x]
    out = set()
    for x in v:
        x = str(x).strip().upper()
        if x in ("ALL", "*"): return "all"
        x = ASSET_ALIASES.get(x, x)
        if x not in ASSETS: raise ValueError(f"bad asset {x!r} (allowed: {', '.join(ASSETS)} or 'all')")
        out.add(x)
    return [a for a in ASSETS if a in out]

def applies_to(e, asset):
    """True if event e is tagged for `asset` (or 'all'); asset=None matches everything."""
    if asset is None: return True
    tags = norm_assets(e.get("assets"), e.get("category"))
    return tags == "all" or asset.upper() in tags

def _norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
def key(e):
    """Dedupe key: scheduled -> title + MT date; news -> source_url (fallback title)."""
    if e.get("time_mt"):
        return f"s|{_norm(e['title'])}|{parse(e['time_mt']).date()}"
    return f"n|{(e.get('source_url') or '').split('#')[0].rstrip('/').lower() or _norm(e['title'])}"
def make_id(e): return hashlib.sha1(key(e).encode()).hexdigest()[:12]

def normalize(e):
    """Accept legacy/foreign shapes ({time, links, impact:'medium'}) and map them onto this schema."""
    e = dict(e)
    if "time_mt" not in e and e.get("time"):
        t = iso(parse(e.pop("time")))
        if e.get("status") == "scheduled": e["time_mt"] = t
        else: e["time_mt"] = None; e.setdefault("published_mt", t)
    if not e.get("source_url") and e.get("links"): e["source_url"] = e["links"][0]
    e["impact"] = {"medium": "med", "moderate": "med", "mid": "med"}.get(e.get("impact"), e.get("impact"))
    e.setdefault("time_mt", None); e.setdefault("published_mt", None); e.setdefault("notified", False)
    e.setdefault("recommendation", "no action")
    e["assets"] = norm_assets(e.get("assets"), e.get("category"))
    return e

def load(path=PATH):
    if not os.path.exists(path): return {"scanned_at": None, "events": []}
    with open(path) as f: data = json.load(f)
    data.setdefault("scanned_at", None)
    data["events"] = [normalize(e) for e in data.get("events", [])]
    return data

def save(data, path=PATH):
    data["events"].sort(key=lambda e: (parse(e.get("time_mt") or e.get("published_mt")) or now_mt()).timestamp())
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".events.", suffix=".json")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2)
    os.chmod(tmp, 0o644); os.replace(tmp, path)  # atomic so outlook.py never reads a half-written file

def validate(e):
    for k in ("title", "category", "status", "impact", "recommendation", "source_url"):
        if not e.get(k): raise ValueError(f"event missing '{k}': {e.get('title')}")
    e["assets"] = norm_assets(e.get("assets"), e.get("category"))
    if e["category"] not in CATS: raise ValueError(f"bad category {e['category']}")
    if e["status"] not in STATUSES: raise ValueError(f"bad status {e['status']}")
    if e["impact"] not in IMPACTS: raise ValueError(f"bad impact {e['impact']}")
    if e["status"] == "scheduled" and not e.get("time_mt"): raise ValueError(f"scheduled event needs time_mt: {e['title']}")
    for k in ("time_mt", "published_mt", "pause_until_mt"):
        if e.get(k): e[k] = iso(parse(e[k]))  # normalise to MT ISO with offset
    return e

def merge(data, new_events, scanned=False):
    """Insert or update by dedupe key. Keeps notified=True and first_seen from the existing copy."""
    t = iso(now_mt()); idx = {key(e): e for e in data["events"]}; added = updated = 0
    by_id = {e.get("id"): e for e in data["events"] if e.get("id")}
    for e in new_events:
        explicit_assets = e.get("assets") not in (None, "", [])
        e = validate(normalize(e)); k = key(e)
        if e.get("id") in by_id or k in idx:  # pass an existing "id" to update an event whose title/time changed
            old = by_id.get(e.get("id")) or idx[k]; keep_n = old.get("notified", False); first = old.get("first_seen", t)
            if not explicit_assets and old.get("assets") not in (None, "", []): e["assets"] = old["assets"]  # keep tag
            old.update(e); old["notified"] = keep_n or bool(e.get("notified")); old["first_seen"] = first
            old["last_seen"] = t; old["id"] = old.get("id") or make_id(old); updated += 1
        else:
            e.setdefault("notified", False); e.setdefault("published_mt", None)
            e["id"] = make_id(e); e["first_seen"] = e["last_seen"] = t
            data["events"].append(e); idx[k] = e; added += 1
    if scanned: data["scanned_at"] = t
    return added, updated

def mark_notified(data, selectors=(), due_min=None):
    n, now = 0, now_mt()
    for e in data["events"]:
        hit = any(s == e.get("id") or s.lower() in e["title"].lower() for s in selectors)
        if due_min is not None and not e.get("notified"):
            tm = parse(e.get("time_mt"))
            if (tm and now <= tm <= now + dt.timedelta(minutes=due_min)) or e["status"] == "breaking": hit = True
        if hit and not e.get("notified"): e["notified"] = True; n += 1
    return n

def pause_window(e):
    """(start, end) of the no-new-entries window for a HIGH-impact event, else None.
    Default 30 min before/after time_mt. Overridden by explicit pause_before_min/pause_after_min/pause_until_mt
    fields or by the 'pause' clause of the recommendation, e.g. 'pause new entries 60 min before/after',
    '15 min before, 45 min after', 'until 15:00 MT', '2h after'. 'no pause'/'no action' => no window.
    Breaking/ongoing items (no time_mt) only pause if they give an explicit until-time."""
    if e.get("impact") != "high": return None
    rec = (e.get("recommendation") or "").lower()
    if re.search(r"\bno (pause|action)\b", rec) and "pause new" not in rec: return None
    anchor = parse(e.get("time_mt")) or parse(e.get("published_mt"))
    if anchor is None: return None
    clause = " ".join(c for c in re.split(r"[;.]\s*", rec) if "pause" in c)
    unit = lambda n, u: int(float(n) * (60 if u.startswith("h") else 1))
    before = after = DEFAULT_PAUSE_MIN if e.get("time_mt") else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hours?|m|min|minutes?)\s*before\s*(?:/|and|&)\s*after", clause)
    if m: before = after = unit(*m.groups())
    else:
        mb = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hours?|m|min|minutes?)\s*before", clause)
        ma = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hours?|m|min|minutes?)\s*after", clause)
        if mb: before = unit(*mb.groups())
        if ma: after = unit(*ma.groups())
    end = anchor + dt.timedelta(minutes=after) if after is not None else None
    mu = re.search(r"until\s+(\d{1,2}):(\d{2})", clause)
    if mu:
        u = anchor.replace(hour=int(mu.group(1)), minute=int(mu.group(2)), second=0, microsecond=0)
        if u < anchor: u += dt.timedelta(days=1)
        end = u
    if e.get("pause_before_min") is not None: before = int(e["pause_before_min"])
    if e.get("pause_after_min") is not None: end = anchor + dt.timedelta(minutes=int(e["pause_after_min"]))
    if e.get("pause_until_mt"): end = parse(e["pause_until_mt"])
    if end is None: return None
    start = anchor - dt.timedelta(minutes=before or 0) if e.get("time_mt") else anchor
    return start, end

def active_pauses(events, now=None, asset=None):
    now = now or now_mt(); out = []
    for e in events:
        if not applies_to(e, asset): continue
        w = pause_window(e)
        if w and w[0] <= now <= w[1]: out.append((e, w))
    return out

def relevant(data, hours=24, breaking_hours=24, now=None, asset=None):
    """Upcoming scheduled events within `hours` (plus ones whose pause window is still open) and
    breaking/ongoing items published or re-confirmed within `breaking_hours`. asset='ETH' etc. keeps only
    events tagged for that asset or 'all'."""
    now = now or now_mt(); out = []
    for e in data["events"]:
        if not applies_to(e, asset): continue
        tm, w = parse(e.get("time_mt")), pause_window(e)
        if tm:
            if now <= tm <= now + dt.timedelta(hours=hours) or (w and w[0] <= now <= w[1]): out.append(e)
        else:
            ref = parse(e.get("published_mt")) or parse(e.get("last_seen"))
            if ref and now - ref <= dt.timedelta(hours=breaking_hours): out.append(e)
    return out

def prune(data, breaking_hours=24, grace_min=60, now=None):
    """Drop scheduled events once time_mt + max(pause window end, grace) has passed; drop breaking/ongoing
    items older than `breaking_hours` (by published_mt, else last_seen; ongoing re-merged items stay)."""
    now = now or now_mt(); keep = []
    for e in data["events"]:
        tm = parse(e.get("time_mt"))
        if tm:
            w = pause_window(e); end = max(tm + dt.timedelta(minutes=grace_min), w[1] if w else tm)
            if end >= now: keep.append(e)
        else:
            ref = parse(e.get("published_mt")) if e["status"] == "breaking" else None
            ref = ref or parse(e.get("last_seen")) or parse(e.get("published_mt"))
            if ref and now - ref <= dt.timedelta(hours=breaking_hours): keep.append(e)
    removed = len(data["events"]) - len(keep); data["events"] = keep
    return removed

def _read_new(arg):
    raw = json.load(sys.stdin if arg == "-" else open(arg))
    return raw["events"] if isinstance(raw, dict) else raw

def main(argv):
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default=PATH)
    sp = p.add_subparsers(dest="cmd", required=True)
    m = sp.add_parser("merge"); m.add_argument("src"); m.add_argument("--scan", action="store_true")
    m.add_argument("--no-prune", action="store_true")
    n = sp.add_parser("mark-notified"); n.add_argument("sel", nargs="*"); n.add_argument("--due", type=int)
    r = sp.add_parser("prune"); r.add_argument("--breaking-hours", type=float, default=24); r.add_argument("--grace-min", type=int, default=60)
    l = sp.add_parser("list"); l.add_argument("--hours", type=float, default=24); l.add_argument("--asset")
    ps_ = sp.add_parser("pause-status"); ps_.add_argument("--asset")
    rm = sp.add_parser("remove"); rm.add_argument("ids", nargs="+")
    tg = sp.add_parser("tag"); tg.add_argument("pairs", nargs="+", help="ID ASSETS [ID ASSETS ...]")
    sp.add_parser("migrate")
    a = p.parse_args(argv); data = load(a.file)
    if a.cmd == "merge":
        ad, up = merge(data, _read_new(a.src), scanned=a.scan)
        pr = 0 if a.no_prune else prune(data); save(data, a.file)
        print(json.dumps({"added": ad, "updated": up, "pruned": pr, "total": len(data["events"])}))
    elif a.cmd == "mark-notified":
        k = mark_notified(data, a.sel, a.due); save(data, a.file); print(json.dumps({"marked": k}))
    elif a.cmd == "prune":
        k = prune(data, a.breaking_hours, a.grace_min); save(data, a.file); print(json.dumps({"pruned": k, "total": len(data["events"])}))
    elif a.cmd == "list":
        print(json.dumps(relevant(data, a.hours, asset=a.asset), indent=2))
    elif a.cmd == "remove":
        n0 = len(data["events"]); data["events"] = [e for e in data["events"] if e.get("id") not in a.ids]
        save(data, a.file); print(json.dumps({"removed": n0 - len(data["events"])}))
    elif a.cmd == "tag":
        if len(a.pairs) % 2: raise SystemExit("tag needs ID ASSETS pairs")
        by_id, done = {e.get("id"): e for e in data["events"]}, {}
        for i in range(0, len(a.pairs), 2):
            eid, tags = a.pairs[i], norm_assets(a.pairs[i + 1])
            if eid not in by_id: raise SystemExit(f"unknown id {eid}")
            by_id[eid]["assets"] = tags; done[eid] = tags
        save(data, a.file); print(json.dumps({"tagged": done}))
    elif a.cmd == "migrate":
        save(data, a.file)  # load() already normalised every event's assets tag
        import collections
        print(json.dumps({"total": len(data["events"]), "assets": dict(collections.Counter(
            "all" if e["assets"] == "all" else ",".join(e["assets"]) for e in data["events"]))}))
    elif a.cmd == "pause-status":
        ps = active_pauses(data["events"], asset=a.asset)
        print(json.dumps({"event_pause_active": bool(ps), "now_mt": iso(now_mt()), "asset": a.asset,
                          "windows": [{"title": e["title"], "start": iso(w[0]), "end": iso(w[1])} for e, w in ps]}, indent=2))

if __name__ == "__main__": main(sys.argv[1:])
