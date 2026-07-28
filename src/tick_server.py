"""Tick-live P&L for open positions. Fully SEPARATE from the trader:
it only READS results/paper_state.json and writes results/live_ticks.json.
It never places, modifies, or closes anything and shares no code path with
paper_trade.py's decision logic.

Flow: read open positions -> resolve their option legs to Upstox instrument
keys -> stream live LTP via the V3 market-data WebSocket -> on every tick,
recompute MTM = (credit - (short_ltp - long_ltp)) * lot per position (the
same formula the trader marks with) -> write live_ticks.json (throttled).

The dashboard fast-polls live_ticks.json, so the P&L moves in real time.
Runs during market hours (exits after STOP_TIME); a systemd timer starts it.
"""
import json
import sys
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

import requests
import upstox_client

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (ROOT / "config" / "token.txt").read_text().strip()
STATE_FILE = ROOT / "results" / "paper_state.json"
OUT_FILE = ROOT / "results" / "live_ticks.json"
LOG_FILE = ROOT / "results" / "tick_server.log"
NIFTY = "NSE_INDEX|Nifty 50"
STOP_TIME = dtime(15, 35)
WRITE_THROTTLE = 0.15          # seconds; cap file writes at ~7/sec
BASE = "https://api.upstox.com"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------- positions + instrument keys ----------

_contract_cache = {}


def option_keys(expiry):
    """(strike, kind) -> instrument_key for one expiry (cached)."""
    if expiry in _contract_cache:
        return _contract_cache[expiry]
    r = requests.get(f"{BASE}/v2/option/contract",
                     params={"instrument_key": NIFTY, "expiry_date": expiry},
                     headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
                     timeout=15)
    m = {}
    if r.status_code == 200:
        for c in r.json().get("data", []):
            m[(int(c["strike_price"]), c["instrument_type"])] = c["instrument_key"]
    else:
        log(f"contract fetch {expiry} -> HTTP {r.status_code}")
    _contract_cache[expiry] = m
    return m


def read_positions():
    """Open positions with their leg instrument keys. Returns (positions, keyset)."""
    try:
        raw = json.loads(STATE_FILE.read_text())
    except Exception:
        return [], set()
    positions = raw.get("positions") or ([raw["position"]] if raw.get("position") else [])
    out, keys = [], set()
    for p in positions:
        km = option_keys(p["expiry"][:10] if len(p.get("expiry", "")) >= 10 else p["expiry"])
        sk = km.get((int(p["short_strike"]), p["kind"]))
        lk = km.get((int(p["long_strike"]), p["kind"]))
        if not sk or not lk:
            log(f"no instrument key for {p['short_strike']}/{p['long_strike']}{p['kind']}")
            continue
        out.append({"type": p["type"], "kind": p["kind"], "credit": float(p["credit"]),
                    "lot": int(p["lot"]), "margin": float(p["margin"]),
                    "short_strike": p["short_strike"], "long_strike": p["long_strike"],
                    "entry_ts": p.get("entry_ts"), "exit_date": p.get("exit_date"),
                    "short_key": sk, "long_key": lk})
        keys.add(sk); keys.add(lk)
    return out, keys


# ---------- live tick stream ----------

class Ticker:
    def __init__(self):
        self.ltp = {}                      # instrument_key -> last price
        self.positions = []
        self.keys = set()
        self.lock = threading.Lock()
        self.last_write = 0.0
        self.logged_shape = False
        self.streamer = None

    def _extract_ltp(self, msg):
        """Pull {instrument_key: ltp} out of a decoded feed message, defensively."""
        found = {}
        feeds = msg.get("feeds") or msg.get("Feeds") or {}
        for k, v in feeds.items():
            if not isinstance(v, dict):
                continue
            node = v.get("ltpc") or v.get("fullFeed") or v
            ltp = None
            stack = [node]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    if "ltp" in cur:
                        ltp = cur["ltp"]; break
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
        with self.lock:
            rows, total = [], 0.0
            for p in self.positions:
                sc = self.ltp.get(p["short_key"]); lc = self.ltp.get(p["long_key"])
                if sc is None or lc is None:
                    continue
                mtm = (p["credit"] - (sc - lc)) * p["lot"]
                total += mtm
                rows.append({"type": p["type"], "kind": p["kind"],
                             "short_strike": p["short_strike"], "long_strike": p["long_strike"],
                             "short_px": round(sc, 2), "long_px": round(lc, 2),
                             "cost_to_close": round(sc - lc, 2),
                             "mtm": round(mtm, 2), "mtm_pct_margin": round(mtm / p["margin"] * 100, 2),
                             "sl_amount": round(0.15 * p["margin"], 2),
                             "entry_ts": p["entry_ts"], "exit_date": p["exit_date"]})
        payload = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                   "epoch_ms": int(now * 1000), "positions": rows, "total_mtm": round(total, 2)}
        tmp = OUT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(OUT_FILE)

    def sync_positions(self):
        """Re-read state; resubscribe if the leg set changed. Returns has_positions."""
        positions, keys = read_positions()
        with self.lock:
            self.positions = positions
        if keys != self.keys:
            new = keys - self.keys
            if self.streamer and new:
                try:
                    self.streamer.subscribe(list(new), "ltpc")
                    log(f"subscribed {len(new)} new legs; holding {len(positions)} position(s)")
                except Exception as e:
                    log(f"subscribe failed: {e}")
            self.keys = keys
        if not positions:
            self._maybe_write(force=True)   # publish idle/flat state
        return bool(positions)


def main():
    test_seconds = None
    if "--test-seconds" in sys.argv:
        test_seconds = int(sys.argv[sys.argv.index("--test-seconds") + 1])
    log("=" * 50)
    log("tick server start")

    t = Ticker()
    positions, keys = read_positions()
    t.positions, t.keys = positions, keys
    log(f"open positions at start: {len(positions)}")

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
        t.sync_positions()
        time.sleep(2)

    try:
        streamer.disconnect()
    except Exception:
        pass
    log("tick server done")


if __name__ == "__main__":
    main()
