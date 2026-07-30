#!/bin/bash
#
# plot_all_data.sh
#
# Gathers every real day-of-year results file we have anywhere --
# whatever's sitting locally right now, plus every dated folder
# under external storage -- into one place, then regenerates the
# full water-level plot spanning the complete available range,
# rather than whatever narrow window happens to be sitting locally
# after the day's export cycle.
#
# Does NOT reprocess any raw data -- this only combines and re-plots
# results that already exist somewhere. Use process_gps_data.sh (or
# recover_missing_days.sh for a specific missing day) to actually
# process new data first.
#
# Usage: ./plot_all_data.sh

set -uo pipefail

PROJECT_DIR="$HOME/GNSS/v4.1"
STATION_ID="usgs"
YEAR="2026"  # bump this once a year
EXTERNAL_PRODUCTS_DIR="/mnt/I2Rgus_Data/GPS_Data/Products"

cd "$PROJECT_DIR" || { echo "Could not cd to $PROJECT_DIR -- aborting."; exit 1; }
source gnssrefl_venv/bin/activate

export REFL_CODE="$PROJECT_DIR/products/refl_code"
export ORBITS="$REFL_CODE/orbits"
export EXE="$REFL_CODE/exe"

RESULTS_DIR="$REFL_CODE/$YEAR/results/$STATION_ID"
mkdir -p "$RESULTS_DIR"

echo "================================================================"
echo "  Gathering every real result file into one place"
echo "================================================================"

gathered=0

if [ -d "$EXTERNAL_PRODUCTS_DIR" ]; then
    while IFS= read -r -d '' file; do
        # -u: only copies if the source is newer than any existing
        # local copy (or no local copy exists yet) -- confirmed
        # necessary since the same day can genuinely exist in more
        # than one export folder (e.g. if it was recovered and
        # re-exported later), and we want whichever copy is
        # actually the most current, not just whichever the search
        # happens to find last.
        cp -u "$file" "$RESULTS_DIR/"
        gathered=$((gathered + 1))
    done < <(find "$EXTERNAL_PRODUCTS_DIR" -path "*/results/$STATION_ID/*.txt" -not -path "*failQC*" -print0)
fi

echo "Checked $gathered result file(s) found in external storage against"
echo "local copies (any local copy that was already newer was left alone)."

day_files=$(find "$RESULTS_DIR" -maxdepth 1 -name "*.txt" -exec basename {} .txt \; | sort -n)

if [ -z "$day_files" ]; then
    echo ""
    echo "No results found anywhere -- nothing to plot."
    exit 1
fi

echo ""
echo "Real results found for these days of year (combined from every"
echo "location):"
for d in $day_files; do
    track_count=$(grep -vc "^%" "$RESULTS_DIR/$d.txt" 2>/dev/null || echo 0)
    echo "  Day $d: $track_count tracks"
done

min_day=$(echo "$day_files" | head -1)
max_day=$(echo "$day_files" | tail -1)

echo ""
echo "================================================================"
echo "  Generating plots (days $min_day-$max_day)"
echo "================================================================"

subdaily "$STATION_ID" "$YEAR" -doy1 "$min_day" -doy2 "$max_day" -rhdot True -knots 4
subdaily_exit=$?

if [ "$subdaily_exit" -ne 0 ]; then
    echo ""
    echo "!! subdaily did not exit cleanly (exit code $subdaily_exit)."
else
    echo ""
    echo "Plots saved under:"
    echo "  $REFL_CODE/Files/$STATION_ID/"
fi
