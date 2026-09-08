"""Real broker margin from Upstox, for capital reporting.

Why this exists: a credit spread's (width - credit) x qty is the MAX LOSS, not
what a broker blocks. SPAN+exposure with hedge benefit is materially larger -
about 2.4x for a 400-point NIFTY spread - so quoting max loss as "capital"
understates the funding requirement.

Two gotchas the API hides:
  * read `final_margin`, NOT `required_margin`. required_margin is the legs
    summed with no netting (Rs 856k for a spread whose real margin is Rs 237k).
  * quantity is in UNITS for NSE (65 = one NIFTY lot) but in LOTS for MCX
    (1 = one CRUDEOIL lot). Passing units for MCX overstates by the lot size.

Never let this break trading: every call is wrapped and returns None on any
failure, and callers must treat None as "unknown", not zero.
"""
from __future__ import annotations

import time

import requests

import config

_URL = f"{config.BASE_URL}/v2/charges/margin"
_cache: dict = {}
_CACHE_TTL = 3600.0      # margins move slowly; one call per structure per hour


def _headers():
    return {"Authorization": f"Bearer {config.TOKEN}",
            "Accept": "application/json", "Content-Type": "application/json"}


def blocked(legs, timeout=15):
    """Margin the broker actually blocks for `legs`, or None if unavailable.

    legs: [(instrument_key, quantity, "BUY"|"SELL"), ...]
          quantity in UNITS for NSE keys, in LOTS for MCX keys.
    """
    if not legs or not config.TOKEN:
        return None
    key = tuple(sorted((k, int(q), s) for k, q, s in legs))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        payload = {"instruments": [
            {"instrument_key": k, "quantity": int(q),
             "transaction_type": s, "product": "D"} for k, q, s in legs]}
        r = requests.post(_URL, headers=_headers(), json=payload, timeout=timeout)
        if r.status_code != 200:
            return None
        d = r.json().get("data") or {}
        # final_margin nets the hedge; required_margin does not
        val = d.get("final_margin")
        val = round(float(val)) if val is not None else None
    except Exception:
        return None
    _cache[key] = (time.time(), val)
    return val


def spread(short_key, long_key, qty):
    """Blocked margin for a two-leg credit spread (sell near, buy far)."""
    return blocked([(short_key, qty, "SELL"), (long_key, qty, "BUY")])
