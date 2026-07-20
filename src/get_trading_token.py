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


def write_status(ok, reason):
    """Write a dashboard-visible status file. This file is committed to a
    PUBLIC repo, so `reason` must be a short, non-sensitive label only -
    never the token, credentials, or raw exception text."""
    import json
    status = {
        "status": "success" if ok else "failed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
    }
    try:
        (config.RESULTS_DIR / "token_status.json").write_text(json.dumps(status, indent=1))
    except Exception as exc:      # never let status-writing mask the real result
        log(f"(could not write token_status.json: {exc})")


def _fail(reason, detail=None):
    """Log a detailed message locally, but record only a safe label."""
    log(f"FAILED: {detail or reason}")
    write_status(False, reason)
    return 1


def main():
    load_env_file()
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        return _fail("missing credentials",
                     f"missing credentials {missing} - run "
                     f"oracle/setup_trading_secrets.sh on the VM first")

    try:
        from upstox_totp import UpstoxTOTP
    except ImportError:
        return _fail("upstox-totp not installed",
                     "upstox-totp not installed (pip install upstox-totp, needs Python 3.12+)")

    try:
        upx = UpstoxTOTP()
        resp = upx.app_token.get_access_token()
    except Exception as exc:
        # detail (with exception text) stays in the local log only
        return _fail("login/token-exchange error",
                     f"login/token-exchange raised {type(exc).__name__}: {exc}")

    if not (resp and getattr(resp, "success", False) and resp.data):
        return _fail("no access token returned",
                     getattr(resp, "message", "unknown error, no access_token in response"))

    token = resp.data.access_token
    (config.CONFIG_DIR / "token.txt").write_text(token.strip())
    log(f"OK: new access token written to config/token.txt (len={len(token)})")
    write_status(True, f"token generated (len={len(token)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
