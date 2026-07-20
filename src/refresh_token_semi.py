"""Semi-automated Upstox token refresh: click a link, log in, done.

Nothing about your Upstox *account* is stored - no password, PIN, or TOTP
secret. The only thing kept on disk is your *app* credentials (API key,
API secret, redirect URI), which by themselves cannot log in or mint a
token: every refresh requires you to authenticate in the browser, which
produces a single-use auth code that this script exchanges for the token.

Morning routine (one command):
    python refresh_token_semi.py
-> browser opens the Upstox login
-> you log in (password / OTP / PIN, as usual)
-> Upstox redirects back with a one-time code
-> script captures it, gets the access token, writes config/token.txt
-> if a VM is configured, scp's the token there too

App credentials are read from (first found wins):
  1. environment variables
  2. config/upstox_app.env   (gitignored; see upstox_app.env.example)
Required: UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI
Optional push-to-VM: VM_SSH_HOST, VM_SSH_KEY, VM_TOKEN_PATH

Flags:
  --print-url   just print the login URL and exit (for a headless box:
                open it on any device, then rerun with --code "<redirected url>")
  --code URL    supply the redirected URL (or bare code) instead of the
                browser/localhost capture
  --no-browser  don't auto-open the browser
  --no-push     don't scp to the VM even if VM_* vars are set
"""
import argparse
import http.server
import subprocess
import sys
import threading
import urllib.parse as up
import webbrowser
from datetime import datetime

import requests

import config

AUTH_URL = f"{config.BASE_URL}/v2/login/authorization/dialog"
TOKEN_URL = f"{config.BASE_URL}/v2/login/authorization/token"
APP_ENV = config.CONFIG_DIR / "upstox_app.env"
REQUIRED = ["UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI"]


def log(msg):
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def load_app_config():
    import os
    cfg = {}
    if APP_ENV.exists():
        for line in APP_ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(REQUIRED) + ["VM_SSH_HOST", "VM_SSH_KEY", "VM_TOKEN_PATH"]:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        log(f"missing app credentials {missing}")
        log(f"create {APP_ENV} from config/upstox_app.env.example, or set env vars")
        sys.exit(1)
    return cfg


def build_auth_url(cfg, state="algo"):
    q = up.urlencode({
        "response_type": "code",
        "client_id": cfg["UPSTOX_CLIENT_ID"],
        "redirect_uri": cfg["UPSTOX_REDIRECT_URI"],
        "state": state,
    })
    return f"{AUTH_URL}?{q}"


def extract_code(text):
    """Accept a bare code or a full redirected URL and return the code."""
    text = text.strip()
    if not text:
        return None
    if "code=" in text:
        parsed = up.urlparse(text)
        qs = up.parse_qs(parsed.query)
        if "code" in qs:
            return qs["code"][0]
    return text  # assume the user pasted the bare code


def capture_via_localhost(redirect_uri, timeout=180):
    """If redirect_uri is localhost, run a one-shot server to grab ?code=."""
    parsed = up.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = up.parse_qs(up.urlparse(self.path).query)
            holder["code"] = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            ok = holder["code"] is not None
            self.wfile.write(
                (f"<html><body style='font-family:sans-serif;text-align:center;"
                 f"margin-top:80px'><h2>{'Token code received' if ok else 'No code found'}"
                 f"</h2><p>You can close this tab and return to the terminal.</p>"
                 f"</body></html>").encode())

        def log_message(self, *a):
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        log(f"could not bind localhost:{port} ({e}); falling back to manual paste")
        return None
    srv.timeout = timeout
    log(f"waiting for the login redirect on localhost:{port} ...")
    t = threading.Thread(target=srv.handle_request)
    t.start()
    t.join(timeout)
    srv.server_close()
    return holder.get("code")


def exchange(cfg, code):
    data = {
        "code": code,
        "client_id": cfg["UPSTOX_CLIENT_ID"],
        "client_secret": cfg["UPSTOX_CLIENT_SECRET"],
        "redirect_uri": cfg["UPSTOX_REDIRECT_URI"],
        "grant_type": "authorization_code",
    }
    headers = {"accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if r.status_code != 200:
        log(f"token exchange failed HTTP {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    tok = r.json().get("access_token")
    if not tok:
        log(f"no access_token in response: {r.text[:300]}")
        sys.exit(1)
    return tok


def push_to_vm(cfg, token_file):
    host, key, path = cfg.get("VM_SSH_HOST"), cfg.get("VM_SSH_KEY"), cfg.get("VM_TOKEN_PATH")
    if not (host and key and path):
        return
    cmd = ["scp", "-i", key, "-o", "StrictHostKeyChecking=accept-new",
           str(token_file), f"{host}:{path}"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log(f"token pushed to VM ({host}:{path})")
    except subprocess.CalledProcessError as e:
        log(f"scp to VM failed: {e.stderr.strip()[:200]}")
    except FileNotFoundError:
        log("scp not found; skipped VM push")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print-url", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    cfg = load_app_config()
    url = build_auth_url(cfg)

    if args.print_url:
        print(url)
        return

    code = None
    if args.code:
        code = extract_code(args.code)
    else:
        if not args.no_browser:
            log("opening Upstox login in your browser...")
            webbrowser.open(url)
        else:
            log(f"open this URL to log in:\n{url}")
        code = capture_via_localhost(cfg["UPSTOX_REDIRECT_URI"])
        if not code:
            pasted = input("paste the redirected URL (or the code): ")
            code = extract_code(pasted)

    if not code:
        log("no auth code obtained; aborting (config/token.txt unchanged)")
        sys.exit(1)

    log("exchanging code for access token...")
    token = exchange(cfg, code)
    token_file = config.CONFIG_DIR / "token.txt"
    token_file.write_text(token.strip())
    log(f"OK: wrote {token_file} (len={len(token)})")

    if not args.no_push:
        push_to_vm(cfg, token_file)


if __name__ == "__main__":
    main()
