"""Backtest July 2026 only (1st -> 16th; Jul 15-16 use the live Jul-21 expiry).

Spot history is truncated to 2026-06-19 which still leaves ~3000 minutes of
warm-up before July 1 -- more than the 800-minute alpha window, so July
signals match a full-history run exactly.
"""
import pandas as pd

import config
config.BACKTEST_START = "2026-07-01"
config.BACKTEST_END = "2026-07-16"

import signals
import backtest

_orig_load = signals.load_spot


def _load_spot_trunc():
    df = _orig_load()
    return df[df["ts"] >= pd.Timestamp("2026-06-19")].reset_index(drop=True)


signals.load_spot = _load_spot_trunc

if __name__ == "__main__":
    tr = backtest.run(tag="july2026")
    if not tr.empty:
        cols = ["date", "type", "entry_ts", "exit_ts", "exit_reason", "atm",
                "short_strike", "long_strike", "credit", "net_pnl"]
        print(tr[cols].to_string(index=False))
