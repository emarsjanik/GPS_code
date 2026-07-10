#!/bin/bash
#
# stop_station.sh
#
# USGS GNSS Reference Station
#
# Gracefully stops station_manager.py, started via start_station.sh.
# Sends SIGTERM (station_manager.py's own signal handler, confirmed
# tonight against real hardware, turns this into a graceful stop
# request rather than an immediate kill).
#
# IMPORTANT: graceful shutdown does not happen immediately -- it
# waits for whatever recording chunk is currently in progress to
# finish naturally (confirmed against real hardware: record_raw()
# cannot be safely interrupted mid-call). This script waits up to
# MAX_WAIT_SECONDS for that to happen before giving up and reporting
# it as still running -- it does NOT escalate to SIGKILL on its own,
# since that would defeat the whole point of a graceful stop. If you
# need an immediate hard stop regardless of data loss, that is a
# deliberate separate action (`kill -9`), not this script's job.

set -euo pipefail

PIDFILE="/home/argus_user/GNSS/v4.1/logs/station_manager.pid"
MAX_WAIT_SECONDS=3700  # must exceed record_raw_chunk_seconds; see station.json

if [ ! -f "$PIDFILE" ]; then
    echo "No PID file at $PIDFILE -- station_manager.py does not appear to be running."
    exit 0
fi

PID="$(cat "$PIDFILE")"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "PID $PID (from $PIDFILE) is not running. Removing stale PID file."
    rm -f "$PIDFILE"
    exit 0
fi

echo "Sending SIGTERM to PID $PID (graceful stop -- waits for the current chunk to finish)..."
kill -TERM "$PID"

WAITED=0
while kill -0 "$PID" 2>/dev/null; do
    if [ "$WAITED" -ge "$MAX_WAIT_SECONDS" ]; then
        echo "Still running after ${MAX_WAIT_SECONDS}s. Not escalating automatically."
        echo "Check station_manager.pid's log, or 'kill -9 $PID' if you are certain"
        echo "an immediate stop (with possible data loss for the current chunk) is needed."
        exit 1
    fi
    sleep 5
    WAITED=$((WAITED + 5))
done

rm -f "$PIDFILE"
echo "Stopped cleanly after ${WAITED}s."
