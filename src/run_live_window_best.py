"""Full overnight backtest over Dhan's live window with the windows that
best match their live entries (vol_ratio_window=15, opt_vol_window=240),
SL 15% of margin - then monthly comparison against their live P&L.
"""
import json

import pandas as pd

import config
config.BACKTEST_START = "2025-07-09"
config.BACKTEST_END = "2026-07-16"
config.SL_PCT_OF_MARGIN = 0.15
config.VOL_RATIO_WINDOW = 15
config.OPT_VOL_WINDOW = 240

import signals
import backtest_overnight
from compare_dhan import load_dhan_trades

_orig_load = signals.load_spot


def _trunc():
    df = _orig_load()
    return df[df["ts"] >= pd.Timestamp("2025-06-20")].reset_index(drop=True)


signals.load_spot = _trunc

if __name__ == "__main__":
    tr = backtest_overnight.run(tag="live_window_best")
    dh = load_dhan_trades()
    dh_m = dh.set_index("entry_ts")["pnl_per_lot"].resample("ME").sum()
    tr["entry_ts"] = pd.to_datetime(tr["entry_ts"])
    bt_m = tr.set_index("entry_ts")["net_pnl"].resample("ME").sum()
    comp = pd.DataFrame({"dhan_per_lot": dh_m, "backtest": bt_m}).fillna(0.0)
    print("\nmonth      dhan Rs/lot   backtest Rs/lot")
    for ts, r in comp.iterrows():
        print(f"{ts.strftime('%Y-%m')}   {r['dhan_per_lot']:>11,.0f}   {r['backtest']:>15,.0f}")
    print(f"TOTAL      {comp['dhan_per_lot'].sum():>11,.0f}   {comp['backtest'].sum():>15,.0f}")
    print(f"monthly correlation: {comp.corr().iloc[0,1]:.2f}")
    print(f"win rates: dhan {(dh['pnl']>0).mean()*100:.1f}%  backtest {(tr['net_pnl']>0).mean()*100:.1f}%")
