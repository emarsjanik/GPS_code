#!/usr/bin/env python3
"""
check_layout.py

USGS GNSS Reference Station

A quick, standalone sanity check for the project's file layout.
Place this file directly in the project root (~/GNSS/v4.1/, as a
sibling of station/ and tests/) and run:

    python3 check_layout.py

It does NOT import any of the station's own modules (so it still
works even if something is placed wrong enough that imports would
fail) -- it only checks that files exist where they're expected, are
non-empty where they're supposed to be implemented, and compile
cleanly as Python.

Exit code is 0 if everything looks right, 1 if there's at least one
problem worth fixing.
"""

from __future__ import annotations

import json
import py_compile
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# Files that should exist in station/ with real content (non-empty,
# and valid Python). The description is shown only when something's
# wrong, for context.
IMPLEMENTED_STATION_FILES = {
    "config.py": "loads/validates station.json, computes all project paths",
    "receiver.py": "serial comms with the UM980, message routing, record_raw()",
    "database.py": "SQLite persistence layer",
    "rinex_processor.py": "convbin wrapper, raw -> RINEX conversion",
    "gnssrefl_processor.py": "gnssrefl wrapper: rinex2snr -> gnssir GNSS-IR pipeline",
    "pipeline.py": "operations manager: scan/queue/convert/process/archive",
    "station_manager.py": "unattended orchestration: chunked daily recording, pipeline, health checks",
    "station.py": "station controller / startup dashboard",
    "exceptions.py": "shared orchestration-level exceptions",
    "version.py": "station software version string",
}

# Files that are known, deliberate stubs (empty or placeholder) as of
# this writing -- not yet implemented. Listed so the script doesn't
# cry wolf about them, but still tells you they're stubs.
KNOWN_STUB_STATION_FILES = {
    "logger.py": "likely unneeded; logging is handled per-module already",
    "__init__.py": "intentionally empty, marks station/ as a package",
}

EXPECTED_TEST_FILES = [
    "test_receiver.py",
    "test_record_raw.py",
    "test_rinex.py",
    "test_gnssrefl_processor.py",
    "test_station_manager.py",
]

REQUIRED_DIRECTORIES = [
    "station",
    "station/resources",
    "tests",
]

REQUIRED_RESOURCE_FILES = [
    "station/resources/station.json",
]


def _human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    return f"{num_bytes / 1024:.1f} KB"


def check_directories() -> list[str]:
    problems = []

    print("Directories")
    print("-" * 60)

    for rel_path in REQUIRED_DIRECTORIES:
        path = PROJECT_ROOT / rel_path
        if path.is_dir():
            print(f"  OK       {rel_path}/")
        else:
            print(f"  MISSING  {rel_path}/  <-- create this directory")
            problems.append(f"Missing directory: {rel_path}/")

    return problems


def check_implemented_files() -> list[str]:
    problems = []

    print()
    print("station/ -- implemented modules")
    print("-" * 60)

    for filename, description in IMPLEMENTED_STATION_FILES.items():
        path = PROJECT_ROOT / "station" / filename

        if not path.exists():
            print(f"  MISSING  station/{filename}")
            print(f"           ({description})")
            problems.append(f"Missing file: station/{filename}")
            continue

        size = path.stat().st_size

        if size == 0:
            print(f"  EMPTY    station/{filename}  (0 bytes)")
            problems.append(
                f"station/{filename} exists but is empty -- upload didn't "
                f"go through, or the wrong (stub) copy was uploaded"
            )
            continue

        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"  BROKEN   station/{filename}  ({_human_size(size)})")
            problems.append(
                f"station/{filename} does not compile as Python: {exc}"
            )
            continue

        print(f"  OK       station/{filename}  ({_human_size(size)})")

    return problems


def check_stub_files() -> None:
    print()
    print("station/ -- known stubs / not-yet-built (informational only)")
    print("-" * 60)

    for filename, note in KNOWN_STUB_STATION_FILES.items():
        path = PROJECT_ROOT / "station" / filename

        if not path.exists():
            print(f"  MISSING  station/{filename}  ({note})")
            continue

        size = path.stat().st_size
        marker = "empty stub" if size == 0 else f"{_human_size(size)}, has content"
        print(f"  --       station/{filename}  ({marker} -- {note})")


def check_test_files() -> list[str]:
    problems = []

    print()
    print("tests/")
    print("-" * 60)

    for filename in EXPECTED_TEST_FILES:
        path = PROJECT_ROOT / "tests" / filename

        if not path.exists():
            print(f"  MISSING  tests/{filename}")
            problems.append(f"Missing file: tests/{filename}")
            continue

        size = path.stat().st_size

        if size == 0:
            print(f"  EMPTY    tests/{filename}  (0 bytes)")
            problems.append(f"tests/{filename} exists but is empty")
            continue

        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"  BROKEN   tests/{filename}  ({_human_size(size)})")
            problems.append(f"tests/{filename} does not compile as Python: {exc}")
            continue

        print(f"  OK       tests/{filename}  ({_human_size(size)})")

    return problems


def check_resources() -> list[str]:
    problems = []

    print()
    print("Resources")
    print("-" * 60)

    for rel_path in REQUIRED_RESOURCE_FILES:
        path = PROJECT_ROOT / rel_path

        if not path.exists():
            print(f"  MISSING  {rel_path}")
            problems.append(f"Missing file: {rel_path}")
            continue

        try:
            with open(path) as handle:
                data = json.load(handle)
            required_keys = ["station_id", "receiver_port", "receiver_baud"]
            missing_keys = [k for k in required_keys if k not in data]
            if missing_keys:
                print(f"  WARNING  {rel_path}  (missing keys: {missing_keys})")
                problems.append(
                    f"{rel_path} is missing expected key(s): {missing_keys}"
                )
            else:
                print(f"  OK       {rel_path}")
        except json.JSONDecodeError as exc:
            print(f"  BROKEN   {rel_path}  (invalid JSON: {exc})")
            problems.append(f"{rel_path} is not valid JSON: {exc}")

    return problems


def check_project_root_alignment() -> list[str]:
    """
    Confirms station/config.py's own path-computation logic would
    land on THIS directory as the project root -- i.e. that
    station/ is a direct child of wherever this script sits. This is
    exactly the class of bug that once caused the database to be
    created at station/database/station.db instead of
    database/station.db.
    """

    problems = []

    print()
    print("Project root alignment")
    print("-" * 60)

    config_path = PROJECT_ROOT / "station" / "config.py"

    if not config_path.exists():
        print("  SKIPPED  (station/config.py not found; checked above)")
        return problems

    # Mirrors config.py's own computation: Path(__file__).resolve().parent.parent
    computed_root = config_path.resolve().parent.parent

    if computed_root == PROJECT_ROOT:
        print("  OK       station/config.py would compute project_root as:")
        print(f"           {computed_root}")
    else:
        print("  MISMATCH station/config.py would compute project_root as:")
        print(f"           {computed_root}")
        print("           but this script is running from:")
        print(f"           {PROJECT_ROOT}")
        problems.append(
            "station/ is not a direct child of the expected project root -- "
            "database/raw/rinex/logs/etc. would be created in the wrong place"
        )

    return problems


def main() -> int:
    print("=" * 60)
    print("USGS GNSS Reference Station -- Layout Check")
    print(f"Project root: {PROJECT_ROOT}")
    print("=" * 60)

    all_problems: list[str] = []

    all_problems += check_directories()
    all_problems += check_implemented_files()
    check_stub_files()  # informational only, not counted as problems
    all_problems += check_test_files()
    all_problems += check_resources()
    all_problems += check_project_root_alignment()

    print()
    print("=" * 60)

    if not all_problems:
        print("ALL CHECKS PASSED")
        print("=" * 60)
        return 0

    print(f"{len(all_problems)} PROBLEM(S) FOUND")
    print("=" * 60)
    for problem in all_problems:
        print(f"  - {problem}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
