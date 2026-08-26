#!/usr/bin/env python3
"""
plot_raw_vs_spline.py

Overlays the RAW, unsplined GNSS-IR retrievals (converted directly
to quasi-sea-level = Hortho - RH from each day's results file)
against gnssrefl's own spline-fit curve, on the same axis.

If the raw points show full amplitude in a period where the spline
curve looks damped, this directly confirms the amplitude loss is
happening in the spline-fitting step itself (subdaily/-knots), not
in the underlying GNSS-IR retrievals.

Usage:
    python3 plot_raw_vs_spline.py \\
        --results-dir products/refl_code/2026/results/usgs \\
        --spline-file products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --hortho 18.625 \\
        --doy1 205 --doy2 225 \\
        --year 2026 \\
        --output raw_vs_spline.png
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np


def load_raw_results(results_dir: Path, year: int, doy1: int, doy2: int, hortho: float):
    """
    Parses raw gnssrefl results files directly (confirmed real
    column layout: column index 1 = doy, column index 4 = UTC hour
    as a fraction, column index 2 = RH), converting each row to a
    (datetime, quasi-sea-level) point using the same Hortho - RH
    formula gnssrefl's own spline output file states it uses.
    """
    times, values = [], []
    for doy in range(doy1, doy2 + 1):
        f = results_dir / f"{doy}.txt"
        if not f.exists():
            continue
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            cols = line.split()
            if len(cols) < 5:
                continue
            try:
                row_doy = int(float(cols[1]))
                rh = float(cols[2])
                utc_hour = float(cols[4])
            except (ValueError, IndexError):
                continue

            day = datetime(year, 1, 1) + timedelta(days=row_doy - 1)
            dt = day + timedelta(hours=utc_hour)

            times.append(dt)
            values.append(hortho - rh)

    return times, np.asarray(values, float)


def load_spline_output(path: Path, doy1: int, doy2: int, year: int):
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
    p.add_argument("--results-dir", required=True)
    p.add_argument("--spline-file", required=True)
    p.add_argument("--hortho", type=float, required=True)
    p.add_argument("--doy1", type=int, required=True)
    p.add_argument("--doy2", type=int, required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--output", default="raw_vs_spline.png")
    args = p.parse_args()

    raw_times, raw_values = load_raw_results(
        Path(args.results_dir), args.year, args.doy1, args.doy2, args.hortho
    )
    print(f"Loaded {len(raw_times)} raw retrievals")

    spline_times, spline_values = load_spline_output(
        Path(args.spline_file), args.doy1, args.doy2, args.year
    )
    print(f"Loaded {len(spline_times)} spline points in this window")

    if raw_values.size:
        print(f"Raw retrievals:    min={raw_values.min():.3f}  max={raw_values.max():.3f}  range={raw_values.max()-raw_values.min():.3f}")
    if spline_values.size:
        print(f"Spline curve:      min={spline_values.min():.3f}  max={spline_values.max():.3f}  range={spline_values.max()-spline_values.min():.3f}")

    fig, ax = plt.subplots(figsize=(16, 6))

    ax.scatter(raw_times, raw_values, s=10, color="tab:red", alpha=0.5,
               label=f"Raw GNSS-IR retrievals (n={len(raw_times)})", zorder=3)
    ax.plot(spline_times, spline_values, color="tab:blue", linewidth=1.3,
            label="gnssrefl spline fit", zorder=2)

    ax.set_xlabel("Date")
    ax.set_ylabel("Water level (m)")
    ax.set_title(f"Raw GNSS-IR retrievals vs. spline fit (doy {args.doy1}-{args.doy2})")
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
