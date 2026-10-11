#!/usr/bin/env bash
# Cheap hourly digest for the perps bots. Prints EVENTS since last run; "QUIET" if nothing notable.
cd /workspace/sol-bot/live || exit 1
MARK=.quickcheck_last
LAST=$(cat $MARK 2>/dev/null || date -d '-65 min' '+%Y-%m-%d %H:%M:%S')
NOW=$(date '+%Y-%m-%d %H:%M:%S')
PAT='increase |TRIM|TP fill|LEDGER|CLOSE |position gone|HALT|ERROR|KILL present|SWEEP|STAKE|manual TP|withdraw'
ev=0
for b in sol:logs/bot.log eth:eth/logs/bot.log btc:btc/logs/bot.log; do
  n=${b%%:*}; f=${b#*:}
  lines=$(awk -v t="$LAST" 'substr($0,1,19)>t' "$f" | grep -E "$PAT" | grep -v '"ok": true' | cut -c1-220)
  # keep ok increases (adds/opens) as events too
  adds=$(awk -v t="$LAST" 'substr($0,1,19)>t' "$f" | grep -E 'INFO increase' | cut -c1-160)
  if [ -n "$lines$adds" ]; then ev=1; echo "== $n events"; printf '%s\n%s\n' "$adds" "$lines" | sed '/^$/d' | tail -n 12; fi
  last=$(grep -E 'INFO bar ' "$f" | tail -n1 | cut -c1-19)
  echo "$n last bar: $last"
  tp=$(grep -E 'TP state|TP ladder' "$f" | tail -n1 | cut -c1-140); echo "$n $tp"
  grep -E 'INFO bar ' "$f" | tail -n1 | grep -oE 'close=[0-9.]+|pos=[^|]+' | tr '\n' ' '; echo
  # liq distance: state.json size/collateral/avg + bar close; dca_levels.liq_price (maint 1%, borrow 0)
  sp=state.json; [ "$n" != sol ] && sp="$n/state.json"
  px=$(grep -E 'INFO bar ' "$f" | tail -n1 | grep -oE 'close=[0-9.]+' | head -1 | cut -d= -f2)
  if [ -n "$px" ] && [ -f "$sp" ]; then
    liqout=$(python3 - "$sp" "$px" "$n" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
side = int(st.get("side") or 0)
if not side:
    sys.exit(0)
size = float(st.get("pg_m_size") or st.get("last_size") or 0)
margin = float(st.get("pg_margin") or 0)
lots = st.get("lots") or []
lsz = sum(float(l["size"]) for l in lots)
qty = sum(float(l["size"]) / float(l["price"]) for l in lots) if lots else 0.0
if lsz > 0 and size > 0 and abs(lsz - size) > 1e-6:
    qty *= size / lsz
elif (not qty) and size and st.get("last_entry"):
    qty = size / float(st["last_entry"])
if size <= 0 or qty <= 0 or margin <= 0:
    sys.exit(0)
maint, borrow = 0.01, 0.0
liq = (size + side * (borrow + maint * size - margin)) / qty
px = float(sys.argv[2])
pct = abs(px - liq) / px * 100.0
name = sys.argv[3]
print(f"{name} liq={liq:.2f} ({pct:.1f}% away)")
if pct < 3.0:
    print(f"ALERT LIQ {name} {pct:.1f}%")
    sys.exit(2)
PY
)
    rc=$?
    [ -n "$liqout" ] && printf '%s\n' "$liqout"
    [ $rc -eq 2 ] && ev=1
  fi
done
for k in KILL eth/KILL btc/KILL PAUSE eth/PAUSE btc/PAUSE; do [ -e "$k" ] && { echo "FLAG $k present"; ev=1; }; done
for p in ratchet-sol ratchet-eth ratchet-btc ratchet-levels; do systemctl is-active --quiet "$p" || { echo "WRAPPER DOWN $p"; ev=1; }; done  # droplet: systemd services
# stale bar check (>40 min; bars can land ~25 min late)
for f in logs/bot.log eth/logs/bot.log btc/logs/bot.log; do
  t=$(grep -E 'INFO bar ' "$f" | tail -n1 | cut -c1-19); [ -z "$t" ] && continue
  age=$(( $(date +%s) - $(date -d "$t" +%s) )); [ $age -gt 2400 ] && { echo "STALE $f ${age}s"; ev=1; }
done
echo "$NOW" > $MARK
[ $ev -eq 0 ] && echo QUIET || echo EVENTS
# per-bot high-water marks (own coin) - Trevor 2026-10-09 19:59 MT: include in every update
for b in sol:live:SOL eth:live/eth:ETH btc:live/btc:WBTC; do IFS=: read n d u <<< "$b"; echo "$n hwm=$(jq -r '(.bank // .local.bank).hwm' /workspace/sol-bot/$d/state.json 2>/dev/null) $u bal=$(jq -r '(.bank // .local.bank) | (.balance // .bal)' /workspace/sol-bot/$d/state.json 2>/dev/null)"; done
# analyst red-flag risk block (2026-10-10 04:39 MT): red => ALERT RISK, forces attention
for b in sol:/workspace/market-analysis/latest.json eth:/workspace/market-analysis/eth/latest.json btc:/workspace/market-analysis/btc/latest.json; do IFS=: read n f <<< "$b"; r=$(jq -r '.risk | "\(.level) conf=\(.confidence) action=\(.suggested_action)"' $f 2>/dev/null); echo "$n risk $r"; case "$r" in red*) echo "ALERT RISK $n $r"; echo EVENTS;; esac; done
