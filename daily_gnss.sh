#!/bin/bash
#
# daily_gnss.sh
#
# Single cron entry point for the daily GNSS-IR cycle:
#
#   1. process_and_plot.sh   -- convert any new raw data, run the
#                               GNSS-IR analysis, regenerate every
#                               plot, and (if a tide model is
#                               configured) the tide comparison
#   2. s3UploadGnssProducts.sh -- upload those plots and data files
#
# Chained in one process rather than two cron entries, so the upload
# cannot run on stale plots: it starts only after processing has
# actually finished, and does not run at all if processing failed.
# Uploading yesterday's plots while today's run is still going would
# be quietly wrong in a way nobody would notice.
#
# ORDERING RELATIVE TO archive_rinex.sh MATTERS.
#
# This must run BEFORE archive_rinex.sh, not after. Processing
# CREATES RINEX files (raw .um980 -> rinex/*.obs); archiving
# compresses them. Running archive first, then processing, produced
# a real, observed problem on this station: a day was compressed to
# .tar.gz, then reprocessed, leaving both a .tar.gz AND a loose
# .obs/.nav pair for the same day -- which compress_rinex.sh then
# skips forever (it sees the existing archive), so the stray files
# accumulate and get uploaded to S3 as duplicates.
#
# Measured runtime on this station: ~45s when there is no new raw
# data to convert. A run that converts a full day's ~360 MB raw file
# takes longer, but well under the one-hour gap suggested below.
#
# Suggested crontab (local machine time):
#   30 22 * * * /home/argus_user/GNSS/v4.1/daily_gnss.sh
#   30 23 * * * /home/argus_user/GNSS/v4.1/archive_rinex.sh --prune
#
# Usage:
#   ./daily_gnss.sh              (process, then upload)
#   ./daily_gnss.sh --no-upload  (process only)

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPLOAD_SCRIPT="${UPLOAD_SCRIPT:-$HOME/stationTools/s3UploadGnssProducts.sh}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_gnss.log"
LOCK_FILE="$LOG_DIR/daily_gnss.lock"

DO_UPLOAD=true
while [ $# -gt 0 ]; do
    case "$1" in
        --no-upload) DO_UPLOAD=false; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Single-instance lock: -n fails immediately rather than queueing,
# so a long run cannot cause invocations to pile up behind it.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "SKIPPED: another daily_gnss.sh run is still in progress."
    exit 0
fi

log "=== daily_gnss.sh starting (upload=$DO_UPLOAD) ==="

# ----------------------------------------------------------------
# Step 1: process and plot
# ----------------------------------------------------------------
log "--- Step 1: process_and_plot.sh ---"

if [ ! -x "$PROJECT_DIR/process_and_plot.sh" ]; then
    log "ERROR: process_and_plot.sh not found or not executable in $PROJECT_DIR"
    log "=== daily_gnss.sh finished with errors ==="
    exit 1
fi

proc_start=$(date +%s)
if "$PROJECT_DIR/process_and_plot.sh" >>"$LOG_FILE" 2>&1; then
    proc_elapsed=$(( $(date +%s) - proc_start ))
    log "Processing completed in ${proc_elapsed}s."
else
    proc_elapsed=$(( $(date +%s) - proc_start ))
    log "ERROR: process_and_plot.sh failed after ${proc_elapsed}s."
    log "Skipping the upload -- publishing plots from a failed run"
    log "would put stale or partial results on the server."
    log "=== daily_gnss.sh finished with errors ==="
    exit 1
fi

# ----------------------------------------------------------------
# Step 2: upload the products
# ----------------------------------------------------------------
if $DO_UPLOAD; then
    log "--- Step 2: upload products to S3 ---"

    if [ ! -x "$UPLOAD_SCRIPT" ]; then
        log "ERROR: upload script not found or not executable at $UPLOAD_SCRIPT"
        log "Processing succeeded; only the upload was skipped."
        log "=== daily_gnss.sh finished with errors ==="
        exit 1
    fi

    upload_start=$(date +%s)
    if "$UPLOAD_SCRIPT" >>"$LOG_FILE" 2>&1; then
        upload_elapsed=$(( $(date +%s) - upload_start ))
        log "Upload completed in ${upload_elapsed}s."
    else
        upload_elapsed=$(( $(date +%s) - upload_start ))
        log "ERROR: product upload failed after ${upload_elapsed}s."
        log "(Local results are fine -- only the upload failed. The next"
        log "run will re-upload everything, since this uses cp"
        log "--recursive rather than an incremental sync.)"
        log "=== daily_gnss.sh finished with errors ==="
        exit 1
    fi
else
    log "--- Step 2: upload not requested (--no-upload) ---"
fi

# ----------------------------------------------------------------
# Step 3: upload the month-foldered timeseries
#
# Unlike the two steps above, a failure here does NOT abort the run.
# Processing and the product upload are the primary job; this is a
# convenience copy of data that already exists locally and in the
# products folder. A transient S3 error is worth reporting but not
# worth calling the night a failure -- and since the current month
# is re-uploaded every run, a single miss corrects itself.
# ----------------------------------------------------------------
if $DO_UPLOAD; then
    log "--- Step 3: upload month-foldered timeseries ---"

    if [ -x "$PROJECT_DIR/s3UploadTimeseries.sh" ]; then
        ts_start=$(date +%s)
        if "$PROJECT_DIR/s3UploadTimeseries.sh" >>"$LOG_FILE" 2>&1; then
            ts_elapsed=$(( $(date +%s) - ts_start ))
            log "Timeseries upload completed in ${ts_elapsed}s."
        else
            ts_elapsed=$(( $(date +%s) - ts_start ))
            log "WARNING: timeseries upload failed after ${ts_elapsed}s."
            log "(Not fatal -- the next run re-uploads the current month.)"
        fi
    else
        log "WARNING: s3UploadTimeseries.sh not found or not executable -- skipping."
    fi
else
    log "--- Step 3: timeseries upload not requested (--no-upload) ---"
fi

# ----------------------------------------------------------------
# Summary
# ----------------------------------------------------------------
# Year and station code derived rather than hardcoded: the year
# was previously fixed at 2026, which would have silently
# reported "0 days" from January 1st onward.
_year=$(date -u +%Y)
_station=$(python3 -c "
import json
d = json.load(open('$PROJECT_DIR/station/resources/station.json'))
code = d.get('gnssrefl_station_code') or d.get('station_id', '')[:4]
print(code.lower())
" 2>/dev/null || echo "")
RESULTS_DIR="$PROJECT_DIR/products/refl_code/$_year/results/$_station"
if [ -d "$RESULTS_DIR" ]; then
    day_count=$(find "$RESULTS_DIR" -maxdepth 1 -name "*.txt" | wc -l)
    log "Results now available for $day_count day(s)."
fi

log "=== daily_gnss.sh finished successfully ==="
exit 0
