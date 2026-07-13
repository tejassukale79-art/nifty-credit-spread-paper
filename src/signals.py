"""Signal engine: alpha (spot mean-reversion rank) and alpha2 (volume/volatility
scaled rank built from the chained ATM option series).

Definitions implemented (spec ambiguities resolved as noted):
  ret5   = (close_t - close_{t-5}) / day_open        (trailing, within-day)
  alpha  = ts_rank(ret5, 800)
  vr     = mean(vol_CE / SMA60(vol_CE), vol_PE / SMA60(vol_PE))   [chained ATM]
  sigma  = std30(ret1_CE) + std30(ret1_PE)                        [chained ATM]
  alpha2 = ts_rank(ret5 * vr / sigma, 300)
ts_rank = fraction of the trailing-window values <= current value.
"""
import json
from functools import lru_cache

import numpy as np
import pandas as pd

import config


def ts_rank(arr, window):
    """Percentile rank of each value within its trailing window (NaN-aware)."""
    a = np.asarray(arr, dtype=float)
    n = len(a)
    out = np.full(n, np.nan)
    if n < window:
        return out
    sw = np.lib.stride_tricks.sliding_window_view(a, window)  # rows end at t=w-1..n-1
    cur = sw[:, -1]
    le = np.nansum((sw <= cur[:, None]), axis=1).astype(float)
    valid = np.isfinite(sw).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = le / valid
    r[~np.isfinite(cur)] = np.nan
    r[valid < window * 0.5] = np.nan          # demand at least half a window of history
    out[window - 1:] = r
    return out


def load_spot():
    df = pd.read_parquet(config.SPOT_DIR / "nifty_1min.parquet")
    df["date"] = df["ts"].dt.date
    df["day_open"] = df.groupby("date")["open"].transform("first")
    prev5 = df.groupby("date")["close"].shift(config.RET_MINUTES)
    df["ret5"] = (df["close"] - prev5) / df["day_open"]
    return df


def expiry_map(dates, expiries):
    """Map each trading date to its nearest weekly expiry."""
    exp = sorted(pd.Timestamp(e).date() for e in expiries)
    out = {}
    for d in sorted(set(dates)):
        nxt = [e for e in exp if e >= d]
        out[d] = nxt[0] if nxt else None
    return out


class OptionStore:
    """Loads option contract candles per expiry, reindexed to the spot minute grid."""

    def __init__(self, spot_index):
        self.spot_index = spot_index

    @lru_cache(maxsize=4)
    def expiry_data(self, expiry):
        edir = config.OPT_DIR / str(expiry)
        data = {}
        if not edir.exists():
            return data
        for f in edir.glob("*.parquet"):
            kind = f.stem[-2:]
            strike = int(f.stem[:-2])
            raw = pd.read_parquet(f)
            if raw.empty:
                continue
            raw = raw.set_index("ts")
            idx = self.spot_index[(self.spot_index >= raw.index.min()) &
                                  (self.spot_index <= raw.index.max())]
            px = raw["close"].reindex(idx).ffill()
            vol = raw["volume"].reindex(idx).fillna(0.0)
            opn = raw["open"].reindex(idx)
            ret = px.pct_change()
            data[(strike, kind)] = pd.DataFrame(
                {"close": px, "open": opn, "volume": vol, "ret": ret})
        return data

    def lot_size(self, expiry):
        meta = json.loads((config.OPT_DIR / str(expiry) / "meta.json").read_text())
        return int(meta["lot_size"])


def build_alpha2_inputs(spot, emap, store):
    """Chained ATM CE/PE volume and 1-min return series over the whole timeline."""
    n = len(spot)
    vol_ce = np.full(n, np.nan); vol_pe = np.full(n, np.nan)
    ret_ce = np.full(n, np.nan); ret_pe = np.full(n, np.nan)
    atm_arr = (np.round(spot["close"].values / config.STRIKE_STEP)
               * config.STRIKE_STEP).astype(int)

    ts_vals = spot["ts"].values
    dates = spot["date"].values
    for d, grp in spot.groupby("date", sort=True):
        expiry = emap.get(d)
        if expiry is None:
            continue
        data = store.expiry_data(str(expiry))
        if not data:
            continue
        pos = grp.index.values
        for strike in np.unique(atm_arr[pos]):
            for kind, vol_a, ret_a in (("CE", vol_ce, ret_ce), ("PE", vol_pe, ret_pe)):
                dfc = data.get((strike, kind))
                if dfc is None:
                    continue
                sel = pos[atm_arr[pos] == strike]
                sub = dfc.reindex(spot.loc[sel, "ts"])
                vol_a[sel] = sub["volume"].values
                ret_a[sel] = sub["ret"].values
    return atm_arr, vol_ce, vol_pe, ret_ce, ret_pe


def compute_signals(spot, emap, store):
    spot = spot.reset_index(drop=True)
    spot["alpha"] = ts_rank(spot["ret5"].values, config.ALPHA_LOOKBACK)

    atm, vol_ce, vol_pe, ret_ce, ret_pe = build_alpha2_inputs(spot, emap, store)
    w = config.VOL_RATIO_WINDOW
    vr_ce = vol_ce / pd.Series(vol_ce).rolling(w, min_periods=w // 2).mean().values
    vr_pe = vol_pe / pd.Series(vol_pe).rolling(w, min_periods=w // 2).mean().values
    vr = np.nanmean(np.vstack([vr_ce, vr_pe]), axis=0)

    v = config.OPT_VOL_WINDOW
    sig_ce = pd.Series(ret_ce).rolling(v, min_periods=v // 2).std().values
    sig_pe = pd.Series(ret_pe).rolling(v, min_periods=v // 2).std().values
    sigma = sig_ce + sig_pe

    with np.errstate(invalid="ignore", divide="ignore"):
        raw2 = spot["ret5"].values * vr / sigma
    raw2[~np.isfinite(raw2)] = np.nan
    spot["alpha2"] = ts_rank(raw2, config.ALPHA2_LOOKBACK)
    spot["atm"] = atm
    return spot
