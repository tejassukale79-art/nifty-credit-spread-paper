"""Thin Upstox REST client with rate limiting and retries."""
import time
import requests
from urllib.parse import quote

import config

_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {config.TOKEN}",
    "Accept": "application/json",
})

# Upstox limits: 50/s, 500/min, 2000/30min. 1.15s/req = ~1560/30min, safely inside.
MIN_INTERVAL = 1.15
_last_call = 0.0


def _get(url, params=None, retries=10):
    global _last_call
    for attempt in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = _session.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            # a fully spent 30-min window needs a long wait
            time.sleep(min(120 * (attempt + 1), 600))
            continue
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        raise RuntimeError(f"HTTP {r.status_code} for {url}: {r.text[:300]}")
    raise RuntimeError(f"Retries exhausted for {url}")


def spot_candles_1min(from_date, to_date):
    """NIFTY index 1-min candles. Upstox v3 allows about a month per request."""
    key = quote(config.NIFTY_KEY, safe="")
    url = f"{config.BASE_URL}/v3/historical-candle/{key}/minutes/1/{to_date}/{from_date}"
    return _get(url)["data"]["candles"]


def expired_expiries():
    url = f"{config.BASE_URL}/v2/expired-instruments/expiries"
    return _get(url, {"instrument_key": config.NIFTY_KEY})["data"]


def expired_contracts(expiry_date):
    url = f"{config.BASE_URL}/v2/expired-instruments/option/contract"
    return _get(url, {"instrument_key": config.NIFTY_KEY,
                      "expiry_date": expiry_date})["data"]


def active_option_contracts(expiry_date):
    """Contract list for a live (not yet expired) expiry."""
    url = f"{config.BASE_URL}/v2/option/contract"
    return _get(url, {"instrument_key": config.NIFTY_KEY,
                      "expiry_date": expiry_date})["data"]


def live_candles_1min(instrument_key, from_date, to_date):
    """1-min candles for a live instrument (index or F&O), past days only."""
    key = quote(instrument_key, safe="")
    url = f"{config.BASE_URL}/v3/historical-candle/{key}/minutes/1/{to_date}/{from_date}"
    return _get(url)["data"]["candles"]


def intraday_candles_1min(instrument_key):
    """1-min candles for the current trading day."""
    key = quote(instrument_key, safe="")
    url = f"{config.BASE_URL}/v3/historical-candle/intraday/{key}/minutes/1"
    return _get(url)["data"]["candles"]


def expired_candles_1min(expired_key, from_date, to_date):
    key = quote(expired_key, safe="")
    url = (f"{config.BASE_URL}/v2/expired-instruments/historical-candle/"
           f"{key}/1minute/{to_date}/{from_date}")
    return _get(url)["data"]["candles"]
