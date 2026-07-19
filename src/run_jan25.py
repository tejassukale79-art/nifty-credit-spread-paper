"""Backtest Jan-2025 -> today with the paper-traded replica configuration
(Zen Credit Spread Overnight style): overnight exits, stop-loss 15% of margin.

Spot is truncated to 2024-12-15, leaving well over the 800-minute alpha
warm-up before Jan 1, so signals match a full-history run.
"""
import pandas as pd

import config
config.BACKTEST_START = "2025-01-01"
config.BACKTEST_END = "2026-07-16"
config.SL_PCT_OF_MARGIN = 0.15      # the paper trader's SL (best backtested)

import signals
import backtest_overnight

_orig_load = signals.load_spot


def _load_spot_trunc():
    df = _orig_load()
    return df[df["ts"] >= pd.Timestamp("2024-12-15")].reset_index(drop=True)


signals.load_spot = _load_spot_trunc

if __name__ == "__main__":
    backtest_overnight.run(tag="jan25_overnight_sl15")
