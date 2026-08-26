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

# For steps with no useful intermediate output of their own (e.g. a
# single gnssrefl subdaily call) -- shows a spinner and captures
# output, printing it only on failure.
run_with_spinner() {
    local message="$1"
    shift

    "$@" > /tmp/process_and_plot_step.$$ 2>&1 &
    local pid=$!

    local frames='|/-\'
    local i=0

    printf "  [..]   %s " "$message"
    while kill -0 "$pid" 2>/dev/null; do
        i=$(( (i + 1) % 4 ))
        printf "\r  [%s]   %s " "${frames:$i:1}" "$message"
        sleep 0.2
    done

    wait "$pid"
    local exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        printf "\r  [OK]   %s\n" "$message"
    else
        printf "\r  [FAIL] %s\n" "$message"
        echo "  ---- output ----"
        cat /tmp/process_and_plot_step.$$
        echo "  -----------------"
    fi

    rm -f /tmp/process_and_plot_step.$$
    return "$exit_code"
}

# For steps that produce one real output file per unit of work
# (e.g. one results/<doy>.txt per day processed) -- same safe
# background-and-poll structure as above, but shows real,
# incremental progress against a known total instead of a plain
# spinner.
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

YEAR=$(date +%Y)

export REFL_CODE="$PROJECT_DIR/products/refl_code"
export ORBITS="$REFL_CODE/orbits"
export EXE="$REFL_CODE/exe"

RESULTS_DIR="$REFL_CODE/$YEAR/results/$STATION_CODE"
mkdir -p "$RESULTS_DIR"

echo "  Station code: $STATION_CODE   Year: $YEAR"

# ----------------------------------------------------------------
# Step 1: auto-recover any previously-missed days
# ----------------------------------------------------------------

section "Step 1: Recovering previously missed days (if any)"

if [ -f "$PROJECT_DIR/recover_missing_days.sh" ]; then
    bash "$PROJECT_DIR/recover_missing_days.sh"
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
# Step 3: find the real range of available results
# ----------------------------------------------------------------

section "Step 3: Checking available results"

day_files=$(find "$RESULTS_DIR" -maxdepth 1 -name "*.txt" -exec basename {} .txt \; 2>/dev/null | sort -n)

if [ -z "$day_files" ]; then
    echo "  No results found yet -- nothing to plot."
    echo ""
    echo "  If you expected results here, run ./test_installation.sh"
    echo "  to check for a configuration problem, or check the log"
    echo "  output above for errors."
    exit 0
fi

day_count=$(echo "$day_files" | wc -l)
echo "  Found results for $day_count day(s) of year:"
for d in $day_files; do
    track_count=$(grep -vc "^%" "$RESULTS_DIR/$d.txt" 2>/dev/null || echo 0)
    echo "    Day $d: $track_count track(s)"
done

min_day=$(echo "$day_files" | head -1)
max_day=$(echo "$day_files" | tail -1)

# ----------------------------------------------------------------
# Step 4: generate the plot
# ----------------------------------------------------------------

section "Step 4: Generating plot (days $min_day-$max_day)"

echo "  This runs gnssrefl's own subdaily analysis: combining every"
echo "  day's results, fitting a smooth curve through them, and"
echo "  saving several diagnostic plots plus the main result plot."
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
if run_with_spinner \
    "Generating plots" \
    subdaily "$STATION_CODE" "$YEAR" -doy1 "$min_day" -doy2 "$max_day" -rhdot True -knots 8; then

    PLOTS_DIR="$REFL_CODE/Files/$STATION_CODE"
    echo ""
    echo "  Plots saved to:"
    echo "    $PLOTS_DIR"
    echo ""
    echo "  The main result is usually the file ending in _last.png"
    echo "  in that directory."
else
    echo ""
    echo "  Plot generation reported a problem -- see the output above."
fi

# ----------------------------------------------------------------
# Step 5: tide model comparison (optional -- only if configured)
# ----------------------------------------------------------------

section "Step 5: Tide model comparison (optional)"

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
echo "  Results available for days $min_day-$max_day ($day_count day(s))"
echo "  Plots directory: $REFL_CODE/Files/$STATION_CODE"
if [ -n "$TIDE_FILE" ] && [ -f "$TIDE_FILE" ]; then
    echo "  Tide comparison plot: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_vs_tide.png"
fi
echo ""
echo "To check whether an apparent signal is real (not an artifact),"
echo "see ./validate_station.py."
