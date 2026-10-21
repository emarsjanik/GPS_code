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

A USGS gage measures actual water against a surveyed benchmark.
Tied to NAVD88, that makes it a genuine reference for the constant
offset, which the model comparison could never resolve.

WHICH GAGE SERIES IS NAVD88 (review B19)

NWIS parameter 00065 is gage height: feet above the gage's own datum,
an arbitrary local zero. It is NAVD88 only when that datum is NAVD88
zero. Parameter 62620 is the water-surface elevation above NAVD88
itself. This script used to fetch 00065 and call it NAVD88, so every
offset it printed was off by the gage datum's elevation, unknown.

Now (--parameter auto) it asks for 62620 first. If the site has none,
it takes 00065 and adds the gage datum's altitude from the NWIS site
file (alt_va, when its alt_datum_cd is NAVD88), or the value given
with --gage-datum-navd88-ft (from the station description). Without
either, the levels stay on the gage datum and the offsets are labelled
as such: not a datum tie. The amplitude, timing and slope do not
depend on the datum.

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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from station_datum import spline_shift  # noqa: E402

import numpy as np

FEET_TO_M = 0.3048
DEFAULT_SITE = "420259070105600"   # Provincetown, MA

# NWIS parameter codes (see "Which gage series is NAVD88").
NAVD88_ELEVATION = "62620"   # water-surface elevation above NAVD 1988, ft
GAGE_HEIGHT = "00065"        # gage height above the gage datum, ft


def _get(url: str) -> str | None:
    """The response text, or None when NWIS has no such series (it
    answers 404 'No sites found matching all criteria')."""
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def read_rdb(text: str) -> list[dict]:
    """Rows of an NWIS RDB table as dicts keyed by its header names
    (the line after the header is the column-format line)."""
    rows, header, skip_format = [], None, False
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if header is None:
            header, skip_format = parts, True
            continue
        if skip_format:
            skip_format = False
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def fetch_series(site: str, start: str, end: str, parameter: str):
    """(times_utc, values_m) of one parameter from the instantaneous
    values service, or None if the site does not report it."""
    text = _get("https://waterservices.usgs.gov/nwis/iv/"
                f"?sites={site}&parameterCd={parameter}"
                f"&startDT={start}&endDT={end}&format=rdb")
    if text is None:
        return None
    rows = read_rdb(text)
    times, values = [], []
    for row in rows:
        if row.get("agency_cd") != "USGS":
            continue
        # the value column is <ts_id>_<parameter>; <...>_cd is its flag
        keys = [k for k in row if k.endswith(f"_{parameter}")]
        if not keys:
            continue
        raw = row[keys[0]]
        if not raw or raw in ("Ice", "Eqp", "Bkw"):
            continue
        try:
            naive = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M")
            value = float(raw)
        except (KeyError, ValueError):
            continue
        # EDT is UTC-4, EST is UTC-5. The gage reports local
        # time with the zone named per record, so honour it
        # rather than assuming one or the other.
        offset = 4 if row.get("tz_cd", "").upper() == "EDT" else 5
        times.append(naive + timedelta(hours=offset))
        values.append(value * FEET_TO_M)
    if not times:
        return None
    return times, np.asarray(values, dtype=float)


def fetch_gage_datum(site: str):
    """(altitude_ft, datum_code, accuracy_ft) of the gage datum from
    the NWIS site file, or None. accuracy_ft may be None."""
    text = _get("https://waterservices.usgs.gov/nwis/site/"
                f"?sites={site}&siteOutput=expanded&format=rdb")
    if text is None:
        return None
    for row in read_rdb(text):
        try:
            altitude = float(row.get("alt_va", ""))
        except ValueError:
            continue
        try:
            accuracy = float(row.get("alt_acy_va", ""))
        except ValueError:
            accuracy = None
        return altitude, row.get("alt_datum_cd", "").strip(), accuracy
    return None


def fetch_gage(site: str, start: str, end: str, parameter: str = "auto",
               gage_datum_navd88_ft: float | None = None):
    """
    Returns (times_utc, heights_m, on_navd88, note): the gage series in
    metres, whether it is on NAVD88, and a line saying where its datum
    came from. See "Which gage series is NAVD88".
    """
    if parameter in ("auto", NAVD88_ELEVATION):
        series = fetch_series(site, start, end, NAVD88_ELEVATION)
        if series is not None:
            unused = ("" if gage_datum_navd88_ft is None else
                      "; --gage-datum-navd88-ft not used")
            return (*series, True,
                    f"parameter {NAVD88_ELEVATION}, water-surface elevation above NAVD88"
                    f"{unused}")
        if parameter == NAVD88_ELEVATION:
            return [], np.asarray([], float), True, f"no parameter {NAVD88_ELEVATION}"

    series = fetch_series(site, start, end, GAGE_HEIGHT)
    if series is None:
        return [], np.asarray([], float), False, f"no parameter {GAGE_HEIGHT}"
    times, heights = series
    label = f"parameter {GAGE_HEIGHT}, gage height"

    if gage_datum_navd88_ft is not None:
        return (times, heights + gage_datum_navd88_ft * FEET_TO_M, True,
                f"{label} + gage datum {gage_datum_navd88_ft:+.3f} ft NAVD88 "
                f"(--gage-datum-navd88-ft)")

    datum = fetch_gage_datum(site)
    if datum is not None and datum[1].upper().replace(" ", "") == "NAVD88":
        altitude, _code, accuracy = datum
        acy = f" +/-{accuracy:g} ft" if accuracy is not None else ""
        return (times, heights + altitude * FEET_TO_M, True,
                f"{label} + gage datum {altitude:+.3f} ft NAVD88{acy} (NWIS site file)")

    why = (f"the site file gives the gage datum on {datum[1] or 'no datum'}"
           if datum is not None else "the site file gives no gage datum")
    return (times, heights, False,
            f"{label} above the gage datum, NOT NAVD88: {why}; "
            f"give --gage-datum-navd88-ft to tie it")


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
    # On NAVD88 whichever antenna height wrote the file (station_datum.py).
    return times, np.asarray(values, float) + spline_shift(path)


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
    p.add_argument("--parameter", choices=("auto", NAVD88_ELEVATION, GAGE_HEIGHT),
                   default="auto",
                   help=f"NWIS series: {NAVD88_ELEVATION} (NAVD88 elevation), "
                        f"{GAGE_HEIGHT} (gage height), or auto = {NAVD88_ELEVATION} "
                        f"if the site has it")
    p.add_argument("--gage-datum-navd88-ft", type=float, default=None,
                   help=f"NAVD88 elevation of the gage datum, ft, for {GAGE_HEIGHT} "
                        "(default: the NWIS site file's, if it is on NAVD88)")
    args = p.parse_args()

    print(f"Fetching USGS site {args.site}, {args.start} to {args.end} ...")
    gage_t, gage_v, on_navd88, datum_note = fetch_gage(
        args.site, args.start, args.end, args.parameter, args.gage_datum_navd88_ft)
    if not gage_t:
        print(f"No gage data returned ({datum_note}).")
        return 1
    gage_ref = "NAVD88" if on_navd88 else "gage datum, NOT NAVD88"
    print(f"  {len(gage_t)} observations, "
          f"{gage_t[0]} to {gage_t[-1]} UTC")
    print(f"  {datum_note}")
    print(f"  range {gage_v.min():+.3f} to {gage_v.max():+.3f} m ({gage_ref})")

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

        x = interp[ok]          # gage, metres (NAVD88 if on_navd88)
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
        if on_navd88:
            print(f"              (offset at gage zero, i.e. at NAVD88 datum)")
        else:
            print(f"              (offset at the gage's own zero: NOT a NAVD88 tie)")
        print()
        print(f"  Residual scatter about the fit: {resid.std():.3f} m")
        print(f"  ({n} points)")
        print()
        print("  The residual scatter is the better measure of how well the")
        print("  two records agree in shape, since it no longer contains the")
        print("  amplitude difference.")
        print()
        if on_navd88:
            print("  CAUTION: the intercept is this station's vertical reference")
            print("  relative to NAVD88 as realized at Provincetown, PLUS any")
            print("  real difference in mean water level between two sites 27 km")
            print("  apart. A single remote gage cannot separate those. Treat it")
            print("  as narrowing the datum question, not settling it.")
            print()
            print(f"  The gage series is on NAVD88 ({datum_note}).")
            print("  This station's water levels are NAVD88 too, through the")
            print("  OPUS antenna height (GEOID18, +/-0.061 m). The mean above")
            print("  is therefore mostly the real difference in mean water level")
            print("  between two sites 27 km apart in different tidal regimes,")
            print("  plus the antenna-height and gage-datum uncertainties --")
            print("  not a datum offset.")
        else:
            print("  The gage series is NOT on NAVD88 (gage height above the")
            print("  gage's own zero), so the mean and the intercept above")
            print("  contain the gage datum's elevation: they say nothing")
            print("  about this station's datum. Give --gage-datum-navd88-ft.")

    print()
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
