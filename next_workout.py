#!/usr/bin/env python
"""Berechnet die nächste StrongLifts-5x5-Einheit und bringt sie als Workout auf die Uhr.

Aus der Historie (strength_set_all) ergibt sich, welcher Plan dran ist (A: Kniebeuge,
Bankdrücken, Rudern — B: Kniebeuge, Overhead Press, Kreuzheben) und mit welchem Gewicht.
Daraus entsteht EIN Workout in Garmin Connect, das bei jeder Änderung überschrieben,
ans zuletzt benutzte Gerät geschickt und in den Kalender gelegt wird. Die Uhr führt
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
LIFTS = {
    "squat": dict(cat="SQUAT", name="BARBELL_BACK_SQUAT", label="Kniebeuge", sets=5, step=2.5, fast=5),
    "bench": dict(cat="BENCH_PRESS", name="BARBELL_BENCH_PRESS", label="Bankdrücken", sets=5, step=2.5, fast=5),
    "row": dict(cat="ROW", name="BARBELL_ROW", label="Rudern", sets=5, step=2.5, fast=5),
    "press": dict(cat="SHOULDER_PRESS", name="OVERHEAD_BARBELL_PRESS", label="Overhead Press",
                  sets=5, step=2.5, slow_from=30),
    "deadlift": dict(cat="DEADLIFT", name="BARBELL_DEADLIFT", label="Kreuzheben", sets=1, step=5),
}
PLANS = {"A": ["squat", "bench", "row"], "B": ["squat", "press", "deadlift"]}
REPS = 5
BAR = 20.0
REST_DAYS = 2          # übernächster Tag
RESTART_AFTER_DAYS = 28  # längere Pause bei einer Übung: mit 85 % wieder einsteigen
RESTART_FACTOR = 0.85
FAILS_BEFORE_DELOAD = 3
DELOAD_FACTOR = 0.9


def round_plate(kg):
    return max(BAR, round(kg / 2.5) * 2.5)


def history():
    """Je Einheit (Tag, {Übung: [(kg, Wdh.), …]}), älteste zuerst."""
    by_name = {v["name"]: k for k, v in LIFTS.items()}
    con = sqlite3.connect(f"file:{DATA / 'garmin_extras.db'}?mode=ro", uri=True)
    sessions = {}
    for aid, day, name, kg, reps in con.execute(
            "SELECT activity_id, calendarDate, exercise_name, weight_kg, reps FROM strength_set_all"
            " WHERE set_type = 'ACTIVE' ORDER BY activity_id, set_idx"):
        lift = by_name.get(name)
        if lift and kg and reps:
            sessions.setdefault((aid, day), {}).setdefault(lift, []).append((kg, reps))
    return [(date.fromisoformat(day), lifts) for (_, day), lifts in sorted(sessions.items())]


def outcome(sets, lift):
    """Arbeitsgewicht einer Übung in einer Einheit und ob alle Sätze geschafft wurden."""
    top = max(kg for kg, _ in sets)
    if LIFTS[lift]["sets"] == 1:
        kg = top
    else:  # das Gewicht mit den meisten Sätzen nahe am Maximum, nicht die Aufwärmsätze
        count = {}
        for w, _ in sets:
            if w >= 0.8 * top:
                count[w] = count.get(w, 0) + 1
        kg = max(count, key=lambda w: (count[w], w))
    done = sum(1 for w, reps in sets if w == kg and reps >= REPS)
    return kg, done >= LIFTS[lift]["sets"]


def next_weight(lift, past, planned):
    """past: [(Tag, kg, geschafft), …] dieser Übung, älteste zuerst."""
    cfg = LIFTS[lift]
    if not past:
        return BAR, "erste Einheit"
    last_day, kg, ok = past[-1]
    if (planned - last_day).days > RESTART_AFTER_DAYS:
        return round_plate(kg * RESTART_FACTOR), f"Wiedereinstieg nach Pause (zuletzt {kg:g} kg am {last_day:%d.%m.%Y})"
    streak = 0  # wie oft in Folge dasselbe Gewicht mit demselben Ausgang
    for _, w, o in reversed(past):
        if w != kg or o != ok:
            break
        streak += 1
    if not ok:
        if streak >= FAILS_BEFORE_DELOAD:
            return round_plate(kg * DELOAD_FACTOR), f"{streak}× nicht geschafft bei {kg:g} kg → 10 % zurück"
        return kg, f"zuletzt nicht alle Sätze geschafft → {kg:g} kg wiederholen"
    if cfg.get("slow_from") and kg >= cfg["slow_from"] and streak < 2:
        return kg, f"ab {cfg['slow_from']:g} kg erst nach zwei geschafften Einheiten steigern"
    best = max(w for _, w, _ in past)
    step = cfg["fast"] if cfg.get("fast") and kg < 0.75 * best else cfg["step"]
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


def plan_next(today):
    sessions = history()
    if not sessions:
        return None
    last_day, last_lifts = sessions[-1]
    plan = "A" if "deadlift" in last_lifts or "press" in last_lifts else "B"
    planned = max(today, last_day + timedelta(days=REST_DAYS))
    exercises = []
    for lift in PLANS[plan]:
        past = [(day, *outcome(lifts[lift], lift)) for day, lifts in sessions if lift in lifts]
        kg, why = next_weight(lift, past, planned)
        exercises.append({"lift": lift, "kg": kg, "why": why, "warmup": warmups(lift, kg)})
    return {"plan": plan, "date": planned.isoformat(), "after": last_day.isoformat(), "exercises": exercises}


def build_workout(plan):
    """Garmin-Workout: Aufwärmsätze einzeln, Arbeitssätze als Wiederholungsblock."""
    order = 0

    def nxt():
        nonlocal order
        order += 1
        return order

    def exercise(lift, kg, reps):
        cfg = LIFTS[lift]
        return {
            "type": "ExecutableStepDTO", "stepOrder": nxt(),
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
            "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True},
            "endConditionValue": float(reps),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
            "category": cfg["cat"], "exerciseName": cfg["name"],
            "weightValue": kg * 1000.0, "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0},
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
        lift, kg, sets = ex["lift"], ex["kg"], LIFTS[ex["lift"]]["sets"]
        for w, reps in ex["warmup"]:
            steps += [exercise(lift, w, reps), rest(60)]
        # Schwere Sätze brauchen längere Pausen; LAP auf der Uhr überspringt sie.
        pause = 180 if kg >= 60 else 120
        if sets == 1:
            steps += [exercise(lift, kg, REPS), rest(pause)]
        else:
            group = {
                "type": "RepeatGroupDTO", "stepOrder": nxt(),
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": sets, "smartRepeat": False,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                "endConditionValue": float(sets),
            }
            group["workoutSteps"] = [exercise(lift, kg, REPS), rest(pause)]
            steps.append(group)
    sport = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
    summary = " · ".join(f"{LIFTS[e['lift']]['label']} {e['kg']:g}" for e in plan["exercises"])
    return {
        "workoutName": f"5x5 {plan['plan']}: " + "/".join(f"{e['kg']:g}" for e in plan["exercises"]),
        "description": f"{summary} kg. Automatisch berechnet aus der Einheit vom {plan['after']}.",
        "sportType": sport, "estimatedDurationInSecs": 0,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}],
    }


def describe(plan):
    lines = [f"Nächste Einheit: Plan {plan['plan']} am {date.fromisoformat(plan['date']):%d.%m.%Y}"]
    for ex in plan["exercises"]:
        cfg = LIFTS[ex["lift"]]
        warm = ", ".join(f"{w:g}×{r}" for w, r in ex["warmup"]) or "–"
        lines.append(f"  {cfg['label']:<15} {cfg['sets']}×{REPS} mit {ex['kg']:g} kg  (Aufwärmen: {warm}; {ex['why']})")
    return "\n".join(lines)


def open_client():
    from garmin_health_data.garmin_client import GarminClient
    tokens = sorted(glob.glob(os.path.expanduser("~/.garminconnect/[0-9]*")))
    if not tokens:
        sys.exit("nicht eingeloggt: erst 'garmin auth' ausführen")
    client = GarminClient()
    client.load(tokens[0])
    return client


def publish(plan, workout, state):
    """Legt das Workout an bzw. überschreibt es, schickt es ans Gerät, plant es ein."""
    client = open_client()
    digest = hashlib.sha1(json.dumps(workout, sort_keys=True).encode()).hexdigest()
    workout_id = state.get("workout_id")
    if state.get("hash") != digest or not workout_id:
        if workout_id:
            try:  # überschreiben: gleiche ID, der Kalendereintrag bleibt gültig
                client._request("PUT", f"/workout-service/workout/{workout_id}",
                                json=workout | {"workoutId": workout_id})
            except Exception as exc:
                if "API Error 404" not in str(exc):
                    raise
                workout_id = None  # in Garmin Connect gelöscht → neu anlegen
                state.pop("schedule_id", None)
        if not workout_id:
            workout_id = client._request("POST", "/workout-service/workout", json=workout).json()["workoutId"]
        device = client._connectapi("/device-service/deviceservice/mylastused")
        client._request("POST", "/device-service/devicemessage/messages", json=[{
            "deviceId": device["userDeviceId"], "messageUrl": f"workout-service/workout/FIT/{workout_id}",
            "messageType": "workouts", "groupName": None, "messageName": workout["workoutName"],
            "priority": 1, "fileType": "FIT", "metaDataId": workout_id}])
        state.update(workout_id=workout_id, hash=digest)
        print(f"Workout {workout_id} geschrieben und an {device.get('lastUsedDeviceName')} geschickt")
    if state.get("schedule_date") != plan["date"] or not state.get("schedule_id"):
        if state.get("schedule_id"):
            try:
                client._request("DELETE", f"/workout-service/schedule/{state['schedule_id']}")
            except Exception as exc:
                if "API Error 404" not in str(exc):
                    raise
        scheduled = client._request("POST", f"/workout-service/schedule/{workout_id}",
                                    json={"date": plan["date"]}).json()
        state.update(schedule_id=scheduled.get("workoutScheduleId"), schedule_date=plan["date"])
        print(f"im Kalender eingeplant für {plan['date']}")
    state["plan"] = plan


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="nur berechnen und anzeigen")
    args = ap.parse_args()

    plan = plan_next(date.today())
    if not plan:
        print("keine 5x5-Einheit in der Historie — nichts zu planen")
        return
    workout = build_workout(plan)
    print(describe(plan))
    if args.dry_run:
        print(f"Workout-Name: {workout['workoutName']}")
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    before = json.dumps(state, sort_keys=True)
    publish(plan, workout, state)
    if json.dumps(state, sort_keys=True) != before:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    else:
        print("unverändert — nichts zu Garmin geschrieben")


if __name__ == "__main__":
    main()
