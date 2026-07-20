#!/usr/bin/env bash
# Fully-automated Upstox token refresh, invoked by token-refresh.timer at
# 08:00 IST Mon-Fri. Logs in via TOTP (no browser) and writes a fresh
# config/token.txt, well before the 09:10 trading session needs it.
set -uo pipefail
DIR="$HOME/paper"

cd "$DIR"
git pull --rebase origin main >/dev/null 2>&1 || true

cd "$DIR/src"
"$DIR/.venv/bin/python" get_trading_token.py
