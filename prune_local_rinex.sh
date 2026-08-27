#!/bin/bash
#
# prune_local_rinex.sh
#
# Frees local disk by removing old compressed RINEX archives that
# are verified safely stored in S3 -- keeping a recent window on
# local disk.
#
# DRY RUN by default. Deletes nothing unless given --execute.
#
# HOW IT VERIFIES (this is the important part)
#
# Before deleting anything, each candidate archive is downloaded
# back out of S3 to a temporary file and compared byte-for-byte
# (md5) against the local copy. Only on an exact match is the local
# file removed.
#
# This is deliberately the slow, certain approach. The cheap
# alternative -- comparing S3's ETag against a local md5 -- does not
# work here: confirmed directly against this bucket, these uploads
# are multipart (ETag "70f9cfd805...-21"), and a multipart ETag is a
# composite hash, not the object's md5. Comparing sizes alone would
# also be too weak, given that a partially-completed upload over
# this connection is a real, already-observed failure mode.
#
# WHY S3 MUST NOT USE --delete
#
# Once a day is pruned locally, S3 holds the only copy. s3SyncRinex.sh
# therefore deliberately does NOT pass --delete: with it, the next
# sync would see the pruned day missing locally and erase it from S3
# too, destroying the archive one run at a time.
#
# Usage:
#   ./prune_local_rinex.sh                 (dry run, 30-day window)
#   ./prune_local_rinex.sh --keep-days 60  (dry run, 60-day window)
#   ./prune_local_rinex.sh --execute       (actually prune)

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RINEX_DIR="$PROJECT_DIR/rinex"
S3_PREFIX="s3://cmgp-coastcam/cameras/caco-05/GPS/rinex"
AWS="/usr/local/bin/aws"

KEEP_DAYS=30
EXECUTE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --execute)   EXECUTE=true; shift ;;
        --keep-days) KEEP_DAYS="${2:-30}"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

_BAR="================================================================"
pruned=0
kept=0
failed=0
freed_bytes=0

echo "$_BAR"
echo "  Prune local RINEX archives already stored in S3"
echo "$_BAR"
echo ""
echo "  Local:  $RINEX_DIR"
echo "  Remote: $S3_PREFIX"
echo "  Keeping the most recent $KEEP_DAYS day(s) on local disk."
if ! $EXECUTE; then
    echo ""
    echo "  DRY RUN -- nothing will be deleted."
fi
echo ""

if [ ! -d "$RINEX_DIR" ]; then
    echo "  No rinex/ directory -- nothing to do."
    exit 0
fi

cutoff_epoch=$(date -d "$KEEP_DAYS days ago" +%s)

# ----------------------------------------------------------------
# SAFETY GUARD -- do not remove.
#
# Pruning locally is only safe while the sync script leaves S3
# alone. If s3SyncRinex.sh is ever given --delete, the next sync
# sees each pruned day missing locally and erases it from S3 too --
# destroying the only remaining copy, quietly, one run at a time.
#
# This is a real risk rather than a theoretical one: --delete is a
# reasonable-looking thing for someone to add to a sync script, and
# under cron nobody is watching when it happens. So this is enforced
# here in code rather than left to a comment.
#
# The check deliberately ignores comments -- s3SyncRinex.sh
# discusses --delete at length in its own header explaining why it
# must not be used, and that must not trip the guard.
# ----------------------------------------------------------------
SYNC_SCRIPT="${SYNC_SCRIPT:-$HOME/stationTools/s3SyncRinex.sh}"

if [ -f "$SYNC_SCRIPT" ]; then
    if grep -v '^[[:space:]]*#' "$SYNC_SCRIPT" 2>/dev/null \
        | grep -E 'aws[[:space:]]+s3[[:space:]]+sync' \
        | grep -q -- '--delete'; then
        echo "  REFUSING TO RUN."
        echo ""
        echo "  $SYNC_SCRIPT passes --delete to 'aws s3 sync'."
        echo ""
        echo "  That combination destroys data: this script removes old"
        echo "  archives from local disk once they are safely in S3, and"
        echo "  --delete then removes anything from S3 that is no longer"
        echo "  present locally. Together they erase the archive one sync"
        echo "  at a time, with no copy left anywhere."
        echo ""
        echo "  Remove --delete from the sync command, then re-run this."
        exit 1
    fi
else
    echo "  [!!] Sync script not found at $SYNC_SCRIPT"
    echo "       Cannot confirm it is safe to prune (this script needs to"
    echo "       verify the sync does not use --delete). Set SYNC_SCRIPT"
    echo "       to the correct path, e.g.:"
    echo "         SYNC_SCRIPT=/path/to/s3SyncRinex.sh $0 $*"
    exit 1
fi

TMPDIR_VERIFY=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_VERIFY"; }
trap cleanup EXIT

shopt -s nullglob
for archive in "$RINEX_DIR"/*.tar.gz; do
    base=$(basename "$archive")

    # Only consider files old enough to fall outside the retention
    # window. Files with no parseable date (one-off recordings) are
    # deliberately never pruned automatically.
    datestr=$(echo "$base" | grep -oE '[0-9]{8}' | head -1)
    if [ -z "$datestr" ]; then
        echo "  [keep] $base -- no date in filename, not pruned automatically"
        kept=$((kept + 1))
        continue
    fi

    file_epoch=$(date -d "${datestr:0:4}-${datestr:4:2}-${datestr:6:2}" +%s 2>/dev/null || echo 0)
    if [ "$file_epoch" -ge "$cutoff_epoch" ]; then
        echo "  [keep] $base -- within the most recent $KEEP_DAYS day(s)"
        kept=$((kept + 1))
        continue
    fi

    local_size=$(stat -c%s "$archive" 2>/dev/null || echo 0)

    # Cheap pre-check: is it in S3 at all, at the right size? Skips
    # a pointless multi-hundred-MB download when it obviously isn't.
    remote_size=$("$AWS" s3api head-object \
        --bucket "$(echo "$S3_PREFIX" | sed 's|s3://||; s|/.*||')" \
        --key "$(echo "$S3_PREFIX" | sed 's|s3://[^/]*/||')/$base" \
        --query 'ContentLength' --output text 2>/dev/null)

    if [ -z "$remote_size" ] || [ "$remote_size" = "None" ]; then
        echo "  [KEEP] $base -- NOT found in S3. Not pruned."
        failed=$((failed + 1))
        continue
    fi

    if [ "$remote_size" != "$local_size" ]; then
        echo "  [KEEP] $base -- size mismatch (local $local_size, S3 $remote_size). Not pruned."
        failed=$((failed + 1))
        continue
    fi

    if ! $EXECUTE; then
        echo "  [would prune] $base ($(numfmt --to=iec-i --suffix=B "$local_size" 2>/dev/null || echo "$local_size")) -- present in S3 at matching size; would be checksum-verified before deletion"
        freed_bytes=$((freed_bytes + local_size))
        pruned=$((pruned + 1))
        continue
    fi

    # Full verification: pull it back and compare byte-for-byte.
    printf "  [verifying] %s ... " "$base"
    verify_path="$TMPDIR_VERIFY/$base"

    if ! "$AWS" s3 cp "$S3_PREFIX/$base" "$verify_path" --quiet 2>/dev/null; then
        echo "FAILED to download from S3 -- not pruned"
        rm -f "$verify_path"
        failed=$((failed + 1))
        continue
    fi

    local_md5=$(md5sum "$archive" | awk '{print $1}')
    remote_md5=$(md5sum "$verify_path" | awk '{print $1}')
    rm -f "$verify_path"

    if [ "$local_md5" != "$remote_md5" ]; then
        echo "CHECKSUM MISMATCH -- not pruned"
        echo "        local:  $local_md5"
        echo "        S3:     $remote_md5"
        failed=$((failed + 1))
        continue
    fi

    rm -f "$archive"
    freed_bytes=$((freed_bytes + local_size))
    pruned=$((pruned + 1))
    echo "verified ($local_md5) -- pruned locally"
done
shopt -u nullglob

echo ""
echo "$_BAR"
echo "  Summary"
echo "$_BAR"

human_freed=$(numfmt --to=iec-i --suffix=B "$freed_bytes" 2>/dev/null || echo "$freed_bytes bytes")

if $EXECUTE; then
    echo "  Pruned locally: $pruned"
    echo "  Kept:           $kept"
    [ "$failed" -gt 0 ] && echo "  NOT pruned (verification failed): $failed"
    echo "  Local space freed: $human_freed"
else
    echo "  Would prune: $pruned"
    echo "  Would keep:  $kept"
    [ "$failed" -gt 0 ] && echo "  Would NOT prune (not safely in S3): $failed"
    echo "  Local space that would be freed: $human_freed"
    echo ""
    echo "  Note: --execute additionally downloads each file back from"
    echo "  S3 and compares checksums before deleting it. That is slow"
    echo "  over this connection -- expect several minutes per file."
    echo ""
    echo "  To actually prune:  ./prune_local_rinex.sh --execute"
fi
