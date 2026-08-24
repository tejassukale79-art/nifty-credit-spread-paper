"""Re-test the credit spread against the rules Dhan's own trade log implies.

Three candidate corrections were read off 174 live Dhan trades. Only one of
them survived being tested over Dhan's own window (2025-07-09 .. 2026-07-16):

  CONFIRMED  There is no stop-loss. All 348 legs carry stopLoss: 0, and the
             replica's invented 15%-of-margin stop costs Rs 67,407 per lot
             over that window - Rs 34,615 with it, Rs 102,022 without.

  REJECTED   "Entries cluster in the first 15 minutes." 67% of Dhan's entries
             land by 10:30, which looked like a tight window, but narrowing
             the replica to 10:15-10:30 made it WORSE (Rs 30,691). The wide
             10:15-14:15 window in the spec is right; entries are merely
             front-loaded.

  REJECTED   "The time exit is 15:00, not 15:15." Moving it earlier did not
             help either (Rs 86,372 vs Rs 102,022). Dhan's 15:00 exits sit
             alongside same-day signal exits this engine cannot reproduce.

A gap to Dhan's Rs 156,712 per lot remains, and it is a signal-reconstruction
gap: the replica agrees with Dhan on direction on only 70% of shared days.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest
import config
import run_2year as R

pd.set_option("display.width", 250)


def simulate(df, emap, store, sl_pct, entry_end, square_off):
    long_cond = (df["alpha"] > config.LONG_TH) & (df["alpha2"] > config.LONG_TH)
    short_cond = (df["alpha"] < config.SHORT_TH) & (df["alpha2"] < config.SHORT_TH)
    sig = np.where(long_cond & ~long_cond.shift(fill_value=False), 1,
          np.where(short_cond & ~short_cond.shift(fill_value=False), -1, 0))

    t0 = pd.Timestamp(config.ENTRY_START).time()
    t1 = pd.Timestamp(entry_end).time()
    tsq = pd.Timestamp(square_off).time()

    days = sorted(df["date"].unique())
    next_day = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    ts = pd.DatetimeIndex(df["ts"])
    times = np.array(ts.time)
    dates = df["date"].values
    atmv, closev = df["atm"].values, df["close"].values

    trades, pos = [], None
    for i in range(len(df) - 1):
        t_next = ts[i + 1]
        if pos is not None:
            sc = pos["short_df"]["close"].get(ts[i], np.nan)
            lc = pos["long_df"]["close"].get(ts[i], np.nan)
            hit = False
            if sl_pct is not None and np.isfinite(sc) and np.isfinite(lc):
                hit = (pos["credit"] - (sc - lc)) * pos["lot"] <= -pos["sl"]
            timeout = dates[i] >= pos["exit_date"] and times[i] >= tsq
            if hit or timeout:
                sb = backtest.leg_fill(pos["short_df"], t_next, "buy")
                lb = backtest.leg_fill(pos["long_df"], t_next, "sell")
                if sb is None or lb is None:
                    sb, lb = sc + config.SLIPPAGE, max(lc - config.SLIPPAGE, 0.05)
                gross = (pos["credit"] - (sb - lb)) * pos["lot"]
                cost = backtest.charges((pos["long_entry"] + sb) * pos["lot"],
                                        (pos["short_entry"] + lb) * pos["lot"], 4)
                trades.append({**pos["info"], "exit_ts": t_next,
                               "exit_reason": "SL" if hit else "TIME",
                               "gross_pnl": gross, "charges": cost, "net_pnl": gross - cost})
                pos = None
            continue

        if sig[i] == 0 or not (t0 <= times[i] <= t1):
            continue
        d = dates[i]
        expiry = emap.get(d)
        if expiry is None:
            continue
        data = store.expiry_data(str(expiry))
        if not data:
            continue
        lot = store.lot_size(str(expiry))
        atm = int(atmv[i])
        if sig[i] == 1:
            sk, lk, kind = (atm, "PE"), (atm - config.WING_POINTS, "PE"), "bull_put"
        else:
            sk, lk, kind = (atm, "CE"), (atm + config.WING_POINTS, "CE"), "bear_call"
        sdf, ldf = data.get(sk), data.get(lk)
        sf = backtest.leg_fill(sdf, t_next, "sell")
        lf = backtest.leg_fill(ldf, t_next, "buy")
        if sf is None or lf is None:
            continue
        credit = sf - lf
        if credit <= 0:
            continue
        margin = (config.WING_POINTS - credit) * lot
        exit_date = min(next_day.get(d, d), expiry) if d != expiry else d
        pos = {"short_df": sdf, "long_df": ldf, "credit": credit, "lot": lot,
               "short_entry": sf, "long_entry": lf, "sl": (sl_pct or 0) * margin,
               "exit_date": exit_date,
               "info": {"date": d, "expiry": str(expiry), "type": kind,
                        "entry_ts": t_next, "atm": atm, "lot": lot,
                        "short_strike": sk[0], "long_strike": lk[0],
                        "credit": credit, "margin": margin, "spot_entry": closev[i]}}
    return pd.DataFrame(trades)


def build_cached():
    """compute_signals is the slow part; keep it on disk so rule variants are cheap."""
    import signals as SG
    import upstox_api
    cache = config.RESULTS_DIR / "signals_2y.parquet"
    spot = SG.load_spot()
    local = [d.name for d in config.OPT_DIR.iterdir() if (d / "meta.json").exists()]
    expiries = sorted(set(upstox_api.expired_expiries()) | set(local))
    emap = SG.expiry_map(spot["date"], expiries)
    store = SG.OptionStore(pd.DatetimeIndex(spot["ts"]))
    if cache.exists():
        print("using cached signals", flush=True)
        return pd.read_parquet(cache), emap, store
    df, emap, store = R.build()
    df.to_parquet(cache, index=False)
    return df, emap, store


if __name__ == "__main__":
    df, emap, store = build_cached()
    DH_LO, DH_HI = "2025-07-09", "2026-07-16"

    variants = [
        ("replica as written      (10:15-14:15, SL 15%, 15:15)", 0.15, "14:15", "15:15"),
        ("no stop only            (10:15-14:15, no SL, 15:15)",  None, "14:15", "15:15"),
        ("Dhan entry window only  (10:15-10:30, SL 15%, 15:15)", 0.15, "10:30", "15:15"),
        ("Dhan rules              (10:15-10:30, no SL, 15:00)",  None, "10:30", "15:00"),
        ("Dhan rules, wider entry (10:15-11:00, no SL, 15:00)",  None, "11:00", "15:00"),
    ]
    out = []
    for tag, sl, ee, sq in variants:
        tr = simulate(df, emap, store, sl, ee, sq)
        if tr.empty:
            print("  NO TRADES:", tag, flush=True); continue
        tr["date"] = tr["date"].astype(str)          # date objects break the window filter
        tr.to_csv(config.RESULTS_DIR / ("trades_rv_%s.csv" % tag.split()[0].lower()), index=False)
        for window, lo, hi in (("FULL 2y", "2000-01-01", "2100-01-01"),
                               ("Dhan window", DH_LO, DH_HI)):
            s = tr[(tr["date"] >= lo) & (tr["date"] <= hi)]
            if s.empty:
                continue
            n = s["net_pnl"]
            eq = n.cumsum()
            w = n[n > 0]
            out.append({"variant": tag, "window": window, "trades": len(s),
                        "win%": round(100 * len(w) / len(s), 1),
                        "net_1lot": int(n.sum()), "avg": int(n.mean()),
                        "PF": round(w.sum() / -n[n <= 0].sum(), 2) if (n <= 0).any() else np.inf,
                        "maxDD": int((eq.cummax() - eq).max()),
                        "t": round(n.mean() / (n.std(ddof=1) / np.sqrt(len(n))), 2)})
        print("  ran:", tag, flush=True)

    res = pd.DataFrame(out)
    print("\n" + "=" * 108)
    print("WHICH RULE SET? - 1 lot, charges + slippage included")
    print("=" * 108)
    print(res.to_string(index=False))
    print("\nDhan's own live record over 2025-07-09 .. 2026-07-16:")
    print("   174 trades, 56.9% win, per-lot net Rs 156,712, mean Rs 901/trade")
    res.to_csv(config.RESULTS_DIR / "rule_variants.csv", index=False)
