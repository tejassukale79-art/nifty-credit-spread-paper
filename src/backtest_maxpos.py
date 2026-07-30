"""Overnight backtest allowing up to MAX_POS concurrent positions.

Identical signals, entries, SL sizing, exits and costs as backtest_overnight.py;
the ONLY change is that up to MAX_POS spreads may be held at once instead of 1.
A new signal that fires while already holding a position (which the 1-max
system ignores) opens a second spread if a slot is free. Run with MAX_POS=1
to reproduce the baseline exactly.
"""
import sys

import numpy as np
import pandas as pd

import backtest
import config
import signals
import upstox_api


def run(max_pos, tag=None, require_diff_day=False, require_opposite=False, block_samedir_sameday=False):
    """require_diff_day: a 2nd position may not be opened on the same day an
    already-open position was entered (no same-day doubling).
    require_opposite: a 2nd position must be the opposite direction to what is
    already held."""
    tag = tag or f"maxpos{max_pos}"
    spot = signals.load_spot()
    local = [d.name for d in config.OPT_DIR.iterdir() if (d / "meta.json").exists()]
    expiries = sorted(set(upstox_api.expired_expiries()) | set(local))
    emap = signals.expiry_map(spot["date"], expiries)
    store = signals.OptionStore(pd.DatetimeIndex(spot["ts"]))

    print(f"[max_pos={max_pos}] computing signals...", flush=True)
    df = signals.compute_signals(spot, emap, store)
    df = df[(df["date"] >= pd.Timestamp(config.BACKTEST_START).date()) &
            (df["date"] <= pd.Timestamp(config.BACKTEST_END).date())].reset_index(drop=True)

    long_cond = (df["alpha"] > config.LONG_TH) & (df["alpha2"] > config.LONG_TH)
    short_cond = (df["alpha"] < config.SHORT_TH) & (df["alpha2"] < config.SHORT_TH)
    df["sig"] = np.where(long_cond & ~long_cond.shift(fill_value=False), 1,
                np.where(short_cond & ~short_cond.shift(fill_value=False), -1, 0))

    t0 = pd.Timestamp(config.ENTRY_START).time()
    t1 = pd.Timestamp(config.ENTRY_END).time()
    tsq = pd.Timestamp(config.SQUARE_OFF).time()
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
    positions = []
    margin_used = np.zeros(len(df))     # concurrent margin per minute, for utilisation stats
    print(f"[max_pos={max_pos}] simulating...", flush=True)
    for i in range(len(df) - 1):
        t_next = ts[i + 1]

        # --- manage/exit open positions ---
        still = []
        for pos in positions:
            sc = pos["short_df"]["close"].get(ts[i], np.nan)
            lc = pos["long_df"]["close"].get(ts[i], np.nan)
            hit_sl = False
            if np.isfinite(sc) and np.isfinite(lc):
                mtm = (pos["credit"] - (sc - lc)) * pos["lot"]
                hit_sl = mtm <= -pos["sl_amount"]
            time_exit = dates[i] >= pos["exit_date"] and times[i] >= tsq
            if hit_sl or time_exit:
                sb = backtest.leg_fill(pos["short_df"], t_next, "buy")
                lb = backtest.leg_fill(pos["long_df"], t_next, "sell")
                if sb is None or lb is None:
                    sb, lb = sc + config.SLIPPAGE, max(lc - config.SLIPPAGE, 0.05)
                gross = (pos["credit"] - (sb - lb)) * pos["lot"]
                cost = backtest.charges((pos["long_entry"] + sb) * pos["lot"],
                                        (pos["short_entry"] + lb) * pos["lot"], 4)
                trades.append({**pos["info"], "exit_ts": t_next,
                               "exit_reason": "SL" if hit_sl else "TIME",
                               "exit_cost_to_close": sb - lb,
                               "gross_pnl": gross, "charges": cost, "net_pnl": gross - cost})
            else:
                still.append(pos)
        positions = still
        margin_used[i] = sum(p["info"]["margin"] for p in positions)

        # --- entry: a fresh signal + a free slot ---
        if len(positions) >= max_pos:
            continue
        if sigv[i] == 0 or not (t0 <= times[i] <= t1):
            continue
        d = dates[i]
        new_type = "bull_put" if sigv[i] == 1 else "bear_call"
        if positions:
            # no second position on the same day an open one was entered
            if require_diff_day and any(p["info"]["date"] == d for p in positions):
                continue
            # second position must oppose what is already held
            if require_opposite and any(p["info"]["type"] == new_type for p in positions):
                continue
            # block only same-day SAME-DIRECTION doubling (targets 29-Jul case)
            if block_samedir_sameday and any(
                    p["info"]["date"] == d and p["info"]["type"] == new_type for p in positions):
                continue
        expiry = emap.get(d)
        if expiry is None:
            continue
        data = store.expiry_data(str(expiry))
        if not data:
            continue
        lot = store.lot_size(str(expiry))
        atm = int(atmv[i])
        if sigv[i] == 1:
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
        positions.append({
            "short_df": sdf, "long_df": ldf, "credit": credit, "lot": lot,
            "short_entry": s_fill, "long_entry": l_fill,
            "sl_amount": config.SL_PCT_OF_MARGIN * margin, "exit_date": exit_date,
            "info": {"date": d, "expiry": str(expiry), "type": kind,
                     "entry_ts": t_next, "atm": atm, "lot": lot,
                     "short_strike": s_key[0], "long_strike": l_key[0],
                     "credit": credit, "margin": margin,
                     "alpha": alphav[i], "alpha2": alpha2v[i], "spot_entry": closev[i]}})

    # close leftovers at the last bar
    t_last = ts[-1]
    for pos in positions:
        sc = pos["short_df"]["close"].get(t_last, np.nan)
        lc = pos["long_df"]["close"].get(t_last, np.nan)
        gross = (pos["credit"] - (sc - lc)) * pos["lot"]
        cost = backtest.charges((pos["long_entry"] + sc) * pos["lot"],
                                (pos["short_entry"] + lc) * pos["lot"], 4)
        trades.append({**pos["info"], "exit_ts": t_last, "exit_reason": "LAST",
                       "exit_cost_to_close": sc - lc,
                       "gross_pnl": gross, "charges": cost, "net_pnl": gross - cost})

    tr = pd.DataFrame(trades)
    tr.to_csv(config.RESULTS_DIR / f"trades_{tag}.csv", index=False)
    return tr, df, margin_used


def summarize(tr, df, margin_used, label):
    tr = tr.copy()
    tr["exit_ts"] = pd.to_datetime(tr["exit_ts"])
    wins = tr[tr["net_pnl"] > 0]; losses = tr[tr["net_pnl"] <= 0]
    daily = tr.groupby(tr["exit_ts"].dt.date)["net_pnl"].sum()
    all_days = pd.Series(0.0, index=sorted(df["date"].unique()))
    all_days.loc[daily.index] = daily.values
    eq = all_days.cumsum()
    dd = (eq - eq.cummax()).min()
    pf = wins["net_pnl"].sum() / max(1e-9, -losses["net_pnl"].sum())
    sharpe = all_days.mean() / all_days.std() * np.sqrt(252) if all_days.std() > 0 else float("nan")
    peak_margin = margin_used.max()
    return {
        "label": label, "trades": len(tr), "win": len(wins) / len(tr) * 100,
        "net": tr["net_pnl"].sum(), "pf": pf, "dd": dd, "sharpe": sharpe,
        "peak_margin": peak_margin, "avg_margin": margin_used[margin_used > 0].mean(),
    }


def main():
    # match the LIVE spec (paper_trade.py), not config.py's older defaults
    config.SL_PCT_OF_MARGIN = 0.15
    config.SQUARE_OFF = "15:00"
    print(f"using live spec: SL {config.SL_PCT_OF_MARGIN:.0%} of margin, "
          f"exit {config.SQUARE_OFF}, windows {config.VOL_RATIO_WINDOW}/{config.OPT_VOL_WINDOW}\n")
    variants = [
        (dict(max_pos=1, tag="v_max1"), "max 1 (old)"),
        (dict(max_pos=2, tag="v_max2_any"), "max 2, any/same-day (live now)"),
        (dict(max_pos=2, tag="v_max2_diffday", require_diff_day=True), "max 2, diff-day only"),
        (dict(max_pos=2, tag="v_max2_rule", require_diff_day=True, require_opposite=True),
         "max 2, diff-day + opposite"),
    ]
    rows = []
    for kw, label in variants:
        tr, df, mu = run(**kw)
        rows.append(summarize(tr, df, mu, label))
    print("\n" + "=" * 74)
    print(f"{'variant':<32}{'trades':>7}{'win%':>7}{'net P&L':>11}{'PF':>6}{'max DD':>10}{'peak margin':>13}")
    print("-" * 88)
    for r in rows:
        print(f"{r['label']:<32}{r['trades']:>7}{r['win']:>6.1f}%"
              f"{r['net']:>11,.0f}{r['pf']:>6.2f}{r['dd']:>10,.0f}{r['peak_margin']:>13,.0f}")
    print("=" * 88)
    print(f"period: {config.BACKTEST_START} -> {config.BACKTEST_END}")


if __name__ == "__main__":
    main()
