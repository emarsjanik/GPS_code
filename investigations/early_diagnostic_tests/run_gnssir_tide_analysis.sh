#!/bin/bash

set -u

echo "=============================================="
echo "USGS GNSS Reference Station"
echo "GNSS-IR / Tide Analysis"
echo "=============================================="
echo

cd "$HOME/GNSS/v4.1" || exit 1

echo "Working directory:"
pwd
echo

if [ ! -f "gnssrefl_venv/bin/activate" ]; then
    echo "ERROR: gnssrefl virtual environment not found."
    exit 1
fi

echo "Activating gnssrefl virtual environment..."
source "$HOME/GNSS/v4.1/gnssrefl_venv/bin/activate"

echo
echo "Python:"
python3 --version

echo
echo "gnssrefl executables:"
which gnssir
which rinex2snr
which quickLook

echo
echo "REFL_CODE before configuration:"
echo "${REFL_CODE:-}"

# ------------------------------------------------------------------
# Configure gnssrefl environment
# ------------------------------------------------------------------

export REFL_CODE="$HOME/GNSS/v4.1/products/refl_code"
export EXE="$REFL_CODE/exe"
export ORBITS="$REFL_CODE/orbits"

echo
echo "Environment:"
echo "  REFL_CODE=$REFL_CODE"
echo "  EXE=$EXE"
echo "  ORBITS=$ORBITS"

echo

if [ ! -d "$REFL_CODE" ]; then
    echo "ERROR: REFL_CODE directory does not exist:"
    echo "$REFL_CODE"
    exit 1
fi

if [ ! -d "$ORBITS" ]; then
    echo "ERROR: ORBITS directory does not exist:"
    echo "$ORBITS"
    exit 1
fi

if [ ! -f "$HOME/GNSS/v4.1/marconi_tides_sherwood.xlsx" ]; then
    echo "ERROR: tide model workbook not found:"
    echo "$HOME/GNSS/v4.1/marconi_tides_sherwood.xlsx"
    exit 1
fi

echo
echo "Running analysis..."
echo

python3 analyze_gnssir_tide_relationship.py \
    usgs \
    2026 \
    204 \
    207 \
    marconi_tides_sherwood.xlsx

STATUS=$?

echo
echo "=============================================="

if [ "$STATUS" -eq 0 ]; then
    echo "Analysis completed successfully."
else
    echo "Analysis FAILED."
    echo "Exit status: $STATUS"
fi

echo "=============================================="

exit "$STATUS"
