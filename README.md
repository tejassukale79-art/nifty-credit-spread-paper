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

A dedicated systemd timer (`token-refresh.timer`) runs `src/get_trading_token.py`
every weekday at **08:00 IST**, logging in via the
[upstox-totp](https://github.com/batpool/upstox-totp) package (TOTP 2FA, no
browser) and writing a fresh `config/token.txt` — an hour before the 09:10
trading session needs it, so a failed refresh is visible with time to fix.
One-time setup, run **directly over SSH on the VM** — never paste these
credentials into a chat or a file on your dev machine:
```
ssh -i ssh-key-2026-07-19.key ubuntu@140.238.226.69
cd ~/paper && git pull && bash oracle/setup_trading_secrets.sh
```
It prompts for your mobile number, password, trading PIN, TOTP secret, and
developer app credentials (hidden input, not logged), stores them in
`config/upstox_secrets.env` (chmod 600, gitignored), runs a test refresh, and
installs+enables the 08:00 IST timer. Requires **authenticator-app 2FA enabled
on your Upstox account**. Check it any morning with
`systemctl list-timers token-refresh.timer` and `~/token-refresh.log`.

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
`src/run_2year.py` re-runs the overnight version across stop levels,
`src/report_2year.py` breaks the result down, and `src/run_dhanrules.py`
tests it against the rules Dhan's own trade log implies.

### The stop-loss is not part of the strategy

An earlier note here said SL 15% of margin was the only profitable variant
(+Rs 28k Jan–Jul 2026). **That is withdrawn.** Dhan's live trade log settles it:
all 348 legs across 174 trades carry `stopLoss: 0`. The 15% stop was invented
here — `config.py` still flags it *"TUNABLE: spec says 'based on margin
requirements' without a number"* — and it is expensive.

Over Dhan's own window (9 Jul 2025 – 16 Jul 2026), 1 lot, changing only that:

| Rule set | Trades | Win | Net / lot |
|---|---|---|---|
| Dhan, actual live record | 174 | 56.9% | **+Rs 156,712** |
| No stop (only change) | 145 | 64.1% | **+Rs 102,022** |
| Replica as written, SL 15% | 215 | 48.8% | +Rs 34,615 |

The stop costs **Rs 67,407 per lot** over thirteen months and drops the win rate
fifteen points: it closes the spreads that would have recovered by the 15:15
exit. Two other candidate corrections were tested and **rejected** — a tighter
entry window (Rs 30,691) and a 15:00 exit (Rs 86,372) both made it worse.

### How far to trust the backtest

The P&L engine is exact: against this repo's own saved run, every trade both
took on the same timestamp priced identically (188 of 188, max difference
Rs 0.0000). Signal *selection* is approximate — the replica agrees with Dhan on
direction on about 70% of shared trading days, and reproduced 6 of 11 live paper
trades over a two-week overlap. That is enough for aggregates over 406 trades
and **not** enough to tune parameters on. Closing that gap comes first.

Full write-up: `results/twoyear_report.html`.
