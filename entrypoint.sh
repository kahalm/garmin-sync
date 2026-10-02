#!/usr/bin/env bash
# Einstieg des Containers.
#   (ohne Argument)   Dauerbetrieb: Historie einmal holen, dann täglich abgleichen
#   auth [--manual]   einmalige Anmeldung bei Garmin (interaktiv)
#   sync | backfill <Datum> | extras [...] | info | sql [Abfrage]
set -euo pipefail
APP=/app

case "${1:-run}" in
  run) ;;
  auth) shift; exec garmin auth "$@" ;;
  sync) exec "$APP/sync.sh" ;;
  backfill) shift; exec "$APP/backfill.sh" "$@" ;;
  extras) shift; exec python "$APP/extras.py" "$@" ;;
  info) exec garmin info --db-path "$GARMIN_DATA/garmin_data.db" ;;
  sql) shift; exec sqlite3 -header -column \
         -cmd "ATTACH '$GARMIN_DATA/garmin_extras.db' AS extras;" \
         "$GARMIN_DATA/garmin_data.db" "$@" ;;
  *) exec "$@" ;;
esac

say() { echo "$(date '+%F %T') $*"; }

until ls -d "$HOME"/.garminconnect/[0-9]* >/dev/null 2>&1; do
  say "nicht eingeloggt — einmalig ausführen: docker compose run --rm garmin auth"
  sleep 60
done

WANT=$(date -d "$GARMIN_START" +%Y-%m-01)
while true; do
  HAVE=$(cat "$GARMIN_DATA/.backfill/complete" 2>/dev/null || true)
  if [[ -z "$HAVE" || "$WANT" < "$HAVE" ]]; then
    say "Historie ab $WANT wird geholt"
    if ! "$APP/backfill.sh" "$WANT" >/dev/null; then
      say "Historie noch nicht vollständig (Details: logs/backfill.log), neuer Versuch in 30 min"
      sleep 1800
      continue
    fi
    say "Historie vollständig"
  else
    "$APP/sync.sh" >/dev/null && say "Abgleich ok" \
      || say "Abgleich fehlgeschlagen (Details: logs/sync.log), nächster Versuch zur gewohnten Zeit"
  fi

  NOW=$(date +%s)
  NEXT=$(date -d "today $SYNC_TIME" +%s)
  (( NEXT <= NOW )) && NEXT=$(date -d "tomorrow $SYNC_TIME" +%s)
  say "nächster Abgleich $(date -d "@$NEXT" '+%F %H:%M')"
  sleep $(( NEXT - NOW ))
done
