#!/bin/bash
#
# nuc_cleanup.sh
#
# DRY RUN by default: lists exactly what would be deleted, grouped
# by category, with sizes -- deletes NOTHING unless run with
# --execute.
#
# Categories:
#   1. archive/  -- raw .um980 files before 2026-07-25, plus known
#                    junk test files, regardless of date
#   2. rinex/    -- RINEX files before 2026-07-25, plus known junk
#                    test files, regardless of date
#   3. products/refl_code/2026/results/usgs/ -- result files (and
#                    the failQC/ mirror) before 2026-07-25, plus the
#                    13 experimental --extension subdirectories from
#                    tonight's RH-range/knots investigation
#   4. products/refl_code/Files/usgs/ -- the same 13 experimental
#                    subdirectories, plus stale pre-fix quickLook/
#                    tide-comparison plots
#   5. Project root -- old, untracked investigation CSV/PNG output,
#                    plus tonight's own now-finished diagnostic PNGs
#
# Usage:
#   ./nuc_cleanup.sh            (dry run -- lists only)
#   ./nuc_cleanup.sh --execute  (actually deletes)

set -uo pipefail

CUTOFF="2026-07-25"
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

# Adds a file's size to the running total (dry-run tally), and
# deletes it if EXECUTE is true. Never fails the whole script if one
# file is already gone.
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

EXPERIMENTAL_SUBDIRS=(
    ocean17_23_l1_e5_13 ocean17_23_norefr ocean17_23_e5_7 ocean17_23_e7_9
    ocean17_23_e9_11 ocean17_23_e11_13 ocean17_23_e5_9 ocean17_23_e9_13
    ocean90_150 ocean_test
    validate_elev_lower_half validate_elev_upper_half validate_norefraction
)

# ----------------------------------------------------------------
# 1. archive/ -- pre-cutoff raw files + known junk
# ----------------------------------------------------------------
section "1. archive/ (raw .um980 files before $CUTOFF, plus junk test files)"

for f in archive/test_binary1.um980 archive/overnight_20260708_135223.um980; do
    handle_file "$f"
done

for f in archive/station_*.um980; do
    [ -f "$f" ] || continue
    fname=$(basename "$f")
    datestr=$(echo "$fname" | grep -oE '[0-9]{8}')
    [ -z "$datestr" ] && continue
    filedate="${datestr:0:4}-${datestr:4:2}-${datestr:6:2}"
    if [[ "$filedate" < "$CUTOFF" ]]; then
        handle_file "$f"
    fi
done

# ----------------------------------------------------------------
# 2. rinex/ -- pre-cutoff RINEX files + known junk
# ----------------------------------------------------------------
section "2. rinex/ (RINEX files before $CUTOFF, plus junk test files)"

for f in rinex/testbin.nav rinex/testbin.obs rinex/testbin2.nav rinex/testbin2.obs \
         rinex/test_binary1.nav rinex/test_binary1.obs; do
    handle_file "$f"
done

for f in rinex/station_*.nav rinex/station_*.obs; do
    [ -f "$f" ] || continue
    fname=$(basename "$f")
    datestr=$(echo "$fname" | grep -oE '[0-9]{8}')
    [ -z "$datestr" ] && continue
    filedate="${datestr:0:4}-${datestr:4:2}-${datestr:6:2}"
    if [[ "$filedate" < "$CUTOFF" ]]; then
        handle_file "$f"
    fi
done

# ----------------------------------------------------------------
# 3. results/usgs/ -- pre-cutoff results + experimental subdirs
# ----------------------------------------------------------------
section "3. products/refl_code/2026/results/usgs/ (pre-cutoff results + experimental subdirs)"

RESULTS_DIR="products/refl_code/2026/results/usgs"

for f in "$RESULTS_DIR"/*.txt "$RESULTS_DIR"/*.no_data; do
    [ -e "$f" ] || continue
    fname=$(basename "$f")
    doy="${fname%%.*}"
    [[ "$doy" =~ ^[0-9]+$ ]] || continue
    # doy 206 = 2026-07-25 (confirmed: doy 190 = 2026-07-09)
    if [ "$doy" -lt 206 ]; then
        handle_file "$f"
    fi
done

for f in "$RESULTS_DIR"/failQC/*.txt; do
    [ -f "$f" ] || continue
    fname=$(basename "$f")
    doy="${fname%%.*}"
    [[ "$doy" =~ ^[0-9]+$ ]] || continue
    if [ "$doy" -lt 206 ]; then
        handle_file "$f"
    fi
done

for sub in "${EXPERIMENTAL_SUBDIRS[@]}"; do
    handle_dir "$RESULTS_DIR/$sub"
done

# ----------------------------------------------------------------
# 4. Files/usgs/ -- experimental subdirs + stale pre-fix plots
# ----------------------------------------------------------------
section "4. products/refl_code/Files/usgs/ (experimental subdirs + stale pre-fix plots)"

FILES_DIR="products/refl_code/Files/usgs"

for sub in "${EXPERIMENTAL_SUBDIRS[@]}"; do
    handle_dir "$FILES_DIR/$sub"
done

for f in "$FILES_DIR"/quickLook_lsp.png "$FILES_DIR"/quickLook_summary.png \
         "$FILES_DIR"/quickLook_summary_doy204.png "$FILES_DIR"/quickLook_summary_doy205.png \
         "$FILES_DIR"/quickLook_summary_doy206.png "$FILES_DIR"/quickLook_summary_doy207.png \
         "$FILES_DIR"/quickLook_lsp_doy204.png "$FILES_DIR"/quickLook_lsp_doy205.png \
         "$FILES_DIR"/quickLook_lsp_doy206.png "$FILES_DIR"/quickLook_lsp_doy207.png \
         "$FILES_DIR"/tide_model_comparison.png; do
    handle_file "$f"
done

# ----------------------------------------------------------------
# 5. Project root -- old investigation output + tonight's own
#    now-finished diagnostic PNGs
# ----------------------------------------------------------------
section "5. Project root (old investigation CSV/PNG output + tonight's diagnostic PNGs)"

# Old investigation-era output (regenerated locally by scripts that
# are no longer tracked on this branch). Explicit list, not a broad
# wildcard, so nothing unintended gets swept in.
OLD_ROOT_FILES=(
    amplitude_envelope.png arc_error_vs_tide_change.csv arc_error_vs_tide_change.png
    arc_timing_bias_diagnostic.png azimuth_bins_exact_tide_results.csv
    azimuth_bins_zero_lag_results.csv damping_correction_result.png
    filtered_vs_spline.png filtered_vs_spline_aug5_9.png filtered_vs_spline_aug5_9_knots8.png
    gnssir_tide_arc_analysis.csv gnssir_tide_lag_analysis.csv gnssir_tide_lag_search.png
    gnssir_tide_relationship.png gnssir_tide_smearing.png gnssir_vs_tide.png
    individual_arc_tide_response_results.csv
    marconi_absolute_wl_calibration.csv marconi_all_track_ranking.csv
    marconi_candidate_fresnel_validation.csv marconi_datum_sensitivity.csv
    marconi_datum_test_clean.csv marconi_experimental_multitrack_daily.csv
    marconi_experimental_multitrack_observations.csv marconi_experimental_multitrack_track_summary.csv
    marconi_final_physical_daily_product.csv marconi_final_physical_track_screen.csv
    marconi_final_physical_track_summary.csv marconi_fresnel_geometry_all_arcs.csv
    marconi_leave_one_track_out.csv marconi_longterm_track_diagnostics_v5.csv
    marconi_longterm_track_stability.csv marconi_longterm_track_stability_v2.csv
    marconi_longterm_track_stability_v4.csv marconi_selected_topobathy_track_summary.csv
    marconi_selected_topobathy_validation.csv marconi_simple_gnssr_vs_eot20_two_lines.png
    marconi_top10_integrated_track_ranking.csv marconi_topobathy_fresnel_validation.csv
    marconi_vertical_offset_analysis.csv observed_vs_tide_predicted_geometry.csv
    raw_vs_spline.png raw_vs_spline_aug5_9.png
)
for f in "${OLD_ROOT_FILES[@]}"; do
    handle_file "$f"
done

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
    echo "    ./nuc_cleanup.sh --execute"
fi
