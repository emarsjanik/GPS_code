#!/bin/bash
#
# process_and_plot.sh
#
# GNSS-IR Reference Station -- Master Processing and Plotting
#
# The single command to run whenever you want to process whatever
# raw data currently exists and see the resulting water-level (or
# soil moisture / snow depth) plot. Consolidates what used to be
# several separate scripts (process_gps_data.sh, run_and_view.sh,
# recover_missing_days.sh) into one entry point, with real progress
# indicators throughout so long-running steps never look stalled.
#
# What this does, in order:
#   1. Quick sanity check (venv active, required tools present)
#   2. Auto-recovers any previously-missed days whose data still
#      exists in external storage, if configured
#   3. Converts any new raw data to RINEX and runs GNSS-IR analysis
#      on it
#   4. Finds the most recent gap-free stretch of results and
#      generates a plot for it
#
# Usage:
#   ./process_and_plot.sh

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/gnssrefl_venv"
STATION_JSON="$PROJECT_DIR/station/resources/station.json"

_BAR="================================================================"

section() {
    echo ""
    echo "$_BAR"
    echo "  $1"
    echo "$_BAR"
}

# ----------------------------------------------------------------
# Progress helpers.
#
# Deliberately built ONLY on the pattern confirmed safe during
# development: backgrounding the real command and polling it from
# the FOREGROUND with `kill -0`. A separate background "ticker"
# process running independently alongside visible output was tried
# and found to hang under some conditions -- not used here.
# ----------------------------------------------------------------

# For steps that produce one real output file per unit of work
# (e.g. one results/<doy>.txt per day processed) -- the safe
# background-and-poll structure above, showing real, incremental
# progress against a known total.
run_with_progress_count() {
    local message="$1"
    local watch_dir="$2"
    local watch_pattern="$3"
    local total="$4"
    shift 4

    "$@" > /tmp/process_and_plot_step.$$ 2>&1 &
    local pid=$!

    local frames='|/-\'
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        i=$(( (i + 1) % 4 ))
        local current
        current=$(find "$watch_dir" -maxdepth 1 -name "$watch_pattern" 2>/dev/null | wc -l)
        printf "\r  [%s]   %s (%d/%d files)   " "${frames:$i:1}" "$message" "$current" "$total"
        sleep 0.3
    done

    wait "$pid"
    local exit_code=$?

    local final_count
    final_count=$(find "$watch_dir" -maxdepth 1 -name "$watch_pattern" 2>/dev/null | wc -l)

    if [ "$exit_code" -eq 0 ]; then
        printf "\r  [OK]   %s (%d/%d files)   \n" "$message" "$final_count" "$total"
    else
        printf "\r  [FAIL] %s (%d/%d files)   \n" "$message" "$final_count" "$total"
        echo "  ---- output ----"
        cat /tmp/process_and_plot_step.$$
        echo "  -----------------"
    fi

    rm -f /tmp/process_and_plot_step.$$
    return "$exit_code"
}

# ----------------------------------------------------------------
# Step 0: sanity checks
# ----------------------------------------------------------------

section "GNSS-IR Reference Station -- Process and Plot"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "  Virtual environment not found. Run ./install.sh first."
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! command -v convbin >/dev/null 2>&1; then
    echo "  convbin not found on PATH. Run ./install.sh first."
    exit 1
fi

if [ ! -f "$STATION_JSON" ]; then
    echo "  station.json not found. Run ./install.sh or ./setup_station.sh first."
    exit 1
fi

STATION_CODE=$(python3 -c "
import json
d = json.load(open('$STATION_JSON'))
code = d.get('gnssrefl_station_code') or d.get('station_id', '')[:4]
print(code.lower())
")

if [ -z "$STATION_CODE" ]; then
    echo "  Could not determine the gnssrefl station code from station.json."
    echo "  Set 'station_id' or 'gnssrefl_station_code' and try again."
    exit 1
fi

# Only for the progress count in Step 2. Results go to the year of the
# data, and Steps 3-4 use every year (analysis_tools/gnss_record.py).
YEAR=$(date -u +%Y)

export REFL_CODE="$PROJECT_DIR/products/refl_code"
export ORBITS="$REFL_CODE/orbits"
export EXE="$REFL_CODE/exe"

RESULTS_DIR="$REFL_CODE/$YEAR/results/$STATION_CODE"
GNSS_RECORD=(python3 "$PROJECT_DIR/analysis_tools/gnss_record.py"
             --refl-code "$REFL_CODE" --station "$STATION_CODE")
# Optional "gnss_record_first_day": "YYYY-MM-DD" in station.json -- results
# before it (installation tests, a different antenna setup) are left out of
# the water-level record. Every year's results folder counts otherwise.
RECORD_FIRST_DAY=$(python3 -c "
import json
print(json.load(open('$STATION_JSON')).get('gnss_record_first_day') or '')
" 2>/dev/null)
[ -n "$RECORD_FIRST_DAY" ] && GNSS_RECORD+=(--since "$RECORD_FIRST_DAY")

echo "  Station code: $STATION_CODE   Year: $YEAR"

# ----------------------------------------------------------------
# Step 1: auto-recover any previously-missed days
# ----------------------------------------------------------------

section "Step 1: Recovering previously missed days (if any)"

if [ -f "$PROJECT_DIR/maintenance/recover_missing_days.sh" ]; then
    # Last year too, so a late-December day is still recoverable in
    # January -- but only once the record has a last year: RINEX older
    # than the station's own results (tests, another setup) is never
    # pulled in by this.
    this_year=$(date -u +%Y)
    for recover_year in $(( this_year - 1 )) "$this_year"; do
        if [ "$recover_year" -ne "$this_year" ] \
           && [ ! -d "$REFL_CODE/$recover_year/results/$STATION_CODE" ]; then
            continue
        fi
        RECOVER_YEAR="$recover_year" bash "$PROJECT_DIR/maintenance/recover_missing_days.sh"
    done
else
    echo "  recover_missing_days.sh not found -- skipping this step."
    echo "  (This is only needed if you use external storage for"
    echo "  automatic daily exports; safe to skip otherwise.)"
fi

# ----------------------------------------------------------------
# Step 2: process any new raw data
# ----------------------------------------------------------------

section "Step 2: Processing new data"

raw_file_count=$(find "$PROJECT_DIR/raw" -maxdepth 1 -name "*.um980" 2>/dev/null | wc -l)

if [ "$raw_file_count" -eq 0 ]; then
    echo "  No new raw files found in raw/ -- nothing to process."
    echo "  (Existing results, if any, will still be plotted below.)"
else
    echo "  Found $raw_file_count new raw file(s) to process."
    echo "  Each file involves RINEX conversion and GNSS-IR analysis,"
    echo "  and can take anywhere from under a minute to several"
    echo "  minutes per file depending on file size and network"
    echo "  conditions (orbit data is downloaded automatically)."
    echo ""

    results_before=$(find "$RESULTS_DIR" -maxdepth 1 -name "*.txt" 2>/dev/null | wc -l)
    expected_after=$((results_before + raw_file_count))

    run_with_progress_count \
        "Running pipeline" \
        "$RESULTS_DIR" \
        "*.txt" \
        "$expected_after" \
        python3 -c "
import sys
sys.path.insert(0, 'station')
from pipeline import Pipeline

p = Pipeline()
p.initialize()
summary = p.run()
p.shutdown()

print()
print('Files found:     ', summary.files_found)
print('Files processed: ', summary.files_processed)
print('Files failed:    ', summary.files_failed)
print('Products created:', summary.products_created)
if summary.errors:
    print()
    print('Errors:')
    for e in summary.errors:
        print(' -', e)
"
    pipeline_exit=$?

    if [ "$pipeline_exit" -ne 0 ]; then
        echo ""
        echo "  The processing step reported a problem. Full output is"
        echo "  shown above. This is not necessarily fatal -- check"
        echo "  Step 3 below to see whether any results were still"
        echo "  produced."
    fi
fi

# ----------------------------------------------------------------
# Step 3: find the available results -- every year of them
# ----------------------------------------------------------------

section "Step 3: Checking available results"

"${GNSS_RECORD[@]}" summary
summary_exit=$?
if [ "$summary_exit" -ne 0 ] && [ "$summary_exit" -ne 3 ]; then
    echo "  Could not list the results -- see the error above."
    exit 1
elif [ "$summary_exit" -eq 3 ]; then
    echo "  No results found yet -- nothing to plot."
    echo ""
    echo "  If you expected results here, run ./test_installation.sh"
    echo "  to check for a configuration problem, or check the log"
    echo "  output above for errors."
    exit 0
fi

# ----------------------------------------------------------------
# Step 4: fit the water level and update the whole-record spline
# ----------------------------------------------------------------

section "Step 4: Fitting the water level"

echo "  gnssrefl's subdaily combines the days' results and fits a smooth"
echo "  curve through them. It fits from 1 January of the oldest year not"
echo "  yet frozen to the newest day, across the year boundary, and the"
echo "  frozen years are added to it, so the spline always holds the"
echo "  whole record (see analysis_tools/gnss_record.py). About a"
echo "  minute per year of data; subdaily's own output goes to"
echo "  $REFL_CODE/Files/$STATION_CODE/fit/subdaily.log."
echo ""

# Confirmed via direct testing: -knots 4 (gnssrefl's own suggested
# starting point) was too coarse to follow this site's real
# semidiurnal tidal cycle -- it visibly clipped real peaks and
# troughs, and its own reported RMS-vs-raw-retrievals residual was
# 0.542m. -knots 8 (roughly one flexibility point per 3 hours,
# comfortably resolving a ~12.4-hour cycle) dropped that same
# residual to 0.206m and raised correlation against an independent
# tide model from 0.75 to 0.989. Reconsider if your station's own
# tidal period or sampling rate differs substantially from this
# site's.
# Azimuth window for the water level. At Marconi the antenna is only ~75 m
# landward of the waterline, so arcs near the 353/173 deg edges of the
# reflection window run ALONG the shore and reflect off the beach. Measured
# against the Chatham-derived tide, Jul-Sep 2026 (gnssir_reflection_audit.py
# in caco05-waterline): all azimuths 0.248 m RMS calm / 0.492 m rough
# (+0.10 m bias); 35-125 deg only 0.221 / 0.276 m (+0.03 m), keeping 81% of
# arcs. subdaily applies this to the stored results, so no reprocessing is
# needed. To revert, set both to empty: SUBDAILY_AZIM1="" SUBDAILY_AZIM2="".
# A change here refits the frozen years too, once (gnss_record.py).
SUBDAILY_AZIM1=35
SUBDAILY_AZIM2=125
SUBDAILY_SETTINGS="-rhdot True -knots 8"
if [ -n "$SUBDAILY_AZIM1" ] && [ -n "$SUBDAILY_AZIM2" ]; then
    SUBDAILY_SETTINGS="$SUBDAILY_SETTINGS -azim1 $SUBDAILY_AZIM1 -azim2 $SUBDAILY_AZIM2"
fi

# Without the azimuth window as a fallback (a year with too few arcs in
# the window makes subdaily crash); a fallback fit is never frozen.
if "${GNSS_RECORD[@]}" update --settings "$SUBDAILY_SETTINGS" \
        --fallback-settings "-rhdot True -knots 8"; then
    echo ""
    echo "  Water level: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_spline_out.txt"
    echo "  gnssrefl's own plots (the main one ends in _last.png):"
    echo "    $REFL_CODE/Files/$STATION_CODE/fit"
else
    echo ""
    echo "  The fit reported a problem -- see the output above. The"
    echo "  water-level file keeps the previous night's record."
fi

# ----------------------------------------------------------------
# Step 5: rolling 7-day plot for public display
#
# Generated at every station, unlike the tide comparison below which
# only runs when a tide model is configured. This is a published
# product rather than a diagnostic, so it should not depend on an
# internal validation step being switched on.
#
# Writes into the products directory, so the existing upload picks
# it up with everything else.
# ----------------------------------------------------------------

section "Step 5: Rolling 7-day plot"

SEVEN_DAY_PLOT="$REFL_CODE/Files/$STATION_CODE/7_day_plot.png"
SPLINE_FILE="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_spline_out.txt"

if [ ! -f "$SPLINE_FILE" ]; then
    echo "  No spline output yet -- skipping."
elif [ ! -f "$PROJECT_DIR/analysis_tools/plot_7day.py" ]; then
    echo "  analysis_tools/plot_7day.py not found -- skipping."
else
    # --no-qc: the public plot shows every estimate. With the QC on, the
    # storm high tides of Sep 25-27 2026 (up to 3 m above the Chatham gauge,
    # the total water level in heavy surf) were hidden together with an
    # hour either side, which left only the low-tide troughs and made the
    # plot look broken. The QC result is kept as its own diagnostic plot
    # below instead.
    if python3 "$PROJECT_DIR/analysis_tools/plot_7day.py" \
        --spline-file "$SPLINE_FILE" \
        --output "$SEVEN_DAY_PLOT" --no-qc; then
        echo ""
        echo "  This plot is intended for public display and is"
        echo "  referenced to local mean sea level via"
        echo "  water_level_msl_offset in station.json."
    else
        echo ""
        echo "  7-day plot could not be generated -- see above. The"
        echo "  underlying results are unaffected; the next run will"
        echo "  try again."
    fi

    # Diagnostic (not for the public): GNSS-IR minus the Chatham gauge
    # against the wave setup and runup allowances, last 7 days.
    WAVE_SETUP_PLOT="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_wave_setup_check.png"
    if [ -f "$PROJECT_DIR/analysis_tools/water_level_qc.py" ]; then
        if python3 "$PROJECT_DIR/analysis_tools/water_level_qc.py" "$SPLINE_FILE" \
            --plot-setup "$WAVE_SETUP_PLOT"; then
            echo "  Wave-setup check: $WAVE_SETUP_PLOT"
        else
            echo "  Wave-setup check could not be made -- see above (the 7-day plot is unaffected)."
        fi
    fi
fi

# ----------------------------------------------------------------
# Step 6: tide model comparison (optional -- only if configured)
# ----------------------------------------------------------------

section "Step 6: Tide model comparison (optional)"

TIDE_FILE=$(python3 -c "
import json
d = json.load(open('$STATION_JSON'))
print(d.get('tide_model_file', ''))
" 2>/dev/null)

if [ -z "$TIDE_FILE" ]; then
    echo "  No tide model configured for this station -- skipping."
    echo "  (Set this up any time by re-running the tide model step in"
    echo "  ./install.sh, or by hand -- see STATION_JSON_REFERENCE.md.)"
elif [ ! -f "$TIDE_FILE" ]; then
    echo "  A tide model is configured (\"$TIDE_FILE\") but that file"
    echo "  no longer exists at that location -- skipping this step."
    echo "  Check the path, or re-configure it via ./install.sh."
else
    TIDE_VALUE_COL=$(python3 -c "
import json
d = json.load(open('$STATION_JSON'))
print(d.get('tide_model_value_column', ''))
" 2>/dev/null)
    TIDE_TIME_COL=$(python3 -c "
import json
d = json.load(open('$STATION_JSON'))
print(d.get('tide_model_time_column', 'time'))
" 2>/dev/null)

    SPLINE_FILE="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_spline_out.txt"

    if [ ! -f "$SPLINE_FILE" ]; then
        echo "  No spline output found to compare against (Step 4 may not"
        echo "  have completed successfully) -- skipping this step."
    else
        echo "  Comparing against: $TIDE_FILE (column: $TIDE_VALUE_COL)"
        echo ""

        TIDE_PLOT_OUTPUT="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_vs_tide.png"

        python3 "$PROJECT_DIR/analysis_tools/plot_gnssir_vs_tide.py" \
            --spline-file "$SPLINE_FILE" \
            --tide-file "$TIDE_FILE" \
            --tide-time-col "$TIDE_TIME_COL" \
            --tide-value-col "$TIDE_VALUE_COL" \
            --output "$TIDE_PLOT_OUTPUT"
        plot_exit=$?

        echo ""

        python3 "$PROJECT_DIR/analysis_tools/compare_to_tide_deviation.py" \
            --spline-file "$SPLINE_FILE" \
            --tide-file "$TIDE_FILE" \
            --tide-time-col "$TIDE_TIME_COL" \
            --tide-value-col "$TIDE_VALUE_COL"
        compare_exit=$?

        if [ "$plot_exit" -ne 0 ] || [ "$compare_exit" -ne 0 ]; then
            echo ""
            echo "  Tide comparison reported a problem -- see the output above."
        fi
    fi
fi

# ----------------------------------------------------------------
# Done
# ----------------------------------------------------------------

section "Done"

echo "Summary:"
echo "  Water level (whole record): $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_spline_out.txt"
echo "  gnssrefl plots: $REFL_CODE/Files/$STATION_CODE/fit"
if [ -f "$REFL_CODE/Files/$STATION_CODE/7_day_plot.png" ]; then
    echo "  7-day public plot: $REFL_CODE/Files/$STATION_CODE/7_day_plot.png"
fi
if [ -n "$TIDE_FILE" ] && [ -f "$TIDE_FILE" ]; then
    echo "  Tide comparison plot: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_vs_tide.png"
fi
echo ""
echo "To check whether an apparent signal is real (not an artifact),"
echo "see ./validate_station.py."
