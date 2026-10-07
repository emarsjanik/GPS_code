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

  results  $REFL_CODE/<year>/results/<sta>/<doy>.txt of EVERY year
           (optionally from --since on): the source of truth. Nothing
           here changes them.

  fit      Each night subdaily fits from 1 January of the oldest year
           that is not frozen (plus PAD_DAYS of the year before, so a
           year never starts at the edge of a fit) to the newest
           result, across the year boundary with -year_end, into its
           own folder Files/<sta>/fit/ (-subdir). At most a year and a
           few weeks, so the nightly run time is bounded.
           subdaily crashes when its azimuth window leaves a year of
           the range empty, which is exactly what a short first day of
           January (or a short last pad day) does. An edge year with
           fewer than MIN_EDGE_ARCS arcs in the window is therefore left
           out of tonight's fit -- the pad is dropped, or the new year
           waits a night -- instead of refitting the whole record
           without the window.

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
           shorter record can never silently replace a longer one. Only
           then are due years frozen (from tonight's fit, so tomorrow's
           record equals tonight's) and the record replaced, atomically,
           0644. Levels of a frozen year written with another antenna
           height are rewritten to the fit's (level = Hortho - RH; RH
           does not depend on it -- a datum change, not an antenna move).
           The <sta>_<year>_subdaily_edit.txt files of the years the fit
           covers in full are copied next to it (filter_month.py and the
           reflection audits read those). A freshly frozen year leaves
           fit/s3_upload_year_<year> for s3UploadTimeseries.sh, which
           re-uploads that year's months once.

  status   fit/record_status.json says how the last update went
           (diagnostics/station_health.py reports it in the daily mail).

USAGE (process_and_plot.sh, every night)

    gnss_record.py --refl-code $REFL_CODE --station usgs summary
    gnss_record.py --refl-code $REFL_CODE --station usgs update \\
        --settings "-rhdot True -knots 8 -azim1 35 -azim2 125" \\
        --fallback-settings "-rhdot True -knots 8"
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
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
MJD_EPOCH = date(1858, 11, 17)
AZ_COL = 5               # gnssir results: year doy RH sat UTC Azim ...


def mjd(d: date) -> int:
    return (d - MJD_EPOCH).days


def mjd_date(m: float) -> date:
    return MJD_EPOCH + timedelta(days=int(m + 1e-6))


def row_date(line: str) -> date:
    c = line.split()
    return date(int(c[2]), int(c[3]), int(c[4]))


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
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copyfile(src, tmp)
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

    @property
    def fit_days(self):
        return [d for d in self.days if self.start <= d <= self.end]

    def to_freeze(self) -> list[int]:
        """Complete years in tonight's fit that the fit runs far enough past."""
        return [y for y in range(self.first_live, self.end.year)
                if self.end >= date(y, 12, 31) + timedelta(days=FREEZE_AFTER_DAYS)]


def make_plan(refl_code, sta: str, settings: str, since: date | None = None) -> Plan | None:
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
    first_live = years[len(frozen)]
    window = azimuth_window(settings)
    notes = []

    def arcs(y, lo, hi):
        return sum(arcs_in_window(p, window) for d, p in days.items()
                   if d.year == y and lo <= d <= hi)

    # the pad: only if the year before has enough arcs in it to be fitted
    pad_from = date(first_live, 1, 1) - timedelta(days=PAD_DAYS)
    start = min(d for d in days if d >= pad_from)
    if start.year < first_live and arcs(start.year, start, date(start.year, 12, 31)) < MIN_EDGE_ARCS:
        notes.append(f"pad {start}..{start.year}-12-31 has under {MIN_EDGE_ARCS} arcs in the "
                     f"azimuth window -- fit starts 1 January")
        start = min(d for d in days if d.year >= first_live)
    # the newest year: waits a night if it is too thin to be fitted
    end = newest
    if newest.year > start.year and arcs(newest.year, date(newest.year, 1, 1), newest) < MIN_EDGE_ARCS:
        earlier = [d for d in days if start <= d < date(newest.year, 1, 1)]
        if earlier:
            end = earlier[-1]
            notes.append(f"{newest.year} so far has under {MIN_EDGE_ARCS} arcs in the azimuth "
                         f"window -- it waits; tonight's fit ends {end}")
    return Plan(days, newest, frozen, first_live, start, end, fp, notes)


# ----------------------------------------------------------------
# Status (read by diagnostics/station_health.py)
# ----------------------------------------------------------------

def write_status(files_dir: Path, **kw) -> None:
    fit_dir = files_dir / FIT_SUBDIR
    fit_dir.mkdir(parents=True, exist_ok=True)
    kw["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_atomic(fit_dir / STATUS_NAME, [json.dumps(kw, default=str, indent=1) + "\n"])


# ----------------------------------------------------------------
# The record
# ----------------------------------------------------------------

def compose(plan: Plan, files_dir: Path, sta: str, settings: str, freeze: bool = True,
            allow_shrink: bool = False, used: str | None = None) -> int:
    """Check, then freeze what is due and write <sta>_spline_out.txt from
    the frozen years and tonight's fit. Returns 0, or 1 with nothing written."""
    fit_dir = files_dir / FIT_SUBDIR
    fit_file = fit_dir / f"{sta}_spline_out.txt"
    used = used or settings

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

    def rows_of(year, rows_):
        lo, hi = mjd(date(year, 1, 1)) - eps, mjd(date(year + 1, 1, 1)) - eps
        return [line for m, line in rows_ if lo <= m < hi]

    body, parts = [], []
    for y in plan.frozen:
        fh, frows = read_spline(frozen_path(files_dir, sta, y))
        h_y = header_hortho(fh)
        body += [rehortho(line, h_y, h_fit) for line in rows_of(y, frows)]
        parts.append(f"{y} frozen")
    body += [line for m, line in rows if m >= live_from - eps]
    parts.append(f"from {need_from} the fit of {plan.start}..{plan.end}")
    if used.split() != settings.split():
        parts.append(f"FALLBACK settings '{used}' (not frozen)")
    if not body:
        return fail("nothing to write")

    # ---- guards, before anything is written ----
    record = files_dir / f"{sta}_spline_out.txt"
    new_dates = {row_date(l) for l in body}
    if record.exists() and not allow_shrink:
        _, old = read_spline(record)
        if old:
            old_dates = {row_date(l) for _, l in old}
            if float(body[0].split()[0]) > old[0][0] + 1.0:
                return fail(f"the new record would start {min(new_dates)}, later than the current "
                            f"one ({min(old_dates)}): every frame in between would lose its water "
                            f"level (--allow-shrink if intended)")
            lost = sorted(old_dates - new_dates)
            if len(lost) > MAX_LOST_DAYS:
                shown = ", ".join(map(str, lost[:6])) + (" ..." if len(lost) > 6 else "")
                return fail(f"the new record has no readings on {len(lost)} day(s) the current "
                            f"one covers ({shown}) (--allow-shrink if intended)")

    # ---- freeze (from tonight's fit, so tomorrow's record equals tonight's) ----
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    frozen_now = []
    if freeze:
        for y in plan.to_freeze():
            yr = rows_of(y, rows)
            if not yr:
                continue
            write_atomic(frozen_path(files_dir, sta, y),
                         frozen_text(head, settings, plan.fp[y], yr,
                                     f"{y} frozen {stamp} from the fit of {plan.start}..{plan.end}"))
            (fit_dir / f"{UPLOAD_MARK}{y}").touch()
            frozen_now.append(y)
            print(f"  froze {y}: {frozen_path(files_dir, sta, y).name}")

    write_atomic(record, head[:1]
                 + [f"%  record: {'; '.join(parts)} (analysis_tools/gnss_record.py)\n"]
                 + head[1:] + body)

    for y in range(plan.first_live, plan.end.year + 1):
        src = fit_dir / f"{sta}_{y}_subdaily_edit.txt"
        if src.exists():
            copy_atomic(src, files_dir / src.name)

    first, last = min(new_dates), max(new_dates)
    write_status(files_dir, ok=True, fallback=used.split() != settings.split(), settings=used,
                 record_first=first, record_last=last, fit_start=plan.start, fit_end=plan.end,
                 newest_results=plan.newest, frozen=plan.frozen + frozen_now, notes=plan.notes,
                 message="; ".join(parts))
    print(f"  record: {len(body)} readings, {first} to {last} UTC ({'; '.join(parts)})")
    return 0


def run_subdaily(plan: Plan, files_dir: Path, sta: str, settings: str) -> bool:
    """One subdaily run of tonight's range into Files/<sta>/fit/. True if it
    wrote a spline: its exit status alone does not say (it exits 0 when it
    finds no results, or when doy1 > doy2)."""
    y1, d1, y2, d2 = subdaily_range(plan.fit_days)
    fit_dir = files_dir / FIT_SUBDIR
    fit_dir.mkdir(parents=True, exist_ok=True)
    spline = fit_dir / f"{sta}_spline_out.txt"
    for old in [spline, *fit_dir.glob(f"{sta}_[0-9][0-9][0-9][0-9]_subdaily_*.txt")]:
        old.unlink(missing_ok=True)               # nothing of an earlier run is taken for tonight's
    cmd = ["subdaily", sta, str(y1), "-doy1", str(d1), "-year_end", str(y2), "-doy2", str(d2),
           "-subdir", f"{sta}/{FIT_SUBDIR}"] + shlex.split(settings)
    log = fit_dir / "subdaily.log"
    print("  " + " ".join(cmd))
    env = dict(os.environ, REFL_CODE=str(files_dir.parent.parent))   # the same REFL_CODE
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
    print(f"  Results for {len(days)} day(s), {min(days)} to {max(days)} ({years})")
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
    print(f"  Fitting {plan.start} to {plan.end}"
          + (f"; frozen: {', '.join(map(str, plan.frozen))}" if plan.frozen else ""))
    for n in plan.notes:
        print(f"  NOTE: {n}")
    if (plan.end - plan.start).days > LIVE_SPAN_WARN_DAYS:
        print(f"  WARNING: tonight's fit spans {(plan.end - plan.start).days} days -- "
              f"a year is not being frozen (see {STATUS_NAME})")
    tries = [settings] + ([fallback] if fallback and fallback.split() != settings.split() else [])
    try:
        subdaily_range(plan.fit_days)
    except ValueError as exc:
        print(f"  ERROR: {exc} -- the record keeps last night's")
        write_status(files_dir, ok=False, message=str(exc), fit_start=plan.start,
                     fit_end=plan.end, newest_results=plan.newest)
        return 1
    for i, s in enumerate(tries):
        if i:
            print(f"  WARNING: retrying the whole fit with {s} -- used tonight, never frozen")
        if run_subdaily(plan, files_dir, sta, s):
            return compose(plan, files_dir, sta, settings, freeze=(i == 0),
                           allow_shrink=allow_shrink, used=s)
    print("  ERROR: no fit tonight -- the record keeps last night's")
    write_status(files_dir, ok=False, message="subdaily wrote no spline", fit_start=plan.start,
                 fit_end=plan.end, newest_results=plan.newest)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refl-code", default=os.environ.get("REFL_CODE"),
                   help="gnssrefl REFL_CODE (default: $REFL_CODE)")
    p.add_argument("--station", required=True, help="gnssrefl 4-character station code")
    p.add_argument("--since", type=date.fromisoformat, default=None,
                   help="ignore results before this day (YYYY-MM-DD), e.g. installation tests")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary", help="list the results days (exit 3 if none)")
    u = sub.add_parser("update", help="fit, freeze and write the record")
    u.add_argument("--settings", required=True, help="subdaily options, e.g. \"-rhdot True -knots 8\"")
    u.add_argument("--fallback-settings", help="tried if the first fit fails; never frozen")
    u.add_argument("--allow-shrink", action="store_true",
                   help="accept a record that starts later, or covers fewer days, than the current one")
    a = p.parse_args()
    if not a.refl_code:
        p.error("--refl-code or $REFL_CODE is required")
    if a.cmd == "summary":
        return cmd_summary(a.refl_code, a.station, a.since)
    return cmd_update(a.refl_code, a.station, a.settings, a.fallback_settings, a.allow_shrink,
                      a.since)


if __name__ == "__main__":
    sys.exit(main())
