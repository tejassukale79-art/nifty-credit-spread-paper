"""Breakdown of the two-year overnight credit-spread backtest."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

pd.set_option("display.width", 240)

tr = pd.read_csv(config.RESULTS_DIR / "trades_2year_sl15.csv")
tr["exit_ts"] = pd.to_datetime(tr["exit_ts"])
tr["entry_ts"] = pd.to_datetime(tr["entry_ts"])
tr["month"] = tr["exit_ts"].dt.to_period("M").astype(str)
tr["year"] = tr["exit_ts"].dt.year
tr["fy"] = np.where(tr["exit_ts"] < "2025-08-19", "Y1 (Sep24-Aug25)", "Y2 (Aug25-Aug26)")

net = tr["net_pnl"]
eq = net.cumsum()
w, l = net[net > 0], net[net <= 0]
print("=" * 74)
print("NIFTY CREDIT SPREAD - OVERNIGHT, SL 15%% OF MARGIN, 1 LOT")
print("=" * 74)
print("  window          %s -> %s" % (tr["date"].min(), tr["date"].max()))
print("  trades          %d   (bull_put %d, bear_call %d)"
      % (len(tr), (tr["type"] == "bull_put").sum(), (tr["type"] == "bear_call").sum()))
print("  win rate        %.1f%%   (%dW / %dL)" % (100 * len(w) / len(tr), len(w), len(l)))
print("  gross           Rs {:,.0f}".format(tr["gross_pnl"].sum()))
print("  charges         Rs {:,.0f}   ({:.0f}% of gross)".format(
    tr["charges"].sum(), 100 * tr["charges"].sum() / tr["gross_pnl"].sum()))
print("  NET             Rs {:,.0f}".format(net.sum()))
print("  avg / trade     Rs {:,.0f}".format(net.mean()))
print("  avg win / loss  Rs {:,.0f} / Rs {:,.0f}".format(w.mean(), l.mean()))
print("  profit factor   {:.2f}".format(w.sum() / -l.sum()))
print("  best / worst    Rs {:,.0f} / Rs {:,.0f}".format(net.max(), net.min()))
print("  max drawdown    Rs {:,.0f}".format((eq.cummax() - eq).max()))
print("  avg margin      Rs {:,.0f}".format(tr["margin"].mean()))
print("  t-stat          {:.2f}".format(net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))))

boot = np.array([np.random.default_rng(i).choice(net.values, len(net), replace=True).sum()
                 for i in range(4000)])
print("  bootstrap 90%%   Rs {:,.0f} to Rs {:,.0f}   (losing resamples {:.1f}%%)".format(
    np.percentile(boot, 5), np.percentile(boot, 95), 100 * (boot < 0).mean()))


def grp(title, col):
    print("\n" + title)
    g = tr.groupby(col)
    out = pd.DataFrame({
        "trades": g.size(),
        "win%": (g["net_pnl"].apply(lambda s: round(100 * (s > 0).mean(), 1))),
        "net": g["net_pnl"].sum().round(0).astype(int),
        "avg": g["net_pnl"].mean().round(0).astype(int),
    })
    print(out.to_string())


grp("BY YEAR", "fy")
grp("BY TYPE", "type")
grp("BY EXIT", "exit_reason")
grp("BY LOT SIZE (contract size changed over the window)", "lot")

print("\nBY MONTH")
m = tr.groupby("month")["net_pnl"].agg(["size", "sum"])
m.columns = ["trades", "net"]
m["net"] = m["net"].round(0).astype(int)
m["cum"] = m["net"].cumsum()
print(m.to_string())

# how the stop behaves: what did SL trades look like at the time-exit instead?
ns = pd.read_csv(config.RESULTS_DIR / "trades_2year_nostop.csv")
print("\nSTOP vs NO STOP")
print("  with SL 15%%:  %d trades, net Rs {:,.0f}, maxDD Rs {:,.0f}".format(
    net.sum(), (eq.cummax() - eq).max()) % len(tr))
n2 = ns["net_pnl"]
e2 = n2.cumsum()
print("  no stop    :  %d trades, net Rs {:,.0f}, maxDD Rs {:,.0f}".format(
    n2.sum(), (e2.cummax() - e2).max()) % len(ns))
