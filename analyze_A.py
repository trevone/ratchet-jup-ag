"""Variant A (no stop) run analysis: peaks, liquidation runs, reset/skim hypotheticals, last-2y."""
import json
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from data import load
from strategy import Params, DcaStrategy, compute_indicators
from backtest import run, START_EQ

pd.set_option("display.width", 250)
raw = load()
full = compute_indicators(raw, Params())
full = full[full["trend_up"].notna() & full["rsi"].notna()]


class ResetStrategy(DcaStrategy):
    """Hypothetical: top up to $10k after each liquidation; optionally skim cash above $10k after each TP."""
    def __init__(self, p, eq, skim):
        super().__init__(p, eq); self.skim = skim; self.withdrawn = 0.0; self.topped_up = 0.0
    def _close(self, t, price, reason, apply_slip=True):
        tr = super()._close(t, price, reason, apply_slip)
        if reason == "liquidation" and self.cash < START_EQ:
            self.topped_up += START_EQ - self.cash; self.cash = START_EQ
        elif self.skim and reason == "take_profit" and self.cash > START_EQ:
            self.withdrawn += self.cash - START_EQ; self.cash = START_EQ
        return tr


def run_cls(df, strat):
    cols = ["open", "high", "low", "close", "rsi", "trend_up"]
    vals = df[cols].to_numpy(dtype=object); eq = np.empty(len(df))
    for i, (t, row) in enumerate(zip(df.index, vals)):
        bar = dict(zip(cols, row)); bar["time"] = t; strat.on_bar(bar); eq[i] = strat.equity(row[3])
    return strat, pd.Series(eq, index=df.index)


def analyze(df, label):
    strat, eq = run(df, Params())
    tr = pd.DataFrame(strat.trades)
    tr["eq_before"] = tr.equity_after - tr.pnl
    out = {"label": label, "start": str(df.index[0]), "end": str(df.index[-1])}
    out["peak_mtm"] = dict(equity=round(eq.max(), 2), time=str(eq.idxmax()), ret=round(eq.max() / START_EQ - 1, 4))
    i = tr.equity_after.idxmax()
    out["peak_closed"] = dict(equity=round(tr.equity_after[i], 2), time=str(tr.closed[i]), ret=round(tr.equity_after[i] / START_EQ - 1, 4))
    liq_rows, prev_t, prev_eq, prev_idx = [], df.index[0], START_EQ, -1
    for idx in tr.index[tr.reason == "liquidation"]:
        seg = tr.loc[prev_idx + 1: idx - 1]
        r = tr.loc[idx]
        liq_rows.append(dict(liq_time=str(r.closed), side="long" if r.side > 0 else "short",
                             opened=str(r.opened), avg_entry=round(r.avg_entry, 2), liq_price=round(r.exit_price, 2),
                             eq_before=round(r.eq_before, 2), eq_after=round(r.equity_after, 2),
                             loss_pct=round(r.pnl / r.eq_before, 4),
                             wins_since_prev=int((seg.pnl > 0).sum()), other_trades=int((seg.pnl <= 0).sum()),
                             run_profit=round(seg.pnl.sum(), 2), run_profit_pct=round(r.eq_before / prev_eq - 1, 4),
                             days_since_prev=round((r.closed - prev_t) / pd.Timedelta("1D"), 1)))
        prev_t, prev_eq, prev_idx = r.closed, r.equity_after, idx
    tail = tr.loc[prev_idx + 1:]
    out["after_last_liq"] = dict(since=str(prev_t), wins=int((tail.pnl > 0).sum()), profit=round(tail.pnl.sum(), 2),
                                 days=round((df.index[-1] - prev_t) / pd.Timedelta("1D"), 1), final_equity=round(eq.iloc[-1], 2))
    L = pd.DataFrame(liq_rows)
    if len(L):
        out["days_between_liqs"] = dict(median=float(L.days_since_prev.median()), max=float(L.days_since_prev.max()),
                                        min=float(L.days_since_prev.min()))
        out["first_liq"] = liq_rows[0]
        # peak before first liquidation
        pre = eq[eq.index < pd.Timestamp(liq_rows[0]["liq_time"])]
        out["peak_before_first_liq"] = dict(equity=round(pre.max(), 2), time=str(pre.idxmax()))
    # hypotheticals
    rs, req = run_cls(df, ResetStrategy(Params(), START_EQ, skim=False))
    rt = pd.DataFrame(rs.trades); runs = []; start_i = 0
    for k, idx in enumerate(rt.index[rt.reason == "liquidation"]):
        seg = rt.loc[start_i:idx - 1]
        runs.append(dict(run=k + 1, ended=str(rt.closed[idx]), wins=int((seg.pnl > 0).sum()),
                         peak_closed=round(max([START_EQ] + list(seg.equity_after)), 2),
                         eq_before_liq=round(rt.equity_after[idx] - rt.pnl[idx], 2),
                         eq_after_liq=round(rt.equity_after[idx], 2)))
        start_i = idx + 1
    out["reset_runs"] = runs
    out["reset_total_topped_up"] = round(rs.topped_up, 2); out["reset_final_equity"] = round(req.iloc[-1], 2)
    ss, seq = run_cls(df, ResetStrategy(Params(), START_EQ, skim=True))
    out["skim"] = dict(withdrawn=round(ss.withdrawn, 2), topped_up=round(ss.topped_up, 2),
                       final_equity=round(seq.iloc[-1], 2),
                       net=round(ss.withdrawn - ss.topped_up + seq.iloc[-1] - START_EQ, 2))
    return out, L, eq, tr


res_full, L_full, eq_full, tr_full = analyze(full, "full")
res_2y, L_2y, eq_2y, _ = analyze(full[full.index >= "2024-10-01"], "last2y")
L_full.to_csv("A_liquidations.csv", index=False)
json.dump({"full": res_full, "last2y": res_2y}, open("A_runs.json", "w"), indent=2, default=str)
print(L_full.drop(columns=["opened"]).to_string(index=False))
print(json.dumps({k: v for k, v in res_full.items() if k != "first_liq"}, indent=1, default=str))
print("---- last 2y"); print(L_2y.drop(columns=["opened"]).to_string(index=False))
print(json.dumps({k: v for k, v in res_2y.items() if k != "first_liq"}, indent=1, default=str))

fig, ax = plt.subplots(figsize=(13, 7))
ax.plot(eq_full, lw=1.2, label="Variant A equity (no stop, start $10k)")
ax.plot(START_EQ * full.close / full.close.iloc[0], color="gray", lw=.8, alpha=.6, label="Buy & hold SOL")
for j, (_, r) in enumerate(L_full.iterrows()):
    t = pd.Timestamp(r.liq_time)
    ax.vlines(t, r.eq_after, r.eq_before, color="red", lw=2)
    ax.scatter([t], [r.eq_after], color="red", marker="v", zorder=5)
    ax.annotate(f"#{j+1} {t:%Y-%m-%d}\n\\${r.eq_before:,.0f} to \\${r.eq_after:,.0f}", (t, r.eq_after), fontsize=7,
                xytext=(4, -26 if j % 2 == 0 else 10), textcoords="offset points", color="darkred")
pk = pd.Timestamp(res_full["peak_mtm"]["time"])
ax.scatter([pk], [res_full["peak_mtm"]["equity"]], color="green", zorder=6, label=f"Peak \\${res_full['peak_mtm']['equity']:,.0f} ({pk:%Y-%m-%d})")
ax.scatter([], [], color="red", marker="v", label="Liquidation (15)")
ax.set_yscale("log"); ax.grid(alpha=.3, which="both"); ax.legend(loc="upper right")
ax.set_ylabel("Equity, USD (log)")
ax.set_title(f"Variant A: pure DCA to 8% TP, no stop | Binance SOLUSDT 1h {full.index[0]:%Y-%m-%d} to {full.index[-1]:%Y-%m-%d}")
plt.tight_layout(); plt.savefig("equity_A_runs.png", dpi=120)
