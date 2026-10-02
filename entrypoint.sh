#!/usr/bin/env bash
# Einstieg des Containers.
#   (ohne Argument)   Dauerbetrieb: Historie einmal holen, dann täglich abgleichen;
#                     mit REFRESH_SECONDS zusätzlich nachziehen, sobald die Uhr hochlädt
#   auth [--manual]   einmalige Anmeldung bei Garmin (interaktiv)
#   mcp               MCP-Server für Claude (nur lesend, siehe mcp_server.py)
#   sync [since [Datum]] | backfill <Datum> | extras [...] | info | sql [Abfrage]
set -euo pipefail
APP=/app

case "${1:-run}" in
  run) ;;
  auth) shift; exec garmin auth "$@" ;;
  mcp) exec python "$APP/mcp_server.py" ;;
  sync) shift; exec "$APP/sync.sh" "$@" ;;
  backfill) shift; exec "$APP/backfill.sh" "$@" ;;
  extras) shift; exec python "$APP/extras.py" "$@" ;;
  info) exec garmin info --db-path "$GARMIN_DATA/garmin_data.db" ;;
  sql) shift; exec sqlite3 -header -column \
         -cmd "ATTACH '$GARMIN_DATA/garmin_extras.db' AS extras;" \
         "$GARMIN_DATA/garmin_data.db" "$@" ;;
  *) exec "$@" ;;
esac

say() { echo "$(date '+%F %T') $*"; }

# Eine einzelne Anfrage: wann hat die Uhr zuletzt hochgeladen? Nur wenn sich das geändert
# hat, wird wirklich abgerufen — ab dem Tag des vorigen Uploads, damit auch ein
# nachgereichter Vortag ankommt.
refresh_if_uploaded() {
  local seen=$GARMIN_DATA/.last_upload new old
  new=$(flock -n "$GARMIN_DATA/.lock" python "$APP/extras.py" --last-upload 2>/dev/null) || return 0
  old=$(cat "$seen" 2>/dev/null || true)
  [[ -n "$new" && "$new" != "$old" ]] || return 0
  if "$APP/sync.sh" since "${old:0:10}" >/dev/null; then
    echo "$new" > "$seen"
    say "Uhr hat um ${new:11:5} hochgeladen — nachgezogen"
  else
    say "Nachziehen fehlgeschlagen (Details: logs/sync.log)"
  fi
}

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

  NEXT=$(date -d "today $SYNC_TIME" +%s)
  (( NEXT <= $(date +%s) )) && NEXT=$(date -d "tomorrow $SYNC_TIME" +%s)
  say "nächster Abgleich $(date -d "@$NEXT" '+%F %H:%M')"

  # Bis dahin alle REFRESH_SECONDS nachsehen, ob die Uhr neu hochgeladen hat — der volle
  # Abgleich reicht nur bis gestern. Aus, solange REFRESH_SECONDS nicht gesetzt ist.
  REFRESH=${REFRESH_SECONDS:-0}
  while (( $(date +%s) < NEXT )); do
    LEFT=$(( NEXT - $(date +%s) ))
    if (( REFRESH > 0 && LEFT > REFRESH )); then
      sleep "$REFRESH"
      refresh_if_uploaded
    else
      sleep "$LEFT"
    fi
  done
done
