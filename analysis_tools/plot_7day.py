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

  - Gaps are left as gaps. The spline draws straight lines across
    missing data, which on a diagnostic plot is a recognisable
    artifact but on a public plot looks like a real, flat water
    level. Segments separated by more than 90 minutes are broken.

  - The predicted tide is shown alongside. Two curves tracking
    closely demonstrate the measurement works in a way one curve
    cannot. Departures are labelled rather than left to be
    misread: a difference is not an error in either curve, it is
    the part of the water level that astronomy alone does not
    explain.

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
    p.add_argument("--departure-threshold", type=float, default=0.25,
                   help="metres; departures larger than this are annotated "
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

    # The most recent N days present in the data, not the last N
    # calendar days -- if processing is a day behind, the plot should
    # still show a full week rather than an empty strip.
    newest = max(times)
    cutoff = newest - timedelta(days=args.days)
    keep = [(t, v) for t, v in zip(times, values) if t >= cutoff]
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

        # Mark where the estimate departs from the prediction. This
        # is the part of the water level that the tide model does not
        # account for, and is the reason both curves are shown.
        tx = np.array([(t - tide_t[0]).total_seconds() for t in tide_t])
        qx = np.array([(t - tide_t[0]).total_seconds() for t in t_sel])
        order = np.argsort(tx)
        predicted = np.interp(qx, tx[order], np.asarray(tide_v)[order],
                              left=np.nan, right=np.nan)
        departure = v_plot - predicted
        big = np.isfinite(departure) & (np.abs(departure) >= args.departure_threshold)

        if big.any():
            # Annotate the single largest departure rather than every
            # point above the threshold, which would be unreadable.
            i = int(np.nanargmax(np.abs(np.where(big, departure, np.nan))))
            ax.annotate(
                f"{departure[i]:+.2f} metres from the prediction",
                xy=(t_sel[i], v_plot[i]),
                xytext=(0, 28 if departure[i] > 0 else -34),
                textcoords="offset points", ha="center", fontsize=9,
                color="#b3450c",
                arrowprops=dict(arrowstyle="->", color="#b3450c", linewidth=1.0))
            ax.plot([t_sel[j] for j in np.where(big)[0]],
                    [v_plot[j] for j in np.where(big)[0]],
                    linestyle="none", marker="o", markersize=3.5,
                    color="#b3450c", zorder=3)

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

    fig.text(0.01, 0.02,
             f"{explain}"
             f"Provisional data, subject to revision.   {span}   |   "
             f"U.S. Geological Survey",
             fontsize=7.5, color="#555555", va="bottom")

    fig.tight_layout(rect=(0, 0.09, 0.86, 1))
    fig.savefig(args.output, dpi=150)
    print(f"Wrote {args.output}")
    print(f"  {len(t_sel)} points, {span}")
    print(f"  MSL offset applied: {-offset:+.3f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
