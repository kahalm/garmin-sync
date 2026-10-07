#!/usr/bin/env python
"""Berechnet die nächste StrongLifts-5x5-Einheit und bringt sie als Workout auf die Uhr.

Aus der Historie (strength_set_all) ergibt sich, welcher Plan dran ist (A: Kniebeuge,
Bankdrücken, Rudern — B: Kniebeuge, Overhead Press, Kreuzheben) und mit welchem Gewicht.
Daraus entsteht ein Workout in Garmin Connect, das bei jeder Änderung neu angelegt
(das alte wird gelöscht), ans zuletzt benutzte Gerät geschickt und in den Kalender gelegt wird. Die Uhr führt
dann Satz für Satz durch und benennt die Übungen gleich richtig.

Schreibt ins Garmin-Konto, aber nur an dem einen Workout und dem einen Kalendereintrag,
die es selbst angelegt hat (gemerkt in data/.next_workout.json).

  next_workout.py             berechnen und, falls geändert, zu Garmin schreiben
  next_workout.py --dry-run   nur berechnen und anzeigen
"""
import argparse
import glob
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

DATA = Path(os.environ.get("GARMIN_DATA") or Path(__file__).resolve().parent / "data")
STATE = DATA / ".next_workout.json"

# sets: Arbeitssätze; step: Steigerung je geschaffter Einheit; fast: größere Steigerung,
# solange das Gewicht unter 75 % des eigenen Bestwerts liegt (Wiedereinstieg);
# slow_from: ab diesem Gewicht erst nach zwei geschafften Einheiten steigern.
# micro_from/micro_step: ab diesem Gewicht in kleinen Schritten (Mini-Scheiben) steigern.
LIFTS = {
    "squat": dict(cat="SQUAT", name="BARBELL_BACK_SQUAT", label="Kniebeuge", sets=5, step=2.5, fast=5),
    "bench": dict(cat="BENCH_PRESS", name="BARBELL_BENCH_PRESS", label="Bankdrücken", sets=5, step=2.5, fast=5),
    "row": dict(cat="ROW", name="BARBELL_ROW", label="Rudern", sets=5, step=2.5, fast=5),
    # Ab 30 kg in 1-kg-Schritten (0,5-kg-Scheiben je Seite) — bei der Overhead Press sind
    # 2,5 kg schon 7–8 % mehr, daran ist der Nutzer in jedem Block hängen geblieben.
    "press": dict(cat="SHOULDER_PRESS", name="OVERHEAD_BARBELL_PRESS", label="Overhead Press",
                  sets=5, step=2.5, micro_from=30, micro_step=1.0),
    "deadlift": dict(cat="DEADLIFT", name="BARBELL_DEADLIFT", label="Kreuzheben", sets=1, step=5),
}
# Ergänzung am Ende jeder Einheit, ohne Gewicht und ohne automatische Steigerung —
# leichter wird es über ein dünneres Band (Wunsch des Nutzers, 05.10.2026).
ACCESSORIES = {
    "chin": dict(cat="PULL_UP", name="BAND_ASSISTED_CHIN_UP", label="Klimmzug mit Band (Untergriff)",
                 sets=3, reps=5),
    # in den Ellbogen-Schlaufen hängend, gezielte Rumpfübung
    "knee": dict(cat="LEG_RAISE", name="HANGING_KNEE_RAISE", label="Hängendes Knieheben (Schlaufen)",
                 sets=2, reps=10),
}
PLANS = {"A": ["squat", "bench", "row"], "B": ["squat", "press", "deadlift"]}
REPS = 5
BAR = 20.0
WORK_REST_SECONDS = 90
REST_DAYS = 2          # übernächster Tag
# Wochentage ohne Training (0 = Montag … 6 = Sonntag), per Umgebung änderbar, z. B. "2,4".
# Fällt der Termin darauf, rückt er auf den nächsten freien Tag.
BLOCKED_WEEKDAYS = {int(d) for d in os.environ.get("TRAINING_BLOCKED_WEEKDAYS", "").split(",") if d.strip()}
RESTART_AFTER_DAYS = 28  # längere Pause bei einer Übung: mit 85 % wieder einsteigen
RESTART_FACTOR = 0.85
FAILS_BEFORE_DELOAD = 3
DELOAD_FACTOR = 0.9


def round_plate(kg, lift=None):
    """Auf auflegbare Gewichte runden: 2,5-kg-Schritte, mit Mini-Scheiben 0,5 kg."""
    grain = 0.5 if lift and LIFTS[lift].get("micro_from") and kg >= LIFTS[lift]["micro_from"] else 2.5
    return max(BAR, round(kg / grain) * grain)


def history():
    """Je Einheit (Tag, {Übung: (Arbeitsgewicht, geschafft)}), älteste zuerst.

    Das Arbeitsgewicht je Einheit rechnet extras.py aus (Tabelle strength_work).
    """
    by_name = {v["name"]: k for k, v in LIFTS.items()}
    con = sqlite3.connect(f"file:{DATA / 'garmin_extras.db'}?mode=ro", uri=True)
    sessions = {}
    for aid, day, name, kg, ok in con.execute(
            "SELECT activity_id, calendarDate, exercise_name, work_kg, success FROM strength_work"):
        if name in by_name:
            sessions.setdefault((aid, day), {})[by_name[name]] = (kg, bool(ok))
    return [(date.fromisoformat(day), lifts) for (_, day), lifts in sorted(sessions.items())]


def next_weight(lift, past, planned):
    """past: [(Tag, kg, geschafft), …] dieser Übung, älteste zuerst."""
    cfg = LIFTS[lift]
    if not past:
        return BAR, "erste Einheit"
    last_day, kg, ok = past[-1]
    if (planned - last_day).days > RESTART_AFTER_DAYS:
        return round_plate(kg * RESTART_FACTOR, lift), f"Wiedereinstieg nach Pause (zuletzt {kg:g} kg am {last_day:%d.%m.%Y})"
    streak = 0  # wie oft in Folge dasselbe Gewicht mit demselben Ausgang
    for _, w, o in reversed(past):
        if w != kg or o != ok:
            break
        streak += 1
    if not ok:
        if streak >= FAILS_BEFORE_DELOAD:
            return round_plate(kg * DELOAD_FACTOR, lift), f"{streak}× nicht geschafft bei {kg:g} kg → 10 % zurück"
        return kg, f"zuletzt nicht alle Sätze geschafft → {kg:g} kg wiederholen"
    if cfg.get("slow_from") and kg >= cfg["slow_from"] and streak < 2:
        return kg, f"ab {cfg['slow_from']:g} kg erst nach zwei geschafften Einheiten steigern"
    best = max(w for _, w, _ in past)
    step = cfg["fast"] if cfg.get("fast") and kg < 0.75 * best else cfg["step"]
    if cfg.get("micro_from") and kg >= cfg["micro_from"]:
        step = cfg["micro_step"]
    elif cfg.get("micro_from") and kg + step > cfg["micro_from"]:
        step = cfg["micro_from"] - kg  # nicht über die Schwelle springen
    return kg + step, f"geschafft → +{step:g} kg"


def warmups(lift, kg):
    """Aufwärmsätze als [(kg, Wdh.)] — so, wie sie in der Historie üblich waren."""
    if lift == "deadlift":
        ladder = [40, 50] if kg <= 70 else [60, 70] if kg <= 85 else [60, 80]
        sets = [(w, REPS) for w in ladder if w <= kg - 10]
        if kg >= 100:
            sets.append((kg - 10, 3))
        return sets
    if lift == "row":  # kommt nach dem Bankdrücken, ein leichter Satz genügt
        return [(30.0 if kg >= 40 else BAR, REPS)] if kg > BAR else []
    sets = [(BAR, REPS), (BAR, REPS)] if kg > BAR else []
    if lift == "squat":
        if kg >= 50:
            sets.append((40.0, REPS))
        if kg >= 60:
            sets.append((float(round((kg - 10) / 10) * 10), 3))
        elif 40 <= kg < 50:
            sets.append((30.0, 3))
    elif kg >= 40:  # Bankdrücken, Overhead Press
        sets.append((30.0, 3))
    return sets


def plan_next(today, fixed_date=None):
    """fixed_date: vom Nutzer festgelegter Termin (Knopf „heute einplanen“) statt der Regel."""
    sessions = history()
    if not sessions:
        return None
    last_day, last_lifts = sessions[-1]
    plan = "A" if "deadlift" in last_lifts or "press" in last_lifts else "B"
    planned = max(today, last_day + timedelta(days=REST_DAYS))
    while planned.weekday() in BLOCKED_WEEKDAYS and len(BLOCKED_WEEKDAYS) < 7:
        planned += timedelta(days=1)
    if fixed_date:
        planned = fixed_date
    exercises = []
    for lift in PLANS[plan]:
        past = [(day, *lifts[lift]) for day, lifts in sessions if lift in lifts]
        kg, why = next_weight(lift, past, planned)
        exercises.append({"lift": lift, "kg": kg, "why": why, "warmup": warmups(lift, kg)})
    for lift in ACCESSORIES:
        why = {"chin": "mit Band; leichter wird es mit dünnerem Band",
               "knee": "Rumpf; später Beine gestreckt oder mehr Wiederholungen"}[lift]
        exercises.append({"lift": lift, "kg": None, "why": why, "warmup": []})
    return {"plan": plan, "date": planned.isoformat(), "after": last_day.isoformat(), "exercises": exercises}


def cfg_of(lift):
    return LIFTS.get(lift) or ACCESSORIES[lift]


def build_workout(plan):
    """Garmin-Workout: Aufwärmsätze einzeln, Arbeitssätze als Wiederholungsblock."""
    order = 0

    def nxt():
        nonlocal order
        order += 1
        return order

    def exercise(lift, kg, reps):
        cfg = cfg_of(lift)
        weight = {} if kg is None else {
            "weightValue": kg * 1000.0, "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}}
        return weight | {
            "type": "ExecutableStepDTO", "stepOrder": nxt(),
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
            "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True},
            "endConditionValue": float(reps),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
            "category": cfg["cat"], "exerciseName": cfg["name"],
        }

    def rest(seconds):
        return {
            "type": "ExecutableStepDTO", "stepOrder": nxt(),
            "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": float(seconds),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
        }

    steps = []
    for ex in plan["exercises"]:
        lift, kg, sets = ex["lift"], ex["kg"], cfg_of(ex["lift"])["sets"]
        reps = cfg_of(lift).get("reps", REPS)
        for w, reps in ex["warmup"]:
            steps += [exercise(lift, w, reps), rest(60)]
        # Pause zwischen Arbeitssätzen, unabhängig vom Gewicht (Wunsch des Nutzers);
        # LAP auf der Uhr beendet sie vorzeitig.
        pause = WORK_REST_SECONDS
        if sets == 1:
            steps += [exercise(lift, kg, reps), rest(pause)]
        else:
            group = {
                "type": "RepeatGroupDTO", "stepOrder": nxt(),
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": sets, "smartRepeat": False,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                "endConditionValue": float(sets),
            }
            group["workoutSteps"] = [exercise(lift, kg, reps), rest(pause)]
            steps.append(group)
    sport = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
    main = [e for e in plan["exercises"] if e["kg"] is not None]
    summary = " · ".join(f"{LIFTS[e['lift']]['label']} {e['kg']:g}" for e in main)
    return {
        "workoutName": f"5x5 {plan['plan']}: " + "/".join(f"{e['kg']:g}" for e in main),
        "description": f"{summary} kg. Automatisch berechnet aus der Einheit vom {plan['after']}.",
        "sportType": sport, "estimatedDurationInSecs": 0,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}],
    }


def describe(plan):
    lines = [f"Nächste Einheit: Plan {plan['plan']} am {date.fromisoformat(plan['date']):%d.%m.%Y}"]
    for ex in plan["exercises"]:
        cfg = cfg_of(ex["lift"])
        warm = ", ".join(f"{w:g}×{r}" for w, r in ex["warmup"]) or "–"
        load = "Körpergewicht" if ex["kg"] is None else f"{ex['kg']:g} kg"
        lines.append(f"  {cfg['label']:<15} {cfg['sets']}×{cfg.get('reps', REPS)} mit {load}  (Aufwärmen: {warm}; {ex['why']})")
    return "\n".join(lines)


def write_plan_table(plan):
    """Legt den Plan als Tabelle next_workout in die Zusatz-DB — für Dashboards und den Chat."""
    rows = []
    for ex in plan["exercises"]:
        cfg = cfg_of(ex["lift"])
        groups = []  # gleiche Aufwärmsätze zusammenfassen: 20×5, 20×5 → 2 Sätze
        for kg, reps in ex["warmup"]:
            if groups and groups[-1][1:] == [kg, reps]:
                groups[-1][0] += 1
            else:
                groups.append([1, kg, reps])
        for sets, kg, reps in groups:
            rows.append(("Aufwärmen", cfg, sets, reps, kg, None))
        rows.append(("Arbeitssatz", cfg, cfg["sets"], cfg.get("reps", REPS), ex["kg"], ex["why"]))
    con = sqlite3.connect(DATA / "garmin_extras.db")
    with con:
        con.execute("DROP TABLE IF EXISTS next_workout")
        con.execute("CREATE TABLE next_workout (position INTEGER PRIMARY KEY, plan, planned_date, exercise,"
                    " exercise_name, kind, sets INTEGER, reps INTEGER, weight_kg REAL, reason)")
        con.executemany(
            "INSERT INTO next_workout VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(i, plan["plan"], plan["date"], cfg["label"], cfg["name"], kind, sets, reps, kg, why)
             for i, (kind, cfg, sets, reps, kg, why) in enumerate(rows, 1)])
    con.close()


def open_client():
    from garmin_health_data.garmin_client import GarminClient
    tokens = sorted(glob.glob(os.path.expanduser("~/.garminconnect/[0-9]*")))
    if not tokens:
        sys.exit("nicht eingeloggt: erst 'garmin auth' ausführen")
    client = GarminClient()
    client.load(tokens[0])
    return client


def _delete(client, path):
    """DELETE, das ein schon Gelöschtes (404) stillschweigend hinnimmt."""
    try:
        client._request("DELETE", path)
    except Exception as exc:
        if "API Error 404" not in str(exc):
            raise


def publish(plan, workout, state):
    """Legt das Workout neu an, schickt es ans Gerät, plant es ein und räumt das alte weg.

    Bei jeder Änderung entsteht ein NEUES Workout statt das alte zu überschreiben: Die
    fenix 6 übernimmt geänderte Gewichte eines schon gespeicherten Workouts sonst oft nicht.
    """
    client = open_client()
    digest = hashlib.sha1(json.dumps(workout, sort_keys=True).encode()).hexdigest()
    old_id, old_schedule = state.get("workout_id"), state.get("schedule_id")
    if state.get("hash") != digest or not old_id:
        workout_id = client._request("POST", "/workout-service/workout", json=workout).json()["workoutId"]
        device = client._connectapi("/device-service/deviceservice/mylastused")
        client._request("POST", "/device-service/devicemessage/messages", json=[{
            "deviceId": device["userDeviceId"], "messageUrl": f"workout-service/workout/FIT/{workout_id}",
            "messageType": "workouts", "groupName": None, "messageName": workout["workoutName"],
            "priority": 1, "fileType": "FIT", "metaDataId": workout_id}])
        scheduled = client._request("POST", f"/workout-service/schedule/{workout_id}",
                                    json={"date": plan["date"]}).json()
        state.update(workout_id=workout_id, hash=digest, schedule_date=plan["date"],
                     schedule_id=scheduled.get("workoutScheduleId"))
        print(f"Workout {workout_id} angelegt, an {device.get('lastUsedDeviceName')} geschickt,"
              f" eingeplant für {plan['date']}")
        # Erst wenn das neue steht, das alte samt Kalendereintrag entfernen.
        if old_schedule:
            _delete(client, f"/workout-service/schedule/{old_schedule}")
        if old_id and old_id != workout_id:
            _delete(client, f"/workout-service/workout/{old_id}")
            print(f"altes Workout {old_id} gelöscht")
    elif state.get("schedule_date") != plan["date"] or not state.get("schedule_id"):
        if old_schedule:
            _delete(client, f"/workout-service/schedule/{old_schedule}")
        scheduled = client._request("POST", f"/workout-service/schedule/{old_id}",
                                    json={"date": plan["date"]}).json()
        state.update(schedule_id=scheduled.get("workoutScheduleId"), schedule_date=plan["date"])
        print(f"im Kalender verschoben auf {plan['date']}")
    state["plan"] = plan


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="nur berechnen und anzeigen")
    ap.add_argument("--today", action="store_true",
                    help="nächste Einheit auf heute legen (gilt, bis sie gemacht ist oder der Tag vorbei ist)")
    args = ap.parse_args()

    today = date.today()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    before = json.dumps(state, sort_keys=True)
    plan = plan_next(today)
    if not plan:
        print("keine 5x5-Einheit in der Historie — nichts zu planen")
        return
    # Ein von Hand gesetzter Termin gilt nur für genau diese nächste Einheit und nur,
    # solange der Tag nicht vorbei ist — danach greift wieder die Regel.
    if args.today:
        state["pinned"] = {"after": plan["after"], "date": today.isoformat()}
    pinned = state.get("pinned")
    if pinned and pinned["after"] == plan["after"] and pinned["date"] >= today.isoformat():
        plan = plan_next(today, date.fromisoformat(pinned["date"]))
    else:
        state.pop("pinned", None)
    workout = build_workout(plan)
    print(describe(plan))
    if args.dry_run:
        print(f"Workout-Name: {workout['workoutName']}")
        return
    write_plan_table(plan)
    publish(plan, workout, state)
    if json.dumps(state, sort_keys=True) != before:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    else:
        print("unverändert — nichts zu Garmin geschrieben")


if __name__ == "__main__":
    main()
