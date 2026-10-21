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

    elevation    specular point distance (antenna ~18.7 m above the water)
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

WHICH REFLECTOR HEIGHT (review B18)

The spline is fitted to reflector heights with two corrections
applied: the RHdot correction (the bias a moving surface puts on an
arc's RH: the spline's slope dRH/dt times the arc's tan(e)/(de/dt),
EdotF in column 13; Larson, Ray, Nievinski & Freymueller 2013, "The
accidental tide gauge", IEEE GRSL 10(5)) and the inter-frequency
biases (one constant per frequency, relative to GPS L1). An arc's
raw RH compared with that spline disagrees by those corrections too:
RHdot is largest at mid-tide and changes sign between rising and
setting arcs, and the biases differ by frequency, whose mix differs
between bearings (each constellation's tracks lie at its own
azimuths). So the raw comparison mixes the geometry this test is
after with corrections that have nothing to do with the footprint.

gnssrefl 4.1.5's subdaily writes three per-arc files:
  <sta>_<year>_subdaily_edit.txt              raw RH only (column 3)
  ..._subdaily_edit.txt.withrhdot             + RH with RHdot (column 23)
  ..._subdaily_edit.txt.withrhdotIF           + RH with RHdot and IF bias (column 25)
The last two are written by the nightly fit in Files/<sta>/fit/,
named for the years it spans (gnss_record.py), and they hold only
the arcs the fit kept: gnssrefl drops every arc more than 3 sigma
from its spline before writing .withrhdot (rhdot_correction2), and
again before writing .withrhdotIF. The arcs this test exists to find
(wet sand or beach at the top of the window, a bad bearing) are the
ones those edits remove, so a report on either file alone is censored
by the quantity it measures and looks cleaner than the data is.

So give the plain edit file, which has every arc. Each arc is joined
to its row of the .withrhdotIF file found next to it or in its fit/
folder (on year, day, UTC time, satellite and frequency) and takes
that row's corrected RH. Every other arc is corrected here the same
way: the RHdot term is EdotF (column 13) times dRH/dt from the
spline being compared, and the IF bias is the fit's own for that
frequency (the median of column 23 minus column 25 over its rows).
On the fit's own arcs this gives its column 25 to about 0.04 m rms
(a synthetic record run through gnssrefl 4.1.5; the slope is the
spline's, not that of the fit's first spline). Each table then also counts, per bin, the arcs the fit's edits
dropped. The RH line says how many arcs took the fit's RH, how many
were corrected here, and the span the fit's file covers. Given a
.withrhdot or .withrhdotIF file itself, its corrected column is read
and a WARNING says the arcs the fit dropped are missing. --raw-rh
compares the raw RH instead, as this script did before.

Usage:
    python3 elevation_quality.py \\
        --subdaily products/refl_code/Files/usgs/usgs_2026_subdaily_edit.txt \\
        --spline products/refl_code/Files/usgs/usgs_spline_out.txt
"""

from __future__ import annotations

import argparse
from collections import Counter
import math
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from station_datum import navd88_hortho, spline_shift  # noqa: E402

import numpy as np


# 0-based columns of the RH variants (see "Which reflector height").
RAW_RH_COL, RHDOT_RH_COL, IF_RH_COL = 2, 22, 24
RH_LABELS = {
    RAW_RH_COL: "column 3, raw RH (no RHdot or IF correction)",
    RHDOT_RH_COL: "column 23, RH with the RHdot correction",
    IF_RH_COL: "column 25, RH with the RHdot and IF corrections",
}


def rh_column(c: list[str], raw_rh: bool = False) -> int:
    """
    The most corrected RH column this row has. gnssrefl leaves column
    25 at 0 for a frequency missing from its bias table
    (subdaily.rhdot_correction2), so 0 there falls back to column 23.
    """
    if raw_rh:
        return RAW_RH_COL
    if len(c) > IF_RH_COL:
        try:
            if float(c[IF_RH_COL]) != 0.0:
                return IF_RH_COL
        except ValueError:
            pass
    if len(c) > RHDOT_RH_COL:
        return RHDOT_RH_COL
    return RAW_RH_COL


def load_subdaily(path: Path, hortho: float, raw_rh: bool = False, used: Counter | None = None):
    """
    Returns per-arc records: time, water level, mean elevation,
    azimuth, peak-to-noise, and what load_arcs() needs to join and
    correct arcs (key, freq, EdotF, the RH and its column, the RHdot
    RH). The water level is Hortho minus the most corrected RH of each
    row (rh_column()); `used` counts the columns taken.
    """
    recs = []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            c = line.split()
            if len(c) < 22:
                continue
            col = rh_column(c, raw_rh)
            try:
                year = int(float(c[0]))
                rh = float(c[col])
                azim = float(c[5])
                emin = float(c[7])
                emax = float(c[8])
                pknoise = float(c[13])
                freq = int(float(c[10]))
                edotf = float(c[12])
                key = (int(float(c[0])), int(float(c[1])), round(float(c[4]), 3),
                       int(float(c[3])), freq)
                rhdot_rh = float(c[RHDOT_RH_COL]) if len(c) > RHDOT_RH_COL else None
                month, day = int(float(c[17])), int(float(c[18]))
                hh, mm, ss = int(float(c[19])), int(float(c[20])), int(float(c[21]))
            except (ValueError, IndexError):
                continue
            try:
                dt = datetime(year, month, day, hh, mm, ss)
            except ValueError:
                continue
            if used is not None:
                used[col] += 1
            recs.append({
                "time": dt,
                "wl": hortho - rh,
                "elev": (emin + emax) / 2.0,
                "emax": emax,
                "azim": azim,
                "pknoise": pknoise,
                "key": key,
                "freq": freq,
                "edotf": edotf,
                "rh": rh,
                "rh_col": col,
                "rhdot_rh": rhdot_rh,
            })
    return recs


def corrected_file_for(path: Path) -> Path | None:
    """
    The nightly fit's .withrhdotIF file for a plain
    <sta>_<year>_subdaily_edit.txt: next to it or in its fit/ folder,
    the newest if there are several (a fit spanning two years is named
    <sta>_<year>_<year_end>_subdaily_edit.txt.withrhdotIF).
    """
    sta = path.name.split("_")[0]
    found = []
    for folder in (path.parent, path.parent / "fit"):
        found += folder.glob(f"{sta}_*subdaily_edit.txt.withrhdotIF")
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def rhdot_at(times, spline) -> np.ndarray:
    """
    dRH/dt in m/hour at `times`, from the spline of water level being
    compared (RH falls as the water rises: dRH/dt = -d(wl)/dt). Zero
    without a usable spline. Its 999 gap values are left out.
    """
    if spline is None:
        return np.zeros(len(times))
    st, sv = spline
    sv = np.asarray(sv, float)
    good = np.isfinite(sv) & (np.abs(sv) < 900)
    if good.sum() < 2:
        return np.zeros(len(times))
    base = st[0]
    sx = np.array([(t - base).total_seconds() / 3600.0 for t in st])[good]
    sx, idx = np.unique(sx, return_index=True)
    sv = sv[good][idx]
    if len(sx) < 2:
        return np.zeros(len(times))
    qx = np.array([(t - base).total_seconds() / 3600.0 for t in times])
    return -np.interp(qx, sx, np.gradient(sv, sx))


def load_arcs(path: Path, hortho: float, raw_rh: bool = False, spline=None):
    """
    Per-arc records with the most corrected RH available for `path`
    (see "Which reflector height"), and a line saying which was used.

    Given the plain edit file, every arc is kept: an arc in the fit's
    .withrhdotIF takes its corrected RH, any other is corrected here
    with the RHdot term from `spline` (load_spline()'s times and
    levels) and the fit's IF bias for its frequency. Records then
    carry "fit_dropped": the arc lies in the fit's span but its edits
    removed it.
    """
    used: Counter = Counter()
    recs = load_subdaily(path, hortho, raw_rh, used)
    if raw_rh or not recs or set(used) != {RAW_RH_COL}:
        cols = ", ".join(f"{RH_LABELS[k]}: {n}" for k, n in sorted(used.items()))
        note = f"{path.name}: {cols or 'no arcs'}"
        if not raw_rh and set(used) - {RAW_RH_COL}:
            note += ("\n  WARNING: this file holds only the arcs the nightly fit kept "
                     "(gnssrefl drops arcs over 3 sigma from its spline before\n"
                     "  writing it), so the outlying arcs this test looks for are "
                     "missing. Give the plain <sta>_<year>_subdaily_edit.txt.")
        return recs, note

    corrected = corrected_file_for(path)
    fit = {}
    if corrected is not None:
        fit = {r["key"]: r for r in load_subdaily(corrected, hortho)}
    matched = [r for r in recs if r["key"] in fit]
    if not matched:
        return recs, (f"{path.name}: {RH_LABELS[RAW_RH_COL]}. WARNING: no .withrhdotIF "
                      f"file covering it, so the disagreement includes the RHdot and IF "
                      f"corrections the spline has (review B18)")

    # The fit's IF bias per frequency: its column 23 minus column 25.
    diffs: dict = {}
    for r in fit.values():
        if r["rh_col"] == IF_RH_COL:
            diffs.setdefault(r["freq"], []).append(r["rhdot_rh"] - r["rh"])
    bias = {f: float(np.median(v)) for f, v in diffs.items()}
    lo = min(r["time"] for r in fit.values())
    hi = max(r["time"] for r in fit.values())

    for r in matched:
        r["wl"] = fit[r["key"]]["wl"]
        r["fit_dropped"] = False
    others = [r for r in recs if r["key"] not in fit]
    for r, rhdot in zip(others, rhdot_at([r["time"] for r in others], spline)):
        r["wl"] = hortho - (r["rh"] - r["edotf"] * rhdot - bias.get(r["freq"], 0.0))
        r["fit_dropped"] = lo <= r["time"] <= hi
    dropped = sum(r["fit_dropped"] for r in others)

    return recs, (f"{path.name}, every arc. {len(matched)} take the fit's "
                  f"{RH_LABELS[IF_RH_COL]}\n"
                  f"  from {corrected.name} ({lo:%Y-%m-%d} to {hi:%Y-%m-%d}). "
                  f"Corrected here (RHdot from the spline, the fit's IF bias):\n"
                  f"  {dropped} the fit's 3-sigma edits dropped, {len(others) - dropped} "
                  f"outside its span. --raw-rh compares the raw RH")


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
    # On NAVD88 whichever antenna height wrote the file (station_datum.py).
    return times, np.asarray(values, float) + spline_shift(path)


def dropped_header(dropped: np.ndarray | None) -> str:
    return "" if dropped is None else f"  {'fit dropped':>13}"


def dropped_cell(dropped: np.ndarray | None, m: np.ndarray) -> str:
    """Arcs of the bin the fit's 3-sigma edits removed (see load_arcs())."""
    if dropped is None:
        return ""
    d = int(dropped[m].sum())
    return f"  {d:5d} ({100.0 * d / m.sum():4.1f}%)"


def report_bins(label: str, keys: np.ndarray, resid: np.ndarray,
                edges: list[float], unit: str = "deg",
                dropped: np.ndarray | None = None) -> None:
    print(f"  {label}")
    print(f"    {'range':>14}  {'n':>6}  {'mean |resid|':>12}{dropped_header(dropped)}")
    stats = []
    for lo, hi in zip(edges, edges[1:]):
        m = (keys >= lo) & (keys < hi)
        if m.sum() < 30:
            continue
        v = float(np.abs(resid[m]).mean())
        stats.append((lo, hi, int(m.sum()), v))
        print(f"    {lo:5.0f}-{hi:<5.0f}{unit}  {m.sum():6d}  {v:10.3f} m"
              f"{dropped_cell(dropped, m)}")

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
    p.add_argument("--subdaily", required=True,
                   help="per-arc file: the plain <sta>_<year>_subdaily_edit.txt, "
                        "which has every arc (see \"Which reflector height\")")
    p.add_argument("--spline", required=True)
    p.add_argument("--raw-rh", action="store_true",
                   help="compare each arc's raw RH (column 3), as before review B18")
    p.add_argument("--hortho", type=float, default=None,
                   help="antenna height, m NAVD88 (default: station.json, on NAVD88)")
    args = p.parse_args()
    if args.hortho is None:
        args.hortho = navd88_hortho()
    if args.hortho is None:
        p.error("--hortho is required: station.json has no gnssrefl_orthometric_height")

    st, sv = load_spline(Path(args.spline))
    recs, rh_note = load_arcs(Path(args.subdaily), args.hortho, args.raw_rh, (st, sv))

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
    dropped = (np.array([r["fit_dropped"] for r in recs])
               if "fit_dropped" in recs[0] else None)

    ok = np.isfinite(spline_at) & np.isfinite(wl)
    resid = wl[ok] - spline_at[ok]
    elev, emax, azim, pkn = elev[ok], emax[ok], azim[ok], pkn[ok]
    if dropped is not None:
        dropped = dropped[ok]

    print("=" * 68)
    print("  PER-ARC QUALITY vs GEOMETRY")
    print("=" * 68)
    print(f"  {len(resid)} arcs compared against the spline consensus")
    print(f"  RH: {rh_note}")
    print(f"  Overall spread: {np.abs(resid).mean():.3f} m mean absolute")
    print()
    print("  Each arc's water level is compared against the spline at the")
    print("  same moment. An arc reflecting off something other than open")
    print("  water should disagree with the consensus more than one that is")
    print("  not.")
    print()

    report_bins("By mean arc elevation (higher = footprint closer to shore):",
                elev, resid, [5, 7, 9, 11, 13, 15.1], dropped=dropped)

    report_bins("By arc upper elevation (emaxO):",
                emax, resid, [8, 10, 12, 14, 15.1], dropped=dropped)

    az_edges = [0, 30, 60, 90, 120, 150, 175, 353, 361]
    print("  By azimuth (which direction the reflection came from):")
    print(f"    {'range':>14}  {'n':>6}  {'mean |resid|':>12}{dropped_header(dropped)}")
    stats = []
    for lo, hi in zip(az_edges, az_edges[1:]):
        m = (azim >= lo) & (azim < hi)
        if m.sum() < 30:
            continue
        v = float(np.abs(resid[m]).mean())
        stats.append((lo, hi, v))
        print(f"    {lo:5.0f}-{hi:<5.0f}deg  {m.sum():6d}  {v:10.3f} m"
              f"{dropped_cell(dropped, m)}")
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
