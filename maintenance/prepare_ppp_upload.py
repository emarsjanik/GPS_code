#!/usr/bin/env python3
"""
prepare_ppp_upload.py

Prepares a RINEX file for upload to a free positioning service, so
you can find out exactly where your antenna is.

WHY THIS EXISTS

docs/GETTING_A_POSITION.md tells you to find the RINEX file and
upload it. That skips over two things which are not obvious:

  The file is too big. This station records at 1 Hz, so a day is
  about 386 MB. Static PPP does not need that -- 30-second sampling
  gives the same position -- and 386 MB is an awkward upload over a
  field connection. Decimating first brings it to roughly 13 MB.

  The day has to be complete. A partial day gives a worse position,
  and it is easy to grab today's file by mistake while it is still
  being written.

This picks yesterday's complete day, decimates it, and tells you
where to send it.

WHAT IT DOES NOT DO

It does not modify the original. The archive stays as it is; this
writes a separate upload file.

It does not decide anything about your antenna. The header names
AS-ANT3B-BUDSUR-L1L2, which is not in the NGS calibration database,
so a PPP service will report the antenna as Unknown and give you the
height of the antenna phase centre. For GNSS-IR that is the height
you want anyway -- reflector height is measured from the phase
centre down to the water -- but see GETTING_A_POSITION.md before
entering it anywhere.

Usage:
    python3 maintenance/prepare_ppp_upload.py
    python3 maintenance/prepare_ppp_upload.py --date 2026-09-09
    python3 maintenance/prepare_ppp_upload.py --interval 15
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
PROJECT_DIR = _here.parent if _here.name == "maintenance" else _here

RINEX_DIR = PROJECT_DIR / "rinex"

PPP_SERVICES = [
    ("CSRS-PPP (worldwide, free account)",
     "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php"),
    ("OPUS (United States, reports NAVD88)",
     "https://geodesy.noaa.gov/OPUS/"),
    ("AUSPOS (worldwide, no account)",
     "https://gnss.ga.gov.au/auspos"),
]


def find_obs(day: date, workdir: Path) -> Path | None:
    """Locates the observation file for a day, unpacking the day's
    archive into workdir if the loose file is gone."""
    stem = f"station_{day:%Y%m%d}"

    loose = RINEX_DIR / f"{stem}.obs"
    if loose.is_file():
        print(f"  Using {loose.relative_to(PROJECT_DIR)}")
        return loose

    archive = RINEX_DIR / f"{stem}.tar.gz"
    if archive.is_file():
        print(f"  Unpacking {archive.relative_to(PROJECT_DIR)} ...")
        r = subprocess.run(["tar", "-xzf", str(archive), "-C", str(workdir)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  Could not unpack: {r.stderr.strip()}")
            return None
        unpacked = workdir / f"{stem}.obs"
        return unpacked if unpacked.is_file() else None

    return None


def decimate(src: Path, dest: Path, interval: int) -> tuple[int, int]:
    """Writes src to dest keeping only epochs on the interval.

    RINEX 3 groups an epoch header (a line starting with '>') with
    the satellite observation lines that follow it. Both are kept or
    both are dropped -- keeping observations whose header was
    discarded would corrupt the file in a way most software does not
    report cleanly.
    """
    epoch_re = re.compile(
        r"^>\s+(\d{4})\s+(\d{2})\s+(\d{2})\s+(\d{2})\s+(\d{2})\s+([\d.]+)")

    kept = total = 0
    keeping = True          # header lines, before the first epoch

    with open(src, errors="replace") as fin, open(dest, "w") as fout:
        for line in fin:
            if line.startswith(">"):
                m = epoch_re.match(line)
                total += 1
                if m:
                    seconds = int(float(m.group(6)))
                    minutes = int(m.group(5))
                    keeping = (minutes * 60 + seconds) % interval == 0
                else:
                    # An epoch line that does not parse is kept
                    # rather than silently dropped.
                    keeping = True
                if keeping:
                    kept += 1
                    fout.write(line)
                continue

            if keeping:
                fout.write(line)

    return kept, total


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", default=None,
                   help="YYYY-MM-DD; defaults to yesterday, the most recent "
                        "complete day")
    p.add_argument("--interval", type=int, default=30,
                   help="seconds between epochs in the prepared file "
                        "(default 30, which is standard for static PPP)")
    p.add_argument("--output-dir", default=None,
                   help="where to write the prepared file (default: the "
                        "project directory)")
    args = p.parse_args()

    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Could not read '{args.date}' as a date (want YYYY-MM-DD).")
            return 1
        if day >= date.today():
            print(f"{day} is not finished yet. A partial day gives a worse")
            print("position; wait until tomorrow, or pick an earlier date.")
            return 1
    else:
        day = date.today() - timedelta(days=1)

    out_dir = Path(args.output_dir) if args.output_dir else PROJECT_DIR
    out_path = out_dir / f"ppp_upload_{day:%Y%m%d}.obs"

    print("=" * 66)
    print(f"  Preparing {day} for upload to a positioning service")
    print("=" * 66)
    print()

    if not RINEX_DIR.is_dir():
        print(f"  No rinex/ directory at {RINEX_DIR}")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="ppp_prep_"))
    try:
        src = find_obs(day, workdir)
        if src is None:
            print(f"  No observation file found for {day}.")
            print()
            available = sorted(
                set(re.findall(r"station_(\d{8})",
                               " ".join(p.name for p in RINEX_DIR.iterdir()))))
            if available:
                print("  Days present in rinex/:")
                print(f"    {available[0][:4]}-{available[0][4:6]}-{available[0][6:]}"
                      f"  to  "
                      f"{available[-1][:4]}-{available[-1][4:6]}-{available[-1][6:]}")
                print()
                print("  Pick one with --date YYYY-MM-DD.")
            return 1

        size_before = src.stat().st_size
        print(f"  Source: {size_before / 1e6:.0f} MB")
        print(f"  Decimating to one epoch every {args.interval} s ...")

        out_dir.mkdir(parents=True, exist_ok=True)
        kept, total = decimate(src, out_path, args.interval)

        size_after = out_path.stat().st_size

        print()
        print(f"  {total} epochs in, {kept} kept "
              f"({100 * kept / total:.1f}%)" if total else "  no epochs found")
        print(f"  {size_before / 1e6:.0f} MB -> {size_after / 1e6:.1f} MB")

        if kept < 100:
            print()
            print("  WARNING: very few epochs were kept. Check the interval,")
            print("  and that the source file covers a full day.")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("=" * 66)
    print("  Ready to upload")
    print("=" * 66)
    print()
    print(f"  {out_path}")
    print()
    print("  Upload it to any one of these:")
    for name, url in PPP_SERVICES:
        print(f"    {name}")
        print(f"      {url}")
    print()
    print("  Results come back by email, usually within an hour.")
    print()
    print("  Then see docs/GETTING_A_POSITION.md for which numbers from")
    print("  the report go into station.json -- the report gives two")
    print("  different heights and they are not interchangeable.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

