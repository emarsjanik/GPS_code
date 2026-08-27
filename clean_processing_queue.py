#!/usr/bin/env python3
"""
clean_processing_queue.py

Removes processing_queue entries that can never succeed because the
raw file they name no longer exists -- but only when that day's
results are present, so nothing unprocessed is ever discarded.

WHY THIS IS NEEDED

The queue retries failed entries indefinitely. When a raw file is
deleted after processing (archived, or removed during a cleanup),
its queue entry is left behind and retried on every run, failing
every time with:

    RINEX conversion failed: Raw file does not exist: .../raw/station_YYYYMMDD.um980

On this station six such entries had accumulated 61-71 retries each,
generating 126 of the 168 errors logged in a recent week. That noise
buried a genuine, unrelated failure for days -- the cost is not the
wasted retries, it is that a log nobody can read is a log nobody
reads.

An entry is only removed when BOTH are true:

  - the raw file it names is absent from raw/
  - results exist for the day that file covers

If the file is missing but results are absent, the entry is KEPT and
reported: that combination means data was genuinely lost, which is
worth investigating rather than quietly tidying away.

Note on the day mapping: this station records a UTC day per file but
names files by local date, so station_20260722.um980 contains the
UTC day that begins during 2026-07-22 local. Both candidate
day-of-year values are checked rather than assuming which convention
applies.

DRY RUN by default.

Usage:
    ./clean_processing_queue.py             show what would be removed
    ./clean_processing_queue.py --execute   remove them
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def station_code() -> str:
    path = PROJECT_DIR / "station" / "resources" / "station.json"
    if path.exists():
        try:
            d = json.loads(path.read_text())
            code = (d.get("gnssrefl_station_code")
                    or (d.get("station_id") or "")[:4])
            if code:
                return code.lower()
        except Exception:
            pass
    return "usgs"


def results_exist(doy: int, year: int, code: str) -> bool:
    base = PROJECT_DIR / "products" / "refl_code" / str(year) / "results" / code
    if (base / f"{doy}.txt").exists():
        return True
    # A day with no usable retrievals is legitimately empty, not
    # missing -- recover_missing_days.sh records this explicitly.
    if (base / f"{doy}.no_data").exists():
        return True
    return False


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true",
                   help="actually remove the entries (default is a dry run)")
    args = p.parse_args()

    db_path = PROJECT_DIR / "database" / "station.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    code = station_code()
    db = sqlite3.connect(db_path)

    rows = list(db.execute(
        "SELECT id, filename, retry_count FROM processing_queue WHERE completed = 0"))

    if not rows:
        print("No incomplete queue entries -- nothing to do.")
        return 0

    removable: list[tuple[int, str, int]] = []
    keep_present: list[str] = []
    keep_no_results: list[str] = []

    for entry_id, filename, retries in rows:
        raw_path = PROJECT_DIR / "raw" / filename

        if raw_path.exists():
            keep_present.append(f"{filename} (still awaiting processing)")
            continue

        m = re.search(r"(\d{8})", filename or "")
        if not m:
            keep_no_results.append(f"{filename} (no date in filename)")
            continue

        d = datetime.strptime(m.group(1), "%Y%m%d")
        doy = int(d.strftime("%j"))
        year = d.year

        # Check the file's own day and the next, since a file named
        # for a local date holds the UTC day that starts within it.
        if results_exist(doy, year, code) or results_exist(doy + 1, year, code):
            removable.append((entry_id, filename, retries or 0))
        else:
            keep_no_results.append(
                f"{filename} (file gone AND no results for doy {doy}/{doy+1})")

    print("=" * 66)
    print("  processing_queue cleanup")
    print("=" * 66)
    print()

    if removable:
        total_retries = sum(r for _, _, r in removable)
        verb = "Removing" if args.execute else "Would remove"
        print(f"{verb} {len(removable)} dead entr(ies) "
              f"({total_retries} accumulated retries):")
        for _, filename, retries in removable:
            print(f"    {filename:34} {retries} retries")
        print()

    if keep_present:
        print(f"Keeping {len(keep_present)} entr(ies) whose file still exists:")
        for item in keep_present:
            print(f"    {item}")
        print()

    if keep_no_results:
        print(f"KEEPING {len(keep_no_results)} entr(ies) that need investigation:")
        for item in keep_no_results:
            print(f"    {item}")
        print("    ^ file is gone but no results were produced -- this may be")
        print("      genuinely lost data, so the entry is deliberately kept.")
        print()

    if not removable:
        print("Nothing to remove.")
        return 0

    if args.execute:
        db.executemany("DELETE FROM processing_queue WHERE id = ?",
                       [(i,) for i, _, _ in removable])
        db.commit()
        print(f"Removed {len(removable)} entr(ies).")
        print("Those repeated 'Raw file does not exist' errors should now stop.")
    else:
        print("DRY RUN -- nothing was changed.")
        print("To remove them:  ./clean_processing_queue.py --execute")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
