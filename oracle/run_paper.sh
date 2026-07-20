#!/usr/bin/env bash
# Daily paper-trading session on the VM. Mirrors the GitHub Actions job:
# pull latest, run the trader, sync results every 15 min and at the end.
set -uo pipefail

DIR="$HOME/paper"
cd "$DIR"

git pull --rebase origin main || true

cd src
if [ -f "$DIR/config/upstox_secrets.env" ]; then
    if ! "$DIR/.venv/bin/python" get_trading_token.py; then
        echo "$(date) token refresh failed - paper_trade.py will retry/skip if stale" \
            >> "$DIR/results/paper_trade.log"
    fi
else
    echo "$(date) no upstox_secrets.env - skipping auto token refresh, using existing config/token.txt" \
        >> "$DIR/results/paper_trade.log"
fi
cd "$DIR"

sync_results() {
    git add results/paper_* 2>/dev/null || true
    git commit -m "sync: $1 $(date +%H:%M)" >/dev/null 2>&1 || true
    git pull --rebase origin main >/dev/null 2>&1 || true
    git push origin main >/dev/null 2>&1 || true
}

cd src
"$DIR/.venv/bin/python" paper_trade.py &
PID=$!
cd "$DIR"

while kill -0 $PID 2>/dev/null; do
    sleep 900
    kill -0 $PID 2>/dev/null || break
    sync_results "intraday"
done
wait $PID || true

sync_results "end of day $(date +%F)"
echo "session finished $(date)"
