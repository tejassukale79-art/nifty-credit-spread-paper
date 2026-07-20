#!/usr/bin/env bash
# One-time setup for the paper trader on an Oracle Always Free Ubuntu VM.
# Run as the default 'ubuntu' user:  bash setup.sh
set -euo pipefail

REPO_HTTPS="https://github.com/tejassukale79-art/nifty-credit-spread-paper.git"
REPO_SSH="git@github.com:tejassukale79-art/nifty-credit-spread-paper.git"
DIR="$HOME/paper"

sudo timedatectl set-timezone Asia/Kolkata
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git

if [ ! -d "$DIR" ]; then
    git clone "$REPO_HTTPS" "$DIR"
fi
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# deploy key so the VM can push results back to GitHub
if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
    ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" -C "oracle-paper-trader"
fi
ssh-keyscan github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
git remote set-url origin "$REPO_SSH"
git config user.name "paper-trade-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# systemd units
sudo cp oracle/paper-trade.service oracle/paper-trade.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paper-trade.timer

echo "============================================================"
echo "Setup done. Manual steps remain:"
echo "1. Add this DEPLOY KEY to the GitHub repo (Settings > Deploy keys,"
echo "   allow write access):"
cat "$HOME/.ssh/id_ed25519.pub"
echo "2. For automated daily tokens, run: bash oracle/setup_trading_secrets.sh"
echo "   (it stores credentials and enables the 08:00 IST token-refresh timer)."
echo "   Otherwise put a token into $DIR/config/token.txt manually."
echo "============================================================"
systemctl list-timers paper-trade.timer --no-pager
