#!/bin/bash
#
# nuc_cleanup_round2.sh
#
# DRY RUN by default: lists exactly what would be deleted, with
# sizes -- deletes NOTHING unless run with --execute.
#
# Round 2: covers what round 1 (nuc_cleanup.sh) missed due to a
# truncated file listing during planning, plus three additional
# items confirmed for removal afterward:
#   - 18 more old investigation-era CSV/PNG files at the project
#     root (same category as round 1, just not seen in time)
#   - station/resources/station.json.backup (stale, station.json
#     itself is confirmed correct and committed)
#   - station/v4.1_receiver_v1.0.tar.gz (archived old code version)
#   - venv/ (295MB, unused, separate from the real gnssrefl_venv/)
#   - __pycache__/ directories (harmless bytecode cache, regenerates
#     automatically, safe to clear)
#
# Usage:
#   ./nuc_cleanup_round2.sh            (dry run -- lists only)
#   ./nuc_cleanup_round2.sh --execute  (actually deletes)

set -uo pipefail

EXECUTE=false
[ "${1:-}" = "--execute" ] && EXECUTE=true

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 1

_BAR="================================================================"
total_bytes=0

section() {
    echo ""
    echo "$_BAR"
    echo "  $1"
    echo "$_BAR"
}

handle_file() {
    local f="$1"
    [ -f "$f" ] || return 0
    local size
    size=$(stat -c%s "$f" 2>/dev/null || echo 0)
    total_bytes=$((total_bytes + size))
    if $EXECUTE; then
        rm -f "$f"
        echo "  removed: $f"
    else
        echo "  would remove: $f ($(numfmt --to=iec-i --suffix=B "$size" 2>/dev/null || echo "${size} bytes"))"
    fi
}

handle_dir() {
    local d="$1"
    [ -d "$d" ] || return 0
    local size
    size=$(du -sb "$d" 2>/dev/null | cut -f1)
    total_bytes=$((total_bytes + size))
    if $EXECUTE; then
        rm -rf "$d"
        echo "  removed: $d/"
    else
        echo "  would remove: $d/ ($(du -sh "$d" 2>/dev/null | cut -f1))"
    fi
}

# ----------------------------------------------------------------
# 1. Remaining old investigation CSV/PNG output at project root
# ----------------------------------------------------------------
section "1. Project root -- remaining old investigation output"

OLD_ROOT_FILES_2=(
    ocean17_23_extended_repeated_track_tide_results.csv
    ocean17_23_repeated_track_tide_results.csv
    ocean_geometry_frequency_lag_results.csv
    ocean_gnssir_30min_binned.csv
    ocean_gnssir_absolute_vs_tide.png
    ocean_gnssir_anomaly_vs_tide.png
    ocean_gnssir_arc_product.csv
    ocean_gnssir_excluded_arcs.csv
    ocean_test_vs_tide.png
    prn29_arc_tide_test.csv
    prn29_snr_spectral_comparison.csv
    prn29_snr_spectral_comparison.png
    prn29_snr_waveforms.csv
    prn29_snr_waveforms.png
    repeat_track_tide_slopes.csv
    sats_15_26_30_vs_tide.png
    water_level_vs_tide.png
    water_level_vs_tide_data.csv
)
for f in "${OLD_ROOT_FILES_2[@]}"; do
    handle_file "$f"
done

# ----------------------------------------------------------------
# 2. Stale backup / archive files
# ----------------------------------------------------------------
section "2. Stale backup / archive files"

handle_file "station/resources/station.json.backup"
handle_file "station/v4.1_receiver_v1.0.tar.gz"

# ----------------------------------------------------------------
# 3. Unused secondary venv
# ----------------------------------------------------------------
section "3. Unused secondary venv/ (distinct from gnssrefl_venv/)"

handle_dir "venv"

# ----------------------------------------------------------------
# 4. Python bytecode caches (harmless, auto-regenerating)
# ----------------------------------------------------------------
section "4. __pycache__/ directories"

handle_dir "__pycache__"
handle_dir "station/__pycache__"
handle_dir "tests/__pycache__"

# ----------------------------------------------------------------
# Summary
# ----------------------------------------------------------------
section "Summary"

total_human=$(numfmt --to=iec-i --suffix=B "$total_bytes" 2>/dev/null || echo "${total_bytes} bytes")

if $EXECUTE; then
    echo "  Deleted. Total space freed: $total_human"
else
    echo "  DRY RUN ONLY -- nothing was deleted."
    echo "  Total space that WOULD be freed: $total_human"
    echo ""
    echo "  Review the list above carefully. If it looks correct, run:"
    echo "    ./nuc_cleanup_round2.sh --execute"
fi

