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
#   2. Auto-recovers any previously-missed days from the raw files
#      kept in archive/ or in external storage, if configured
#   3. Converts any new raw data to RINEX and runs GNSS-IR analysis
#      on it
#   4. Finds the most recent gap-free stretch of results and
#      generates a plot for it
#
# How each step went is written to logs/process_and_plot_status.json
# (analysis_tools/run_status.py), which the daily health check reads.
#
# EXIT STATUS. Non-zero only when the run cannot start (no venv, no
# convbin, no usable station.json) or cannot list the results (Step 3).
# A step that fails after that only prints a note, the next steps still
# run, and the exit status stays 0 -- deliberately: daily_gnss.sh uploads
# only after an exit 0, and the upload sends every product at once, so
# a failed tide comparison would otherwise also hold back a 7-day plot
# that was redrawn fine. The failure is in the status file instead, and
# station_health.py reports it (a WARN, a FAIL once no run has gone
# through for more than a day and a half).
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
# Run status: logs/process_and_plot_status.json says how each step of
# this run went (see analysis_tools/run_status.py). step_begin names
# the step that starts; step_end or step_skip records it. A step still
# open when the script exits (killed, or an exit nobody expected) is
# recorded as failed by the EXIT trap. Writing the status never stops
# the run and never changes its exit status.
# ----------------------------------------------------------------
STATUS_FILE="$PROJECT_DIR/logs/process_and_plot_status.json"
RUN_STATUS=(python3 "$PROJECT_DIR/analysis_tools/run_status.py" "$STATUS_FILE")
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
RUN_ID="$$@$(utc_now)"
STEP_NUM="" STEP_NAME="" STEP_START="" STEP_OPTIONAL=""
FAILED_STEPS=""
STEP_OUTPUT_TAIL=""

# step_begin <number> <name> [optional]
step_begin() {
    STEP_NUM="$1"
    STEP_NAME="$2"
    STEP_OPTIONAL="${3:-}"
    STEP_START=$(utc_now)
}

# _step_record <rc> <ok|failed|skipped> <message>
_step_record() {
    [ -n "$STEP_NUM" ] || return 0
    "${RUN_STATUS[@]}" step --run "$RUN_ID" --step "$STEP_NUM" --name "$STEP_NAME" \
        --start "$STEP_START" --end "$(utc_now)" --rc "$1" --status "$2" \
        --message "$3" ${STEP_OPTIONAL:+--optional} || true
    [ "$2" = failed ] && FAILED_STEPS="$FAILED_STEPS${FAILED_STEPS:+, }Step $STEP_NUM ($STEP_NAME)"
    STEP_NUM=""
}

# step_end <rc> <why, if it failed> [<note, if it worked>] -- ok when rc is 0
step_end() {
    if [ "$1" = 0 ]; then
        _step_record 0 ok "${3:-}"
    else
        _step_record "$1" failed "$2"
    fi
}

# step_skip <message> -- the step had nothing to do, or could not start
step_skip() {
    _step_record "" skipped "$1"
}

run_status_end() {
    local rc="$1"
    # A step still open: the run stopped inside it. Every exit of this
    # script closes its step first, so this is an error that ended the
    # shell, or a signal. Which one bash does not say ($? is 0 or the
    # status of the last command when a SIGTERM ends it), so a 0 here is
    # not taken for success: no exit status is recorded.
    if [ -n "$STEP_NUM" ]; then
        [ "$rc" = 0 ] && rc=""
        _step_record "$rc" failed \
            "the run ended during this step${rc:+ (exit status $rc)}: killed, or an error that stopped the script"
    fi
    "${RUN_STATUS[@]}" end --run "$RUN_ID" --time "$(utc_now)" --rc "$rc" || true
}

mkdir -p "$PROJECT_DIR/logs"
"${RUN_STATUS[@]}" begin --run "$RUN_ID" --pid "$$" --time "$(utc_now)" || true
trap 'run_status_end $?' EXIT

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
# progress against a known total. count_fn names a function that
# prints how many such files there are now.
run_with_progress_count() {
    local message="$1"
    local count_fn="$2"
    local total="$3"
    shift 3

    "$@" > /tmp/process_and_plot_step.$$ 2>&1 &
    local pid=$!

    local frames='|/-\'
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        i=$(( (i + 1) % 4 ))
        local current
        current=$("$count_fn")
        printf "\r  [%s]   %s (%d/%d files)   " "${frames:$i:1}" "$message" "$current" "$total"
        sleep 0.3
    done

    wait "$pid"
    local exit_code=$?

    local final_count
    final_count=$("$count_fn")

    if [ "$exit_code" -eq 0 ]; then
        printf "\r  [OK]   %s (%d/%d files)   \n" "$message" "$final_count" "$total"
    else
        printf "\r  [FAIL] %s (%d/%d files)   \n" "$message" "$final_count" "$total"
        echo "  ---- output ----"
        cat /tmp/process_and_plot_step.$$
        echo "  -----------------"
    fi
    # its last line (on a crash, the error) for the run status
    STEP_OUTPUT_TAIL=$(grep -v '^[[:space:]]*$' /tmp/process_and_plot_step.$$ | tail -n 1)

    rm -f /tmp/process_and_plot_step.$$
    return "$exit_code"
}

# ----------------------------------------------------------------
# Step 0: sanity checks
# ----------------------------------------------------------------

section "GNSS-IR Reference Station -- Process and Plot"
step_begin 0 "sanity checks"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "  Virtual environment not found. Run ./install.sh first."
    step_end 1 "virtual environment not found at $VENV_DIR -- run ./install.sh"
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! command -v convbin >/dev/null 2>&1; then
    echo "  convbin not found on PATH. Run ./install.sh first."
    step_end 1 "convbin not found on PATH -- run ./install.sh"
    exit 1
fi

if [ ! -f "$STATION_JSON" ]; then
    echo "  station.json not found. Run ./install.sh or ./setup_station.sh first."
    step_end 1 "station.json not found at $STATION_JSON"
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
    step_end 1 "no gnssrefl station code in station.json (unreadable, or no station_id)"
    exit 1
fi
step_end 0 "" "station code $STATION_CODE"

export REFL_CODE="$PROJECT_DIR/products/refl_code"
export ORBITS="$REFL_CODE/orbits"
export EXE="$REFL_CODE/exe"

# Results files of this station, every year: each day's goes to the
# folder of its own year (the 31 December run is already 1 January in
# UTC), so the Step 2 progress count looks at all of them.
count_results() {
    find "$REFL_CODE"/[0-9][0-9][0-9][0-9]/results/"$STATION_CODE" -maxdepth 1 -name "*.txt" 2>/dev/null | wc -l
}

# gnss_record.py reads station.json's optional "gnss_record_first_day"
# ("YYYY-MM-DD") itself: results before it (installation tests, a
# different antenna setup) are left out of the water-level record, and
# setting it later shortens the record on purpose. Every year's results
# folder counts otherwise. Step 1 does not recover days before it.
GNSS_RECORD=(python3 "$PROJECT_DIR/analysis_tools/gnss_record.py"
             --refl-code "$REFL_CODE" --station "$STATION_CODE" --station-json "$STATION_JSON")
RECORD_FIRST_DAY=$(python3 -c "
import json
print(json.load(open('$STATION_JSON')).get('gnss_record_first_day') or '')
" 2>/dev/null)

echo "  Station code: $STATION_CODE"
[ -n "$RECORD_FIRST_DAY" ] && echo "  Water-level record from: $RECORD_FIRST_DAY (gnss_record_first_day)"

# ----------------------------------------------------------------
# Step 1: auto-recover any previously-missed days
# ----------------------------------------------------------------

section "Step 1: Recovering previously missed days (if any)"
step_begin 1 "recover missed days" optional
recover_failed="" recover_rc=0

if [ -f "$PROJECT_DIR/maintenance/recover_missing_days.sh" ]; then
    # Last year too, so a late-December day is still recoverable in
    # January -- but only once the record has a last year (its results
    # folder exists), and never before gnss_record_first_day: those days
    # are not part of the record, and each recovered day is a convbin
    # run plus rinex2snr and gnssir.
    this_year=$(date -u +%Y)
    first_year=0
    [[ "$RECORD_FIRST_DAY" =~ ^[0-9]{4}- ]] && first_year=$((10#${RECORD_FIRST_DAY:0:4}))
    for recover_year in $(( this_year - 1 )) "$this_year"; do
        if [ "$recover_year" -ne "$this_year" ] \
           && [ ! -d "$REFL_CODE/$recover_year/results/$STATION_CODE" ]; then
            continue
        fi
        if [ "$recover_year" -lt "$first_year" ]; then
            echo "  $recover_year: before gnss_record_first_day ($RECORD_FIRST_DAY) -- not recovered."
            continue
        fi
        RECOVER_YEAR="$recover_year" RECOVER_SINCE="$RECORD_FIRST_DAY" \
            bash "$PROJECT_DIR/maintenance/recover_missing_days.sh"
        recover_exit=$?
        if [ "$recover_exit" -ne 0 ]; then
            recover_rc=$recover_exit
            recover_failed="$recover_failed $recover_year (exit $recover_exit)"
        fi
    done
    step_end "$recover_rc" "recover_missing_days.sh failed for$recover_failed -- see the log"
else
    echo "  recover_missing_days.sh not found -- skipping this step."
    echo "  (It recovers missed days from the raw files in archive/"
    echo "  or external storage; the rest of this run does not need it.)"
    step_skip "maintenance/recover_missing_days.sh not found"
fi

# ----------------------------------------------------------------
# Step 2: process any new raw data
# ----------------------------------------------------------------

section "Step 2: Processing new data"
step_begin 2 "process new raw data"

raw_file_count=$(find "$PROJECT_DIR/raw" -maxdepth 1 -name "*.um980" 2>/dev/null | wc -l)

if [ "$raw_file_count" -eq 0 ]; then
    echo "  No new raw files found in raw/ -- nothing to process."
    echo "  (Existing results, if any, will still be plotted below.)"
    step_skip "no raw files in raw/"
else
    echo "  Found $raw_file_count new raw file(s) to process."
    echo "  Each file involves RINEX conversion and GNSS-IR analysis,"
    echo "  and can take anywhere from under a minute to several"
    echo "  minutes per file depending on file size and network"
    echo "  conditions (orbit data is downloaded automatically)."
    echo ""

    results_before=$(count_results)
    expected_after=$((results_before + raw_file_count))

    run_with_progress_count \
        "Running pipeline" \
        count_results \
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
    step_end "$pipeline_exit" "the pipeline exited $pipeline_exit: ${STEP_OUTPUT_TAIL:-no output}" \
        "$raw_file_count raw file(s) in raw/"
fi

# ----------------------------------------------------------------
# Step 3: find the available results -- every year of them
# ----------------------------------------------------------------

section "Step 3: Checking available results"
step_begin 3 "list results"

"${GNSS_RECORD[@]}" summary
summary_exit=$?
if [ "$summary_exit" -ne 0 ] && [ "$summary_exit" -ne 3 ]; then
    echo "  Could not list the results -- see the error above."
    step_end "$summary_exit" "gnss_record.py summary exited $summary_exit: the results could not be listed, so nothing was fitted or plotted"
    exit 1
elif [ "$summary_exit" -eq 3 ]; then
    echo "  No results found yet -- nothing to plot."
    echo ""
    echo "  If you expected results here, run ./test_installation.sh"
    echo "  to check for a configuration problem, or check the log"
    echo "  output above for errors."
    step_end 0 "" "no results yet: nothing to fit or plot"
    exit 0
fi
step_end 0

# ----------------------------------------------------------------
# Step 4: fit the water level and update the whole-record spline
# ----------------------------------------------------------------

section "Step 4: Fitting the water level"
step_begin 4 "water-level fit"

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
# gnss_record.py adds -gap_min_val <hours> to both when station.json sets
# "subdaily_gap_min_val" (unset: gnssrefl's own 6 h), and writes
# ${STATION_CODE}_spline_support.txt next to the spline: per level, the
# hours to the nearest arc subdaily kept, so a level drawn across a gap
# in the arcs can be told from a measured one.
if "${GNSS_RECORD[@]}" update --settings "$SUBDAILY_SETTINGS" \
        --fallback-settings "-rhdot True -knots 8"; then
    echo ""
    echo "  Water level: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_spline_out.txt"
    echo "  gnssrefl's main plot: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_last.png"
    echo "  (the rest of tonight's gnssrefl output: $REFL_CODE/Files/$STATION_CODE/fit)"
    step_end 0
else
    fit_exit=$?
    echo ""
    echo "  The fit reported a problem -- see the output above. The"
    echo "  water-level file keeps the previous night's record."
    step_end "$fit_exit" "gnss_record.py update exited $fit_exit: the water-level record keeps the previous night's (see the log, and Files/$STATION_CODE/fit/subdaily.log)"
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
step_begin 5 "7-day plot"

SEVEN_DAY_PLOT="$REFL_CODE/Files/$STATION_CODE/7_day_plot.png"
SPLINE_FILE="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_spline_out.txt"

# Opt-in: refresh the rolling tide prediction before anything reads it,
# only when station.json has a "tide_prediction" section (and it is not
# "enabled": false; a value other than true or false is refused, with a
# note). The plot uses it only once tide_model_file points
# at the file it writes (analysis_tools/make_tide_prediction.py). A
# failed refresh leaves the previous file as it was.
if python3 -c "
import json, sys
tp = json.load(open('$STATION_JSON')).get('tide_prediction')
on = tp.get('enabled', True) if isinstance(tp, dict) else False
if not isinstance(on, bool):
    print('  tide_prediction \"enabled\" is %r, not true or false: not refreshed' % (on,))
sys.exit(0 if on is True else 1)
" 2>/dev/null; then
    if python3 "$PROJECT_DIR/analysis_tools/make_tide_prediction.py"; then
        echo ""
    else
        echo "  The tide prediction could not be refreshed -- see above. The"
        echo "  previous file is unchanged; the next run will try again."
        echo ""
    fi
fi

if [ ! -f "$SPLINE_FILE" ]; then
    echo "  No spline output yet -- skipping."
    step_skip "no water-level record yet (Files/$STATION_CODE/${STATION_CODE}_spline_out.txt)"
elif [ ! -f "$PROJECT_DIR/analysis_tools/plot_7day.py" ]; then
    echo "  analysis_tools/plot_7day.py not found -- skipping."
    step_skip "analysis_tools/plot_7day.py not found"
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
        step_end 0
    else
        plot7_exit=$?
        echo ""
        echo "  7-day plot could not be generated -- see above. The"
        echo "  underlying results are unaffected; the next run will"
        echo "  try again."
        step_end "$plot7_exit" "plot_7day.py exited $plot7_exit: 7_day_plot.png was not redrawn, so the copy uploaded tonight (if any) is an older one"
    fi

    # Diagnostic (not for the public): GNSS-IR minus the Chatham gauge
    # against the wave setup and runup allowances, last 7 days.
    WAVE_SETUP_PLOT="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_wave_setup_check.png"
    if [ -f "$PROJECT_DIR/analysis_tools/water_level_qc.py" ]; then
        step_begin 5 "wave-setup check" optional
        if python3 "$PROJECT_DIR/analysis_tools/water_level_qc.py" "$SPLINE_FILE" \
            --plot-setup "$WAVE_SETUP_PLOT"; then
            echo "  Wave-setup check: $WAVE_SETUP_PLOT"
            step_end 0
        else
            qc_exit=$?
            echo "  Wave-setup check could not be made -- see above (the 7-day plot is unaffected)."
            step_end "$qc_exit" "water_level_qc.py exited $qc_exit: ${STATION_CODE}_wave_setup_check.png was not redrawn"
        fi
    fi
fi

# ----------------------------------------------------------------
# Step 6: tide model comparison (optional -- only if configured)
# ----------------------------------------------------------------

section "Step 6: Tide model comparison (optional)"
step_begin 6 "tide comparison" optional

TIDE_FILE=$(python3 -c "
import json
d = json.load(open('$STATION_JSON'))
print(d.get('tide_model_file', ''))
" 2>/dev/null)

if [ -z "$TIDE_FILE" ]; then
    echo "  No tide model configured for this station -- skipping."
    echo "  (Set this up any time by re-running the tide model step in"
    echo "  ./install.sh, or by hand -- see STATION_JSON_REFERENCE.md.)"
    step_skip "no tide_model_file in station.json"
elif [ ! -f "$TIDE_FILE" ]; then
    echo "  A tide model is configured (\"$TIDE_FILE\") but that file"
    echo "  no longer exists at that location -- skipping this step."
    echo "  Check the path, or re-configure it via ./install.sh."
    step_skip "tide_model_file $TIDE_FILE not found"
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
        step_skip "no water-level record yet"
    else
        echo "  Comparing against: $TIDE_FILE (column: $TIDE_VALUE_COL)"
        echo ""

        TIDE_PLOT_OUTPUT="$REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_vs_tide.png"

        step_begin 6 "tide plot" optional
        python3 "$PROJECT_DIR/analysis_tools/plot_gnssir_vs_tide.py" \
            --spline-file "$SPLINE_FILE" \
            --tide-file "$TIDE_FILE" \
            --tide-time-col "$TIDE_TIME_COL" \
            --tide-value-col "$TIDE_VALUE_COL" \
            --output "$TIDE_PLOT_OUTPUT"
        plot_exit=$?
        step_end "$plot_exit" "plot_gnssir_vs_tide.py exited $plot_exit: ${STATION_CODE}_vs_tide.png was not redrawn"

        echo ""

        step_begin 6 "tide comparison" optional
        python3 "$PROJECT_DIR/analysis_tools/compare_to_tide_deviation.py" \
            --spline-file "$SPLINE_FILE" \
            --tide-file "$TIDE_FILE" \
            --tide-time-col "$TIDE_TIME_COL" \
            --tide-value-col "$TIDE_VALUE_COL"
        compare_exit=$?
        step_end "$compare_exit" "compare_to_tide_deviation.py exited $compare_exit -- see the log (it exits 1 when fewer than 5 water levels fall inside the tide file)"

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
echo "  gnssrefl's main plot: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_last.png (the rest in fit/)"
if [ -f "$REFL_CODE/Files/$STATION_CODE/7_day_plot.png" ]; then
    echo "  7-day public plot: $REFL_CODE/Files/$STATION_CODE/7_day_plot.png"
fi
if [ -n "$TIDE_FILE" ] && [ -f "$TIDE_FILE" ]; then
    echo "  Tide comparison plot: $REFL_CODE/Files/$STATION_CODE/${STATION_CODE}_vs_tide.png"
fi
if [ -n "$FAILED_STEPS" ]; then
    echo ""
    echo "  Failed in this run: $FAILED_STEPS"
    echo "  (the run still exits 0, so daily_gnss.sh uploads what was made;"
    echo "  the health check reports these from $STATUS_FILE)"
fi
echo ""
echo "To check whether an apparent signal is real (not an artifact),"
echo "see ./validate_station.py."
