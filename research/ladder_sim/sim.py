"""Offline DCA add-ladder sizing sim (no chain/IO besides writing results here).
Reuses Jupiter formulas from dca_levels.liq_price (maint 1%, 6bps open/close fee, borrow=0)."""
import sys, itertools, numpy as np, pandas as pd
sys.path.insert(0, '/workspace/sol-bot')
from dca_levels import liq_price
FEE, MAINT, ATR_PCT, BUF_ATR, MIN_ADD, FEE_PER_ADD = 6e-4, 0.01, 1.0, 2.0, 3.0, 0.10

def rungs(n, sp, widen):
    gaps = [sp * widen**k for k in range(n + 1)]   # extra gap = 'next rung' for the last add
    px, out = 100.0, []
    for g in gaps: px *= 1 - g / 100; out.append(px)
    return out
def weights(scheme, n, a, b):
    k = np.arange(n)
    if scheme in ('geom', 'current'): return a ** k
    if scheme == 'accel':
        r, w = a, [1.0]
        for _ in range(n - 1): w.append(w[-1] * r); r = min(2.5, r + b)
        return np.array(w)
    if scheme == 'power': return (k + 1.0) ** a
def fit(w, budget):
    """scale weights to budget with $3 floor; drop deepest... no: drop rungs from the END if floor can't fit"""
    w = np.array(w, float)
    while len(w) and MIN_ADD * len(w) > budget + 1e-9: w = w[:-1]
    s = budget * w / w.sum()
    fl = np.zeros(len(w), bool)
    for _ in range(60):
        new = (s < MIN_ADD - 1e-9) & ~fl
        if not new.any(): break
        fl |= new; s[fl] = MIN_ADD
        s[~fl] = (budget - MIN_ADD * fl.sum()) * w[~fl] / w[~fl].sum()
    return s
def run(bal, n, sp, widen, scheme, a, b, lev_step):
    first = max(10.5, 0.10 * bal); free = bal - first - 0.10
    budget = free - FEE_PER_ADD * n
    sizes = fit(weights(scheme, n, a, b), budget)
    R = rungs(n, sp, widen)
    size = first * 5; qty = size / 100; margin = first - size * FEE; lev = 5.0
    adds, path, minmargin = [], [(100.0, 100.0)], 1e9
    for i, c in enumerate(sizes):
        p = R[i]; L = min(20.0, lev + lev_step); notional = c * L
        nsize, nqty, nmargin = size + notional, qty + notional / p, margin + c - notional * FEE
        lq = liq_price(1, nsize, nqty, nmargin, 0.0, MAINT)
        mrg = (R[i + 1] - lq) / p * 100 - BUF_ATR * ATR_PCT   # % beyond next rung after the 2-ATR buffer
        if mrg < 0: break
        size, qty, margin, lev = nsize, nqty, nmargin, L; adds.append((c, L, notional)); minmargin = min(minmargin, mrg)
        path.append((p, size / qty))
    k = len(adds); pf = R[k - 1] if k else 100.0; avg = size / qty
    be = avg * (1 + FEE) / (1 - FEE)    # close-all break-even incl. close fee + the open fee paid
    dep = sum(x[0] for x in adds) + first
    gapnext = (R[k] / pf - 1) if k else 0
    lp = None
    if k:   # partial proxy: close last add after bounce of one spacing (its notional, open+close fees)
        n_last = adds[-1][2]; bp = pf * (1 + sp * widen ** (k - 1) / 100)
        lp = n_last / pf * bp - n_last - n_last * FEE - n_last / pf * bp * FEE
    lq = liq_price(1, size, qty, margin, 0, MAINT)
    cs = [x[0] for x in adds]
    return dict(balance=bal, n_rungs=n, spacing=sp, widen=widen, scheme=scheme, a=a, b=b, lev_step=lev_step,
        adds=k, depth_pct=round(100 - pf, 3), final_lev=lev, avg=round(avg, 4),
        avg_dist_pct=round((avg / pf - 1) * 100, 3), bounce_be_pct=round((be / pf - 1) * 100, 3),
        avg_improve_pct_per_100usd=round((100 - avg) / max(dep - first, 1e-9) * 100, 3),
        liq=round(lq, 3), liq_beyond_deepest_pct=round((pf - lq) / pf * 100, 3), min_liq_margin_after_buffer_pct=round(minmargin, 3),
        deployed=round(dep, 2), unused=round(bal - dep, 2), first3=cs[:3], last3=cs[-3:],
        partial_last_add_usd=round(lp, 3) if lp else None, sizes=[round(x, 2) for x in cs], path=path)
cfgs = [('current', 1.5, 0, 1.0)]
cfgs += [('geom', r, 0, s) for r in (1.1, 1.2, 1.3, 1.4, 1.5, 1.6) for s in (1.0, 1.5, 2.0)]
cfgs += [('accel', r, d, s) for r in (1.05, 1.1, 1.15, 1.2) for d in (0.05, 0.1, 0.15) for s in (1.0, 1.5, 2.0)]
cfgs += [('power', p, 0, s) for p in (1, 1.5, 2, 2.5, 3) for s in (1.0, 1.5, 2.0)]
rows = []
for bal in (75, 200, 1000):
    for (sc, a, b, s) in cfgs:
        for n in (8, 10, 12, 14, 16, 20):
            if sc == 'current' and n != 12: continue
            for sp in (0.75, 1.0, 1.5, 2.0):
                for w in (1.0, 1.1, 1.2):
                    rows.append(run(bal, n, sp, w, sc, a, b, s))
df = pd.DataFrame(rows)
df['key'] = df.apply(lambda r: f"{r.scheme}(a={r.a},b={r.b}) N={r.n_rungs} sp={r.spacing}% x{r.widen} lev+{r.lev_step}", axis=1)
df.drop(columns=['path']).to_csv('results.csv', index=False)
df.to_pickle('results.pkl')
