#!/usr/bin/env python3
"""
elevation_quality.py

Asks whether the current elevation mask has headroom, using the
station's own data rather than satellite imagery.

THE QUESTION

Raising the upper elevation limit admits more satellite arcs, and
since the residuals are pure random noise, more arcs should tighten
the result. But higher elevation means the reflection point sits
CLOSER to the antenna:

    elevation    specular point distance (H = 18.665 m)
        5 deg        213 m
       15 deg         70 m      <- current upper limit
       20 deg         51 m
       25 deg         40 m

If the waterline retreats past that distance at low tide, arcs at
those elevations would sometimes reflect off wet sand or dry beach.
Those retrievals do not fail -- they return a plausible reflector
height that is not a water level, quietly contaminating the record.

THE TEST

Rather than guess where the waterline sits, this measures whether
the data already shows degradation toward the top of the current
window. Each arc's reflector height is compared against the spline
fit at the same moment (the spline being the consensus of all arcs),
and the disagreement is grouped by the arc's mean elevation.

  - Flat across elevation -> the current window is uniformly good,
    and widening it is worth testing.

  - Worse toward 15 deg -> the footprint is already reaching
    marginal ground at the top of the window, and widening would
    make things worse, not better.

The same grouping is applied by azimuth, which answers the
companion question: is the whole 353-173 window equally good, or
are some bearings systematically worse?

Usage:
    python3 elevation_quality.py \\
        --subdaily products/refl_code/Files/usgs/usgs_2026_subdaily_edit.txt \\
        --spline products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --hortho 18.665
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np


def load_subdaily(path: Path, hortho: float):
    """Returns per-arc records: time, water level, mean elevation, azimuth, peak-to-noise."""
    recs = []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            c = line.split()
            if len(c) < 22:
                continue
            try:
                year = int(float(c[0]))
                rh = float(c[2])
                azim = float(c[5])
                emin = float(c[7])
                emax = float(c[8])
                pknoise = float(c[13])
                month, day = int(float(c[17])), int(float(c[18]))
                hh, mm, ss = int(float(c[19])), int(float(c[20])), int(float(c[21]))
            except (ValueError, IndexError):
                continue
            try:
                dt = datetime(year, month, day, hh, mm, ss)
            except ValueError:
                continue
            recs.append({
                "time": dt,
                "wl": hortho - rh,
                "elev": (emin + emax) / 2.0,
                "emax": emax,
                "azim": azim,
                "pknoise": pknoise,
            })
    return recs


def load_spline(path: Path):
    times, values = [], []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            c = line.split()
            if len(c) < 9:
                continue
            try:
                dt = datetime(int(float(c[2])), int(float(c[3])), int(float(c[4])),
                              int(float(c[5])), int(float(c[6])), int(float(c[7])))
                values.append(float(c[8]))
                times.append(dt)
            except (ValueError, IndexError):
                continue
    return times, np.asarray(values, float)


def report_bins(label: str, keys: np.ndarray, resid: np.ndarray,
                edges: list[float], unit: str = "deg") -> None:
    print(f"  {label}")
    print(f"    {'range':>14}  {'n':>6}  {'mean |resid|':>12}")
    stats = []
    for lo, hi in zip(edges, edges[1:]):
        m = (keys >= lo) & (keys < hi)
        if m.sum() < 30:
            continue
        v = float(np.abs(resid[m]).mean())
        stats.append((lo, hi, int(m.sum()), v))
        print(f"    {lo:5.0f}-{hi:<5.0f}{unit}  {m.sum():6d}  {v:10.3f} m")

    if len(stats) >= 2:
        vals = [s[3] for s in stats]
        ratio = max(vals) / min(vals)
        worst = stats[int(np.argmax(vals))]
        if ratio > 1.4:
            print(f"    -> {ratio:.2f}x spread; worst is "
                  f"{worst[0]:.0f}-{worst[1]:.0f}{unit}. Systematic.")
        else:
            print(f"    -> {ratio:.2f}x spread; uniform across the range.")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subdaily", required=True)
    p.add_argument("--spline", required=True)
    p.add_argument("--hortho", type=float, required=True)
    args = p.parse_args()

    recs = load_subdaily(Path(args.subdaily), args.hortho)
    st, sv = load_spline(Path(args.spline))

    if not recs or not st:
        print("Could not load data.")
        return 1

    base = st[0]
    sx = np.array([(t - base).total_seconds() for t in st])
    order = np.argsort(sx)
    sx, sv = sx[order], sv[order]

    qx = np.array([(r["time"] - base).total_seconds() for r in recs])
    spline_at = np.interp(qx, sx, sv, left=np.nan, right=np.nan)

    wl = np.array([r["wl"] for r in recs])
    elev = np.array([r["elev"] for r in recs])
    emax = np.array([r["emax"] for r in recs])
    azim = np.array([r["azim"] for r in recs])
    pkn = np.array([r["pknoise"] for r in recs])

    ok = np.isfinite(spline_at)
    resid = wl[ok] - spline_at[ok]
    elev, emax, azim, pkn = elev[ok], emax[ok], azim[ok], pkn[ok]

    print("=" * 68)
    print("  PER-ARC QUALITY vs GEOMETRY")
    print("=" * 68)
    print(f"  {len(resid)} arcs compared against the spline consensus")
    print(f"  Overall spread: {np.abs(resid).mean():.3f} m mean absolute")
    print()
    print("  Each arc's water level is compared against the spline at the")
    print("  same moment. An arc reflecting off something other than open")
    print("  water should disagree with the consensus more than one that is")
    print("  not.")
    print()

    report_bins("By mean arc elevation (higher = footprint closer to shore):",
                elev, resid, [5, 7, 9, 11, 13, 15.1])

    report_bins("By arc upper elevation (emaxO):",
                emax, resid, [8, 10, 12, 14, 15.1])

    az_edges = [0, 30, 60, 90, 120, 150, 175, 353, 361]
    print("  By azimuth (which direction the reflection came from):")
    print(f"    {'range':>14}  {'n':>6}  {'mean |resid|':>12}")
    stats = []
    for lo, hi in zip(az_edges, az_edges[1:]):
        m = (azim >= lo) & (azim < hi)
        if m.sum() < 30:
            continue
        v = float(np.abs(resid[m]).mean())
        stats.append((lo, hi, v))
        print(f"    {lo:5.0f}-{hi:<5.0f}deg  {m.sum():6d}  {v:10.3f} m")
    if len(stats) >= 2:
        vals = [s[2] for s in stats]
        worst = stats[int(np.argmax(vals))]
        best = stats[int(np.argmin(vals))]
        print(f"    -> best {best[0]:.0f}-{best[1]:.0f} ({best[2]:.3f} m), "
              f"worst {worst[0]:.0f}-{worst[1]:.0f} ({worst[2]:.3f} m)")
    print()

    # Peak-to-noise is gnssrefl's own confidence measure. If it
    # tracks the disagreement, it is a usable quality filter; if not,
    # raising the threshold would discard arcs arbitrarily.
    c = float(np.corrcoef(pkn, np.abs(resid))[0, 1])
    print(f"  Peak-to-noise vs |residual|: r = {c:+.3f}")
    if c < -0.2:
        print("    -> higher peak-to-noise really does mean a better arc;")
        print("       raising gnssrefl_peak2noise would improve quality.")
    else:
        print("    -> peak-to-noise does not predict which arcs disagree,")
        print("       so raising that threshold would discard arcs at random.")
    print()
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
