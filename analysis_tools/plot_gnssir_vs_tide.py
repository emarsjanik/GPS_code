#!/usr/bin/env python3
"""
plot_gnssir_vs_tide.py

Generates a line graph directly overlaying the GNSS-IR water level
(gnssrefl's evenly-sampled spline output) against the tide model, on
a shared time and value axis -- so the real agreement in shape, and
the real, still-unresolved constant offset between them, are both
visible directly, honestly, without artificially aligning the two
curves to hide the open question.

Confirmed, real file format used here (from direct inspection
earlier in this project): gnssrefl's own evenly-sampled spline
output file has the date in columns 3-8 (YYYY MM DD HH MM SS,
1-indexed) and column 9 is the already-computed quasi-sea-level
value (Hortho - RH), in meters.

EARLIER TIDE FILES (--tide-history)

The tide model comes in spreadsheets that each cover a stretch of
time, and --tide-file (station.json tide_model_file) is only the
current one, so on its own the orange line starts where that file
starts. --tide-history adds the earlier ones: a path or a glob
pattern, as many times as needed (station.json tide_model_history in
process_and_plot.sh). They are joined with --tide-file into one line:
  - each file wins over the stretches it covers (from its first row
    to its last, split at any gap in it): --tide-file over all the
    earlier ones, and among those the one that starts later. A file's
    rows inside a winning file's stretch are dropped, not interleaved,
    so two files a row apart in time cannot zigzag; a gap in a winning
    file is filled from the files below it;
  - where two files share timestamps the run prints their largest
    difference, and warns above 5 mm (merge_tide_files.py explains
    the one-row offset between the first two spreadsheets);
  - an earlier file without the height column, or one that cannot be
    read, is left out and named; a pattern that matches nothing is
    named. Neither stops the plot. --tide-file must still load.
A gap is two consecutive rows further apart than an hour or three
sample spacings, whichever is longer. Where the joined tide still has
one, the orange line breaks instead of drawing a straight line across
it, and the GNSS-IR points inside the gap are left out of the offset.

Usage:
    python3 plot_gnssir_vs_tide.py \\
        --spline-file products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --tide-file marconi_tides_sherwood.xlsx \\
        --tide-history "marconi_tides_sherwood_*.xlsx" \\
        --tide-value-col EOT20_heightm \\
        --output comparison_plot.png
"""

from __future__ import annotations

import argparse
import glob
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from station_datum import spline_shift, tide_model_in_datum  # noqa: E402

import matplotlib
matplotlib.use("Agg")  # write directly to a file, no display needed
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np


def load_spline_output(path: Path):
    times, values = [], []
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            cols = line.split()
            if len(cols) < 9:
                continue
            try:
                year = int(float(cols[2]))
                month = int(float(cols[3]))
                day = int(float(cols[4]))
                hour = int(float(cols[5]))
                minute = int(float(cols[6]))
                second = int(float(cols[7]))
                water_level = float(cols[8])
            except (ValueError, IndexError):
                continue
            try:
                dt = datetime(year, month, day, hour, minute, second)
            except ValueError:
                continue
            times.append(dt)
            values.append(water_level)
    # On NAVD88 whichever antenna height wrote the file (station_datum.py).
    return times, np.asarray(values, float) + spline_shift(path)


def load_tide_reference(path: Path, time_col: str, value_col: str):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    header = [c.value for c in ws[1]]
    time_idx = header.index(time_col)
    value_idx = header.index(value_col)

    times, values = [], []
    for row in ws.iter_rows(min_row=2, values_only=True):
        t = row[time_idx]
        if not isinstance(t, datetime):
            continue
        try:
            v = float(row[value_idx])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        times.append(t)
        values.append(v)
    wb.close()
    return times, np.asarray(values, float)


# Two files whose values differ by more than this at a shared
# timestamp are not the same model on the same clock.
OVERLAP_TOLERANCE_M = 0.005
# A tide gap: consecutive rows further apart than this, or than three
# sample spacings, whichever is longer.
MIN_GAP = timedelta(hours=1)


def expand_tide_history(patterns, current: Path):
    """The files --tide-history names, globs expanded (sorted), each
    once and never --tide-file itself; and the patterns that matched
    nothing."""
    seen = {os.path.realpath(current)}
    files, unmatched = [], []
    for pattern in patterns:
        pattern = os.path.expanduser(pattern)
        hits = (sorted(glob.glob(pattern)) if glob.has_magic(pattern)
                else [pattern] if os.path.isfile(pattern) else [])
        if not hits:
            unmatched.append(pattern)
        for f in hits:
            real = os.path.realpath(f)
            if real not in seen and os.path.isfile(f):
                seen.add(real)
                files.append(Path(f))
    return files, unmatched


def load_tide_series(current: Path, history, time_col: str, value_col: str):
    """--tide-file and the earlier files joined into one series, sorted
    by time: (times, values, one line per file for the log).

    Priority: --tide-file, then the earlier files from the latest
    start back. Each file keeps only its rows outside the stretches
    (covered_stretches) of the files above it, so a gap in a file is
    filled from the files below it. --tide-file must load; an earlier
    file that does not is left out and named."""
    loaded = [(current, *load_tide_reference(current, time_col, value_col))]
    notes = []
    for path in history:
        try:
            times, values = load_tide_reference(path, time_col, value_col)
        except ValueError as e:   # header.index: no such column
            notes.append(f"  left out {path}: no '{time_col}' or '{value_col}' column ({e})")
            continue
        except Exception as e:     # not a workbook, unreadable, ...
            notes.append(f"  left out {path}: could not read it ({type(e).__name__}: {e})")
            continue
        if not times:
            notes.append(f"  left out {path}: no rows with a time and a '{value_col}' value")
            continue
        loaded.append((path, times, values))
    head, rest = loaded[0], loaded[1:]
    rest.sort(key=lambda f: min(f[1]), reverse=True)

    spans, by_time = [], {}
    all_t, all_v = [], []
    for path, times, values in [head] + rest:
        values = np.asarray(values, float)
        keep = [not any(a <= t <= b for a, b in spans) for t in times]
        shared = [(by_time[t], v) for t, v, k in zip(times, values, keep)
                  if not k and t in by_time]
        used = [(t, v) for t, v, k in zip(times, values, keep) if k]
        for t, v in used:
            by_time[t] = v
            all_t.append(t)
            all_v.append(v)
        if times:
            stretches = covered_stretches(times)
            spans.extend(stretches)
            first, last = stretches[0][0], stretches[-1][1]
            holes = len(stretches) - 1
            notes.append(f"  {path}: {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M}"
                         + (f" with {holes} gap(s)" if holes else "")
                         + f", {len(used)} of {len(times)} rows used")
        if shared:
            worst = max(abs(a - b) for a, b in shared)
            notes.append(f"    {len(shared)} timestamps in common with a file "
                         f"that wins there: largest difference {worst:.4f} m"
                         + ("  WARNING: they disagree (a time offset between the files?); "
                            "the winning file is drawn" if worst > OVERLAP_TOLERANCE_M else ""))
    order = sorted(range(len(all_t)), key=all_t.__getitem__)
    return [all_t[i] for i in order], np.asarray([all_v[i] for i in order], float), notes


def covered_stretches(times):
    """(first, last) of each stretch of a file's rows: its first row to
    its last, split wherever two consecutive rows are a gap apart."""
    ts = sorted(times)
    gap = tide_gap(ts)
    out, start = [], ts[0]
    for a, b in zip(ts, ts[1:]):
        if b - a > gap:
            out.append((start, a))
            start = b
    out.append((start, ts[-1]))
    return out


def tide_gap(times) -> timedelta:
    """The spacing above which two consecutive tide rows (sorted) are a
    gap: an hour or three median spacings, whichever is longer."""
    if len(times) < 2:
        return MIN_GAP
    steps = np.diff(np.array([(t - times[0]).total_seconds() for t in times]))
    return max(MIN_GAP, timedelta(seconds=3 * float(np.median(steps))))


def break_at_gaps(times, values, gap: timedelta):
    """The series with a NaN between rows more than `gap` apart, so the
    line is not drawn across a stretch with no tide."""
    out_t, out_v = [], []
    for i, (t, v) in enumerate(zip(times, values)):
        if i and t - times[i - 1] > gap:
            out_t.append(times[i - 1] + (t - times[i - 1]) / 2)
            out_v.append(np.nan)
        out_t.append(t)
        out_v.append(v)
    return out_t, np.asarray(out_v, float)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spline-file", required=True)
    p.add_argument("--tide-file", required=True, help="the current tide file (tide_model_file)")
    p.add_argument("--tide-history", action="append", default=[], metavar="PATH_OR_GLOB",
                   help="an earlier tide file, or a glob pattern for several (quote it); "
                        "repeat as needed. See EARLIER TIDE FILES above")
    p.add_argument("--tide-time-col", default="time")
    p.add_argument("--tide-value-col", required=True)
    p.add_argument("--tide-datum-offset", type=float, default=None,
                   help="m added to the tide model to put it on NAVD88 "
                        "(default: tide_model_navd88_offset in station.json, else +0.09)")
    p.add_argument("--output", default="gnssir_vs_tide.png")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD, optional, restricts the plotted window")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD, optional, restricts the plotted window")
    p.add_argument("--no-align", action="store_true",
                   help="do not remove the constant offset; show both series "
                        "on their raw, unadjusted axes")
    args = p.parse_args()

    spline_times, spline_values = load_spline_output(Path(args.spline_file))
    print(f"Loaded {len(spline_times)} GNSS-IR spline points")
    if not spline_times:
        raise SystemExit("No spline points loaded -- check --spline-file path/format.")

    history, unmatched = expand_tide_history(args.tide_history, Path(args.tide_file))
    for pattern in unmatched:
        print(f"Earlier tide files: nothing matches {pattern}")
    tide_times, tide_values, notes = load_tide_series(
        Path(args.tide_file), history, args.tide_time_col, args.tide_value_col)
    if history:
        print(f"Tide model from {Path(args.tide_file).name} and {len(history)} earlier file(s):")
        print("\n".join(notes))
    tide_values, tide_shift = tide_model_in_datum(np.asarray(tide_values, float), "NAVD88",
                                                  model_offset=args.tide_datum_offset)
    print(f"Loaded {len(tide_times)} tide model points (moved {tide_shift:+.3f} m onto NAVD88)")

    if args.start_date:
        start = datetime.strptime(args.start_date, "%Y-%m-%d")
        mask = [t >= start for t in spline_times]
        spline_times = [t for t, m in zip(spline_times, mask) if m]
        spline_values = spline_values[mask]
        tide_mask = [t >= start for t in tide_times]
        tide_times = [t for t, m in zip(tide_times, tide_mask) if m]
        tide_values = tide_values[tide_mask]

    if args.end_date:
        end = datetime.strptime(args.end_date, "%Y-%m-%d")
        mask = [t <= end for t in spline_times]
        spline_times = [t for t, m in zip(spline_times, mask) if m]
        spline_values = spline_values[mask]
        tide_mask = [t <= end for t in tide_times]
        tide_times = [t for t, m in zip(tide_times, tide_mask) if m]
        tide_values = tide_values[tide_mask]

    fig, ax = plt.subplots(figsize=(16, 6))

    # Constant offset between the two series, measured from the
    # overlapping data rather than hardcoded -- it has already
    # changed twice as processing was corrected, and a stale
    # constant would quietly make this plot dishonest.
    shift = 0.0
    if not args.no_align and len(tide_times) > 1:
        base = tide_times[0]
        tx = np.array([(t - base).total_seconds() for t in tide_times])
        order = np.argsort(tx)
        qx = np.array([(t - base).total_seconds() for t in spline_times])
        tx_sorted = tx[order]
        tide_at = np.interp(qx, tx_sorted, np.asarray(tide_values)[order],
                            left=np.nan, right=np.nan)
        # not across a gap in the tide: no model value to compare there
        lo = tx_sorted[np.clip(np.searchsorted(tx_sorted, qx, "right") - 1, 0, None)]
        hi = tx_sorted[np.clip(np.searchsorted(tx_sorted, qx, "left"), None, len(tx_sorted) - 1)]
        tide_at[hi - lo > tide_gap(tide_times).total_seconds()] = np.nan
        sv = np.asarray(spline_values, dtype=float)
        usable = np.isfinite(tide_at) & np.isfinite(sv)
        if usable.sum() > 10:
            shift = float(np.mean(sv[usable] - tide_at[usable]))

    plotted_values = (np.asarray(spline_values, dtype=float) - shift
                      if shift else spline_values)
    gnss_label = ("GNSS-IR water level (this station)" if not shift else
                  f"GNSS-IR water level (shifted {-shift:+.3f} m)")

    ax.plot(spline_times, plotted_values, color="tab:blue", linewidth=1.2,
            label=gnss_label)
    tide_times, tide_values = break_at_gaps(tide_times, tide_values, tide_gap(tide_times))
    ax.plot(tide_times, tide_values, color="tab:orange", linewidth=1.0,
            alpha=0.85, label=f"Tide model ({args.tide_value_col})")

    ax.set_xlabel("Date")
    ax.set_ylabel("Water level (m NAVD88)")
    if shift:
        subtitle = (f"(GNSS-IR shifted by {-shift:+.3f} m to remove the constant offset "
                    f"between the two vertical references, so shape and timing "
                    f"can be compared directly)")
    else:
        subtitle = ("(shown on a shared, unadjusted axis -- any constant vertical "
                    "offset between the two references is intentionally left visible, not corrected)")
    ax.set_title("GNSS-IR Water Level vs. Tide Model\n" + subtitle)
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

