#!/usr/bin/env bash
# Garmin-Abgleich.
#   sync.sh         alles seit dem letzten Stand, mindestens aber die letzten 7 Tage neu —
#                   die Uhr synct manchmal erst Tage später, und ein einmal leer geholter
#                   Tag würde sonst nie nachgetragen. Reicht bis gestern.
#   sync.sh since [Datum]   ab diesem Tag (Vorgabe: heute) bis einschließlich heute —
#                   für das Nachziehen, sobald die Uhr neu hochgeladen hat
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

MODE=${1:-full}
exec > >(tee -a "$LOGS/sync.log") 2>&1
echo "=== $(date '+%F %T') sync ($MODE)"

# Ein Lock für sync und backfill: nie zwei Abrufe gleichzeitig gegen Garmin
# (das Refresh-Token rotiert, der zweite Prozess hielte ein totes).
exec 9>"$DATA/.lock"
if ! flock -n 9; then
  echo "anderer Lauf aktiv, übersprungen"
  exit 0
fi

# Ohne Token würde das Werkzeug nach Zugangsdaten fragen — unbeaufsichtigt sinnlos.
if ! ls -d "$HOME"/.garminconnect/[0-9]* >/dev/null 2>&1; then
  echo "nicht eingeloggt: erst 'garmin auth' ausführen"
  exit 1
fi

RC=0
if [[ "$MODE" == since ]]; then
  TODAY=$(date +%F)
  START=${2:-$TODAY}
  # Nie weiter zurück als eine Woche — den Rest holt der volle Abgleich.
  FLOOR=$(date -d '7 days ago' +%F)
  [[ "$START" < "$FLOOR" ]] && START=$FLOOR
  # --end-date ist exklusiv, morgen schließt also den laufenden Tag ein.
  "$BIN/garmin" extract --db-path "$DB" --start-date "$START" --end-date "$(date -d tomorrow +%F)" || RC=$?
  "$BIN/python" "$APP/extras.py" --from "$START" --to "$TODAY" || RC=$?
else
  START=$("$BIN/python" - "$DB" <<'EOF'
import os, sys
from datetime import date, timedelta
from garmin_health_data.db import get_latest_date

db = sys.argv[1]
floor = date.today() - timedelta(days=7)
latest = get_latest_date(db) if os.path.exists(db) else None
print(min(latest + timedelta(days=1), floor) if latest else floor)
EOF
  )
  "$BIN/garmin" extract --db-path "$DB" --start-date "$START" || RC=$?
  # Tageszusammenfassung, Uhr-FIT-Dateien, HRV, VO2max — holt das Werkzeug nicht.
  "$BIN/python" "$APP/extras.py" --from "$START" || RC=$?
fi
# MyFitnessPal-Tagebuch (nur wenn data/.mfp/cookies.txt da ist); ein Fehler dort hält den
# Garmin-Abgleich nicht auf. Danach die Zusatz-DB neu bauen, damit die Tabellen stimmen.
if [[ -f "$DATA/.mfp/cookies.txt" ]]; then
  MFP_PY=${MFP_PYTHON:-/opt/mfp/bin/python}
  [[ -x "$MFP_PY" ]] || MFP_PY="$APP/.venv-mfp/bin/python"
  "$MFP_PY" "$APP/mfp_sync.py" && "$BIN/python" "$APP/extras.py" --db-only >/dev/null || true
fi

# Nächste 5x5-Einheit als Workout nach Garmin Connect schreiben — nur wenn gewünscht,
# denn das ist der einzige Schritt, der im Garmin-Konto etwas ändert.
if [[ "${PUSH_NEXT_WORKOUT:-0}" == 1 ]]; then
  "$BIN/python" "$APP/next_workout.py" || RC=$?
fi
echo "=== $(date '+%F %T') fertig (ab $START, rc=$RC)"
exit $RC
