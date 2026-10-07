FROM python:3.13-slim

# Garmin ändert die Anmeldung öfter: dann Version anheben und neu bauen.
ARG GARMIN_HEALTH_DATA_VERSION=2.16.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "garmin-health-data==${GARMIN_HEALTH_DATA_VERSION}" "mcp==2.2.0" \
 && python -m venv /opt/mfp && /opt/mfp/bin/pip install --no-cache-dir "myfitnesspal==2.1.2"
# python-myfitnesspal pinnt alte Abhängigkeiten — darum eine eigene Umgebung.

COPY env.sh sync.sh backfill.sh extras.py next_workout.py workout_button.py mfp_sync.py mcp_server.py entrypoint.sh /app/

# HOME liegt auf dem Volume, damit die Garmin-Tokens (~/.garminconnect) überleben.
ENV GARMIN_DATA=/data \
    GARMIN_BIN=/usr/local/bin \
    HOME=/data \
    GARMIN_START=2017-01-01 \
    SYNC_TIME=05:40 \
    PYTHONUNBUFFERED=1

VOLUME /data
ENTRYPOINT ["/app/entrypoint.sh"]
