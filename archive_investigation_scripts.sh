#!/bin/bash
#
# archive_investigation_scripts.sh
#
# Moves the Marconi-specific, one-off investigation/debugging scripts
# accumulated over this project's development into a clearly
# separated investigations/ directory, so the general-purpose,
# reusable tooling (station/, setup_station.sh, process_gps_data.sh,
# run_and_view.sh, generate_fresnel_mask.py, process_all_and_plot.py,
# rinex_inventory.py, validate_station.py) stays clean and immediately
# navigable at the repo root.
#
# This is a `git mv`-based move, not a delete -- every file's full
# history is preserved, and nothing is destroyed. Review the plan
# below before running; every step prints what it's doing.
#
# Run from the repo root:
#   cd ~/GNSS/v4.1
#   bash archive_investigation_scripts.sh

set -euo pipefail

echo "================================================================"
echo "  Archiving investigation scripts into investigations/"
echo "================================================================"

mkdir -p investigations/longterm_stability_iterations
mkdir -p investigations/topobathy_validation_iterations
mkdir -p investigations/ocean_mask_and_fresnel_kml
mkdir -p investigations/tide_correlation_analysis
mkdir -p investigations/early_diagnostic_tests
mkdir -p investigations/azimuth_elevation_refraction_tests
mkdir -p investigations/superseded_duplicates

git_mv_if_exists() {
    local src="$1"
    local dst="$2"
    if [ -e "$src" ]; then
        git mv "$src" "$dst"
        echo "  moved: $src"
    else
        echo "  skip (not found): $src"
    fi
}

# ------------------------------------------------------------------
# Group 1: the long-term stability pipeline iterations (V2 through
# V8, plus every intermediate BEFORE_* checkpoint and their output
# directories). One investigation, many iterations -- kept together.
# ------------------------------------------------------------------
echo ""
echo "--- Group 1: longterm_stability_iterations ---"
for f in \
    marconi_longterm_stability_and_tide_plots.py \
    marconi_longterm_stability_v2_BEFORE_GNSSREFL_PARSER_FIX.py \
    marconi_longterm_stability_v2_BEFORE_LOADER_FIX.py \
    marconi_longterm_stability_v2_BEFORE_RESULT_FILTER.py \
    marconi_longterm_stability_v2_BEFORE_RINEX_REGEX_FIX.py \
    MARCONI_LONGTERM_STABILITY_V2_NEW_20260818.py \
    marconi_longterm_stability_v2.py \
    "marconi_longterm_stability_v2.py.BEFORE_RINEX_FIX" \
    marconi_longterm_stability_v2_RINEX_DISCOVERY_FIXED.py \
    marconi_longterm_stability_v3_CORRECT.py \
    marconi_longterm_stability_v3.py \
    marconi_longterm_stability_v3_rinex_pattern_fixed.py \
    MARCONI_LONGTERM_STABILITY_V4_20260818.py \
    MARCONI_LONGTERM_STABILITY_V5_DIAGNOSTIC.py \
    "MARCONI_LONGTERM_STABILITY_V5_DIAGNOSTIC.py.BEFORE_RUN" \
    MARCONI_LONGTERM_STABILITY_V6.py \
    MARCONI_LONGTERM_STABILITY_V7.py \
    MARCONI_LONGTERM_STABILITY_V8.py \
    marconi_longterm_track_diagnostics_v5_summary.txt \
    marconi_longterm_track_stability_summary.txt \
    marconi_longterm_track_stability_v2_summary.txt \
    marconi_longterm_track_stability_v4_summary.txt \
    marconi_longterm_v6 \
    marconi_longterm_v7 \
    marconi_longterm_v8 \
    ; do
    git_mv_if_exists "$f" investigations/longterm_stability_iterations/
done

# ------------------------------------------------------------------
# Group 2: the topobathy/Fresnel validation iterations.
# ------------------------------------------------------------------
echo ""
echo "--- Group 2: topobathy_validation_iterations ---"
for f in \
    marconi_selected_track_topobathy_validation_fixed.py \
    marconi_selected_track_topobathy_validation.py \
    marconi_selected_track_topobathy_validation_v2.py \
    marconi_selected_track_topobathy_validation_v3.py \
    marconi_selected_track_topobathy_validation_v4.py \
    marconi_selected_track_topobathy_validation_v5.py \
    marconi_selected_track_topobathy_validation_v6.py \
    marconi_selected_track_topobathy_validation_v7.py \
    marconi_topobathy_fresnel_validation.py \
    marconi_topobathy_fresnel_validation.kml \
    marconi_topobathy_fresnel_validation_summary.txt \
    marconi_candidate_fresnel_validation.py \
    marconi_candidate_fresnel_validation.kml \
    marconi_candidate_fresnel_validation_summary.txt \
    marconi_selected_topobathy.kml \
    marconi_selected_topobathy_summary.txt \
    ; do
    git_mv_if_exists "$f" investigations/topobathy_validation_iterations/
done

# ------------------------------------------------------------------
# Group 3: ocean-mask / Fresnel-zone KML generation history (the
# predecessors to generate_fresnel_mask.py, which is the kept,
# generalized version of this work).
# ------------------------------------------------------------------
echo ""
echo "--- Group 3: ocean_mask_and_fresnel_kml ---"
for f in \
    marconi_ocean_mask_corrected.py \
    marconi_ocean_mask_proposed_180deg.kml \
    marconi_ocean_mask_proposed_35_135.kml \
    marconi_ocean_mask_proposed.kml \
    marconi_ocean_mask_RH11m.kml \
    marconi_ocean_mask_RH11m.py \
    marconi_ocean_mask_test.kml \
    marconi_fresnel_geometry_kml.kml \
    marconi_fresnel_geometry_summary.txt \
    marconi_final_physical_track_extension_report.py \
    marconi_final_physical_track.kml \
    marconi_final_physical_track_screen.py \
    marconi_final_physical_track_summary.txt \
    marconi_top10_integrated_fresnel.kml \
    marconi_top10_integrated_fresnel.py \
    marconi_top10_integrated_track_ranking_summary.txt \
    marconi_reflection_zone_geometry_test.kml \
    fresnel_mask.kml \
    usgs_reflzones.kml \
    ; do
    git_mv_if_exists "$f" investigations/ocean_mask_and_fresnel_kml/
done

# ------------------------------------------------------------------
# Group 4: tide-correlation / water-level analysis scripts (the
# predecessors to process_all_and_plot.py, which is the kept,
# generalized version of this work).
# ------------------------------------------------------------------
echo ""
echo "--- Group 4: tide_correlation_analysis ---"
for f in \
    analyze_arc_error_vs_tide_change.py \
    arc_error_vs_tide_change_summary.txt \
    analyze_gnssir_tide_relationship.py \
    analyze_ocean17_23_tracks_extended.py \
    analyze_ocean17_23_tracks.py \
    analyze_production_success_vs_tide.py \
    build_ocean_tide_product.py \
    classify_marconi_fresnel_water.py \
    compare_observed_vs_tide_predicted_fresnel.py \
    compare_to_noaa.py \
    compare_to_tide_models.py \
    gnssir_tide_analysis_summary.txt \
    marconi_absolute_wl_calibration.py \
    marconi_absolute_wl_calibration_summary.txt \
    marconi_all_data_tide_check.py \
    marconi_all_track_ranking_summary.txt \
    marconi_correct_overlay_plots.py \
    marconi_datum_corrected_validation.py \
    marconi_datum_sensitivity_summary.txt \
    marconi_datum_test_clean.py \
    marconi_datum_test_clean_summary.txt \
    marconi_experimental_multitrack_product.py \
    marconi_experimental_multitrack_summary.txt \
    marconi_leave_one_track_and_rank.py \
    marconi_leave_one_track_out_summary.txt \
    marconi_overlay_plot_summary.txt \
    marconi_run_extended_17_23.py \
    marconi_run_quicklook_matrix.py \
    marconi_simple_two_line_plot.py \
    marconi_tide_datum_sensitivity.py \
    marconi_vertical_offset_analysis.py \
    marconi_vertical_offset_summary.txt \
    observed_vs_tide_predicted_geometry_summary.txt \
    ocean_gnssir_summary.txt \
    plot_ocean_test_vs_tide.py \
    plot_sats_15_26_30_vs_tide.py \
    run_ocean_candidate_gnssir.py \
    ; do
    git_mv_if_exists "$f" investigations/tide_correlation_analysis/
done

# ------------------------------------------------------------------
# Group 5: earlier, one-off diagnostic test scripts (root-level
# test_*.py investigation scripts -- NOT the real unit-test suite in
# tests/, which stays exactly where it is).
# ------------------------------------------------------------------
echo ""
echo "--- Group 5: early_diagnostic_tests ---"
for f in \
    test_azimuth_bins_exact_tide.py \
    test_azimuth_bins_zero_lag.py \
    test_elevation_windows.py \
    test_individual_arc_tide_response.py \
    test_marconi_ocean_mask.py \
    test_marconi_reflection_zones.py \
    test_ocean_geometry_frequency_lag.py \
    test_ocean_lag_correct.py \
    test_prn29_arc_tide.py \
    test_repeat_track_slopes.py \
    prn29_snr_waveform_comparison.py \
    diagnose_arc_timing_bias.py \
    filter_by_time.py \
    dampening_correction.py \
    damping_correction.py \
    run_damping_correction.py \
    run_gnssir_tide_analysis.sh \
    ; do
    git_mv_if_exists "$f" investigations/early_diagnostic_tests/
done

# ------------------------------------------------------------------
# Group 6: tonight's azimuth/elevation/refraction methodology --
# kept together as the direct provenance of validate_station.py's
# four checks, rather than mixed into the general tide-correlation
# pile above.
# ------------------------------------------------------------------
echo ""
echo "--- Group 6: azimuth_elevation_refraction_tests ---"
for f in \
    analyze_e5_9.py \
    analyze_e9_13.py \
    analyze_elevation_dependence.py \
    analyze_norefr_azimuth_test.py \
    ; do
    git_mv_if_exists "$f" investigations/azimuth_elevation_refraction_tests/
done

# ------------------------------------------------------------------
# Group 7: confirmed superseded/duplicate files within station/ and
# scripts/ -- archived, not deleted, in case any is ever needed for
# reference.
# ------------------------------------------------------------------
echo ""
echo "--- Group 7: superseded_duplicates ---"
git_mv_if_exists "station/receiver_v1.0.py" investigations/superseded_duplicates/
git_mv_if_exists "station/tools/capture_binary.py" investigations/superseded_duplicates/
git_mv_if_exists "station/tools/capture_ephemeris.py" investigations/superseded_duplicates/
git_mv_if_exists "station/tools/capture_ephemeris2.py" investigations/superseded_duplicates/
git_mv_if_exists "station/tools/capture_ephemeris3.py" investigations/superseded_duplicates/
git_mv_if_exists "station/tools/capture_ephemeris5.py" investigations/superseded_duplicates/
git_mv_if_exists "station/tools/send_cmd.py" investigations/superseded_duplicates/
git_mv_if_exists "station/um980_cmd.py" investigations/superseded_duplicates/
git_mv_if_exists "scripts/run_and_view.sh" investigations/superseded_duplicates/
git_mv_if_exists "station/resources/station.json.backup_180deg" investigations/superseded_duplicates/
git_mv_if_exists "station/resources/station.json.backup_before_azimuth_fix" investigations/superseded_duplicates/

echo ""
echo "================================================================"
echo "  Done. Review with: git status"
echo "================================================================"
echo ""
echo "Nothing was deleted -- every file moved with 'git mv', so full"
echo "history is preserved (git log --follow <new path> still works)."
echo ""
echo "What remains at the repo root should now be almost entirely the"
echo "general-purpose, reusable tooling: station/, setup_station.sh,"
echo "scripts/process_gps_data.sh, run_and_view.sh,"
echo "recover_missing_days.sh, plot_all_data.sh, check_layout.py,"
echo "generate_fresnel_mask.py, process_all_and_plot.py,"
echo "rinex_inventory.py, validate_station.py, tests/,"
echo "integration_tests/, docs/, README.md, plus real reference data"
echo "(2021022FA_Marconi_topobathy_1m.tif, Files/station_pos_2024.db)."
echo ""
echo "Review the new investigations/ tree, then commit:"
echo "  git add -A"
echo "  git commit -m 'Archive investigation scripts and superseded"
echo "duplicates into investigations/, keeping general-purpose tooling"
echo "at the repo root'"
echo "  git push"
