#!/bin/bash
#
# station_health_mail.sh
#
# Runs station_health.py and emails the result -- but only when
# something actually needs attention.
#
# WHY THIS WRAPPER EXISTS
#
# cron mails a job's output to the crontab owner, but that relies on
# a local mail spool this machine does not have (/var/mail/<user>
# does not exist). So scheduling station_health.py directly would
# produce output nobody ever sees -- which is precisely the failure
# this whole line of work was meant to fix.
#
# msmtp is already configured here and demonstrably delivers, since
# send_daily_upload_count.sh uses it. This follows that same
# pattern.
#
# QUIET WHEN HEALTHY, BY DESIGN
#
# A daily "everything is fine" email trains you to ignore it, and an
# ignored alert is worth nothing. This sends only on a non-zero exit
# from the health check -- i.e. a real problem -- unless --always is
# given.
#
# Usage:
#   ./station_health_mail.sh            email only if there is a problem
#   ./station_health_mail.sh --always   email regardless (useful for testing)
#   ./station_health_mail.sh --dry-run  print what would be sent
#
# Suggested crontab, after the overnight jobs have finished:
#   0 7 * * * /home/argus_user/GNSS/v4.1/station_health_mail.sh

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_SCRIPT="$PROJECT_DIR/station_health.py"

# Space-separated. Note that send_daily_upload_count.sh passes its
# recipients as one semicolon-separated string, which is not the
# usual msmtp convention and may only reach the first address --
# worth checking separately. This uses separate arguments, which is
# unambiguous.
RECIPIENTS="emarsjanik@usgs.gov csherwood@usgs.gov"

STATION_NAME="$(python3 -c "
import json
try:
    d = json.load(open('$PROJECT_DIR/station/resources/station.json'))
    print(d.get('station_name') or d.get('station_id') or 'GNSS station')
except Exception:
    print('GNSS station')
" 2>/dev/null)"

ALWAYS=false
DRY_RUN=false
while [ $# -gt 0 ]; do
    case "$1" in
        --always)  ALWAYS=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ ! -x "$HEALTH_SCRIPT" ]; then
    echo "station_health.py not found or not executable at $HEALTH_SCRIPT" >&2
    exit 1
fi

REPORT=$("$HEALTH_SCRIPT" 2>&1)
HEALTH_EXIT=$?

if [ "$HEALTH_EXIT" -eq 0 ] && ! $ALWAYS; then
    # Healthy and not asked to report anyway -- stay quiet.
    exit 0
fi

if [ "$HEALTH_EXIT" -eq 0 ]; then
    SUBJECT="$STATION_NAME: healthy"
else
    # Lead with the count of real problems so the subject line alone
    # says whether this needs attention now.
    n_fail=$(echo "$REPORT" | grep -c "^  \[FAIL\]" || true)
    SUBJECT="$STATION_NAME: $n_fail problem(s) need attention"
fi

BODY="$REPORT

--
Sent by station_health_mail.sh on $(hostname) at $(date -u '+%Y-%m-%d %H:%M')Z.
This message is sent only when a check fails, so receiving it means
something genuinely needs looking at.

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

printf 'Subject: %s\n\n%s\n' "$SUBJECT" "$BODY" | msmtp $RECIPIENTS
mail_exit=$?

if [ "$mail_exit" -ne 0 ]; then
    echo "msmtp failed (exit $mail_exit). Health check output follows:" >&2
    echo "$REPORT" >&2
fi

exit "$HEALTH_EXIT"
