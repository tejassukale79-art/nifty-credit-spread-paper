"""Replay Dhan's 174 live Zen trades through our own 1-min option data.

For each live trade we know: entry minute (IST), short/long strikes, CE/PE,
expiry (from the symbol), exit timestamp, and their reported P&L (5 lots).
We reprice every trade with our Upstox candles:
  replay_pnl  = (credit_at_entry - cost_at_exit) * 65      [per lot, no costs]
  replay_net  = replay_pnl - full Indian F&O charges per lot

Outputs per-trade CSV and a verdict: does their reported P&L line up with
gross (no charges) or net (with charges) repricing? Also infers their
stop-loss threshold from the loss/margin ratio of early exits.
"""
import calendar
import json
import re

import numpy as np
import pandas as pd

import config
import signals
from backtest import charges
from compare_dhan import load_dhan_trades

LOT = 65


def expiry_from_symbol(sym, known_expiries):
    m = re.match(r"NIFTY(\d{2})([1-9OND])(\d{2})(\d{4,5})(CE|PE)$", sym)
    if m:  # weekly: YY M DD, month Oct/Nov/Dec coded as O/N/D
        mo = {"O": 10, "N": 11, "D": 12}.get(m.group(2)) or int(m.group(2))
        return pd.Timestamp(2000 + int(m.group(1)), mo, int(m.group(3))).date()
    m = re.match(r"NIFTY(\d{2})([A-Z]{3})(\d{4,5})(CE|PE)$", sym)
    if m:  # monthly: last expiry of that month in our archive
        y = 2000 + int(m.group(1))
        mo = list(calendar.month_abbr).index(m.group(2).capitalize())
        cands = [e for e in known_expiries if e.year == y and e.month == mo]
        return max(cands) if cands else None
    return None


def main():
    dh = load_dhan_trades()
    dh = dh[dh["type"].isin(["bull_put", "bear_call"])].copy()
    raw = json.load(open(config.RESULTS_DIR / "dhan_GetHistoricalTrades.json"))["data"]
    sym_by_entry = {}
    for t in raw:
        m = re.search(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2})$", t["name"])
        entry = pd.to_datetime(m.group(1), format="%d/%m/%Y %H:%M")
        syms = {tr["symbol"] for s in t["summary"] for tr in s["trades"]}
        sym_by_entry[entry] = sorted(syms)

    known = sorted(pd.Timestamp(d.name).date()
                   for d in config.OPT_DIR.iterdir() if (d / "meta.json").exists())

    spot = pd.read_parquet(config.SPOT_DIR / "nifty_1min.parquet")
    store = signals.OptionStore(pd.DatetimeIndex(spot["ts"]))

    rows, skipped = [], 0
    for _, r in dh.iterrows():
        syms = sym_by_entry.get(r["entry_ts"])
        if not syms:
            skipped += 1
            continue
        expiry = expiry_from_symbol(syms[0], known)
        if expiry is None or str(expiry) not in [str(k) for k in known]:
            skipped += 1
            continue
        data = store.expiry_data(str(expiry))
        kind = "PE" if r["type"] == "bull_put" else "CE"
        sdf = data.get((int(r["short_strike"]), kind))
        ldf = data.get((int(r["long_strike"]), kind))
        if sdf is None or ldf is None:
            skipped += 1
            continue
        t_in = r["entry_ts"].floor("min")
        t_out = pd.Timestamp(r["exit_ts"]).floor("min") if pd.notna(r["exit_ts"]) else None
        try:
            s_in, l_in = sdf["close"].asof(t_in), ldf["close"].asof(t_in)
            s_out = sdf["close"].asof(t_out) if t_out is not None else np.nan
            l_out = ldf["close"].asof(t_out) if t_out is not None else np.nan
        except Exception:
            skipped += 1
            continue
        if not all(np.isfinite([s_in, l_in, s_out, l_out])):
            skipped += 1
            continue
        credit = s_in - l_in
        cost_out = s_out - l_out
        gross = (credit - cost_out) * LOT
        fee = charges((l_in + s_out) * LOT, (s_in + l_out) * LOT, 4)
        margin = (config.WING_POINTS - credit) * LOT
        held_min = (t_out - t_in).total_seconds() / 60
        # scheduled time-exit would be 15:15 next trading day (or entry day if expiry)
        early = t_out.time() < pd.Timestamp("15:05").time() or t_out.date() == t_in.date()
        rows.append({"entry_ts": t_in, "exit_ts": t_out, "type": r["type"],
                     "short": r["short_strike"], "long": r["long_strike"],
                     "expiry": str(expiry), "credit": credit,
                     "dhan_pnl_per_lot": r["pnl_per_lot"],
                     "replay_gross": gross, "replay_net": gross - fee,
                     "margin": margin, "loss_over_margin": -gross / margin,
                     "early_exit": early, "held_min": held_min})
    df = pd.DataFrame(rows)
    df.to_csv(config.RESULTS_DIR / "dhan_replay.csv", index=False)

    print(f"replayed {len(df)} / {len(dh)} trades (skipped {skipped})")
    d = df["dhan_pnl_per_lot"]
    print(f"\ntotal per lot : dhan {d.sum():>10,.0f}")
    print(f"                replay gross {df['replay_gross'].sum():>10,.0f}")
    print(f"                replay net   {df['replay_net'].sum():>10,.0f}")
    cg = np.corrcoef(d, df["replay_gross"])[0, 1]
    print(f"per-trade corr (dhan vs replay gross): {cg:.3f}")
    resid_g = (d - df["replay_gross"]).abs().median()
    resid_n = (d - df["replay_net"]).abs().median()
    print(f"median |residual| vs gross: Rs {resid_g:,.0f}, vs net: Rs {resid_n:,.0f}")

    sl = df[df["early_exit"] & (df["replay_gross"] < 0)]
    if len(sl):
        print(f"\ninferred SL: {len(sl)} early losing exits, "
              f"loss/margin at exit: median {sl['loss_over_margin'].median()*100:.1f}%  "
              f"(p25 {sl['loss_over_margin'].quantile(.25)*100:.1f}%, "
              f"p75 {sl['loss_over_margin'].quantile(.75)*100:.1f}%)")
    te = df[~df["early_exit"]]
    print(f"time exits: {len(te)}, median exit clock: "
          f"{pd.Series([t.time() for t in te['exit_ts']]).astype(str).mode().iloc[0]}")


if __name__ == "__main__":
    main()
