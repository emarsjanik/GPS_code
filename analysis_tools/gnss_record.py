#!/usr/bin/env python3
"""
gnss_record.py

The one place this station's water-level RECORD -- every year of it --
is put together. Everything downstream reads one file,

    products/refl_code/Files/<sta>/<sta>_spline_out.txt

the public 7-day plot, the QC, export_twl, the S3 monthly archive,
ultra_rapid_check's comparison, and the waterline/DEM pipeline in
caco05-waterline, which gives every camera frame its water level from
this file and DROPS any frame the file does not cover.

WHY

gnssrefl's subdaily fits one stretch of per-day results and writes it
to that fixed name, replacing what was there. process_and_plot.sh used
to fit "this calendar year" (YEAR=$(date +%Y)), so on 2 January the
record would have become one day long: every frame of the year before
loses its water level, and the DEM, rebuilt from the whole archive,
loses them with it. Nothing fails loudly -- subdaily exits 0 even when
it writes nothing.

HOW

  results  $REFL_CODE/<year>/results/<sta>/<doy>.txt of EVERY year,
           from station.json's optional "gnss_record_first_day" on
           (--since overrides it): the source of truth. Nothing here
           changes them.

  fit      Each night subdaily fits from 1 January of the oldest year
           that is not frozen (plus PAD_DAYS of the year before, so a
           year never starts at the edge of a fit) to the newest
           result, across the year boundary with -year_end, into its
           own folder Files/<sta>/fit/ (-subdir). At most a year and a
           few weeks, so the nightly run time is bounded. fit/ holds
           only the last run's gnssrefl output; its main plot is copied
           to Files/<sta>/<sta>_last.png.
           subdaily crashes when its azimuth window leaves a year of
           the range empty, which is exactly what a short first day of
           January (or a short last pad day) does. An edge year with
           fewer than MIN_EDGE_ARCS arcs in the window is therefore left
           out of tonight's fit -- the pad is dropped, or the new year
           waits a night -- instead of refitting the whole record
           without the window. A PAST year that thin (the record's
           first year beginning on 31 December, say) is fitted by
           itself instead -- with the window if that works, else with
           the fallback settings, said so in its header -- and frozen
           at once; tonight's fit starts after it.

  frozen   FREEZE_AFTER_DAYS into a new year (counted on the fit's own
           end), the old year's rows of that night's fit are kept as
               Files/<sta>/<sta>_<year>_spline_frozen.txt
           with the subdaily settings, a fingerprint of that year's
           results and its row count in the header, and the year is no
           longer refitted. A frozen year whose results change (a
           recovered or deleted day), whose settings change, or whose
           rows no longer match the count is refitted and frozen again
           by itself. To force it: delete the file.

  record   <sta>_spline_out.txt = the frozen years, then tonight's fit
           from 1 January of the first year not frozen. Before anything
           is written it is checked: it must cover the fit's range, must
           not START later than the current file, and must not lose more
           than MAX_LOST_DAYS of the days the current file covers -- so a
           shorter record can never silently replace a longer one (days
           before gnss_record_first_day do not count: setting it is how
           the record is shortened on purpose). Only then are due years
           frozen (from tonight's fit, so tomorrow's record equals
           tonight's) and the record replaced, atomically, 0644.
           Levels of a frozen year written with another antenna
           height are rewritten to the fit's (level = Hortho - RH; RH
           does not depend on it -- a datum change, not an antenna move).
           The <sta>_<year>_subdaily_edit.txt files of the years the fit
           covers in full are copied next to it (filter_month.py and the
           reflection audits read those). A freshly frozen year leaves
           fit/s3_upload_year_<year> for s3UploadTimeseries.sh, which
           re-uploads that year's months once.

  support  A water level every 30 minutes is not a measurement every 30
           minutes. subdaily writes a level wherever the arcs it kept to
           the end (those within 3 sigma of its final spline) are no more
           than gap_min_val (6 h) apart, and before fitting it replaces
           every spacing over 5 h between arcs with straight-line fake
           data (flipit3) -- so a level can lie hours from any arc, a
           storm high tide drawn as a chord, and nothing in the spline
           says so. <sta>_spline_support.txt, next to the record, has one
           row per record row (the same MJD): the hours to the nearest
           kept arc, the kept arcs within +-1 h, and the kept-arc gap the
           row sits in -- subdaily drops a row exactly when that gap is
           over gap_min_val (except next to the newest arc, which it never
           tests), so the column also says what a smaller gap_min_val
           would drop: the fit itself does not change. The kept arcs
           are tonight's fit/<sta>_<years>_subdaily_edit.txt.withrhdotIF;
           a frozen year keeps the support it was frozen with in
           <sta>_<year>_spline_support_frozen.txt (a year frozen before
           this file existed: "nan", not known). station.json's optional
           "subdaily_gap_min_val" (hours) is passed to subdaily as
           -gap_min_val, with the fallback settings too; unset, gnssrefl's
           6 h applies. Changing it refits the frozen years once.

  status   fit/record_status.json says how the last update went
           (diagnostics/station_health.py reports it in the daily mail),
           including any year whose edit file subdaily did not write,
           the gnssrefl version that ran, per UTC day of the last
           two weeks the arcs kept with their median Amp and PkNoise
           (arc_stats; a storm shows as a drop), and gap_support: of the
           newest 7 days' levels (the public plot's week), how many are
           more than 2 h from a kept arc.

USAGE (process_and_plot.sh, every night)

    gnss_record.py --refl-code $REFL_CODE --station usgs summary
    gnss_record.py --refl-code $REFL_CODE --station usgs update \\
        --settings "-rhdot True -knots 8 -azim1 35 -azim2 125" \\
        --fallback-settings "-rhdot True -knots 8"

By hand (gnssrefl_venv active; --refl-code defaults to $REFL_CODE, else
products/refl_code of this project), e.g. to accept a record that lost
days on purpose -- the same settings as process_and_plot.sh, or the
frozen years are refitted (subdaily_gap_min_val is added from
station.json here too):

    cd /home/argus_user/GNSS/v4.1 && source gnssrefl_venv/bin/activate
    python3 analysis_tools/gnss_record.py --refl-code products/refl_code \\
        --station usgs update --settings "-rhdot True -knots 8 -azim1 35 -azim2 125" \\
        --fallback-settings "-rhdot True -knots 8" --allow-shrink
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PAD_DAYS = 7             # days of the year before at the start of each fit
FREEZE_AFTER_DAYS = 14   # how far past 31 Dec the fit must reach to freeze that year
MIN_EDGE_ARCS = 24       # arcs in the azimuth window an edge year of a fit needs
MAX_LOST_DAYS = 2        # days of the current record a new one may lose (refit noise)
LIVE_SPAN_WARN_DAYS = 430
FIT_SUBDIR = "fit"       # subdaily's own output: Files/<sta>/fit/
SETTINGS_TAG = "%  subdaily settings:"
RESULTS_TAG = "%  results:"
STATUS_NAME = "record_status.json"
UPLOAD_MARK = "s3_upload_year_"
FIT_LOG = "subdaily.log"
MJD_EPOCH = date(1858, 11, 17)
AZ_COL = 5               # gnssir results: year doy RH sat UTC Azim ...
AMP_COL = 6              # ... Amp eminO emaxO NumbOf freq rise EdotF PkNoise ...
PKNOISE_COL = 13
ARC_STATS_DAYS = 14      # days of arc statistics kept in record_status.json
KEPT_SUFFIX = "_subdaily_edit.txt.withrhdotIF"   # subdaily's arcs kept to the end
MJD_COL = 15             # ... their MJD column (as in the results)
SUPPORT_HALF_WINDOW_H = 1.0   # kept arcs counted within +-1 h of each level
UNSUPPORTED_H = 2.0      # a level further than this from every kept arc is unsupported
SUPPORT_DAYS = 7         # gap_support in the status: the newest 7 days, as the public plot
FAKE_FILL_H = 5.0        # gnssrefl 4.1.5 flipit3 fills spacings over 5 h with fake data
GAP_MIN_VAL_H = 6.0      # gnssrefl 4.1.5's -gap_min_val
PROJECT_DIR = Path(__file__).resolve().parent.parent
STATION_JSON = PROJECT_DIR / "station" / "resources" / "station.json"


def mjd(d: date) -> int:
    return (d - MJD_EPOCH).days


def mjd_date(m: float) -> date:
    return MJD_EPOCH + timedelta(days=int(m + 1e-6))


def row_date(line: str) -> date:
    c = line.split()
    return date(int(c[2]), int(c[3]), int(c[4]))


def record_first_day(station_json: Path | str = STATION_JSON) -> date | None:
    """station.json's optional "gnss_record_first_day" (YYYY-MM-DD): results
    before it (installation tests, an earlier antenna setup) are not part
    of the record. None when unset or there is no station.json; a value
    that is not a date raises ValueError."""
    try:
        value = json.loads(Path(station_json).read_text()).get("gnss_record_first_day")
    except (OSError, ValueError, AttributeError):
        return None
    return date.fromisoformat(str(value)) if value else None


def parse_gap_min_val(value) -> float | None:
    """A "subdaily_gap_min_val" value: None when unset, else a positive
    number of hours; anything else raises ValueError."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a number of hours")
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number of hours") from None
    if not (math.isfinite(hours) and hours > 0):
        raise ValueError(f"{value!r} is not a positive number of hours")
    return hours


def subdaily_gap_min_val(station_json: Path | str = STATION_JSON) -> float | None:
    """station.json's optional "subdaily_gap_min_val" (hours): passed to
    subdaily as -gap_min_val, the longest spacing of kept arcs it still
    draws a level across. Unset (or no station.json): None, and gnssrefl's
    own 6 h applies. Lowering it to 5 h drops the stretches gnssrefl fills
    with straight-line fake data before fitting (flipit3: spacings over
    5 h; "support" in this file's docstring). A value that is not a
    positive number raises ValueError."""
    try:
        cfg = json.loads(Path(station_json).read_text())
    except (OSError, ValueError):
        return None
    return parse_gap_min_val(cfg.get("subdaily_gap_min_val")) if isinstance(cfg, dict) else None


def with_gap_min_val(settings: str | None, hours: float | None) -> str | None:
    """These subdaily settings with -gap_min_val <hours> added -- unless
    hours is None, or they give one already (that one is used)."""
    if hours is None or not settings or "-gap_min_val" in shlex.split(settings):
        return settings
    return f"{settings} -gap_min_val {hours:g}"


def gap_min_val_hours(settings: str) -> float:
    """The -gap_min_val (hours) these subdaily settings run with."""
    a = shlex.split(settings or "")
    try:
        return float(a[a.index("-gap_min_val") + 1])
    except (ValueError, IndexError):
        return GAP_MIN_VAL_H


# ----------------------------------------------------------------
# Results: every year
# ----------------------------------------------------------------

def _data_lines(path: Path):
    with open(path, errors="replace") as f:
        for s in f:
            if s.strip() and not s.startswith("%"):
                yield s


def _has_data(path: Path) -> bool:
    """A results file with at least one retrieval (subdaily skips empty ones)."""
    try:
        return path.stat().st_size > 0 and next(_data_lines(path), None) is not None
    except OSError:
        return False


def results_days(refl_code: Path | str, sta: str, since: date | None = None) -> dict[date, Path]:
    """{day: results file} for every day of every year with a non-empty
    $REFL_CODE/<year>/results/<sta>/<doy>.txt, oldest first."""
    found = {}
    for ydir in Path(refl_code).glob("[0-9][0-9][0-9][0-9]"):
        year = int(ydir.name)
        for f in (ydir / "results" / sta).glob("[0-9][0-9][0-9].txt"):
            d = date(year, 1, 1) + timedelta(days=int(f.stem) - 1)
            if d.year == year and (since is None or d >= since) and _has_data(f):
                found[d] = f
    return dict(sorted(found.items()))


def azimuth_window(settings: str) -> tuple[float, float] | None:
    a = shlex.split(settings)
    try:
        return float(a[a.index("-azim1") + 1]), float(a[a.index("-azim2") + 1])
    except (ValueError, IndexError):
        return None


def arcs_in_window(path: Path, window) -> int:
    n = 0
    for s in _data_lines(path):
        if window is None:
            n += 1
            continue
        c = s.split()
        try:
            if window[0] <= float(c[AZ_COL]) <= window[1]:
                n += 1
        except (IndexError, ValueError):
            continue
    return n


def fingerprint(files) -> str:
    """What a frozen year was made from: names and contents of its results."""
    h = hashlib.sha1()
    for p in sorted(files, key=lambda p: p.name):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def subdaily_range(days) -> tuple[int, int, int, int]:
    """subdaily's (year, doy1, year_end, doy2) for these days.

    subdaily reads doy1..31 Dec of the first year, every day of any
    year in between, and 1..doy2 of the last, and exits 0 WITHOUT
    writing anything if one of those years has no results -- so that
    is refused here instead."""
    days = sorted(days)
    if not days:
        raise ValueError("no results days")
    first, last = days[0], days[-1]
    have = {d.year for d in days}
    empty = [y for y in range(first.year, last.year + 1) if y not in have]
    if empty:
        raise ValueError(f"no results at all in {empty}: subdaily cannot fit "
                         f"{first} to {last} in one run")
    return first.year, first.timetuple().tm_yday, last.year, last.timetuple().tm_yday


# ----------------------------------------------------------------
# Spline files
# ----------------------------------------------------------------

def read_spline(path: Path) -> tuple[list[str], list[tuple[float, str]]]:
    """(header lines, [(MJD, data line)]) of a gnssrefl spline file."""
    head, rows = [], []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("%"):
                if not rows:
                    head.append(line)
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            try:
                float(parts[8])
                rows.append((float(parts[0]), line if line.endswith("\n") else line + "\n"))
            except ValueError:
                continue
    return head, rows


def header_value(head: list[str], tag: str) -> str | None:
    for line in head:
        if line.startswith(tag):
            return line[len(tag):].strip()
    return None


def header_hortho(head: list[str]) -> float | None:
    """The antenna height gnssrefl wrote ("... Hortho (m) is   19.014")."""
    for line in head:
        if "Hortho (m) is" in line:
            try:
                return float(line.rsplit("is", 1)[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None


def rehortho(line: str, h_from: float | None, h_to: float | None) -> str:
    """A spline row's level (column 9) recomputed for antenna height h_to.
    level = Hortho - RH and RH (column 2) does not depend on Hortho, so
    this is exactly what gnssrefl would have written. 999 gap rows and
    unknown heights are left alone."""
    if h_from is None or h_to is None or abs(h_from - h_to) < 0.0005:
        return line
    parts = line.split()
    level = float(parts[8])
    if abs(level) > 900:
        return line
    old = parts[8]
    new = f"{h_to - float(parts[1]):.3f}".rjust(len(old))
    i = line.rindex(old)
    return line[:i] + new + line[i + len(old):]


def write_atomic(path: Path, lines) -> None:
    """Replace path in one step, mode 0644 whatever the umask, flushed to
    disk first, so a reader (the waterline cron) never sees a half-written
    file, even after a power cut."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def copy_atomic(src: Path, dest: Path) -> None:
    """write_atomic for a copy: the same one-step replace, 0644 and fsync."""
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copyfile(src, tmp)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)


# ----------------------------------------------------------------
# Frozen years
# ----------------------------------------------------------------

def frozen_path(files_dir: Path, sta: str, year: int) -> Path:
    return files_dir / f"{sta}_{year}_spline_frozen.txt"


def frozen_text(head, settings: str, fp: str, rows: list[str], note: str) -> list[str]:
    return (head[:1] + [f"{SETTINGS_TAG} {settings}\n",
                        f"{RESULTS_TAG} {fp} rows {len(rows)}\n",
                        f"%  {note} (analysis_tools/gnss_record.py)\n"]
            + head[1:] + rows)


def frozen_ok(path: Path, settings: str, fp: str) -> bool:
    """A frozen year can be used as it is: made with these subdaily
    settings, from exactly these results, and still holding every row
    it was written with (a truncated or hand-edited file is refitted)."""
    try:
        head, rows = read_spline(path)
    except OSError:
        return False
    s, r = header_value(head, SETTINGS_TAG), header_value(head, RESULTS_TAG)
    if s is None or r is None or s.split() != settings.split():
        return False
    return r.split() == [fp, "rows", str(len(rows))] and len(rows) > 0


# ----------------------------------------------------------------
# Gap support: how far each level is from the arcs under it
# ----------------------------------------------------------------
#
# subdaily is the RHdot-corrected cubic spline of Larson, Ray & Williams
# (2017, J. Atmos. Oceanic Technol. 34, 295-307) as gnssrefl implements
# it (Larson 2024, GPS Solutions 28, 165). In 4.1.5 (rhdot_correction2)
# the final spline is fitted to the arcs left after the first 3-sigma
# pass, with every spacing over 5 h first filled with straight-line fake
# points an hour apart (sd_libs.flipit3; the first real arc after each
# such gap is left out of the fit too). The arcs within 3 sigma of that
# spline are the "kept" ones: written to <...>_subdaily_edit.txt
# .withrhdotIF and given to the gap test, which leaves out the 30-minute
# levels strictly inside any spacing of kept arcs over gap_min_val
# (sd_libs.RH_ortho_plot2) -- except the spacing before the last kept
# arc, which it never tests. With knots every 3 h (-knots 8), a level
# more than 2 h from the nearest kept arc sits in a gap longer than a
# knot interval: the spline there is set by its continuity with the data
# either side (and, over 5 h, by the fake chord), not by a measurement.
# In synthetic tests of the 4.1.5 code (20 Oct 2026), a 5-6 h gap at high
# or low water that survived the 3-sigma pass was drawn 0.5-1.1 m off.

SUPPORT_HEAD = [
    "%  hours_to_arc: hours from this level to the nearest arc subdaily kept (within 3 sigma of\n",
    "%  its final spline); arcs_1h: kept arcs within +-1 h; gap_h: hours between the kept arcs\n",
    "%  either side (0 on an arc, nan before the first or after the last). subdaily leaves out\n",
    "%  the levels with gap_h over gap_min_val (6 h unless -gap_min_val), except next to the last\n",
    "%  arc, and fills spacings over 5 h with straight-line fake data before fitting. All three\n",
    "%  nan: not known (a year frozen before this file existed, or no kept-arc file).\n",
    "%  MJD              YYYY  MM  DD  HH  MM  SS  hours_to_arc  arcs_1h     gap_h\n",
    "%  (1)               (2)   (3) (4) (5) (6) (7)          (8)      (9)      (10)\n",
]


def support_path(files_dir: Path, sta: str) -> Path:
    return files_dir / f"{sta}_spline_support.txt"


def frozen_support_path(files_dir: Path, sta: str, year: int) -> Path:
    return files_dir / f"{sta}_{year}_spline_support_frozen.txt"


def kept_arcs(fit_dir: Path, sta: str) -> list[float] | None:
    """MJDs, sorted, of the arcs subdaily kept to the end of the run whose
    output is in fit_dir (fit/<sta>_<years>_subdaily_edit.txt.withrhdotIF).
    None when it wrote no such file, or one without arcs."""
    found = sorted(fit_dir.glob(f"{sta}_*{KEPT_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    if not found:
        return None
    out = []
    for s in _data_lines(found[-1]):
        try:
            out.append(float(s.split()[MJD_COL]))
        except (IndexError, ValueError):
            continue
    return sorted(out) or None


def row_support(m: float, arcs: list[float]) -> tuple[float, int, float]:
    """(hours to the nearest kept arc, kept arcs within +-1 h, hours
    between the kept arcs either side) for a level at MJD m; arcs sorted.
    The gap is 0 on an arc and nan outside the first..last arc."""
    eps = 1e-7                                         # days, ~0.01 s
    i = bisect.bisect_left(arcs, m - eps)
    before = arcs[i - 1] if i > 0 else None
    after = arcs[i] if i < len(arcs) else None
    near = min(abs(x - m) for x in (before, after) if x is not None) * 24
    w = SUPPORT_HALF_WINDOW_H / 24
    n = bisect.bisect_right(arcs, m + w + eps) - bisect.bisect_left(arcs, m - w - eps)
    if after is not None and abs(after - m) <= eps:
        gap = 0.0
    elif before is not None and after is not None:
        gap = (after - before) * 24
    else:
        gap = float("nan")
    return near, n, gap


def support_lines(lines, arcs: list[float] | None) -> list[str]:
    """One support line per spline data line (its MJD and time as written);
    all "nan" when the kept arcs are not known."""
    out = []
    for line in lines:
        c = line.split()
        key = f"{c[0]:>15} {c[2]:>4} {c[3]:>3} {c[4]:>3} {c[5]:>3} {c[6]:>3} {c[7]:>3}"
        if arcs:
            near, n, gap = row_support(float(c[0]), arcs)
            out.append(f"{key} {near:12.2f} {n:8d} {gap:9.2f}\n")
        else:
            out.append(f"{key} {'nan':>12} {'nan':>8} {'nan':>9}\n")
    return out


def support_text(sta: str, settings: str, note: str, lines: list[str],
                 results: str | None = None) -> list[str]:
    """A support file: header (the settings, for a frozen year also its
    results line, so it is only used with the frozen file it was made
    with), then the lines."""
    return ([f"%  station {sta}: support of each level of {sta}_spline_out.txt by the arcs "
             f"subdaily kept\n", f"{SETTINGS_TAG} {settings}\n"]
            + ([f"{RESULTS_TAG} {results}\n"] if results else [])
            + [f"%  {note} (analysis_tools/gnss_record.py)\n"] + SUPPORT_HEAD + lines)


def read_support(path: Path) -> tuple[list[str], list[str]]:
    """(header lines, data lines) of a support file."""
    head, lines = [], []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("%"):
                if not lines:
                    head.append(line)
            elif len(line.split()) >= 10:
                lines.append(line if line.endswith("\n") else line + "\n")
    return head, lines


def frozen_support(files_dir: Path, sta: str, year: int, frozen_head: list[str]) -> dict:
    """{MJD as written: support line} of a frozen year, from the file
    written when it was frozen -- if it was written together with that
    frozen file (the same settings and results line), else {}."""
    try:
        head, lines = read_support(frozen_support_path(files_dir, sta, year))
    except OSError:
        return {}
    for tag in (SETTINGS_TAG, RESULTS_TAG):
        if header_value(head, tag) != header_value(frozen_head, tag):
            return {}
    return {line.split()[0]: line for line in lines}


def support_summary(path: Path, days: int = SUPPORT_DAYS) -> dict | None:
    """gap_support for record_status.json: the levels of the newest `days`
    days of a support file (counted back from its newest level, as
    plot_7day.py chooses its window) that are more than UNSUPPORTED_H from
    every kept arc, how many lie in kept-arc gaps over FAKE_FILL_H, and how
    many 30-minute levels are missing (left out by subdaily: gaps over
    gap_min_val). None for a file without levels; OSError if there is no
    file."""
    head, lines = read_support(path)
    rows = []
    for line in lines:
        c = line.split()
        try:
            rows.append((float(c[0]), float(c[7]), float(c[9]), c))
        except (IndexError, ValueError):
            continue
    if not rows:
        return None
    week = [r for r in rows if r[0] >= rows[-1][0] - days - 1e-6]
    known = [r for r in week if not math.isnan(r[1])]
    bad = [r for r in known if r[1] > UNSUPPORTED_H]

    def when(c):
        return f"{c[1]}-{int(c[2]):02d}-{int(c[3]):02d} {int(c[4]):02d}:{int(c[5]):02d}"

    return {"file": path.name, "days": days, "from": when(week[0][3]), "to": when(week[-1][3]),
            "rows": len(week), "unknown_rows": len(week) - len(known),
            "missing_rows": max(0, round((week[-1][0] - week[0][0]) * 48) + 1 - len(week)),
            "unsupported_hours": UNSUPPORTED_H, "unsupported_rows": len(bad),
            "unsupported_fraction": round(len(bad) / len(known), 4) if known else None,
            "max_hours_to_arc": round(max(r[1] for r in known), 2) if known else None,
            "rows_in_gaps_over_5h": sum(1 for r in known if r[2] > FAKE_FILL_H),
            "gap_min_val_hours": gap_min_val_hours(header_value(head, SETTINGS_TAG) or "")}


def support_report(g: dict | None) -> str:
    """One line for the nightly output and the health mail."""
    if not g:
        return "no levels"
    if g["unknown_rows"] >= g["rows"]:
        return f"not known for the newest {g['days']} days (no kept arcs)"
    known = g["rows"] - g["unknown_rows"]
    s = (f"newest {g['days']} days: {g['unsupported_rows']} of {known} levels "
         f"({100 * g['unsupported_fraction']:.1f} %) more than {g['unsupported_hours']:g} h "
         f"from any kept arc (longest {g['max_hours_to_arc']:g} h)")
    if g["rows_in_gaps_over_5h"]:
        s += f"; {g['rows_in_gaps_over_5h']} in kept-arc gaps over {FAKE_FILL_H:g} h"
    if g.get("missing_rows"):
        s += (f"; {g['missing_rows']} left out (kept-arc gaps over "
              f"{g['gap_min_val_hours']:g} h)")
    return s


# ----------------------------------------------------------------
# Tonight's plan
# ----------------------------------------------------------------

@dataclass
class Plan:
    days: dict          # {date: results file}, every year
    newest: date        # newest results day
    frozen: list        # years taken from their _spline_frozen.txt
    first_live: int     # first year taken from tonight's fit
    start: date         # first day tonight's fit reads
    end: date           # last day tonight's fit reads (newest, unless that year waits)
    fp: dict = field(default_factory=dict)      # {year: results fingerprint}
    notes: list = field(default_factory=list)
    thin: dict = field(default_factory=dict)    # {past year fitted by itself: arcs in the window}
    since: date | None = None                   # gnss_record_first_day

    @property
    def fit_days(self):
        return [d for d in self.days if self.start <= d <= self.end]

    def to_freeze(self) -> list[int]:
        """Complete years in tonight's fit that the fit runs far enough past."""
        return [y for y in range(self.first_live, self.end.year)
                if self.end >= date(y, 12, 31) + timedelta(days=FREEZE_AFTER_DAYS)]


def make_plan(refl_code, sta: str, settings: str, since: date | None = None,
              separate_thin: bool = True) -> Plan | None:
    days = results_days(refl_code, sta, since)
    if not days:
        return None
    newest = max(days)
    files_dir = Path(refl_code) / "Files" / sta
    years = sorted({d.year for d in days})
    fp = {y: fingerprint([p for d, p in days.items() if d.year == y]) for y in years}
    frozen = []
    for y in years:                       # oldest first; frozen years are a prefix
        if y >= newest.year or not frozen_ok(frozen_path(files_dir, sta, y), settings, fp[y]):
            break
        frozen.append(y)
    window = azimuth_window(settings)
    notes = []

    def arcs(y, lo, hi):
        return sum(arcs_in_window(p, window) for d, p in days.items()
                   if d.year == y and lo <= d <= hi)

    # a past year (not frozen) too thin to be fitted with the window --
    # the record's first year beginning on 31 December, say -- would crash
    # every fit that holds it: it is fitted by itself and frozen at once
    # (fit_thin_year), and tonight's fit starts after it. Not across a year
    # without results: a stray file that far back is refused (subdaily_range)
    thin = {}
    k = len(frozen)
    while separate_thin and years[k] < newest.year and years[k + 1] == years[k] + 1:
        n = arcs(years[k], date(years[k], 1, 1), date(years[k], 12, 31))
        if n >= MIN_EDGE_ARCS:
            break
        thin[years[k]] = n
        notes.append(f"{years[k]} has {n} arc(s) in the azimuth window -- fitted by itself "
                     f"and frozen")
        k += 1
    first_live = years[k]

    # the pad: only if the year before has enough arcs in it to be fitted
    pad_from = date(first_live, 1, 1) - timedelta(days=PAD_DAYS)
    start = min(d for d in days if d >= pad_from)
    if start.year < first_live and arcs(start.year, start, date(start.year, 12, 31)) < MIN_EDGE_ARCS:
        notes.append(f"pad {start}..{start.year}-12-31 has under {MIN_EDGE_ARCS} arcs in the "
                     f"azimuth window -- fit starts 1 January")
        start = min(d for d in days if d.year >= first_live)
    # the newest year: waits a night if it is too thin to be fitted
    end = newest
    if newest.year > first_live and arcs(newest.year, date(newest.year, 1, 1), newest) < MIN_EDGE_ARCS:
        earlier = [d for d in days if start <= d < date(newest.year, 1, 1)]
        if earlier:
            end = earlier[-1]
            notes.append(f"{newest.year} so far has under {MIN_EDGE_ARCS} arcs in the azimuth "
                         f"window -- it waits; tonight's fit ends {end}")
    return Plan(days, newest, frozen, first_live, start, end, fp, notes, thin, since)


# ----------------------------------------------------------------
# Status (read by diagnostics/station_health.py)
# ----------------------------------------------------------------

def gnssrefl_version() -> str | None:
    """The installed gnssrefl, so a status says which code made the record
    (the output names this file relies on are those of 4.1.5)."""
    try:
        from importlib.metadata import version
        return version("gnssrefl")
    except Exception:
        return None


def _median(values):
    v = sorted(values)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def arc_stats(refl_code: Path | str, sta: str, days: int = ARC_STATS_DAYS) -> list[dict]:
    """Per UTC day of the newest `days` results days: arcs kept by gnssir,
    their median LSP amplitude (Amp) and median peak-to-noise (PkNoise).
    A rough sea lowers the reflected power, so in a storm arcs fall below
    gnssir's amplitude / peak2noise limits and drop out before anything
    looks wrong in the water level; station_health.py warns on a sharp
    drop. Read-only; a day that cannot be read is left out."""
    out = []
    for d, path in list(results_days(refl_code, sta).items())[-days:]:
        amp, pkn = [], []
        try:
            for s in _data_lines(path):
                c = s.split()
                try:
                    a, p = float(c[AMP_COL]), float(c[PKNOISE_COL])
                except (IndexError, ValueError):
                    continue
                amp.append(a)
                pkn.append(p)
        except OSError:
            continue
        if amp:
            out.append({"day": d.isoformat(), "arcs": len(amp),
                        "median_amp": round(_median(amp), 2),
                        "median_pknoise": round(_median(pkn), 2)})
    return out


def write_status(files_dir: Path, **kw) -> None:
    fit_dir = files_dir / FIT_SUBDIR
    fit_dir.mkdir(parents=True, exist_ok=True)
    kw["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kw.setdefault("gnssrefl_version", gnssrefl_version())
    if "arc_stats" not in kw:
        try:
            # files_dir is <REFL_CODE>/Files/<sta>
            kw["arc_stats"] = arc_stats(files_dir.parent.parent, files_dir.name)
        except Exception as exc:     # a status must still be written
            kw["arc_stats"] = []
            kw["arc_stats_error"] = str(exc)
    if "gap_support" not in kw:
        # of the support file on disk: the one of the record on disk, also
        # when tonight's update failed and left last night's in place
        try:
            kw["gap_support"] = support_summary(support_path(files_dir, files_dir.name))
        except OSError:
            kw["gap_support"] = None
        except Exception as exc:
            kw["gap_support"] = None
            kw["gap_support_error"] = str(exc)
    write_atomic(fit_dir / STATUS_NAME, [json.dumps(kw, default=str, indent=1) + "\n"])


# ----------------------------------------------------------------
# The record
# ----------------------------------------------------------------

@dataclass
class ThinFit:
    """A thin past year's own fit (fit_thin_year), kept until compose."""
    head: list          # gnssrefl's header
    rows: list          # [(MJD, data line)] of that year
    used: str           # the settings that worked
    edit: str | None    # its <sta>_<year>_subdaily_edit.txt, if subdaily wrote one
    arcs: list | None = None    # MJDs of the arcs that fit kept (kept_arcs)


def compose(plan: Plan, files_dir: Path, sta: str, settings: str, freeze: bool = True,
            allow_shrink: bool = False, used: str | None = None,
            thin_fits: dict | None = None) -> int:
    """Check, then freeze what is due and write <sta>_spline_out.txt from
    the frozen years, the thin years' own fits and tonight's fit. Returns
    0, or 1 with nothing written."""
    fit_dir = files_dir / FIT_SUBDIR
    fit_file = fit_dir / f"{sta}_spline_out.txt"
    used = used or settings
    thin_fits = thin_fits or {}

    def fail(msg):
        print(f"  ERROR: {msg} -- record not updated")
        write_status(files_dir, ok=False, message=msg, fit_start=plan.start, fit_end=plan.end,
                     newest_results=plan.newest)
        return 1

    if not fit_file.exists():
        return fail(f"no fit at {fit_file}")
    head, rows = read_spline(fit_file)
    live_from = mjd(date(plan.first_live, 1, 1))
    need_from = max(plan.start, date(plan.first_live, 1, 1))
    if (not rows or mjd_date(rows[0][0]) > need_from + timedelta(days=1)
            or mjd_date(rows[-1][0]) < plan.end - timedelta(days=1)):
        span = f"{mjd_date(rows[0][0])}..{mjd_date(rows[-1][0])}" if rows else "no rows"
        return fail(f"the fit ({span}) does not cover {need_from}..{plan.end}")
    h_fit = header_hortho(head)
    eps = 1e-6
    arcs = kept_arcs(fit_dir, sta)        # tonight's kept arcs: the support of its levels

    def rows_of(year, rows_):
        lo, hi = mjd(date(year, 1, 1)) - eps, mjd(date(year + 1, 1, 1)) - eps
        return [line for m, line in rows_ if lo <= m < hi]

    # the record and, line for line, its support
    body, support, parts = [], [], []
    for y in plan.frozen:
        fh, frows = read_spline(frozen_path(files_dir, sta, y))
        h_y = header_hortho(fh)
        lines = rows_of(y, frows)
        body += [rehortho(line, h_y, h_fit) for line in lines]
        known = frozen_support(files_dir, sta, y, fh)
        support += [known.get(line.split()[0]) or support_lines([line], None)[0] for line in lines]
        parts.append(f"{y} frozen")
    for y, tf in thin_fits.items():
        h_y = header_hortho(tf.head)
        body += [rehortho(line, h_y, h_fit) for _, line in tf.rows]
        support += support_lines([line for _, line in tf.rows], tf.arcs)
        parts.append(f"{y} frozen (fitted by itself with '{tf.used}': {plan.thin[y]} arc(s) "
                     f"in the azimuth window)")
    live_rows = [line for m, line in rows if m >= live_from - eps]
    body += live_rows
    support += support_lines(live_rows, arcs)
    parts.append(f"from {need_from} the fit of {plan.start}..{plan.end}")
    if used.split() != settings.split():
        parts.append(f"FALLBACK settings '{used}' (not frozen)")
    if not body:
        return fail("nothing to write")

    # ---- guards, before anything is written ----
    record = files_dir / f"{sta}_spline_out.txt"
    new_dates = {row_date(l) for l in body}
    hint = "(if intended: set gnss_record_first_day in station.json, or run once with --allow-shrink)"
    if record.exists() and not allow_shrink:
        _, old = read_spline(record)
        if plan.since:                    # the days before gnss_record_first_day are meant to go
            old = [(m, l) for m, l in old if row_date(l) >= plan.since]
        if old:
            old_dates = {row_date(l) for _, l in old}
            if float(body[0].split()[0]) > old[0][0] + 1.0:
                return fail(f"the new record would start {min(new_dates)}, later than the current "
                            f"one ({min(old_dates)}): every frame in between would lose its water "
                            f"level {hint}")
            lost = sorted(old_dates - new_dates)
            if len(lost) > MAX_LOST_DAYS:
                shown = ", ".join(map(str, lost[:6])) + (" ..." if len(lost) > 6 else "")
                return fail(f"the new record has no readings on {len(lost)} day(s) the current "
                            f"one covers ({shown}) {hint}")

    # ---- freeze (from tonight's fit, so tomorrow's record equals tonight's) ----
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    frozen_now = []
    # a frozen year's support goes first: a frozen file without it (a power
    # cut in between) would be used with its support never known
    for y, tf in thin_fits.items():       # a fit of that year alone: final now
        lines = [line for _, line in tf.rows]
        note = (f"{y} frozen {stamp} from a fit of {y} by itself with '{tf.used}' "
                f"({plan.thin[y]} arc(s) in the azimuth window)")
        write_atomic(frozen_support_path(files_dir, sta, y),
                     support_text(sta, settings, note, support_lines(lines, tf.arcs),
                                  f"{plan.fp[y]} rows {len(lines)}"))
        write_atomic(frozen_path(files_dir, sta, y),
                     frozen_text(tf.head, settings, plan.fp[y], lines, note))
        if tf.edit is not None:
            write_atomic(files_dir / f"{sta}_{y}_subdaily_edit.txt", [tf.edit])
        (fit_dir / f"{UPLOAD_MARK}{y}").touch()
        frozen_now.append(y)
        print(f"  froze {y} (fitted by itself): {frozen_path(files_dir, sta, y).name}")
    if freeze:
        for y in plan.to_freeze():
            yr = rows_of(y, rows)
            if not yr:
                continue
            note = f"{y} frozen {stamp} from the fit of {plan.start}..{plan.end}"
            write_atomic(frozen_support_path(files_dir, sta, y),
                         support_text(sta, settings, note, support_lines(yr, arcs),
                                      f"{plan.fp[y]} rows {len(yr)}"))
            write_atomic(frozen_path(files_dir, sta, y),
                         frozen_text(head, settings, plan.fp[y], yr, note))
            (fit_dir / f"{UPLOAD_MARK}{y}").touch()
            frozen_now.append(y)
            print(f"  froze {y}: {frozen_path(files_dir, sta, y).name}")

    write_atomic(record, head[:1]
                 + [f"%  record: {'; '.join(parts)} (analysis_tools/gnss_record.py)\n"]
                 + head[1:] + body)
    # its support, one line per level, right after it (a reader joins on the MJD)
    write_atomic(support_path(files_dir, sta),
                 support_text(sta, used, f"record: {'; '.join(parts)}", support))
    if arcs is None:
        print(f"  WARNING: subdaily wrote no {sta}_<years>{KEPT_SUFFIX} -- the support of "
              f"tonight's levels is not known ({support_path(files_dir, sta).name}: nan)")

    # the edit files of the years tonight's fit covers in full; one that
    # subdaily did not write (a renamed gnssrefl output, say) is reported,
    # since filter_month.py skips its months without a word
    missing_edit = [y for y, tf in thin_fits.items() if tf.edit is None]
    for y in range(plan.first_live, plan.end.year + 1):
        src = fit_dir / f"{sta}_{y}_subdaily_edit.txt"
        if src.exists():
            copy_atomic(src, files_dir / src.name)
        else:
            missing_edit.append(y)
    if missing_edit:
        print(f"  WARNING: subdaily wrote no {sta}_<year>_subdaily_edit.txt for "
              f"{', '.join(map(str, missing_edit))} -- the copy in {files_dir} is not updated")
    # gnssrefl's main plot under one name, whatever years tonight's fit spans
    # (<sta>_<year>_last.png, <sta>_<year>_<year_end>_last.png)
    lasts = sorted(fit_dir.glob(f"{sta}_*_last.png"), key=lambda p: p.stat().st_mtime)
    if lasts:
        copy_atomic(lasts[-1], files_dir / f"{sta}_last.png")

    # what is refitted every night from now on: from 1 January of the first
    # year still not frozen (a year frozen tonight no longer counts)
    still = [y for y in range(plan.first_live, plan.end.year + 1) if y not in frozen_now]
    live = date(still[0], 1, 1) if still else plan.start
    if (plan.end - live).days > LIVE_SPAN_WARN_DAYS:
        print(f"  WARNING: the live fit spans {(plan.end - live).days} days from {live} -- "
              f"a year is not being frozen (see {STATUS_NAME})")

    first, last = min(new_dates), max(new_dates)
    try:
        gap = {"gap_support": support_summary(support_path(files_dir, sta))}
    except Exception:
        gap = {}                          # write_status tries again and says why
    write_status(files_dir, ok=True, fallback=used.split() != settings.split(), settings=used,
                 record_first=first, record_last=last, fit_start=plan.start, fit_end=plan.end,
                 live_from=live, newest_results=plan.newest, frozen=plan.frozen + frozen_now,
                 missing_edit=missing_edit, first_day=plan.since, notes=plan.notes,
                 message="; ".join(parts), **gap)
    print(f"  record: {len(body)} readings, {first} to {last} UTC ({'; '.join(parts)})")
    if gap:
        print(f"  support: {support_report(gap['gap_support'])} "
              f"({support_path(files_dir, sta).name})")
    return 0


def clear_fit_dir(fit_dir: Path) -> None:
    """Remove what earlier subdaily runs left in fit/ (the spline, edit files
    and plots, whose names change with the years a fit spans), so nothing
    of theirs is taken for tonight's and fit/ holds one run's output.
    Kept: the status, the upload marks and hidden files (the lock)."""
    for p in fit_dir.iterdir():
        if (p.is_file() and not p.name.startswith(".") and p.name != STATUS_NAME
                and not p.name.startswith(UPLOAD_MARK)):
            p.unlink(missing_ok=True)


def run_subdaily(days, files_dir: Path, sta: str, settings: str) -> bool:
    """One subdaily run over these days into Files/<sta>/fit/. True if it
    wrote a spline: its exit status alone does not say (it exits 0 when it
    finds no results, or when doy1 > doy2)."""
    y1, d1, y2, d2 = subdaily_range(days)
    fit_dir = files_dir / FIT_SUBDIR
    fit_dir.mkdir(parents=True, exist_ok=True)
    clear_fit_dir(fit_dir)
    spline = fit_dir / f"{sta}_spline_out.txt"
    cmd = ["subdaily", sta, str(y1), "-doy1", str(d1), "-year_end", str(y2), "-doy2", str(d2),
           "-subdir", f"{sta}/{FIT_SUBDIR}"] + shlex.split(settings)
    log = fit_dir / FIT_LOG
    print("  " + " ".join(cmd))
    refl = str(files_dir.parent.parent)                 # the same REFL_CODE
    env = dict(os.environ, REFL_CODE=refl)
    env.setdefault("ORBITS", f"{refl}/orbits")          # as process_and_plot.sh sets them
    env.setdefault("EXE", f"{refl}/exe")
    with open(log, "w") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env).returncode
    if spline.exists() and read_spline(spline)[1]:
        if rc != 0:
            print(f"  WARNING: subdaily exited {rc} after writing the spline (a later plot or "
                  f"file failed; see {log}) -- the spline is used")
        return True
    print(f"  subdaily wrote no spline (exit status {rc}); last lines of {log}:")
    for line in log.read_text(errors="replace").splitlines()[-8:]:
        print("    " + line)
    return False


def fit_thin_year(plan: Plan, year: int, files_dir: Path, sta: str, tries) -> ThinFit | None:
    """A past year with under MIN_EDGE_ARCS arcs in the azimuth window,
    fitted by itself: with the window if subdaily manages, else with the
    fallback. None if no try gives a spline covering that year's days."""
    days = [d for d in plan.days if d.year == year]
    fit_dir = files_dir / FIT_SUBDIR
    eps = 1e-6
    print(f"  {year} by itself ({plan.thin[year]} arc(s) in the azimuth window):")
    for i, s in enumerate(tries):
        if i:
            print(f"  WARNING: retrying {year} with {s} -- that year is frozen from it")
        if not run_subdaily(days, files_dir, sta, s):
            continue
        head, rows = read_spline(fit_dir / f"{sta}_spline_out.txt")
        rows = [(m, line) for m, line in rows
                if mjd(date(year, 1, 1)) - eps <= m < mjd(date(year + 1, 1, 1)) - eps]
        if (not rows or mjd_date(rows[0][0]) > days[0] + timedelta(days=1)
                or mjd_date(rows[-1][0]) < days[-1] - timedelta(days=1)):
            print(f"  the fit of {year} does not cover {days[0]}..{days[-1]}")
            continue
        edit = fit_dir / f"{sta}_{year}_subdaily_edit.txt"
        return ThinFit(head, rows, s, edit.read_text(errors="replace") if edit.exists() else None,
                       kept_arcs(fit_dir, sta))       # before the next run clears fit/
    return None


# ----------------------------------------------------------------
# Command line
# ----------------------------------------------------------------

def cmd_summary(refl_code, sta, since=None) -> int:
    days = results_days(refl_code, sta, since)
    if not days:
        return 3
    per_year = {}
    for d in days:
        per_year[d.year] = per_year.get(d.year, 0) + 1
    years = ", ".join(f"{y}: {n}" for y, n in per_year.items())
    print(f"  Results for {len(days)} day(s), {min(days)} to {max(days)} ({years})"
          + (f" -- from gnss_record_first_day {since} on" if since else ""))
    print("  Newest days:")
    for d in list(days)[-7:]:
        n = sum(1 for _ in _data_lines(days[d]))
        print(f"    {d} (doy {d.timetuple().tm_yday:03d}): {n} track(s)")
    return 0


def cmd_update(refl_code, sta, settings, fallback, allow_shrink, since=None) -> int:
    """One night's update, one at a time (a hand run of process_and_plot.sh
    meeting the cron run would otherwise share Files/<sta>/fit/)."""
    files_dir = Path(refl_code) / "Files" / sta
    (files_dir / FIT_SUBDIR).mkdir(parents=True, exist_ok=True)
    with open(files_dir / FIT_SUBDIR / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("  ERROR: another gnss_record update is running -- not starting a second one")
            return 1
        return _update(refl_code, files_dir, sta, settings, fallback, allow_shrink, since)


def _update(refl_code, files_dir, sta, settings, fallback, allow_shrink, since) -> int:
    plan = make_plan(refl_code, sta, settings, since)
    if plan is None:
        print("  No results found.")
        return 3
    if shutil.which("subdaily") is None:
        print("  ERROR: gnssrefl's subdaily is not on PATH (source gnssrefl_venv/bin/activate "
              "first) -- record not updated")
        return 1

    def check_range(plan):
        try:
            subdaily_range(plan.fit_days)
            return True
        except ValueError as exc:
            print(f"  ERROR: {exc} -- the record keeps last night's")
            write_status(files_dir, ok=False, message=str(exc), fit_start=plan.start,
                         fit_end=plan.end, newest_results=plan.newest)
            return False

    def show(plan):
        print(f"  Fitting {plan.start} to {plan.end}"
              + (f"; frozen: {', '.join(map(str, plan.frozen))}" if plan.frozen else "")
              + (f"; from gnss_record_first_day {plan.since} on" if plan.since else ""))
        for n in plan.notes:
            print(f"  NOTE: {n}")

    show(plan)
    if not check_range(plan):
        return 1
    tries = [settings] + ([fallback] if fallback and fallback.split() != settings.split() else [])
    thin_fits = {}
    for y in plan.thin:
        tf = fit_thin_year(plan, y, files_dir, sta, tries)
        if tf is None:
            print(f"  WARNING: {y} could not be fitted by itself -- it stays in tonight's fit")
            plan = make_plan(refl_code, sta, settings, since, separate_thin=False)
            thin_fits = {}
            show(plan)
            if not check_range(plan):
                return 1
            break
        thin_fits[y] = tf
    for i, s in enumerate(tries):
        if i:
            print(f"  WARNING: retrying the whole fit with {s} -- used tonight, never frozen")
        if run_subdaily(plan.fit_days, files_dir, sta, s):
            return compose(plan, files_dir, sta, settings, freeze=(i == 0),
                           allow_shrink=allow_shrink, used=s, thin_fits=thin_fits)
    print("  ERROR: no fit tonight -- the record keeps last night's")
    write_status(files_dir, ok=False, message="subdaily wrote no spline", fit_start=plan.start,
                 fit_end=plan.end, newest_results=plan.newest)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refl-code", default=os.environ.get("REFL_CODE") or None,
                   help="gnssrefl REFL_CODE (default: $REFL_CODE, else "
                        f"{PROJECT_DIR / 'products' / 'refl_code'})")
    p.add_argument("--station", required=True, help="gnssrefl 4-character station code")
    p.add_argument("--station-json", default=str(STATION_JSON),
                   help="read gnss_record_first_day from here (default: %(default)s)")
    p.add_argument("--since", type=date.fromisoformat, default=None,
                   help="ignore results before this day (YYYY-MM-DD) instead of station.json's "
                        "gnss_record_first_day")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary", help="list the results days (exit 3 if none)")
    u = sub.add_parser("update", help="fit, freeze and write the record")
    u.add_argument("--settings", required=True,
                   help="subdaily options, e.g. \"-rhdot True -knots 8\"; station.json's "
                        "subdaily_gap_min_val is added as -gap_min_val unless they give one")
    u.add_argument("--fallback-settings", help="tried if the first fit fails; never frozen")
    u.add_argument("--allow-shrink", action="store_true",
                   help="accept a record that starts later, or covers fewer days, than the current one")
    a = p.parse_args()
    refl_code = os.path.abspath(a.refl_code or PROJECT_DIR / "products" / "refl_code")
    since = a.since
    if since is None:
        try:
            since = record_first_day(a.station_json)
        except ValueError as exc:
            p.error(f"gnss_record_first_day in {a.station_json}: {exc}")
    if a.cmd == "summary":
        return cmd_summary(refl_code, a.station, since)
    try:
        gap = subdaily_gap_min_val(a.station_json)
    except ValueError as exc:
        # said in the status too, so the health mail names it
        msg = f"subdaily_gap_min_val in {a.station_json}: {exc}"
        write_status(Path(refl_code) / "Files" / a.station, ok=False, message=msg)
        p.error(msg)
    settings = with_gap_min_val(a.settings, gap)
    fallback = with_gap_min_val(a.fallback_settings, gap)
    if gap is not None and settings != a.settings:
        print(f"  subdaily_gap_min_val {gap:g} h (station.json): -gap_min_val {gap:g} added "
              f"to the subdaily settings")
    return cmd_update(refl_code, a.station, settings, fallback, a.allow_shrink, since)


if __name__ == "__main__":
    sys.exit(main())
