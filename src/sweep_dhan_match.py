"""Reverse-engineer the ambiguous signal parameters by scoring every
combination against Dhan's live trade log (174 real entries).

Phase 1 (slow, once): build the parameter-independent inputs over the live
window - spot alpha, chained ATM option volume/return series. Cached to npz.

Phase 2 (fast, per combo): alpha2 for each (vol_ratio_window, opt_vol_window),
edge-triggered signals, then:
  recall    = % of Dhan entries with a same-direction signal within +/-5 min
  precision = % of our signals that line up with a Dhan entry
"""
import json
import sys

import numpy as np
import pandas as pd

import config
config.BACKTEST_START = "2025-07-01"
config.BACKTEST_END = "2026-07-16"

import signals
import upstox_api
from compare_dhan import load_dhan_trades

CACHE = config.RESULTS_DIR / "alpha2_inputs_live.npz"
WARMUP_START = "2025-06-20"
TOL = pd.Timedelta(minutes=5)


def precompute():
    spot = signals.load_spot()
    spot = spot[spot["ts"] >= pd.Timestamp(WARMUP_START)].reset_index(drop=True)
    local = [d.name for d in config.OPT_DIR.iterdir() if (d / "meta.json").exists()]
    expiries = sorted(set(upstox_api.expired_expiries()) | set(local))
    emap = signals.expiry_map(spot["date"], expiries)
    store = signals.OptionStore(pd.DatetimeIndex(spot["ts"]))
    alpha = signals.ts_rank(spot["ret5"].values, config.ALPHA_LOOKBACK)
    print("building alpha2 inputs (one-time)...", flush=True)
    atm, vol_ce, vol_pe, ret_ce, ret_pe = signals.build_alpha2_inputs(spot, emap, store)
    np.savez(CACHE, ts=spot["ts"].values.astype("datetime64[ns]"),
             ret5=spot["ret5"].values, alpha=alpha, atm=atm,
             vol_ce=vol_ce, vol_pe=vol_pe, ret_ce=ret_ce, ret_pe=ret_pe)
    print(f"cached -> {CACHE}")


def alpha2_for(z, vrw, ovw):
    vr_ce = z["vol_ce"] / pd.Series(z["vol_ce"]).rolling(vrw, min_periods=vrw // 2).mean().values
    vr_pe = z["vol_pe"] / pd.Series(z["vol_pe"]).rolling(vrw, min_periods=vrw // 2).mean().values
    vr = np.nanmean(np.vstack([vr_ce, vr_pe]), axis=0)
    sig_ce = pd.Series(z["ret_ce"]).rolling(ovw, min_periods=ovw // 2).std().values
    sig_pe = pd.Series(z["ret_pe"]).rolling(ovw, min_periods=ovw // 2).std().values
    sigma = sig_ce + sig_pe
    with np.errstate(invalid="ignore", divide="ignore"):
        raw2 = z["ret5"] * vr / sigma
    raw2[~np.isfinite(raw2)] = np.nan
    return signals.ts_rank(raw2, config.ALPHA2_LOOKBACK)


def score(z, alpha2, dh):
    ts = pd.DatetimeIndex(z["ts"])
    alpha = z["alpha"]
    lc = (alpha > config.LONG_TH) & (alpha2 > config.LONG_TH)
    sc = (alpha < config.SHORT_TH) & (alpha2 < config.SHORT_TH)
    sig = np.where(lc & ~np.roll(lc, 1), 1, np.where(sc & ~np.roll(sc, 1), -1, 0))
    sig[0] = 0
    tmin = pd.Timestamp(config.ENTRY_START).time()
    tmax = pd.Timestamp(config.ENTRY_END).time()
    in_win = np.array([(tmin <= t <= tmax) for t in ts.time])
    live = ts >= pd.Timestamp("2025-07-09")
    ours = pd.DataFrame({"ts": ts[(sig != 0) & in_win & live],
                         "dir": sig[(sig != 0) & in_win & live]})
    hit = 0
    used = set()
    for _, r in dh.iterrows():
        want = 1 if r["type"] == "bull_put" else -1
        cand = ours[(ours["dir"] == want) & (abs(ours["ts"] - r["entry_ts"]) <= TOL)]
        cand = cand[~cand["ts"].isin(used)]
        if len(cand):
            hit += 1
            used.add(cand["ts"].iloc[0])
    recall = hit / len(dh) * 100
    precision = len(used) / max(1, len(ours)) * 100
    return recall, precision, len(ours)


def main():
    if not CACHE.exists() or "--rebuild" in sys.argv:
        precompute()
    z = dict(np.load(CACHE))
    dh = load_dhan_trades()
    dh = dh[dh["type"].isin(["bull_put", "bear_call"])]
    print(f"target: {len(dh)} Dhan live entries\n")
    print("vrw  ovw   recall%  precision%  our_signals")
    results = []
    for vrw in (15, 30, 60, 120, 240, 375):
        for ovw in (15, 30, 60, 120, 240):
            a2 = alpha2_for(z, vrw, ovw)
            rec, prec, n = score(z, a2, dh)
            results.append((vrw, ovw, rec, prec, n))
            print(f"{vrw:>3}  {ovw:>3}   {rec:6.1f}   {prec:6.1f}      {n}", flush=True)
    best = max(results, key=lambda r: r[2])
    print(f"\nbest: vol_ratio_window={best[0]} opt_vol_window={best[1]} "
          f"recall {best[2]:.1f}% precision {best[3]:.1f}%")


if __name__ == "__main__":
    main()
