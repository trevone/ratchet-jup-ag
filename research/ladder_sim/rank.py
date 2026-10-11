import pandas as pd, numpy as np
df = pd.read_pickle('results.pkl')
df['retain'] = 1 - df.bounce_be_pct / df.depth_pct          # share of drawdown the avg has "caught up"
df['ok'] = (df.adds >= 8)
g = df.groupby('key').agg(min_adds=('adds','min'), ok=('ok','all'), adds200=('adds', lambda s: s[df.loc[s.index,'balance']==200].iloc[0]),
    depth200=('depth_pct', lambda s: s[df.loc[s.index,'balance']==200].iloc[0]), retain=('retain','mean'),
    bounce200=('bounce_be_pct', lambda s: s[df.loc[s.index,'balance']==200].iloc[0]),
    liqm=('min_liq_margin_after_buffer_pct','min'), partial=('partial_last_add_usd', lambda s: s[df.loc[s.index,'balance']==200].iloc[0]))
g = g[g.ok].copy()
# score: adds (to 16) 35%, avg catch-up 40%, depth covered (to 15%) 15%, liq margin (to 3%) 10%
g['score'] = (.35*np.minimum(g.min_adds,16)/16 + .40*g.retain + .15*np.minimum(g.depth200,15)/15 + .10*np.minimum(g.liqm,3)/3).round(4)
g = g.sort_values('score', ascending=False)
g.to_csv('ranking.csv')
print(g.head(15).to_string()); print(len(g))
cur = df[df.scheme=='current']; print(cur[['balance','spacing','widen','adds','depth_pct','bounce_be_pct','retain','sizes']].to_string())
