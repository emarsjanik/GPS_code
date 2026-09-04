#!/usr/bin/env python3
"""
plot_7day.py

Rolling 7-day water level plot for public display.

Always shows the most recent 7 days present in the data, regardless
of date or month -- there is nothing to configure or update as time
passes.

DIFFERENCES FROM THE DIAGNOSTIC PLOTS

This one is public-facing, on a USGS page, so it is built to
different standards than the internal comparison plots:

  - No tide model. The comparison is a validation tool, not
    something a general reader needs, and showing two curves invites
    the question of which is "right".

  - Referenced to local mean sea level, not the geoid. The raw
    GNSS-IR water levels sit about 0.255 m below local MSL; a public
    plot that reads systematically low without explanation is
    misleading. The offset comes from station.json
    (water_level_msl_offset) rather than being hardcoded, because it
    has already been revised twice as processing was corrected, and
    a stale correction on a public page would be worse than none.

  - Labelled provisional. This is experimental GNSS-IR, not an
    accredited tide gauge, and the plot says so.

  - Gaps are left as gaps. The spline draws straight lines across
    missing data, which on a diagnostic plot is a recognizable
    artifact but on a public plot looks like a real, flat water
    level. Segments separated by more than an hour are broken.

Usage:
    python3 plot_7day.py \\
        --spline-file products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --output products/refl_code/Files/usgs/7_day_plot.png
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np


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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spline-file", required=True)
    p.add_argument("--output", default="7_day_plot.png")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--station-name", default=None,
                   help="shown in the title; read from station.json if omitted")
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

    t_sel = [t for t, _ in keep]
    v_sel = np.array([v for _, v in keep], dtype=float)

    offset = msl_offset(project_dir)
    v_plot = v_sel - offset

    station_name = args.station_name
    if station_name is None:
        try:
            d = json.loads((project_dir / "station" / "resources"
                            / "station.json").read_text())
            station_name = d.get("station_name") or "GNSS-IR station"
        except Exception:
            station_name = "GNSS-IR station"

    fig, ax = plt.subplots(figsize=(12, 5))

    for seg_t, seg_v in split_on_gaps(t_sel, list(v_plot)):
        ax.plot(seg_t, seg_v, color="#1f6fb4", linewidth=1.6, solid_capstyle="round")

    ax.axhline(0.0, color="#999999", linewidth=0.8, linestyle="--", zorder=0)

    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("Water level (m above local mean sea level)")
    ax.set_title(f"{station_name} \u2014 water level, last {args.days} days\n"
                 f"Measured by GNSS interferometric reflectometry",
                 fontsize=13)

    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
    fig.autofmt_xdate()

    span = f"{min(t_sel).strftime('%Y-%m-%d %H:%M')} to {newest.strftime('%Y-%m-%d %H:%M')} UTC"
    fig.text(0.01, 0.02,
             f"Provisional data, subject to revision. Derived from reflected GPS "
             f"signals, not an accredited tide gauge.\n{span}   |   "
             f"U.S. Geological Survey",
             fontsize=7.5, color="#555555", va="bottom")

    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(args.output, dpi=150)
    print(f"Wrote {args.output}")
    print(f"  {len(t_sel)} points, {span}")
    print(f"  MSL offset applied: {-offset:+.3f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
