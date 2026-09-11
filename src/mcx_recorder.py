"""Daily 1-minute MCX archive.

Why this exists: Upstox serves ZERO expired MCX instruments (it serves 100+ for
NSE), so once an MCX contract expires its intraday history is gone for good.
Everything listed today can still be fetched; nothing before it can. That makes
MCX history a use-it-or-lose-it proposition, and this job captures it daily.

NSE is deliberately NOT recorded - its expired-instruments archive already
reaches back years, so recording it would only duplicate what the broker keeps.

Design notes:
  * Pulls COMPLETED 1-minute bars after the session, rather than streaming.
    A websocket recorder loses whatever it was holding when it drops - and the
    desks on this box sat with dead streams for two weeks without anyone
    noticing. An end-of-day pull is idempotent: a failed run is simply redone.
  * Resumable and self-healing: files already on disk are skipped, and each run
    also looks BACKFILL_DAYS back to fill anything a failed run missed.
  * brotli parquet, measured at ~13.8 KB per contract-session.

Scope is a judgment call that cannot be undone later - a strike not captured
today is unrecoverable - so it is deliberately wider than current strategies
need. Live crude trades have never gone beyond 7.4% from the future; the
default band is 20%.
"""
from __future__ import annotations

import gzip
import io
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

import config

# ---------------- scope ----------------
# Main contracts only. The minis (CRUDEOILM/GOLDM/SILVERM) are the same
# underlying at the same strikes - only the lot size differs - so their
# option prices track these and recording them would double the archive
# for no analytical gain.
SYMBOLS = ["CRUDEOIL", "GOLD", "SILVER"]
# Match SYMBOL + 2-digit year + 3-letter month, e.g. SILVER26SEPFUT. A loose
# "SILVER\d" also catches SILVER10026SEPFUT, a different contract quoted on a
# different scale - that made the front SILVER "spot" read 2,322 against
# strikes near 236,000, so every SILVER option fell outside the band.
CONTRACT_RE = r"\d\d[A-Z]{3}"
N_EXPIRIES = 3          # front N expiries per symbol
STRIKE_BAND = 0.20      # keep strikes within +/- this fraction of the front future
BACKFILL_DAYS = 5       # each run also repairs this many prior sessions
MIN_BARS = 5            # below this a contract is treated as untraded and skipped

OUT = Path(config.DATA_DIR) / "market"
LOG_FILE = Path(config.RESULTS_DIR) / "mcx_recorder.log"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.csv.gz"
COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]
PAUSE = 0.22            # ~4.5 req/s, well inside 500/min and 2000/30min


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def headers():
    return {"Authorization": f"Bearer {config.TOKEN}", "Accept": "application/json"}


def load_master():
    """Today's MCX contract list. Refetched daily - contracts come and go."""
    r = requests.get(MASTER_URL, timeout=90)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(gzip.decompress(r.content)))
    df["exp"] = pd.to_datetime(df["expiry"], errors="coerce")
    return df


def front_future(df, sym):
    """Nearest-expiry future for a symbol, and its last price."""
    f = df[(df.instrument_type == "FUTCOM")
           & (df.tradingsymbol.astype(str).str.match(sym + CONTRACT_RE))].sort_values("exp")
    if f.empty:
        return None, None
    row = f.iloc[0]
    try:
        r = requests.get(f"{config.BASE_URL}/v2/market-quote/ltp",
                         params={"instrument_key": row.instrument_key},
                         headers=headers(), timeout=20)
        d = r.json().get("data", {})
        px = float(list(d.values())[0]["last_price"]) if d else None
    except Exception:
        px = None
    return row, px


def in_scope(df):
    """[(symbol, label, instrument_key)] for everything worth recording today."""
    out = []
    for sym in SYMBOLS:
        fut, spot = front_future(df, sym)
        if fut is None:
            log(f"  {sym}: no future listed, skipped")
            continue
        futs = df[(df.instrument_type == "FUTCOM")
                  & (df.tradingsymbol.astype(str).str.match(sym + CONTRACT_RE))].sort_values("exp")
        exps = sorted(futs.exp.dropna().unique())[:N_EXPIRIES]
        for _, r in futs[futs.exp.isin(exps)].iterrows():
            out.append((sym, f"{sym}_{str(r.exp)[:10]}_FUT", r.instrument_key))
        if spot is None:
            log(f"  {sym}: no spot price, options skipped this run")
            continue
        o = df[(df.instrument_type == "OPTFUT")
               & (df.tradingsymbol.astype(str).str.match(sym + CONTRACT_RE))]
        oexps = sorted(o.exp.dropna().unique())[:N_EXPIRIES]
        sel = o[(o.exp.isin(oexps))
                & (o.strike.between(spot * (1 - STRIKE_BAND), spot * (1 + STRIKE_BAND)))]
        for _, r in sel.iterrows():
            kind = "CE" if str(r.tradingsymbol).endswith("CE") else "PE"
            out.append((sym, f"{sym}_{str(r.exp)[:10]}_{int(r.strike)}{kind}", r.instrument_key))
        log(f"  {sym}: spot {spot:,.0f} -> {len(sel)} options + "
            f"{len(futs[futs.exp.isin(exps)])} futures")
    return out


def fetch_day(key, day):
    """1-minute bars for one instrument on one date."""
    url = (f"{config.BASE_URL}/v3/historical-candle/{quote(key, safe='')}"
           f"/minutes/1/{day}/{day}")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers(), timeout=45)
        except Exception:
            time.sleep(3); continue
        if r.status_code == 200:
            return r.json().get("data", {}).get("candles", [])
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1)); continue
        if r.status_code >= 500:
            time.sleep(4); continue
        return []
    return []


def record(day, scope):
    """Write one parquet per traded contract for `day`. Returns (files, bytes)."""
    daydir = OUT / str(day)
    daydir.mkdir(parents=True, exist_ok=True)
    files = written = skipped = empty = 0
    for sym, label, key in scope:
        f = daydir / f"{label}.parquet"
        if f.exists():
            skipped += 1
            continue
        candles = fetch_day(key, day)
        time.sleep(PAUSE)
        if len(candles) < MIN_BARS:
            empty += 1
            continue
        d = pd.DataFrame(candles, columns=COLS)
        d["ts"] = pd.to_datetime(d["ts"]).dt.tz_localize(None)
        d = d.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        if d.empty:
            empty += 1
            continue
        d.to_parquet(f, compression="brotli", index=False)
        files += 1
        written += f.stat().st_size
    return files, written, skipped, empty


def main():
    if not config.TOKEN:
        log("no token - nothing recorded"); return 1
    only = None
    if "--date" in sys.argv:
        only = date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    log("=" * 62)
    log(f"MCX recorder start | {N_EXPIRIES} expiries, +/-{STRIKE_BAND:.0%} strikes")
    try:
        master = load_master()
    except Exception as exc:
        log(f"instrument master fetch failed: {exc}"); return 1
    scope = in_scope(master)
    log(f"scope: {len(scope)} instruments")

    days = [only] if only else [date.today() - timedelta(days=i)
                                for i in range(BACKFILL_DAYS)]
    total_f = total_b = 0
    for day in days:
        if day.weekday() >= 5:          # MCX is closed at the weekend
            continue
        t0 = time.time()
        f, b, sk, em = record(day, scope)
        total_f += f; total_b += b
        log(f"  {day}: {f} written ({b/1e6:.1f} MB), {sk} already had, "
            f"{em} untraded, {time.time()-t0:.0f}s")
    log(f"done: {total_f} files, {total_b/1e6:.1f} MB this run")
    try:
        import shutil
        used = sum(x.stat().st_size for x in OUT.rglob("*.parquet"))
        free = shutil.disk_usage(OUT).free
        log(f"archive now {used/1e9:.2f} GB; {free/1e9:.1f} GB free on disk")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
