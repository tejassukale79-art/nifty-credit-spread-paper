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

## Daily Upstox token — three options

The Upstox access token expires every morning (~03:30 IST). Pick the trade-off
you're comfortable with; each writes a fresh token to `config/token.txt`.

### 1. Semi-automated — click a link each morning (recommended)

`src/refresh_token_semi.py` stores **only your app credentials** (API
key/secret/redirect URI) — never your account password, PIN, or TOTP secret.
Each morning you run one command, log in through the browser, and it captures
the one-time code and exchanges it for the token:
```
# one-time: copy config/upstox_app.env.example -> config/upstox_app.env, fill in
cd src
python refresh_token_semi.py
```
The browser opens the Upstox login; after you log in it captures the code
(automatically if your redirect URI is a `localhost` one, otherwise you paste
the redirected URL back), writes `config/token.txt`, and — if `VM_SSH_*` are
set in `upstox_app.env` — `scp`s it to the VM. Run it before 09:10 IST.
Headless box? `python refresh_token_semi.py --print-url`, open that on any
device, then rerun with `--code "<redirected url>"`.

The app credentials alone cannot log in or mint a token — every refresh
requires your browser login — so nothing account-sensitive is ever stored.

### 2. Fully automated — zero daily effort (higher trust)

`oracle/run_paper.sh` calls `src/get_trading_token.py` at the start of every
session, logging in via the [upstox-totp](https://github.com/batpool/upstox-totp)
package (TOTP 2FA, no browser). One-time setup, run **directly over SSH on the
VM** — never paste these credentials into a chat or a file on your dev machine:
```
ssh -i ssh-key-2026-07-19.key ubuntu@140.238.226.69
cd ~/paper && bash oracle/setup_trading_secrets.sh
```
It prompts for your mobile number, password, trading PIN, TOTP secret, and
developer app credentials (hidden input, not logged), stores them in
`config/upstox_secrets.env` (chmod 600, gitignored), and runs a test refresh.
Requires **authenticator-app 2FA enabled on your Upstox account**.

This stores your password/PIN/TOTP secret on the VM — a real trust decision:
whoever controls that VM can log into your Upstox account. Only use it on
infrastructure you trust.

### 3. Manual — no setup

Log in to Upstox, copy the access token, and `scp` it to the VM's
`config/token.txt` (or SSH in and paste it). If the token is stale the run
logs `TOKEN EXPIRED` and trades nothing.

`oracle/run_paper.sh` auto-refreshes only if `config/upstox_secrets.env`
(option 2) exists; the semi-automated and manual options write the token
before/independently of the session, and the runner just uses whatever is in
`config/token.txt`.

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
