# NIFTY Credit Spread — Paper Trading

Automated **paper trading** of an overnight NIFTY credit-spread strategy
(replica of Stratzy's "Zen Credit Spread Overnight" signal logic), with the
best backtested configuration: stop-loss 15% of margin, exit 15:15 the next
trading day, 1 lot, entries 10:15–14:15.

**No real orders are placed — this only simulates fills at live prices.**

## How it runs

- The trading engine runs on an **Oracle Cloud Always Free VM** (`oracle/`
  directory), not GitHub Actions. A systemd timer (`paper-trade.timer`) starts
  `oracle/run_paper.sh` every weekday at **09:10 IST** — fixed start time, no
  cron-delay risk, no 6-hour job cap. See `oracle/setup.sh` for a from-scratch
  VM deploy.
- The VM pushes `results/paper_trades.csv` and `results/paper_state.json`
  back to this repo every 15 minutes during the session and once at the end
  of day, via a write-scoped deploy key.
- The dashboard (GitHub Pages, `docs/index.html`) reads those files and shows
  the open position, P&L stats, equity curve, and full trade table. GitHub's
  role is now purely results storage + dashboard hosting — the old
  `.github/workflows/paper-trade*.yml` jobs are disabled and kept only as a
  manual fallback (`gh workflow enable paper-trade.yml`).

## Daily Upstox token — automatic (recommended) or manual

The Upstox access token expires every morning (~03:30 IST). Two ways to keep
it fresh:

**Automatic (once set up, zero daily effort):** `oracle/run_paper.sh` calls
`src/get_trading_token.py` at the start of every session, which logs in via
the [upstox-totp](https://github.com/batpool/upstox-totp) package (TOTP
2FA, no browser) and writes a fresh token to `config/token.txt`. One-time
setup, run **directly over SSH on the VM** — never paste these credentials
into a chat or a file on your dev machine:
```
ssh -i ssh-key-2026-07-19.key ubuntu@140.238.226.69
cd ~/paper && bash oracle/setup_trading_secrets.sh
```
It prompts for your Upstox mobile number, password, trading PIN, TOTP
secret, and developer app credentials (hidden input, not logged), stores
them in `config/upstox_secrets.env` (chmod 600, gitignored), and runs a test
refresh. Requires **authenticator-app 2FA enabled on your Upstox account**
(Upstox app → Profile → Security → Two-Factor Authentication) — the base32
secret shown there is `UPSTOX_TOTP_SECRET`.

Storing your password/PIN/TOTP secret on the VM is a real trust decision:
whoever controls that VM can log into your Upstox account. Only set this up
on infrastructure you trust, and treat `config/upstox_secrets.env` like a
password vault (it never leaves the VM and is never committed).

**Manual (no setup, daily effort):** if `config/upstox_secrets.env` doesn't
exist, the automatic step is skipped and the run falls back to whatever is
in `config/token.txt`. Log in to Upstox, copy the access token, and either
`scp` it to the VM's `config/token.txt` or SSH in and paste it directly.
If the token is stale the run logs `TOKEN EXPIRED` and trades nothing.

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
