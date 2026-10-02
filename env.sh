# Gemeinsame Pfade für sync.sh und backfill.sh. Im Container setzt das Image
# GARMIN_DATA/GARMIN_BIN; auf dem Host gilt der Ordner neben den Skripten.
APP=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
DATA=${GARMIN_DATA:-$APP/data}
BIN=${GARMIN_BIN:-$APP/.venv/bin}
DB=$DATA/garmin_data.db
LOGS=$DATA/logs
MARKS=$DATA/.backfill
export GARMIN_NO_VERSION_CHECK=1

mkdir -p "$DATA" "$LOGS" "$MARKS"
