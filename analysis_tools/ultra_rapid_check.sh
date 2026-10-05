#!/bin/bash
#
# ultra_rapid_check.sh -- run ultra_rapid_check.py in the gnssrefl venv.
#
# A CHECK only: same-day GNSS-IR with ultra-rapid orbits, written to
# products/refl_code_ultra and compared with the production results
# once those exist. Production processing is not touched.
#
#   ./analysis_tools/ultra_rapid_check.sh             run once, then compare
#   ./analysis_tools/ultra_rapid_check.sh --compare   compare only
#
# Optional crontab line (every 3 hours, log in logs/ultra_rapid_check.log):
#   15 */3 * * * /home/argus_user/GNSS/v4.1/analysis_tools/ultra_rapid_check.sh >> /home/argus_user/GNSS/v4.1/logs/ultra_rapid_check.log 2>&1

set -u
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/gnssrefl_venv"
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "gnssrefl venv not found at $VENV_DIR" >&2
    exit 1
fi
source "$VENV_DIR/bin/activate"
cd "$PROJECT_DIR" || exit 1
echo "=== ultra_rapid_check $(date -u '+%Y-%m-%d %H:%M')Z ==="
exec python3 "$PROJECT_DIR/analysis_tools/ultra_rapid_check.py" "$@"
