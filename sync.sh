#!/usr/bin/env bash
# Täglicher Garmin-Abgleich. Holt alles seit dem letzten Stand, mindestens aber
# die letzten 7 Tage neu — die Uhr synct manchmal erst Tage später, und ein
# einmal leer geholter Tag würde sonst nie nachgetragen.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

exec > >(tee -a "$LOGS/sync.log") 2>&1
echo "=== $(date '+%F %T') sync"

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

RC=0
"$BIN/garmin" extract --db-path "$DB" --start-date "$START" || RC=$?
# Tageszusammenfassung, Uhr-FIT-Dateien, HRV, VO2max — holt das Werkzeug nicht.
"$BIN/python" "$APP/extras.py" --from "$START" || RC=$?
echo "=== $(date '+%F %T') fertig (ab $START, rc=$RC)"
exit $RC
