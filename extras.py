#!/usr/bin/env python
"""Ergänzung zu garmin-health-data: holt, was das Werkzeug auslässt.

Garmin liefert Puls-/Schritt-Verläufe über die API nur für die letzten Monate.
Für die ganze Historie bleiben zwei Quellen, die hier roh abgelegt werden:
  - Tageszusammenfassung (~90 Felder je Tag: Schritte, Kalorien, Ruhepuls, …)
  - Original-FIT-Dateien der Uhr je Tag (volle Auflösung, ungeparst)
Dazu je Monat HRV und VO2max. Aus dem JSON entsteht data/garmin_extras.db —
samt strength_set_all, der vollständigen Fassung der Kraftsätze des Werkzeugs.

  extras.py                      letzte 7 Tage neu (täglicher Lauf)
  extras.py --from 2017-01-01    Historie nachholen; Vorhandenes wird übersprungen
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from garmin_health_data.garmin_client import GarminClient
from garmin_health_data.garmin_client.exceptions import GarminTooManyRequestsError

DATA = Path(os.environ.get("GARMIN_DATA") or Path(__file__).resolve().parent / "data")
RAW = DATA / "extras"
DB = DATA / "garmin_extras.db"
PAUSE = 0.2  # Sekunden zwischen Anfragen
MAX_FAILS_IN_A_ROW = 20


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


def is_not_found(exc):
    return "API Error 404" in str(exc)


def download_with_retry(client, path, attempts=4):
    """Garmin antwortet beim Packen der FIT-ZIPs öfter mit 502/503/504."""
    for attempt in range(1, attempts + 1):
        try:
            return client._download(path)
        except GarminTooManyRequestsError:
            raise
        except Exception as exc:
            if attempt == attempts or not any(f"API Error {c}" in str(exc) for c in (502, 503, 504)):
                raise
            time.sleep(5 * attempt)


def fetch_days(client, start, end, refresh_from):
    """Tage von end rückwärts bis start; liefert die Zahl der Fehlschläge."""
    fails = fails_in_a_row = 0
    day = end
    while day >= start:
        d = day.isoformat()
        refresh = day >= refresh_from
        summary = RAW / "daily_summary" / d[:4] / f"{d}.json"
        fit = RAW / "wellness_fit" / d[:4] / f"{d}.zip"
        fit_none = fit.with_suffix(".none")  # Marke: Garmin hat für den Tag nichts
        try:
            if refresh or not summary.exists():
                data = client._connectapi(
                    f"/usersummary-service/usersummary/daily/{client.display_name}",
                    params={"calendarDate": d},
                )
                write(summary, json.dumps(data, ensure_ascii=False).encode())
                time.sleep(PAUSE)
            if refresh or not (fit.exists() or fit_none.exists()):
                try:
                    write(fit, download_with_retry(client, f"/download-service/files/wellness/{d}"))
                    fit_none.unlink(missing_ok=True)
                except GarminTooManyRequestsError:
                    raise
                except Exception as exc:
                    if not is_not_found(exc):
                        raise
                    write(fit_none, b"")
                time.sleep(PAUSE)
            fails_in_a_row = 0
        except GarminTooManyRequestsError:
            raise
        except Exception as exc:
            fails += 1
            fails_in_a_row += 1
            log(f"FEHLER {d}: {exc!r:.200}")
            if fails_in_a_row >= MAX_FAILS_IN_A_ROW:
                raise RuntimeError(f"{fails_in_a_row} Fehlschläge in Folge, Abbruch")
            time.sleep(5)
        if day.day == 1:
            log(f"{d[:7]} fertig")
        day -= timedelta(days=1)
    return fails


def fetch_months(client, start, end, refresh_from):
    month = end.replace(day=1)
    while month >= start.replace(day=1):
        last = (month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        refresh = last >= refresh_from
        span = f"{month.isoformat()}/{min(last, end).isoformat()}"
        for name, path in (
            ("hrv", f"/hrv-service/hrv/daily/{span}"),
            ("vo2max", f"/metrics-service/metrics/maxmet/daily/{span}"),
        ):
            target = RAW / name / f"{month:%Y-%m}.json"
            if refresh or not target.exists():
                data = client._connectapi(path)
                write(target, json.dumps(data, ensure_ascii=False).encode())
                time.sleep(PAUSE)
        month = (month - timedelta(days=1)).replace(day=1)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


SQUAT = ("SQUAT", "BARBELL_BACK_SQUAT")
BENCH = ("BENCH_PRESS", "BARBELL_BENCH_PRESS")
ROW = ("ROW", "BARBELL_ROW")
PRESS = ("SHOULDER_PRESS", "OVERHEAD_BARBELL_PRESS")
DEADLIFT = ("DEADLIFT", "BARBELL_DEADLIFT")
# Eine Schreibweise je Übung — Garmin liefert je nach Eingabe mal nur die Kategorie
# (BENCH_PRESS), mal eine Variante (WEIGHTED_SQUAT), mal den vollen Namen.
CANONICAL = dict([SQUAT, BENCH, ROW, PRESS, DEADLIFT, ("SHRUG", "DUMBBELL_SHRUG")])


def guess_stronglifts(active):
    """Errät die Übungen einer Einheit, die nach StrongLifts 5x5 aufgebaut ist.

    Die Uhr erkennt Übungen nur ungefähr (UNKNOWN, Overhead Press als Bankdrücken).
    Der Aufbau ist aber fest: erst Kniebeuge, dann entweder Bankdrücken + Rudern (A)
    oder Overhead Press + Kreuzheben (B). Welcher Plan es war, verrät ein von der Uhr
    erkannter Ruder- bzw. Kreuzhebe-Satz; ohne einen solchen wird nichts erraten.

    active: aktive Sätze in Reihenfolge, je {"kg", "category"}.
    Liefert je Satz (Kategorie, Name) oder None für „unverändert lassen".
    """
    cats = {s["category"] for s in active}
    plan = "A" if "ROW" in cats else "B" if "DEADLIFT" in cats else None
    if plan is None:
        return [None] * len(active)
    kg = [s["kg"] or 0 for s in active]
    # Ein deutlich leichterer Satz heißt: nächste Übung. Ein kleiner Schritt zurück
    # (ein Satz nicht geschafft, 2,5 kg weniger) bleibt dieselbe Übung.
    drops = [i for i in range(1, len(kg)) if kg[i] < 0.85 * kg[i - 1]]
    if not drops:
        return [None] * len(active)
    labels = [None] * len(active)
    second = drops[0]  # hier endet die Kniebeuge
    labels[:second] = [SQUAT] * second

    if plan == "A":
        later = [i for i in drops if i > second]
        if later:  # Rudern beginnt mit einem leichteren Aufwärmsatz
            third = later[-1]
        else:      # gleiches oder höheres Gewicht: Rudern sind die letzten (höchstens 5) Sätze
            run = len(kg) - 1
            while run > second and kg[run - 1] == kg[-1]:
                run -= 1
            third = max(run, len(kg) - 5)
        labels[second:third] = [BENCH] * (third - second)
        labels[third:] = [ROW] * (len(kg) - third)
        return labels

    # Plan B: die Arbeitssätze der Overhead Press sind die erste Serie von mindestens
    # drei gleich schweren Sätzen; danach steigt das Kreuzheben bis zum ersten Abfall.
    i = second
    while i + 2 < len(kg) and not (kg[i] == kg[i + 1] == kg[i + 2]):
        i += 1
    if i + 2 >= len(kg):
        return labels
    third = i
    while third < len(kg) and kg[third] == kg[i]:
        third += 1
    end = next((d for d in drops if d > third), len(kg))
    labels[second:third] = [PRESS] * (third - second)
    labels[third:end] = [DEADLIFT] * (end - third)
    return labels


def read_strength_sets():
    """Alle Kraftsätze aus den Rohdateien, je Einheit (activity_id, Tag, Sätze)."""
    for f in sorted(glob.glob(str(DATA / "garmin_files" / "storage" / "*_EXERCISE_SETS_*.json"))):
        m = re.search(r"_EXERCISE_SETS_(\d+)_(\d{4}-\d{2}-\d{2})T", f)
        sets = []
        for idx, s in enumerate((json.loads(Path(f).read_text()) or {}).get("exerciseSets") or []):
            best = max(s.get("exercises") or [{}], key=lambda e: e.get("probability") or 0)
            weight = s.get("weight")
            sets.append({
                "idx": idx, "type": s.get("setType"), "start": s.get("startTime"),
                "duration": s.get("duration"), "reps": s.get("repetitionCount"),
                "kg": weight / 1000 if weight is not None and weight >= 0 else None,
                "category": best.get("category"), "name": best.get("name"),
                "probability": best.get("probability"),
                "garmin_category": best.get("category"), "garmin_name": best.get("name"),
                "guessed": False,
            })
        active = [s for s in sets if s["type"] == "ACTIVE"]
        # Nur Einheiten mit unerkannten Sätzen anfassen; von Hand benannte bleiben, wie sie sind.
        if any((s["category"] or "UNKNOWN") == "UNKNOWN" for s in active):
            for s, label in zip(active, guess_stronglifts(active)):
                if label and (s["category"], s["name"]) != label:
                    s["category"], s["name"], s["guessed"] = label[0], label[1], True
        # Auch erkannte und von Hand benannte Sätze bekommen den einheitlichen Namen.
        for s in active:
            s["name"] = CANONICAL.get(s["category"], s["name"])
        yield int(m.group(1)), m.group(2), sets


def rebuild_db():
    """Baut die Tabellen jedes Mal komplett aus den Rohdateien neu."""
    summaries = []
    for f in sorted(glob.glob(str(RAW / "daily_summary" / "*" / "*.json"))):
        row = json.loads(Path(f).read_text())
        if not isinstance(row, dict) or row.get("totalKilocalories") is None:
            continue  # Tag ohne Daten
        row["calendarDate"] = Path(f).stem
        summaries.append({k: v for k, v in row.items() if not isinstance(v, (dict, list))})

    hrv = []
    for f in sorted(glob.glob(str(RAW / "hrv" / "*.json"))):
        for s in (json.loads(Path(f).read_text()) or {}).get("hrvSummaries") or []:
            base = s.get("baseline") or {}
            hrv.append((s["calendarDate"], s.get("weeklyAvg"), s.get("lastNightAvg"),
                        s.get("lastNight5MinHigh"), s.get("status"), base.get("lowUpper"),
                        base.get("balancedLow"), base.get("balancedUpper")))

    vo2 = []
    for f in sorted(glob.glob(str(RAW / "vo2max" / "*.json"))):
        for s in json.loads(Path(f).read_text()) or []:
            g, c = s.get("generic") or {}, s.get("cycling") or {}
            day = g.get("calendarDate") or c.get("calendarDate")
            if day:
                vo2.append((day, g.get("vo2MaxPreciseValue"), c.get("vo2MaxPreciseValue"),
                            g.get("fitnessAge")))

    # Kraftsätze: Das Werkzeug verwirft Sätze ohne messageIndex — den lässt Garmin weg,
    # sobald man die Übungen einer Einheit in Connect von Hand benennt. Hier zählt
    # stattdessen die Position in der Rohdatei.
    strength = []
    for activity_id, day, sets in read_strength_sets():
        for s in sets:
            strength.append((activity_id, day, s["idx"], s["type"], s["start"], s["duration"],
                             s["reps"], s["kg"], s["category"], s["name"], s["probability"],
                             s["garmin_category"], s["garmin_name"], int(s["guessed"])))

    tmp = DB.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    cols = sorted({k for row in summaries for k in row} - {"calendarDate"})
    con.execute("CREATE TABLE daily_summary (calendarDate TEXT PRIMARY KEY"
                + "".join(f', "{c}"' for c in cols) + ")")
    con.executemany(
        f"INSERT INTO daily_summary VALUES ({','.join('?' * (len(cols) + 1))})",
        [[row["calendarDate"]] + [row.get(c) for c in cols] for row in summaries],
    )
    con.execute("CREATE TABLE hrv_daily (calendarDate TEXT PRIMARY KEY, weeklyAvg, lastNightAvg,"
                " lastNight5MinHigh, status, baselineLowUpper, baselineBalancedLow,"
                " baselineBalancedUpper)")
    con.executemany("INSERT OR REPLACE INTO hrv_daily VALUES (?,?,?,?,?,?,?,?)", hrv)
    con.execute("CREATE TABLE vo2max_daily (calendarDate TEXT PRIMARY KEY, vo2maxRunning,"
                " vo2maxCycling, fitnessAge)")
    con.executemany("INSERT OR REPLACE INTO vo2max_daily VALUES (?,?,?,?)", vo2)
    # exercise_* ist die korrigierte Fassung, garmin_* was Garmin geliefert hat;
    # guessed = 1, wo die Übung aus dem Aufbau der Einheit erraten wurde.
    con.execute("CREATE TABLE strength_set_all (activity_id INTEGER, calendarDate TEXT, set_idx INTEGER,"
                " set_type, start_time, duration_s, reps, weight_kg, exercise_category,"
                " exercise_name, exercise_probability, garmin_category, garmin_name, guessed INTEGER,"
                " PRIMARY KEY (activity_id, set_idx))")
    con.executemany("INSERT INTO strength_set_all VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", strength)
    con.commit()
    con.close()
    tmp.replace(DB)
    log(f"DB neu gebaut: {len(summaries)} Tage, {len(hrv)} HRV, {len(vo2)} VO2max, "
        f"{len(strength)} Kraftsätze")


def main():
    yesterday = date.today() - timedelta(days=1)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", type=date.fromisoformat,
                    default=yesterday - timedelta(days=6))
    ap.add_argument("--to", dest="end", type=date.fromisoformat, default=yesterday)
    ap.add_argument("--refresh-days", type=int, default=7,
                    help="so viele jüngste Tage auch dann neu holen, wenn sie schon da sind")
    ap.add_argument("--db-only", action="store_true", help="nur die DB aus den Rohdateien bauen")
    ap.add_argument("--last-upload", action="store_true",
                    help="nur ausgeben, wann die Uhr zuletzt zu Garmin hochgeladen hat (Ortszeit)")
    args = ap.parse_args()

    fails = 0
    if not args.db_only:
        tokens = sorted(glob.glob(os.path.expanduser("~/.garminconnect/[0-9]*")))
        if not tokens:
            sys.exit("nicht eingeloggt: erst 'garmin auth' ausführen")
        client = GarminClient()
        client.load(tokens[0])
        if args.last_upload:
            # Eine einzelne Anfrage — damit lässt sich billig prüfen, ob es Neues gibt.
            device = client._connectapi("/device-service/deviceservice/mylastused")
            ms = device["lastUsedDeviceUploadTime"]
            print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)))
            return
        client._load_profile()
        refresh_from = date.today() - timedelta(days=args.refresh_days)
        log(f"extras {args.start} bis {args.end}")
        try:
            fails = fetch_days(client, args.start, args.end, refresh_from)
            fetch_months(client, args.start, args.end, refresh_from)
        except GarminTooManyRequestsError:
            log("Garmin drosselt (429) — später neu starten, Vorhandenes bleibt")
            rebuild_db()
            sys.exit(75)
    rebuild_db()
    if fails:
        log(f"{fails} Tage fehlgeschlagen — beim nächsten Lauf werden sie nachgeholt")
        sys.exit(1)


if __name__ == "__main__":
    main()
