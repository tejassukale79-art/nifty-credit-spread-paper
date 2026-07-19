"""Sweep round 2: score parameter combos by REALIZED trade sequences.

Occupancy simulation (price-free approximation of the overnight variant):
enter on the first edge-triggered signal while flat inside 10:15-14:15,
then stay busy until 15:15 of the next trading day (same day if entry is on
expiry-week Tuesday... approximated as next trading day). SL re-entries are
not simulated - so our realized count is a floor, Dhan's includes re-entries.

Scores vs Dhan live log:
  entries   = realized entry count (target ~174, incl. their re-entries)
  dayhit%   = of Dhan first-entries-of-day, we entered the same day
  dirhit%   = ...and our first entry that day had the same direction
  strike%   = ...and the same short strike (ATM agreement)
"""
import numpy as np
import pandas as pd

import config
config.BACKTEST_START = "2025-07-01"
config.BACKTEST_END = "2026-07-16"

import signals
from compare_dhan import load_dhan_trades
from sweep_dhan_match import CACHE, alpha2_for


def realized_entries(z, alpha2):
    ts = pd.DatetimeIndex(z["ts"])
    alpha, atm = z["alpha"], z["atm"]
    lc = (alpha > config.LONG_TH) & (alpha2 > config.LONG_TH)
    sc = (alpha < config.SHORT_TH) & (alpha2 < config.SHORT_TH)
    sig = np.where(lc & ~np.roll(lc, 1), 1, np.where(sc & ~np.roll(sc, 1), -1, 0))
    sig[0] = 0
    tmin = pd.Timestamp(config.ENTRY_START).time()
    tmax = pd.Timestamp(config.ENTRY_END).time()
    t_sq = pd.Timestamp(config.SQUARE_OFF).time()
    dates = np.array([t.date() for t in ts])
    days = sorted(set(dates))
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    times = np.array(ts.time)
    live0 = pd.Timestamp("2025-07-09").date()

    out = []
    busy_until = None   # (date, time)
    for i in range(len(ts)):
        if busy_until is not None:
            d, t = dates[i], times[i]
            if (d > busy_until[0]) or (d == busy_until[0] and t >= busy_until[1]):
                busy_until = None
            else:
                continue
        if sig[i] == 0 or not (tmin <= times[i] <= tmax) or dates[i] < live0:
            continue
        out.append({"ts": ts[i], "date": dates[i], "dir": sig[i],
                    "atm": int(round(atm[i] / 50) * 50)})
        busy_until = (nxt.get(dates[i], dates[i]), t_sq)
    return pd.DataFrame(out)


def main():
    z = dict(np.load(CACHE))
    dh = load_dhan_trades()
    dh = dh[dh["type"].isin(["bull_put", "bear_call"])].copy()
    dh_first = dh.sort_values("entry_ts").drop_duplicates("date")
    print(f"Dhan: {len(dh)} entries on {dh['date'].nunique()} days\n")
    print("vrw  ovw  entries  dayhit%  dirhit%  strike%  timegap(med min)")
    rows = []
    for vrw in (15, 30, 60, 120, 240, 375):
        for ovw in (15, 30, 60, 120, 240):
            a2 = alpha2_for(z, vrw, ovw)
            ours = realized_entries(z, a2)
            if ours.empty:
                continue
            of = ours.drop_duplicates("date").set_index("date")
            day = dir_ = strike = 0
            gaps = []
            for _, r in dh_first.iterrows():
                o = of.loc[r["date"]] if r["date"] in of.index else None
                if o is None:
                    continue
                day += 1
                want = 1 if r["type"] == "bull_put" else -1
                if o["dir"] == want:
                    dir_ += 1
                    gaps.append(abs((o["ts"] - r["entry_ts"]).total_seconds()) / 60)
                    if o["atm"] == r["short_strike"]:
                        strike += 1
            n = len(dh_first)
            rows.append((vrw, ovw, len(ours), day / n * 100, dir_ / n * 100,
                         strike / n * 100, np.median(gaps) if gaps else np.nan))
            print(f"{vrw:>3}  {ovw:>3}  {len(ours):>6}   {day/n*100:5.1f}    "
                  f"{dir_/n*100:5.1f}    {strike/n*100:5.1f}      "
                  f"{np.median(gaps) if gaps else float('nan'):.0f}", flush=True)
    best = max(rows, key=lambda r: r[4])
    print(f"\nbest by dirhit: vrw={best[0]} ovw={best[1]} "
          f"(entries {best[2]}, dir {best[4]:.1f}%, strike {best[5]:.1f}%)")


if __name__ == "__main__":
    main()
