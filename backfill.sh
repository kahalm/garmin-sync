#!/usr/bin/env bash
# Einmaliger Komplettabruf der Historie, monatsweise von heute rückwärts.
#   ./backfill.sh 2017-01-01
# Fertige Monate bekommen eine Marke und werden beim nächsten Aufruf übersprungen;
# bricht ein Monat ab (z. B. Garmin drosselt mit 429), einfach später neu starten.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

FROM=${1:?Startdatum fehlt, z. B. ./backfill.sh 2017-01-01}
FROM_MONTH=$(date -d "$FROM" +%Y-%m-01)

exec > >(tee -a "$LOGS/backfill.log") 2>&1
echo "=== $(date '+%F %T') backfill ab $FROM_MONTH"

exec 9>"$DATA/.lock"
if ! flock -w 600 9; then
  echo "Lock nach 10 min nicht frei (läuft sync.sh?), Abbruch"
  exit 1
fi

MONTH=$(date +%Y-%m-01)
TODAY=$(date +%F)
while [[ ! "$MONTH" < "$FROM_MONTH" ]]; do
  KEY=${MONTH:0:7}
  if [[ ! -e "$MARKS/$KEY.done" ]]; then
    # --end-date ist exklusiv; im laufenden Monat nur bis gestern.
    END=$(date -d "$MONTH +1 month" +%F)
    [[ "$END" > "$TODAY" ]] && END=$TODAY
    if [[ "$MONTH" < "$END" ]]; then
      echo "--- $(date '+%T') $KEY ($MONTH bis vor $END)"
      "$BIN/garmin" extract --db-path "$DB" --start-date "$MONTH" --end-date "$END"
      sleep 5
    fi
    touch "$MARKS/$KEY.done"
  fi
  MONTH=$(date -d "$MONTH -1 month" +%F)
done

echo "--- $(date '+%T') Zusatzdaten (Tagessummen, Uhr-FIT, HRV, VO2max)"
"$BIN/python" "$APP/extras.py" --from "$FROM_MONTH" --refresh-days 0

# Erst wenn beide Teile ohne Fehler durch sind, gilt die Historie als geholt.
echo "$FROM_MONTH" > "$MARKS/complete"
echo "=== $(date '+%F %T') backfill komplett"
