#!/bin/bash
#
# s3UploadTimeseries.sh
#
# Uploads month-filtered timeseries data to the archive server,
# organized into month folders:
#
#   GPS/timeseries/august2026/usgs_spline_out_august2026.txt
#   GPS/timeseries/august2026/usgs_subdaily_august2026.txt
#   GPS/timeseries/august2026/usgs_timeseries_august2026.csv
#
# WHY THE CURRENT MONTH IS RE-UPLOADED EVERY RUN
#
# gnssrefl regenerates one whole-record spline and one whole-record
# retrieval file on every processing run -- there is no per-day
# output to append. So a month's slice keeps growing until that
# month ends: august2026 on the 3rd holds 3 days, on the 20th it
# holds 20. Re-uploading the current month each night keeps it
# current. Once the month rolls over it stops changing by itself,
# so no end-of-month job is needed -- the last upload of the month
# is simply the final one.
#
# These files are small (a month of 30-minute spline data is well
# under 100 KB; the retrieval file a few hundred KB), so
# re-uploading is cheap even on a slow link -- unlike the RINEX
# archives, which are hundreds of MB each and must never be re-sent.
#
# By default only the current month is processed. --backfill walks
# every month present in the data, which is what you want once, to
# populate months that predate this script.
#
# Usage:
#   ./s3UploadTimeseries.sh             (current month only)
#   ./s3UploadTimeseries.sh --backfill  (every month in the record)

set -uo pipefail

# ---------------------------------------------------------------
# Deployment settings. See archive.conf.template.
#
# Kept out of this script (and out of git) so the bucket name,
# paths, and station code are per-deployment rather than baked into
# shared code.
# ---------------------------------------------------------------
CONF_FILE="${ARCHIVE_CONF:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/archive.conf}"
if [ ! -f "$CONF_FILE" ]; then
    echo "Configuration not found: $CONF_FILE"
    echo ""
    echo "Create it from the template:"
    echo "    cp archive.conf.template archive.conf"
    echo "    nano archive.conf"
    exit 1
fi
# shellcheck disable=SC1090
. "$CONF_FILE"

for _required in S3_BASE STATION_CODE; do
    if [ -z "${!_required:-}" ]; then
        echo "$_required is not set in $CONF_FILE"
        exit 1
    fi
done
AWS_CLI="${AWS_CLI:-/usr/local/bin/aws}"

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PRODUCTS_DIR="$PROJECT_DIR/products/refl_code/Files/$STATION_CODE"
FILTER_SCRIPT="$PROJECT_DIR/filter_month.py"
S3_TIMESERIES="$S3_BASE/timeseries"
AWS="$AWS_CLI"


# Tide comparison is optional -- read from station.json so this
# stays correct if the configuration changes.
STATION_JSON="$PROJECT_DIR/station/resources/station.json"
TIDE_FILE=""
TIDE_VALUE_COL=""
TIDE_TIME_COL="time"
if [ -f "$STATION_JSON" ]; then
    TIDE_FILE=$(python3 -c "
import json
print(json.load(open('$STATION_JSON')).get('tide_model_file') or '')
" 2>/dev/null)
    TIDE_VALUE_COL=$(python3 -c "
import json
print(json.load(open('$STATION_JSON')).get('tide_model_value_column') or '')
" 2>/dev/null)
    TIDE_TIME_COL=$(python3 -c "
import json
print(json.load(open('$STATION_JSON')).get('tide_model_time_column') or 'time')
" 2>/dev/null)
fi

BACKFILL=false
[ "${1:-}" = "--backfill" ] && BACKFILL=true

COUNT_FILE="/mnt/I2Rgus_Data/gnss_timeseries_upload_count.txt"
LOCK_FILE="/mnt/I2Rgus_Data/gnss_timeseries_upload_count.lock"

if [ ! -f "$FILTER_SCRIPT" ]; then
    echo "filter_month.py not found at $FILTER_SCRIPT"
    exit 1
fi

SPLINE="$PRODUCTS_DIR/${STATION_CODE}_spline_out.txt"
if [ ! -f "$SPLINE" ]; then
    echo "No spline output at $SPLINE -- has process_and_plot.sh run?"
    exit 1
fi

month_name() {
    case "$1" in
        01|1) echo "january" ;;   02|2) echo "february" ;;
        03|3) echo "march" ;;     04|4) echo "april" ;;
        05|5) echo "may" ;;       06|6) echo "june" ;;
        07|7) echo "july" ;;      08|8) echo "august" ;;
        09|9) echo "september" ;; 10) echo "october" ;;
        11) echo "november" ;;    12) echo "december" ;;
    esac
}

# Which year/month pairs to process.
if $BACKFILL; then
    # Every distinct year/month actually present in the spline file
    # (columns 3 and 4 are year and month -- confirmed format).
    mapfile -t periods < <(
        grep -v "^%" "$SPLINE" \
            | awk '{printf "%d %d\n", $3, $4}' \
            | sort -u -k1,1n -k2,2n
    )
    echo "Backfill: found ${#periods[@]} month(s) in the record."
else
    periods=("$(date -u +'%Y %-m')")
fi

uploaded_count=0
failed_count=0

for period in "${periods[@]}"; do
    year=$(echo "$period" | awk '{print $1}')
    month=$(echo "$period" | awk '{print $2}')
    mname=$(month_name "$(printf '%02d' "$month")")
    label="${mname}${year}"

    tmpdir=$(mktemp -d)

    filter_args=(
        --year "$year" --month "$month"
        --products-dir "$PRODUCTS_DIR"
        --output-dir "$tmpdir"
        --station-code "$STATION_CODE"
    )
    if [ -n "$TIDE_FILE" ] && [ -f "$TIDE_FILE" ] && [ -n "$TIDE_VALUE_COL" ]; then
        filter_args+=(--tide-file "$TIDE_FILE"
                      --tide-value-col "$TIDE_VALUE_COL"
                      --tide-time-col "$TIDE_TIME_COL")
    fi

    if ! python3 "$FILTER_SCRIPT" "${filter_args[@]}" >/dev/null 2>&1; then
        echo "  $label: no data for this month -- skipped"
        rm -rf "$tmpdir"
        continue
    fi

    for f in "$tmpdir"/*; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        if "$AWS" s3 cp "$f" "$S3_TIMESERIES/$label/$name" --quiet 2>/dev/null; then
            echo "  upload: $label/$name"
            uploaded_count=$((uploaded_count + 1))
        else
            echo "  FAILED: $label/$name"
            failed_count=$((failed_count + 1))
        fi
    done

    rm -rf "$tmpdir"
done

echo ""
echo "Uploaded: $uploaded_count   Failed: $failed_count"

if [ "$uploaded_count" -gt 0 ]; then
    (
        flock -x 200
        current_total=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
        echo $((current_total + uploaded_count)) > "$COUNT_FILE"
    ) 200>"$LOCK_FILE"
fi

[ "$failed_count" -gt 0 ] && exit 1
exit 0
