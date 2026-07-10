#!/bin/bash
#
# start_station.sh
#
# USGS GNSS Reference Station
#
# Idempotent start wrapper for station_manager.py, meant to be called
# from cron (both @reboot and periodically as a watchdog -- see
# crontab setup below). Safe to call repeatedly: if the station is
# already running, this does nothing. If it isn't running (crashed,
# never started, whatever), this starts it.
#
# This is what makes a cron-based setup safe for a long-running
# daemon like station_manager.py: calling this script every few
# minutes gives you the same practical effect as systemd's
# Restart=on-failure, without ever risking two overlapping instances
# fighting over the same serial port.

set -euo pipefail

VENV_PYTHON="/home/argus_user/GNSS/v4.1/gnssrefl_venv/bin/python3.10"
SCRIPT="/home/argus_user/GNSS/v4.1/station/station_manager.py"
WORKDIR="/home/argus_user/GNSS/v4.1/station"
PIDFILE="/home/argus_user/GNSS/v4.1/logs/station_manager.pid"
LOGFILE="/home/argus_user/GNSS/v4.1/logs/station_manager.out"

mkdir -p "$(dirname "$PIDFILE")"

# Is it already running? Check the PID file AND that the PID it
# names is actually alive and is genuinely our process -- a stale
# PID file (from an unclean shutdown, or reused by an unrelated
# process since) must not cause a false "already running" positive.
if [ -f "$PIDFILE" ]; then
    EXISTING_PID="$(cat "$PIDFILE")"
    if kill -0 "$EXISTING_PID" 2>/dev/null \
        && ps -p "$EXISTING_PID" -o cmd= 2>/dev/null | grep -q "station_manager.py"; then
        # Already running -- nothing to do. This is the normal,
        # expected outcome for every watchdog invocation except the
        # very first (or one following an actual crash).
        exit 0
    fi
    # Stale PID file (process gone, or PID reused by something
    # else) -- clean it up before starting fresh.
    rm -f "$PIDFILE"
fi

cd "$WORKDIR"

nohup "$VENV_PYTHON" "$SCRIPT" >> "$LOGFILE" 2>&1 &
NEW_PID=$!

echo "$NEW_PID" > "$PIDFILE"

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') Started station_manager.py (PID $NEW_PID)" >> "$LOGFILE"
