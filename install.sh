#!/bin/bash
#
# install.sh
#
# GNSS-IR Reference Station -- Master Installer
#
# One-command setup for a brand-new machine, with no prior GNSS-IR
# or Python experience required. Checks and installs every system
# dependency this project needs, sets up an isolated Python virtual
# environment, creates the project's directory structure, and then
# hands off to setup_station.sh to configure your specific station.
#
# What this installs, and why:
#
#   python3, python3-venv, python3-pip
#       Runs everything else. Ubuntu ships an old, non-isolated
#       Python by default; python3-venv lets us create a clean,
#       project-local environment that can't conflict with anything
#       else on your system.
#
#   git, build-essential (gcc/make)
#       Needed to download and compile RTKLIB from source (below).
#
#   RTKLIB's convbin (built from source, NOT from apt)
#       Converts the receiver's raw binary output into standard
#       RINEX files gnssrefl can read. Confirmed necessary to build
#       this from source rather than use Ubuntu's own "rtklib"
#       package: that package is the original RTKLIB, not the
#       rtklibexplorer fork this project depends on for native
#       Unicore/UM980 receiver support -- using the wrong one would
#       silently produce broken or empty RINEX files with no error.
#       As of mid-2025, rtklibexplorer retired the old "demo5"
#       branch name; all of that work is now on the repository's
#       default branch, which is what this script builds.
#
#   gnssrefl (via pip, inside the venv)
#       The actual GNSS-IR analysis engine this whole project is
#       built around.
#
# Usage:
#   ./install.sh
#
# Safe to re-run: every step checks whether it's already done before
# doing it again, so re-running this after a partial/interrupted
# install (or just to check everything is still in order) is fine.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/gnssrefl_venv"
RTKLIB_SRC_DIR="$PROJECT_DIR/.rtklib_src"
LOCAL_BIN_DIR="$HOME/.local/bin"

# ----------------------------------------------------------------
# Small, reusable UI helpers -- kept dependency-free (no external
# progress-bar libraries) so this script works on a totally bare
# machine, before anything else has been installed yet.
# ----------------------------------------------------------------

_BAR="================================================================"

section() {
    echo ""
    echo "$_BAR"
    echo "  $1"
    echo "$_BAR"
}

ok()   { echo "  [OK]   $1"; }
info() { echo "  [..]   $1"; }
warn() { echo "  [!!]   $1"; }
fail() { echo "  [FAIL] $1"; }

confirm() {
    # Simple yes/no prompt. Returns 0 for yes, 1 for anything else.
    local prompt="$1"
    local answer
    read -rp "$prompt [y/N]: " answer
    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# A minimal spinner for steps with no incremental output of their
# own (e.g. compiling), so the terminal never looks frozen. Runs the
# given command in the background and animates while it's alive.
run_with_spinner() {
    local message="$1"
    shift

    "$@" > /tmp/install_step_output.$$ 2>&1 &
    local pid=$!

    local frames='|/-\'
    local i=0

    printf "  [..]   %s " "$message"
    while kill -0 "$pid" 2>/dev/null; do
        i=$(( (i + 1) % 4 ))
        printf "\r  [%s]   %s " "${frames:$i:1}" "$message"
        sleep 0.2
    done

    wait "$pid"
    local exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        printf "\r  [OK]   %s\n" "$message"
    else
        printf "\r  [FAIL] %s\n" "$message"
        echo "  ---- output ----"
        cat /tmp/install_step_output.$$
        echo "  -----------------"
    fi

    rm -f /tmp/install_step_output.$$
    return "$exit_code"
}

# ----------------------------------------------------------------
# Step 0: sanity checks
# ----------------------------------------------------------------

section "GNSS-IR Reference Station -- Installer"

echo "This will check and install everything needed to run this"
echo "project on this machine: system packages, a Python virtual"
echo "environment, RTKLIB's convbin (built from source), and"
echo "gnssrefl. It will ask before making any system-level changes"
echo "that need administrator (sudo) access."
echo ""
echo "Project directory: $PROJECT_DIR"

if [ "$EUID" -eq 0 ]; then
    warn "Running as root. This is not required and not recommended --"
    warn "run this as your normal user; it will ask for sudo only for"
    warn "the specific steps that need it."
fi

OS_NAME="unknown"
if [ -f /etc/os-release ]; then
    OS_NAME=$(. /etc/os-release && echo "$ID")
fi

if [ "$OS_NAME" != "ubuntu" ] && [ "$OS_NAME" != "debian" ]; then
    warn "This installer is written for Ubuntu/Debian (apt-based)"
    warn "systems. Detected: $OS_NAME. The apt-based steps below will"
    warn "likely fail; you may need to install the equivalent packages"
    warn "for your system by hand (see the comments in this script for"
    warn "exactly what's needed and why)."
fi

# ----------------------------------------------------------------
# Step 1: system packages (apt)
# ----------------------------------------------------------------

section "Step 1: System packages"

REQUIRED_APT_PACKAGES=(python3 python3-venv python3-pip git build-essential)
MISSING_APT_PACKAGES=()

for pkg in "${REQUIRED_APT_PACKAGES[@]}"; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        ok "$pkg already installed"
    else
        MISSING_APT_PACKAGES+=("$pkg")
    fi
done

if [ "${#MISSING_APT_PACKAGES[@]}" -gt 0 ]; then
    echo ""
    echo "  The following system packages are missing:"
    echo "    ${MISSING_APT_PACKAGES[*]}"
    echo ""
    if confirm "  Install them now with sudo apt-get install?"; then
        sudo apt-get update
        if sudo apt-get install -y "${MISSING_APT_PACKAGES[@]}"; then
            ok "System packages installed"
        else
            fail "apt-get install failed -- see the output above."
            echo "  You can install these by hand and re-run this script:"
            echo "    sudo apt-get install ${MISSING_APT_PACKAGES[*]}"
            exit 1
        fi
    else
        fail "Cannot continue without these packages. Install them by hand:"
        echo "    sudo apt-get install ${MISSING_APT_PACKAGES[*]}"
        exit 1
    fi
fi

# ----------------------------------------------------------------
# Step 2: RTKLIB's convbin, built from source
# ----------------------------------------------------------------

section "Step 2: RTKLIB (convbin)"

if command -v convbin >/dev/null 2>&1; then
    ok "convbin already on PATH: $(command -v convbin)"
elif [ -x "$LOCAL_BIN_DIR/convbin" ]; then
    ok "convbin already installed at $LOCAL_BIN_DIR/convbin"
    case ":$PATH:" in
        *":$LOCAL_BIN_DIR:"*) : ;;
        *)
            warn "$LOCAL_BIN_DIR is not on your PATH yet."
            echo "  Add this line to ~/.bashrc (then restart your terminal, or"
            echo "  run it directly in this shell to continue now):"
            echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
            export PATH="$LOCAL_BIN_DIR:$PATH"
            ;;
    esac
else
    echo "  convbin was not found. This project depends on the"
    echo "  rtklibexplorer fork of RTKLIB specifically (for native"
    echo "  Unicore/UM980 support) -- NOT the 'rtklib' package"
    echo "  available via apt, which is a different, incompatible"
    echo "  version. Building this from source, which is the only"
    echo "  reliable way to get the correct version:"
    echo ""
    echo "    1. Clone https://github.com/rtklibexplorer/RTKLIB"
    echo "    2. Build convbin (app/consapp/convbin/gcc, make)"
    echo "    3. Install the resulting binary to $LOCAL_BIN_DIR"
    echo ""

    if confirm "  Proceed with building convbin from source now?"; then
        mkdir -p "$LOCAL_BIN_DIR"

        if [ -d "$RTKLIB_SRC_DIR" ]; then
            info "Existing source checkout found at $RTKLIB_SRC_DIR; updating it"
            run_with_spinner "Updating RTKLIB source" \
                git -C "$RTKLIB_SRC_DIR" pull --ff-only
        else
            run_with_spinner "Cloning RTKLIB (rtklibexplorer fork)" \
                git clone https://github.com/rtklibexplorer/RTKLIB.git "$RTKLIB_SRC_DIR"
        fi

        if [ ! -d "$RTKLIB_SRC_DIR/app/consapp/convbin/gcc" ]; then
            fail "Expected build directory not found in the cloned source"
            fail "(app/consapp/convbin/gcc). The repository layout may have"
            fail "changed -- check https://github.com/rtklibexplorer/RTKLIB"
            fail "by hand."
            exit 1
        fi

        if run_with_spinner "Compiling convbin" \
            make -C "$RTKLIB_SRC_DIR/app/consapp/convbin/gcc"; then

            cp "$RTKLIB_SRC_DIR/app/consapp/convbin/gcc/convbin" "$LOCAL_BIN_DIR/convbin"
            chmod +x "$LOCAL_BIN_DIR/convbin"
            ok "convbin installed to $LOCAL_BIN_DIR/convbin"

            case ":$PATH:" in
                *":$LOCAL_BIN_DIR:"*) : ;;
                *)
                    export PATH="$LOCAL_BIN_DIR:$PATH"
                    warn "Added $LOCAL_BIN_DIR to PATH for this session only."
                    echo "  Add this line to ~/.bashrc to make it permanent:"
                    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
                    ;;
            esac
        else
            fail "convbin build failed -- see the output above."
            echo "  Common cause: missing build-essential (should have been"
            echo "  installed in Step 1). Try building by hand for a more"
            echo "  detailed error:"
            echo "    make -C $RTKLIB_SRC_DIR/app/consapp/convbin/gcc"
            exit 1
        fi
    else
        fail "Cannot continue without convbin. RINEX conversion will not work."
        exit 1
    fi
fi

# ----------------------------------------------------------------
# Step 3: Python virtual environment + gnssrefl
# ----------------------------------------------------------------

section "Step 3: Python environment and gnssrefl"

if [ -f "$VENV_DIR/bin/activate" ]; then
    ok "Virtual environment already exists at $VENV_DIR"
else
    if run_with_spinner "Creating virtual environment" \
        python3 -m venv "$VENV_DIR"; then
        ok "Virtual environment created at $VENV_DIR"
    else
        fail "Could not create virtual environment."
        exit 1
    fi
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if python3 -c "import gnssrefl" >/dev/null 2>&1; then
    installed_version=$(python3 -c "import importlib.metadata; print(importlib.metadata.version('gnssrefl'))" 2>/dev/null)
    ok "gnssrefl already installed (version ${installed_version:-unknown})"
else
    run_with_spinner "Upgrading pip" \
        pip install --upgrade pip --quiet

    if run_with_spinner "Installing gnssrefl (this can take a few minutes)" \
        pip install gnssrefl --quiet; then
        ok "gnssrefl installed"
    else
        fail "gnssrefl installation failed -- see the output above."
        exit 1
    fi
fi

# This project's own Python dependencies beyond gnssrefl itself.
OTHER_PACKAGES=(pyserial simplekml openpyxl numpy)
MISSING_PY_PACKAGES=()

for pkg_spec in "${OTHER_PACKAGES[@]}"; do
    # pyserial's importable module name is "serial", not "pyserial".
    import_name="$pkg_spec"
    [ "$pkg_spec" = "pyserial" ] && import_name="serial"

    if python3 -c "import $import_name" >/dev/null 2>&1; then
        ok "$pkg_spec already installed"
    else
        MISSING_PY_PACKAGES+=("$pkg_spec")
    fi
done

if [ "${#MISSING_PY_PACKAGES[@]}" -gt 0 ]; then
    if run_with_spinner "Installing ${MISSING_PY_PACKAGES[*]}" \
        pip install "${MISSING_PY_PACKAGES[@]}" --quiet; then
        ok "Additional Python packages installed"
    else
        fail "Could not install: ${MISSING_PY_PACKAGES[*]}"
        exit 1
    fi
fi

# ----------------------------------------------------------------
# Step 4: project directory structure
# ----------------------------------------------------------------

section "Step 4: Project directories"

REQUIRED_DIRS=(raw rinex archive products logs database reports)

for dir in "${REQUIRED_DIRS[@]}"; do
    full_path="$PROJECT_DIR/$dir"
    if [ -d "$full_path" ]; then
        ok "$dir/ already exists"
    else
        mkdir -p "$full_path"
        ok "$dir/ created"
    fi
done

# ----------------------------------------------------------------
# Step 5: hand off to the station configuration wizard
# ----------------------------------------------------------------

section "Step 5: Station configuration"

STATION_JSON="$PROJECT_DIR/station/resources/station.json"

if [ -f "$STATION_JSON" ]; then
    ok "station.json already exists at $STATION_JSON"
    echo ""
    if confirm "  Re-run the configuration wizard anyway (e.g. to change settings)?"; then
        bash "$PROJECT_DIR/setup_station.sh"
    else
        info "Keeping existing configuration."
    fi
else
    echo "  No station.json found yet. This is where you'll enter your"
    echo "  station's identity, receiver settings, and location --"
    echo "  every field is explained as you go. See also"
    echo "  STATION_JSON_REFERENCE.md for a complete, standalone"
    echo "  reference to every setting."
    echo ""
    bash "$PROJECT_DIR/setup_station.sh"
fi

# ----------------------------------------------------------------
# Done
# ----------------------------------------------------------------

section "Installation complete"

echo "Next steps:"
echo "  1. Verify everything is working:"
echo "       ./test_installation.sh"
echo "  2. Once you have raw data (or want to process what you have):"
echo "       ./process_and_plot.sh"
echo ""
echo "See QUICKSTART.md for a full, start-to-finish walkthrough."
