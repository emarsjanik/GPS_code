#!/bin/bash
#
# recover_missing_days.sh
#
# Recovers day-of-year data that the automatic pipeline missed.
#
# DEFAULT BEHAVIOR (no arguments): fully automatic. Scans external
# storage for every day that has recoverable RINEX data but no local
# results yet, and attempts to recover all of them, unattended.
# Nothing needs to be specified by hand -- this is the normal way to
# run this script.
#
#   ./recover_missing_days.sh
#
# Safe to run repeatedly/unattended: days already processed are
# automatically skipped, and days whose orbit still isn't published
# yet simply get reported as unrecoverable this time, harmlessly,
# ready to retry on a future run.
#
# Manual override, for a specific day or days only:
#
#   ./recover_missing_days.sh 204 205 206 207
#
# CONFIRMED, REAL GAP THIS SCRIPT WORKS AROUND: the automatic daily
# cycle tries to process "yesterday" once, right after its own
# rollover -- but at that point the day's official orbit product
# almost never exists yet (confirmed: this routinely takes 1-2 days
# to publish). That single attempt fails, the raw file then gets
# exported/archived before anything ever retries, and the day is
# silently lost -- unless a human manually recovers it while the
# RINEX data still exists in the external export, which is exactly
# what this script automates.
#
# For each day-of-year identified (automatically or given explicitly),
# this:
#   1. Searches every dated export folder under external storage for
#      that day's already-converted RINEX file (confirmed still
#      present there even after the raw .um980 itself is gone)
#   2. Copies it to where gnssrefl's own tools expect to find a
#      local file
#   3. Regenerates the SNR file from it (this is the step that
#      should now succeed, since real time has passed and the
#      orbit should be published by now)
#   4. Runs the actual GNSS-IR analysis for that day, using
#      whatever tuning is currently configured in station.json
#
# CONFIRMED, REAL DISK-LEAK BUG THIS VERSION FIXES: step 2's staged
# copy (a full RINEX file, ~350-400MB) was only ever cleaned up by a
# plain, sequential "rm -f" line after step 3/4 completed -- which
# does NOT run if the script itself is interrupted mid-iteration
# (an SSH disconnect, Ctrl+C, or the process being killed), since a
# script that's been killed never reaches its own later lines.
# Confirmed directly on real hardware: 35 files, matching exactly
# this staged-copy pattern, found accumulated at the project root
# from prior runs of this script. Fixed here using bash's `trap ...
# EXIT`, which (unlike a plain sequential command) fires even when
# the script is interrupted or killed -- verified directly, in
# isolation, that a plain "rm -f" leaves the in-progress file behind
# under SIGTERM while the trap-based version does not, under
# otherwise identical conditions.
#
# After running this, regenerate the plots as usual with
# process_gps_data.sh (or ~/process_gps_data.sh) to include the
# newly recovered day(s).

set -uo pipefail

PROJECT_DIR="$HOME/GNSS/v4.1"
STATION_CODE="usgs"           # 4-character code, used for GNSS-IR analysis
STATION_CODE_LONG="usgs00usa" # 9-character code, used for RINEX-related steps
YEAR="2026"                   # bump this once a year
EXTERNAL_PRODUCTS_DIR="/mnt/I2Rgus_Data/GPS_Data/Products"
LOCAL_RESULTS_DIR="$PROJECT_DIR/products/refl_code/$YEAR/results/$STATION_CODE"

# ------------------------------------------------------------
# Global cleanup guarantee: this holds the path of whatever staged
# RINEX copy currently exists at the project root, if any. Set (and
# cleared) once per iteration in the main loop below. Registered
# once, here, at script start -- not re-registered inside the loop
# -- since a single global trap that reads this variable at
# fire-time behaves correctly regardless of which iteration the
# script happens to be interrupted during, and is simpler to reason
# about than re-registering the trap every iteration.
# ------------------------------------------------------------
current_staged_path=""

cleanup_staged_file() {
    if [ -n "$current_staged_path" ] && [ -f "$current_staged_path" ]; then
        rm -f "$current_staged_path"
    fi
}

trap cleanup_staged_file EXIT

# ------------------------------------------------------------
# Auto mode: find every day-of-year with a recoverable RINEX file
# somewhere in external storage that doesn't already have a local
# results file for it. This is the DEFAULT behavior (no arguments
# given) -- nothing needs to be specified by hand for normal use.
# ------------------------------------------------------------
if [ "$#" -eq 0 ]; then
    echo "No specific days given -- auto-detecting missing, recoverable days..."

    if [ ! -d "$EXTERNAL_PRODUCTS_DIR" ]; then
        echo "External storage path not found at $EXTERNAL_PRODUCTS_DIR -- nothing to check."
        exit 0
    fi

    # Confirmed real filename pattern (RINEX 3): matches
    # USGS00USA_R_2026<DOY>0000_01D_01S_MO.rnx across every dated
    # export folder, extracting just the 3-digit day-of-year from
    # each match.
    recoverable_doys=$(
        find "$EXTERNAL_PRODUCTS_DIR" -iname "${STATION_CODE_LONG^^}_R_${YEAR}*_01D_01S_MO.rnx" 2>/dev/null \
            | sed -E "s/.*${STATION_CODE_LONG^^}_R_${YEAR}([0-9]{3}).*/\1/" \
            | sort -u
    )

    if [ -z "$recoverable_doys" ]; then
        echo "No recoverable RINEX files found anywhere under $EXTERNAL_PRODUCTS_DIR."
        exit 0
    fi

    days_to_recover=()
    for doy_padded in $recoverable_doys; do
        doy=$((10#$doy_padded))  # force base-10 so e.g. "008" isn't read as invalid octal
        local_results_file="$LOCAL_RESULTS_DIR/${doy}.txt"
        local_no_data_marker="$LOCAL_RESULTS_DIR/${doy}.no_data"
        # A day is only still "missing" if it has neither a real
        # results file NOR a no-data marker (see below) recording
        # that gnssir was already run and genuinely found nothing --
        # without checking the marker too, a day with zero usable
        # satellite passes would be reported as "missing" and
        # re-attempted on every single future auto-detection run
        # forever, since gnssir itself never writes anything for
        # such a day regardless of how many times it's re-run
        # against the same underlying SNR data.
        if [ ! -f "$local_results_file" ] && [ ! -f "$local_no_data_marker" ]; then
            days_to_recover+=("$doy")
        fi
    done

    if [ "${#days_to_recover[@]}" -eq 0 ]; then
        echo "Every recoverable day already has local results -- nothing to do."
        exit 0
    fi

    echo "Found ${#days_to_recover[@]} day(s) with recoverable data but no local results:"
    echo "  ${days_to_recover[*]}"
    echo ""

    # Replace the positional arguments ($@) with the auto-detected
    # day list, so everything below runs exactly as it would in
    # manual mode -- no separate code path to maintain.
    set -- "${days_to_recover[@]}"
fi

cd "$PROJECT_DIR" || { echo "Could not cd to $PROJECT_DIR -- aborting."; exit 1; }
source gnssrefl_venv/bin/activate

export REFL_CODE="$PROJECT_DIR/products/refl_code"
export ORBITS="$REFL_CODE/orbits"
export EXE="$REFL_CODE/exe"

recovered_days=()
failed_days=()
no_data_days=()

for doy in "$@"; do
    doy_padded=$(printf "%03d" "$doy")
    rinex_filename="${STATION_CODE_LONG^^}_R_${YEAR}${doy_padded}0000_01D_01S_MO.rnx"

    echo ""
    echo "================================================================"
    echo "  Day of year $doy -- looking for $rinex_filename"
    echo "================================================================"

    found_path=$(find "$EXTERNAL_PRODUCTS_DIR" -name "$rinex_filename" -print -quit 2>/dev/null)

    if [ -z "$found_path" ]; then
        echo "!! Could not find $rinex_filename in any dated export folder"
        echo "!! under $EXTERNAL_PRODUCTS_DIR -- this day may be genuinely"
        echo "!! unrecoverable (the RINEX data itself is gone, not just the"
        echo "!! results). Skipping."
        failed_days+=("$doy")
        continue
    fi

    echo "Found: $found_path"

    # Confirmed necessary: rinex2snr's "local directory" means the
    # current working directory it's run from, not any arbitrary
    # folder -- copying here specifically, not e.g. station/, is
    # what actually works.
    local_rinex_path="$PROJECT_DIR/$rinex_filename"

    # Registers this specific file with the global EXIT trap above
    # BEFORE it's created, so an interruption at any point from here
    # onward -- including one that happens during the cp itself, or
    # during rinex2snr, or during gnssir -- is guaranteed to clean
    # it up on the way out. Cleared back to empty once this
    # iteration's own, normal cleanup below has already run, so the
    # trap does not try to remove a file that's already gone (rm -f
    # would no-op harmlessly either way, but clearing this keeps the
    # intent explicit).
    current_staged_path="$local_rinex_path"

    if [ -f "$local_rinex_path" ]; then
        rm -f "$local_rinex_path"
    fi
    cp "$found_path" "$local_rinex_path"

    echo "Regenerating SNR file..."
    rinex2snr "$STATION_CODE_LONG" "$YEAR" "$doy" -nolook True -orb gnss -samplerate 1
    snr_exit=$?

    # Clean up the staged RINEX copy either way -- it's only ever a
    # temporary local copy for this one step. This normal-path
    # cleanup is kept (not just relying on the EXIT trap) so the
    # working directory stays clean between iterations during an
    # ordinary, uninterrupted run, not only when something goes
    # wrong.
    rm -f "$local_rinex_path"
    current_staged_path=""

    if [ "$snr_exit" -ne 0 ]; then
        echo "!! rinex2snr did not exit cleanly (exit code $snr_exit) for day $doy."
        failed_days+=("$doy")
        continue
    fi

    echo "Running GNSS-IR analysis for day $doy..."
    gnssir "$STATION_CODE" "$YEAR" "$doy"
    gnssir_exit=$?

    if [ "$gnssir_exit" -ne 0 ]; then
        echo "!! gnssir did not exit cleanly (exit code $gnssir_exit) for day $doy."
        failed_days+=("$doy")
        continue
    fi

    # Confirmed, real gap this closes: gnssir exits 0 (success) even
    # when it finds zero usable satellite passes and writes no
    # results file at all -- confirmed directly, real output:
    # "No good retrievals found so no LSP file should be created".
    # Trusting the exit code alone previously reported these days as
    # "Recovered" even though nothing was actually produced, which
    # then caused the next auto-detection run to try to "recover"
    # the exact same day again, forever, since a missing results
    # file is exactly what auto-detection looks for. Same "do not
    # assume success" principle already applied throughout this
    # project's Python code (rinex_processor.py,
    # gnssrefl_processor.py) -- this shell script never had the
    # same verification until now.
    results_file="$LOCAL_RESULTS_DIR/${doy}.txt"

    if [ ! -s "$results_file" ]; then
        echo "!! gnssir exited cleanly but produced no usable results for"
        echo "!! day $doy (a genuine, real outcome -- not every day has"
        echo "!! enough satellite passes to produce a retrieval, "
        echo "!! especially under a narrow elevation mask). Not counted"
        echo "!! as recovered; recording a marker so future auto-detection"
        echo "!! runs correctly skip this day instead of retrying it"
        echo "!! forever (delete the marker by hand to force a retry, e.g."
        echo "!! after changing station.json's elevation/RH configuration)."
        mkdir -p "$LOCAL_RESULTS_DIR"
        date -u "+%Y-%m-%dT%H:%M:%SZ -- no usable retrievals" > "$LOCAL_RESULTS_DIR/${doy}.no_data"
        no_data_days+=("$doy")
        continue
    fi

    recovered_days+=("$doy")
done

echo ""
echo "================================================================"
echo "  Summary"
echo "================================================================"

if [ "${#recovered_days[@]}" -gt 0 ]; then
    echo "Recovered: ${recovered_days[*]}"
else
    echo "Recovered: none"
fi

if [ "${#no_data_days[@]}" -gt 0 ]; then
    echo "Processed, but no usable retrievals found: ${no_data_days[*]}"
    echo "(A genuine, real outcome -- not every day has enough satellite"
    echo "passes to produce a retrieval, especially under a narrow"
    echo "elevation mask. These will NOT be retried on future runs unless"
    echo "you pass their day-of-year explicitly, since re-running gnssir"
    echo "against the same SNR data will not produce a different result.)"
fi

if [ "${#failed_days[@]}" -gt 0 ]; then
    echo "Could not recover: ${failed_days[*]}"
    echo "(Either the RINEX data is genuinely gone, or the orbit isn't"
    echo "published yet -- try again in a day or two for the latter case.)"
fi

echo ""
echo "Next step: regenerate plots to include any newly recovered days:"
echo "  cd ~ && ./process_gps_data.sh"
