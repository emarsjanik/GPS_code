#!/bin/bash
#
# process_gps_data.sh
#
# One-command verification and processing of the GNSS reference
# station's data. Run this any time you want to:
#   1. Confirm the software itself is still working correctly (test suite)
#   2. Confirm the project layout is intact
#   3. See what raw data actually exists
#   4. Convert any unprocessed raw files to RINEX and run GNSS-IR on them
#   5. Generate real, multi-day water-level plots from whatever
#      results currently exist
#
# Safe to run any time, including while station_manager.py is
# running autonomously in the background -- this only touches
# files already sitting in raw/ and products/, and never restarts
# or interferes with the live recording process.
#
# Usage: ./process_gps_data.sh

set -uo pipefail
# Deliberately NOT using -e: later steps should still attempt to run
# and report their own results even if an earlier, non-fatal step
# (e.g. an individual raw file failing conversion) had problems --
# each step here checks and reports its own success/failure
# explicitly, rather than the whole script silently stopping partway.

PROJECT_DIR="$HOME/GNSS/v4.1"
STATION_ID="usgs"
YEAR="2026"  # bump this once a year

section() {
    echo ""
    echo "================================================================"
    echo "  $1"
    echo "================================================================"
}

cd "$PROJECT_DIR" || { echo "Could not cd to $PROJECT_DIR -- aborting."; exit 1; }
source gnssrefl_venv/bin/activate

export REFL_CODE="$PROJECT_DIR/products/refl_code"
export ORBITS="$REFL_CODE/orbits"
export EXE="$REFL_CODE/exe"

overall_ok=true

# ------------------------------------------------------------
# Step 1: Run the test suite
# ------------------------------------------------------------
section "Step 1: Running the test suite"

test_output=$(python -m unittest discover -s tests -v 2>&1)
test_exit=$?
echo "$test_output" | tail -10

if [ "$test_exit" -ne 0 ]; then
    echo ""
    echo "!! TEST SUITE FAILED. Stopping here -- fix this before trusting"
    echo "!! anything else this script would do with real data."
    exit 1
fi

echo ""
echo "Test suite: OK"

# ------------------------------------------------------------
# Step 2: Verify project layout
# ------------------------------------------------------------
section "Step 2: Verifying project layout"

layout_output=$(python check_layout.py 2>&1)
layout_exit=$?
echo "$layout_output" | tail -10

if [ "$layout_exit" -ne 0 ]; then
    echo ""
    echo "!! Layout check reported a problem -- see output above."
    overall_ok=false
fi

# ------------------------------------------------------------
# Step 3: Show what raw data actually exists right now
# ------------------------------------------------------------
section "Step 3: Current raw data"

ls -la raw/ 2>&1

# ------------------------------------------------------------
# Step 4: Convert to RINEX and run GNSS-IR on anything unprocessed
# ------------------------------------------------------------
section "Step 4: Running the pipeline (RINEX conversion + GNSS-IR)"

cd station
python3 -c "
from pipeline import Pipeline

p = Pipeline()
p.initialize()
summary = p.run()
p.shutdown()
"
pipeline_exit=$?
cd "$PROJECT_DIR"

if [ "$pipeline_exit" -ne 0 ]; then
    echo ""
    echo "!! Pipeline run itself did not exit cleanly (exit code $pipeline_exit)."
    overall_ok=false
fi

# ------------------------------------------------------------
# Step 5: Find the real range of days with actual results
# ------------------------------------------------------------
section "Step 5: Checking available results"

results_dir="$REFL_CODE/$YEAR/results/$STATION_ID"

if [ ! -d "$results_dir" ]; then
    echo "No results directory found yet at $results_dir -- nothing to plot."
    day_files=""
else
    day_files=$(find "$results_dir" -maxdepth 1 -name "*.txt" -exec basename {} .txt \; | sort -n)
fi

if [ -z "$day_files" ]; then
    echo "No day-of-year results files found -- skipping plot generation."
else
    echo "Real results found for these days of year:"
    for d in $day_files; do
        track_count=$(grep -vc "^%" "$results_dir/$d.txt" 2>/dev/null || echo 0)
        echo "  Day $d: $track_count tracks"
    done

    min_day=$(echo "$day_files" | head -1)
    max_day=$(echo "$day_files" | tail -1)

    # ------------------------------------------------------------
    # Step 6: Generate plots across the real available range
    # ------------------------------------------------------------
    section "Step 6: Generating plots (days $min_day-$max_day)"

    # Confirmed necessary via real, direct A/B testing: the default
    # spline flexibility (8 knots/day) can badly overshoot between
    # real data points when there are only ~15-20 observations/day
    # (which our narrow elevation + azimuth mask combination
    # produces) -- verified this caused dramatic, non-physical
    # spikes with no support in the underlying corrected
    # observations. -knots 4 (roughly matching real points per knot
    # to our actual data density) eliminated the spikes entirely in
    # a direct side-by-side comparison, without changing the overall
    # RMS -- a reminder that RMS alone doesn't catch this kind of
    # artifact and the actual plot should always be checked too.
    subdaily "$STATION_ID" "$YEAR" -doy1 "$min_day" -doy2 "$max_day" -rhdot True -knots 4
    subdaily_exit=$?

    if [ "$subdaily_exit" -ne 0 ]; then
        echo ""
        echo "!! subdaily did not exit cleanly (exit code $subdaily_exit)."
        overall_ok=false
    else
        echo ""
        echo "Plots saved under:"
        echo "  $REFL_CODE/Files/$STATION_ID/"
    fi
fi

# ------------------------------------------------------------
# Final summary
# ------------------------------------------------------------
section "Summary"

if [ "$overall_ok" = true ]; then
    echo "Everything completed without a reported problem."
else
    echo "One or more steps reported a problem -- scroll up to find the"
    echo "specific section marked with '!!' above."
fi
