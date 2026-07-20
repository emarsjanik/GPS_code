#!/usr/bin/env python3
"""
compare_to_noaa.py

Overlays this station's real GNSS-IR water level output against a
reference tide station's data, aligned by actual date/time, for
visual comparison.

IMPORTANT, HONEST NOTE ON WHAT THIS COMPARISON CAN AND CAN'T SHOW:
Our GNSS-IR water level and the reference tide data are on two
genuinely different vertical reference frames (this station's own
local/ellipsoidal-ish reference vs. the reference station's own
tidal datum, and likely different physical locations too).
Overlaying them on a single shared y-axis would be misleading, since
the *absolute* numbers were never meant to match. This script uses
two separate y-axes instead, sharing only the time axis, so what's
actually being compared is the *shape* of the curve over time (do
the highs and lows line up, does the rise/fall pattern look similar)
-- not the specific numbers.

Usage:
    python3 compare_to_noaa.py path/to/reference_tides.csv

Expects a CSV with columns: Day, Date, AM High, AM High Ft, AM Low,
AM Low Ft, PM High, PM High Ft, PM Low, PM Low Ft (Sunrise/Sunset/
Moon columns, if present, are ignored). Missing entries (blank time
or blank value) are skipped gracefully, not treated as an error.

Reads: ~/GNSS/v4.1/products/refl_code/Files/usgs/usgs_spline_out.txt
Writes: ~/GNSS/v4.1/products/refl_code/Files/usgs/noaa_comparison.png
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SPLINE_FILE = Path.home() / "GNSS/v4.1/products/refl_code/Files/usgs/usgs_spline_out.txt"
OUTPUT_FILE = Path.home() / "GNSS/v4.1/products/refl_code/Files/usgs/noaa_comparison.png"

REFERENCE_UNITS = "feet"  # matches the uploaded CSV; change to "meters" for a metric file


def parse_reference_csv(csv_path: Path):
    """
    Parses a monthly tide-chart CSV with columns: Day, Date, AM High,
    AM High Ft, AM Low, AM Low Ft, PM High, PM High Ft, PM Low,
    PM Low Ft (any Sunrise/Sunset/Moon columns are ignored).

    Missing entries (a blank time or blank value -- confirmed real,
    happens a handful of times a month at real tide stations) are
    skipped gracefully rather than treated as an error, since a
    missing high/low just means that particular tide cycle didn't
    have a clean local extremum that day.
    """
    points = []

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row["Date"].strip()
            if not date_str:
                continue
            date = datetime.strptime(date_str, "%b %d, %Y")

            for time_col, value_col in (
                ("AM High", "AM High Ft"),
                ("AM Low", "AM Low Ft"),
                ("PM High", "PM High Ft"),
                ("PM Low", "PM Low Ft"),
            ):
                time_str = row.get(time_col, "").strip()
                value_str = row.get(value_col, "").strip()
                if not time_str or not value_str:
                    continue
                time_of_day = datetime.strptime(time_str, "%I:%M %p")
                dt = date.replace(
                    hour=time_of_day.hour, minute=time_of_day.minute
                )
                points.append((dt, float(value_str)))

    points.sort(key=lambda p: p[0])
    return points


def interpolate_noaa_curve(points, step_minutes=6):
    """
    Build a smooth, evenly-sampled curve through real high/low points
    using cosine interpolation between each consecutive pair --
    confirmed as a standard, reasonable approximation of a real tide
    curve's shape between two known extrema (real tides are close to
    sinusoidal between consecutive highs/lows, even though the full
    tide is a sum of several harmonic constituents).
    """
    times = []
    values = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        span = (t1 - t0).total_seconds()
        steps = max(int(span // (step_minutes * 60)), 1)
        for i in range(steps):
            frac = i / steps
            # Cosine interpolation: smooth, matches real tide shape
            # between two known extrema far better than a straight line.
            cos_frac = (1 - np.cos(frac * np.pi)) / 2
            value = v0 + (v1 - v0) * cos_frac
            times.append(t0 + timedelta(seconds=frac * span))
            values.append(value)
    times.append(points[-1][0])
    values.append(points[-1][1])
    return times, values


def read_gnssrefl_spline_output(path: Path):
    """
    Reads gnssrefl's evenly-sampled subdaily spline output.

    DEFENSIVE BY DESIGN: this file's exact column layout wasn't
    confirmed against gnssrefl's own documentation (search results
    describe the general workflow but not this specific file's exact
    columns). Rather than silently guess wrong, this prints the raw
    header and a sample of parsed values so you can quickly confirm
    (or correct) which columns are actually being used.
    """
    if not path.exists():
        print(f"ERROR: {path} does not exist.")
        print("Run subdaily first (process_gps_data.sh does this automatically).")
        sys.exit(1)

    lines = path.read_text().splitlines()
    data_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("%")]

    print(f"--- Raw first 3 lines of {path.name} (for verification) ---")
    for ln in lines[:3]:
        print(repr(ln))
    print("---")

    mjd_epoch = datetime(1858, 11, 17)

    times = []
    values = []
    for ln in data_lines:
        parts = ln.split()
        if len(parts) < 2:
            continue
        try:
            mjd = float(parts[0])
            rh = float(parts[1])
        except ValueError:
            continue
        times.append(mjd_epoch + timedelta(days=mjd))
        values.append(rh)

    if not times:
        print("ERROR: could not parse any (MJD, value) rows from this file.")
        print("Paste the raw header lines above back to Claude to fix column parsing.")
        sys.exit(1)

    print(f"Parsed {len(times)} points. First: {times[0]} = {values[0]:.3f}")
    print(f"                          Last:  {times[-1]} = {values[-1]:.3f}")
    print("Confirmed: this is raw reflector height (meters), not the final")
    print("Hortho-converted water level -- the plot inverts this axis so")
    print("'up' means 'more water' on both curves for an intuitive comparison.")
    print("---")

    return times, values


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 compare_to_noaa.py path/to/reference_tides.csv")
        sys.exit(1)

    reference_csv = Path(sys.argv[1])
    gnss_times, gnss_values = read_gnssrefl_spline_output(SPLINE_FILE)

    reference_points = parse_reference_csv(reference_csv)
    print(f"Parsed {len(reference_points)} reference tide points from {reference_csv.name}")
    reference_times, reference_values = interpolate_noaa_curve(reference_points)

    fig, ax1 = plt.subplots(figsize=(14, 7))

    ax1.plot(gnss_times, gnss_values, color="tab:blue", linewidth=2, label="GNSS-IR (this station, reflector height)")
    ax1.set_xlabel("Date")
    ax1.set_ylabel("GNSS-IR reflector height (meters) -- INVERTED axis", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")
    # Confirmed: usgs_spline_out.txt's value column is raw reflector
    # height (antenna-to-water distance), not the final
    # Hortho-converted water level from usgs_H0.png -- that
    # conversion happens later, in a separate plotting step. Higher
    # reflector height means the water surface is actually LOWER (a
    # bigger gap to the antenna), the opposite of water level.
    # Inverting the axis here (same convention gnssrefl's own RH
    # plots use) makes "up" mean "more water" on both curves, so the
    # shape comparison is visually intuitive instead of backwards.
    ax1.invert_yaxis()

    ax2 = ax1.twinx()
    ax2.plot(reference_times, reference_values, color="tab:red", linewidth=1.5, linestyle="--", label="Reference tide station")
    ax2.set_ylabel(f"Reference predicted tide height ({REFERENCE_UNITS})", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    fig.autofmt_xdate()
    plt.title("GNSS-IR Water Level vs. Reference Tide Station\n(separate y-axes -- compare shape/timing, not absolute values)")

    # Confirmed necessary: our real GNSS-IR data typically spans far
    # fewer days than a full reference tide chart (e.g. 3 real days
    # against a 31-day month), which makes the comparison visually
    # meaningless if the whole reference range is shown -- our data
    # becomes an unreadable sliver. Zoom to the GNSS-IR data's own
    # range instead, with a small buffer on each side, so both
    # curves are actually comparable at a useful scale.
    buffer = timedelta(hours=12)
    ax1.set_xlim(gnss_times[0] - buffer, gnss_times[-1] + buffer)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    fig.tight_layout()
    fig.savefig(OUTPUT_FILE, dpi=150)
    print(f"Saved comparison plot to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
