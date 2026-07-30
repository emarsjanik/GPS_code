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
# Usage: ./recover_missing_days.sh 204 205 206 207
#        (one or more day-of-year numbers, space separated)

set -uo pipefail

PROJECT_DIR="$HOME/GNSS/v4.1"
STATION_CODE="usgs"           # 4-character code, used for GNSS-IR analysis
STATION_CODE_LONG="usgs00usa" # 9-character code, used for RINEX-related steps
YEAR="2026"                   # bump this once a year
EXTERNAL_PRODUCTS_DIR="/mnt/I2Rgus_Data/GPS_Data/Products"

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 <doy1> [doy2] [doy3] ..."
    echo "Example: $0 204 205 206 207"
    exit 1
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
