#!/usr/bin/env python
"""Kleine Webseite mit einem Knopf: nächste 5x5-Einheit auf heute legen.

Gedacht hinter dem Reverse-Proxy der Grafana-Seite (nur aus dem LAN erreichbar).
GET zeigt die geplante Einheit, POST /heute legt sie auf heute und schickt sie an die Uhr.
Der Garmin-Zugriff läuft über next_workout.py und dasselbe Lock wie der Abgleich.
"""
import html
import json
import os
import sqlite3
import subprocess
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("GARMIN_DATA", "/data"))
APP = Path(__file__).resolve().parent
PORT = int(os.environ.get("BUTTON_PORT", "8000"))
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

STYLE = """<style>
:root{--bg:#111217;--card:#181b1f;--fg:#ccccdc;--muted:#8e8e9e;--ok:#73bf69;--warn:#f2cc0c;--accent:#3d71d9}
@media (prefers-color-scheme: light){:root{--bg:#f4f5f5;--card:#fff;--fg:#24292e;--muted:#6a737d}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:24px 16px}
.card{background:var(--card);border-radius:8px;padding:20px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 12px} table{width:100%;border-collapse:collapse}
td{padding:4px 0;border-bottom:1px solid #8882} td.r{text-align:right}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:12px 20px;font-size:16px;cursor:pointer;width:100%}
.muted{color:var(--muted);font-size:14px} .warn{color:var(--warn)} .ok{color:var(--ok)}
pre{white-space:pre-wrap;font-size:13px;color:var(--muted)} a{color:var(--accent)}
</style>"""


def plan_rows():
    con = sqlite3.connect(f"file:{DATA / 'garmin_extras.db'}?mode=ro", uri=True)
    rows = con.execute("SELECT plan, planned_date, exercise, sets, reps, weight_kg FROM next_workout"
                       " WHERE kind = 'Arbeitssatz' ORDER BY position").fetchall()
    last = con.execute("SELECT max(calendarDate) FROM strength_work").fetchone()[0]
    return rows, last


def page(body):
    return f"<!doctype html><html lang=de><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Nächste Einheit</title>{STYLE}<main>{body}</main></html>"


def overview(message=""):
    rows, last = plan_rows()
    if not rows:
        return page("<div class=card><h1>Keine Einheit geplant</h1></div>")
    plan, planned = rows[0][0], date.fromisoformat(rows[0][1])
    today = date.today()
    days_since = (today - date.fromisoformat(last)).days if last else None
    lines = "".join(f"<tr><td>{html.escape(ex)}</td><td class=r>{s}×{r}</td><td class=r>{'–' if kg is None else f'{kg:g} kg'}</td></tr>"
                    for _, _, ex, s, r, kg in rows)
    ago = {0: "heute", 1: "gestern"}.get(days_since, f"vor {days_since} Tagen")
    when = "heute" if planned == today else f"{WEEKDAYS[planned.weekday()]}, {planned:%d.%m.}"
    hint = ""
    if days_since is not None and days_since < 2:
        hint = (f"<p class=warn>Letzte Einheit war {'heute' if days_since == 0 else 'gestern'} — "
                "zwischen zwei Einheiten mit Kniebeugen gehört ein Ruhetag.</p>")
    button = ("<p class=ok>Ist schon für heute eingeplant.</p>" if planned == today else
              "<form method=post action=heute><button>Heute machen — auf die Uhr schicken</button></form>")
    return page(f"""{message}<div class=card><h1>Nächste Einheit: Plan {plan}</h1>
<p class=muted>Geplant für {when} · letzte Einheit {ago}</p>
<table>{lines}</table></div>
<div class=card>{hint}{button}
<p class=muted>Legt die Einheit in den Garmin-Kalender auf heute und schickt sie an die Uhr; danach dort synchronisieren.</p></div>""")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/index.html"):
            try:
                self._send(200, overview())
            except Exception as exc:
                self._send(500, page(f"<div class=card><h1>Fehler</h1><pre>{html.escape(str(exc))}</pre></div>"))
        else:
            self._send(404, page("<div class=card>Nicht gefunden</div>"))

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/heute":
            return self._send(404, page("<div class=card>Nicht gefunden</div>"))
        args = ["python", str(APP / "next_workout.py"), "--today"]
        if "probe=1" in self.path:  # zum Testen: nur rechnen, nichts zu Garmin schreiben
            args.append("--dry-run")
        try:
            done = subprocess.run(["flock", "-w", "120", str(DATA / ".lock")] + args,
                                  capture_output=True, text=True, timeout=300)
            ok = done.returncode == 0
            out = (done.stdout + done.stderr).strip()
        except subprocess.TimeoutExpired:
            ok, out = False, "Zeitüberschreitung — läuft gerade ein Abgleich? Bitte in ein paar Minuten erneut."
        msg = ("<div class=card><p class=ok>Erledigt — auf der Uhr synchronisieren.</p>" if ok else
               "<div class=card><p class=warn>Hat nicht geklappt.</p>") + f"<pre>{html.escape(out[-1500:])}</pre></div>"
        print(f"POST /heute rc={'0' if ok else 'fehler'}", flush=True)
        self._send(200 if ok else 500, overview(msg))

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"Workout-Knopf auf Port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
