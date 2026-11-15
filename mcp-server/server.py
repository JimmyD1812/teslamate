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

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _tou_rates():
    """Time-of-use price bands from the TESLAMATE_TOU_RATES env var.

    JSON list like [{"from_hour": 0, "to_hour": 6, "price_per_kwh": 0.11},
    {"from_hour": 6, "to_hour": 24, "price_per_kwh": 0.24}]. Hours are local
    time in TESLAMATE_TIMEZONE (default UTC). Returns None when unconfigured.
    """
    raw = os.environ.get("TESLAMATE_TOU_RATES", "")
    if not raw:
        return None
    try:
        bands = json.loads(raw)
        tz = ZoneInfo(os.environ.get("TESLAMATE_TIMEZONE", "UTC"))
        return bands, tz
    except Exception:
        return None


def _tou_cost_for_process(process_id, bands, tz):
    """Estimate a session's cost by pricing the energy added in each hour
    band, using the per-sample charge_energy_added deltas (TeslaMateAgile
    style). Returns (cost, priced_kwh) or (None, 0)."""
    samples = q(
        "SELECT date, charge_energy_added FROM charges"
        " WHERE charging_process_id = %s ORDER BY date",
        (process_id,),
    )
    total = 0.0
    priced = 0.0
    prev = 0.0
    for s in samples:
        if s["charge_energy_added"] is None:
            continue
        added = float(s["charge_energy_added"])
        delta = max(added - prev, 0.0)
        prev = added
        if delta == 0:
            continue
        hour = s["date"].astimezone(tz).hour
        price = None
        for b in bands:
            if b["from_hour"] <= hour < b["to_hour"]:
                price = float(b["price_per_kwh"])
                break
        if price is not None:
            total += delta * price
            priced += delta
    return (round(total, 2), round(priced, 2)) if priced else (None, 0.0)


@mcp.tool()
def charging_cost_report(days: int = 90, car_id: int | None = None) -> dict:
    """Charging costs over the last N days, emulating the paid add-ons:
    sessions TeslaMate already priced (via its geofence rates) are summed as
    recorded; sessions with no recorded cost are estimated from a
    time-of-use rate schedule when one is configured on the server
    (TESLAMATE_TOU_RATES), the way TeslaMateAgile prices by time band.
    DC fast (Supercharger) sessions are broken out separately, and any
    session that could not be priced is flagged instead of silently dropped."""
    car = resolve_car(car_id)
    rows = q(
        """
        SELECT cp.id, cp.start_date, cp.charge_energy_added, cp.cost,
               g.name AS geofence, a.display_name AS address,
               EXISTS (
                 SELECT 1 FROM charges c
                 WHERE c.charging_process_id = cp.id AND c.fast_charger_present
               ) AS dc_fast
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        WHERE cp.car_id = %s AND cp.start_date >= now() - make_interval(days => %s)
        ORDER BY cp.start_date
        """,
        (car["id"], days),
    )
    tou = _tou_rates()
    recorded_total = 0.0
    estimated_total = 0.0
    unpriced = []
    dc = {"sessions": 0, "kwh": 0.0, "recorded_cost": 0.0, "estimated_cost": 0.0}
    for r in rows:
        kwh = float(r["charge_energy_added"]) if r["charge_energy_added"] is not None else 0.0
        cost = None
        source = None
        if r["cost"] is not None:
            cost = float(r["cost"])
            source = "recorded"
            recorded_total += cost
        elif tou:
            est, priced_kwh = _tou_cost_for_process(r["id"], tou[0], tou[1])
            if est is not None:
                cost = est
                source = "tou_estimate"
                estimated_total += est
        if cost is None:
            unpriced.append(
                {
                    "charge_id": r["id"],
                    "start": r["start_date"].isoformat() if r["start_date"] else None,
                    "location": place_name(r["geofence"], r["address"]),
                    "kwh": round(kwh, 1),
                }
            )
        if r["dc_fast"]:
            dc["sessions"] += 1
            dc["kwh"] = round(dc["kwh"] + kwh, 1)
            if source == "recorded":
                dc["recorded_cost"] = round(dc["recorded_cost"] + cost, 2)
            elif source == "tou_estimate":
                dc["estimated_cost"] = round(dc["estimated_cost"] + cost, 2)
    return {
        "days": days,
        "sessions": len(rows),
        "recorded_cost_total": round(recorded_total, 2),
        "tou_estimated_cost_total": round(estimated_total, 2),
        "tou_schedule_configured": tou is not None,
        "supercharger_dc": dc,
        "unpriced_sessions": unpriced,
    }


@mcp.tool()
def charge_curve_analysis(charge_id: int | None = None, car_id: int | None = None) -> dict:
    """DC fast-charge curve for one session (the given charge_id, or the most
    recent DC session): peak power, power at about 50% and 80% battery,
    minutes from 10% to 80%, and how the peak compares with the median peak
    of past DC sessions. Shows taper behavior numerically instead of just a
    graph."""
    car = resolve_car(car_id)
    if charge_id is None:
        row = q1(
            """
            SELECT cp.id FROM charging_processes cp
            WHERE cp.car_id = %s AND EXISTS (
              SELECT 1 FROM charges c
              WHERE c.charging_process_id = cp.id AND c.fast_charger_present)
            ORDER BY cp.start_date DESC LIMIT 1
            """,
            (car["id"],),
        )
        if row is None:
            raise ValueError("No DC fast-charging sessions logged yet.")
        charge_id = row["id"]
    samples = q(
        "SELECT date, battery_level, charger_power, charge_energy_added"
        " FROM charges WHERE charging_process_id = %s ORDER BY date",
        (charge_id,),
    )
    if not samples:
        raise ValueError(f"No charge samples for session {charge_id}.")

    def kw_at(target_level):
        best = min(samples, key=lambda s: abs((s["battery_level"] or 0) - target_level))
        return best["charger_power"]

    def time_at(target_level):
        for s in samples:
            if (s["battery_level"] or 0) >= target_level:
                return s["date"]
        return None

    peak = max((s["charger_power"] or 0) for s in samples)
    t10, t80 = time_at(10), time_at(80)
    minutes_10_80 = (
        round((t80 - t10).total_seconds() / 60.0, 1) if t10 and t80 else None
    )
    peaks = q(
        """
        SELECT max(c.charger_power) AS peak
        FROM charges c
        JOIN charging_processes cp ON cp.id = c.charging_process_id
        WHERE cp.car_id = %s AND c.fast_charger_present
        GROUP BY cp.id
        """,
        (car["id"],),
    )
    median_peak = _median([p["peak"] for p in peaks])
    return {
        "charge_id": charge_id,
        "samples": len(samples),
        "peak_kw": peak,
        "kw_at_50pct": kw_at(50),
        "kw_at_80pct": kw_at(80),
        "minutes_10_to_80pct": minutes_10_80,
        "median_peak_kw_all_dc_sessions": median_peak,
        "peak_vs_median_pct": (
            round((peak - median_peak) / median_peak * 100.0, 1)
            if median_peak
            else None
        ),
    }


@mcp.tool()
def commute_insights(min_trips: int = 3, car_id: int | None = None) -> dict:
    """Recurring-route analysis: groups drives by start and end place
    (geofence or address), and for every route taken at least min_trips
    times reports how often, the typical departure hour, median duration and
    efficiency, and which departure hour band has the fastest median trip.
    This is the pattern layer a dashboard does not give you."""
    car = resolve_car(car_id)
    rows = q(
        DRIVE_SELECT + "WHERE d.car_id = %s ORDER BY d.start_date",
        (car["id"],),
    )
    groups = {}
    for r in rows:
        key = (
            place_name(r["start_geofence"], r["start_address"]) or "Unknown",
            place_name(r["end_geofence"], r["end_address"]) or "Unknown",
        )
        groups.setdefault(key, []).append(r)
    out = []
    for (src, dst), trips in groups.items():
        if len(trips) < min_trips or src == dst:
            continue
        summaries = [drive_row(t, car["efficiency"]) for t in trips]
        durations = [s["duration_min"] for s in summaries if s["duration_min"]]
        whmis = [s["est_wh_per_mi"] for s in summaries if s["est_wh_per_mi"]]
        hours = [t["start_date"].hour for t in trips if t["start_date"]]
        by_hour = {}
        for t, s in zip(trips, summaries):
            if t["start_date"] and s["duration_min"]:
                by_hour.setdefault(t["start_date"].hour, []).append(s["duration_min"])
        best_hour, best_med = None, None
        for h, ds in by_hour.items():
            if len(ds) >= 2:
                med = _median(ds)
                if best_med is None or med < best_med:
                    best_hour, best_med = h, med
        out.append(
            {
                "from": src,
                "to": dst,
                "trips": len(trips),
                "typical_departure_hour_utc": _median(hours),
                "median_duration_min": _median(durations),
                "fastest_departure_hour_utc": best_hour,
                "fastest_median_duration_min": best_med,
                "median_wh_per_mi": _median(whmis),
            }
        )
    out.sort(key=lambda x: -x["trips"])
    return {"routes": out[:10]}


@mcp.tool()
def anomaly_scan(days: int = 30, car_id: int | None = None) -> dict:
    """Scans the last N days for things that look wrong, comparing each event
    against the car's own norms: drives far less efficient than usual for
    that route, parked-drain spikes, charging sessions with abnormal losses,
    weak DC fast-charge peaks, and sudden projected-range drops. Returns
    structured findings for the assistant to explain — the point is catching
    connections (a software update, a cold snap, a failing tire, a new
    parasitic drain) early."""
    car = resolve_car(car_id)
    findings = []

    rows = q(
        DRIVE_SELECT
        + "WHERE d.car_id = %s AND d.start_date >= now() - make_interval(days => %s)"
          " ORDER BY d.start_date",
        (car["id"], days),
    )
    summaries = [drive_row(r, car["efficiency"]) for r in rows]
    all_whmi = [s["est_wh_per_mi"] for s in summaries if s["est_wh_per_mi"]]
    overall_med = _median(all_whmi)
    by_route = {}
    for s in summaries:
        by_route.setdefault((s["from"], s["to"]), []).append(s)
    for s in summaries:
        route_vals = [x["est_wh_per_mi"] for x in by_route[(s["from"], s["to"])] if x["est_wh_per_mi"]]
        baseline = _median(route_vals) if len(route_vals) >= 3 else overall_med
        if baseline and s["est_wh_per_mi"] and s["est_wh_per_mi"] > baseline * 1.4:
            findings.append(
                {
                    "type": "inefficient_drive",
                    "when": s["start"],
                    "detail": (
                        f"Drive {s['from']} -> {s['to']} used {s['est_wh_per_mi']:.0f} Wh/mi"
                        f" vs a typical {baseline:.0f} Wh/mi for that comparison set"
                        f" (outside temp {s['outside_temp_avg_f']} F)."
                    ),
                }
            )

    drain = vampire_drain(days=days, min_hours=3.0, car_id=car["id"])
    rates = [e["pct_per_day"] for e in drain["events"] if e["pct_per_day"] is not None]
    med_rate = _median(rates)
    if med_rate:
        for e in drain["events"]:
            if e["pct_per_day"] and e["pct_per_day"] > max(med_rate * 3, med_rate + 1.0):
                findings.append(
                    {
                        "type": "parked_drain_spike",
                        "when": e["from"],
                        "detail": (
                            f"Parked for {e['hours_parked']} h and lost {e['battery_lost_pct']}%"
                            f" ({e['pct_per_day']}%/day vs a typical {med_rate}%/day)."
                            " Usual suspects: Sentry mode, cabin overheat protection,"
                            " a door/trunk left ajar, or an app polling the car awake."
                        ),
                    }
                )

    for c in recent_charges(days=days, limit=50, car_id=car["id"]):
        if c["charging_loss_pct"] is not None and c["charging_loss_pct"] > 15:
            findings.append(
                {
                    "type": "high_charging_loss",
                    "when": c["start"],
                    "detail": (
                        f"{c['type']} session at {c['location']} lost"
                        f" {c['charging_loss_pct']}% between wall and battery"
                        f" (typical is roughly 8-12% for AC, less for DC)."
                    ),
                }
            )

    health = battery_health(car_id=car["id"])
    trend = [t for t in health["weekly_trend"] if t["projected_rated_range_km"]]
    if len(trend) >= 2:
        first, last = trend[0], trend[-1]
        drop = (
            (last["projected_rated_range_km"] - first["projected_rated_range_km"])
            / first["projected_rated_range_km"]
            * 100.0
        )
        if drop < -1.5:
            findings.append(
                {
                    "type": "range_trend_drop",
                    "when": last["week_of"],
                    "detail": (
                        f"Projected full-charge rated range is down {abs(drop):.1f}%"
                        f" from the first logged week ({first['projected_rated_range_mi']} mi"
                        f" -> {last['projected_rated_range_mi']} mi). Early-life settling is"
                        " normal; a sudden step usually follows a software update's"
                        " recalibration rather than real capacity loss."
                    ),
                }
            )

    return {"days_scanned": days, "findings": findings}


@mcp.tool()
def battery_care_score(days: int = 90, car_id: int | None = None) -> dict:
    """Battery-care habits over the last N days, scored out of 100 with the
    evidence shown: DC fast-charge share of energy, charges to ~100%,
    deep discharges below 10%, and the share of sampled parked time spent
    sitting above 90% charge. A coaching summary no dashboard computes."""
    car = resolve_car(car_id)
    ch = q1(
        """
        SELECT
          coalesce(sum(cp.charge_energy_added), 0) AS total_kwh,
          coalesce(sum(cp.charge_energy_added) FILTER (
            WHERE EXISTS (SELECT 1 FROM charges c
                          WHERE c.charging_process_id = cp.id
                            AND c.fast_charger_present)), 0) AS dc_kwh,
          count(*) FILTER (WHERE cp.end_battery_level >= 98) AS full_charges,
          count(*) AS sessions
        FROM charging_processes cp
        WHERE cp.car_id = %s AND cp.start_date >= now() - make_interval(days => %s)
        """,
        (car["id"], days),
    )
    deep = q1(
        "SELECT count(*) AS n FROM drives d JOIN positions p ON p.id = d.end_position_id"
        " WHERE d.car_id = %s AND d.start_date >= now() - make_interval(days => %s)"
        " AND p.battery_level < 10",
        (car["id"], days),
    )
    parked = q1(
        "SELECT count(*) AS total,"
        " count(*) FILTER (WHERE battery_level >= 90) AS high"
        " FROM positions WHERE car_id = %s AND drive_id IS NULL"
        " AND date >= now() - make_interval(days => %s)",
        (car["id"], days),
    )
    total_kwh = float(ch["total_kwh"] or 0)
    dc_share = round(float(ch["dc_kwh"] or 0) / total_kwh * 100.0, 1) if total_kwh else 0.0
    parked_high_pct = (
        round(parked["high"] / parked["total"] * 100.0, 1) if parked["total"] else None
    )
    score = 100
    notes = []
    if dc_share > 25:
        score -= 15
        notes.append(f"DC fast charging supplied {dc_share}% of energy (over a quarter).")
    elif dc_share > 10:
        score -= 7
        notes.append(f"DC fast charging supplied {dc_share}% of energy.")
    if ch["full_charges"]:
        score -= min(ch["full_charges"] * 3, 15)
        notes.append(f"{ch['full_charges']} charge(s) ended at 98-100%.")
    if deep["n"]:
        score -= min(deep["n"] * 4, 16)
        notes.append(f"{deep['n']} drive(s) ended below 10% battery.")
    if parked_high_pct is not None and parked_high_pct > 30:
        score -= 12
        notes.append(
            f"The car sat above 90% charge for about {parked_high_pct}% of sampled parked time."
        )
    if not notes:
        notes.append("No habit flags in this window: gentle charging profile.")
    return {
        "days": days,
        "score_out_of_100": max(score, 0),
        "dc_fast_charge_share_pct": dc_share,
        "charges_to_full": ch["full_charges"],
        "charge_sessions": ch["sessions"],
        "deep_discharges_below_10pct": deep["n"],
        "parked_time_above_90pct_share": parked_high_pct,
        "notes": notes,
    }


@mcp.tool()
def update_impact(car_id: int | None = None) -> dict:
    """For each software update TeslaMate logged, compares the 14 days before
    and after it: median efficiency and projected full-charge range. Shows
    whether an update actually changed the car or just felt like it did."""
    car = resolve_car(car_id)
    updates = q(
        "SELECT version, start_date FROM updates WHERE car_id = %s"
        " AND start_date IS NOT NULL ORDER BY start_date",
        (car["id"],),
    )
    out = []
    for u in updates:
        def window(sign):
            op = "<" if sign < 0 else ">="
            rng = "start_date - interval '14 days'" if sign < 0 else "start_date + interval '14 days'"
            d = q1(
                f"""
                SELECT percentile_cont(0.5) WITHIN GROUP (
                         ORDER BY (start_rated_range_km - end_rated_range_km)
                                  / NULLIF(distance, 0)) AS rated_per_km
                FROM drives
                WHERE car_id = %s AND start_date {op} %s
                  AND start_date {"<" if sign > 0 else ">="} ({rng.replace('start_date', '%s')})
                  AND distance > 2
                """,
                (car["id"], u["start_date"], u["start_date"]),
            )
            return d["rated_per_km"] if d else None

        before = window(-1)
        after = window(1)
        eff = float(car["efficiency"]) if car["efficiency"] else None

        def to_whmi(rated_per_km):
            if rated_per_km is None or not eff:
                return None
            return round(float(rated_per_km) * eff * 1000.0 / KM_TO_MI, 0)

        out.append(
            {
                "version": u["version"],
                "installed": u["start_date"].isoformat() if u["start_date"] else None,
                "median_wh_per_mi_14d_before": to_whmi(before),
                "median_wh_per_mi_14d_after": to_whmi(after),
            }
        )
    return {"updates": out}


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
