#!/usr/bin/env python3
"""
plot_7day.py

Rolling 7-day water level plot for public display.

Always shows the most recent 7 days present in the data, regardless
of date or month -- there is nothing to configure or update as time
passes.

WRITTEN FOR A GENERAL AUDIENCE

This plot goes on a public page, so it is built to different
standards than the internal diagnostic plots:

  - No acronyms. "Reflected navigation satellite signals", not
    GNSS-IR. A reader should not need a glossary.

  - Local time, not UTC. The record is kept in UTC, but a tide curve
    labelled in UTC is four or five hours wrong for anyone reading
    it in Massachusetts, and most people will assume local time
    whatever the label says. zoneinfo applies the daylight-saving
    rules automatically, so the March and November changeovers need
    no intervention.

  - Referenced to local mean sea level. The raw water levels sit
    about a quarter of a metre below local mean sea level; a public
    plot that reads systematically low without explanation is
    misleading. The offset comes from station.json
    (water_level_msl_offset) rather than being hardcoded, because it
    has been revised more than once as processing was corrected, and
    a stale correction on a public page is worse than none.

  - "Estimated", not "measured". The water level is inferred from a
    reflected signal, not read off a staff gauge, and the caption
    says so.

  - Readings that fail an automatic quality check are left out
    (water_level_qc.py). In heavy surf the reflections scatter off
    waves and foam and the estimate can read metres too high; in the
    25-26 Sep 2026 storm it reached ~3 m above what the NOAA gauge at
    Chatham implied. Shown, those readings would be labelled as real
    departures from the tide. They become gaps, and the caption says
    so. --no-qc turns this off.

  - Gaps are left as gaps. The spline draws straight lines across
    missing data, which on a diagnostic plot is a recognisable
    artifact but on a public plot looks like a real, flat water
    level. Segments separated by more than 90 minutes are broken.

  - The predicted tide is shown alongside. Two curves tracking
    closely demonstrate the measurement works in a way one curve
    cannot. Departures are marked with a numbered dot, each with its
    own legend entry (when, and by how much), rather than left to be
    misread: a difference is not an error in either curve, it is
    the part of the water level that astronomy alone does not
    explain. They are marked only at high and low tide, comparing
    each measured high (or low) with the predicted one: halfway up
    or down, a tide arriving a little early or late opens a large
    gap between the curves that is about timing, not height.

Usage:
    python3 plot_7day.py \\
        --spline-file products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --output products/refl_code/Files/usgs/7_day_plot.png
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
# zoneinfo arrived in Python 3.9; this station's system python is
# 3.8 while its virtual environment is 3.10, and the script should
# work under either rather than depending on which interpreter the
# caller happens to use. The fallback is a fixed set of US Eastern
# daylight-saving rules -- correct for this site, and simpler than
# requiring a new dependency.
try:
    from zoneinfo import ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

# The record is kept in UTC. Displayed times are converted to the
# station's local zone so a reader sees the tide at the hour it
# actually happens.
DISPLAY_ZONE_LABEL = "Eastern Time"

MARK_COLOUR = "#b3450c"
MAX_MARKED = 8          # most high/low tides marked and listed in the legend


class _USEastern(tzinfo):
    """US Eastern time without zoneinfo.

    Daylight saving runs from the second Sunday in March to the first
    Sunday in November, which has been the rule since 2007. Only used
    when zoneinfo is unavailable; where it is available the system
    database is authoritative and handles any future rule change.
    """

    _STD = timedelta(hours=-5)
    _DST = timedelta(hours=-4)

    @staticmethod
    def _nth_sunday(year, month, n):
        d = datetime(year, month, 1)
        # weekday(): Monday is 0, Sunday is 6
        first_sunday = 1 + (6 - d.weekday()) % 7
        return datetime(year, month, first_sunday + 7 * (n - 1), 2)

    def _is_dst(self, dt):
        start = self._nth_sunday(dt.year, 3, 2)
        end = self._nth_sunday(dt.year, 11, 1)
        naive = dt.replace(tzinfo=None)
        return start <= naive < end

    def utcoffset(self, dt):
        return self._DST if self._is_dst(dt) else self._STD

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self._is_dst(dt) else "EST"


DISPLAY_ZONE = (ZoneInfo("America/New_York") if _HAVE_ZONEINFO
                else _USEastern())
_UTC_ZONE = ZoneInfo("UTC") if _HAVE_ZONEINFO else timezone.utc


def load_spline(path: Path):
    """Reads gnssrefl's evenly-sampled spline output.

    Column 9 is the water level (orthometric height minus reflector
    height). Rows carrying gnssrefl's 999 no-data sentinel are
    dropped rather than plotted.
    """
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
            if abs(v) > 900:          # gnssrefl's no-data sentinel
                continue
            times.append(dt)
            values.append(v)
    return times, np.asarray(values, dtype=float)


def load_tide(path: Path, time_col: str, value_col: str):
    """Reads the tide model spreadsheet. Returns ([], []) rather than
    raising if anything is wrong -- the water level is the point of
    the plot, and it is better to draw it without the prediction
    than not at all."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        header = [c.value for c in ws[1]]
        ti, vi = header.index(time_col), header.index(value_col)
        times, values = [], []
        for row in ws.iter_rows(min_row=2, values_only=True):
            t = row[ti]
            if not isinstance(t, datetime):
                continue
            v = row[vi]
            if isinstance(v, (int, float)):
                times.append(t)
                values.append(float(v))
        wb.close()
        return times, values
    except Exception as exc:
        print(f"  (tide model not plotted: {exc})")
        return [], []


def msl_offset(project_dir: Path) -> float:
    """Offset between this station's water levels and local mean sea
    level, from station.json. Zero if unset -- an unset offset should
    produce an unshifted plot, not a guess."""
    path = project_dir / "station" / "resources" / "station.json"
    try:
        d = json.loads(path.read_text())
        v = d.get("water_level_msl_offset")
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _turning_points(secs, values, half_window_s=3 * 3600, min_turn_m=0.1,
                    max_step_s=90 * 60):
    """High and low tides in a series: [(index, "high" | "low"), ...].

    A reading is a high (low) tide if it is the largest (smallest)
    within +/- half_window_s -- about a quarter of a tidal cycle, so
    small wiggles on the way up or down do not count -- AND the level
    falls (rises) by at least min_turn_m on BOTH sides within that
    window, with readings on both sides no more than max_step_s away
    (the gap used by split_on_gaps). The last two conditions stop the
    cut end of a line at a data gap, which is also the largest nearby
    value, from being taken for a turning point.
    """
    secs = np.asarray(secs, dtype=float)
    values = np.asarray(values, dtype=float)
    out = []
    for i in range(len(values)):
        v = values[i]
        if not np.isfinite(v):
            continue
        # both neighbours present and not across a gap: the line
        # actually continues through this reading
        if (i == 0 or i == len(values) - 1
                or not np.isfinite(values[i - 1]) or not np.isfinite(values[i + 1])
                or secs[i] - secs[i - 1] > max_step_s or secs[i + 1] - secs[i] > max_step_s):
            continue
        near = (np.abs(secs - secs[i]) <= half_window_s) & np.isfinite(values)
        before = near & (secs < secs[i])
        after = near & (secs > secs[i])
        if not before.any() or not after.any():
            continue
        if v >= values[near].max():
            kind = "high"
            turns = (values[before].min() <= v - min_turn_m and
                     values[after].min() <= v - min_turn_m)
        elif v <= values[near].min():
            kind = "low"
            turns = (values[before].max() >= v + min_turn_m and
                     values[after].max() >= v + min_turn_m)
        else:
            continue
        if not turns:
            continue
        # a flat top gives several equal readings; keep the first
        if out and out[-1][1] == kind and secs[i] - secs[out[-1][0]] <= half_window_s:
            continue
        out.append((i, kind))
    return out


def peak_departures(obs_secs, obs_values, tide_secs, tide_values, max_shift_s=3 * 3600):
    """Measured minus predicted height at each high and low tide.

    Each measured high (low) is paired with the nearest predicted high
    (low) within max_shift_s. Returns [(index, kind, difference), ...].
    """
    predicted = _turning_points(tide_secs, tide_values)
    tide_secs = np.asarray(tide_secs, dtype=float)
    out = []
    for i, kind in _turning_points(obs_secs, obs_values):
        match = [j for j, k in predicted
                 if k == kind and abs(tide_secs[j] - obs_secs[i]) <= max_shift_s]
        if not match:
            continue
        j = min(match, key=lambda j: abs(tide_secs[j] - obs_secs[i]))
        out.append((i, kind, float(obs_values[i] - tide_values[j])))
    return out


def split_on_gaps(times, values, max_gap_minutes=90):
    """Breaks the series wherever data is missing.

    Without this, matplotlib joins the points either side of a gap
    with a straight line. On a diagnostic plot that reads as an
    obvious artifact; on a public plot it reads as a real, flat
    water level, which is worse than showing nothing.
    """
    if not times:
        return []
    segments, cur_t, cur_v = [], [times[0]], [values[0]]
    for i in range(1, len(times)):
        if (times[i] - times[i - 1]) > timedelta(minutes=max_gap_minutes):
            segments.append((cur_t, cur_v))
            cur_t, cur_v = [], []
        cur_t.append(times[i])
        cur_v.append(values[i])
    if cur_t:
        segments.append((cur_t, cur_v))
    return segments


def to_local(times):
    """UTC timestamps to the display zone. Applied only after the
    7-day window has been chosen, so the window boundary is still
    decided on the underlying UTC times and does not shift by the
    offset."""
    return [t.replace(tzinfo=_UTC_ZONE).astimezone(DISPLAY_ZONE)
            for t in times]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spline-file", required=True)
    p.add_argument("--output", default="7_day_plot.png")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--station-name", default=None,
                   help="shown in the title; read from station.json if omitted")
    p.add_argument("--tide-file", default=None,
                   help="tide model spreadsheet; read from station.json if omitted")
    p.add_argument("--tide-value-col", default=None)
    p.add_argument("--tide-time-col", default=None)
    p.add_argument("--no-tide", action="store_true",
                   help="plot the water level alone, without the predicted tide")
    p.add_argument("--no-qc", action="store_true",
                   help="plot every reading, without the automatic quality check")
    p.add_argument("--gauge-cache", default=None,
                   help="CSV cache of the NOAA Chatham gauge used by the quality "
                        "check (default: next to --output)")
    p.add_argument("--departure-threshold", type=float, default=0.25,
                   help="metres; high or low tides that differ from the predicted "
                        "ones by more than this are marked "
                        "(default 0.25, about 2.8 sigma at this station)")
    args = p.parse_args()

    spline_path = Path(args.spline_file)
    project_dir = Path(__file__).resolve().parent
    if project_dir.name == "analysis_tools":
        project_dir = project_dir.parent

    times, values = load_spline(spline_path)
    if not times:
        print("No usable spline data found.")
        return 1

    # Quality check on the whole record (the gauge fit uses the last
    # 30 days), before the window is chosen. Failed readings become
    # NaN, which breaks the line there and keeps them out of the
    # departure labels.
    n_failed_window = 0
    n_setup_window = 0
    n_runup_window = 0
    runup_values = np.full(len(values), np.nan)
    qc_line = None
    if not args.no_qc:
        try:
            from water_level_qc import GAUGE_STATION, is_runup, public_mask, qc_series
            cache = (Path(args.gauge_cache) if args.gauge_cache else
                     Path(args.output).resolve().parent / f"gauge_{GAUGE_STATION}.csv")
            flags, ep, summarize, qc_reasons = qc_series(times, values, cache)
            # Failed readings, plus the spline next to them and short
            # pieces left between failures (see water_level_qc.py).
            # Readings kept as TOTAL water level (wave runup in heavy
            # surf) are not still-water levels: they come off the main
            # line and are drawn as their own dotted series.
            runup = is_runup(qc_reasons)
            runup_values = np.where(runup, values, np.nan)
            hidden = ~public_mask(ep, flags, qc_reasons)
            values = np.where(hidden, np.nan, values)
            win_start = max(times) - timedelta(days=args.days)
            n_failed_window = int(sum(1 for t, h, u in zip(times, hidden, runup)
                                      if h and not u and t >= win_start))
            n_runup_window = int(sum(1 for t, u in zip(times, runup) if u and t >= win_start))
            n_setup_window = int(sum(1 for t, h, r in zip(times, hidden, qc_reasons)
                                     if not h and t >= win_start and "possible_setup" in r))
            qc_line = (summarize(win_start.replace(tzinfo=timezone.utc).timestamp())
                       + f"; {n_failed_window} left blank, {n_runup_window} drawn as total "
                         f"water level on the plot")
        except Exception as exc:          # never lose the plot over the check
            print(f"  QC not applied: {exc}")

    # The most recent N days present in the data, not the last N
    # calendar days -- if processing is a day behind, the plot should
    # still show a full week rather than an empty strip.
    newest = max(times)
    cutoff = newest - timedelta(days=args.days)
    keep = [(t, v) for t, v in zip(times, values) if t >= cutoff]
    runup_sel = np.array([u for t, u in zip(times, runup_values) if t >= cutoff], dtype=float)
    if not keep:
        print("No data in the requested window.")
        return 1

    t_utc = [t for t, _ in keep]
    v_sel = np.array([v for _, v in keep], dtype=float)

    offset = msl_offset(project_dir)
    v_plot = v_sel - offset

    station_name = args.station_name
    if station_name is None:
        try:
            d = json.loads((project_dir / "station" / "resources"
                            / "station.json").read_text())
            station_name = d.get("station_name") or "This station"
        except Exception:
            station_name = "This station"

    t_sel = to_local(t_utc)

    fig, ax = plt.subplots(figsize=(13.5, 5))

    # Label only the first segment: the series is broken wherever
    # data is missing, and labelling each piece would repeat the
    # legend entry once per gap.
    for seg_i, (seg_t, seg_v) in enumerate(split_on_gaps(t_sel, list(v_plot))):
        ax.plot(seg_t, seg_v, color="#1f6fb4", linewidth=1.5,
                solid_capstyle="round", zorder=2,
                label="Estimated water level" if seg_i == 0 else None)

    # Total water level in heavy surf (tide + surge + setup + wave runup),
    # dotted so it cannot be mistaken for the still-water level.
    if np.isfinite(runup_sel).any():
        r_ok = np.isfinite(runup_sel)
        r_t = [t for t, ok in zip(t_sel, r_ok) if ok]
        r_v = list(runup_sel[r_ok] - offset)
        for seg_i, (seg_t, seg_v) in enumerate(split_on_gaps(r_t, r_v)):
            ax.plot(seg_t, seg_v, color="#7d3c98", linewidth=1.6, linestyle=":",
                    marker="o", markersize=2.5, zorder=2,
                    label="Total water level in heavy surf\n(includes waves running up the beach)"
                    if seg_i == 0 else None)

    # Predicted tide, drawn beneath so the estimate stays visually
    # primary.
    tide_t, tide_v = [], []
    if not args.no_tide:
        tide_file = args.tide_file
        tide_col = args.tide_value_col
        tide_time_col = args.tide_time_col or "time"
        if tide_file is None or tide_col is None:
            try:
                cfg = json.loads((project_dir / "station" / "resources"
                                  / "station.json").read_text())
                tide_file = tide_file or cfg.get("tide_model_file")
                tide_col = tide_col or cfg.get("tide_model_value_column")
                tide_time_col = args.tide_time_col or cfg.get(
                    "tide_model_time_column") or "time"
            except Exception:
                pass

        if tide_file and tide_col and Path(tide_file).exists():
            all_t, all_v = load_tide(Path(tide_file), tide_time_col, tide_col)
            keep_t = [(t, v) for t, v in zip(all_t, all_v)
                      if cutoff <= t <= newest]
            tide_t = to_local([t for t, _ in keep_t])
            tide_v = [v for _, v in keep_t]

    if tide_t:
        ax.plot(tide_t, tide_v, color="#d9822b", linewidth=1.6, alpha=0.65,
                zorder=1, label="Predicted tide")

        # Mark where the estimate departs from the prediction -- the
        # part of the water level the tide model does not account
        # for, and the reason both curves are shown. Compared at high
        # and low tide only (see the module docstring).
        t_ref = min(tide_t[0], t_sel[0])
        tx = np.array([(t - t_ref).total_seconds() for t in tide_t])
        qx = np.array([(t - t_ref).total_seconds() for t in t_sel])
        order = np.argsort(tx)
        marks = [(i, kind, d) for i, kind, d in
                 peak_departures(qx, v_plot, tx[order], np.asarray(tide_v, dtype=float)[order])
                 if abs(d) >= args.departure_threshold]

        if marks:
            # Every marked tide gets a number beside its dot and its
            # own legend entry saying when it was and by how much it
            # differed. At most MAX_MARKED (the largest), so a stormy
            # week does not bury the legend; numbered in time order.
            marks = sorted(sorted(marks, key=lambda m: -abs(m[2]))[:MAX_MARKED])
            for k, (i, kind, d) in enumerate(marks, start=1):
                ax.plot([t_sel[i]], [v_plot[i]], linestyle="none", marker="o",
                        markersize=4.5, color=MARK_COLOUR, zorder=3)
                ax.annotate(str(k), xy=(t_sel[i], v_plot[i]),
                            xytext=(0, 7 if kind == "high" else -15),
                            textcoords="offset points", ha="center",
                            fontsize=8.5, fontweight="bold", color=MARK_COLOUR)
                when = t_sel[i].strftime("%b %d, %I:%M %p").replace(" 0", " ")
                ax.plot([], [], linestyle="none", marker=f"${k}$", markersize=7,
                        color=MARK_COLOUR,
                        label=f"{when}\n{abs(d):.2f} m {'above' if d > 0 else 'below'} "
                              f"predicted {kind} tide")

    ax.axhline(0.0, color="#999999", linewidth=0.8, linestyle="--", zorder=0)

    ax.set_xlabel(f"Date ({DISPLAY_ZONE_LABEL})")
    ax.set_ylabel("Water level in metres\nabove local mean sea level")
    ax.set_title(f"{station_name} water level, last {args.days} days",
                 fontsize=14, pad=12)

    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_locator(mdates.DayLocator(tz=DISPLAY_ZONE))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=DISPLAY_ZONE))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18],
                                                  tz=DISPLAY_ZONE))
    fig.autofmt_xdate()

    # Outside the axes, vertically centred, so it never covers data.
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5),
              fontsize=9, framealpha=0.0, borderpad=0.4,
              labelspacing=0.6, handlelength=1.8)

    span = (f"{t_sel[0].strftime('%Y-%m-%d %H:%M')} to "
            f"{t_sel[-1].strftime('%Y-%m-%d %H:%M')} {DISPLAY_ZONE_LABEL}")

    if tide_t:
        explain = (
            f"The blue line is the water level at {station_name} estimated "
            f"using reflected navigation satellite signals. The orange line "
            f"is the\npredicted tide. Differences between them may be caused "
            f"by oceanographic and atmospheric effects or measurement error.\n")
    else:
        explain = (
            f"The blue line is the water level at {station_name} estimated "
            f"using reflected navigation satellite signals.\n")

    if n_runup_window:
        explain += ("The dotted line is the total water level at the shore in heavy surf: tide, "
                    "storm surge and waves running up the beach.\n")
    if n_setup_window:
        explain += ("In heavy surf breaking waves raise the water level at the beach "
                    "(wave setup) above what the tide and a harbour tide gauge show.\n")
    if n_failed_window:
        explain += ("Periods where the estimate failed an automatic quality check "
                    "(for example in heavy surf) are left blank.\n")

    fig.text(0.01, 0.02,
             f"{explain}"
             f"Provisional data, subject to revision.   {span}   |   "
             f"U.S. Geological Survey",
             fontsize=7.5, color="#555555", va="bottom")

    fig.tight_layout(rect=(0, 0.09 + 0.02 * (bool(n_failed_window) + bool(n_setup_window)
                                          + bool(n_runup_window)), 0.86, 1))
    fig.savefig(args.output, dpi=150)
    print(f"Wrote {args.output}")
    print(f"  {len(t_sel)} points, {span}")
    print(f"  MSL offset applied: {-offset:+.3f} m")
    if qc_line:
        print(f"  {qc_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
