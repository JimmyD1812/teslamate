#!/usr/bin/env python3
"""TeslaMate MCP server (read-only).

Exposes the data TeslaMate has logged about your car as MCP tools, so an
assistant can answer questions like "how is my battery degrading?", "what did
charging cost me last month?" or "why is my efficiency worse this week?".

Safety model (defense in depth):
  1. Connect to PostgreSQL with a dedicated user that has SELECT-only grants
     (see sql/create_readonly_user.sql).
  2. Every connection is opened with default_transaction_read_only=on, so the
     database itself refuses writes even if a query were wrong.
  3. Tools are a fixed set of hand-written, parameterized SELECT queries.
     There is deliberately NO free-form SQL tool.
  4. The server holds no Tesla credentials and never talks to Tesla's API.
     It only reads TeslaMate's log database.
  5. HTTP access requires a bearer token (MCP_AUTH_TOKEN) and the port must
     only be reachable over a private network (LAN / Tailscale). Never expose
     it to the public internet.

TeslaMate stores distances in km and temperatures in degC. Tool results
include miles / degF conversions alongside the raw values.
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from mcp.server.fastmcp import FastMCP

DB = {
    "host": os.environ.get("TESLAMATE_DB_HOST", "database"),
    "port": int(os.environ.get("TESLAMATE_DB_PORT", "5432")),
    "dbname": os.environ.get("TESLAMATE_DB_NAME", "teslamate"),
    "user": os.environ.get("TESLAMATE_DB_USER", "teslamate_mcp"),
    "password": os.environ.get("TESLAMATE_DB_PASSWORD", ""),
}
PORT = int(os.environ.get("MCP_PORT", "8765"))
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

KM_TO_MI = 0.621371

mcp = FastMCP("teslamate", host="0.0.0.0", port=PORT)


@contextmanager
def conn():
    """Open a read-only connection to the TeslaMate database."""
    with psycopg.connect(
        **DB,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
        connect_timeout=10,
    ) as c:
        yield c


def q(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchall()


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def resolve_car(car_id):
    """Return the car row for car_id, or the first car when omitted."""
    if car_id:
        row = q1("SELECT * FROM cars WHERE id = %s", (car_id,))
    else:
        row = q1("SELECT * FROM cars ORDER BY display_priority, id LIMIT 1")
    if row is None:
        raise ValueError("No matching car found in the TeslaMate database.")
    return row


def mi(km):
    return None if km is None else round(float(km) * KM_TO_MI, 2)


def fahr(c):
    return None if c is None else round(float(c) * 9.0 / 5.0 + 32.0, 1)


def place_name(geofence, address):
    if geofence:
        return geofence
    return address


def drive_row(r, efficiency):
    dist_km = float(r["distance"]) if r["distance"] is not None else None
    rated_used = None
    if r["start_rated_range_km"] is not None and r["end_rated_range_km"] is not None:
        rated_used = float(r["start_rated_range_km"]) - float(r["end_rated_range_km"])
    energy_kwh = None
    wh_per_mi = None
    if rated_used is not None and efficiency:
        energy_kwh = round(rated_used * float(efficiency), 2)
        if dist_km:
            wh_per_mi = round(energy_kwh * 1000.0 / (dist_km * KM_TO_MI), 0)
    return {
        "drive_id": r["id"],
        "start": r["start_date"].isoformat() if r["start_date"] else None,
        "end": r["end_date"].isoformat() if r["end_date"] else None,
        "from": place_name(r["start_geofence"], r["start_address"]),
        "to": place_name(r["end_geofence"], r["end_address"]),
        "distance_mi": mi(dist_km),
        "distance_km": round(dist_km, 2) if dist_km is not None else None,
        "duration_min": r["duration_min"],
        "battery_used_pct": (
            None
            if r["start_battery_level"] is None or r["end_battery_level"] is None
            else r["start_battery_level"] - r["end_battery_level"]
        ),
        "est_energy_kwh": energy_kwh,
        "est_wh_per_mi": wh_per_mi,
        "outside_temp_avg_f": fahr(r["outside_temp_avg"]),
        "speed_max_mph": mi(r["speed_max"]),
    }


DRIVE_SELECT = """
SELECT d.id, d.start_date, d.end_date, d.distance, d.duration_min,
       d.speed_max, d.outside_temp_avg,
       d.start_rated_range_km, d.end_rated_range_km,
       ps.battery_level AS start_battery_level,
       pe.battery_level AS end_battery_level,
       gs.name AS start_geofence, ge.name AS end_geofence,
       ads.display_name AS start_address, ade.display_name AS end_address
FROM drives d
LEFT JOIN positions ps ON ps.id = d.start_position_id
LEFT JOIN positions pe ON pe.id = d.end_position_id
LEFT JOIN geofences gs ON gs.id = d.start_geofence_id
LEFT JOIN geofences ge ON ge.id = d.end_geofence_id
LEFT JOIN addresses ads ON ads.id = d.start_address_id
LEFT JOIN addresses ade ON ade.id = d.end_address_id
"""


@mcp.tool()
def list_cars() -> list:
    """List the cars TeslaMate is logging (id, name, model, trim, VIN suffix)."""
    rows = q(
        "SELECT id, name, model, trim_badging, marketing_name, exterior_color,"
        " efficiency, right(vin, 6) AS vin_suffix FROM cars ORDER BY id"
    )
    return rows


@mcp.tool()
def car_status(car_id: int | None = None) -> dict:
    """Current status of the car: latest battery level, odometer, location,
    tire pressures, temperatures, whether it is online/asleep and since when,
    plus the most recent drive and charge."""
    car = resolve_car(car_id)
    cid = car["id"]
    pos = q1(
        "SELECT date, latitude, longitude, battery_level, usable_battery_level,"
        " rated_battery_range_km, odometer, outside_temp, inside_temp,"
        " tpms_pressure_fl, tpms_pressure_fr, tpms_pressure_rl, tpms_pressure_rr"
        " FROM positions WHERE car_id = %s ORDER BY date DESC LIMIT 1",
        (cid,),
    )
    state = q1(
        "SELECT state, start_date FROM states WHERE car_id = %s"
        " ORDER BY start_date DESC LIMIT 1",
        (cid,),
    )
    out = {
        "car": {
            "id": cid,
            "name": car["name"],
            "model": car["model"],
            "trim": car["trim_badging"],
        },
        "state": state["state"] if state else None,
        "state_since": state["start_date"].isoformat() if state else None,
    }
    if pos:
        out["latest_position_at"] = pos["date"].isoformat()
        out["battery_level_pct"] = pos["battery_level"]
        out["usable_battery_level_pct"] = pos["usable_battery_level"]
        out["rated_range_mi"] = mi(pos["rated_battery_range_km"])
        out["odometer_mi"] = mi(pos["odometer"])
        out["latitude"] = float(pos["latitude"]) if pos["latitude"] is not None else None
        out["longitude"] = float(pos["longitude"]) if pos["longitude"] is not None else None
        out["outside_temp_f"] = fahr(pos["outside_temp"])
        out["inside_temp_f"] = fahr(pos["inside_temp"])
        out["tpms_psi"] = {
            "front_left": _bar_to_psi(pos["tpms_pressure_fl"]),
            "front_right": _bar_to_psi(pos["tpms_pressure_fr"]),
            "rear_left": _bar_to_psi(pos["tpms_pressure_rl"]),
            "rear_right": _bar_to_psi(pos["tpms_pressure_rr"]),
        }
    return out


def _bar_to_psi(bar):
    return None if bar is None else round(float(bar) * 14.5038, 1)


@mcp.tool()
def recent_drives(days: int = 7, limit: int = 20, car_id: int | None = None) -> list:
    """Drives from the last N days, newest first: where from/to, distance,
    duration, battery used, estimated energy and efficiency, temperature."""
    car = resolve_car(car_id)
    rows = q(
        DRIVE_SELECT
        + "WHERE d.car_id = %s AND d.start_date >= now() - make_interval(days => %s)"
          " ORDER BY d.start_date DESC LIMIT %s",
        (car["id"], days, limit),
    )
    return [drive_row(r, car["efficiency"]) for r in rows]


@mcp.tool()
def drive_detail(drive_id: int, car_id: int | None = None) -> dict:
    """Detail for one drive: summary plus track statistics from its logged
    positions (points recorded, peak power, elevation change)."""
    car = resolve_car(car_id)
    row = q1(DRIVE_SELECT + "WHERE d.id = %s AND d.car_id = %s", (drive_id, car["id"]))
    if row is None:
        raise ValueError(f"Drive {drive_id} not found.")
    out = drive_row(row, car["efficiency"])
    stats = q1(
        "SELECT count(*) AS points, max(power) AS power_max_kw,"
        " min(power) AS power_min_kw, max(elevation) - min(elevation) AS elevation_span_m"
        " FROM positions WHERE drive_id = %s",
        (drive_id,),
    )
    out["track"] = {
        "points_recorded": stats["points"],
        "power_max_kw": stats["power_max_kw"],
        "power_min_kw": stats["power_min_kw"],
        "elevation_span_ft": (
            None
            if stats["elevation_span_m"] is None
            else round(float(stats["elevation_span_m"]) * 3.28084)
        ),
    }
    return out


@mcp.tool()
def recent_charges(days: int = 30, limit: int = 20, car_id: int | None = None) -> list:
    """Charging sessions from the last N days, newest first: energy added vs
    used (the gap is charging loss), battery levels, duration, cost if
    TeslaMate knows it, location, and whether it was DC fast charging."""
    car = resolve_car(car_id)
    rows = q(
        """
        SELECT cp.id, cp.start_date, cp.end_date, cp.charge_energy_added,
               cp.charge_energy_used, cp.start_battery_level, cp.end_battery_level,
               cp.duration_min, cp.cost, cp.outside_temp_avg,
               g.name AS geofence, a.display_name AS address,
               EXISTS (
                 SELECT 1 FROM charges c
                 WHERE c.charging_process_id = cp.id AND c.fast_charger_present
               ) AS dc_fast
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        WHERE cp.car_id = %s AND cp.start_date >= now() - make_interval(days => %s)
        ORDER BY cp.start_date DESC LIMIT %s
        """,
        (car["id"], days, limit),
    )
    out = []
    for r in rows:
        added = float(r["charge_energy_added"]) if r["charge_energy_added"] is not None else None
        used = float(r["charge_energy_used"]) if r["charge_energy_used"] is not None else None
        loss_pct = None
        if added and used:
            loss_pct = round((used - added) / used * 100.0, 1)
        out.append(
            {
                "charge_id": r["id"],
                "start": r["start_date"].isoformat() if r["start_date"] else None,
                "end": r["end_date"].isoformat() if r["end_date"] else None,
                "location": place_name(r["geofence"], r["address"]),
                "type": "DC fast" if r["dc_fast"] else "AC",
                "energy_added_kwh": added,
                "energy_used_kwh": used,
                "charging_loss_pct": loss_pct,
                "battery_from_pct": r["start_battery_level"],
                "battery_to_pct": r["end_battery_level"],
                "duration_min": r["duration_min"],
                "cost": float(r["cost"]) if r["cost"] is not None else None,
                "outside_temp_avg_f": fahr(r["outside_temp_avg"]),
            }
        )
    return out


@mcp.tool()
def battery_health(car_id: int | None = None) -> dict:
    """Battery health trend, estimated from logged charge data: for each week,
    the projected rated range at a 100% charge and the implied usable capacity.
    These are estimates derived from TeslaMate's logs (the same approach as its
    Grafana degradation dashboard), not a Tesla service measurement."""
    car = resolve_car(car_id)
    rows = q(
        """
        SELECT date_trunc('week', c.date) AS week,
               percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY c.rated_battery_range_km / NULLIF(c.usable_battery_level, 0) * 100
               ) AS projected_rated_km
        FROM charges c
        JOIN charging_processes cp ON cp.id = c.charging_process_id
        WHERE cp.car_id = %s AND c.usable_battery_level >= 20
          AND c.rated_battery_range_km IS NOT NULL
        GROUP BY week ORDER BY week
        """,
        (car["id"],),
    )
    eff = float(car["efficiency"]) if car["efficiency"] else None
    trend = []
    for r in rows:
        km = float(r["projected_rated_km"]) if r["projected_rated_km"] is not None else None
        trend.append(
            {
                "week_of": r["week"].date().isoformat() if r["week"] else None,
                "projected_rated_range_mi": mi(km),
                "projected_rated_range_km": round(km, 1) if km is not None else None,
                "est_capacity_kwh": round(km * eff, 1) if km and eff else None,
            }
        )
    out = {"car_id": car["id"], "weekly_trend": trend}
    if len(trend) >= 2 and trend[0]["projected_rated_range_km"] and trend[-1]["projected_rated_range_km"]:
        first = trend[0]["projected_rated_range_km"]
        last = trend[-1]["projected_rated_range_km"]
        out["change_since_first_week_pct"] = round((last - first) / first * 100.0, 2)
    return out


@mcp.tool()
def efficiency_report(days: int = 30, car_id: int | None = None) -> dict:
    """Efficiency over the last N days: overall Wh/mi plus breakdowns by
    outside-temperature band and by trip length, so patterns like "cold
    mornings" or "short trips" show up. Energy is estimated from rated-range
    deltas and the car's TeslaMate efficiency factor."""
    car = resolve_car(car_id)
    rows = q(
        """
        SELECT d.distance,
               (d.start_rated_range_km - d.end_rated_range_km) AS rated_used_km,
               d.outside_temp_avg
        FROM drives d
        WHERE d.car_id = %s AND d.start_date >= now() - make_interval(days => %s)
          AND d.distance > 0 AND d.start_rated_range_km IS NOT NULL
          AND d.end_rated_range_km IS NOT NULL
        """,
        (car["id"], days),
    )
    eff = float(car["efficiency"]) if car["efficiency"] else None

    def wh_per_mi(subset):
        dist_km = sum(float(r["distance"]) for r in subset)
        rated = sum(float(r["rated_used_km"]) for r in subset if r["rated_used_km"] is not None)
        if not dist_km or not eff:
            return None
        return round(rated * eff * 1000.0 / (dist_km * KM_TO_MI), 0)

    def band(temp_f, lo, hi):
        return [
            r
            for r in rows
            if r["outside_temp_avg"] is not None
            and lo <= float(r["outside_temp_avg"]) * 9.0 / 5.0 + 32.0 < hi
        ]

    def length_band(lo_mi, hi_mi):
        return [
            r
            for r in rows
            if lo_mi <= float(r["distance"]) * KM_TO_MI < hi_mi
        ]

    return {
        "days": days,
        "drives_counted": len(rows),
        "overall_wh_per_mi": wh_per_mi(rows),
        "by_outside_temp_f": {
            "below_40": wh_per_mi(band(None, -100, 40)),
            "40_to_60": wh_per_mi(band(None, 40, 60)),
            "60_to_80": wh_per_mi(band(None, 60, 80)),
            "above_80": wh_per_mi(band(None, 80, 300)),
        },
        "by_trip_length": {
            "under_5_mi": wh_per_mi(length_band(0, 5)),
            "5_to_20_mi": wh_per_mi(length_band(5, 20)),
            "over_20_mi": wh_per_mi(length_band(20, 100000)),
        },
    }


@mcp.tool()
def vampire_drain(days: int = 30, min_hours: float = 3.0, car_id: int | None = None) -> dict:
    """Parked (vampire) drain: for each asleep/offline stretch of at least
    min_hours in the last N days, how much battery was lost and the implied
    percent-per-day rate. Sentry mode, cabin overheat protection and
    third-party polling all show up here."""
    car = resolve_car(car_id)
    rows = q(
        """
        SELECT s.start_date, s.end_date,
               EXTRACT(EPOCH FROM (s.end_date - s.start_date)) / 3600.0 AS hours,
               pb.battery_level AS level_before,
               pa.battery_level AS level_after
        FROM states s
        JOIN LATERAL (
          SELECT battery_level FROM positions
          WHERE car_id = s.car_id AND date <= s.start_date
          ORDER BY date DESC LIMIT 1
        ) pb ON true
        JOIN LATERAL (
          SELECT battery_level FROM positions
          WHERE car_id = s.car_id AND date >= s.end_date
          ORDER BY date ASC LIMIT 1
        ) pa ON true
        WHERE s.car_id = %s AND s.state IN ('asleep', 'offline')
          AND s.end_date IS NOT NULL
          AND s.start_date >= now() - make_interval(days => %s)
          AND s.end_date - s.start_date >= make_interval(hours => %s)
        ORDER BY s.start_date DESC
        """,
        (car["id"], days, min_hours),
    )
    events = []
    rates = []
    for r in rows:
        lost = None
        rate = None
        if r["level_before"] is not None and r["level_after"] is not None:
            lost = r["level_before"] - r["level_after"]
            if r["hours"]:
                rate = round(lost / float(r["hours"]) * 24.0, 2)
                rates.append(rate)
        events.append(
            {
                "from": r["start_date"].isoformat() if r["start_date"] else None,
                "to": r["end_date"].isoformat() if r["end_date"] else None,
                "hours_parked": round(float(r["hours"]), 1) if r["hours"] else None,
                "battery_lost_pct": lost,
                "pct_per_day": rate,
            }
        )
    return {
        "days": days,
        "parked_stretches": len(events),
        "median_pct_per_day": sorted(rates)[len(rates) // 2] if rates else None,
        "events": events[:25],
    }


@mcp.tool()
def monthly_summary(year: int, month: int, car_id: int | None = None) -> dict:
    """One month at a glance: distance driven, number of drives, energy
    charged split AC vs DC, charging cost if recorded, and average
    efficiency."""
    car = resolve_car(car_id)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    d = q1(
        "SELECT count(*) AS drives, coalesce(sum(distance), 0) AS dist_km,"
        " coalesce(sum(start_rated_range_km - end_rated_range_km), 0) AS rated_used_km"
        " FROM drives WHERE car_id = %s AND start_date >= %s AND start_date < %s",
        (car["id"], start, end),
    )
    c = q1(
        """
        SELECT count(*) AS sessions,
               coalesce(sum(cp.charge_energy_added), 0) AS added_kwh,
               coalesce(sum(cp.cost), 0) AS cost,
               coalesce(sum(cp.charge_energy_added) FILTER (
                 WHERE EXISTS (SELECT 1 FROM charges ch
                               WHERE ch.charging_process_id = cp.id
                                 AND ch.fast_charger_present)
               ), 0) AS dc_kwh
        FROM charging_processes cp
        WHERE cp.car_id = %s AND cp.start_date >= %s AND cp.start_date < %s
        """,
        (car["id"], start, end),
    )
    eff = float(car["efficiency"]) if car["efficiency"] else None
    dist_km = float(d["dist_km"])
    wh_per_mi = None
    if dist_km and eff:
        wh_per_mi = round(float(d["rated_used_km"]) * eff * 1000.0 / (dist_km * KM_TO_MI), 0)
    added = float(c["added_kwh"])
    return {
        "year": year,
        "month": month,
        "distance_mi": mi(dist_km),
        "drives": d["drives"],
        "avg_wh_per_mi": wh_per_mi,
        "charge_sessions": c["sessions"],
        "energy_added_kwh": round(added, 1),
        "dc_fast_charge_kwh": round(float(c["dc_kwh"]), 1),
        "ac_charge_kwh": round(added - float(c["dc_kwh"]), 1),
        "charging_cost_recorded": round(float(c["cost"]), 2),
    }


@mcp.tool()
def top_places(limit: int = 10, car_id: int | None = None) -> dict:
    """Most common drive destinations and charging locations, by geofence name
    or street address, from everything TeslaMate has logged."""
    car = resolve_car(car_id)
    dests = q(
        """
        SELECT coalesce(ge.name, ade.display_name, 'Unknown') AS place, count(*) AS visits
        FROM drives d
        LEFT JOIN geofences ge ON ge.id = d.end_geofence_id
        LEFT JOIN addresses ade ON ade.id = d.end_address_id
        WHERE d.car_id = %s
        GROUP BY place ORDER BY visits DESC LIMIT %s
        """,
        (car["id"], limit),
    )
    charges_at = q(
        """
        SELECT coalesce(g.name, a.display_name, 'Unknown') AS place,
               count(*) AS sessions, round(sum(cp.charge_energy_added), 1) AS kwh
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        WHERE cp.car_id = %s
        GROUP BY place ORDER BY sessions DESC LIMIT %s
        """,
        (car["id"], limit),
    )
    return {
        "top_destinations": [
            {"place": r["place"], "visits": r["visits"]} for r in dests
        ],
        "top_charging_locations": [
            {"place": r["place"], "sessions": r["sessions"], "kwh_added": float(r["kwh"]) if r["kwh"] is not None else None}
            for r in charges_at
        ],
    }


class BearerAuthMiddleware:
    """Reject HTTP requests without the configured bearer token."""

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
