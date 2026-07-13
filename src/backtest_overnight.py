"""Overnight variant of the credit-spread backtester (Zen Credit Spread
Overnight style): a position is held until 15:15 on the NEXT trading day
instead of being squared off the same day. Exit date is capped at the
expiry day (entries on expiry day itself close the same day). Stop-loss is
monitored on every minute bar, including the first bars after the
overnight gap; an SL that triggers on the day's last bar fills at the next
morning's open, as it would live.

Writes results/trades_overnight.csv. Entry logic, signals, fills, SL sizing
and charges are identical to backtest.py.
"""
import numpy as np
import pandas as pd

import backtest
import config
import signals
import upstox_api


def run():
    spot = signals.load_spot()
    expiries = [e for e in upstox_api.expired_expiries() if e <= config.BACKTEST_END]
    emap = signals.expiry_map(spot["date"], expiries)
    store = signals.OptionStore(pd.DatetimeIndex(spot["ts"]))

    print("computing signals...", flush=True)
    df = signals.compute_signals(spot, emap, store)
    df = df[(df["date"] >= pd.Timestamp(config.BACKTEST_START).date()) &
            (df["date"] <= pd.Timestamp(config.BACKTEST_END).date())].reset_index(drop=True)

    long_cond = (df["alpha"] > config.LONG_TH) & (df["alpha2"] > config.LONG_TH)
    short_cond = (df["alpha"] < config.SHORT_TH) & (df["alpha2"] < config.SHORT_TH)
    df["sig"] = np.where(long_cond & ~long_cond.shift(fill_value=False), 1,
                np.where(short_cond & ~short_cond.shift(fill_value=False), -1, 0))

    t_entry_start = pd.Timestamp(config.ENTRY_START).time()
    t_entry_end = pd.Timestamp(config.ENTRY_END).time()
    t_square = pd.Timestamp(config.SQUARE_OFF).time()

    days = sorted(df["date"].unique())
    next_day = {d: days[i + 1] for i, d in enumerate(days[:-1])}

    ts = pd.DatetimeIndex(df["ts"])
    times = np.array(ts.time)
    dates = df["date"].values
    sigv = df["sig"].values
    atmv = df["atm"].values
    closev = df["close"].values
    alphav = df["alpha"].values
    alpha2v = df["alpha2"].values

    trades = []
    pos = None
    print("simulating...", flush=True)
    for i in range(len(df) - 1):
        t_next = ts[i + 1]

        if pos is not None:
            sc = pos["short_df"]["close"].get(ts[i], np.nan)
            lc = pos["long_df"]["close"].get(ts[i], np.nan)
            hit_sl = False
            if np.isfinite(sc) and np.isfinite(lc):
                mtm = (pos["credit"] - (sc - lc)) * pos["lot"]
                hit_sl = mtm <= -pos["sl_amount"]
            time_exit = dates[i] >= pos["exit_date"] and times[i] >= t_square
            if hit_sl or time_exit:
                sb = backtest.leg_fill(pos["short_df"], t_next, "buy")
                lb = backtest.leg_fill(pos["long_df"], t_next, "sell")
                if sb is None or lb is None:   # no next-minute data: use MTM closes
                    sb, lb = sc + config.SLIPPAGE, max(lc - config.SLIPPAGE, 0.05)
                gross = (pos["credit"] - (sb - lb)) * pos["lot"]
                cost = backtest.charges(
                    buy_turnover=(pos["long_entry"] + sb) * pos["lot"],
                    sell_turnover=(pos["short_entry"] + lb) * pos["lot"],
                    n_orders=4)
                trades.append({**pos["info"], "exit_ts": t_next,
                               "exit_reason": "SL" if hit_sl else "TIME",
                               "exit_cost_to_close": sb - lb,
                               "gross_pnl": gross, "charges": cost,
                               "net_pnl": gross - cost})
                pos = None
            continue

        if sigv[i] == 0 or not (t_entry_start <= times[i] <= t_entry_end):
            continue
        d = dates[i]
        expiry = emap.get(d)
        if expiry is None or str(expiry) > config.BACKTEST_END:
            continue
        data = store.expiry_data(str(expiry))
        if not data:
            continue
        lot = store.lot_size(str(expiry))
        atm = int(atmv[i])
        if sigv[i] == 1:
            s_key, l_key = (atm, "PE"), (atm - config.WING_POINTS, "PE")
            kind = "bull_put"
        else:
            s_key, l_key = (atm, "CE"), (atm + config.WING_POINTS, "CE")
            kind = "bear_call"
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
               "sl_amount": config.SL_PCT_OF_MARGIN * margin,
               "exit_date": exit_date,
               "info": {"date": d, "expiry": str(expiry), "type": kind,
                        "entry_ts": t_next, "atm": atm, "lot": lot,
                        "short_strike": s_key[0], "long_strike": l_key[0],
                        "credit": credit, "margin": margin,
                        "alpha": alphav[i], "alpha2": alpha2v[i],
                        "spot_entry": closev[i]}}

    # safety: close anything still open at the very last bar
    if pos is not None:
        t_last = ts[-1]
        sc = pos["short_df"]["close"].get(t_last, np.nan)
        lc = pos["long_df"]["close"].get(t_last, np.nan)
        gross = (pos["credit"] - (sc - lc)) * pos["lot"]
        cost = backtest.charges((pos["long_entry"] + sc) * pos["lot"],
                                (pos["short_entry"] + lc) * pos["lot"], 4)
        trades.append({**pos["info"], "exit_ts": t_last, "exit_reason": "LAST",
                       "exit_cost_to_close": sc - lc,
                       "gross_pnl": gross, "charges": cost, "net_pnl": gross - cost})

    tr = pd.DataFrame(trades)
    tr.to_csv(config.RESULTS_DIR / "trades_overnight.csv", index=False)
    backtest.report(tr, df)
    return tr


if __name__ == "__main__":
    run()
