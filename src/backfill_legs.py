"""One-off: reconstruct individual leg prices for trades closed before leg
prices were recorded. The recorded net credit and exit cost are exact (from
live LTP fills); we anchor to them so the reconstructed legs stay perfectly
consistent:

  long leg (cheap wing) comes from the real 1-min option close at the
  entry/exit minute, with the live slippage convention applied;
  short leg (ATM) is derived so short - long == the exact recorded net.

Rows whose option data can't be fetched are left untouched (stay blank).
"""
import numpy as np
import pandas as pd

import config
import upstox_api

SLIP = config.SLIPPAGE
COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]
_keys = {}      # expiry -> {(strike, kind): instrument_key}
_expired = set(upstox_api.expired_expiries())


def contract_key(expiry, strike, kind):
    if expiry not in _keys:
        try:
            if expiry in _expired:
                cs = upstox_api.expired_contracts(expiry)
            else:
                cs = upstox_api.active_option_contracts(expiry)
            _keys[expiry] = {(int(c["strike_price"]), c["instrument_type"]): c["instrument_key"]
                             for c in cs}
        except Exception as e:
            print(f"  contract list failed for {expiry}: {e}")
            _keys[expiry] = {}
    return _keys[expiry].get((int(strike), kind))


def candles(expiry, key, d0, d1):
    if expiry in _expired:
        raw = upstox_api.expired_candles_1min(key, d0, expiry)
    else:
        raw = upstox_api.live_candles_1min(key, d0, d1)
        try:
            raw = raw + upstox_api.intraday_candles_1min(key)
        except Exception:
            pass
    df = pd.DataFrame(raw, columns=COLS)
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    return df.drop_duplicates("ts").set_index("ts")["close"].sort_index()


def close_at(series, ts):
    if series is None:
        return np.nan
    s = series[series.index <= ts]
    return s.iloc[-1] if len(s) else np.nan


def main():
    tr = pd.read_csv(config.RESULTS_DIR / "paper_trades.csv")
    filled = 0
    for i, r in tr.iterrows():
        if pd.notna(r.get("long_entry")) and pd.notna(r.get("short_entry")):
            continue
        kind = "PE" if r["type"] == "bull_put" else "CE"
        exp = r["expiry"]
        key = contract_key(exp, r["long_strike"], kind)
        if not key:
            print(f"  {r['entry_ts'][:16]} {r['type']}: no long-leg contract, skip")
            continue
        t_in = pd.Timestamp(r["entry_ts"]).floor("min")
        t_out = pd.Timestamp(r["exit_ts"]).floor("min")
        ser = candles(exp, key, str(t_in.date()), str(t_out.date()))
        lc_in, lc_out = close_at(ser, t_in), close_at(ser, t_out)
        if not (np.isfinite(lc_in) and np.isfinite(lc_out)):
            print(f"  {r['entry_ts'][:16]} {r['type']}: no candle at entry/exit, skip")
            continue
        long_entry = lc_in + SLIP
        long_exit = max(lc_out - SLIP, 0.05)
        tr.at[i, "long_entry"] = round(long_entry, 2)
        tr.at[i, "short_entry"] = round(long_entry + r["credit"], 2)
        tr.at[i, "long_exit"] = round(long_exit, 2)
        tr.at[i, "short_exit"] = round(long_exit + r["exit_cost_to_close"], 2)
        filled += 1
        print(f"  {r['entry_ts'][:16]} {r['type']}: short {tr.at[i,'short_entry']}->{tr.at[i,'short_exit']}, "
              f"long {tr.at[i,'long_entry']}->{tr.at[i,'long_exit']}")

    from paper_trade import TRADE_COLUMNS
    tr.reindex(columns=TRADE_COLUMNS).to_csv(config.RESULTS_DIR / "paper_trades.csv", index=False)
    print(f"\nfilled {filled} / {len(tr)} rows")


if __name__ == "__main__":
    main()
