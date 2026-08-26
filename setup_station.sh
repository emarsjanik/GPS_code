#!/bin/bash
#
# setup_station.sh
#
# Interactive setup wizard for a new GNSS-IR reference station.
# Walks through every user-configurable variable across this
# project's own code (station identity, receiver, recording) AND
# real gnssrefl parameters that exist but were never previously
# exposed here (elevation mask, reflector height range, azimuth
# mask, QC thresholds, orthometric height, refraction model) --
# confirmed via a direct review of gnssrefl's own documentation.
#
# Everything gets written to one consolidated station.json. Press
# Enter at any prompt to accept the default shown in [brackets].
# Advanced/optional settings have no default shown -- press Enter
# to skip them entirely and let gnssrefl use its own internal
# default instead.

set -uo pipefail

# Confirmed, real portability bug this fixes: this path was
# previously hardcoded to $HOME/GNSS/v4.1, so a user who cloned this
# project anywhere else had their configuration written to a
# directory with no relation to their actual install -- silently,
# with no warning, leaving the real install with no config at all.
# Every other script in this project computes its paths relative to
# its own file location; this one now does too.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATION_JSON="$PROJECT_DIR/station/resources/station.json"

prompt() {
    local question="$1"
    local default="$2"
    local answer
    if [ -n "$default" ]; then
        read -rp "$question [$default]: " answer
        echo "${answer:-$default}"
    else
        read -rp "$question (optional -- Enter to skip): " answer
        echo "$answer"
    fi
}

require_prompt() {
    # Same as prompt(), but keeps re-asking until a non-empty value
    # is given -- for fields critical enough that skipping them
    # would break the whole system (coordinates). Returns non-zero
    # (rather than calling exit, which would only kill this
    # function's own command-substitution subshell, not the actual
    # script) if input runs out -- callers must check this.
    local question="$1"
    local answer=""
    while [ -z "$answer" ]; do
        if ! read -rp "$question (REQUIRED): " answer; then
            echo "" >&2
            echo "No input received -- this script must be run interactively." >&2
            return 1
        fi
        if [ -z "$answer" ]; then
            echo "This field is required -- the system cannot work without it." >&2
        fi
    done
    echo "$answer"
}

require_numeric_prompt() {
    # Same as require_prompt(), but also rejects anything that isn't
    # a plain decimal number -- confirmed necessary: real users
    # naturally try degrees/minutes/seconds format (e.g.
    # 41 53'35.07") since that's how coordinates are often displayed
    # elsewhere, but this script (and the underlying gnssrefl code)
    # only accepts plain decimal degrees.
    local question="$1"
    local example="$2"
    local answer=""
    while true; do
        if ! read -rp "$question, e.g. $example (REQUIRED): " answer; then
            echo "" >&2
            echo "No input received -- this script must be run interactively." >&2
            return 1
        fi
        if [ -z "$answer" ]; then
            echo "This field is required -- the system cannot work without it." >&2
            continue
        fi
        if ! python3 -c "import sys; float(sys.argv[1])" "$answer" 2>/dev/null; then
            echo "That doesn't look like a plain decimal number (got: $answer)." >&2
            echo "Use decimal degrees only, e.g. $example -- not degrees/minutes/seconds." >&2
            answer=""
            continue
        fi
        break
    done
    echo "$answer"
}

prompt_numeric_list() {
    # Optional (blank is fine and means "skip"), but if any non-blank
    # value is given, every space-separated token must be a valid
    # number -- re-prompts on invalid input rather than silently
    # accepting it and only failing much later, at the final
    # JSON-writing step, which would otherwise throw away every
    # other answer already given in a long session.
    local question="$1"
    local answer=""
    while true; do
        if ! read -rp "$question (optional -- Enter to skip): " answer; then
            echo "" >&2
            echo "No input received -- this script must be run interactively." >&2
            return 1
        fi
        if [ -z "$answer" ]; then
            break
        fi
        local all_valid=true
        for token in $answer; do
            if ! python3 -c "import sys; float(sys.argv[1])" "$token" 2>/dev/null; then
                all_valid=false
                break
            fi
        done
        if [ "$all_valid" = true ]; then
            break
        fi
        echo "That doesn't look like a space-separated list of plain numbers (got: $answer)." >&2
        echo "Example: 0 150 180 360 -- or just press Enter to skip this entirely." >&2
        answer=""
    done
    echo "$answer"
}

prompt_optional_numeric() {
    # Optional (blank is fine and means "skip"), but if a non-blank
    # value is given, it must be a valid plain number -- re-prompts
    # on invalid input (e.g. a stray keystroke or accidental partial
    # paste landing in this field) rather than silently accepting it
    # and only failing much later, at the final JSON-writing step,
    # which would otherwise throw away every other answer already
    # given in a long session.
    local question="$1"
    local answer=""
    while true; do
        if ! read -rp "$question (optional -- Enter to skip): " answer; then
            echo "" >&2
            echo "No input received -- this script must be run interactively." >&2
            return 1
        fi
        if [ -z "$answer" ]; then
            break
        fi
        if python3 -c "import sys; float(sys.argv[1])" "$answer" 2>/dev/null; then
            break
        fi
        echo "That doesn't look like a plain number (got: $answer)." >&2
        echo "Enter a number, or just press Enter to skip this entirely." >&2
        answer=""
    done
    echo "$answer"
}

echo "================================================================"
echo "  GNSS-IR Reference Station Setup Wizard"
echo "================================================================"
echo ""
echo "This will walk you through every configurable setting for this"
echo "station. Press Enter to accept a default shown in [brackets]."
echo "Advanced settings with no default can be skipped entirely."
echo ""

echo "--- Station identity ---"
station_id=$(prompt "Station ID" "USGS001")
station_name=$(prompt "Station name" "USGS Station")
agency=$(prompt "Agency" "USGS")
observer=$(prompt "Observer / organization" "")

echo ""
echo "--- Receiver ---"
receiver_model=$(prompt "Receiver model" "Unicore UM980")
receiver_firmware=$(prompt "Receiver firmware version" "")
receiver_port=$(prompt "Receiver serial port" "/dev/USB_GPS")
receiver_baud=$(prompt "Receiver baud rate" "115200")
receiver_timeout=$(prompt "Receiver timeout (seconds)" "2.0")

echo ""
echo "--- Location (real, accurate coordinates matter a lot here) ---"
echo "Use plain decimal degrees only -- NOT degrees/minutes/seconds"
echo "(e.g. use 41.8928, not 41 53'34\")."
latitude=$(require_numeric_prompt "Latitude (decimal degrees)" "41.8928") || exit 1
longitude=$(require_numeric_prompt "Longitude (decimal degrees)" "-69.9633") || exit 1
height=$(require_numeric_prompt "Antenna height (meters, ellipsoidal)" "21.774") || exit 1

echo ""
echo "--- Marker / antenna ---"
marker_name=$(prompt "Marker name" "$station_id")
marker_number=$(prompt "Marker number" "$station_id")
antenna_model=$(prompt "Antenna model" "Unknown")
antenna_serial=$(prompt "Antenna serial number" "")
antenna_height=$(prompt "Antenna height offset (meters)" "0.0")
antenna_east=$(prompt "Antenna east offset (meters)" "0.0")
antenna_north=$(prompt "Antenna north offset (meters)" "0.0")

echo ""
echo "--- RINEX ---"
rinex_version=$(prompt "RINEX version" "3.05")

echo ""
echo "--- Recording ---"
chunk_seconds=$(prompt "Recording chunk size (seconds)" "600")

echo ""
echo "--- gnssrefl station code ---"
echo "Normally auto-derived from Station ID's first 4 characters,"
echo "lowercased. Only set this if you need something different."
gnssrefl_station_code=$(prompt "gnssrefl 4-character station code" "")
gnssrefl_monument_number=$(prompt "Monument number" "00")
gnssrefl_country_code=$(prompt "Country code (3 letters)" "usa")

echo ""
echo "--- Orbit source ---"
echo "Leave blank for gnssrefl's own default (multi-GNSS SP3 orbits"
echo "from CDDIS). Set to 'nav' only if you need offline-friendlier"
echo "GPS-only broadcast ephemeris instead."
gnssrefl_orbit_source=$(prompt "Orbit source" "")

echo ""
echo "--- Sample rate ---"
gnssrefl_sample_rate=$(prompt "GNSS-IR sample rate in seconds (must match your receiver's real logging rate)" "1")

echo ""
echo "--- Multi-constellation processing ---"
echo "Strongly recommended: yes, unless you specifically want"
echo "GPS-only analysis."
all_freq_input=$(prompt "Use all constellations (GPS+GLONASS+Galileo+BeiDou)? (yes/no)" "yes")
if [ "$all_freq_input" = "yes" ] || [ "$all_freq_input" = "y" ]; then
    gnssrefl_all_frequencies="true"
else
    gnssrefl_all_frequencies="false"
fi

echo ""
echo "================================================================"
echo "  Advanced gnssrefl fine-tuning (all optional)"
echo "================================================================"
echo "gnssrefl has sensible defaults for all of these -- only set a"
echo "value if you have a specific reason to (an obstructed view, an"
echo "unusually tall or short site, a noisy environment)."

echo ""
echo "--- Elevation angle mask ---"
echo "gnssrefl's own default if left blank: roughly 5-25 degrees."
elevation_min=$(prompt_optional_numeric "Minimum elevation angle (degrees)") || exit 1
elevation_max=$(prompt_optional_numeric "Maximum elevation angle (degrees)") || exit 1

echo ""
echo "--- Reflector height search range ---"
echo "gnssrefl's own default if left blank: roughly 0.5-8 meters."
echo "Widen this if your antenna sits much higher above the"
echo "reflecting surface."
rh_min=$(prompt_optional_numeric "Minimum reflector height (meters)") || exit 1
rh_max=$(prompt_optional_numeric "Maximum reflector height (meters)") || exit 1

echo ""
echo "--- Azimuth mask ---"
echo "Which compass directions have a genuinely clear view? Default"
echo "is all directions (0-360). If part of your view is blocked (a"
echo "building, trees, land), list the clear region(s) instead, e.g."
echo "'0 150 180 360' to exclude the 150-180 degree range."
azimuth_regions=$(prompt_numeric_list "Azimuth regions (space-separated degrees)") || exit 1

echo ""
echo "--- Quality control thresholds ---"
peak2noise=$(prompt_optional_numeric "Minimum peak-to-noise ratio") || exit 1
amplitude_min=$(prompt_optional_numeric "Minimum amplitude") || exit 1

echo ""
echo "--- Orthometric height reference ---"
echo "If you know your antenna's height above a specific vertical"
echo "datum (e.g. NAVD88, MSL), enter it here to get real, absolute"
echo "water level values instead of just relative reflector height."
orthometric_height=$(prompt_optional_numeric "Orthometric height (meters)") || exit 1

echo ""
echo "--- Refraction model ---"
echo "1 = standard Bennett correction (gnssrefl's default). Leave"
echo "blank unless you have a specific reason to change this."
refraction_model=$(prompt_optional_numeric "Refraction model number") || exit 1

echo ""
echo "--- Maximum arc length ---"
echo "gnssrefl's own default if left blank: 75 minutes. Documented as"
echo "too long for sites with a fast tidal rate of change -- a single"
echo "satellite pass's reflector height estimate gets blurred across"
echo "whatever real water-level change happens during that whole"
echo "window. Consider a much shorter value (e.g. 20-30) for a site"
echo "with strong or fast tides."
max_arc_minutes=$(prompt_optional_numeric "Maximum arc length (minutes)") || exit 1

echo ""
echo "--- Arc elevation-span tolerance ---"
echo "gnssrefl's own default if left blank: 2 degrees. Controls how"
echo "close to your full elevation range (above) a satellite pass"
echo "must actually reach to be accepted -- e.g. with elevation limits"
echo "5-15 and the default of 2, an arc must span at least 7-13"
echo "degrees. Documented as worth tightening to 1 for a narrow"
echo "elevation range like 5-15 (this station's own setting above),"
echo "since the default can otherwise reject a meaningful fraction of"
echo "otherwise-good arcs."
elevation_span_tolerance=$(prompt_optional_numeric "Arc elevation-span tolerance (degrees)") || exit 1

echo ""
echo "--- Direct-signal removal polynomial order ---"
echo "gnssrefl's own default if left blank: 4. A lower-order"
echo "polynomial removes the antenna gain/power trend from raw SNR"
echo "data before the real analysis begins. A narrow elevation mask"
echo "(like the 5-15 degree setting above) gives each satellite pass"
echo "less raw data to fit against, which can make the default order"
echo "numerically unstable -- if you've seen a 'RankWarning: the fit"
echo "may be poorly conditioned' message in your own pipeline output,"
echo "lowering this (e.g. to 2) is the standard, documented fix."
direct_signal_poly_order=$(prompt_optional_numeric "Direct-signal removal polynomial order") || exit 1

echo ""
echo "--- External storage (optional) ---"
echo "If you want each day's raw data and processed results moved to"
echo "external storage automatically after processing, enter the"
echo "destination path."
external_storage_path=$(prompt "External storage path" "")

echo ""
echo "================================================================"
echo "  Writing station.json"
echo "================================================================"

mkdir -p "$(dirname "$STATION_JSON")"

python3 - "$STATION_JSON" \
    "$station_id" "$station_name" "$agency" "$observer" \
    "$receiver_model" "$receiver_firmware" "$receiver_port" "$receiver_baud" "$receiver_timeout" \
    "$latitude" "$longitude" "$height" \
    "$marker_name" "$marker_number" "$antenna_model" "$antenna_serial" "$antenna_height" "$antenna_east" "$antenna_north" \
    "$rinex_version" "$chunk_seconds" \
    "$gnssrefl_station_code" "$gnssrefl_monument_number" "$gnssrefl_country_code" \
    "$gnssrefl_orbit_source" "$gnssrefl_sample_rate" "$gnssrefl_all_frequencies" \
    "$elevation_min" "$elevation_max" "$rh_min" "$rh_max" "$azimuth_regions" \
    "$peak2noise" "$amplitude_min" "$orthometric_height" "$refraction_model" "$max_arc_minutes" "$elevation_span_tolerance" "$direct_signal_poly_order" \
    "$external_storage_path" <<'PYEOF'
import json
import sys

(
    path, station_id, station_name, agency, observer,
    receiver_model, receiver_firmware, receiver_port, receiver_baud, receiver_timeout,
    latitude, longitude, height,
    marker_name, marker_number, antenna_model, antenna_serial, antenna_height, antenna_east, antenna_north,
    rinex_version, chunk_seconds,
    gnssrefl_station_code, gnssrefl_monument_number, gnssrefl_country_code,
    gnssrefl_orbit_source, gnssrefl_sample_rate, gnssrefl_all_frequencies,
    elevation_min, elevation_max, rh_min, rh_max, azimuth_regions,
    peak2noise, amplitude_min, orthometric_height, refraction_model,
    max_arc_minutes, elevation_span_tolerance, direct_signal_poly_order,
    external_storage_path,
) = sys.argv[1:]


def as_float(s):
    return float(s) if s.strip() else None


def as_int(s):
    return int(s) if s.strip() else None


data = {
    "station_id": station_id,
    "station_name": station_name,
    "agency": agency,
    "observer": observer,
    "receiver_model": receiver_model,
    "receiver_firmware": receiver_firmware,
    "receiver_port": receiver_port,
    "receiver_baud": as_int(receiver_baud) or 115200,
    "receiver_timeout": as_float(receiver_timeout) or 2.0,
    "latitude": as_float(latitude),
    "longitude": as_float(longitude),
    "height": as_float(height),
    "marker_name": marker_name,
    "marker_number": marker_number,
    "rinex_version": rinex_version,
    "antenna": {
        "model": antenna_model,
        "serial": antenna_serial,
        "height": as_float(antenna_height) or 0.0,
        "east_offset": as_float(antenna_east) or 0.0,
        "north_offset": as_float(antenna_north) or 0.0,
    },
    "record_raw_chunk_seconds": as_int(chunk_seconds) or 600,
    "gnssrefl_sample_rate": as_int(gnssrefl_sample_rate) or 1,
    "gnssrefl_all_frequencies": gnssrefl_all_frequencies == "true",
}

if gnssrefl_station_code.strip():
    data["gnssrefl_station_code"] = gnssrefl_station_code
if gnssrefl_monument_number.strip():
    data["gnssrefl_monument_number"] = gnssrefl_monument_number
if gnssrefl_country_code.strip():
    data["gnssrefl_country_code"] = gnssrefl_country_code
if gnssrefl_orbit_source.strip():
    data["gnssrefl_orbit_source"] = gnssrefl_orbit_source
if as_float(elevation_min) is not None:
    data["gnssrefl_elevation_min"] = as_float(elevation_min)
if as_float(elevation_max) is not None:
    data["gnssrefl_elevation_max"] = as_float(elevation_max)
if as_float(rh_min) is not None:
    data["gnssrefl_reflector_height_min"] = as_float(rh_min)
if as_float(rh_max) is not None:
    data["gnssrefl_reflector_height_max"] = as_float(rh_max)
if azimuth_regions.strip():
    data["gnssrefl_azimuth_regions"] = [float(x) for x in azimuth_regions.split()]
if as_float(peak2noise) is not None:
    data["gnssrefl_peak2noise"] = as_float(peak2noise)
if as_float(amplitude_min) is not None:
    data["gnssrefl_amplitude_min"] = as_float(amplitude_min)
if as_float(orthometric_height) is not None:
    data["gnssrefl_orthometric_height"] = as_float(orthometric_height)
if as_int(refraction_model) is not None:
    data["gnssrefl_refraction_model"] = as_int(refraction_model)
if as_float(max_arc_minutes) is not None:
    data["gnssrefl_max_arc_minutes"] = as_float(max_arc_minutes)
if as_float(elevation_span_tolerance) is not None:
    data["gnssrefl_elevation_span_tolerance"] = as_float(elevation_span_tolerance)
if as_int(direct_signal_poly_order) is not None:
    data["gnssrefl_direct_signal_poly_order"] = as_int(direct_signal_poly_order)
if external_storage_path.strip():
    data["external_storage_path"] = external_storage_path

with open(path, "w") as f:
    json.dump(data, f, indent=4)

print(f"Wrote {path}")
PYEOF
python_exit=$?

echo ""
if [ "$python_exit" -ne 0 ]; then
    echo "!! Something went wrong writing $STATION_JSON -- see the error above."
    echo "!! Nothing was saved. Please try again."
    exit 1
fi

echo "Done. Review the file with: cat $STATION_JSON"
