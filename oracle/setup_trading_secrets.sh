#!/usr/bin/env bash
# Run this DIRECTLY ON THE VM over SSH (never paste secrets into chat/Windows):
#   ssh -i ssh-key-2026-07-19.key ubuntu@140.238.226.69
#   cd ~/paper && bash oracle/setup_trading_secrets.sh
#
# Prompts for Upstox credentials with input hidden (read -s), writes them to
# config/upstox_secrets.env with chmod 600, and installs Python 3.12 +
# upstox-totp so get_trading_token.py can run daily without a browser.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "This stores your Upstox password, PIN, and TOTP secret on this VM so"
echo "the paper trader can refresh its access token every morning with no"
echo "manual login. Only proceed if you're comfortable with that trade-off."
echo "Nothing you type here is echoed to the screen or logged."
echo

read -rp "Upstox mobile number (10 digits): " UN
read -rsp "Upstox login password: " PW; echo
read -rsp "Upstox trading PIN: " PIN; echo
read -rsp "TOTP secret (from enabling authenticator 2FA in the Upstox app): " TOTP; echo
read -rp "Developer app Client ID (API key): " CID
read -rsp "Developer app Client Secret (API secret): " CSEC; echo
read -rp "Developer app Redirect URI: " RURI

ENV_FILE="config/upstox_secrets.env"
umask 177   # file created 600 from the start, never briefly world-readable
cat > "$ENV_FILE" <<EOF
UPSTOX_USERNAME=$UN
UPSTOX_PASSWORD=$PW
UPSTOX_PIN_CODE=$PIN
UPSTOX_TOTP_SECRET=$TOTP
UPSTOX_CLIENT_ID=$CID
UPSTOX_CLIENT_SECRET=$CSEC
UPSTOX_REDIRECT_URI=$RURI
EOF
chmod 600 "$ENV_FILE"
echo "wrote $ENV_FILE (permissions: $(stat -c '%a' "$ENV_FILE"))"

echo "installing Python 3.12 + upstox-totp..."
~/.local/bin/uv python install 3.12 >/dev/null
~/.local/bin/uv venv --python 3.12 .venv --clear >/dev/null
~/.local/bin/uv pip install -q --python .venv/bin/python -r requirements.txt upstox-totp

echo
echo "running a test token refresh..."
cd src
if ../.venv/bin/python get_trading_token.py; then
    echo "SUCCESS - config/token.txt now has a live token."
else
    echo "FAILED - check the error above (wrong credentials, TOTP not enabled"
    echo "on the Upstox account, or a login-flow change). Nothing was overwritten."
fi
