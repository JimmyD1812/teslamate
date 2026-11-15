# Tesla command gateway (climate only)

A tiny companion service to the [read-only TeslaMate MCP server](../mcp-server/).
That server can *look* at the car's logged history and nothing more. This
gateway exists for the single command category the owner approved: **cabin
climate pre-conditioning** — "cool the car down on a hot day."

It is a separate process, with separate credentials, on a separate port,
from the read-only server. Either can be revoked or deleted without
touching the other.

## What it can do — the complete list

| Tool | Effect |
| --- | --- |
| `climate_start` | Start cabin climate, optionally at a target temperature; wakes the car first if needed |
| `climate_stop` | Stop cabin climate |
| `climate_set_temperature` | Set target temperature (Fahrenheit in, clamped to 58–84 °F, sent as °C) |

That is the entire public surface. There is no generic "send a command"
tool, and there never will be without a reviewed code change.

## What it can never do

The Fleet API's `vehicle_cmds` scope technically bundles climate together
with door locks, trunk, Sentry mode and remote start. This gateway makes
that bundle irrelevant: `_send_command()` in `server.py` is the single
choke point for every vehicle command, and it raises `PermissionError` for
anything outside the hard-coded allowlist
(`auto_conditioning_start`, `auto_conditioning_stop`, `set_temps`).
Unlock, remote start, frunk/trunk, horn — unreachable, not just unused.

Further guarantees:

- **One car.** The target VIN is configuration (`TESLA_VIN`). Tools take
  no vehicle argument, so a caller cannot aim the gateway elsewhere.
- **The signing key is not here.** Modern Teslas require signed commands.
  Signing is done by a local Tesla vehicle-command proxy
  (`tesla-http-proxy`), which holds the developer private key. The gateway
  holds only OAuth tokens (for API authorization) and talks to the proxy
  over the private container network.
- **Wake is internal.** A sleeping car cannot start climate, so
  `climate_start` may call `wake_up` as a sub-step. `wake_up` is not a
  tool and cannot be invoked on its own.
- **Audit log.** Every call, accepted or rejected, is appended as a JSON
  line to `audit.log` on the host (timestamp, tool, parameters, outcome).
- **Kill switch.** Unless `GATEWAY_ENABLED` is exactly `true`, every tool
  refuses and logs the refusal. Deleting the service or its tokens revokes
  it completely without affecting TeslaMate or the read-only MCP server.
- **Owner-gated by policy.** The assistant calls these tools only when the
  owner explicitly asks in chat. No schedules, no automations, no
  "helpful" pre-cooling on its own initiative.

## Prerequisites (install day)

Commands are a bigger lift than logging. All of this is the Fleet API
developer setup:

1. A Tesla developer application (developer.tesla.com) with:
   - the `vehicle_cmds` scope,
   - redirect URI `http://localhost:8767/callback` allowed,
   - the application's public key hosted at
     `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`
     and paired with the car as a virtual key (Tesla app prompt).
2. The matching private key on the host, given only to the proxy.
3. OAuth tokens for the gateway, minted once with
   `scripts/get_tokens.py` (scopes: `openid offline_access vehicle_cmds`)
   and copied to the gateway's data directory as `tokens.json`.
4. Fleet API usage is metered by Tesla with a monthly free allowance;
   occasional climate commands sit comfortably inside it.

## Install sketch

1. Finish the prerequisites above.
2. `cp command-gateway/.env.example .env` in your TeslaMate compose
   directory; fill in real values (VIN, client ID/secret, a fresh
   `GATEWAY_AUTH_TOKEN`, and leave `GATEWAY_ENABLED=false` for now).
3. Merge `command-gateway/docker-compose.example.yml` into your compose
   file, completing the `tesla-http-proxy` service per Tesla's official
   vehicle-command documentation (image/flags evolve; confirm at install).
4. Create `command-gateway-data/`, put `tokens.json` in it.
5. `docker compose up -d tesla-http-proxy tesla-climate-gateway`
6. Smoke test with the gateway still disabled: call `climate_stop` and
   confirm the refusal is written to `audit.log`. Then set
   `GATEWAY_ENABLED=true`, recreate the gateway container, and test one
   real `climate_start` / `climate_stop` pair while watching the car.
7. Expose to the assistant over Tailscale only (same pattern as the
   read-only server), registering this gateway as its **own** connector
   with its **own** bearer token.

## Relationship to the read-only server

| | TeslaMate MCP (`mcp-server/`) | This gateway |
| --- | --- | --- |
| Direction | Reads history | Sends 3 climate commands |
| Credentials | Read-only database user | Fleet API OAuth tokens (`vehicle_cmds`) |
| Port | 8765 | 8766 |
| Token | Its own | Its own |
| Can affect the car | Never | Climate only, when enabled |

## License

AGPL-3.0-or-later, same as TeslaMate.
