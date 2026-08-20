#!/usr/bin/env python3
"""
Marconi extended-duration GNSS-R production-candidate run.

Purpose
-------
Run the already validated, NON-PRODUCTION experimental configuration:

    RH       = 17-23 m
    GPS L1   = frequency 1
    elevation= 5-13 deg
    current azimuth configuration from ocean17_23_l1_e5_13/usgs.json

over every available SNR day in the tide-model period.

This script DOES NOT modify the production usgs.json.

It:
  1. Verifies the experimental JSON.
  2. Scans the local SNR directory for available DOYs.
  3. Limits processing to the tide workbook coverage:
         DOY 196 through 243 (2026-07-15 through 2026-08-31)
  4. Runs gnssir for every available day.
  5. Verifies each result file.
  6. Prints a final processing inventory.

The next analysis can then use all available repeated tracks rather than
only DOY 204-207.

Expected result directory:
  products/refl_code/2026/results/usgs/ocean17_23_l1_e5_13/

No data are deleted.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
from pathlib import Path


YEAR = 2026
STATION = "usgs"

REFL_CODE = Path(
    "products/refl_code"
)

CONFIG = (
    REFL_CODE
    / "input"
    / STATION
    / "ocean17_23_l1_e5_13"
    / "usgs.json"
)

SNR_DIR = (
    REFL_CODE
    / str(YEAR)
    / "snr"
    / STATION
)

RESULT_DIR = (
    REFL_CODE
    / str(YEAR)
    / "results"
    / STATION
    / "ocean17_23_l1_e5_13"
)

LOG_DIR = (
    REFL_CODE
    / "logs"
    / STATION
    / "ocean17_23_l1_e5_13"
    / str(YEAR)
)

# Marconi tide workbook:
# 2026-07-15 through 2026-08-31
DOY_MIN = 196
DOY_MAX = 243


def discover_snr_doys():
    """
    Recognize normal gnssrefl SNR filenames:
        usgsDDD0.26.snr66.gz
    """
    pattern = re.compile(
        rf"^{STATION}(\d{{3}})0\."
        rf"{str(YEAR)[2:]}"
        rf"\.snr66(?:\.gz)?$"
    )

    doys = set()

    if not SNR_DIR.exists():
        raise SystemExit(
            f"SNR directory not found:\n{SNR_DIR}"
        )

    for p in SNR_DIR.iterdir():
        m = pattern.match(p.name)
        if not m:
            continue

        doy = int(m.group(1))

        if DOY_MIN <= doy <= DOY_MAX:
            doys.add(doy)

    return sorted(doys)


def verify_config():
    if not CONFIG.exists():
        raise SystemExit(
            f"Experimental JSON not found:\n{CONFIG}"
        )

    with open(CONFIG) as f:
        d = json.load(f)

    expected = {
        "minH": 17.0,
        "maxH": 23.0,
        "NReg": [17.0, 23.0],
        "e1": 5.0,
        "e2": 13.0,
        "freqs": [1],
        "reqAmp": [5.0],
    }

    print("=" * 96)
    print("VERIFYING EXPERIMENTAL GNSS-IR CONFIGURATION")
    print("=" * 96)

    problems = []

    for key, want in expected.items():
        got = d.get(key)
        print(
            f"{key:10s} = {got!r}"
        )

        if got != want:
            problems.append(
                f"{key}: expected {want!r}, got {got!r}"
            )

    if problems:
        print()
        print("CONFIGURATION ERROR:")
        for p in problems:
            print(" ", p)
        raise SystemExit(
            "Refusing to run until experimental configuration is correct."
        )

    print()
    print(
        "EXPERIMENTAL CONFIGURATION VERIFIED."
    )
    print(
        "Production usgs.json is NOT modified."
    )


def run_day(doy):
    cmd = [
        "gnssir",
        STATION,
        str(YEAR),
        str(doy),
        "-extension",
        "ocean17_23_l1_e5_13",
        "-fr",
        "1",
        "-nooverwrite",
        "False",
    ]

    print()
    print("=" * 96)
    print(f"RUNNING DOY {doy}")
    print("=" * 96)
    print(
        "$",
        " ".join(cmd),
    )

    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )

    print(result.stdout)

    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        return False, (
            f"gnssir return code "
            f"{result.returncode}"
        )

    result_file = (
        RESULT_DIR
        / f"{doy}.txt"
    )

    if not result_file.exists():
        return False, (
            "result file not created"
        )

    return True, (
        f"{result_file}"
    )


def main():
    verify_config()

    RESULT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 96)
    print(
        "MARCONI EXTENDED-DURATION GNSS-R RUN"
    )
    print("=" * 96)
    print(
        "Date range: 2026-07-15 through 2026-08-31"
    )
    print(
        "DOY range:",
        DOY_MIN,
        "through",
        DOY_MAX,
    )
    print(
        "Experimental extension:",
        "ocean17_23_l1_e5_13",
    )

    doys = discover_snr_doys()

    print()
    print(
        "AVAILABLE SNR DOYs:"
    )

    if not doys:
        raise SystemExit(
            "No SNR files found in the target date range."
        )

    print(
        " ",
        ", ".join(
            str(x)
            for x in doys
        ),
    )

    missing = [
        d for d in range(
            DOY_MIN,
            DOY_MAX + 1,
        )
        if d not in doys
    ]

    if missing:
        print()
        print(
            "DOYs in tide period with NO local SNR file:"
        )
        print(
            " ",
            ", ".join(
                str(x)
                for x in missing
            ),
        )

    print()
    print(
        f"Days to process: {len(doys)}"
    )

    successes = []
    failures = []

    for doy in doys:
        ok, msg = run_day(doy)

        if ok:
            successes.append(doy)
            print(
                f"VERIFIED DOY {doy}: {msg}"
            )
        else:
            failures.append(
                (doy, msg)
            )
            print(
                f"FAILED DOY {doy}: {msg}"
            )

    print()
    print("=" * 96)
    print(
        "FINAL PROCESSING INVENTORY"
    )
    print("=" * 96)

    print(
        f"SNR days available : {len(doys)}"
    )
    print(
        f"Result days created : {len(successes)}"
    )
    print(
        f"Failed days         : {len(failures)}"
    )

    print()
    print(
        "Successful DOYs:"
    )
    print(
        " ",
        ", ".join(
            str(x)
            for x in successes
        )
    )

    if failures:
        print()
        print(
            "Failures:"
        )
        for doy, msg in failures:
            print(
                f"  DOY {doy}: {msg}"
            )

    print()
    print(
        "RESULT DIRECTORY:"
    )
    print(
        f"  {RESULT_DIR.resolve()}"
    )

    print()
    print(
        "NEXT STEP:"
    )
    print(
        "Use the extended result set for repeated-track analysis."
    )

    print()
    print(
        "DONE"
    )


if __name__ == "__main__":
    main()
