#!/usr/bin/env python
"""MCP-Server: gibt Claude (Browser, Desktop, Handy) lesenden Zugriff auf die Garmin-Daten.

Claude verbindet sich aus Anthropics Rechenzentrum, der Server muss also von außen
erreichbar sein. Schutz in drei Schichten:
  - die Adresse enthält ein Geheimnis (MCP_PATH_SECRET), alles andere ist 404
  - der Reverse-Proxy davor lässt nur Anthropics Adressbereich durch
  - die Datenbanken sind nur lesend geöffnet, erlaubt ist ausschließlich SELECT
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

DATA = Path(os.environ.get("GARMIN_DATA") or Path(__file__).resolve().parent / "data")
MAIN_DB = DATA / "garmin_data.db"
EXTRAS_DB = DATA / "garmin_extras.db"
SECRET = os.environ.get("MCP_PATH_SECRET", "")
HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
PORT = int(os.environ.get("MCP_PORT", "8000"))
MAX_ROWS = 1000
QUERY_TIMEOUT_S = 20

NOTES = """\
Garmin-Daten einer Person, gespiegelt aus Garmin Connect. Neues kommt wenige Minuten,
nachdem die Uhr hochgeladen hat; as_of zeigt den Datenstand.

Zwei SQLite-Datenbanken, in Abfragen so ansprechbar:
  - Haupt-DB (ohne Präfix): activity, sleep, steps, heart_rate, stress, body_battery,
    respiration, hrv, body_composition, vo2_max, training_load, personal_record, …
  - Zusatz-DB (Präfix extras.): daily_summary, hrv_daily, vo2max_daily, strength_set_all

Wichtig beim Auswerten:
  - Zeitstempel (start_ts, timestamp, start_time) sind UTC; die Ortszeit ergibt sich bei
    activity und sleep aus timezone_offset_hours. calendarDate/calendar_date sind Ortstage.
  - extras.daily_summary hat eine Zeile je Tag ab 2017-04-07 (Schritte, Kalorien, Ruhepuls,
    Stress, Body Battery, Schlafdauer, Intensitätsminuten, …) und ist die beste Quelle für
    Verläufe über Monate und Jahre.
  - heart_rate, stress, body_battery, respiration (Verläufe innerhalb eines Tages) gibt es
    nur für die letzten Monate; steps davor nur als Nullzeilen. Das ist eine Grenze von Garmin.
  - Krafttraining: extras.strength_set_all nehmen (weight_kg, reps, set_type ACTIVE/REST).
    strength_set in der Haupt-DB ist unvollständig. exercise_category/exercise_name sind
    korrigiert: wo die Uhr eine Übung nicht oder falsch erkannt hat, ist sie aus dem festen
    Trainingsaufbau erraten (guessed = 1); Garmins Original steht in garmin_category/garmin_name.
  - Gewichte in der Haupt-DB stehen in Gramm (body_composition.weight, strength_set.weight),
    Dauern in Sekunden, Distanzen in Metern.
  - activity_ts_metric hat ~19 Mio. Zeilen (Sekundenwerte der Aktivitäten): immer über
    activity_id einschränken.
"""

mcp = MCPServer(
    "garmin",
    instructions=(
        "Lesender Zugriff auf die gespiegelten Garmin-Daten des Nutzers (Aktivitäten, Schlaf, "
        "Tageswerte, HRV, Gewicht, Krafttraining). Für die üblichen Fragen genügen overview und "
        "strength_sessions. Für alles andere erst describe_data lesen, dann query benutzen."
    ),
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _select_only(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def connect(restricted=True):
    """Je Aufruf neu verbinden: die Zusatz-DB wird täglich durch eine neue Datei ersetzt."""
    con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    con.execute(f"ATTACH DATABASE 'file:{EXTRAS_DB}?mode=ro' AS extras")
    con.row_factory = sqlite3.Row
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    con.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    if restricted:
        con.set_authorizer(_select_only)
    return con


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def as_of():
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(MAIN_DB.stat().st_mtime))


@mcp.tool(annotations=READ_ONLY)
def describe_data() -> str:
    """Beschreibt die Garmin-Datenbanken: Hinweise zu Einheiten und Lücken sowie alle Tabellen
    mit ihren Spalten. Vor der ersten eigenen SQL-Abfrage aufrufen."""
    con = connect(restricted=False)
    out = [NOTES, f"Datenstand: {as_of()}", ""]
    for prefix, schema in (("", "main"), ("extras.", "extras")):
        for (table,) in con.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall():
            cols = [r["name"] for r in con.execute(f'PRAGMA {schema}.table_info("{table}")')]
            out.append(f"{prefix}{table}: {', '.join(cols)}")
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
def query(sql: str, max_rows: int = 200) -> dict:
    """Führt eine einzelne SELECT-Abfrage (SQLite) auf den Garmin-Daten aus. Tabellen der
    Zusatz-DB mit Präfix extras. ansprechen. Nur lesend, höchstens 1000 Zeilen, Abbruch
    nach 20 Sekunden."""
    limit = max(1, min(max_rows, MAX_ROWS))
    con = connect()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        data = cur.fetchmany(limit + 1)
    except sqlite3.Error as exc:
        if "interrupted" in str(exc):
            return {"error": f"Abbruch nach {QUERY_TIMEOUT_S} s — Abfrage enger fassen (Zeitraum, activity_id)."}
        return {"error": str(exc), "hint": "Nur SELECT ist erlaubt; Schema siehe describe_data."}
    return {
        "columns": cols,
        "rows": [list(r) for r in data[:limit]],
        "truncated": len(data) > limit,
        "as_of": as_of(),
    }


@mcp.tool(annotations=READ_ONLY)
def overview(days: int = 7) -> dict:
    """Überblick über die letzten Tage: je Tag Schritte, Ruhepuls, Stress, Body Battery,
    Schlafdauer und HRV, dazu alle Aktivitäten im Zeitraum (Ortszeit). Erste Wahl für
    Fragen wie „wie war meine Woche" oder „wie habe ich geschlafen"."""
    days = max(1, min(days, 90))
    con = connect()
    daily = rows(con, """
        SELECT d.calendarDate AS date, d.totalSteps AS steps, d.restingHeartRate AS resting_hr,
               d.averageStressLevel AS avg_stress, d.bodyBatteryHighestValue AS body_battery_high,
               d.bodyBatteryLowestValue AS body_battery_low,
               round(d.sleepingSeconds / 3600.0, 1) AS sleep_hours,
               round(d.totalKilocalories) AS kcal_total, round(d.activeKilocalories) AS kcal_active,
               h.lastNightAvg AS hrv_last_night, h.weeklyAvg AS hrv_weekly_avg, h.status AS hrv_status
          FROM extras.daily_summary d LEFT JOIN extras.hrv_daily h USING (calendarDate)
         WHERE d.calendarDate >= date('now', 'localtime', ?)
         ORDER BY d.calendarDate DESC""", (f"-{days} days",))
    activities = rows(con, """
        SELECT activity_id,
               strftime('%Y-%m-%d %H:%M', start_ts, printf('%+d minutes', CAST(timezone_offset_hours * 60 AS INT))) AS start_local,
               activity_type_key AS type, activity_name AS name,
               round(duration / 60.0) AS duration_min, round(distance / 1000.0, 1) AS distance_km,
               average_hr, max_hr, round(calories) AS kcal, round(activity_training_load) AS training_load,
               round(aerobic_training_effect, 1) AS aerobic_effect
          FROM activity
         WHERE start_ts >= datetime('now', ?)
         ORDER BY start_ts DESC""", (f"-{days} days",))
    return {"as_of": as_of(), "days": daily, "activities": activities}


@mcp.tool(annotations=READ_ONLY)
def strength_sessions(limit: int = 3) -> dict:
    """Die letzten Krafteinheiten mit allen Sätzen: je Einheit Datum, Dauer, Puls und je Übung
    und Gewicht die Wiederholungen der Reihe nach. Erste Wahl für Fragen zum Krafttraining."""
    limit = max(1, min(limit, 40))
    con = connect()
    sessions = rows(con, """
        SELECT a.activity_id,
               strftime('%Y-%m-%d %H:%M', a.start_ts, printf('%+d minutes', CAST(a.timezone_offset_hours * 60 AS INT))) AS start_local,
               round(a.duration / 60.0) AS duration_min, a.average_hr, a.max_hr,
               round(a.calories) AS kcal, round(a.activity_training_load) AS training_load
          FROM activity a
         WHERE a.activity_id IN (SELECT activity_id FROM extras.strength_set_all)
         ORDER BY a.start_ts DESC LIMIT ?""", (limit,))
    for s in sessions:
        blocks = []
        for r in con.execute("""
                SELECT coalesce(exercise_name, exercise_category, 'UNKNOWN') AS exercise, weight_kg, reps
                  FROM extras.strength_set_all
                 WHERE activity_id = ? AND set_type = 'ACTIVE' ORDER BY set_idx""", (s["activity_id"],)):
            if blocks and (blocks[-1]["exercise"], blocks[-1]["weight_kg"]) == (r["exercise"], r["weight_kg"]):
                blocks[-1]["reps"].append(r["reps"])
            else:
                blocks.append({"exercise": r["exercise"], "weight_kg": r["weight_kg"], "reps": [r["reps"]]})
        s["sets"] = blocks
    return {"as_of": as_of(), "sessions": sessions}


def main():
    if len(SECRET) < 32:
        sys.exit("MCP_PATH_SECRET fehlt oder ist zu kurz (mindestens 32 Zeichen)")
    mcp.run(
        "streamable-http",
        host="0.0.0.0",
        port=PORT,
        streamable_http_path=f"/{SECRET}/mcp",
        # Ohne Sitzungen und als schlichtes JSON: läuft durch jeden Reverse-Proxy.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=HOSTS + ["127.0.0.1:*", "localhost:*"],
        ),
    )


if __name__ == "__main__":
    main()
