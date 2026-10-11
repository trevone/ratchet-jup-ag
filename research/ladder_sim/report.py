import pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
df = pd.read_pickle('results.pkl'); g = pd.read_csv('ranking.csv', index_col=0)
cur = [k for k in g.index if k.startswith('current')]
print('current ranked:', g.loc[cur, ['score','min_adds','retain','liqm']].to_string())
for s in ('geom','power'):
    print(g[g.index.str.startswith(s)].head(2)[['score','min_adds','depth200','bounce200','liqm']].to_string())
picks = ['accel(a=1.15,b=0.1) N=16 sp=0.75% x1.0 lev+1.0','accel(a=1.15,b=0.1) N=14 sp=0.75% x1.0 lev+1.0', g[g.index.str.contains('N=20')].index[0], g[g.index.str.contains('sp=1.0%')].index[0], g[g.index.str.startswith('power')].index[0], 'current(a=1.5,b=0.0) N=12 sp=1.0% x1.0 lev+1.0','current(a=1.5,b=0.0) N=12 sp=0.75% x1.0 lev+1.0']
cols=['balance','adds','depth_pct','final_lev','avg_dist_pct','bounce_be_pct','avg_improve_pct_per_100usd','liq_beyond_deepest_pct','min_liq_margin_after_buffer_pct','unused','partial_last_add_usd','sizes']
for k in picks:
    print('\n##', k, 'score', g.score.get(k)); print(df[df.key==k][cols].to_string(index=False))
fig, ax = plt.subplots(1, 2, figsize=(14, 5.5))
for k in picks[:3] + picks[-2:-1]:
    r = df[(df.key==k)&(df.balance==200)].iloc[0]
    ax[0].plot(range(1, len(r.sizes)+1), r.sizes, 'o-', label=k)
    px, av = zip(*r.path); l, = ax[1].plot(px, av, 'o-', label=k); ax[1].plot(px, px, ':', color='gray')
ax[0].set(title='Add collateral by add # ($200 balance)', xlabel='add #', ylabel='collateral $'); ax[0].legend(fontsize=7)
ax[1].set(title='Average entry vs fill price ($200, entry 100)', xlabel='fill price', ylabel='avg'); ax[1].invert_xaxis(); ax[1].legend(fontsize=7)
plt.tight_layout(); plt.savefig('ladders.png', dpi=110)
