#!/bin/bash
#
# test_installation.sh
#
# GNSS-IR Reference Station -- Installation Verification
#
# Checks that everything this project needs is actually working:
# the Python environment, every required external tool, the unit
# test suite, the project's file layout, and (if a receiver is
# connected) basic communication with it.
#
# Meant to be run any time you want a clear, beginner-friendly
# answer to "is my setup actually working?" -- after first
# installing, after changing configuration, or any time something
# seems wrong and you want to rule out the basics before digging
# deeper.
#
# Unlike running the underlying tools directly (python -m unittest,
# check_layout.py, ...), this summarizes each one into a single
# pass/fail line with a short, plain-language explanation of what
# went wrong and what to do about it -- the full, detailed output
# from any failing step is saved to a log file so you (or someone
# helping you) can see exactly what happened without it flooding
# your terminal by default.
#
# Usage:
#   ./test_installation.sh

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/gnssrefl_venv"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/test_installation_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"

_BAR="================================================================"

section() {
    echo ""
    echo "$_BAR"
    echo "  $1"
    echo "$_BAR"
}

pass_count=0
fail_count=0
warn_count=0

report_pass() { echo "  [PASS] $1"; pass_count=$((pass_count + 1)); }
report_fail() { echo "  [FAIL] $1"; fail_count=$((fail_count + 1)); }
report_warn() { echo "  [WARN] $1"; warn_count=$((warn_count + 1)); }

# Runs a command, logging its full output to LOG_FILE, and returns
# its exit code without spilling that output to the terminal unless
# it's explicitly asked for (see the failure branches below, which
# tail the log for a quick hint rather than dumping everything).
run_logged() {
    local label="$1"
    shift

    {
        echo ""
        echo "### $label ###"
        echo "\$ $*"
    } >> "$LOG_FILE"

    "$@" >> "$LOG_FILE" 2>&1
    return $?
}

echo "$_BAR"
echo "  GNSS-IR Reference Station -- Installation Verification"
echo "$_BAR"
echo ""
echo "Full detailed output is being saved to:"
echo "  $LOG_FILE"

# ----------------------------------------------------------------
# 1. Virtual environment
# ----------------------------------------------------------------

section "1. Python environment"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    report_fail "Virtual environment not found at $VENV_DIR"
    echo "         Run ./install.sh first."
else
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    report_pass "Virtual environment found and activated"
fi

# ----------------------------------------------------------------
# 2. Required tools and packages
# ----------------------------------------------------------------

section "2. Required tools and packages"

if command -v convbin >/dev/null 2>&1; then
    report_pass "convbin found ($(command -v convbin))"
else
    report_fail "convbin not found on PATH"
    echo "         RINEX conversion will not work. Run ./install.sh,"
    echo "         or add its install location to your PATH if you"
    echo "         already built it."
fi

if python3 -c "import gnssrefl" >/dev/null 2>&1; then
    gnssrefl_version=$(python3 -c "import importlib.metadata; print(importlib.metadata.version('gnssrefl'))" 2>/dev/null || echo "unknown")
    report_pass "gnssrefl importable (version $gnssrefl_version)"
else
    report_fail "gnssrefl is not importable"
    echo "         Run ./install.sh, or 'pip install gnssrefl' inside"
    echo "         the activated virtual environment."
fi

for module_spec in "serial:pyserial" "simplekml:simplekml" "openpyxl:openpyxl" "numpy:numpy"; do
    import_name="${module_spec%%:*}"
    package_name="${module_spec##*:}"
    if python3 -c "import $import_name" >/dev/null 2>&1; then
        report_pass "$package_name importable"
    else
        report_fail "$package_name is not importable"
        echo "         Run ./install.sh, or 'pip install $package_name'"
        echo "         inside the activated virtual environment."
    fi
done

# ----------------------------------------------------------------
# 3. Station configuration
# ----------------------------------------------------------------

section "3. Station configuration"

STATION_JSON="$PROJECT_DIR/station/resources/station.json"

if [ ! -f "$STATION_JSON" ]; then
    report_fail "station.json not found at $STATION_JSON"
    echo "         Run ./install.sh or ./setup_station.sh to create it."
else
    if python3 -c "import json; json.load(open('$STATION_JSON'))" >/dev/null 2>&1; then
        report_pass "station.json exists and is valid JSON"

        # Consolidated configuration check. Emits one "STATUS message"
        # line per finding; FAIL is used for anything that would
        # silently produce confident-looking but physically
        # meaningless results, rather than an error the user would
        # notice on their own.
        _check_station_config=$(python3 - "$STATION_JSON" <<'PYCHECK'
import json
import sys

path = sys.argv[1]
try:
    d = json.load(open(path))
except Exception as exc:
    print(f"FAIL Could not read station.json: {exc}")
    sys.exit(0)

PLACEHOLDERS = {"CHANGEME", "", None}

station_id = d.get("station_id")
if station_id in PLACEHOLDERS:
    print("FAIL station_id is not set (still a placeholder). Run ./setup_station.sh")
else:
    print(f"PASS station_id: {station_id}")

for field in ("latitude", "longitude"):
    v = d.get(field)
    if v is None:
        print(f"FAIL {field} is not set. GNSS-IR cannot produce meaningful "
              f"results without your station's real coordinates. Run ./setup_station.sh")
    elif isinstance(v, (int, float)) and float(v) == 0.0:
        print(f"FAIL {field} is 0.0, which is a placeholder rather than a real "
              f"position (0,0 is open ocean off West Africa). Run ./setup_station.sh")
    else:
        print(f"PASS {field}: {v}")

height = d.get("height")
if height is None:
    print("FAIL height is not set. This must be your antenna's ELLIPSOIDAL "
          "(WGS84) height in meters, not orthometric/MSL. Run ./setup_station.sh")
else:
    print(f"PASS height: {height} m (ellipsoidal)")

rh_min = d.get("gnssrefl_reflector_height_min")
rh_max = d.get("gnssrefl_reflector_height_max")
if rh_min is None or rh_max is None:
    print("FAIL gnssrefl_reflector_height_min/max are not set. Without these, "
          "gnssrefl searches its own default range (roughly 0.5-8m), which only "
          "finds a surface within a few meters BELOW the antenna -- wrong for any "
          "antenna mounted higher than that, and it fails silently. "
          "Run ./setup_station.sh, or see STATION_JSON_REFERENCE.md")
else:
    print(f"PASS reflector height search range: {rh_min}-{rh_max} m")
PYCHECK
)

        while IFS= read -r finding; do
            [ -z "$finding" ] && continue
            status="${finding%% *}"
            message="${finding#* }"
            case "$status" in
                PASS) report_pass "$message" ;;
                WARN) report_warn "$message" ;;
                FAIL) report_fail "$message" ;;
            esac
        done <<< "$_check_station_config"

        # Confirmed, real gap this check exists to catch: a real
        # multi-hour investigation was needed to discover that this
        # station's RH search range (gnssrefl_reflector_height_min/
        # max) was set to a value appropriate for an antenna sitting
        # right at the water's edge, while gnssrefl_orthometric_height
        # (the antenna's real, surveyed height above sea level) was
        # 18.665m -- the antenna sat on a bluff. RH is the distance
        # from the antenna DOWN to the reflecting surface, so a tall
        # antenna needs an RH range centered near its own height
        # above the target surface, not near zero. Getting this
        # wrong doesn't produce an error -- it silently returns
        # confident-looking, physically meaningless results (in this
        # real case, azimuth-independent noise that only looked like
        # a signal). This check catches the same class of mistake
        # before hours are spent debugging its downstream symptoms.
        rh_check=$(python3 -c "
import json
d = json.load(open('$STATION_JSON'))
hortho = d.get('gnssrefl_orthometric_height')
rh_min = d.get('gnssrefl_reflector_height_min')
rh_max = d.get('gnssrefl_reflector_height_max')

if hortho is None:
    print('SKIP no Hortho configured -- this check only applies when gnssrefl_orthometric_height is set')
elif rh_min is None or rh_max is None:
    print(f'WARN gnssrefl_orthometric_height is set ({hortho}m) but gnssrefl_reflector_height_min/max are not -- gnssrefl.s own default RH search range (roughly 0.5-8m) is very unlikely to be correct for an antenna this high above its target surface. Set both explicitly, centered near {hortho}m.')
else:
    hortho = float(hortho)
    rh_min = float(rh_min)
    rh_max = float(rh_max)
    midpoint = (rh_min + rh_max) / 2.0
    diff = abs(midpoint - hortho)
    threshold = max(3.0, 0.3 * abs(hortho))
    if diff > threshold:
        print(f'WARN RH range ({rh_min}-{rh_max}m, midpoint {midpoint:.2f}m) looks inconsistent with gnssrefl_orthometric_height ({hortho}m) -- they differ by {diff:.2f}m. If your antenna truly sits about {hortho}m above the target surface, RH should be centered near that value (e.g. roughly {hortho-3:.1f}-{hortho+3:.1f}m for a few meters of expected surface movement), not near {midpoint:.2f}m. This exact mismatch (RH range near the wrong height) previously cost hours to diagnose on this project as azimuth-independent, physically meaningless retrievals -- see STATION_JSON_REFERENCE.md.')
    else:
        print(f'OK RH range ({rh_min}-{rh_max}m) is consistent with gnssrefl_orthometric_height ({hortho}m)')
")
        rh_status="${rh_check%% *}"
        rh_message="${rh_check#* }"

        case "$rh_status" in
            OK)
                report_pass "$rh_message"
                ;;
            WARN)
                report_warn "RH range / Hortho consistency:"
                echo "         $rh_message"
                ;;
            SKIP)
                : # not applicable to this station, no output needed
                ;;
        esac
    else
        report_fail "station.json exists but is not valid JSON"
        echo "         Re-run ./setup_station.sh to regenerate it, or fix"
        echo "         the syntax error by hand."
    fi
fi

# ----------------------------------------------------------------
# 4. Project layout
# ----------------------------------------------------------------

section "4. Project layout"

if [ -f "$PROJECT_DIR/check_layout.py" ]; then
    if run_logged "check_layout.py" python3 "$PROJECT_DIR/check_layout.py"; then
        report_pass "Project layout check passed"
    else
        report_fail "Project layout check found a problem"
        echo "         See the tail of the log for details:"
        tail -15 "$LOG_FILE" | sed 's/^/         /'
    fi
else
    report_warn "check_layout.py not found -- skipping this check"
fi

# ----------------------------------------------------------------
# 5. Unit test suite
# ----------------------------------------------------------------

section "5. Unit test suite"

if [ -d "$PROJECT_DIR/tests" ]; then
    echo "  Running the full test suite -- this normally takes under a"
    echo "  minute. Full output is being saved to the log; only a summary"
    echo "  is shown here."
    echo ""

    test_output=$(cd "$PROJECT_DIR" && python3 -m unittest discover -s tests -v 2>&1)
    echo "$test_output" >> "$LOG_FILE"

    tests_run_line=$(echo "$test_output" | grep -E "^Ran [0-9]+ test" | tail -1)

    if echo "$test_output" | grep -q "^OK$"; then
        report_pass "$tests_run_line -- all passed"
    else
        failure_count=$(echo "$test_output" | grep -cE "^(FAIL|ERROR):")
        report_fail "$tests_run_line -- $failure_count failure(s)/error(s)"
        echo ""
        echo "  Failing tests:"
        echo "$test_output" | grep -E "^(FAIL|ERROR):" | sed 's/^/    /'
        echo ""
        echo "  Full detail is in the log file listed at the top of this"
        echo "  output."
    fi
else
    report_warn "tests/ directory not found -- skipping the test suite"
fi

# ----------------------------------------------------------------
# 6. Receiver connectivity (optional -- does not fail the overall
#    check if no receiver is connected, since this script is also
#    meant to be run before hardware is set up at all)
# ----------------------------------------------------------------

section "6. Receiver connectivity (optional)"

RECEIVER_PORT="/dev/USB_GPS"
if [ -f "$STATION_JSON" ]; then
    configured_port=$(python3 -c "import json; print(json.load(open('$STATION_JSON')).get('receiver_port', ''))" 2>/dev/null)
    [ -n "$configured_port" ] && RECEIVER_PORT="$configured_port"
fi

if [ ! -e "$RECEIVER_PORT" ]; then
    report_warn "No device found at $RECEIVER_PORT"
    echo "         This is expected if the receiver isn't connected yet,"
    echo "         or if you haven't set up the udev rule that creates"
    echo "         this symlink. Not treated as a failure -- the software"
    echo "         install itself is still fully verified without it."
else
    report_pass "Device found at $RECEIVER_PORT"

    if [ -f "$PROJECT_DIR/station/receiver.py" ]; then
        echo "  Attempting to query the receiver's version (VERSIONA)..."
        echo "  (If station_manager.py is already running continuously in"
        echo "  the background -- the normal, healthy state for a deployed"
        echo "  station -- it holds this port most of the time, so a"
        echo "  warning below can simply mean this check caught it between"
        echo "  recording chunks, not a real problem. Check with:"
        echo "    ps aux | grep station_manager.py)"

        # Confirmed necessary: receiver.py logs its own retry
        # warnings (e.g. a harmless first-attempt timeout before a
        # successful retry -- normal, expected behavior, not an
        # error) via Python's logging module, which writes to
        # stderr. Capturing stdout and stderr together here would
        # mix that log noise into the actual result, making a
        # genuine, healthy pass display a confusing "failed" message.
        # stdout (the script's own print() result) and stderr
        # (receiver.py's logging) are kept separate: stdout is
        # parsed for the actual result, stderr goes only to the log
        # file for anyone who wants the detail.
        query_stdout=$(cd "$PROJECT_DIR/station" && timeout 10 python3 -c "
from receiver import Receiver, ReceiverError
try:
    with Receiver(device='$RECEIVER_PORT') as rx:
        v = rx.version()
        print('OK', v.model, v.firmware)
except ReceiverError as e:
    print('ERROR', e)
" 2>>"$LOG_FILE")

        echo "$query_stdout" >> "$LOG_FILE"

        # The script's own print() output is always the LAST line
        # of stdout (everything before it, if anything, would be
        # unexpected and is not assumed to be the result).
        result_line=$(echo "$query_stdout" | tail -1)

        if echo "$result_line" | grep -q "^OK"; then
            model_info=$(echo "$result_line" | sed 's/^OK //')
            report_pass "Receiver responded: $model_info"
        else
            report_warn "Device exists but did not respond as expected"
            echo "         (see log for details -- this can happen if the"
            echo "         receiver is mid-boot, or another program has the"
            echo "         port open)"
        fi
    fi
fi

# ----------------------------------------------------------------
# Summary
# ----------------------------------------------------------------

section "Summary"

echo "  Passed:   $pass_count"
echo "  Warnings: $warn_count"
echo "  Failed:   $fail_count"
echo ""

if [ "$fail_count" -eq 0 ]; then
    echo "  Everything that can be checked without hardware or real data"
    echo "  connected is working correctly."
    if [ "$warn_count" -gt 0 ]; then
        echo "  There are some warnings above worth a look, but nothing"
        echo "  blocking you from proceeding."
    fi
    echo ""
    echo "  Next step: ./process_and_plot.sh"
    exit 0
else
    echo "  $fail_count check(s) failed -- see the details above and in:"
    echo "    $LOG_FILE"
    echo ""
    echo "  Fix these before proceeding; each failure above includes a"
    echo "  suggested next step."
    exit 1
fi
