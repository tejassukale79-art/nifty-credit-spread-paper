"""Download 1-min candles for every NIFTY weekly option contract the strategy
can touch: ATM CE+PE across each expiry week's spot range, plus PE wings 400
points below and CE wings 400 points above. Resumable: existing files are skipped.
"""
import json
import pandas as pd

import config
import upstox_api

COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def strike_sets(week_lo, week_hi):
    step = config.STRIKE_STEP
    lo = int(week_lo // step) * step - 100
    hi = (int(week_hi // step) + 1) * step + 100
    atm = list(range(lo, hi + 1, step))
    pe_wings = [s - config.WING_POINTS for s in atm]
    ce_wings = [s + config.WING_POINTS for s in atm]
    need = {(s, "CE") for s in atm} | {(s, "PE") for s in atm}
    need |= {(s, "PE") for s in pe_wings} | {(s, "CE") for s in ce_wings}
    return need


def save_contract(expiry_dir, contract, from_date, expiry):
    strike = int(contract["strike_price"])
    kind = contract["instrument_type"]
    out = expiry_dir / f"{strike}{kind}.parquet"
    if out.exists():
        return False
    candles = upstox_api.expired_candles_1min(contract["instrument_key"], from_date, expiry)
    df = pd.DataFrame(candles, columns=COLS)
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df.to_parquet(out, index=False)
    return True


def main():
    spot = pd.read_parquet(config.SPOT_DIR / "nifty_1min.parquet")
    spot["date"] = spot["ts"].dt.date

    expiries = [e for e in upstox_api.expired_expiries()
                if e <= config.BACKTEST_END]
    print(f"{len(expiries)} expiries: {expiries[0]} -> {expiries[-1]}", flush=True)

    prev = None
    for expiry in expiries:
        edate = pd.Timestamp(expiry).date()
        wstart = pd.Timestamp(prev).date() if prev else pd.Timestamp(config.BACKTEST_START).date()
        prev = expiry
        # week = trading days on which this expiry is the nearest weekly
        mask = (spot["date"] > wstart) & (spot["date"] <= edate) if wstart != pd.Timestamp(config.BACKTEST_START).date() or expiry != expiries[0] \
            else (spot["date"] >= wstart) & (spot["date"] <= edate)
        week = spot[mask]
        if week.empty:
            print(f"{expiry}: no spot days, skip", flush=True)
            continue

        need = strike_sets(week["low"].min(), week["high"].max())
        expiry_dir = config.OPT_DIR / expiry
        expiry_dir.mkdir(parents=True, exist_ok=True)

        contracts = upstox_api.expired_contracts(expiry)
        meta = {"expiry": expiry, "lot_size": contracts[0]["lot_size"],
                "week_start": str(week["date"].min()), "week_end": str(week["date"].max())}
        (expiry_dir / "meta.json").write_text(json.dumps(meta))

        by_key = {(int(c["strike_price"]), c["instrument_type"]): c for c in contracts}
        # fetch from a few days before the week start so rolling windows warm up
        from_date = (pd.Timestamp(week["date"].min()) - pd.Timedelta(days=6)).date().isoformat()
        fetched = missing = 0
        for sk in sorted(need):
            c = by_key.get(sk)
            if c is None:
                missing += 1
                continue
            try:
                if save_contract(expiry_dir, c, from_date, expiry):
                    fetched += 1
            except Exception as exc:      # log and move on; rerun picks it up
                print(f"  FAIL {sk}: {exc}", flush=True)
        print(f"{expiry}: lot={meta['lot_size']} need={len(need)} fetched={fetched} missing={missing}", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()
