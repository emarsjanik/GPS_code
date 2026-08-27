#!/bin/bash
#
# archive_rinex.sh
#
# Single cron entry point for RINEX archiving:
#
#   1. compress any completed days still sitting uncompressed
#   2. sync the compressed archive up to S3
#   3. prune local archives that are verified safely in S3
#
# Run as one chained process rather than three cron entries, because
# the steps are strictly ordered and their durations are
# unpredictable on this connection. Separate cron entries could
# start a prune while an upload was still in flight; the prune's
# checksum verification would catch that and refuse, but it would
# produce confusing spurious failures. Chaining removes the
# possibility entirely.
#
# A lock file prevents overlapping runs -- a real concern here, since
# a full sync over a slow link can easily outlast the interval
# between cron invocations.
#
# Every step is logged with timestamps to logs/archive_rinex.log.
# Cron mails only on non-zero exit, so a healthy run stays quiet
# while a real failure gets noticed.
#
# Usage:
#   ./archive_rinex.sh                  (compress + sync only; no pruning)
#   ./archive_rinex.sh --prune          (also prune verified-uploaded days)
#   ./archive_rinex.sh --prune --keep-days 60
#
# Suggested crontab (daily, well clear of the station's own midnight
# rollover and daily processing):
#   30 4 * * * /home/argus_user/GNSS/v4.1/archive_rinex.sh --prune

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="${SYNC_SCRIPT:-$HOME/stationTools/s3SyncRinex.sh}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/archive_rinex.log"
LOCK_FILE="$LOG_DIR/archive_rinex.lock"

DO_PRUNE=false
KEEP_DAYS=30
COMPRESS_KEEP_DAYS=1   # never compress today's still-being-written data

while [ $# -gt 0 ]; do
    case "$1" in
        --prune)     DO_PRUNE=true; shift ;;
        --keep-days) KEEP_DAYS="${2:-30}"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ----------------------------------------------------------------
# Single-instance lock.
#
# flock on a dedicated file, held for the life of this process.
# -n means fail immediately rather than queue up behind a run that
# is already going -- queued cron jobs would just pile up.
# ----------------------------------------------------------------
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "SKIPPED: another archive_rinex.sh run is still in progress."
    exit 0
fi

log "=== archive_rinex.sh starting (prune=$DO_PRUNE, keep-days=$KEEP_DAYS) ==="

overall_status=0

# ----------------------------------------------------------------
# Step 1: compress completed days
# ----------------------------------------------------------------
log "--- Step 1: compress completed days ---"

if [ -x "$PROJECT_DIR/compress_rinex.sh" ]; then
    if "$PROJECT_DIR/compress_rinex.sh" --execute --keep-days "$COMPRESS_KEEP_DAYS" >>"$LOG_FILE" 2>&1; then
        log "Compression step completed."
    else
        log "WARNING: compression step returned non-zero -- see log above."
        overall_status=1
    fi
else
    log "WARNING: compress_rinex.sh not found or not executable at $PROJECT_DIR -- skipping."
    overall_status=1
fi

# ----------------------------------------------------------------
# Step 2: sync to S3
#
# This is the step that can run long. Nothing after it starts until
# it has actually finished.
# ----------------------------------------------------------------
log "--- Step 2: sync to S3 ---"

if [ ! -x "$SYNC_SCRIPT" ]; then
    log "ERROR: sync script not found or not executable at $SYNC_SCRIPT"
    log "Nothing was pruned (pruning requires a completed, verified sync)."
    log "=== archive_rinex.sh finished with errors ==="
    exit 1
fi

sync_start=$(date +%s)
if "$SYNC_SCRIPT" >>"$LOG_FILE" 2>&1; then
    sync_elapsed=$(( $(date +%s) - sync_start ))
    log "Sync completed in ${sync_elapsed}s."
else
    sync_elapsed=$(( $(date +%s) - sync_start ))
    log "ERROR: sync failed after ${sync_elapsed}s."
    log "Skipping the prune step -- pruning local data after a failed"
    log "upload is exactly how an archive gets lost."
    log "=== archive_rinex.sh finished with errors ==="
    exit 1
fi

# ----------------------------------------------------------------
# Step 3: prune local archives verified to be in S3
#
# Only reached if the sync above genuinely succeeded. The prune
# script independently re-verifies each file by checksum before
# deleting it, and refuses to run at all if the sync script uses
# --delete.
# ----------------------------------------------------------------
if $DO_PRUNE; then
    log "--- Step 3: prune local archives already in S3 ---"

    if [ -x "$PROJECT_DIR/prune_local_rinex.sh" ]; then
        if SYNC_SCRIPT="$SYNC_SCRIPT" "$PROJECT_DIR/prune_local_rinex.sh" \
                --execute --keep-days "$KEEP_DAYS" >>"$LOG_FILE" 2>&1; then
            log "Prune step completed."
        else
            log "ERROR: prune step returned non-zero -- see log above."
            log "(This is a refusal to delete, not data loss: the prune"
            log "script leaves files in place whenever it cannot verify"
            log "them.)"
            overall_status=1
        fi
    else
        log "WARNING: prune_local_rinex.sh not found or not executable -- skipping."
        overall_status=1
    fi
else
    log "--- Step 3: pruning not requested (pass --prune to enable) ---"
fi

# ----------------------------------------------------------------
# Summary
# ----------------------------------------------------------------
if [ -d "$PROJECT_DIR/rinex" ]; then
    local_size=$(du -sh "$PROJECT_DIR/rinex" 2>/dev/null | cut -f1)
    local_count=$(find "$PROJECT_DIR/rinex" -maxdepth 1 -type f | wc -l)
    log "Local rinex/: $local_count file(s), $local_size"
fi

if [ "$overall_status" -eq 0 ]; then
    log "=== archive_rinex.sh finished successfully ==="
else
    log "=== archive_rinex.sh finished with warnings ==="
fi

exit "$overall_status"
