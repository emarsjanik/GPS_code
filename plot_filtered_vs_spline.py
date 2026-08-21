#!/usr/bin/env python3
"""
plot_filtered_vs_spline.py

Corrected version: overlays the FILTERED GNSS-IR retrievals (from
gnssrefl's own usgs_2026_subdaily_edit.txt, i.e. the same set the
spline was actually fit to, AFTER gnssrefl's own 2.5-sigma daily
outlier removal) against the spline curve.

An earlier version of this comparison read from the raw, unfiltered
per-day results/<doy>.txt files instead, which produced dense
clusters of points sitting outside the spline curve -- confirmed to
be genuine, real outliers that gnssrefl's own subdaily step had
already, correctly excluded before ever fitting the spline, not a
plotting bug or a spline problem. Comparing the spline against a
dataset it wasn't actually fit to was not a fair comparison; this
version fixes that.

Confirmed real column layout of usgs_2026_subdaily_edit.txt (from
its own header comment): year, doy, RH, sat, UTCtime, Azim, Amp,
eminO, emaxO, NumbOf, freq, rise, EdotF, PkNoise, DelT, MJD, refr,
MM, DD, HH, MM, SS (0-indexed: RH is column 2, and the real calendar
month/day/hour/minute/second are columns 17-21).

Usage:
    python3 plot_filtered_vs_spline.py \\
        --filtered-file products/refl_code/Files/usgs/usgs_2026_subdaily_edit.txt \\
        --spline-file products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --hortho 18.625 \\
        --doy1 205 --doy2 225 \\
        --output filtered_vs_spline.png
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np


def load_filtered_results(path: Path, hortho: float, doy1: int, doy2: int):
    times, values = [], []
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            cols = line.split()
            if len(cols) < 22:
                continue
            try:
                doy = int(float(cols[1]))
                rh = float(cols[2])
                year = int(float(cols[0]))
                month = int(float(cols[17]))
                day = int(float(cols[18]))
                hour = int(float(cols[19]))
                minute = int(float(cols[20]))
                second = int(float(cols[21]))
            except (ValueError, IndexError):
                continue

            if not (doy1 <= doy <= doy2):
                continue

            try:
                dt = datetime(year, month, day, hour, minute, second)
            except ValueError:
                continue

            times.append(dt)
            values.append(hortho - rh)

    return times, np.asarray(values, float)


def load_spline_output(path: Path, doy1: int, doy2: int, year: int):
    from datetime import timedelta

    times, values = [], []
    window_start = datetime(year, 1, 1) + timedelta(days=doy1 - 1)
    window_end = datetime(year, 1, 1) + timedelta(days=doy2)

    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            cols = line.split()
            if len(cols) < 9:
                continue
            try:
                yr = int(float(cols[2]))
                month = int(float(cols[3]))
                day = int(float(cols[4]))
                hour = int(float(cols[5]))
                minute = int(float(cols[6]))
                second = int(float(cols[7]))
                water_level = float(cols[8])
            except (ValueError, IndexError):
                continue
            try:
                dt = datetime(yr, month, day, hour, minute, second)
            except ValueError:
                continue
            if window_start <= dt <= window_end:
                times.append(dt)
                values.append(water_level)

    return times, np.asarray(values, float)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--filtered-file", required=True)
    p.add_argument("--spline-file", required=True)
    p.add_argument("--hortho", type=float, required=True)
    p.add_argument("--doy1", type=int, required=True)
    p.add_argument("--doy2", type=int, required=True)
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--output", default="filtered_vs_spline.png")
    args = p.parse_args()

    filt_times, filt_values = load_filtered_results(
        Path(args.filtered_file), args.hortho, args.doy1, args.doy2
    )
    print(f"Loaded {len(filt_times)} FILTERED retrievals (post outlier-removal)")

    spline_times, spline_values = load_spline_output(
        Path(args.spline_file), args.doy1, args.doy2, args.year
    )
    print(f"Loaded {len(spline_times)} spline points in this window")

    if filt_values.size:
        print(f"Filtered retrievals: min={filt_values.min():.3f}  max={filt_values.max():.3f}  range={filt_values.max()-filt_values.min():.3f}")
    if spline_values.size:
        print(f"Spline curve:        min={spline_values.min():.3f}  max={spline_values.max():.3f}  range={spline_values.max()-spline_values.min():.3f}")

    fig, ax = plt.subplots(figsize=(16, 6))

    ax.scatter(filt_times, filt_values, s=10, color="tab:green", alpha=0.5,
               label=f"Filtered GNSS-IR retrievals (n={len(filt_times)})", zorder=3)
    ax.plot(spline_times, spline_values, color="tab:blue", linewidth=1.3,
            label="gnssrefl spline fit", zorder=2)

    ax.set_xlabel("Date")
    ax.set_ylabel("Water level (m)")
    ax.set_title(f"Filtered GNSS-IR retrievals (post outlier-removal) vs. spline fit (doy {args.doy1}-{args.doy2})")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(args.output, dpi=150)
    print(f"Plot saved to: {args.output}")


if __name__ == "__main__":
    main()
