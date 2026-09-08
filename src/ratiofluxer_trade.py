"""Ratio-Fluxer Credit Spread — PAPER trading engine.

Replica of Stratzy/Dhan's "Ratio-Fluxer Credit Spread Expiry", decoded from
their live trade log and backtested on real 1-min option data (39 trades,
2025-09 to 2026-09: net Rs 659,289, 84.6% win, max DD Rs -46,917).

RULES
  when       a session that is 1 day before the weekly expiry
  signal     IV entropy / IV dispersion <= 13, evaluated on each poll;
             enters on the FIRST crossing (causal - no intraday lookahead)
  direction  skew >= 2%  -> sell PUT spread ; else sell CALL spread
             skew = mean OTM put IV - mean OTM call IV, 150-350 pts out
  structure  sell the ATM strike, buy WIDTH points further OTM
  stop       exit if MTM loss >= 35% of max loss [(width-credit) x qty]
  exit       otherwise hold to expiry day EXIT_TIME

NO REAL ORDERS ARE PLACED. Fills are simulated at live quoted prices.
Shares nothing with paper_trade.py except config (token + paths).
"""
import json, math, time, sys
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path

import requests
import config

WIDTH        = 400        # points between short and long leg
LOTS         = 5
STOP_FRAC    = 0.35       # of max loss
EDR_TH       = 13.0       # entropy / dispersion entry threshold
SKEW_PE_TH   = 0.02       # skew above this -> sell puts instead of calls
ENTRY_FROM   = dtime(9, 30)
ENTRY_TO     = dtime(14, 30)
EXIT_TIME    = dtime(14, 59)
POLL_SEC     = 60
SPAN         = 500        # strikes within +/- this of spot feed the IV surface
R            = 0.065

STATE_FILE  = config.RESULTS_DIR / "rf_state.json"
TRADES_FILE = config.RESULTS_DIR / "rf_trades.csv"
LOG_FILE    = config.RESULTS_DIR / "rf_trade.log"
TRADE_COLUMNS = ["date","expiry","kind","entry_ts","spot_entry","short_strike","long_strike",
                 "width","lot","lots","qty","credit","max_loss","edr","skew",
                 "exit_ts","exit_reason","exit_cost","gross_pnl","charges","net_pnl",
                 "short_entry","long_entry","short_exit","long_exit"]

H = {"Authorization": f"Bearer {config.TOKEN}", "Accept": "application/json"}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- market data
def _get(url, **params):
    r = requests.get(url, headers=H, params=params or None, timeout=25)
    if r.status_code == 401:
        raise SystemExit("TOKEN EXPIRED")
    r.raise_for_status()
    return r.json()


def spot_ltp():
    d = _get(f"{config.BASE_URL}/v2/market-quote/ltp",
             instrument_key=config.NIFTY_KEY).get("data", {})
    return float(list(d.values())[0]["last_price"]) if d else None


def option_chain():
    """[(expiry_date, strike, kind, instrument_key, lot_size)] for live contracts."""
    d = _get(f"{config.BASE_URL}/v2/option/contract",
             instrument_key=config.NIFTY_KEY).get("data", [])
    out = []
    for c in d:
        out.append((datetime.fromisoformat(c["expiry"][:10]).date(),
                    int(float(c["strike_price"])), c["instrument_type"],
                    c["instrument_key"], int(c["lot_size"])))
    return out


def ltps(keys):
    """LTP for many instrument keys (batched)."""
    out = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        d = _get(f"{config.BASE_URL}/v2/market-quote/ltp",
                 instrument_key=",".join(chunk)).get("data", {})
        for v in d.values():
            k = v.get("instrument_token") or v.get("instrument_key")
            if k:
                out[k] = float(v["last_price"])
    return out


# ---------------------------------------------------------------- IV surface
def _N(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, sig, kind):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if kind == "CE" else max(K - S, 0.0)
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (R + 0.5 * sig * sig) * T) / v
    d2 = d1 - v
    if kind == "CE":
        return S * _N(d1) - K * math.exp(-R * T) * _N(d2)
    return K * math.exp(-R * T) * _N(-d2) - S * _N(-d1)


def implied_vol(px, S, K, T, kind):
    intr = max(S - K, 0.0) if kind == "CE" else max(K - S, 0.0)
    if T <= 0 or px <= intr + 1e-6:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, kind) < px:
            lo = mid
        else:
            hi = mid
    v = (lo + hi) / 2
    return v if 0.02 < v < 2.9 else None


def surface(chain, expiry, spot, prices):
    """entropy/dispersion ratio and skew from the live OTM smile."""
    exp_dt = datetime.combine(expiry, dtime(15, 30))
    T = max((exp_dt - datetime.now()).total_seconds() / (365 * 86400), 1e-6)
    pts = []
    for (e, K, kind, key, _lot) in chain:
        if e != expiry or abs(K - spot) > SPAN:
            continue
        if kind == "CE" and K < spot - 25:      # OTM side only
            continue
        if kind == "PE" and K > spot + 25:
            continue
        px = prices.get(key)
        if px is None or px <= 0.2:
            continue
        iv = implied_vol(px, spot, K, T, kind)
        if iv:
            pts.append((K, kind, iv))
    if len(pts) < 6:
        return None
    ivs = [p[2] for p in pts]
    mean = sum(ivs) / len(ivs)
    disp = (sum((v - mean) ** 2 for v in ivs) / len(ivs)) ** 0.5 / max(mean, 1e-9)
    tot = sum(ivs)
    ent = -sum((v / tot) * math.log(v / tot + 1e-12) for v in ivs) / math.log(len(ivs))
    def wing(kind_, lo_, hi_):
        sel = [v for (K, k_, v) in pts if k_ == kind_ and lo_ <= abs(K - spot) <= hi_]
        return sum(sel) / len(sel) if sel else None
    c_far, p_far = wing("CE", 150, 350), wing("PE", 150, 350)
    skew = (p_far - c_far) if (c_far is not None and p_far is not None) else None
    return dict(entropy=ent, disp=disp, edr=ent / max(disp, 1e-9), skew=skew,
                n=len(pts), T_days=T * 365)


# ---------------------------------------------------------------- costs
def charges(buy_turnover, sell_turnover, legs=4):
    brok = config.BROKERAGE_PER_ORDER * legs
    stt  = sell_turnover * config.STT_SELL
    exch = (buy_turnover + sell_turnover) * config.EXCH_TXN
    sebi = (buy_turnover + sell_turnover) * config.SEBI
    stamp = buy_turnover * config.STAMP_BUY
    return brok + stt + exch + sebi + stamp + config.GST * (brok + exch + sebi)


# ---------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"position": None, "live": None, "last_signal": None}


def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str))
    tmp.replace(STATE_FILE)


def append_trade(row):
    import pandas as pd
    df = pd.DataFrame([row]).reindex(columns=TRADE_COLUMNS)
    if TRADES_FILE.exists():
        old = pd.read_csv(TRADES_FILE).reindex(columns=TRADE_COLUMNS)
        df = pd.concat([old, df], ignore_index=True)
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(TRADES_FILE, index=False)


# ---------------------------------------------------------------- trading
def weekly_expiries(chain):
    return sorted({e for (e, *_rest) in chain})


def find_keys(chain, expiry, strike, kind):
    for (e, K, k, key, lot) in chain:
        if e == expiry and K == strike and k == kind:
            return key, lot
    return None, None


def try_entry(state, chain, spot, prices, now):
    exps = weekly_expiries(chain)
    fut = [e for e in exps if e >= now.date()]
    if not fut:
        return
    expiry = fut[0]
    dte = (expiry - now.date()).days
    m = surface(chain, expiry, spot, prices)
    if m:
        state["last_signal"] = {"ts": now.isoformat(), "dte": dte, "edr": round(m["edr"], 2),
                                "entropy": round(m["entropy"], 5), "disp": round(m["disp"], 4),
                                "skew": round(m["skew"], 4) if m["skew"] is not None else None,
                                "strikes": m["n"], "threshold": EDR_TH,
                                "eligible": dte == 1, "armed": bool(m["edr"] <= EDR_TH)}
    if dte != 1:
        return
    if not (ENTRY_FROM <= now.time() <= ENTRY_TO):
        return
    if m is None or m["skew"] is None:
        return
    if m["edr"] > EDR_TH:
        return
    kind = "PE" if m["skew"] >= SKEW_PE_TH else "CE"
    sgn = 1 if kind == "CE" else -1
    K1 = int(round(spot / config.STRIKE_STEP) * config.STRIKE_STEP)
    K2 = K1 + sgn * WIDTH
    sk, lot = find_keys(chain, expiry, K1, kind)
    lk, _ = find_keys(chain, expiry, K2, kind)
    if not sk or not lk:
        log(f"entry skipped: missing contract {K1}/{K2}{kind}")
        return
    px = ltps([sk, lk])
    s_px, l_px = px.get(sk), px.get(lk)
    if s_px is None or l_px is None:
        return
    s_fill = s_px - config.SLIPPAGE      # we SELL the short leg
    l_fill = l_px + config.SLIPPAGE      # we BUY the long leg
    credit = s_fill - l_fill
    if credit <= 2:
        log(f"entry skipped: credit {credit:.2f} too small")
        return
    qty = lot * LOTS
    pos = {"date": str(now.date()), "expiry": str(expiry), "kind": kind,
           "entry_ts": now.strftime("%Y-%m-%d %H:%M:%S"), "spot_entry": round(spot, 2),
           "short_strike": K1, "long_strike": K2, "width": WIDTH,
           "lot": lot, "lots": LOTS, "qty": qty,
           "short_key": sk, "long_key": lk,
           "short_entry": round(s_fill, 2), "long_entry": round(l_fill, 2),
           "credit": round(credit, 2), "max_loss": round((WIDTH - credit) * qty, 2),
           "edr": round(m["edr"], 2), "skew": round(m["skew"], 4)}
    state["position"] = pos
    save_state(state)
    log(f"ENTRY {kind} {K1}/{K2} credit {credit:.2f} x{qty} "
        f"(edr {m['edr']:.2f}, skew {m['skew']*100:.2f}%) max loss {pos['max_loss']:,.0f}")


def close_position(state, s_px, l_px, reason, now):
    p = state["position"]
    s_exit = s_px + config.SLIPPAGE      # buy back the short leg
    l_exit = max(l_px - config.SLIPPAGE, 0.05)
    cost = s_exit - l_exit
    gross = (p["credit"] - cost) * p["qty"]
    ch = charges((p["long_entry"] + s_exit) * p["qty"],
                 (p["short_entry"] + l_exit) * p["qty"], 4)
    row = {k: p.get(k) for k in TRADE_COLUMNS if k in p}
    row.update({"exit_ts": now.strftime("%Y-%m-%d %H:%M:%S"), "exit_reason": reason,
                "exit_cost": round(cost, 2), "gross_pnl": round(gross, 2),
                "charges": round(ch, 2), "net_pnl": round(gross - ch, 2),
                "short_exit": round(s_exit, 2), "long_exit": round(l_exit, 2)})
    append_trade(row)
    log(f"EXIT {reason}: cost {cost:.2f} gross {gross:,.0f} charges {ch:,.0f} "
        f"NET {gross-ch:,.0f}")
    state["position"] = None
    state["live"] = None
    save_state(state)


def try_exit(state, now):
    p = state["position"]
    px = ltps([p["short_key"], p["long_key"]])
    s_px, l_px = px.get(p["short_key"]), px.get(p["long_key"])
    if s_px is None or l_px is None:
        return
    cost = s_px - l_px
    mtm = (p["credit"] - cost) * p["qty"]
    state["live"] = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                     "short_px": round(s_px, 2), "long_px": round(l_px, 2),
                     "cost_to_close": round(cost, 2), "mtm": round(mtm, 2),
                     "max_loss": p["max_loss"],
                     "stop_at": round(-STOP_FRAC * p["max_loss"], 2),
                     "mtm_pct_maxloss": round(mtm / p["max_loss"] * 100, 1)}
    save_state(state)
    if mtm <= -STOP_FRAC * p["max_loss"]:
        close_position(state, s_px, l_px, "STOP", now)
        return
    exp = datetime.fromisoformat(p["expiry"]).date()
    if now.date() > exp or (now.date() == exp and now.time() >= EXIT_TIME):
        close_position(state, s_px, l_px, "EXPIRY", now)


def main():
    log("=" * 60)
    log(f"Ratio-Fluxer paper desk start | width {WIDTH} lots {LOTS} "
        f"stop {STOP_FRAC:.0%} of max loss | edr<={EDR_TH}")
    if not config.TOKEN:
        log("NO TOKEN - config/token.txt empty"); return
    state = load_state()
    chain = None; chain_day = None
    while True:
        now = datetime.now()
        if now.time() >= dtime(15, 35):
            log("session over"); break
        try:
            if chain_day != now.date():
                chain = option_chain(); chain_day = now.date()
                log(f"chain loaded: {len(chain)} contracts, "
                    f"expiries {weekly_expiries(chain)[:3]}")
            spot = spot_ltp()
            if spot is None:
                time.sleep(POLL_SEC); continue
            if state.get("position"):
                try_exit(state, now)
            else:
                exps = [e for e in weekly_expiries(chain) if e >= now.date()]
                if exps:
                    keys = [k for (e, K, kd, k, _l) in chain
                            if e == exps[0] and abs(K - spot) <= SPAN]
                    prices = ltps(keys) if keys else {}
                    try_entry(state, chain, spot, prices, now)
        except SystemExit:
            log("TOKEN EXPIRED - stopping"); break
        except Exception as exc:
            log(f"ERROR: {exc}")
        time.sleep(POLL_SEC)
    log("done")


if __name__ == "__main__":
    main()
