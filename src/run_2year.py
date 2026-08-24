"""Two-year backtest of the overnight credit spread, plus a stop-loss sweep.

The live paper trader runs SL = 15% of margin. The open question is whether that
stop earns its keep, so the same window is re-run across stop levels and with no
stop at all. Everything else - signals, fills, charges, slippage - is untouched.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import backtest
import config
import signals
import upstox_api

pd.set_option("display.width", 240)


def build():
    """Signals are independent of the stop, so build them once and reuse."""
    spot = signals.load_spot()
    local = [d.name for d in config.OPT_DIR.iterdir() if (d / "meta.json").exists()]
    expiries = sorted(set(upstox_api.expired_expiries()) | set(local))
    emap = signals.expiry_map(spot["date"], expiries)
    store = signals.OptionStore(pd.DatetimeIndex(spot["ts"]))
    print("computing signals ...", flush=True)
    df = signals.compute_signals(spot, emap, store)
    df = df[(df["date"] >= pd.Timestamp(config.BACKTEST_START).date()) &
            (df["date"] <= pd.Timestamp(config.BACKTEST_END).date())].reset_index(drop=True)
    return df, emap, store


def simulate(df, emap, store, sl_pct):
    """One pass of the overnight engine at a given stop level.

    sl_pct is None for 'no stop' - the position then always runs to 15:15 the
    next session, which is the control the live 15% needs to beat.
    """
    long_cond = (df["alpha"] > config.LONG_TH) & (df["alpha2"] > config.LONG_TH)
    short_cond = (df["alpha"] < config.SHORT_TH) & (df["alpha2"] < config.SHORT_TH)
    sig = np.where(long_cond & ~long_cond.shift(fill_value=False), 1,
          np.where(short_cond & ~short_cond.shift(fill_value=False), -1, 0))

    t_entry_start = pd.Timestamp(config.ENTRY_START).time()
    t_entry_end = pd.Timestamp(config.ENTRY_END).time()
    t_square = pd.Timestamp(config.SQUARE_OFF).time()

    days = sorted(df["date"].unique())
    next_day = {d: days[i + 1] for i, d in enumerate(days[:-1])}

    ts = pd.DatetimeIndex(df["ts"])
    times = np.array(ts.time)
    dates = df["date"].values
    atmv, closev = df["atm"].values, df["close"].values
    alphav, alpha2v = df["alpha"].values, df["alpha2"].values

    trades, pos = [], None
    for i in range(len(df) - 1):
        t_next = ts[i + 1]
        if pos is not None:
            sc = pos["short_df"]["close"].get(ts[i], np.nan)
            lc = pos["long_df"]["close"].get(ts[i], np.nan)
            hit_sl = False
            if sl_pct is not None and np.isfinite(sc) and np.isfinite(lc):
                mtm = (pos["credit"] - (sc - lc)) * pos["lot"]
                hit_sl = mtm <= -pos["sl_amount"]
            time_exit = dates[i] >= pos["exit_date"] and times[i] >= t_square
            if hit_sl or time_exit:
                sb = backtest.leg_fill(pos["short_df"], t_next, "buy")
                lb = backtest.leg_fill(pos["long_df"], t_next, "sell")
                if sb is None or lb is None:
                    sb, lb = sc + config.SLIPPAGE, max(lc - config.SLIPPAGE, 0.05)
                gross = (pos["credit"] - (sb - lb)) * pos["lot"]
                cost = backtest.charges(
                    buy_turnover=(pos["long_entry"] + sb) * pos["lot"],
                    sell_turnover=(pos["short_entry"] + lb) * pos["lot"], n_orders=4)
                trades.append({**pos["info"], "exit_ts": t_next,
                               "exit_reason": "SL" if hit_sl else "TIME",
                               "gross_pnl": gross, "charges": cost,
                               "net_pnl": gross - cost})
                pos = None
            continue

        if sig[i] == 0 or not (t_entry_start <= times[i] <= t_entry_end):
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
            s_key, l_key, kind = (atm, "PE"), (atm - config.WING_POINTS, "PE"), "bull_put"
        else:
            s_key, l_key, kind = (atm, "CE"), (atm + config.WING_POINTS, "CE"), "bear_call"
        sdf, ldf = data.get(s_key), data.get(l_key)
        s_fill = backtest.leg_fill(sdf, t_next, "sell")
        l_fill = backtest.leg_fill(ldf, t_next, "buy")
        if s_fill is None or l_fill is None:
            continue
        credit = s_fill - l_fill
        if credit <= 0:
            continue
        margin = (config.WING_POINTS - credit) * lot
        exit_date = min(next_day.get(d, d), expiry) if d != expiry else d
        pos = {"short_df": sdf, "long_df": ldf, "credit": credit, "lot": lot,
               "short_entry": s_fill, "long_entry": l_fill,
               "sl_amount": (sl_pct or 0) * margin, "exit_date": exit_date,
               "info": {"date": d, "expiry": str(expiry), "type": kind,
                        "entry_ts": t_next, "atm": atm, "lot": lot,
                        "short_strike": s_key[0], "long_strike": l_key[0],
                        "credit": credit, "margin": margin,
                        "alpha": alphav[i], "alpha2": alpha2v[i],
                        "spot_entry": closev[i]}}
    return pd.DataFrame(trades)


def stats(tag, tr):
    if tr.empty:
        return {"config": tag, "trades": 0}
    net = tr["net_pnl"]
    eq = net.cumsum()
    w, l = net[net > 0], net[net <= 0]
    ex = tr["exit_reason"].value_counts().to_dict()
    return {
        "config": tag, "trades": len(tr),
        "win%": round(100 * len(w) / len(tr), 1),
        "net": int(net.sum()), "avg": int(net.mean()),
        "PF": round(w.sum() / -l.sum(), 2) if len(l) and l.sum() else np.inf,
        "maxDD": int((eq.cummax() - eq).max()),
        "avg_win": int(w.mean()) if len(w) else 0,
        "avg_loss": int(l.mean()) if len(l) else 0,
        "SL_hits": ex.get("SL", 0),
        "t": round(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))), 2) if len(net) > 1 else 0,
    }


if __name__ == "__main__":
    df, emap, store = build()
    print("window: %s -> %s  (%d trading days)\n"
          % (df["date"].min(), df["date"].max(), df["date"].nunique()), flush=True)

    rows, keep = [], {}
    for tag, sl in [("no stop", None), ("SL 10%", 0.10), ("SL 15%  <- LIVE", 0.15),
                    ("SL 20%", 0.20), ("SL 25%", 0.25), ("SL 30%", 0.30),
                    ("SL 40%", 0.40), ("SL 50%", 0.50)]:
        tr = simulate(df, emap, store, sl)
        keep[tag] = tr
        rows.append(stats(tag, tr))
        print("  done %-16s trades=%-5d net=%s" % (tag, len(tr), f"{tr['net_pnl'].sum():,.0f}"),
              flush=True)

    print("\n" + "=" * 100)
    print("TWO-YEAR BACKTEST - STOP-LOSS SWEEP (1 lot, charges + slippage included)")
    print("=" * 100)
    print(pd.DataFrame(rows).to_string(index=False))

    live = keep["SL 15%  <- LIVE"]
    live.to_csv(config.RESULTS_DIR / "trades_2year_sl15.csv", index=False)
    keep["no stop"].to_csv(config.RESULTS_DIR / "trades_2year_nostop.csv", index=False)
    pd.DataFrame(rows).to_csv(config.RESULTS_DIR / "sl_sweep_2year.csv", index=False)
    print("\nsaved results/trades_2year_sl15.csv, trades_2year_nostop.csv, sl_sweep_2year.csv")
