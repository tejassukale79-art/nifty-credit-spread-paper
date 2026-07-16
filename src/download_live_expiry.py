"""Download 1-min candles for a LIVE (not yet expired) weekly expiry, so days
between the last archived expiry and today can be backtested.

Also appends today's spot candles (intraday endpoint) to the spot parquet.
Layout matches download_options.py so OptionStore needs no changes.
"""
import json
import sys

import pandas as pd

import config
import upstox_api
from download_options import COLS, strike_sets

EXPIRY = sys.argv[1] if len(sys.argv) > 1 else "2026-07-21"
PREV_EXPIRY = sys.argv[2] if len(sys.argv) > 2 else "2026-07-14"


def refresh_spot():
    spot_file = config.SPOT_DIR / "nifty_1min.parquet"
    old = pd.read_parquet(spot_file)
    rows = upstox_api.intraday_candles_1min(config.NIFTY_KEY)
    nd = pd.DataFrame(rows, columns=COLS)
    if len(nd):
        nd["ts"] = pd.to_datetime(nd["ts"]).dt.tz_localize(None)
        old = (pd.concat([old.drop(columns=["date"], errors="ignore"), nd])
               .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
        old.to_parquet(spot_file, index=False)
    print("spot:", old["ts"].min(), "->", old["ts"].max(), flush=True)
    return old


def main():
    spot = refresh_spot()
    spot["date"] = spot["ts"].dt.date
    today = spot["date"].max()
    week = spot[spot["date"] > pd.Timestamp(PREV_EXPIRY).date()]
    if week.empty:
        print("no spot days after", PREV_EXPIRY)
        return
    print(f"week days: {sorted(set(week['date']))}", flush=True)

    contracts = upstox_api.active_option_contracts(EXPIRY)
    by_key = {(int(c["strike_price"]), c["instrument_type"]): c for c in contracts}
    need = strike_sets(week["low"].min(), week["high"].max())

    edir = config.OPT_DIR / EXPIRY
    edir.mkdir(parents=True, exist_ok=True)
    meta = {"expiry": EXPIRY, "lot_size": contracts[0]["lot_size"], "live": True,
            "week_start": str(week["date"].min()), "week_end": str(week["date"].max())}
    (edir / "meta.json").write_text(json.dumps(meta))

    from_date = (pd.Timestamp(week["date"].min()) - pd.Timedelta(days=6)).date().isoformat()
    yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date().isoformat()

    fetched = missing = 0
    for sk in sorted(need):
        c = by_key.get(sk)
        if c is None:
            missing += 1
            continue
        out = edir / f"{sk[0]}{sk[1]}.parquet"
        try:
            rows = upstox_api.live_candles_1min(c["instrument_key"], from_date, yesterday)
            rows += upstox_api.intraday_candles_1min(c["instrument_key"])
        except Exception as exc:
            print(f"  FAIL {sk}: {exc}", flush=True)
            continue
        df = pd.DataFrame(rows, columns=COLS)
        if len(df):
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        df.to_parquet(out, index=False)
        fetched += 1
    print(f"{EXPIRY}: lot={meta['lot_size']} need={len(need)} "
          f"fetched={fetched} missing={missing}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
