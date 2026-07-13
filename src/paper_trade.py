"""Live paper trading for the overnight credit-spread strategy.

Configuration (the backtest's best variant):
  - signals/entries identical to backtest.py (alpha & alpha2, 10:15-14:15,
    edge-triggered, one position at a time, 1 lot)
  - stop-loss 15% of margin, checked every minute
  - exit at 15:15 on the next trading day (same day if entry is on expiry)

Run it each trading day (it exits after square-off time):
    python paper_trade.py

State is kept in results/paper_state.json so an overnight position survives
restarts; completed trades are appended to results/paper_trades.csv.
NOTE: the Upstox access token in config/token.txt expires every morning
around 3:30 AM - paste a fresh one before starting the script.
"""
import json
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
import signals
import upstox_api
from backtest import charges

SL_PCT = 0.15                       # best backtested value (NOT config's 0.25)
EXIT_TIME = "15:15"                 # next-day square-off
STATE_FILE = config.RESULTS_DIR / "paper_state.json"
TRADES_FILE = config.RESULTS_DIR / "paper_trades.csv"
LOG_FILE = config.RESULTS_DIR / "paper_trade.log"

COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- Upstox live endpoints ----------

def _candles_df(candles):
    df = pd.DataFrame(candles, columns=COLS)
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def intraday_1min(key):
    from urllib.parse import quote
    url = f"{config.BASE_URL}/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/1"
    return _candles_df(upstox_api._get(url)["data"]["candles"])


def hist_1min(key, from_date, to_date):
    from urllib.parse import quote
    url = (f"{config.BASE_URL}/v3/historical-candle/{quote(key, safe='')}"
           f"/minutes/1/{to_date}/{from_date}")
    return _candles_df(upstox_api._get(url)["data"]["candles"])


def live_option_contracts():
    """All live NIFTY option contracts, grouped {expiry: {(strike,kind): key_info}}."""
    url = f"{config.BASE_URL}/v2/option/contract"
    data = upstox_api._get(url, {"instrument_key": config.NIFTY_KEY})["data"]
    by_exp = {}
    for c in data:
        e = c["expiry"]
        by_exp.setdefault(e, {})[(int(c["strike_price"]), c["instrument_type"])] = c
    return by_exp


def ltp(keys):
    """Last traded prices for a list of instrument keys -> {key: price}."""
    url = f"{config.BASE_URL}/v2/market-quote/ltp"
    data = upstox_api._get(url, {"instrument_key": ",".join(keys)})["data"]
    out = {}
    for v in data.values():
        out[v["instrument_token"]] = v["last_price"]
    return out


# ---------- state ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"position": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, default=str))


def append_trade(row):
    df = pd.DataFrame([row])
    header = not TRADES_FILE.exists()
    df.to_csv(TRADES_FILE, mode="a", header=header, index=False)


# ---------- market data assembly ----------

class LiveData:
    """Keeps spot and option 1-min frames up to date and builds the signal frame."""

    def __init__(self):
        self.today = datetime.now().date()
        self.contracts_by_expiry = live_option_contracts()
        live_exps = sorted(e for e in self.contracts_by_expiry
                           if pd.Timestamp(e).date() >= self.today)
        if not live_exps:
            raise RuntimeError("no live NIFTY expiries returned")
        self.expiry = live_exps[0]
        self.expiry_date = pd.Timestamp(self.expiry).date()
        self.contracts = self.contracts_by_expiry[self.expiry]
        self.lot = int(next(iter(self.contracts.values()))["lot_size"])
        self.opt_cache = {}            # (strike, kind) -> DataFrame ts/close/volume
        self.expired_keys = None       # lazy: prev-week contracts via expired API

        # spot warm-up: ~4 trading days for the 800-min alpha window
        frm = (self.today - timedelta(days=9)).isoformat()
        to = (self.today - timedelta(days=1)).isoformat()
        self.spot_hist = hist_1min(config.NIFTY_KEY, frm, to)
        log(f"warm-up spot: {len(self.spot_hist)} bars "
            f"({self.spot_hist['ts'].min()} -> {self.spot_hist['ts'].max()})")
        log(f"trading expiry {self.expiry}, lot {self.lot}")

    def refresh_spot(self):
        intra = intraday_1min(config.NIFTY_KEY)
        self.spot = pd.concat([self.spot_hist, intra]).drop_duplicates("ts") \
                      .sort_values("ts").reset_index(drop=True)
        return self.spot

    # -- option candles --
    def _expired_lookup(self, day):
        """Contracts for a past day that belonged to an already-expired weekly."""
        if self.expired_keys is None:
            exps = upstox_api.expired_expiries()
            past = [e for e in exps if pd.Timestamp(e).date() >= day]
            if not past:
                return None, None
            e = past[0]
            cons = upstox_api.expired_contracts(e)
            self.expired_keys = (e, {(int(c["strike_price"]), c["instrument_type"]): c
                                     for c in cons})
        return self.expired_keys

    def option_series(self, strike, kind, need_from):
        """1-min close/volume for a contract from need_from to now (cached)."""
        k = (strike, kind)
        cached = self.opt_cache.get(k)
        if cached is None or cached.attrs.get("static_until") is None:
            parts = []
            # past days: live contract history (works while contract is alive)
            c = self.contracts.get(k)
            if c is not None and need_from < self.today:
                try:
                    parts.append(hist_1min(c["instrument_key"], need_from.isoformat(),
                                           (self.today - timedelta(days=1)).isoformat()))
                except Exception:
                    pass
            # if the warm-up day belonged to last week's (expired) contract
            if need_from < self.today and (not parts or parts[0].empty):
                exp_info = self._expired_lookup(need_from)
                if exp_info and exp_info[1] and k in exp_info[1]:
                    e, keys = exp_info
                    try:
                        parts.append(_candles_df(upstox_api.expired_candles_1min(
                            keys[k]["instrument_key"], need_from.isoformat(), e)))
                    except Exception:
                        pass
            static = pd.concat(parts).drop_duplicates("ts").sort_values("ts") \
                       .reset_index(drop=True) if parts else pd.DataFrame(columns=COLS)
            static.attrs["static_until"] = str(self.today)
            self.opt_cache[k] = static
            cached = static
        # today's bars are refreshed on every call
        c = self.contracts.get(k)
        if c is None:
            return cached
        try:
            intra = intraday_1min(c["instrument_key"])
        except Exception:
            intra = pd.DataFrame(columns=COLS)
        full = pd.concat([cached, intra]).drop_duplicates("ts") \
                 .sort_values("ts").reset_index(drop=True)
        full.attrs["static_until"] = cached.attrs.get("static_until")
        return full

    def contract_key(self, strike, kind):
        c = self.contracts.get((strike, kind))
        return c["instrument_key"] if c else None


def build_signal_frame(live):
    """Spot frame with alpha/alpha2/atm, mirroring signals.compute_signals."""
    spot = live.refresh_spot().copy()
    spot["date"] = spot["ts"].dt.date
    spot["day_open"] = spot.groupby("date")["open"].transform("first")
    prev5 = spot.groupby("date")["close"].shift(config.RET_MINUTES)
    spot["ret5"] = (spot["close"] - prev5) / spot["day_open"]
    spot["alpha"] = signals.ts_rank(spot["ret5"].values, config.ALPHA_LOOKBACK)

    atm = (np.round(spot["close"].values / config.STRIKE_STEP) * config.STRIKE_STEP).astype(int)
    spot["atm"] = atm

    # chained ATM option volume/return series (only the alpha2 lookback matters:
    # 300-min rank + 60-min SMA -> prev trading day + today is enough)
    need = config.ALPHA2_LOOKBACK + config.VOL_RATIO_WINDOW + config.RET_MINUTES + 30
    sub = spot.tail(min(len(spot), need + 400)).copy()
    need_from = sub["date"].min()
    n = len(sub)
    vol_ce = np.full(n, np.nan); vol_pe = np.full(n, np.nan)
    ret_ce = np.full(n, np.nan); ret_pe = np.full(n, np.nan)
    sub_atm = sub["atm"].values
    for strike in np.unique(sub_atm):
        for kind, vol_a, ret_a in (("CE", vol_ce, ret_ce), ("PE", vol_pe, ret_pe)):
            dfc = live.option_series(int(strike), kind, need_from)
            if dfc.empty:
                continue
            s = dfc.set_index("ts")
            px = s["close"].reindex(pd.DatetimeIndex(sub["ts"])).ffill()
            vol = s["volume"].reindex(pd.DatetimeIndex(sub["ts"])).fillna(0.0)
            ret = px.pct_change()
            sel = sub_atm == strike
            vol_a[sel] = vol.values[sel]
            ret_a[sel] = ret.values[sel]

    w = config.VOL_RATIO_WINDOW
    vr_ce = vol_ce / pd.Series(vol_ce).rolling(w, min_periods=w // 2).mean().values
    vr_pe = vol_pe / pd.Series(vol_pe).rolling(w, min_periods=w // 2).mean().values
    with np.errstate(invalid="ignore"):
        vr = np.nanmean(np.vstack([vr_ce, vr_pe]), axis=0)
    v = config.OPT_VOL_WINDOW
    sig_ce = pd.Series(ret_ce).rolling(v, min_periods=v // 2).std().values
    sig_pe = pd.Series(ret_pe).rolling(v, min_periods=v // 2).std().values
    sigma = sig_ce + sig_pe
    with np.errstate(invalid="ignore", divide="ignore"):
        raw2 = sub["ret5"].values * vr / sigma
    raw2[~np.isfinite(raw2)] = np.nan
    a2 = signals.ts_rank(raw2, config.ALPHA2_LOOKBACK)
    spot["alpha2"] = np.nan
    spot.loc[sub.index, "alpha2"] = a2
    return spot


# ---------- trading ----------

def paper_fill(live, strike, kind, side):
    key = live.contract_key(strike, kind)
    if key is None:
        return None
    px = ltp([key]).get(key)
    if px is None or px <= 0:
        return None
    return max(px + config.SLIPPAGE, 0.05) if side == "buy" else max(px - config.SLIPPAGE, 0.05)


def close_position(state, live, reason):
    pos = state["position"]
    sb = paper_fill(live, pos["short_strike"], pos["kind"], "buy")
    lb = paper_fill(live, pos["long_strike"], pos["kind"], "sell")
    if sb is None or lb is None:
        log(f"WARN: no quote to close ({reason}); will retry next minute")
        return False
    lot = pos["lot"]
    gross = (pos["credit"] - (sb - lb)) * lot
    cost = charges(buy_turnover=(pos["long_entry"] + sb) * lot,
                   sell_turnover=(pos["short_entry"] + lb) * lot, n_orders=4)
    row = {**{k: pos[k] for k in ("date", "expiry", "type", "entry_ts", "atm", "lot",
                                  "short_strike", "long_strike", "credit", "margin",
                                  "alpha", "alpha2", "spot_entry")},
           "exit_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "exit_reason": reason, "exit_cost_to_close": sb - lb,
           "gross_pnl": gross, "charges": cost, "net_pnl": gross - cost}
    append_trade(row)
    log(f"CLOSED {pos['type']} {pos['short_strike']}/{pos['long_strike']} "
        f"({reason}) net {gross - cost:,.0f}")
    state["position"] = None
    save_state(state)
    return True


def try_exit(state, live, now):
    pos = state["position"]
    t_exit = pd.Timestamp(EXIT_TIME).time()
    exit_date = pd.Timestamp(pos["exit_date"]).date()
    expiry = pd.Timestamp(pos["expiry"]).date()
    # time exit: first trading day >= exit_date at/after 15:15 (or expiry day)
    if (now.date() >= exit_date or now.date() >= expiry) and now.time() >= t_exit:
        return close_position(state, live, "TIME")
    # stop-loss on live LTP
    skey = live.contract_key(pos["short_strike"], pos["kind"])
    lkey = live.contract_key(pos["long_strike"], pos["kind"])
    if not skey or not lkey:
        return False
    q = ltp([skey, lkey])
    sc, lc = q.get(skey), q.get(lkey)
    if sc is None or lc is None:
        return False
    mtm = (pos["credit"] - (sc - lc)) * pos["lot"]
    if mtm <= -SL_PCT * pos["margin"]:
        log(f"SL hit: MTM {mtm:,.0f} <= -{SL_PCT * pos['margin']:,.0f}")
        return close_position(state, live, "SL")
    return False


def try_entry(state, live, frame, now):
    t0 = pd.Timestamp(config.ENTRY_START).time()
    t1 = pd.Timestamp(config.ENTRY_END).time()
    if not (t0 <= now.time() <= t1):
        return
    cur, prev = frame.iloc[-1], frame.iloc[-2]
    lc = cur["alpha"] > config.LONG_TH and cur["alpha2"] > config.LONG_TH
    lp = prev["alpha"] > config.LONG_TH and prev["alpha2"] > config.LONG_TH
    sc = cur["alpha"] < config.SHORT_TH and cur["alpha2"] < config.SHORT_TH
    sp = prev["alpha"] < config.SHORT_TH and prev["alpha2"] < config.SHORT_TH
    sig = 1 if (lc and not lp) else (-1 if (sc and not sp) else 0)
    if sig == 0:
        return
    atm = int(cur["atm"])
    if sig == 1:
        kind, s_strike, l_strike, typ = "PE", atm, atm - config.WING_POINTS, "bull_put"
    else:
        kind, s_strike, l_strike, typ = "CE", atm, atm + config.WING_POINTS, "bear_call"
    s_fill = paper_fill(live, s_strike, kind, "sell")
    l_fill = paper_fill(live, l_strike, kind, "buy")
    if s_fill is None or l_fill is None:
        log(f"signal {typ} but no quotes for {s_strike}/{l_strike}{kind}, skipped")
        return
    credit = s_fill - l_fill
    if credit <= 0:
        log(f"signal {typ} but credit <= 0, skipped")
        return
    lot = live.lot
    margin = (config.WING_POINTS - credit) * lot
    today = now.date()
    exit_date = today if today == live.expiry_date else (today + timedelta(days=1))
    state["position"] = {
        "date": str(today), "expiry": live.expiry, "type": typ, "kind": kind,
        "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"), "atm": atm, "lot": lot,
        "short_strike": s_strike, "long_strike": l_strike,
        "credit": credit, "margin": margin,
        "short_entry": s_fill, "long_entry": l_fill,
        "alpha": float(cur["alpha"]), "alpha2": float(cur["alpha2"]),
        "spot_entry": float(cur["close"]),
        "exit_date": str(min(exit_date, live.expiry_date)),
    }
    save_state(state)
    log(f"OPENED {typ}: sell {s_strike}{kind} @ {s_fill:.2f}, "
        f"buy {l_strike}{kind} @ {l_fill:.2f}, credit {credit:.2f}, "
        f"margin {margin:,.0f}, SL {SL_PCT * margin:,.0f}, exit {state['position']['exit_date']} {EXIT_TIME}")


def main():
    log("=" * 60)
    log(f"paper trading start | SL {SL_PCT:.0%} of margin | exit next day {EXIT_TIME}")
    try:
        live = LiveData()
    except RuntimeError as e:
        if "401" in str(e) or "Unauthorized" in str(e):
            log("TOKEN EXPIRED - paste a fresh Upstox token into config/token.txt")
            return
        raise
    state = load_state()
    if state["position"]:
        log(f"restored open position: {state['position']['type']} "
            f"{state['position']['short_strike']}/{state['position']['long_strike']}")

    end = pd.Timestamp(datetime.now().date()).replace(hour=15, minute=30)
    while datetime.now() < end:
        # wait for the current minute's candle to complete (+8s cushion)
        now = datetime.now()
        wake = now.replace(second=8, microsecond=0) + timedelta(minutes=1)
        time.sleep(max(1.0, (wake - now).total_seconds()))
        now = datetime.now()
        if now.time() < pd.Timestamp("09:16").time():
            continue
        try:
            if state["position"]:
                try_exit(state, live, now)
            if not state["position"]:
                frame = build_signal_frame(live)
                if frame["ts"].iloc[-1].date() != now.date():
                    if now.time() >= pd.Timestamp("10:00").time():
                        log("no candles by 10:00 - market closed today, exiting")
                        break
                    log("no intraday candles yet (holiday or feed delay?)")
                    continue
                try_entry(state, live, frame, now)
        except RuntimeError as e:
            if "401" in str(e) or "Unauthorized" in str(e):
                log("TOKEN EXPIRED - paste a fresh token into config/token.txt; retrying in 60s")
                time.sleep(60)
            else:
                log(f"ERROR: {e}")
        except Exception as e:
            log(f"ERROR: {e}")

    log(f"day done. open position: {bool(state['position'])} "
        f"(state saved; run again tomorrow)")


if __name__ == "__main__":
    main()
