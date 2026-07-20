"""Refresh config/token.txt automatically using the upstox-totp package.

Upstox access tokens expire daily (~03:30 IST) and Upstox has no public
headless-login API - the normal path is: click through the browser login
(mobile number -> OTP/TOTP -> PIN), get a one-time auth code, exchange it
for an access token. upstox-totp reproduces that flow via direct HTTP
calls to the same endpoints the login page uses, driven by a TOTP secret
instead of a human typing a 6-digit code.

Credentials are read ONLY from environment variables, sourced from
config/upstox_secrets.env (chmod 600, gitignored, created once via
`oracle/setup_trading_secrets.sh` run directly on the VM over SSH - the
values never pass through chat history or any file on the dev machine).

Required env vars (see upstox_secrets.env.example):
  UPSTOX_USERNAME       10-digit mobile number registered with Upstox
  UPSTOX_PASSWORD       Upstox login password
  UPSTOX_PIN_CODE       6-digit trading PIN
  UPSTOX_TOTP_SECRET    base32 secret shown when enabling authenticator 2FA
  UPSTOX_CLIENT_ID      API key from developer.upstox.com
  UPSTOX_CLIENT_SECRET  API secret from developer.upstox.com
  UPSTOX_REDIRECT_URI   redirect URI registered for the app

Never logs secret values - only success/failure and error class/message.
Exit codes: 0 = token written, 1 = refresh failed (config/token.txt left
untouched so a stale-but-valid token from earlier in the day is not lost).
"""
import os
import sys
from datetime import datetime

import config

REQUIRED_VARS = [
    "UPSTOX_USERNAME", "UPSTOX_PASSWORD", "UPSTOX_PIN_CODE",
    "UPSTOX_TOTP_SECRET", "UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET",
    "UPSTOX_REDIRECT_URI",
]


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def load_env_file():
    env_file = config.CONFIG_DIR / "upstox_secrets.env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env_file()
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        log(f"FAILED: missing credentials {missing} - run "
            f"oracle/setup_trading_secrets.sh on the VM first")
        return 1

    try:
        from upstox_totp import UpstoxTOTP
    except ImportError:
        log("FAILED: upstox-totp not installed (pip install upstox-totp, needs Python 3.12+)")
        return 1

    try:
        upx = UpstoxTOTP()
        resp = upx.app_token.get_access_token()
    except Exception as exc:
        log(f"FAILED: login/token-exchange raised {type(exc).__name__}: {exc}")
        return 1

    if not (resp and getattr(resp, "success", False) and resp.data):
        log(f"FAILED: {getattr(resp, 'message', 'unknown error, no access_token in response')}")
        return 1

    token = resp.data.access_token
    (config.CONFIG_DIR / "token.txt").write_text(token.strip())
    log(f"OK: new access token written to config/token.txt (len={len(token)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
