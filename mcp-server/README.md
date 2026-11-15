# TeslaMate MCP server (read-only)

A small [MCP](https://modelcontextprotocol.io) server that sits next to a
self-hosted [TeslaMate](https://github.com/teslamate-org/teslamate) install
and exposes the car's logged history to an AI assistant as a fixed set of
read-only tools. Ask questions in plain language — "how is my battery
degrading?", "what did charging cost me in March?", "why is my efficiency
worse on cold mornings?", "how much range am I losing parked?" — and the
assistant answers from your own logged data.

This is an add-on living in a fork of TeslaMate. It is purely additive: the
whole thing is this `mcp-server/` directory, it touches no upstream files,
and upstream TeslaMate updates merge as cleanly as they do in a vanilla fork.

## Security model

Read-only is enforced in three independent layers:

1. **Database grants** — the server connects as a dedicated PostgreSQL user
   (`teslamate_mcp`) with `SELECT`-only grants
   (see `sql/create_readonly_user.sql`).
2. **Session setting** — every connection is opened with
   `default_transaction_read_only=on`, so the database refuses writes even
   if a query were somehow wrong.
3. **Fixed queries** — every tool is a hand-written, parameterized `SELECT`.
   There is deliberately **no** free-form SQL tool.

Additionally:

- The server holds **no Tesla credentials** and never contacts Tesla's API.
  It reads TeslaMate's log database and nothing else. It cannot send
  commands to the car — there is no code path that could.
- HTTP requests require an `Authorization: Bearer <MCP_AUTH_TOKEN>` header.
- The port must only ever be reachable over a private network (home LAN or
  Tailscale). **Never** port-forward it or put it on the public internet.
- Secrets live in a local `.env` file on the host. They are never committed
  to git (`.env.example` contains placeholders only).

## Tools

| Tool | What it answers |
| --- | --- |
| `list_cars` | Which cars are being logged |
| `car_status` | Latest battery, range, odometer, location, tire pressures, online/asleep state |
| `recent_drives` | Drives in the last N days with distance, energy and efficiency estimates |
| `drive_detail` | One drive in depth, including its recorded position track |
| `recent_charges` | Charging sessions with energy added/used, charging losses, cost, AC vs DC |
| `battery_health` | Weekly projected full-charge range and implied capacity trend (estimate) |
| `efficiency_report` | Wh/mi overall, by outside temperature band, and by trip length |
| `vampire_drain` | Battery lost while parked, per asleep/offline stretch, as %/day |
| `monthly_summary` | A month at a glance: distance, drives, kWh charged (AC/DC split), cost |
| `top_places` | Most frequent destinations and charging locations |
| `charging_cost_report` | Recorded charging costs plus time-of-use estimates for unpriced sessions; Supercharger/DC breakout |
| `charge_curve_analysis` | One DC session's curve in numbers: peak kW, kW at 50%/80%, 10–80% time, vs the car's own median |
| `commute_insights` | Recurring routes: frequency, typical departure, median duration/efficiency, fastest departure hour |
| `anomaly_scan` | Findings vs the car's own norms: inefficient drives, drain spikes, charging losses, range drops |
| `battery_care_score` | Charging-habit score out of 100 with evidence: DC share, full charges, deep discharges, time parked >90% |
| `update_impact` | Efficiency before vs after each software update TeslaMate logged |

## Parity with MyTeslaMate (the paid hosted service)

MyTeslaMate sells hosted TeslaMate (about €4.99/month per vehicle) plus
extras. How this self-hosted setup stacks up, honestly:

| MyTeslaMate feature | Here |
| --- | --- |
| Hosted, no server to run | Your own always-on box (a Raspberry Pi is enough). The one thing money genuinely buys is skipping this. |
| Automatic backups | `scripts/backup.sh` — nightly `pg_dump` with rotation, run from cron. Your backups live on your disk, not their cloud. |
| Grafana dashboards | Same upstream TeslaMate dashboards, included. |
| AI chat assistant | This MCP server + your assistant. Deliberately **query-only**: MyTeslaMate's assistant can also *command* the car; this server is read-only by design and cannot. |
| TeslaMateAgile (time-of-use pricing) | Emulated: `charging_cost_report` prices unpriced sessions from a `TESLAMATE_TOU_RATES` schedule in the server config. |
| Supercharger costs imported automatically | Partially emulated: DC sessions are broken out with recorded costs where TeslaMate has them, and unpriced sessions are flagged rather than hidden. (Their price feed is theirs; there is no public equivalent to import.) |
| TeslaMateAPI + MQTT access | Upstream TeslaMate already publishes a live MQTT feed; this MCP covers the query/API side. |
| Free Fleet API proxy + raw token | Not emulated — that makes them a Tesla API provider with usage billing. If direct Fleet API access is ever needed, it is a separate project. |
| Sentry Guard break-in alerts | Not emulatable on the logging path: it relies on Fleet API telemetry events, which a read-only Owner API logger never sees. |

## What the paid services do not have

These tools exist because an assistant reading the whole logbook can compute
things a dashboard only displays:

- **`anomaly_scan`** — cross-referencing drives, drain, charging losses and
  range trend against the car's own baselines, and naming the likely causes.
- **`commute_insights`** — route-level patterns (including which departure
  hour is actually fastest) instead of one trip at a time.
- **`charge_curve_analysis`** — DC taper behavior as comparable numbers
  across sessions, so "this charger was slow" becomes provable.
- **`battery_care_score`** — habit coaching with the evidence attached, not
  just a degradation graph.
- **`update_impact`** — settles "the update ruined my efficiency" with the
  car's own before/after numbers.
- **Pattern questions in plain language** — any cross-cut of the above,
  asked conversationally, e.g. "are my winter tires costing me range on the
  work run?"

TeslaMate stores distances in km and temperatures in °C; results include
miles / °F conversions. Energy and efficiency figures are estimates derived
from rated-range deltas and the car's TeslaMate efficiency factor, the same
approach TeslaMate's own Grafana dashboards use.

## Install (on the host that runs TeslaMate)

Prerequisite: TeslaMate itself is installed and logging (see the upstream
documentation). Then:

1. Create the read-only database user (once), as TeslaMate's database owner:

   ```sh
   psql -U teslamate -d teslamate -f mcp-server/sql/create_readonly_user.sql
   ```

   Edit the password in the SQL file first, or change it afterwards with
   `ALTER USER teslamate_mcp WITH PASSWORD '...'`.

2. Create a `.env` next to your TeslaMate `docker-compose.yml` (see
   `.env.example`): set `TESLAMATE_MCP_DB_PASSWORD` to the password from
   step 1 and `MCP_AUTH_TOKEN` to a fresh random secret
   (`openssl rand -hex 32`).

3. Merge the `teslamate-mcp` service from `docker-compose.example.yml` into
   your TeslaMate `docker-compose.yml`.

4. Start it:

   ```sh
   docker compose up -d teslamate-mcp
   ```

5. Smoke test from the host:

   ```sh
   curl -s http://127.0.0.1:8765/mcp \
     -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
   ```

   Expect a JSON list of the tools above. A `401` means the token header is
   missing or wrong.

6. Make it reachable by your assistant over Tailscale only, e.g. with
   `tailscale serve` pointing at `127.0.0.1:8765`, then register the
   resulting private URL plus the bearer token as an MCP connector on the
   client side.

## Local development

```sh
cd mcp-server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TESLAMATE_DB_HOST=localhost TESLAMATE_DB_PASSWORD=... MCP_AUTH_TOKEN=dev \
  python server.py
```

## License

AGPL-3.0-or-later, same as TeslaMate.
