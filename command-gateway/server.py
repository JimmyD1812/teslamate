#!/usr/bin/env python3
"""Tesla command gateway (climate ONLY) — companion to the TeslaMate MCP.

This is a deliberately tiny, separate service from the read-only TeslaMate
MCP server. It exists so an assistant can do exactly one category of thing
on request: manage cabin climate pre-conditioning. Nothing else.

Hard guarantees, enforced in this code (not by policy):

  1. ALLOWLIST: the only vehicle commands this process can construct are
     auto_conditioning_start, auto_conditioning_stop and set_temps.
     _send_command() raises for anything else — door_lock, door_unlock,
     remote_start_drive, actuate_trunk and every other Fleet API command
     are unreachable, not merely unused. wake_up exists only as an internal
     step of climate_start (a sleeping car cannot start climate), is never
     exposed as a tool, and cannot be invoked on its own.
  2. ONE CAR: the target VIN comes from configuration (TESLA_VIN). Tools
     accept no vehicle parameter, so a caller cannot aim the gateway at a
     different vehicle.
  3. SEPARATE CREDENTIALS: this service has its own MCP bearer token and
     its own Tesla Fleet API OAuth tokens, stored on the host only. It has
     no access to the TeslaMate database, and the read-only MCP server has
     no access to this service's tokens. Revoking either side leaves the
     other untouched.
  4. SIGNING KEY LIVES ELSEWHERE: commands are sent through a local Tesla
     vehicle-command proxy (tesla-http-proxy), which holds the developer
     private key and signs requests. This gateway never sees that key.
  5. AUDIT LOG: every tool call — accepted or rejected — is appended as a
     JSON line to the audit log before/after execution.
  6. KILL SWITCH: with GATEWAY_ENABLED unset or not exactly "true", every
     tool refuses to run.

Caller policy (outside this code, stated for the operator): the assistant
calls these tools only when the owner explicitly asks in chat. There are no
standing automations.
"""

import json
import os
import time
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP

# --- Configuration (environment; secrets live in the host's .env only) ---

PORT = int(os.environ.get("GATEWAY_PORT", "8766"))
AUTH_TOKEN = os.environ.get("GATEWAY_AUTH_TOKEN", "")
ENABLED = os.environ.get("GATEWAY_ENABLED", "false") == "true"
VIN = os.environ.get("TESLA_VIN", "")
PROXY_BASE = os.environ.get("TESLA_PROXY_BASE_URL", "https://tesla-http-proxy:4443")
TOKEN_FILE = os.environ.get("TESLA_TOKEN_FILE", "/data/tokens.json")
AUDIT_LOG = os.environ.get("GATEWAY_AUDIT_LOG", "/data/audit.log")
CLIENT_ID = os.environ.get("TESLA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")
AUTH_BASE = os.environ.get("TESLA_AUTH_BASE_URL", "https://auth.tesla.com")
FLEET_AUDIENCE = os.environ.get(
    "TESLA_FLEET_AUDIENCE", "https://fleet-api.prd.na.vn.cloud.tesla.com"
)
WAKE_FIRST = os.environ.get("WAKE_BEFORE_CLIMATE", "true") == "true"

ALLOWED_COMMANDS = frozenset(
    {"auto_conditioning_start", "auto_conditioning_stop", "set_temps"}
)

TEMP_MIN_F = 58.0
TEMP_MAX_F = 84.0

mcp = FastMCP("tesla-climate-gateway", host="0.0.0.0", port=PORT)


# --- Audit ---

def _audit(tool, detail, ok, info=""):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "detail": detail,
        "ok": ok,
        "info": str(info)[:300],
    }
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # never let logging failure block or fake a command result


# --- Tesla Fleet API token handling (this service's own OAuth tokens) ---

def _load_tokens():
    with open(TOKEN_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def _save_tokens(data):
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def _access_token():
    data = _load_tokens()
    if data.get("expires_at", 0) > time.time() + 60:
        return data["access_token"]
    resp = httpx.post(
        f"{AUTH_BASE}/oauth2/v3/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": data["refresh_token"],
            "audience": FLEET_AUDIENCE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    fresh = resp.json()
    data["access_token"] = fresh["access_token"]
    if fresh.get("refresh_token"):
        data["refresh_token"] = fresh["refresh_token"]
    data["expires_at"] = time.time() + int(fresh.get("expires_in", 28800))
    _save_tokens(data)
    return data["access_token"]


# --- Command transport (through the local signing proxy) ---

def _post(path, body=None):
    token = _access_token()
    resp = httpx.post(
        f"{PROXY_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body or {},
        timeout=30,
        verify=False,  # local proxy on the private container network, self-signed cert
    )
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:300]}
    return resp.status_code, payload


def _result_ok(status, payload):
    if status < 200 or status >= 300:
        return False
    inner = payload.get("response", payload)
    return not (isinstance(inner, dict) and inner.get("result") is False)


def _send_command(name, body=None):
    """The single choke point for vehicle commands. The allowlist is the
    security boundary: anything not in ALLOWED_COMMANDS cannot be sent."""
    if name not in ALLOWED_COMMANDS:
        raise PermissionError(
            f"Command '{name}' is not on this gateway's allowlist and can never be sent."
        )
    return _post(f"/api/1/vehicles/{VIN}/command/{name}", body)


def _wake():
    """Internal only — a prerequisite for climate commands on a sleeping
    car. Deliberately NOT a tool and NOT routed through _send_command."""
    return _post(f"/api/1/vehicles/{VIN}/wake_up")


def _require_ready(tool):
    if not ENABLED:
        _audit(tool, {}, False, "gateway disabled (GATEWAY_ENABLED is not 'true')")
        raise RuntimeError("The command gateway is disabled by its kill switch.")
    if not VIN:
        _audit(tool, {}, False, "TESLA_VIN not configured")
        raise RuntimeError("The command gateway has no target vehicle configured.")
    if not os.path.exists(TOKEN_FILE):
        _audit(tool, {}, False, "token file missing")
        raise RuntimeError("The command gateway has no Tesla tokens yet (install step pending).")


def _f_to_c(temp_f):
    clamped = max(TEMP_MIN_F, min(TEMP_MAX_F, float(temp_f)))
    celsius = (clamped - 32.0) * 5.0 / 9.0
    return round(celsius * 2) / 2.0, clamped


# --- Tools (the entire public surface of this service) ---

@mcp.tool()
def climate_start(temp_f: float | None = None) -> dict:
    """Start cabin climate (pre-cool or pre-heat). Optionally also set the
    target temperature in Fahrenheit. Wakes the car first if needed."""
    _require_ready("climate_start")
    detail = {"temp_f": temp_f}
    attempts = []
    if WAKE_FIRST:
        status, payload = _wake()
        attempts.append({"wake_up": status})
    status, payload = 0, {}
    for i in range(7):
        status, payload = _send_command("auto_conditioning_start")
        if _result_ok(status, payload):
            break
        if i < 6:
            time.sleep(10)  # car may still be waking up
    ok = _result_ok(status, payload)
    out = {"ok": ok, "action": "climate_start", "http_status": status, "response": payload}
    if ok and temp_f is not None:
        out["temperature"] = climate_set_temperature(temp_f)
    _audit("climate_start", detail, ok, f"http {status}; wake attempts: {attempts}")
    return out


@mcp.tool()
def climate_stop() -> dict:
    """Stop cabin climate."""
    _require_ready("climate_stop")
    status, payload = _send_command("auto_conditioning_stop")
    ok = _result_ok(status, payload)
    _audit("climate_stop", {}, ok, f"http {status}")
    return {"ok": ok, "action": "climate_stop", "http_status": status, "response": payload}


@mcp.tool()
def climate_set_temperature(temp_f: float) -> dict:
    """Set the cabin target temperature in Fahrenheit (clamped to
    58-84 F, sent to the car in half-degree Celsius steps, both zones)."""
    _require_ready("climate_set_temperature")
    celsius, clamped_f = _f_to_c(temp_f)
    status, payload = _send_command(
        "set_temps", {"driver_temp": celsius, "passenger_temp": celsius}
    )
    ok = _result_ok(status, payload)
    _audit(
        "climate_set_temperature",
        {"requested_f": temp_f, "sent_c": celsius},
        ok,
        f"http {status}",
    )
    return {
        "ok": ok,
        "action": "set_temps",
        "requested_f": temp_f,
        "applied_f": clamped_f,
        "sent_celsius": celsius,
        "http_status": status,
        "response": payload,
    }


class BearerAuthMiddleware:
    """Reject HTTP requests without the gateway's own bearer token."""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            headers = {
                k.decode().lower(): v.decode()
                for k, v in scope.get("headers", [])
            }
            if headers.get("authorization") != f"Bearer {self.token}":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [[b"content-type", b"text/plain"]],
                    }
                )
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    app = mcp.streamable_http_app()
    if AUTH_TOKEN:
        app = BearerAuthMiddleware(app, AUTH_TOKEN)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
