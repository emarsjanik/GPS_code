#!/usr/bin/env python3
"""
station_health.py

Checks whether the station is actually working right now -- as
distinct from test_installation.sh, which checks whether it is
correctly *set up*. Both matter, and a station can pass the second
while failing the first.

WHY THIS EXISTS

This station recorded, in its own error_log table, five consecutive
days of identical processing failures before anyone noticed:

    Pipeline run failed: Cannot process: RINEX conversion is
    unavailable (RinexProcessor.initialize() reported: NOT_READY)

Nothing was watching that table. Nothing visibly broke, because a
separate nightly job happened to convert the same files
successfully. 844 error rows had accumulated unread. The underlying
bug was survivable; not noticing it for five days was the real
weakness, and this script exists to close that gap.

Designed for cron: prints a short report, exits 0 when healthy and
non-zero when something needs attention, so cron mails only on real
problems.

    # daily, after the processing run has had time to finish
    0 7 * * * /home/argus_user/GNSS/v4.1/station_health.py

Usage:
    ./station_health.py             normal check
    ./station_health.py --verbose   include passing checks
    ./station_health.py --days 7    widen the error lookback
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# This script lives in diagnostics/, so the project root is one
# level up. Tolerates being run from the root itself.
_here = Path(__file__).resolve().parent
PROJECT_DIR = _here.parent if _here.name == "diagnostics" else _here
sys.path.insert(0, str(PROJECT_DIR / "analysis_tools"))
import gnss_record  # noqa: E402
from gnss_record import results_days  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"

findings: list[tuple[str, str, str]] = []


def record(status: str, check: str, message: str) -> None:
    findings.append((status, check, message))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime | None:
    """Parses the ISO-ish timestamps this project writes, e.g.
    '2026-08-27T00:00:13.110Z'."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------------
# 1. Errors the station recorded but nobody read
# ------------------------------------------------------------------
def _normalize_message(text: str) -> str:
    """
    Collapses a log message to its shape, so that the same problem
    affecting different files or days groups as one finding.

        "Raw file does not exist: /path/station_20260722.um980"
        "Raw file does not exist: /path/station_20260723.um980"
            -> "Raw file does not exist: <PATH>"

    Deliberately conservative: it removes only paths, filenames,
    dates and bare numbers. Two genuinely different failures should
    never collapse into one, since hiding a real problem is far
    worse than listing one twice.
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"/[^\s:,]+", "<PATH>", t)          # absolute paths
    t = re.sub(r"\b\d{8}\b", "<DATE>", t)           # YYYYMMDD
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<DATE>", t)
    t = re.sub(r"\b\d+\b", "<N>", t)               # any remaining number
    return t[:200]


def _is_future_day_message(text: str) -> bool:
    """
    True when a message complains about a day that has not finished
    recording yet.

    gnssir reports "No results file produced: .../<doy>.txt" for a
    day it has no complete data for, which is correct behaviour, not
    a failure -- the day simply has not happened. Because processing
    runs nightly, this would otherwise generate a FAIL every single
    morning, and a monitor that cries wolf daily stops being read
    shortly before the morning it says something true.
    """
    if not text or "No results file produced" not in text:
        return False
    m = re.search(r"/(\d{4})/results/[^/\s]+/(\d{1,3})\.txt", text)
    if not m:
        return False
    day = datetime(int(m.group(1)), 1, 1).date() + timedelta(days=int(m.group(2)) - 1)
    # Today and yesterday are both legitimately incomplete: yesterday's
    # UTC day only closes at 00:00 today, and is processed that evening.
    # Compared as dates, so 31 December still counts as yesterday on 1 January.
    return day >= utcnow().date() - timedelta(days=1)


def _failure_since_resolved(text: str) -> bool:
    """
    True when a logged failure named a specific artefact that now
    exists -- i.e. the failure was transient and a later attempt
    succeeded.

    Only messages naming a checkable output qualify. A failure with
    no verifiable artefact (a crash, a lost connection, a permission
    error) is never suppressed by this, because there is no evidence
    it resolved and staying quiet would be a guess rather than an
    observation.

    Archived RINEX counts as present: compress_rinex.sh replaces a
    day's .obs/.nav pair with a single .tar.gz once processing is
    done, so the original file being absent is the expected end
    state, not a missing output.
    """
    if not text:
        return False

    for marker in ("No results file produced:",
                   "observation file was not created:"):
        if marker in text:
            candidate = text.split(marker, 1)[1].strip().split()[0]
            p = Path(candidate)
            if p.exists():
                return True
            # A processed day's RINEX is replaced by its archive.
            if p.suffix in (".obs", ".nav"):
                archive = p.with_suffix(".tar.gz")
                if archive.exists():
                    return True
                stem_archive = p.parent / (p.stem + ".tar.gz")
                if stem_archive.exists():
                    return True
            return False

    return False


def check_recent_errors(db: sqlite3.Connection, days: int) -> None:
    cutoff = utcnow() - timedelta(days=days)

    rows = list(db.execute(
        "SELECT timestamp, module, severity, description, recovered "
        "FROM error_log ORDER BY id DESC LIMIT 500"
    ))

    recent = []
    for ts, module, severity, description, recovered in rows:
        dt = parse_ts(ts)
        if not (dt and dt >= cutoff):
            continue
        # Not a failure: the day has not finished recording yet.
        if _is_future_day_message(description or ""):
            continue
        # Not worth reporting: it failed then, but a later attempt
        # succeeded and the output exists now.
        if _failure_since_resolved(description or ""):
            continue
        recent.append((dt, module, severity, description or "", recovered))

    if not recent:
        record(OK, "errors", f"No errors logged in the last {days} day(s)")
        return

    # Group related messages -- five days of the same failure is one
    # problem, not five, and reading it as five obscures that it is
    # ongoing rather than transient.
    #
    # Grouping on the raw text was not enough: six stale queue
    # entries produced six separate findings because each message
    # named a different file. Normalizing paths, dates and numbers
    # first collapses those into the one problem they actually are.
    groups: dict[tuple[str, str, str], list[datetime]] = {}
    examples: dict[tuple[str, str, str], str] = {}
    for dt, module, severity, description, _recovered in recent:
        normalized = _normalize_message(description)
        key = (module or "?", severity or "?", normalized)
        groups.setdefault(key, []).append(dt)
        examples.setdefault(key, description.strip())

    unrecovered = [g for g in groups.items() if g[0][1] in ("ERROR", "CRITICAL")]

    if not unrecovered:
        record(OK, "errors",
               f"{len(recent)} log entries in {days}d, none at ERROR level or above")
        return

    for (module, severity, description), times in sorted(
            unrecovered, key=lambda g: len(g[1]), reverse=True):
        times.sort()
        n = len(times)
        first, last = times[0], times[-1]
        span_days = (last - first).days

        if n >= 3 and span_days >= 2:
            detail = (f"{n}x since {first.strftime('%Y-%m-%d')} "
                      f"(most recent {last.strftime('%Y-%m-%d %H:%M')}Z) -- "
                      f"recurring, not transient")
            status = FAIL
        elif n > 1:
            detail = f"{n}x, most recent {last.strftime('%Y-%m-%d %H:%M')}Z"
            status = WARN
        else:
            detail = f"once, {last.strftime('%Y-%m-%d %H:%M')}Z"
            status = WARN

        example = examples.get((module, severity, description), description)
        record(status, f"errors/{module}", f"{example[:150]} [{detail}]")


# ------------------------------------------------------------------
# 2. Is processing keeping up?
# ------------------------------------------------------------------
def check_processing_currency(station_code: str, since=None) -> None:
    base = PROJECT_DIR / "products" / "refl_code"
    # Every year's results: on 1-2 January the newest day is still in
    # last year's folder while this year's is empty. Days before
    # gnss_record_first_day (station.json) are not part of the record:
    # neither counted nor reported as gaps.
    if not any(base.glob(f"[0-9][0-9][0-9][0-9]/results/{station_code}")):
        record(WARN, "processing", "No results directory found yet")
        return

    days = list(results_days(base, station_code, since))
    if not days:
        record(FAIL, "processing", "Results directories exist but contain no days"
                                   + (f" from gnss_record_first_day ({since})" if since else ""))
        return

    newest = days[-1]
    lag = (utcnow().date() - newest).days

    # A two-day lag is this pipeline's normal steady state, not a
    # symptom. A UTC day cannot be processed until it has finished
    # recording (00:00 UTC the following day), and processing runs
    # once nightly at 22:30 local -- so the freshest result is
    # routinely two day-numbers behind "today".
    if lag <= 2:
        record(OK, "processing",
               f"{len(days)} days processed, newest is {newest} "
               f"({lag}d behind, which is normal for a nightly run)")
    elif lag <= 4:
        record(WARN, "processing",
               f"Newest result is {newest}, {lag} days behind today "
               f"-- a run may have been missed")
    else:
        record(FAIL, "processing",
               f"Newest result is {newest}, {lag} days behind today "
               f"-- processing appears stalled")

    # Interior gaps over the last year, across the year boundary,
    # ignoring days explicitly marked as having no usable data.
    recent = [d for d in days if d > newest - timedelta(days=365)]
    gaps = []
    for a, b in zip(recent, recent[1:]):
        for k in range(1, (b - a).days):
            missing = a + timedelta(days=k)
            marker = (base / str(missing.year) / "results" / station_code
                      / f"{missing.timetuple().tm_yday}.no_data")
            if not marker.exists():
                gaps.append(missing)
    if gaps:
        shown = ", ".join(str(g) for g in gaps[:8])
        more = f" (+{len(gaps) - 8} more)" if len(gaps) > 8 else ""
        record(WARN, "processing", f"Gap(s) in the record: {shown}{more}")


# ------------------------------------------------------------------
# 2b. Does the water-level record hold every results day?
#     (analysis_tools/gnss_record.py; the waterline DEM drops every
#     camera frame the record does not cover, so a truncated or stale
#     record must show up here, not only days later on the camera side.)
# ------------------------------------------------------------------
def check_water_level_record(station_code: str, since=None) -> None:
    base = PROJECT_DIR / "products" / "refl_code"
    days = list(results_days(base, station_code, since))
    if not days:
        return
    files = base / "Files" / station_code
    rec = files / f"{station_code}_spline_out.txt"
    try:
        _, rows = gnss_record.read_spline(rec)
    except OSError:
        record(FAIL, "record", f"{rec.name} not found")
        return
    if not rows:
        record(FAIL, "record", f"{rec.name} has no readings")
        return
    first = gnss_record.row_date(rows[0][1])
    last = gnss_record.row_date(rows[-1][1])
    behind = (days[-1] - last).days
    if first > days[0] + timedelta(days=1):
        record(FAIL, "record", f"water level starts {first} but results start {days[0]}: "
                              f"every camera frame in between has no water level")
    if behind > 3:
        record(FAIL, "record", f"water level ends {last}, {behind} days before the newest "
                              f"results day {days[-1]}: the nightly fit is not updating it")
    elif behind > 1:
        record(WARN, "record", f"water level ends {last}, {behind} days before the newest "
                              f"results day {days[-1]}")
    try:
        st = json.loads((files / gnss_record.FIT_SUBDIR / gnss_record.STATUS_NAME).read_text())
    except (OSError, ValueError):
        st = {}
    if st and not st.get("ok", True):
        record(FAIL, "record", f"last update ({st.get('time')}) failed: {st.get('message')}")
    elif st.get("fallback"):
        record(WARN, "record", f"last update used the fallback settings '{st.get('settings')}' "
                              f"(no azimuth window) for the whole live year")
    if st.get("missing_edit"):
        record(WARN, "record", f"subdaily wrote no {station_code}_<year>_subdaily_edit.txt for "
                              f"{', '.join(map(str, st['missing_edit']))}: filter_month.py skips "
                              f"those months (a renamed gnssrefl output?)")
    try:
        # from 1 January of the first year still not frozen after the update
        # (live_from), so the night that refreezes a year is not counted
        span = (datetime.fromisoformat(str(st["fit_end"]))
                - datetime.fromisoformat(str(st.get("live_from") or st["fit_start"]))).days
        if span > gnss_record.LIVE_SPAN_WARN_DAYS:
            record(WARN, "record", f"the nightly fit spans {span} days: a past year is not "
                                  f"being frozen")
    except (KeyError, ValueError):
        pass
    if not any(f[1] == "record" and f[0] != OK for f in findings):
        record(OK, "record", f"water level {first} to {last} ({len(days)} results days)")


# ------------------------------------------------------------------
# 2b'. Are arcs dropping out? A rough sea lowers the reflected power, so
#     in a storm arcs fall below gnssir's amplitude / peak2noise limits
#     and the survivors are the doubtful ones. gnss_record.py keeps per
#     UTC day arc count, median Amp and median PkNoise (arc_stats in
#     record_status.json); a sharp drop on the newest day against the
#     days before it is a WARN. It is equally what a receiver or antenna
#     problem looks like, which is worth a look too.
# ------------------------------------------------------------------
ARC_BASELINE_DAYS = 7    # days before the newest one it is compared with
ARC_MIN_BASELINE = 3
ARC_COUNT_DROP = 0.5     # newest day's arcs below half the baseline median
ARC_AMP_DROP = 0.7       # newest day's median Amp below 70 % of the baseline


def check_arc_stats(station_code: str) -> None:
    status = (PROJECT_DIR / "products" / "refl_code" / "Files" / station_code
              / gnss_record.FIT_SUBDIR / gnss_record.STATUS_NAME)
    try:
        stats = json.loads(status.read_text()).get("arc_stats") or []
    except (OSError, ValueError, AttributeError):
        return
    if len(stats) < ARC_MIN_BASELINE + 1:
        return
    newest, before = stats[-1], stats[-1 - ARC_BASELINE_DAYS:-1]
    try:
        base_arcs = gnss_record._median([d["arcs"] for d in before])
        base_amp = gnss_record._median([d["median_amp"] for d in before])
        base_pkn = gnss_record._median([d["median_pknoise"] for d in before])
        newest["arcs"], newest["median_amp"], newest["median_pknoise"]
    except (KeyError, TypeError):
        return
    summary = (f"{newest['day']}: {newest['arcs']} arcs, median Amp {newest['median_amp']}, "
               f"PkNoise {newest['median_pknoise']} (previous {len(before)} days: "
               f"{base_arcs:.0f} arcs, Amp {base_amp:.1f}, PkNoise {base_pkn:.1f})")
    dropped = []
    if newest["arcs"] < ARC_COUNT_DROP * base_arcs:
        dropped.append("arcs")
    if newest["median_amp"] < ARC_AMP_DROP * base_amp:
        dropped.append("amplitude")
    if dropped:
        record(WARN, "arcs", f"sharp drop in {' and '.join(dropped)} -- {summary}. Rough "
                             f"seas damp the reflection (the water levels kept may be the "
                             f"biased ones); so does a receiver or antenna problem")
    else:
        record(OK, "arcs", summary)


# ------------------------------------------------------------------
# 2c. Is the public plot being redrawn, and does the tide prediction
#     cover it? process_and_plot.sh reports success when the 7-day plot
#     fails, so a stale 7_day_plot.png is still uploaded and attached;
#     and when the tide spreadsheet (tide_model_file) runs out, the
#     predicted tide and every departure marker vanish from the plot
#     without a word (plot_7day.load_tide).
# ------------------------------------------------------------------
PLOT_STALE_HOURS = 36    # the plot is redrawn nightly
TIDE_AHEAD_DAYS = 2      # warn this long before the tide prediction runs out
PLOT_DAYS = 7            # plot_7day.py's default window
TIDE_GAP_HOURS = 3       # a longer hole in the prediction can hide a high or low tide


def _newest_spline_time(station_code: str) -> datetime | None:
    """Newest water-level time (naive UTC, as plot_7day uses it)."""
    rec = (PROJECT_DIR / "products" / "refl_code" / "Files" / station_code
           / f"{station_code}_spline_out.txt")
    try:
        _, rows = gnss_record.read_spline(rec)
    except OSError:
        return None
    if not rows:
        return None
    return datetime(1858, 11, 17) + timedelta(days=rows[-1][0])


def check_public_plot(station_code: str) -> None:
    files = PROJECT_DIR / "products" / "refl_code" / "Files" / station_code
    plot = files / "7_day_plot.png"
    try:
        mtime = plot.stat().st_mtime
    except OSError:
        record(WARN, "plot", f"{plot.name} not found")
        return
    when = datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_h = (utcnow() - when).total_seconds() / 3600
    if age_h > PLOT_STALE_HOURS:
        record(WARN, "plot", f"{plot.name} last drawn {when:%Y-%m-%d %H:%M}Z, "
                             f"{age_h / 24:.1f} days ago: the public plot is not updating "
                             f"(see logs/daily_gnss.log, Step 5)")
        return
    try:
        rec_mtime = (files / f"{station_code}_spline_out.txt").stat().st_mtime
    except OSError:
        rec_mtime = None
    # The plot is drawn right after the record is written each night.
    if rec_mtime is not None and rec_mtime - mtime > 3600:
        record(WARN, "plot", f"{plot.name} ({when:%Y-%m-%d %H:%M}Z) is older than the "
                             f"water-level record it is drawn from: last night's plot failed")
        return
    record(OK, "plot", f"{plot.name} drawn {when:%Y-%m-%d %H:%M}Z")


def _tide_times(path: Path, time_col: str, value_col: str) -> list:
    """The times plot_7day.load_tide draws a predicted tide at, sorted:
    rows of the first sheet, below a header in row 1, whose time is a
    date-time and whose value is a number. Checking only the time
    column said "covered" for times with no tide value (a sheet filled
    out ahead of its values), which the plot leaves blank. Uses openpyxl
    exactly as load_tide does when this python has it -- not its
    read_only mode, which reads only as far as the sheet's stated
    <dimension>, and some writers state A1:B2 for a whole sheet -- else
    reads the xlsx directly: cron runs this script under the system
    python, which may not have openpyxl."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return _tide_times_stdlib(path, time_col, value_col)
    wb = load_workbook(path, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        header = [c.value for c in ws[1]]
        ti, vi = _tide_columns(header, time_col, value_col)
        times = [row[ti] for row in ws.iter_rows(min_row=2, values_only=True)
                 if isinstance(row[ti], datetime) and isinstance(row[vi], (int, float))]
    finally:
        wb.close()
    return sorted(times)


def _tide_columns(header: list, time_col: str, value_col: str) -> tuple:
    for name in (time_col, value_col):
        if name not in header:
            raise ValueError(f"no '{name}' column in the first row")
    return header.index(time_col), header.index(value_col)


# Excel's built-in number formats that are dates or times (the rest of
# 0-49 are numbers, text or currency). openpyxl reads a cell as a date
# from its style's number format, built-in or custom, never from the
# value: the same serial number is a date in one cell and a plain
# number in the next.
_XLSX_BUILTIN_DATE_FORMATS = {
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "m/d/yy h:mm",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0",
}


def _xlsx_is_date_format(fmt: str | None) -> bool:
    """openpyxl's is_date_format: a d, m, h, y or s outside quoted text
    and [locale] brackets in the first section of the format."""
    if fmt is None:
        return False
    fmt = re.sub(r'".*?"|\[(?!hh?\]|mm?\]|ss?\])[^\]]*\]', "", fmt.split(";")[0])
    return re.search(r"(?<![_\\])[dmhysDMHYS]", fmt) is not None


def _xlsx_is_timedelta_format(fmt: str | None) -> bool:
    """openpyxl's is_timedelta_format ([h]:mm:ss and kin), read as a
    duration, not a date-time."""
    return fmt is not None and re.search(
        r"\[hh?\](:mm(:ss(\.0*)?)?)?|\[mm?\](:ss(\.0*)?)?|\[ss?\](\.0*)?",
        fmt.split(";")[0], re.I) is not None


def _tide_times_stdlib(path: Path, time_col: str, value_col: str) -> list:
    """_tide_times without openpyxl, reading the xlsx (a zip of XML) the
    way openpyxl does, so both pythons give the same answer:

    - parts found through the package's relationships, not fixed names;
    - shared and inline strings: plain text plus rich-text runs, without
      phonetic (rPh) runs;
    - a number is a date only if its style's number format is a date
      format (built-in or custom, as openpyxl's is_date_format), counted
      from 1899-12-30, or from 1904-01-01 when the workbook says
      date1904 (older Mac Excel); a duration format is not a date-time;
    - ISO 8601 date-time cells (t="d");
    - cells and rows without an r attribute take the next position;
    - the header is row 1 only, as load_tide's ws[1]."""
    import posixpath
    import zipfile
    import xml.etree.ElementTree as ET
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    pkg = "{http://schemas.openxmlformats.org/package/2006/relationships}"

    def rels_of(z, part):
        folder, name = posixpath.split(part)
        try:
            tree = ET.fromstring(z.read(posixpath.join(folder, "_rels", name + ".rels")))
        except KeyError:
            return {}
        out = {}
        for rel in tree.iter(f"{pkg}Relationship"):
            target = rel.get("Target", "")
            target = (target.lstrip("/") if target.startswith("/")
                      else posixpath.normpath(posixpath.join(folder, target)))
            out[rel.get("Id")] = (rel.get("Type", ""), target)
        return out

    def text_of(element):
        # <t> directly under <si>/<is>, and under its rich-text runs <r>
        return "".join(t.text or "" for t in element.findall(f"{m}t")) + "".join(
            t.text or "" for r in element.findall(f"{m}r") for t in r.findall(f"{m}t"))

    with zipfile.ZipFile(path) as z:
        book = next((t for kind, t in rels_of(z, "").values()
                     if kind.endswith("/officeDocument")), "xl/workbook.xml")
        workbook = ET.fromstring(z.read(book))
        book_rels = rels_of(z, book)
        pr = workbook.find(f"{m}workbookPr")
        flag = pr.get("date1904") if pr is not None else None
        epoch = (datetime(1904, 1, 1) if flag and flag not in ("false", "f", "0")
                 else datetime(1899, 12, 30))
        shared, date_styles, duration_styles = [], set(), set()
        for kind, target in book_rels.values():
            if kind.endswith("/sharedStrings"):
                shared = [text_of(si) for si in
                          ET.fromstring(z.read(target)).findall(f"{m}si")]
            elif kind.endswith("/styles"):
                styles = ET.fromstring(z.read(target))
                custom = {int(f.get("numFmtId")): f.get("formatCode")
                          for f in styles.iter(f"{m}numFmt")}
                xfs = styles.find(f"{m}cellXfs")
                for index, xf in enumerate(xfs.findall(f"{m}xf") if xfs is not None else []):
                    fmt_id = int(xf.get("numFmtId", 0))
                    fmt = custom.get(fmt_id, _XLSX_BUILTIN_DATE_FORMATS.get(fmt_id))
                    if _xlsx_is_date_format(fmt):
                        date_styles.add(index)
                    if _xlsx_is_timedelta_format(fmt):
                        duration_styles.add(index)
        first = next(workbook.iter(f"{m}sheet"))
        sheet = ET.fromstring(z.read(book_rels[first.get(f"{r_ns}id")][1]))

    def value_of(c):
        kind = c.get("t", "n")
        if kind == "inlineStr":
            inline = c.find(f"{m}is")
            return text_of(inline) if inline is not None else None
        v = c.findtext(f"{m}v") or None
        if v is None:
            return None
        if kind == "n":
            number = float(v) if any(ch in v for ch in ".Ee") else int(v)
            style = int(c.get("s", 0))
            if style not in date_styles or style in duration_styles:
                return number
            day, fraction = divmod(number, 1)      # openpyxl's from_excel
            if 0 <= number < 1:
                return None                         # a time of day, not a date-time
            if 0 < number < 60 and epoch.year == 1899:
                day += 1                            # Excel's 29 Feb 1900
            try:
                return epoch + timedelta(days=day, milliseconds=round(fraction * 86400000))
            except OverflowError:
                return None                         # openpyxl makes it an error cell
        if kind == "s":
            return shared[int(v)]
        if kind == "b":
            return bool(int(v))
        if kind == "d":
            # openpyxl's from_ISO8601: date and time, any offset ignored;
            # a date alone is not a date-time
            iso = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(\.\d{1,3})?)?", v)
            if not iso:
                return None
            parts = [int(g) for g in iso.groups()[:6] if g is not None]
            micro = int(float(iso.group(7)) * 1000000) if iso.group(7) else 0
            try:
                return datetime(*parts, microsecond=micro)
            except ValueError:
                return None
        return v                                    # str (a formula's text), e (an error)

    cells = {}                                      # {(row, column): value}
    row_number = 0
    for row in sheet.iter(f"{m}row"):
        row_number = int(row.get("r", row_number + 1))
        column = 0
        for c in row.findall(f"{m}c"):
            ref = re.match(r"\$?([A-Za-z]+)\$?(\d+)", c.get("r") or "")
            if ref:
                column = 0
                for letter in ref.group(1).upper():
                    column = column * 26 + ord(letter) - 64
                row_number = int(ref.group(2))
            else:
                column += 1
            cells[(row_number, column)] = value_of(c)

    width = max([col for _, col in cells] or [0])
    header = [cells.get((1, col)) for col in range(1, width + 1)]
    ti, vi = _tide_columns(header, time_col, value_col)
    times = []
    for (row_number, column), t in cells.items():
        if row_number > 1 and column == ti + 1 and isinstance(t, datetime):
            value = cells.get((row_number, vi + 1))
            if isinstance(value, (int, float)):
                times.append(t)
    return sorted(times)


def check_tide_coverage(station_code: str, cfg: dict) -> None:
    tide_file = cfg.get("tide_model_file")
    if not tide_file:
        record(OK, "tide", "no tide_model_file set: the 7-day plot has no predicted tide")
        return
    value_col = cfg.get("tide_model_value_column")
    if not value_col:
        # plot_7day draws the tide only with both set
        record(WARN, "tide", "tide_model_file is set but tide_model_value_column is not: "
                             "the 7-day plot has no predicted tide or departure markers")
        return
    path = Path(tide_file)
    if not path.is_absolute() and not path.exists():
        path = PROJECT_DIR / path
    if not path.exists():
        record(WARN, "tide", f"tide model file {tide_file} not found: the 7-day plot "
                             f"has no predicted tide or departure markers")
        return
    column = cfg.get("tide_model_time_column") or "time"
    try:
        times = _tide_times(path, column, value_col)
    except Exception as exc:
        record(WARN, "tide", f"could not read {path.name}: {exc}")
        return
    if not times:
        record(WARN, "tide", f"{path.name} has no rows with a time in '{column}' and a "
                             f"number in '{value_col}'")
        return
    start, end = times[0], times[-1]
    newest = _newest_spline_time(station_code) or utcnow().replace(tzinfo=None)
    need_from = newest - timedelta(days=PLOT_DAYS)
    need_to = newest + timedelta(days=TIDE_AHEAD_DAYS)
    # holes inside the window: a file joined from two predictions
    # (merge_tide_files.py) can span the window and still miss part of it
    inside = [need_from] + [t for t in times if need_from < t < need_to] + [need_to]
    holes = [(a, b) for a, b in zip(inside, inside[1:])
             if b - a > timedelta(hours=TIDE_GAP_HOURS)]
    if end < need_to:
        state = ("are already missing from" if end < newest
                 else f"disappear from it within {TIDE_AHEAD_DAYS} days of")
        record(WARN, "tide", f"{path.name} ends {end:%Y-%m-%d %H:%M}, the 7-day plot needs it "
                             f"to {need_to:%Y-%m-%d} (newest water level + {TIDE_AHEAD_DAYS} "
                             f"days): the predicted tide and departure markers {state} "
                             f"7_day_plot.png -- extend the tide prediction")
    elif start > need_from:
        record(WARN, "tide", f"{path.name} starts {start:%Y-%m-%d %H:%M}, after the 7-day "
                             f"plot's window starts ({need_from:%Y-%m-%d %H:%M})")
    elif holes:
        a, b = max(holes, key=lambda h: h[1] - h[0])
        record(WARN, "tide", f"{path.name} has no predicted tide from {a:%Y-%m-%d %H:%M} to "
                             f"{b:%Y-%m-%d %H:%M}"
                             + (f" ({len(holes)} gaps over {TIDE_GAP_HOURS} h)" if len(holes) > 1 else "")
                             + f"; the 7-day plot needs it from {need_from:%Y-%m-%d %H:%M} to "
                               f"{need_to:%Y-%m-%d} (newest water level + {TIDE_AHEAD_DAYS} days)")
    else:
        record(OK, "tide", f"{path.name} covers {start:%Y-%m-%d} to {end:%Y-%m-%d}")


# ------------------------------------------------------------------
# 3. Is anything piling up unprocessed?
# ------------------------------------------------------------------
def check_raw_backlog() -> None:
    raw_dir = PROJECT_DIR / "raw"
    if not raw_dir.is_dir():
        record(WARN, "backlog", "raw/ directory not found")
        return

    files = sorted(raw_dir.glob("*.um980"))
    if not files:
        record(OK, "backlog", "No unprocessed raw files")
        return

    now = utcnow()
    # The newest file is normally today's, still being written.
    stale = []
    for f in files:
        age_h = (now - datetime.fromtimestamp(f.stat().st_mtime,
                                              tz=timezone.utc)).total_seconds() / 3600
        if age_h > 36:
            stale.append((f.name, age_h))

    if not stale:
        record(OK, "backlog",
               f"{len(files)} raw file(s) present, none older than 36h "
               f"(current-day files are expected here)")
    else:
        names = ", ".join(f"{n} ({h:.0f}h)" for n, h in stale[:4])
        record(FAIL, "backlog",
               f"{len(stale)} raw file(s) unprocessed for over 36h: {names}")


# ------------------------------------------------------------------
# 4. Did the scheduled jobs actually run?
# ------------------------------------------------------------------
def check_cron_ran() -> None:
    logs = {
        "daily_gnss": PROJECT_DIR / "logs" / "daily_gnss.log",
        "archive_rinex": PROJECT_DIR / "logs" / "archive_rinex.log",
    }
    now = utcnow()

    for name, path in logs.items():
        if not path.exists():
            record(WARN, f"cron/{name}", "No log file yet -- has it ever run?")
            continue

        age_h = (now - datetime.fromtimestamp(path.stat().st_mtime,
                                              tz=timezone.utc)).total_seconds() / 3600

        # Look for an explicit success marker in the tail.
        try:
            tail = path.read_text(errors="replace")[-4000:]
        except OSError:
            tail = ""
        succeeded = "finished successfully" in tail
        errored = "finished with errors" in tail

        last_line = ""
        for line in reversed(tail.strip().split("\n")):
            if line.strip():
                last_line = line.strip()
                break

        if age_h > 36:
            record(FAIL, f"cron/{name}",
                   f"Last ran {age_h:.0f}h ago -- expected daily")
        elif errored and not succeeded:
            record(FAIL, f"cron/{name}", f"Last run reported errors: {last_line[:120]}")
        elif errored:
            record(WARN, f"cron/{name}",
                   f"Ran {age_h:.0f}h ago; recent history includes errors")
        else:
            record(OK, f"cron/{name}", f"Ran {age_h:.0f}h ago, reported success")


# ------------------------------------------------------------------
# 5. Is the station still recording?
# ------------------------------------------------------------------
def check_recording(db: sqlite3.Connection) -> None:
    try:
        out = subprocess.run(["pgrep", "-f", "station_manager.py"],
                             capture_output=True, text=True, timeout=10)
        running = bool(out.stdout.strip())
    except Exception:
        running = False

    if running:
        record(OK, "recording", "station_manager.py is running")
    else:
        record(FAIL, "recording",
               "station_manager.py is NOT running -- no data is being recorded")

    # Is the newest raw file actually growing? This is the real
    # question -- a status flag can drift out of date, a file that
    # stopped growing cannot be misinterpreted.
    raw_dir = PROJECT_DIR / "raw"
    if raw_dir.is_dir():
        raws = sorted(raw_dir.glob("*.um980"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if raws:
            newest = raws[0]
            age_min = (utcnow() - datetime.fromtimestamp(
                newest.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
            if age_min > 90:
                record(FAIL, "recording",
                       f"{newest.name} has not been written to for "
                       f"{age_min:.0f} minutes -- recording may have stopped")
            else:
                record(OK, "recording",
                       f"{newest.name} written {age_min:.0f} min ago")
        else:
            record(WARN, "recording", "No raw files present")

    row = list(db.execute(
        "SELECT timestamp, disk_free, receiver_connected, internet_connected "
        "FROM system_health ORDER BY id DESC LIMIT 1"))
    if not row:
        record(WARN, "health", "No system_health records")
        return

    ts, disk_free, receiver_connected, internet_connected = row[0]
    dt = parse_ts(ts)
    if dt:
        age_h = (utcnow() - dt).total_seconds() / 3600
        if age_h > 36:
            record(WARN, "health",
                   f"Newest health record is {age_h:.0f}h old")
        else:
            record(OK, "health", f"Health recorded {age_h:.0f}h ago")

    # system_health.receiver_connected is deliberately NOT trusted
    # here. On this station it reads 0 on every record going back
    # days, while recording demonstrably works -- station_manager.py
    # evidently never sets it. Asserting a failure on the strength of
    # a field nobody maintains produces false alarms, and false
    # alarms are how a monitoring report earns being ignored.
    #
    # Recording is verified directly instead, below.


# ------------------------------------------------------------------
# 6. Disk headroom
# ------------------------------------------------------------------
def check_disk() -> None:
    try:
        st = os.statvfs(str(PROJECT_DIR))
        free_gb = st.f_bavail * st.f_frsize / 1e9
        total_gb = st.f_blocks * st.f_frsize / 1e9
        pct_used = 100 * (1 - st.f_bavail / st.f_blocks)
    except Exception as exc:
        record(WARN, "disk", f"Could not check disk: {exc}")
        return

    msg = f"{free_gb:.0f} GB free of {total_gb:.0f} GB ({pct_used:.0f}% used)"
    if free_gb < 20:
        record(FAIL, "disk", msg + " -- critically low")
    elif free_gb < 50:
        record(WARN, "disk", msg + " -- getting low")
    else:
        record(OK, "disk", msg)


# ------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7,
                   help="how far back to look for errors (default 7). Seven "
                        "rather than two or three deliberately: a failure that "
                        "recurs once a day only looks recurring over a window "
                        "wide enough to contain several instances, and 'this "
                        "has happened every day for a week' is a materially "
                        "different signal from 'this happened twice'.")
    p.add_argument("--verbose", action="store_true",
                   help="also show checks that passed")
    args = p.parse_args()

    station_code = "usgs"
    since = None
    d = {}
    station_json = PROJECT_DIR / "station" / "resources" / "station.json"
    if station_json.exists():
        try:
            d = json.loads(station_json.read_text())
            station_code = (d.get("gnssrefl_station_code")
                            or (d.get("station_id") or "")[:4]).lower() or "usgs"
            since = gnss_record.record_first_day(station_json)
        except Exception:
            pass

    db_path = PROJECT_DIR / "database" / "station.db"
    if not db_path.exists():
        print(f"FAIL  database not found at {db_path}")
        return 1

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    check_recent_errors(db, args.days)
    check_processing_currency(station_code, since)
    check_water_level_record(station_code, since)
    check_arc_stats(station_code)
    check_public_plot(station_code)
    check_tide_coverage(station_code, d if isinstance(d, dict) else {})
    check_raw_backlog()
    check_cron_ran()
    check_recording(db)
    check_disk()

    db.close()

    fails = [f for f in findings if f[0] == FAIL]
    warns = [f for f in findings if f[0] == WARN]

    print("=" * 68)
    print(f"  Station health -- {utcnow().strftime('%Y-%m-%d %H:%M')}Z")
    print("=" * 68)

    for status, check, message in findings:
        if status == OK and not args.verbose:
            continue
        print(f"  [{status:4}] {check}: {message}")

    print()
    if fails:
        print(f"  {len(fails)} problem(s) need attention, {len(warns)} warning(s).")
        return 1
    if warns:
        print(f"  No failures. {len(warns)} warning(s) worth a look.")
        return 0
    print("  Everything looks healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
