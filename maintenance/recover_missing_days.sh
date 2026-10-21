#!/bin/bash
#
# recover_missing_days.sh
#
# Recovers UTC days that the automatic pipeline missed (no GNSS-IR
# results), from the raw receiver files the station keeps: archive/
# first, then the Raw/ folder of external storage, then the RINEX that
# older exports carry. process_and_plot.sh Step 1 runs it every night,
# once for this UTC year and once for last year.
#
# The work is done by recover_missing_days.py next to this script, with
# the station's own converter and GNSS-IR processor
# (station/rinex_processor.py, station/gnssrefl_processor.py); its
# docstring says where each day comes from and what can happen to it.
# Paths and station codes come from station/resources/station.json
# (external_storage_path, gnssrefl_station_code, ...), not from this
# script.
#
# Before review B12 this script looked only for RINEX files under one
# hard-coded export folder, files that gnssrefl_processor.py has since
# stopped leaving behind, so it found nothing to recover; it also
# hard-coded the project folder and station codes.
#
# Usage:
#
#   ./recover_missing_days.sh                automatic: every day of this
#                                            UTC year with data but no results
#   ./recover_missing_days.sh 204 205 206    those days of year (tried even
#                                            if they already have results)
#   ./recover_missing_days.sh --dry-run      list the days and their sources
#
#   RECOVER_YEAR=2026 ./recover_missing_days.sh 364 365
#       days of another year (process_and_plot.sh passes last year in
#       January, so a late-December day can still be recovered)
#   RECOVER_SINCE=YYYY-MM-DD
#       automatic mode skips days before it (process_and_plot.sh passes
#       station.json's gnss_record_first_day)
#
# Safe to run repeatedly: days with results are skipped, a day whose
# orbit is not published yet is tried again next time, and a day with
# no usable retrievals gets a <doy>.no_data marker so it is not tried
# every night (delete the marker to try again).
#
# Recovered days enter the water-level record on the next
# process_and_plot.sh run (nightly, via daily_gnss.sh); run that by
# hand to see them sooner.

set -uo pipefail

# This script lives in maintenance/, so the project root is one level
# up from the script itself.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# gnssrefl needs the project's venv python (3.10+); the system python3
# is only a fallback, e.g. for --dry-run on a machine without it.
PYTHON="$PROJECT_DIR/gnssrefl_venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"

args=()
[ -n "${RECOVER_YEAR:-}" ] && args+=(--year "$RECOVER_YEAR")
[ -n "${RECOVER_SINCE:-}" ] && args+=(--since "$RECOVER_SINCE")

exec "$PYTHON" "$PROJECT_DIR/maintenance/recover_missing_days.py" ${args[@]+"${args[@]}"} "$@"
