#!/bin/bash
#
# recover_missing_days.sh
#
# Recovers day-of-year data that the automatic pipeline missed.
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
# For each day-of-year given as an argument, this:
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
# After running this, regenerate the plots as usual with
# process_gps_data.sh (or ~/process_gps_data.sh) to include the
# newly recovered day(s).
#
# Usage:
#   ./recover_missing_days.sh                  <- AUTO mode: finds
#       every day with recoverable RINEX data in external storage
#       that doesn't already have local results, and attempts to
#       recover all of them. Safe to run repeatedly/unattended: days
#       already processed are automatically skipped, and days whose
#       orbit still isn't published simply get reported as
#       unrecoverable this time, harmlessly, ready to retry later.
#   ./recover_missing_days.sh 204 205 206 207  <- MANUAL mode: recover
#       specific days only, unchanged from before.

set -uo pipefail

PROJECT_DIR="$HOME/GNSS/v4.1"
STATION_CODE="usgs"           # 4-character code, used for GNSS-IR analysis
STATION_CODE_LONG="usgs00usa" # 9-character code, used for RINEX-related steps
YEAR="2026"                   # bump this once a year
EXTERNAL_PRODUCTS_DIR="/mnt/I2Rgus_Data/GPS_Data/Products"
LOCAL_RESULTS_DIR="$PROJECT_DIR/products/refl_code/$YEAR/results/$STATION_CODE"

# ------------------------------------------------------------
# Auto mode: find every day-of-year with a recoverable RINEX file
# somewhere in external storage that doesn't already have a local
# results file for it.
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
        if [ ! -f "$local_results_file" ]; then
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
    if [ -f "$local_rinex_path" ]; then
        rm -f "$local_rinex_path"
    fi
    cp "$found_path" "$local_rinex_path"

    echo "Regenerating SNR file..."
    rinex2snr "$STATION_CODE_LONG" "$YEAR" "$doy" -nolook True -orb gnss -samplerate 1
    snr_exit=$?

    # Clean up the staged RINEX copy either way -- it's only ever a
    # temporary local copy for this one step.
    rm -f "$local_rinex_path"

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

if [ "${#failed_days[@]}" -gt 0 ]; then
    echo "Could not recover: ${failed_days[*]}"
    echo "(Either the RINEX data is genuinely gone, or the orbit still"
    echo "isn't published yet -- try again in a day or two for the latter.)"
fi

echo ""
echo "Next step: regenerate plots to include any newly recovered days:"
echo "  cd ~ && ./process_gps_data.sh"
