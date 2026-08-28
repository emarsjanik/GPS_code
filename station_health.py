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

PROJECT_DIR = Path(__file__).resolve().parent

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
    m = re.search(r"/(\d{1,3})\.txt", text)
    if not m:
        return False
    doy = int(m.group(1))
    today_doy = int(utcnow().strftime("%j"))
    # Today and yesterday are both legitimately incomplete: yesterday's
    # UTC day only closes at 00:00 today, and is processed that evening.
    return doy >= today_doy - 1


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
def check_processing_currency(station_code: str) -> None:
    results_dir = None
    base = PROJECT_DIR / "products" / "refl_code"
    if base.is_dir():
        for year_dir in sorted(base.glob("[0-9][0-9][0-9][0-9]"), reverse=True):
            candidate = year_dir / "results" / station_code
            if candidate.is_dir():
                results_dir = candidate
                break

    if results_dir is None:
        record(WARN, "processing", "No results directory found yet")
        return

    days = sorted(int(p.stem) for p in results_dir.glob("*.txt")
                  if p.stem.isdigit())
    if not days:
        record(FAIL, "processing", "Results directory exists but contains no days")
        return

    today_doy = int(utcnow().strftime("%j"))
    newest = days[-1]
    lag = today_doy - newest

    # A two-day lag is this pipeline's normal steady state, not a
    # symptom. A UTC day cannot be processed until it has finished
    # recording (00:00 UTC the following day), and processing runs
    # once nightly at 22:30 local -- so the freshest result is
    # routinely two day-numbers behind "today".
    if lag <= 2:
        record(OK, "processing",
               f"{len(days)} days processed, newest is doy {newest} "
               f"({lag}d behind, which is normal for a nightly run)")
    elif lag <= 4:
        record(WARN, "processing",
               f"Newest result is doy {newest}, {lag} days behind today "
               f"({today_doy}) -- a run may have been missed")
    else:
        record(FAIL, "processing",
               f"Newest result is doy {newest}, {lag} days behind today ({today_doy}) "
               f"-- processing appears stalled")

    # Interior gaps, ignoring days explicitly marked as having no
    # usable data.
    gaps = []
    for a, b in zip(days, days[1:]):
        for missing in range(a + 1, b):
            if not (results_dir / f"{missing}.no_data").exists():
                gaps.append(missing)
    if gaps:
        shown = ", ".join(str(g) for g in gaps[:8])
        more = f" (+{len(gaps) - 8} more)" if len(gaps) > 8 else ""
        record(WARN, "processing", f"Gap(s) in the record: doy {shown}{more}")


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
    station_json = PROJECT_DIR / "station" / "resources" / "station.json"
    if station_json.exists():
        try:
            d = json.loads(station_json.read_text())
            station_code = (d.get("gnssrefl_station_code")
                            or (d.get("station_id") or "")[:4]).lower() or "usgs"
        except Exception:
            pass

    db_path = PROJECT_DIR / "database" / "station.db"
    if not db_path.exists():
        print(f"FAIL  database not found at {db_path}")
        return 1

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    check_recent_errors(db, args.days)
    check_processing_currency(station_code)
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
