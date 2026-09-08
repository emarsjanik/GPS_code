#!/usr/bin/env python3
"""
compare_to_usgs_gage.py

Compares this station's GNSS-IR water levels against observed data
from a USGS tide gage.

WHY THIS IS DIFFERENT FROM THE TIDE MODEL COMPARISON

The tide model comparison answers "does the measurement track the
astronomical tide?" -- and it does, closely. But a tide model is a
prediction, with its own error (EOT20 agrees with tide gauges at
about 6 cm RSS in coastal waters), and it carries no absolute datum.
So it cannot say whether the measured water level is correct in an
absolute sense, only whether it varies correctly.

A USGS gage measures actual water against a surveyed NAVD88
benchmark. That makes it a genuine reference for the constant
offset, which the model comparison could never resolve.

WHAT THIS COMPARISON CAN AND CANNOT SHOW

The nearest continuous gage (Provincetown, 420259070105600) is about
27 km away and -- more importantly -- sits in Cape Cod Bay while
this station faces the Atlantic. Those are different tidal regimes.
Expect real differences in amplitude and phase that are physics, not
error.

So this is NOT a clean accuracy measurement the way a co-located
gauge would be. What it can establish:

  - whether the timing is consistent day to day (a stable phase
    offset between two sites is physical; a drifting one is not)
  - whether the amplitude ratio is stable
  - an absolute datum tie, once the site-to-site difference is
    accounted for

Read the correlation as a check on tracking, not as an accuracy
figure. The RMS between two sites in different tidal regimes is
dominated by the regime difference.

Usage:
    python3 compare_to_usgs_gage.py \\
        --spline-file products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --start 2026-09-01 --end 2026-09-08
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

FEET_TO_M = 0.3048
DEFAULT_SITE = "420259070105600"   # Provincetown, MA


def fetch_gage(site: str, start: str, end: str):
    """Returns (times_utc, heights_m) from the USGS instantaneous
    values service. Heights are gage height in feet above NAVD88,
    converted to metres."""
    url = ("https://waterservices.usgs.gov/nwis/iv/"
           f"?sites={site}&parameterCd=00065"
           f"&startDT={start}&endDT={end}&format=rdb")
    with urllib.request.urlopen(url, timeout=120) as r:
        text = r.read().decode("utf-8", errors="replace")

    times, values = [], []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if parts[0] != "USGS":
            continue          # header or format row
        if len(parts) < 5:
            continue
        try:
            stamp, tz, raw = parts[2], parts[3], parts[4]
            if not raw or raw in ("Ice", "Eqp", "Bkw"):
                continue
            naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
            # EDT is UTC-4, EST is UTC-5. The gage reports local
            # time with the zone named per record, so honour it
            # rather than assuming one or the other.
            offset = 4 if tz.upper() == "EDT" else 5
            times.append(naive + timedelta(hours=offset))
            values.append(float(raw) * FEET_TO_M)
        except (ValueError, IndexError):
            continue
    return times, np.asarray(values, dtype=float)


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
                v = float(c[8])
            except (ValueError, IndexError):
                continue
            if abs(v) > 900:
                continue
            times.append(dt)
            values.append(v)
    return times, np.asarray(values, dtype=float)


def best_lag(gnss_t, gnss_v, gage_t, gage_v, max_minutes=180, step=5):
    """Finds the time shift maximizing correlation. Two sites in
    different parts of the same estuary have a genuine phase
    difference; measuring it is more informative than assuming
    zero."""
    base = gage_t[0]
    gx = np.array([(t - base).total_seconds() for t in gage_t])
    order = np.argsort(gx)
    gx, gv = gx[order], gage_v[order]

    results = []
    for shift in range(-max_minutes, max_minutes + 1, step):
        qx = np.array([(t - base).total_seconds() + shift * 60
                       for t in gnss_t])
        interp = np.interp(qx, gx, gv, left=np.nan, right=np.nan)
        ok = np.isfinite(interp)
        if ok.sum() < 50:
            continue
        r = float(np.corrcoef(gnss_v[ok], interp[ok])[0, 1])
        results.append((shift, r, int(ok.sum())))
    if not results:
        return None
    return max(results, key=lambda x: x[1])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spline-file", required=True)
    p.add_argument("--site", default=DEFAULT_SITE)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = p.parse_args()

    print(f"Fetching USGS site {args.site}, {args.start} to {args.end} ...")
    gage_t, gage_v = fetch_gage(args.site, args.start, args.end)
    if not gage_t:
        print("No gage data returned.")
        return 1
    print(f"  {len(gage_t)} observations, "
          f"{gage_t[0]} to {gage_t[-1]} UTC")
    print(f"  range {gage_v.min():+.3f} to {gage_v.max():+.3f} m (NAVD88)")

    gnss_t, gnss_v = load_spline(Path(args.spline_file))
    lo, hi = gage_t[0], gage_t[-1]
    sel = [(t, v) for t, v in zip(gnss_t, gnss_v) if lo <= t <= hi]
    if len(sel) < 50:
        print(f"\nOnly {len(sel)} GNSS points overlap the gage record.")
        return 1
    gt = [t for t, _ in sel]
    gv = np.array([v for _, v in sel])
    print(f"\nGNSS-IR: {len(gt)} points in the same window")
    print(f"  range {gv.min():+.3f} to {gv.max():+.3f} m "
          f"(station's own reference)")

    print()
    print("=" * 68)
    print("  AMPLITUDE")
    print("=" * 68)
    gage_range = gage_v.max() - gage_v.min()
    gnss_range = gv.max() - gv.min()
    print(f"  Gage tidal range : {gage_range:.3f} m")
    print(f"  GNSS tidal range : {gnss_range:.3f} m")
    print(f"  Ratio            : {gnss_range/gage_range:.3f}")
    print()
    print("  These are different sites in different tidal regimes, so a")
    print("  ratio away from 1.0 is expected and physical. What matters")
    print("  is whether it stays stable over time.")

    print()
    print("=" * 68)
    print("  TIMING")
    print("=" * 68)
    lag = best_lag(gt, gv, gage_t, gage_v)
    if lag is None:
        print("  Not enough overlap to estimate a lag.")
    else:
        shift, r, n = lag
        print(f"  Best correlation at a {shift:+d} minute shift: r = {r:+.4f}")
        print(f"  ({n} points compared)")
        print()
        if shift > 0:
            print(f"  This station's tide arrives about {shift} minutes AFTER")
            print(f"  Provincetown's.")
        elif shift < 0:
            print(f"  This station's tide arrives about {abs(shift)} minutes")
            print(f"  BEFORE Provincetown's.")
        else:
            print("  The two sites are in phase to within the 5-minute step.")

    # Offset, at the best lag, is the datum-relevant number.
    if lag is not None:
        shift = lag[0]
        base = gage_t[0]
        gx = np.array([(t - base).total_seconds() for t in gage_t])
        order = np.argsort(gx)
        qx = np.array([(t - base).total_seconds() + shift * 60 for t in gt])
        interp = np.interp(qx, gx[order], gage_v[order],
                           left=np.nan, right=np.nan)
        ok = np.isfinite(interp)
        diff = gv[ok] - interp[ok]

        print()
        print("=" * 68)
        print("  OFFSET  (raw difference)")
        print("=" * 68)
        print(f"  Mean (GNSS - gage): {diff.mean():+.3f} m")
        print(f"  Std of difference : {diff.std():.3f} m")
        print()
        print("  That spread is dominated by the amplitude difference, not")
        print("  by noise -- the two sites diverge by up to a metre at the")
        print("  tidal extremes. The fit below separates the two.")

        print()
        print("=" * 68)
        print("  SCALE AND OFFSET  (the datum-relevant fit)")
        print("=" * 68)

        x = interp[ok]          # gage, NAVD88 metres
        y = gv[ok]              # this station, its own reference

        # Ordinary least squares. Both series carry error, so a
        # total-least-squares fit would be more correct in principle;
        # with the gage an order of magnitude more precise than the
        # GNSS retrievals, OLS is a reasonable approximation and its
        # slope bias is small.
        n = len(x)
        slope, intercept = np.polyfit(x, y, 1)
        fitted = slope * x + intercept
        resid = y - fitted
        rss = float(np.sum(resid ** 2))

        # Standard errors from the usual OLS expressions.
        sxx = float(np.sum((x - x.mean()) ** 2))
        s_err = np.sqrt(rss / (n - 2))
        se_slope = s_err / np.sqrt(sxx)
        se_intercept = s_err * np.sqrt(1.0 / n + x.mean() ** 2 / sxx)

        print(f"  GNSS = {slope:.4f} x gage {intercept:+.4f}")
        print()
        print(f"  Slope     : {slope:.4f} +/- {se_slope:.4f}")
        print(f"              (amplitude ratio; 1.0 would mean identical range)")
        print(f"  Intercept : {intercept:+.4f} +/- {se_intercept:.4f} m")
        print(f"              (offset at gage zero, i.e. at NAVD88 datum)")
        print()
        print(f"  Residual scatter about the fit: {resid.std():.3f} m")
        print(f"  ({n} points)")
        print()
        print("  The residual scatter is the better measure of how well the")
        print("  two records agree in shape, since it no longer contains the")
        print("  amplitude difference.")
        print()
        print("  CAUTION: the intercept is this station's vertical reference")
        print("  relative to NAVD88 as realized at Provincetown, PLUS any")
        print("  real difference in mean water level between two sites 27 km")
        print("  apart. A single remote gage cannot separate those. Treat it")
        print("  as narrowing the datum question, not settling it.")
        print()
        print("  The gage is referenced to NAVD88, surveyed to 0.02 ft.")
        print("  This station's water levels are relative to a CGVD2013")
        print("  orthometric height from CSRS-PPP. The mean above therefore")
        print("  mixes a genuine datum difference with the real difference")
        print("  in mean water level between two sites 27 km apart in")
        print("  different tidal regimes -- it is a starting point for the")
        print("  datum question, not an answer to it.")

    print()
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
