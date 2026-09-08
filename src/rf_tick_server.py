"""Tick-live P&L for the Ratio-Fluxer desk.

Fully SEPARATE from the trader: it only READS results/rf_state.json and writes
results/rf_live_ticks.json. It never opens, closes or alters a position and
shares no code path with ratiofluxer_trade.py's decision logic.

The trader already stores each leg's instrument_key in the state, so this just
subscribes to those two keys plus NIFTY spot and recomputes
MTM = (credit - (short_ltp - long_ltp)) * qty on every tick.
"""
import json
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
import upstox_client

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (ROOT / "config" / "token.txt").read_text().strip()
STATE_FILE = ROOT / "results" / "rf_state.json"
OUT_FILE = ROOT / "results" / "rf_live_ticks.json"
LOG_FILE = ROOT / "results" / "rf_tick_server.log"
NIFTY = "NSE_INDEX|Nifty 50"
STOP_TIME = dtime(15, 35)
WRITE_THROTTLE = 0.15
STOP_FRAC = 0.35
BASE = "https://api.upstox.com"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_position():
    """Open position with its leg keys, or (None, empty) when flat."""
    try:
        raw = json.loads(STATE_FILE.read_text())
    except Exception:
        return None, set()
    p = raw.get("position")
    if not p or not p.get("short_key") or not p.get("long_key"):
        return None, set()
    return p, {p["short_key"], p["long_key"]}


class Ticker:
    def __init__(self):
        self.ltp = {}
        self.pos = None
        self.keys = set()
        self.lock = threading.Lock()
        self.last_write = 0.0
        self.streamer = None
        self.prev_close = None
        self._ref_tried = 0.0
        self.logged_shape = False

    def fetch_prev_close(self):
        """Previous session's close - the reference NSE/brokers quote change against."""
        if self.prev_close is not None or time.time() - self._ref_tried < 300:
            return
        self._ref_tried = time.time()
        try:
            today = datetime.now().date()
            frm = (today - timedelta(days=12)).isoformat()
            r = requests.get(
                f"{BASE}/v3/historical-candle/{quote(NIFTY, safe='')}/days/1/{today}/{frm}",
                headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
                timeout=15)
            candles = sorted(r.json().get("data", {}).get("candles", []), key=lambda c: c[0])
            prior = [c for c in candles if c[0][:10] < today.isoformat()]
            if prior:
                self.prev_close = float(prior[-1][4])
                log(f"NIFTY prev close ({prior[-1][0][:10]}): {self.prev_close}")
        except Exception as exc:
            log(f"prev-close fetch failed: {exc}")

    def _extract_ltp(self, msg):
        found = {}
        for k, v in (msg.get("feeds") or msg.get("Feeds") or {}).items():
            if not isinstance(v, dict):
                continue
            node = v.get("ltpc") or v.get("fullFeed") or v
            ltp, stack = None, [node]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    if "ltp" in cur:
                        ltp = cur["ltp"]
                        break
                    stack.extend(cur.values())
            if ltp is not None:
                try:
                    found[k] = float(ltp)
                except (TypeError, ValueError):
                    pass
        return found

    def on_message(self, msg):
        if not self.logged_shape:
            log(f"first feed message keys: {list(msg)[:6]}")
            self.logged_shape = True
        got = self._extract_ltp(msg)
        if not got:
            return
        with self.lock:
            self.ltp.update(got)
        self._maybe_write()

    def _maybe_write(self, force=False):
        now = time.time()
        if not force and now - self.last_write < WRITE_THROTTLE:
            return
        self.last_write = now
        row = None
        with self.lock:
            p = self.pos
            if p:
                sc = self.ltp.get(p["short_key"])
                lc = self.ltp.get(p["long_key"])
                if sc is not None and lc is not None:
                    cost = sc - lc
                    mtm = (p["credit"] - cost) * p["qty"]
                    row = {"kind": p["kind"], "short_strike": p["short_strike"],
                           "long_strike": p["long_strike"], "short_px": round(sc, 2),
                           "long_px": round(lc, 2), "cost_to_close": round(cost, 2),
                           "mtm": round(mtm, 2), "max_loss": p["max_loss"],
                           "stop_at": round(-STOP_FRAC * p["max_loss"], 2),
                           "mtm_pct_maxloss": round(mtm / p["max_loss"] * 100, 1),
                           "entry_ts": p.get("entry_ts"), "expiry": p.get("expiry"),
                           "credit": p.get("credit"), "qty": p.get("qty")}
            spot = self.ltp.get(NIFTY)
        ref = self.prev_close
        chg = (spot - ref) if (spot is not None and ref) else None
        payload = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                   "epoch_ms": int(now * 1000), "position": row,
                   "total_mtm": row["mtm"] if row else None,
                   "spot": round(spot, 2) if spot is not None else None,
                   "spot_prev_close": ref,
                   "spot_chg": round(chg, 2) if chg is not None else None,
                   "spot_chg_pct": round(chg / ref * 100, 2) if chg is not None else None}
        tmp = OUT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(OUT_FILE)

    def sync(self):
        self.fetch_prev_close()
        pos, keys = read_position()
        keys = keys | {NIFTY}
        with self.lock:
            self.pos = pos
        if keys != self.keys:
            new = keys - self.keys
            if self.streamer and new:
                try:
                    self.streamer.subscribe(list(new), "ltpc")
                    state = "open" if pos else "flat"
                    log(f"subscribed {len(new)} key(s); position: {state}")
                except Exception as e:
                    log(f"subscribe failed: {e}")
            self.keys = keys
        if not pos:
            self._maybe_write(force=True)


def main():
    test_seconds = None
    if "--test-seconds" in sys.argv:
        test_seconds = int(sys.argv[sys.argv.index("--test-seconds") + 1])
    log("=" * 50)
    log("ratio-fluxer tick server start")
    pos, keys = read_position()
    keys = keys | {NIFTY}
    t = Ticker()
    t.pos, t.keys = pos, keys
    log(f"position at start: {'open' if pos else 'flat'} (+ NIFTY spot)")

    cfg = upstox_client.Configuration()
    cfg.access_token = TOKEN
    streamer = upstox_client.MarketDataStreamerV3(
        upstox_client.ApiClient(cfg), list(keys), "ltpc")
    t.streamer = streamer
    streamer.auto_reconnect(True, interval=5, retry_count=100)
    streamer.on("message", t.on_message)
    streamer.on("error", lambda e: log(f"stream error: {e}"))
    streamer.on("open", lambda *_: log("stream open"))
    threading.Thread(target=streamer.connect, daemon=True).start()
    t._maybe_write(force=True)

    started = time.time()
    while True:
        if test_seconds is not None:
            if time.time() - started > test_seconds:
                break
        elif datetime.now().time() >= STOP_TIME:
            break
        t.sync()
        time.sleep(2)
    try:
        streamer.disconnect()
    except Exception:
        pass
    log("ratio-fluxer tick server done")


if __name__ == "__main__":
    main()
