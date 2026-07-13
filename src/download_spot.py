"""Download NIFTY 50 index 1-min candles and store one parquet file."""
import pandas as pd

import config
import upstox_api


def main():
    chunks = []
    start = pd.Timestamp(config.SPOT_START)
    end = pd.Timestamp(config.BACKTEST_END) + pd.Timedelta(days=7)
    cur = start
    while cur <= end:
        chunk_end = min(cur + pd.Timedelta(days=27), end)
        candles = upstox_api.spot_candles_1min(cur.date().isoformat(),
                                               chunk_end.date().isoformat())
        print(f"{cur.date()} -> {chunk_end.date()}: {len(candles)} candles", flush=True)
        chunks.extend(candles)
        cur = chunk_end + pd.Timedelta(days=1)

    df = pd.DataFrame(chunks, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    out = config.SPOT_DIR / "nifty_1min.parquet"
    df.to_parquet(out, index=False)
    print(f"saved {len(df)} rows -> {out}")
    print(df["ts"].min(), "->", df["ts"].max())


if __name__ == "__main__":
    main()
