# NIFTY Credit Spread — Paper Trading

Automated **paper trading** of an overnight NIFTY credit-spread strategy
(replica of Stratzy's "Zen Credit Spread Overnight" signal logic), with the
best backtested configuration: stop-loss 15% of margin, exit 15:15 the next
trading day, 1 lot, entries 10:15–14:15.

**No real orders are placed — this only simulates fills at live prices.**

## How it runs

- `.github/workflows/paper-trade.yml` starts every weekday at ~09:40 IST on
  GitHub Actions and runs `src/paper_trade.py` until 15:30 IST.
- Trades append to `results/paper_trades.csv`; the open position lives in
  `results/paper_state.json` (committed back to the repo so overnight
  positions survive between daily runs).
- The dashboard (GitHub Pages, `docs/index.html`) reads those files and shows
  the open position, P&L stats, equity curve, and full trade table.

## Token — use the 1-year Analytics Token (recommended)

The script only reads market data, so Upstox's read-only **Analytics Token**
(valid 1 year) works — no daily refresh needed:

1. In your Upstox account: **Developer Apps → your app → Generate Analytics
   Token** (see the [docs](https://upstox.com/developer/api-documentation/analytics-token/)).
2. Put it in the repo secret once:
   **Settings → Secrets and variables → Actions → UPSTOX_TOKEN → Update**,
   or `gh secret set UPSTOX_TOKEN --body "PASTE_TOKEN_HERE"`.

Notes: generating a new Analytics Token revokes the previous one; it cannot
place orders (fine — paper trading never does). A normal daily access token
also works if you prefer. If the token is stale the run logs `TOKEN EXPIRED`
and trades nothing — update the secret and re-run from the Actions tab
(**paper-trade → Run workflow**).

## Local run (alternative)

```
# paste token into config/token.txt, then
cd src
python paper_trade.py
```

## Backtests

`src/backtest.py` (intraday square-off) and `src/backtest_overnight.py`
(overnight hold) run against 1-min option data downloaded by
`src/download_options.py` (not committed — ~5,000 parquet files).
Backtest conclusion (Sep 2024 – Jul 2026): the intraday version loses;
the overnight version with SL 15% of margin is the only profitable variant
(+Rs 28k Jan–Jul 2026 per lot, after charges). SL 10% whipsaws and loses;
SL 25% loses. See `results/trades_overnight_sl15.csv`.
