#!/usr/bin/env python3

"""
Marconi GNSS-IR reflector-height reconnaissance matrix.

IMPORTANT:
This is an isolated reconnaissance experiment.
It does NOT modify station.json or production settings.

Purpose:
Test whether the newly discovered ~11 m reflector-height candidates
persist when the GNSS-IR search window is widened.

Tests:
    RH 8-25 m, elevation 5-13°
    RH 8-25 m, elevation 5-15°
    RH 10-20 m, elevation 5-13°
    RH 10-20 m, elevation 5-15°

Frequency:
    GPS L1 only (freq=1)

Days:
    DOY 204-207, 2026

Outputs:
    ~/GNSS/v4.1/marconi_quicklook_matrix/

Each strategy gets its own log file.
"""

from pathlib import Path
import subprocess


YEAR = 2026
DOYS = [204, 205, 206, 207]

MATRIX = [
    {
        "label": "rh8_25_e5_13",
        "h1": 8,
        "h2": 25,
        "e1": 5,
        "e2": 13,
    },
    {
        "label": "rh8_25_e5_15",
        "h1": 8,
        "h2": 25,
        "e1": 5,
        "e2": 15,
    },
    {
        "label": "rh10_20_e5_13",
        "h1": 10,
        "h2": 20,
        "e1": 5,
        "e2": 13,
    },
    {
        "label": "rh10_20_e5_15",
        "h1": 10,
        "h2": 20,
        "e1": 5,
        "e2": 15,
    },
]

OUT = Path("marconi_quicklook_matrix")
OUT.mkdir(
    exist_ok=True
)

print()
print("=" * 88)
print("MARCONI GNSS-IR REFLECTOR-HEIGHT RECONNAISSANCE")
print("=" * 88)
print()
print("Production configuration is NOT being modified.")
print("Frequency: GPS L1 only")
print("DOY:", DOYS)
print()

for item in MATRIX:

    label = item["label"]
    logfile = OUT / f"{label}.log"

    print()
    print("=" * 88)
    print(f"STRATEGY: {label}")
    print("=" * 88)

    with open(
        logfile,
        "w",
    ) as log:

        log.write(
            "=" * 88 + "\n"
        )
        log.write(
            f"STRATEGY: {label}\n"
        )
        log.write(
            "=" * 88 + "\n"
        )

        for doy in DOYS:

            cmd = [
                "quickLook",
                "usgs",
                str(YEAR),
                str(doy),

                "-fr",
                "1",

                "-h1",
                str(item["h1"]),

                "-h2",
                str(item["h2"]),

                "-e1",
                str(item["e1"]),

                "-e2",
                str(item["e2"]),

                "-plt",
                "False",
            ]

            print()
            print(
                "$",
                " ".join(cmd)
            )

            log.write(
                "\n"
                + "-" * 88
                + "\n"
            )

            log.write(
                f"DOY {doy}\n"
            )

            log.write(
                "$ "
                + " ".join(cmd)
                + "\n"
            )

            try:

                result = subprocess.run(
                    cmd,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                output = (
                    result.stdout
                    + "\n"
                    + result.stderr
                )

                log.write(output)

                print(output)

                if result.returncode != 0:

                    print(
                        f"RETURN CODE: "
                        f"{result.returncode}"
                    )

            except Exception as exc:

                message = (
                    f"ERROR executing quickLook "
                    f"for DOY {doy}: {exc}\n"
                )

                log.write(message)
                print(message)

print()
print("=" * 88)
print("DONE")
print("=" * 88)
print()
print(
    "Logs written to:"
)
print(
    OUT.resolve()
)
print()

for p in sorted(
    OUT.glob("*.log")
):
    print(
        " ",
        p
    )
