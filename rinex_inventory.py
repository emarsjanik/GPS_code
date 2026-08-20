#!/usr/bin/env python3
"""
rinex_inventory.py

Scans the entire filesystem for RINEX observation files (any location,
not just the standard gnssrefl staging directory), builds a complete
inventory of which days have data and where, and identifies:

  1. Genuinely missing days -- no RINEX file anywhere on the system
     for that day, within the range you specify.
  2. Days with RINEX data that hasn't been converted to SNR yet
     (candidates for processing).
  3. Duplicate copies of the same day scattered across multiple
     locations (useful to know before consolidating/cleaning up).

RINEX 3 long-format filenames are used to identify station and day,
e.g.:
    USGS00USA_R_20262080000_01D_01S_MO.rnx
              ^^^^^^^ this is year (2026) + day-of-year (208)

Usage:
    python3 rinex_inventory.py --station usgs --year 2026 \\
        --doy-min 189 --doy-max 243

Searches the whole filesystem by default (slow but thorough, ~seconds
on a typical NUC). Narrow the search root with --search-root if you
know RINEX only lives under one directory tree.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path


def find_rinex_files(search_root, station_code_prefix):
    """
    Finds every .rnx file under search_root whose filename matches the
    RINEX 3 long-format convention for the given station prefix
    (case-insensitive), e.g. station_code_prefix='USGS' matches
    'USGS00USA_R_...'.
    """
    pattern = re.compile(
        rf"^{re.escape(station_code_prefix)}\d*[A-Z]*_R_(\d{{4}})(\d{{3}})\d{{4}}_01D_01S_MO\.rnx$",
        re.IGNORECASE,
    )

    matches = []
    for path in Path(search_root).rglob("*.rnx"):
        m = pattern.match(path.name)
        if m:
            year = int(m.group(1))
            doy = int(m.group(2))
            matches.append((year, doy, path))

    return matches


def find_snr_days(refl_code_dir, station, year):
    snr_dir = refl_code_dir / str(year) / "snr" / station
    if not snr_dir.exists():
        return set()

    yy = str(year)[2:]
    pattern = re.compile(rf"^{re.escape(station)}(\d{{3}})0\.{yy}\.snr66(?:\.gz)?$")

    doys = set()
    for p in snr_dir.iterdir():
        m = pattern.match(p.name)
        if m:
            doys.add(int(m.group(1)))
    return doys


def main():
    parser = argparse.ArgumentParser(description="RINEX file inventory and gap report.")
    parser.add_argument("--station", default="usgs", help="Station code, lowercase (default: usgs)")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--doy-min", type=int, default=1, help="Start of expected range")
    parser.add_argument("--doy-max", type=int, default=366, help="End of expected range")
    parser.add_argument("--search-root", default="/", help="Where to search for RINEX files (default: whole filesystem)")
    parser.add_argument("--refl-code", default=None, help="Path to REFL_CODE dir (default: $REFL_CODE env var)")
    args = parser.parse_args()

    import os
    refl_code_dir = Path(args.refl_code or os.environ.get("REFL_CODE") or "products/refl_code")

    station_prefix = args.station.upper()

    print()
    print("=" * 90)
    print("RINEX INVENTORY AND GAP REPORT")
    print("=" * 90)
    print(f"Station           : {args.station} (matching prefix: {station_prefix})")
    print(f"Year              : {args.year}")
    print(f"Expected DOY range: {args.doy_min}-{args.doy_max}")
    print(f"Searching under   : {args.search_root}")
    print()
    print("Scanning filesystem for .rnx files (this may take a minute)...")

    matches = find_rinex_files(args.search_root, station_prefix)
    matches = [m for m in matches if m[0] == args.year]

    print(f"Found {len(matches)} matching RINEX file(s) for {args.year}.")

    # Group by DOY -> list of paths (to catch duplicates across locations)
    by_doy = defaultdict(list)
    for year, doy, path in matches:
        by_doy[doy].append(path)

    doys_with_rinex = sorted(by_doy.keys())
    snr_doys = find_snr_days(refl_code_dir, args.station, args.year)

    expected_doys = list(range(args.doy_min, args.doy_max + 1))

    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"Days with RINEX found anywhere : {len(doys_with_rinex)}")
    print(f"Days with SNR already generated: {len(snr_doys)}")

    missing_entirely = [d for d in expected_doys if d not in by_doy]
    rinex_not_yet_snr = [d for d in doys_with_rinex if d not in snr_doys]
    duplicated = {d: paths for d, paths in by_doy.items() if len(paths) > 1}

    print()
    print("=" * 90)
    print(f"DAYS WITH NO RINEX FOUND ANYWHERE ({len(missing_entirely)} day(s))")
    print("=" * 90)
    if missing_entirely:
        print(", ".join(str(d) for d in missing_entirely))
    else:
        print("None -- every expected day has RINEX data somewhere.")

    print()
    print("=" * 90)
    print(f"DAYS WITH RINEX BUT NOT YET CONVERTED TO SNR ({len(rinex_not_yet_snr)} day(s))")
    print("=" * 90)
    if rinex_not_yet_snr:
        print(", ".join(str(d) for d in rinex_not_yet_snr))
        print()
        print("These are candidates for conversion with rinex2snr before")
        print("they can be processed with gnssir.")
    else:
        print("None -- every day with RINEX already has SNR generated.")

    print()
    print("=" * 90)
    print(f"DAYS WITH RINEX COPIES IN MULTIPLE LOCATIONS ({len(duplicated)} day(s))")
    print("=" * 90)
    if duplicated:
        for d in sorted(duplicated.keys()):
            print(f"  DOY {d}:")
            for p in duplicated[d]:
                print(f"    {p}")
    else:
        print("None -- every day with RINEX has exactly one copy found.")

    print()
    print("=" * 90)
    print("FULL PER-DAY DETAIL")
    print("=" * 90)
    print(f"{'DOY':>4}  {'RINEX?':>7}  {'SNR?':>5}  Locations")
    for d in expected_doys:
        has_rinex = "yes" if d in by_doy else "NO"
        has_snr = "yes" if d in snr_doys else "no"
        locations = ", ".join(str(p) for p in by_doy.get(d, [])) if d in by_doy else ""
        flag = " <-- MISSING" if d not in by_doy else ""
        print(f"{d:>4}  {has_rinex:>7}  {has_snr:>5}  {locations}{flag}")

    print()
    print("DONE")


if __name__ == "__main__":
    main()
