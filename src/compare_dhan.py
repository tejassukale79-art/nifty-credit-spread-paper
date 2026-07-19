"""Compare the Jan-2025+ replica backtest with Dhan's live record for
Stratzy's Zen Credit Spread Overnight (the strategy this system replicates).

Dhan inputs (results/dhan_*.json, fetched from algo-api.dhan.co):
  GetHistoricalTrades   - 174 live trade legs with entry time, symbols, pnl, lots
  GetHistoricalPnlBreakdown - daily rupee P&L
  GetQuantStats         - monthly % heatmap, win rate, drawdown stats

Backtest input: results/trades_jan25_overnight_sl15.csv (1 lot).
Dhan trades run 5 lots -> everything is normalised to per-lot rupees.
"""
import json
import re

import numpy as np
import pandas as pd

import config

R = config.RESULTS_DIR
LIVE_START = pd.Timestamp("2025-07-09")


def load_dhan_trades():
    raw = json.load(open(R / "dhan_GetHistoricalTrades.json"))["data"]
    rows = []
    for t in raw:
        m = re.search(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2})$", t["name"])
        entry = pd.to_datetime(m.group(1), format="%d/%m/%Y %H:%M")
        lots = max((s.get("lotsTraded") or 0) for s in t["summary"]) or 5
        syms = {tr["symbol"] for s in t["summary"] for tr in s["trades"]}
        strikes, kind = [], None
        for s in syms:
            sm = (re.match(r"NIFTY\d{3}\d{2}(\d{4,5})(CE|PE)$", s)        # weekly
                  or re.match(r"NIFTY\d{2}[A-Z]{3}(\d{4,5})(CE|PE)$", s))  # monthly
            if sm:
                strikes.append(int(sm.group(1)))
                kind = sm.group(2)
        typ = ("bull_put" if kind == "PE" else "bear_call") if kind else "?"
        rows.append({"entry_ts": entry, "date": entry.date(), "type": typ,
                     "short_strike": max(strikes) if kind == "PE" else (min(strikes) if strikes else np.nan),
                     "long_strike": min(strikes) if kind == "PE" else (max(strikes) if strikes else np.nan),
                     "pnl": t["pnl"], "lots": lots,
                     "pnl_per_lot": t["pnl"] / lots,
                     "exit_ts": pd.to_datetime(t["closedOn"]).tz_convert("Asia/Kolkata").tz_localize(None) if t.get("closedOn") else None})
    return pd.DataFrame(rows).sort_values("entry_ts").reset_index(drop=True)


def main():
    dh = load_dhan_trades()
    bt = pd.read_csv(R / "trades_jan25_overnight_sl15.csv", parse_dates=["entry_ts", "exit_ts"])
    bt["date"] = pd.to_datetime(bt["date"]).dt.date

    # ---- overlap window: Dhan live period ----
    bt_l = bt[bt["entry_ts"] >= LIVE_START].copy()
    print(f"Dhan live trades: {len(dh)}  ({dh['entry_ts'].min():%Y-%m-%d} -> {dh['entry_ts'].max():%Y-%m-%d})")
    print(f"Backtest trades in same window: {len(bt_l)}")

    # ---- trade-by-trade matching on entry date ----
    m = pd.merge(dh, bt_l, on="date", suffixes=("_dh", "_bt"))
    m["same_type"] = m["type_dh"] == m["type_bt"]
    m["same_short"] = m["short_strike_dh"] == m["short_strike_bt"]
    m["entry_diff_min"] = (m["entry_ts_bt"] - m["entry_ts_dh"]).dt.total_seconds().abs() / 60
    both = len(m)
    print(f"\ndays where both traded : {both}")
    print(f"  same direction       : {m['same_type'].sum()}  ({m['same_type'].mean()*100:.0f}%)")
    print(f"  same short strike    : {m['same_short'].sum()}  ({m['same_short'].mean()*100:.0f}%)")
    print(f"  median entry-time gap: {m['entry_diff_min'].median():.0f} min")
    pnl_corr = m[["pnl_per_lot", "net_pnl"]].corr().iloc[0, 1]
    print(f"  per-trade P&L corr   : {pnl_corr:.2f}")
    dh_only = set(dh["date"]) - set(bt_l["date"])
    bt_only = set(bt_l["date"]) - set(dh["date"])
    print(f"  Dhan-only days: {len(dh_only)}, backtest-only days: {len(bt_only)}")

    # ---- monthly per-lot comparison ----
    dh_m = dh.set_index("entry_ts")["pnl_per_lot"].resample("ME").sum()
    bt_m = bt_l.set_index("entry_ts")["net_pnl"].resample("ME").sum()
    qs = json.load(open(R / "dhan_GetQuantStats.json"))["data"]["algoStats"]
    heat = qs["plotMonthlyHeatmap"]
    order = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun"]
    print("\nmonth      dhan Rs/lot   backtest Rs/lot   dhan(pub %)")
    comp = pd.DataFrame({"dhan": dh_m, "backtest": bt_m}).fillna(0.0)
    for ts, row in comp.iterrows():
        pub = heat.get(f"{ts.strftime('%b')}-00", np.nan)
        print(f"{ts.strftime('%Y-%m')}   {row['dhan']:>11,.0f}   {row['backtest']:>15,.0f}   {pub:>9.1f}%")
    print(f"TOTAL      {comp['dhan'].sum():>11,.0f}   {comp['backtest'].sum():>15,.0f}")
    mcorr = comp.corr().iloc[0, 1]
    print(f"monthly P&L correlation: {mcorr:.2f}")

    # ---- headline stats ----
    dwin = (dh["pnl"] > 0).mean() * 100
    bwin = (bt_l["net_pnl"] > 0).mean() * 100
    print(f"\nwin rate   : dhan {dwin:.1f}% (site claims {qs.get('winRatio', qs.get('hitRatio', 'n/a'))})  vs backtest {bwin:.1f}%")
    print(f"total/lot  : dhan Rs {dh['pnl_per_lot'].sum():,.0f}  vs backtest Rs {bt_l['net_pnl'].sum():,.0f}")
    print(f"trades     : dhan {len(dh)}  vs backtest {len(bt_l)}")
    print(f"max DD (pub): {qs['maxDrawdown']:.1f}%  ({qs['maxDrawdownStart'][:10]} -> {qs['maxDrawdownEnd'][:10]})")

    m.to_csv(R / "dhan_vs_backtest_by_trade.csv", index=False)
    comp.to_csv(R / "dhan_vs_backtest_monthly.csv")
    print("\nsaved dhan_vs_backtest_by_trade.csv / dhan_vs_backtest_monthly.csv")


if __name__ == "__main__":
    main()
