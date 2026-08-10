#!/bin/bash
#
# run_and_view.sh
#
# One-command convenience wrapper for quick, easy viewing of results:
#   1. Auto-recover any missing days whose RINEX data still exists in
#      external storage but never got processed (e.g. because their
#      orbit product wasn't published yet when the automatic pipeline
#      first tried them)
#   2. Process any new GPS data and regenerate all standard plots
#   3. Find the most recent day-of-year stretch we have result files
#      for, then check gnssrefl's own real gap warnings and
#      iteratively narrow that range until it's genuinely gap-free
#      at the sub-daily level (not just "every day-file exists"),
#      and overlay the result against the real tide models
#   4. Change into the folder where every plot (including the new
#      overlay) is saved, for easy viewing/uploading
#
# Confirmed necessary via real, direct testing: gnssrefl's own
# subdaily spline fit is explicitly a beta feature that "does not
# work well with gaps" -- and a day-of-year file existing for every
# day in a range does NOT guarantee that day's actual observations
# are gap-free internally (e.g. day 208 had real results but still
# had a 14.6-hour internal gap, which alone was enough to reintroduce
# spline overshoot). This checks gnssrefl's own real "Gap on MJD"
# warnings directly, rather than trusting file presence alone.
#
# IMPORTANT: run this with "source", not "./" -- a script run the
# normal way (./run_and_view.sh) executes as a separate child
# process, and a "cd" inside a child process can never change the
# directory of the shell that launched it (this is a fundamental
# property of how processes work, not a bug). Sourcing instead runs
# it directly in your own current shell, so the final "cd" actually
# takes effect and stays in place once the script finishes.
#
# Usage:
#   source run_and_view.sh
#   (or the shorthand: . run_and_view.sh)

# Deliberately NOT using "set -e" or "exit": since this is meant to
# be sourced directly into an interactive shell, "exit" would close
# that shell entirely rather than just stopping the script -- "return"
# is used instead wherever an early stop is needed, which works
# correctly both when sourced and when run normally.

TIDE_MODELS_FILE="$HOME/GNSS/v4.1/marconi_tides_sherwood.xlsx"
PLOTS_DIR="$HOME/GNSS/v4.1/products/refl_code/Files/usgs"
RESULTS_DIR="$HOME/GNSS/v4.1/products/refl_code/2026/results/usgs"
RECOVER_SCRIPT="$HOME/GNSS/v4.1/recover_missing_days.sh"
STATION_ID="usgs"
YEAR="2026"
RANGE_RESULT_FILE="/tmp/run_and_view_gap_free_range.$$"

# ------------------------------------------------------------
# Finds the most recent contiguous (no missing day-files) run of
# day-of-year result files -- walks backward from the latest day as
# long as each step back is exactly one day earlier. Prefers "most
# recent" over "longest overall": an older, longer-but-stale stretch
# shouldn't win over the current picture just because it happens to
# span more days.
# ------------------------------------------------------------
find_most_recent_run() {
    local days=($1)
    local n=${#days[@]}
    if [ "$n" -eq 0 ]; then
        return 1
    fi

    local end_idx=$((n - 1))
    local end_day=${days[$end_idx]}
    local start_idx=$end_idx

    while [ "$start_idx" -gt 0 ] && [ $((days[start_idx] - days[start_idx-1])) -eq 1 ]; do
        start_idx=$((start_idx - 1))
    done

    echo "${days[$start_idx]} $end_day"
}

# ------------------------------------------------------------
# Takes a day-file-based range and iteratively narrows it based on
# gnssrefl's own real "Gap on MJD ... year/doy YYYY DOY" warnings,
# confirmed to appear even within days that have a real result file
# but insufficient internal coverage. Trims the end of the range back
# to just before the earliest such gap and re-checks, repeating until
# either no gaps remain or trimming can't narrow any further (in
# which case it gives up and uses the last range it tried, since a
# single remaining day can't be split further by this method).
#
# Writes the final "start end" to $RANGE_RESULT_FILE (simpler and
# more reliable than juggling stdout/stderr channels for this) --
# everything else this function prints is subdaily's own real
# terminal output, shown to the user as-is.
# ------------------------------------------------------------
find_gap_free_range() {
    local start="$1"
    local end="$2"
    local max_iterations=20
    local iteration=0

    while [ "$iteration" -lt "$max_iterations" ]; do
        iteration=$((iteration + 1))

        echo ""
        echo "--- Trying days $start-$end ---"

        local subdaily_output
        subdaily_output=$(subdaily "$STATION_ID" "$YEAR" -doy1 "$start" -doy2 "$end" -rhdot True -knots 4 2>&1)
        echo "$subdaily_output"

        local gap_days
        gap_days=$(echo "$subdaily_output" | grep "Gap on MJD" | awk '{print $NF}' | sort -n | uniq)

        local earliest_actionable_gap=""
        for d in $gap_days; do
            if [ "$d" -ge "$start" ] && [ "$d" -le "$end" ]; then
                earliest_actionable_gap="$d"
                break
            fi
        done

        if [ -z "$earliest_actionable_gap" ]; then
            echo "No internal gaps found -- this range is genuinely clean."
            echo "$start $end" > "$RANGE_RESULT_FILE"
            return 0
        fi

        if [ "$earliest_actionable_gap" -le "$start" ]; then
            echo "Gap found at the start of the range itself -- cannot narrow"
            echo "further. Using this range as-is; treat results with some caution."
            echo "$start $end" > "$RANGE_RESULT_FILE"
            return 1
        fi

        echo "Real gap found at day $earliest_actionable_gap -- narrowing range to exclude it."
        end=$((earliest_actionable_gap - 1))
    done

    echo "$start $end" > "$RANGE_RESULT_FILE"
    return 1
}

echo "================================================================"
echo "  Step 1: Auto-recovering any missing days"
echo "================================================================"

if [ -f "$RECOVER_SCRIPT" ]; then
    ( cd "$HOME/GNSS/v4.1" && bash "$RECOVER_SCRIPT" )
else
    echo "recover_missing_days.sh not found at $RECOVER_SCRIPT -- skipping this step."
fi

echo ""
echo "================================================================"
echo "  Step 2: Processing GPS data"
echo "================================================================"

"$HOME/process_gps_data.sh"

echo ""
echo "================================================================"
echo "  Step 3: Finding a genuinely gap-free stretch and generating"
echo "  a clean plot for it"
echo "================================================================"

day_files=$(find "$RESULTS_DIR" -maxdepth 1 -name "*.txt" -exec basename {} .txt \; 2>/dev/null | sort -n)

if [ -z "$day_files" ]; then
    echo "No local day-of-year results found -- nothing to scope a clean plot to."
else
    read -r candidate_start candidate_end <<< "$(find_most_recent_run "$day_files")"

    echo "Most recent day-file stretch found: days $candidate_start-$candidate_end"
    echo "Checking for real internal gaps and narrowing if needed..."

    rm -f "$RANGE_RESULT_FILE"

    (
        cd "$HOME/GNSS/v4.1" && \
        source gnssrefl_venv/bin/activate && \
        export REFL_CODE="$HOME/GNSS/v4.1/products/refl_code" && \
        export EXE="$REFL_CODE/exe" && \
        export ORBITS="$REFL_CODE/orbits" && \
        find_gap_free_range "$candidate_start" "$candidate_end"
    )

    if [ -f "$RANGE_RESULT_FILE" ]; then
        read -r final_start final_end < "$RANGE_RESULT_FILE"
        rm -f "$RANGE_RESULT_FILE"
        echo ""
        echo "Final gap-free range used: days $final_start-$final_end"
    else
        echo ""
        echo "!! Could not determine a gap-free range -- see output above."
    fi

    echo ""
    echo "================================================================"
    echo "  Step 4: Overlaying with real tide models"
    echo "================================================================"

    if [ ! -f "$TIDE_MODELS_FILE" ]; then
        echo "!! Tide models file not found at $TIDE_MODELS_FILE"
        echo "!! Skipping the overlay step -- upload it there and re-run if needed."
    else
        ( cd "$HOME/GNSS/v4.1" && source gnssrefl_venv/bin/activate && python3 compare_to_tide_models.py "$TIDE_MODELS_FILE" )
    fi
fi

echo ""
echo "================================================================"
echo "  Step 5: Moving to the plots directory"
echo "================================================================"

cd "$PLOTS_DIR" || {
    echo "!! Could not cd to $PLOTS_DIR -- it may not exist yet if"
    echo "!! nothing has ever processed successfully."
    return 1 2>/dev/null || exit 1
}

echo "Now in: $(pwd)"
echo ""
ls -la
