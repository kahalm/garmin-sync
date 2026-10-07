#!/usr/bin/env python
"""Holt das MyFitnessPal-Ernährungstagebuch (Lebensmittel, Makros) ohne Premium.

Nutzt python-myfitnesspal, das die Website ausliest — inoffiziell, kann jederzeit brechen.
Angemeldet wird über die Cookies einer Browser-Sitzung: einmal auf myfitnesspal.com
einloggen, die Cookies als cookies.txt (Netscape-Format) exportieren und als
data/.mfp/cookies.txt ablegen. Die Datei wird nach jedem Lauf mit den aufgefrischten
Cookies zurückgeschrieben, damit die Sitzung lebendig bleibt.

Rohdaten je Tag landen in data/extras/mfp/<Jahr>/<Datum>.json; extras.py baut daraus
die Tabellen food_entry und food_daily.

  mfp_sync.py                    letzte 7 Tage
  mfp_sync.py --from 2025-01-01  ab Datum (vorhandene Tage älter als 7 Tage überspringen)
"""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import date, timedelta
from http.cookiejar import MozillaCookieJar
from pathlib import Path

DATA = Path(os.environ.get("GARMIN_DATA") or Path(__file__).resolve().parent / "data")
COOKIES = DATA / ".mfp" / "cookies.txt"  # eigener Ordner, damit andere Container ihn verdecken können
RAW = DATA / "extras" / "mfp"
PAUSE = 2.0  # Sekunden zwischen Tagen — die Website schonen


def log(msg):
    print(f"{time.strftime('%F %T')} mfp: {msg}", flush=True)


def day_to_dict(day):
    meals = []
    for meal in day.meals:
        meals.append({
            "name": meal.name,
            "entries": [{"name": e.name, "quantity": e.quantity, "unit": e.unit,
                         "nutrition": {k: float(v) for k, v in e.nutrition_information.items()}}
                        for e in meal.entries],
        })
    return {"date": day.date.isoformat(), "complete": bool(day.complete),
            "goals": {k: float(v) for k, v in day.goals.items()},
            "totals": {k: float(v) for k, v in day.totals.items()}, "meals": meals}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", type=date.fromisoformat,
                    default=date.today() - timedelta(days=6))
    ap.add_argument("--to", dest="end", type=date.fromisoformat, default=date.today())
    args = ap.parse_args()

    # Eigenes Lock: ein langer Nachhol-Lauf soll die Garmin-Abgleiche nicht blockieren,
    # aber zwei MyFitnessPal-Läufe dürfen nicht gleichzeitig die Cookie-Datei schreiben.
    lock = open(DATA / ".mfp" / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("anderer MyFitnessPal-Lauf aktiv — übersprungen")
        return 0
    if not COOKIES.exists():
        log(f"keine Cookies ({COOKIES.name}) — MyFitnessPal übersprungen")
        return 0
    import myfitnesspal
    from myfitnesspal.exceptions import MyfitnesspalLoginError

    jar = MozillaCookieJar(str(COOKIES))
    jar.load(ignore_discard=True, ignore_expires=True)
    try:
        client = myfitnesspal.Client(cookiejar=jar)
    except MyfitnesspalLoginError:
        log("Anmeldung abgelaufen — Cookies neu aus dem Browser exportieren (data/.mfp/cookies.txt)")
        return 2

    fresh_from = date.today() - timedelta(days=7)
    day, fetched, fails = args.end, 0, 0
    while day >= args.start:
        target = RAW / str(day.year) / f"{day.isoformat()}.json"
        if day >= fresh_from or not target.exists():
            try:
                data = day_to_dict(client.get_date(day))
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False))
                tmp.replace(target)
                fetched += 1
            except Exception as exc:
                fails += 1
                log(f"FEHLER {day}: {exc!r:.200}")
                if fails >= 5:
                    log("5 Fehler — Abbruch")
                    break
            time.sleep(PAUSE)
        day -= timedelta(days=1)

    # aufgefrischte Sitzungs-Cookies zurückschreiben
    for cookie in client.session.cookies:
        jar.set_cookie(cookie)
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(COOKIES, 0o600)
    log(f"{fetched} Tage geholt ({args.start} bis {args.end}), {fails} Fehler")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
