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


# ---------------------------------------------------------------
# Explicit PATH.
#
# cron runs with PATH=/usr/bin:/bin, which does not include
# /usr/local/bin where convbin is installed. Without this, RINEX
# conversion silently reports NOT_READY and the station manager
# stops processing -- confirmed to have happened here for five days
# before anyone noticed, because nothing visibly broke.
# ---------------------------------------------------------------
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin${PATH:+:$PATH}"
set -euo pipefail

# Derived from this script's own location (station/), so this works
# from any install path and any username -- these were previously
# hardcoded to one specific machine's home directory, which silently
# broke continuous operation for anyone who installed elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# The venv's python: prefer whatever version is actually present
# rather than assuming python3.10 specifically.
VENV_PYTHON="$(ls "$PROJECT_DIR"/gnssrefl_venv/bin/python3* 2>/dev/null | head -1)"
[ -z "$VENV_PYTHON" ] && VENV_PYTHON="$PROJECT_DIR/gnssrefl_venv/bin/python3"

SCRIPT="$PROJECT_DIR/station/station_manager.py"
WORKDIR="$PROJECT_DIR/station"
PIDFILE="$PROJECT_DIR/logs/station_manager.pid"
LOGFILE="$PROJECT_DIR/logs/station_manager.out"

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
