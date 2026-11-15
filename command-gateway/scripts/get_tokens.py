#!/usr/bin/env python3
"""One-time Fleet API token setup for the command gateway (install day).

Run this on a machine with a web browser (your laptop/desktop), NOT on the
server. It walks Tesla's OAuth flow for YOUR developer app and YOUR Tesla
account, and writes a tokens.json that you then copy onto the gateway host
(the Pi) into the gateway's data directory.

Prerequisites (developer.tesla.com):
  - A developer application whose allowed redirect URI includes
    http://localhost:8767/callback
  - The application's public key hosted and paired with the car (the
    .well-known/appspecific step), otherwise signed commands will fail
    later even with valid tokens.

Usage:
  TESLA_CLIENT_ID=... TESLA_CLIENT_SECRET=... python3 get_tokens.py

The client secret is read from the environment only — never put it in this
file or commit it anywhere. Scopes requested: openid, offline_access,
vehicle_cmds. Nothing else. The gateway's tokens.json is a bearer secret:
file permissions are set to 0600 and it must never be committed to git.
"""

import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_ID = os.environ.get("TESLA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")
AUTH_BASE = os.environ.get("TESLA_AUTH_BASE_URL", "https://auth.tesla.com")
AUDIENCE = os.environ.get(
    "TESLA_FLEET_AUDIENCE", "https://fleet-api.prd.na.vn.cloud.tesla.com"
)
REDIRECT_URI = "http://localhost:8767/callback"
SCOPES = "openid offline_access vehicle_cmds"
OUT_FILE = os.environ.get("TESLA_TOKEN_FILE", "tokens.json")

result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/callback" and "code" in params:
            result["code"] = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b"Got it - Tesla sign-in complete. You can close this tab."
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit(
            "Set TESLA_CLIENT_ID and TESLA_CLIENT_SECRET in the environment first."
        )
    state = secrets.token_urlsafe(16)
    authorize_url = (
        f"{AUTH_BASE}/oauth2/v3/authorize?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "state": state,
                "audience": AUDIENCE,
            }
        )
    )
    print("\n1. Open this URL and sign in with your Tesla account:\n")
    print("   " + authorize_url + "\n")
    print("2. Waiting for the sign-in to come back on localhost:8767 ...")
    server = HTTPServer(("127.0.0.1", 8767), Handler)
    while "code" not in result:
        server.handle_request()
    server.server_close()

    tokens = post_form(
        f"{AUTH_BASE}/oauth2/v3/token",
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "audience": AUDIENCE,
        },
    )
    out = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + int(tokens.get("expires_in", 28800)),
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    os.chmod(OUT_FILE, 0o600)
    print(f"\n3. Saved {OUT_FILE} (permissions 0600).")
    print("   Copy it to the gateway host's data directory as tokens.json.")
    print("   Treat it like a password. Never commit it to git.")


if __name__ == "__main__":
    main()
