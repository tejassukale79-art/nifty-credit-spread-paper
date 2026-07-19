"""Final verified-replica backtests: signal windows 15/240, SL 15% of margin,
time exit 15:00 next trading day. Two windows:
  A) Dhan live window Jul-09-2025 -> Jul-16-2026 (direct comparison)
  B) Jan-01-2025 -> Jul-16-2026 (the user's requested window)
"""
import pandas as pd

import config
config.SL_PCT_OF_MARGIN = 0.15
config.SQUARE_OFF = "15:00"          # verified from Dhan exit clocks
config.VOL_RATIO_WINDOW = 15
config.OPT_VOL_WINDOW = 240

import signals
import backtest_overnight
from compare_dhan import load_dhan_trades

_orig_load = signals.load_spot


def trunc(start):
    def f():
        df = _orig_load()
        return df[df["ts"] >= pd.Timestamp(start)].reset_index(drop=True)
    return f


def compare(tr):
    dh = load_dhan_trades()
    dh_m = dh.set_index("entry_ts")["pnl_per_lot"].resample("ME").sum()
    tr["entry_ts"] = pd.to_datetime(tr["entry_ts"])
    bt = tr[tr["entry_ts"] >= pd.Timestamp("2025-07-09")]
    bt_m = bt.set_index("entry_ts")["net_pnl"].resample("ME").sum()
    comp = pd.DataFrame({"dhan_per_lot": dh_m, "backtest": bt_m}).fillna(0.0)
    print("\nmonth      dhan Rs/lot   replica Rs/lot")
    for ts, r in comp.iterrows():
        print(f"{ts.strftime('%Y-%m')}   {r['dhan_per_lot']:>11,.0f}   {r['backtest']:>14,.0f}")
    print(f"TOTAL      {comp['dhan_per_lot'].sum():>11,.0f}   {comp['backtest'].sum():>14,.0f}")
    print(f"monthly correlation: {comp.corr().iloc[0, 1]:.2f}")


if __name__ == "__main__":
    print("=== A) Dhan live window ===")
    config.BACKTEST_START = "2025-07-09"
    config.BACKTEST_END = "2026-07-16"
    signals.load_spot = trunc("2025-06-20")
    tr = backtest_overnight.run(tag="replica_final_live")
    compare(tr)

    print("\n=== B) Jan 2025 -> date ===")
    config.BACKTEST_START = "2025-01-01"
    config.BACKTEST_END = "2026-07-16"
    signals.load_spot = trunc("2024-12-15")
    backtest_overnight.run(tag="replica_final_jan25")
