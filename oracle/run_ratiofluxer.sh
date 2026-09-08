#!/usr/bin/env bash
# Daily Ratio-Fluxer paper session. Uses the SAME config/token.txt that the
# 08:00 token-refresh.timer writes - no separate token handling.
set -uo pipefail
DIR="$HOME/paper"
cd "$DIR"
git pull --rebase origin main || true

if [ ! -s "$DIR/config/token.txt" ]; then
    echo "$(date) no token - skipping session" >> "$DIR/results/rf_trade.log"
    exit 0
fi

sync_results() {
    git add results/rf_* 2>/dev/null || true
    git commit -m "ratiofluxer sync: $1 $(date +%H:%M)" >/dev/null 2>&1 || true
    git pull --rebase origin main >/dev/null 2>&1 || true
    git push origin main >/dev/null 2>&1 || true
}

cd src
"$DIR/.venv/bin/python" ratiofluxer_trade.py &
PID=$!
cd "$DIR"
while kill -0 $PID 2>/dev/null; do
    sleep 300
    kill -0 $PID 2>/dev/null || break
    sync_results "intraday"
done
wait $PID || true
sync_results "end of day $(date +%F)"
echo "ratiofluxer session finished $(date)"
