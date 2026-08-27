#!/bin/bash
#
# compress_rinex.sh
#
# Packs each day's RINEX files (.obs + .nav, plus .sbs if present)
# into a single per-day tar.gz inside rinex/, then removes the
# originals -- but ONLY after verifying the archive is readable and
# contains exactly what it should.
#
# Measured on this project's own real data: ~3.6x compression
# (a 395 MB day becomes ~110 MB), which matters both for local disk
# and for uploading over a slow connection.
#
# DRY RUN by default: shows exactly what would happen and deletes
# nothing. Pass --execute to actually do it.
#
# Safe to re-run: days already compressed are skipped.
#
# WHY THIS IS SAFE FOR THIS PROJECT:
# Nothing in this codebase reads existing files back out of rinex/.
# rinex_processor.py only WRITES new .obs/.nav there; pipeline.py
# just passes the directory through. recover_missing_days.sh
# reprocesses old days from EXTERNAL storage using RINEX 3 long
# filenames, not from rinex/ at all -- confirmed by direct review
# before this script was written.
#
# To unpack a day again by hand:
#     tar -xzf rinex/station_20260725.tar.gz -C rinex/
#
# Usage:
#   ./compress_rinex.sh              (dry run)
#   ./compress_rinex.sh --execute    (actually compress)
#   ./compress_rinex.sh --execute --keep-days 14
#       leaves the most recent 14 days uncompressed

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RINEX_DIR="$PROJECT_DIR/rinex"

EXECUTE=false
KEEP_DAYS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --execute) EXECUTE=true; shift ;;
        --keep-days) KEEP_DAYS="${2:-0}"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

_BAR="================================================================"
total_before=0
total_after=0
compressed_count=0
skipped_count=0
failed_count=0

echo "$_BAR"
echo "  RINEX compression"
echo "$_BAR"
echo ""

if [ ! -d "$RINEX_DIR" ]; then
    echo "  No rinex/ directory at $RINEX_DIR -- nothing to do."
    exit 0
fi

if [ "$KEEP_DAYS" -gt 0 ]; then
    cutoff_epoch=$(date -d "$KEEP_DAYS days ago" +%s)
    echo "  Leaving the most recent $KEEP_DAYS day(s) uncompressed."
    echo ""
else
    cutoff_epoch=$(date -d "2100-01-01" +%s)   # effectively "compress everything"
fi

# Collect the distinct basenames (one per day) that have at least a
# .obs file. Handles both station_YYYYMMDD and any other prefix
# (e.g. the one-off overnight_* recordings) without special-casing.
mapfile -t basenames < <(
    find "$RINEX_DIR" -maxdepth 1 -type f -name "*.obs" -printf "%f\n" 2>/dev/null \
        | sed 's/\.obs$//' \
        | sort
)

if [ "${#basenames[@]}" -eq 0 ]; then
    echo "  No uncompressed .obs files found -- nothing to do."
    exit 0
fi

for base in "${basenames[@]}"; do
    obs="$RINEX_DIR/$base.obs"
    nav="$RINEX_DIR/$base.nav"
    sbs="$RINEX_DIR/$base.sbs"
    archive="$RINEX_DIR/$base.tar.gz"

    if [ -f "$archive" ]; then
        echo "  [skip] $base -- archive already exists"
        skipped_count=$((skipped_count + 1))
        continue
    fi

    # Respect --keep-days using the date embedded in the filename
    # where there is one; files without a parseable date are always
    # eligible (they are one-off recordings, not part of the daily
    # series).
    datestr=$(echo "$base" | grep -oE '[0-9]{8}' | head -1)
    if [ -n "$datestr" ] && [ "$KEEP_DAYS" -gt 0 ]; then
        file_epoch=$(date -d "${datestr:0:4}-${datestr:4:2}-${datestr:6:2}" +%s 2>/dev/null || echo 0)
        if [ "$file_epoch" -ge "$cutoff_epoch" ]; then
            echo "  [skip] $base -- within the most recent $KEEP_DAYS day(s)"
            skipped_count=$((skipped_count + 1))
            continue
        fi
    fi

    # Build the member list from whatever actually exists.
    members=()
    [ -f "$obs" ] && members+=("$base.obs")
    [ -f "$nav" ] && members+=("$base.nav")
    [ -f "$sbs" ] && members+=("$base.sbs")

    size_before=0
    for m in "${members[@]}"; do
        s=$(stat -c%s "$RINEX_DIR/$m" 2>/dev/null || echo 0)
        size_before=$((size_before + s))
    done

    if ! $EXECUTE; then
        echo "  [would compress] $base (${#members[@]} file(s), $(numfmt --to=iec-i --suffix=B "$size_before" 2>/dev/null || echo "$size_before bytes"))"
        total_before=$((total_before + size_before))
        compressed_count=$((compressed_count + 1))
        continue
    fi

    printf "  [compressing] %s ... " "$base"

    if ! tar -czf "$archive.partial" -C "$RINEX_DIR" "${members[@]}" 2>/dev/null; then
        echo "FAILED (tar error) -- originals left untouched"
        rm -f "$archive.partial"
        failed_count=$((failed_count + 1))
        continue
    fi

    # Verify the archive is readable and contains every expected
    # member BEFORE deleting anything. A corrupt archive plus deleted
    # originals would be unrecoverable.
    listing=$(tar -tzf "$archive.partial" 2>/dev/null)
    if [ -z "$listing" ]; then
        echo "FAILED (archive unreadable) -- originals left untouched"
        rm -f "$archive.partial"
        failed_count=$((failed_count + 1))
        continue
    fi

    all_present=true
    for m in "${members[@]}"; do
        if ! echo "$listing" | grep -qx "$m"; then
            all_present=false
            break
        fi
    done

    if ! $all_present; then
        echo "FAILED (archive incomplete) -- originals left untouched"
        rm -f "$archive.partial"
        failed_count=$((failed_count + 1))
        continue
    fi

    mv "$archive.partial" "$archive"

    for m in "${members[@]}"; do
        rm -f "$RINEX_DIR/$m"
    done

    size_after=$(stat -c%s "$archive" 2>/dev/null || echo 0)
    total_before=$((total_before + size_before))
    total_after=$((total_after + size_after))
    compressed_count=$((compressed_count + 1))

    ratio="?"
    if [ "$size_before" -gt 0 ]; then
        ratio=$(python3 -c "print(f'{$size_after/$size_before*100:.0f}')" 2>/dev/null || echo "?")
    fi
    echo "OK ($(numfmt --to=iec-i --suffix=B "$size_after" 2>/dev/null || echo "$size_after") -- ${ratio}% of original)"
done

echo ""
echo "$_BAR"
echo "  Summary"
echo "$_BAR"

if $EXECUTE; then
    echo "  Compressed: $compressed_count day(s)"
    [ "$skipped_count" -gt 0 ] && echo "  Skipped:    $skipped_count"
    [ "$failed_count" -gt 0 ] && echo "  FAILED:     $failed_count (originals left in place)"
    if [ "$total_before" -gt 0 ]; then
        echo ""
        echo "  Before: $(numfmt --to=iec-i --suffix=B "$total_before" 2>/dev/null || echo "$total_before")"
        echo "  After:  $(numfmt --to=iec-i --suffix=B "$total_after" 2>/dev/null || echo "$total_after")"
        saved=$((total_before - total_after))
        echo "  Saved:  $(numfmt --to=iec-i --suffix=B "$saved" 2>/dev/null || echo "$saved")"
    fi
else
    echo "  DRY RUN -- nothing was changed."
    echo "  Would compress: $compressed_count day(s)"
    [ "$skipped_count" -gt 0 ] && echo "  Would skip:     $skipped_count"
    if [ "$total_before" -gt 0 ]; then
        echo "  Current size of those files: $(numfmt --to=iec-i --suffix=B "$total_before" 2>/dev/null || echo "$total_before")"
        echo "  Expected after compression: roughly 28% of that, based on"
        echo "  a real measurement against this project's own data."
    fi
    echo ""
    echo "  To actually compress:  ./compress_rinex.sh --execute"
fi
