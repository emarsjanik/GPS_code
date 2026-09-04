#!/bin/bash
#
# station_health_mail.sh
#
# Runs station_health.py and emails the result.
#
# WHY IT SENDS EVERY DAY, INCLUDING WHEN HEALTHY
#
# An earlier version stayed silent unless something was wrong, on
# the reasoning that a daily all-clear trains you to ignore it.
# That reasoning was incomplete: silence is ambiguous. A quiet
# morning could mean the station is fine, or that cron did not run,
# or that mail delivery broke -- and the whole point of this is to
# notice when something has quietly stopped working.
#
# So it reports daily either way. A healthy day says so in one
# cheerful line you can recognize at a glance without opening the
# message; a problem day puts the count in the subject.
#
# RECIPIENTS
#
# Space-separated, not semicolon-separated. msmtp takes recipients
# as separate arguments -- passing "a@x.gov; b@x.gov" as one string
# is a single malformed address, which is why the daily image count
# emails only ever reached the first person. Confirmed in
# ~/.msmtp.log: recipients=emarsjanik@usgs.gov;
#
# Usage:
#   ./station_health_mail.sh            normal daily run
#   ./station_health_mail.sh --dry-run  print instead of sending
#   ./station_health_mail.sh --quiet    send only if there is a problem

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_SCRIPT="$PROJECT_DIR/station_health.py"

RECIPIENTS="emarsjanik@usgs.gov csherwood@usgs.gov"

STATION_NAME="$(python3 -c "
import json
try:
    d = json.load(open('$PROJECT_DIR/station/resources/station.json'))
    print(d.get('station_name') or d.get('station_id') or 'GNSS station')
except Exception:
    print('GNSS station')
" 2>/dev/null)"

DRY_RUN=false
QUIET=false
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --quiet)   QUIET=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ ! -x "$HEALTH_SCRIPT" ]; then
    echo "station_health.py not found or not executable at $HEALTH_SCRIPT" >&2
    exit 1
fi

REPORT=$("$HEALTH_SCRIPT" 2>&1)
HEALTH_EXIT=$?

if [ "$HEALTH_EXIT" -eq 0 ] && $QUIET; then
    exit 0
fi

if [ "$HEALTH_EXIT" -eq 0 ]; then
    SUBJECT="$STATION_NAME GPS: all healthy"
    OPENING="Do a happy dance, the GPS is happy!

Every check passed. Recording, processing, plotting, uploading and
archiving are all working as expected."
else
    n_fail=$(echo "$REPORT" | grep -c "^  \[FAIL\]" || true)
    SUBJECT="$STATION_NAME GPS: $n_fail problem(s) need attention"
    OPENING="The GPS station reported $n_fail problem(s) this morning.
Details below."
fi

BODY="$OPENING

$REPORT

--
Sent by station_health_mail.sh on $(hostname) at $(date -u '+%Y-%m-%d %H:%M')Z.
Run ./station_health.py --verbose on the station for the full report,
including the checks that passed."

if $DRY_RUN; then
    echo "--- would send to: $RECIPIENTS ---"
    echo "Subject: $SUBJECT"
    echo ""
    echo "$BODY"
    exit "$HEALTH_EXIT"
fi

if ! command -v msmtp >/dev/null 2>&1; then
    echo "msmtp not found -- cannot send. Health check output follows:" >&2
    echo "$REPORT" >&2
    exit 1
fi

# Retry on failure. A single attempt turns a momentary network
# problem into a silent morning, which is indistinguishable from a
# healthy one -- see this script's notes above.
MAIL_ATTEMPTS=3
MAIL_BACKOFF=30

mail_exit=1
for attempt in $(seq 1 "$MAIL_ATTEMPTS"); do
    printf 'Subject: %s\n\n%s\n' "$SUBJECT" "$BODY" | msmtp $RECIPIENTS
    mail_exit=$?

    if [ "$mail_exit" -eq 0 ]; then
        [ "$attempt" -gt 1 ] && echo "Sent on attempt $attempt." >&2
        break
    fi

    if [ "$attempt" -lt "$MAIL_ATTEMPTS" ]; then
        echo "msmtp attempt $attempt failed (exit $mail_exit); retrying in ${MAIL_BACKOFF}s..." >&2
        sleep "$MAIL_BACKOFF"
        MAIL_BACKOFF=$((MAIL_BACKOFF * 2))
    fi
done

if [ "$mail_exit" -ne 0 ]; then
    echo "msmtp failed after $MAIL_ATTEMPTS attempts (exit $mail_exit)." >&2
    echo "Health check output follows so it is not lost:" >&2
    echo "$REPORT" >&2
    exit 1
fi

exit "$HEALTH_EXIT"
