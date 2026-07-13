"""Credit-spread backtester.

Rules:
- Entry window 10:15-14:15. Signal is edge-triggered: both alphas cross the
  threshold this minute (condition false on the previous minute). One open
  position at a time.
- alpha > 0.8 and alpha2 > 0.8  -> bull put spread: sell ATM PE, buy (ATM-400) PE
- alpha < 0.2 and alpha2 < 0.2  -> bear call spread: sell ATM CE, buy (ATM+400) CE
- Fills at next minute's open (fallback: last traded price), +/- slippage.
- Stop-loss: MTM loss >= SL_PCT_OF_MARGIN * margin, margin = (width - credit) * lot.
- Square-off 15:15. 1 lot, historically correct lot size per expiry.
- Full Indian F&O charges per leg.
"""
import numpy as np
import pandas as pd

import config
import signals
import upstox_api


def leg_fill(dfc, t, side):
    """Fill price for one leg at minute t (open preferred, else last trade)."""
    if dfc is None or t not in dfc.index:
        return None
    row = dfc.loc[t]
    px = row["open"] if np.isfinite(row["open"]) else row["close"]
    if not np.isfinite(px):
        return None
    return max(px + config.SLIPPAGE, 0.05) if side == "buy" else max(px - config.SLIPPAGE, 0.05)


def charges(buy_turnover, sell_turnover, n_orders):
    brokerage = config.BROKERAGE_PER_ORDER * n_orders
    turnover = buy_turnover + sell_turnover
    stt = config.STT_SELL * sell_turnover
    exch = config.EXCH_TXN * turnover
    sebi = config.SEBI * turnover
    stamp = config.STAMP_BUY * buy_turnover
    gst = config.GST * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def prepare():
    """Load data and compute alphas once; reusable across simulation variants."""
    spot = signals.load_spot()
    expiries = [e for e in upstox_api.expired_expiries() if e <= config.BACKTEST_END]
    emap = signals.expiry_map(spot["date"], expiries)
    store = signals.OptionStore(pd.DatetimeIndex(spot["ts"]))

    print("computing signals...", flush=True)
    df = signals.compute_signals(spot, emap, store)
    df = df[(df["date"] >= pd.Timestamp(config.BACKTEST_START).date()) &
            (df["date"] <= pd.Timestamp(config.BACKTEST_END).date())].reset_index(drop=True)
    return df, emap, store


def run(prepared=None, invert=False, tag="baseline"):
    df, emap, store = prepared if prepared else prepare()
    df = df.copy()

    long_cond = (df["alpha"] > config.LONG_TH) & (df["alpha2"] > config.LONG_TH)
    short_cond = (df["alpha"] < config.SHORT_TH) & (df["alpha2"] < config.SHORT_TH)
    if invert:
        long_cond, short_cond = short_cond, long_cond
    df["sig"] = np.where(long_cond & ~long_cond.shift(fill_value=False), 1,
                np.where(short_cond & ~short_cond.shift(fill_value=False), -1, 0))

    t_entry_start = pd.Timestamp(config.ENTRY_START).time()
    t_entry_end = pd.Timestamp(config.ENTRY_END).time()
    t_square = pd.Timestamp(config.SQUARE_OFF).time()

    trades = []
    print("simulating...", flush=True)
    for d, day in df.groupby("date", sort=True):
        expiry = emap.get(d)
        if expiry is None or str(expiry) > config.BACKTEST_END:
            continue
        data = store.expiry_data(str(expiry))
        if not data:
            continue
        lot = store.lot_size(str(expiry))
        day = day.reset_index(drop=True)
        times = day["ts"].dt.time.values
        pos = None

        for i in range(len(day) - 1):
            t_next = day["ts"].iloc[i + 1]

            if pos is not None:
                # MTM at current minute closes
                sc = pos["short_df"]["close"].get(day["ts"].iloc[i], np.nan)
                lc = pos["long_df"]["close"].get(day["ts"].iloc[i], np.nan)
                hit_sl = False
                if np.isfinite(sc) and np.isfinite(lc):
                    mtm = (pos["credit"] - (sc - lc)) * lot
                    hit_sl = mtm <= -pos["sl_amount"]
                if hit_sl or times[i] >= t_square:
                    sb = leg_fill(pos["short_df"], t_next, "buy")
                    lb = leg_fill(pos["long_df"], t_next, "sell")
                    if sb is None or lb is None:   # no next-minute data: use MTM closes
                        sb, lb = sc + config.SLIPPAGE, max(lc - config.SLIPPAGE, 0.05)
                    gross = (pos["credit"] - (sb - lb)) * lot
                    cost = charges(buy_turnover=(pos["long_entry"] + sb) * lot,
                                   sell_turnover=(pos["short_entry"] + lb) * lot,
                                   n_orders=4)
                    trades.append({**pos["info"], "exit_ts": t_next,
                                   "exit_reason": "SL" if hit_sl else "EOD",
                                   "exit_cost_to_close": sb - lb,
                                   "gross_pnl": gross, "charges": cost,
                                   "net_pnl": gross - cost})
                    pos = None
                continue

            sig = day["sig"].iloc[i]
            if sig == 0 or not (t_entry_start <= times[i] <= t_entry_end):
                continue
            atm = int(day["atm"].iloc[i])
            if sig == 1:
                s_key, l_key = (atm, "PE"), (atm - config.WING_POINTS, "PE")
                kind = "bull_put"
            else:
                s_key, l_key = (atm, "CE"), (atm + config.WING_POINTS, "CE")
                kind = "bear_call"
            sdf, ldf = data.get(s_key), data.get(l_key)
            s_fill = leg_fill(sdf, t_next, "sell")
            l_fill = leg_fill(ldf, t_next, "buy")
            if s_fill is None or l_fill is None:
                continue
            credit = s_fill - l_fill
            if credit <= 0:
                continue
            margin = (config.WING_POINTS - credit) * lot
            pos = {"short_df": sdf, "long_df": ldf, "credit": credit,
                   "short_entry": s_fill, "long_entry": l_fill,
                   "sl_amount": config.SL_PCT_OF_MARGIN * margin,
                   "info": {"date": d, "expiry": str(expiry), "type": kind,
                            "entry_ts": t_next, "atm": atm, "lot": lot,
                            "short_strike": s_key[0], "long_strike": l_key[0],
                            "credit": credit, "margin": margin,
                            "alpha": day["alpha"].iloc[i], "alpha2": day["alpha2"].iloc[i],
                            "spot_entry": day["close"].iloc[i]}}

        # safety: if still open at day end (shouldn't happen), close at last close
        if pos is not None:
            t_last = day["ts"].iloc[-1]
            sc = pos["short_df"]["close"].get(t_last, np.nan)
            lc = pos["long_df"]["close"].get(t_last, np.nan)
            gross = (pos["credit"] - (sc - lc)) * lot
            cost = charges((pos["long_entry"] + sc) * lot, (pos["short_entry"] + lc) * lot, 4)
            trades.append({**pos["info"], "exit_ts": t_last, "exit_reason": "LAST",
                           "exit_cost_to_close": sc - lc,
                           "gross_pnl": gross, "charges": cost, "net_pnl": gross - cost})

    tr = pd.DataFrame(trades)
    tr.to_csv(config.RESULTS_DIR / f"trades_{tag}.csv", index=False)
    df[["ts", "close", "atm", "alpha", "alpha2", "sig"]].to_parquet(
        config.RESULTS_DIR / "signals.parquet", index=False)
    report(tr, df)
    return tr


def report(tr, df):
    if tr.empty:
        print("NO TRADES GENERATED")
        return
    tr["exit_ts"] = pd.to_datetime(tr["exit_ts"])
    ndays = df["date"].nunique()
    daily = tr.groupby(tr["exit_ts"].dt.date)["net_pnl"].sum()
    all_days = pd.Series(0.0, index=sorted(df["date"].unique()))
    all_days.loc[daily.index] = daily.values
    eq = all_days.cumsum()
    dd = eq - eq.cummax()
    avg_margin = tr["margin"].mean()

    wins = tr[tr["net_pnl"] > 0]; losses = tr[tr["net_pnl"] <= 0]
    print("=" * 64)
    print(f"period          : {df['date'].min()} -> {df['date'].max()}  ({ndays} days)")
    print(f"trades          : {len(tr)}  (bull_put {sum(tr['type']=='bull_put')}, "
          f"bear_call {sum(tr['type']=='bear_call')})")
    print(f"win rate        : {len(wins)/len(tr)*100:.1f}%")
    print(f"net P&L         : Rs {tr['net_pnl'].sum():,.0f}   "
          f"(gross {tr['gross_pnl'].sum():,.0f}, charges {tr['charges'].sum():,.0f})")
    print(f"avg win / loss  : {wins['net_pnl'].mean():,.0f} / {losses['net_pnl'].mean():,.0f}")
    print(f"profit factor   : {wins['net_pnl'].sum() / max(1e-9, -losses['net_pnl'].sum()):.2f}")
    print(f"max drawdown    : Rs {dd.min():,.0f}")
    print(f"avg margin/trade: Rs {avg_margin:,.0f}")
    mu, sd = all_days.mean(), all_days.std()
    print(f"daily Sharpe    : {mu/sd*np.sqrt(252):.2f}" if sd > 0 else "daily Sharpe: n/a")
    print(f"exit reasons    : {tr['exit_reason'].value_counts().to_dict()}")
    print("-" * 64)
    m = tr.set_index("exit_ts")["net_pnl"].resample("ME").sum()
    for ts, v in m.items():
        print(f"  {ts.strftime('%Y-%m')}: {v:>12,.0f}")
    print("=" * 64)


if __name__ == "__main__":
    run()
