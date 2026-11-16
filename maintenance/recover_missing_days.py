#!/usr/bin/env python3
"""
recover_missing_days.py

Re-processes UTC days that have no GNSS-IR results, from the raw
receiver files the station keeps. maintenance/recover_missing_days.sh
runs it; process_and_plot.sh Step 1 calls that every night.

WHY THIS REPLACED THE SHELL VERSION (review B12)

The shell version looked only for RINEX 3 long-named files
(USGS00USA_R_<year><doy>0000_01D_01S_MO.rnx) under one hard-coded
export folder (/mnt/I2Rgus_Data/GPS_Data/Products). Those were the
copies gnssrefl_processor.py stages for rinex2snr. It deletes them
after every run now (they were 350-400 MB each and piled up), so the
exports made since then carry none, and the script found nothing to
recover. It also hard-coded the project folder ($HOME/GNSS/v4.1), the
station codes (usgs, usgs00usa) and the sample rate (01S), and told
the user to run process_gps_data.sh, which no longer exists.

WHERE A DAY IS RECOVERED FROM, in this order

  1. archive/station_YYYYMMDD.um980. pipeline.py moves a raw file here
     once its day has been processed. Its results can still go missing
     later (deleted to reprocess with new settings, or lost with
     products/). It also moves here a day it gives up on: no SNR file
     with any orbit, the fallbacks included, once the day is more than
     5 days old (or 72 h old and failed 10 times; pipeline.py, "A day
     that can never give results"). Those are the days this script
     retries every night, until their orbit comes or they are given
     by hand.
  2. <external_storage_path>/Raw/station_YYYYMMDD.um980.
     station_manager.py moves a day's raw file here when it exports the
     day: once the day has results, or, without results, once it is
     export_retry_window_days old. The second case is the day this
     script exists for: its orbit was not out in time.
  3. <external_storage_path>/Products/**/<STA9>_R_<YYYY><DDD>0000_01D_<RR>S_MO.rnx,
     the staged RINEX that older exports still carry.

The raw file is the full record of the day, so it comes first. It is
converted again with station/rinex_processor.py (the same convbin flags
as the pipeline), into a scratch folder, so nothing is added to rinex/ (which compress_rinex.sh, the S3 sync and
prune_local_rinex.sh manage). The raw file itself is only read.
The scratch folder is also the working directory, where gnssrefl's
rinex2snr copies the day's RINEX (350-400 MB) and leaves it
(gnssrefl_processor.py now removes that copy itself, too). It is
emptied after every day and removed at the end; a scratch folder
more than a day old (left by a run that was killed) is removed at
the start of the next run.

rinex/ is not a source. A conversion there may be of the first hours
of a day only (pipeline.py explains how a day still being recorded
used to be converted), and a day recovered from it would then look
complete and never be tried again.

raw/ is left alone. A day there is still the pipeline's: it retries it
on every run and moves it to archive/ when it succeeds, which would
move it from under this script. Today's UTC day is never recovered
either: station_manager.py is still recording it.

Processing is station/gnssrefl_processor.py's process(), as in the
pipeline: the same station codes, sample rate, orbit source, fallback
orbits and gnssir strategy, all from station.json, and the results land
in the station's own results folder. For a day that ended more than
72 h ago, process() tries gnssrefl_orbit_fallbacks ("rapid", then
"nav") when the configured orbit makes no SNR file -- "nav", GPS only,
only for a day more than 5 days old. A day made with a fallback orbit
is said so, and listed in the summary: to redo it with the multi-GNSS
orbit later, delete its results and give it by hand. To reprocess
days with the configured orbit only, set "gnssrefl_orbit_fallbacks": []
in station.json for that run.

WHAT CAN HAPPEN TO A DAY

  recovered   gnssir wrote a results file with data rows.
  no data     rinex2snr wrote a fresh SNR file, and gnssir ran through
              its quality control but kept no arc.
              A <doy>.no_data marker is written next to the results, so
              the automatic search does not try the day every night
              (pipeline.py writes the same marker for such a day).
              Delete it to try again, e.g. after changing the elevation
              or RH settings in station.json.
  not yet     no SNR file with any orbit tried. For a day under 72 h
              old the orbit is most often not published yet (it
              routinely takes 1-2 days); for an older one the fallback
              orbits were tried too, so it is no orbit at all, no
              network, or a RINEX without usable data. Also a day whose
              only SNR file came from the GPS-only fallback and kept no
              arc: a multi-GNSS orbit may still give arcs. Tried again
              on the next run. No marker.
  failed      convbin failed on the raw file, rinex2snr raised with
              every orbit tried or gnssir raised (a gnssrefl error is not
              a lack of data; one orbit raising while another simply had
              none is "not yet"), gnssir stopped before its quality
              control, or it did not replace an existing results file.
              Tried again on the next run.

The shell version wrote the marker whenever gnssir left no results
file, whatever the reason, so a day whose orbit was not out yet could
be marked "no data" for good. A fresh SNR file is what tells the two
apart: rinex2snr cannot write one without an orbit.

WHICH DAYS

Automatic (no days given): every day of --year (default: this UTC
year) that has a source above, no results and no marker, from --since
on (process_and_plot.sh passes gnss_record_first_day: days before it
are not part of the water-level record).

Given by hand (days of year): tried even if they have results or a
marker, to reprocess them. --since does not apply.

CODES AND PATHS

The project folder is the one this script lives in. Station codes,
sample rate, REFL_CODE and external_storage_path come from
station/resources/station.json, through station/config.py and the same
rules gnssrefl_processor.py uses, so the results folder searched here
is the one the pipeline writes. When archive.conf exists, its
STATION_CODE is checked against station.json's: the S3 uploads read
the same results folder, so a mismatch is reported.

The station database is not changed, as before: a recovered day's
leftover processing_queue entry is removed by clean_processing_queue.py
once the day has results.

ONE WRITER AT A TIME

Recovering a day rewrites the station's gnssir json and runs rinex2snr
and gnssir on the station's shared folders, as a pipeline run does. So
the days are recovered holding the pipeline's run lock
(logs/pipeline.lock, station/pipeline.py run_lock): a pipeline run in
progress, e.g. station_manager's midnight run still working through a
backlog, is waited for, up to an hour. Past that the script stops with
exit 1 and the days are tried again on the next run. A lock that cannot
be taken for any other reason is logged and the days are recovered
without it.

Usage:
    maintenance/recover_missing_days.sh                  automatic
    maintenance/recover_missing_days.sh 204 205           those days of this UTC year
    RECOVER_YEAR=2026 maintenance/recover_missing_days.sh 364 365
    maintenance/recover_missing_days.sh --dry-run         list days and sources only
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# This script lives in maintenance/, so the project root is one
# level up.
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "station"))

# Only constants and the marker helpers: nothing of gnssrefl is
# imported until a day is processed.
from gnssrefl_processor import (  # noqa: E402
    OUTCOME_NO_ARCS,
    OUTCOME_NO_ARCS_GPS_ONLY,
    OUTCOME_NO_SNR,
    OUTCOME_OK,
    no_data_marker_path,
    write_no_data_marker,
)

# station_manager.py names each UTC day's raw file station_YYYYMMDD.um980
# (_raw_file_for_day). Other names are one-off recordings.
RAW_NAME = re.compile(r"^station_(\d{8})\.um980$")

# gnssrefl_processor.py's staged name (_stage_rinex_file()); older
# exports carry these.
LEGACY_RINEX = r"^{code9}_R_(\d{{4}})(\d{{3}})0000_01D_\d\dS_MO\.RNX$"

# Slack on "written during this attempt", for file systems that keep
# modification times to the second only.
FRESH_SLACK_S = 2.0

# gnssrefl_processor.py's messages when rinex2snr raised with every
# orbit tried, or gnssir raised (_process_staged(), _no_snr_message()):
# a gnssrefl error, "failed" whatever else the day left.
STAGE_FAILED = ("rinex2snr failed:", "gnssir failed:")

# tempfile.mkdtemp() prefix of the scratch folder in the project root.
SCRATCH_PREFIX = ".recover_missing_days_"

# A scratch folder older than this was left by a run that was killed.
STALE_SCRATCH_S = 24 * 3600

ARCHIVE = "archive/"
EXTERNAL_RAW = "external Raw/"
EXTERNAL_RINEX = "external Products/ (RINEX)"


@dataclass
class Source:
    """Where one day's data was found."""

    day: date
    kind: str
    path: Path

    @property
    def is_raw(self) -> bool:
        return self.kind != EXTERNAL_RINEX


@dataclass
class Station:
    """The codes and folders gnssrefl_processor.py works with."""

    code4: str
    code9: str
    sample_rate: int
    refl_code: Path

    def results_file(self, day: date) -> Path:
        doy = day.timetuple().tm_yday
        return (self.refl_code / str(day.year) / "results" / self.code4
                / f"{doy:03d}.txt")

    def no_data_marker(self, day: date) -> Path:
        # Not zero-padded: the name the shell version wrote, and the
        # one clean_processing_queue.py reads (and pipeline.py writes).
        return no_data_marker_path(self.results_file(day).parent, day)

    def snr_files(self, day: date) -> list[Path]:
        # gnssrefl gps.snr_name(): <sta><DDD>0.<YY>.snr<type>, maybe
        # compressed afterwards.
        doy = day.timetuple().tm_yday
        folder = self.refl_code / str(day.year) / "snr" / self.code4
        return sorted(folder.glob(f"{self.code4}{doy:03d}0.{day:%y}.snr*"))


def station_from_config(cfg) -> Station:
    """
    The station codes, sample rate and REFL_CODE, by
    gnssrefl_processor.py's own rules (its private helpers, on
    purpose: the folder searched for results must be the folder its
    process() writes, and those rules have changed before). Nothing
    is imported from gnssrefl here.
    """

    from gnssrefl_processor import GnssIrProcessor

    processor = GnssIrProcessor(cfg=cfg)
    section = getattr(cfg, "station", {}) or {}
    code4 = processor._resolve_station_code(section)
    code9 = processor._resolve_station_code_9ch(section, code4)
    refl_code, _orbits, _exe = processor._resolve_directories()

    return Station(
        code4=code4,
        code9=code9,
        sample_rate=int(section.get("gnssrefl_sample_rate", 1)),
        refl_code=Path(refl_code),
    )


def archive_conf_station_code(project_dir: Path) -> str | None:
    """STATION_CODE from archive.conf, or None if absent/unset."""

    path = project_dir / "archive.conf"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None

    for line in lines:
        match = re.match(r"""^\s*STATION_CODE=["']?([^"'#\s]*)""", line)
        if match:
            return match.group(1) or None
    return None


def has_results(path: Path) -> bool:
    """A results file with at least one data row (not just its header)."""

    try:
        with open(path) as handle:
            for line in handle:
                stripped = line.strip()
                if stripped and not stripped.startswith("%"):
                    return True
    except OSError:
        return False
    return False


def _raw_day(path: Path) -> date | None:
    match = RAW_NAME.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def find_sources(cfg, station: Station, external: Path | None,
                 year: int) -> dict[date, Source]:
    """
    Every day of `year` with data to recover from, each with its best
    source (see the module docstring for the order).
    """

    sources: dict[date, Source] = {}

    def add(day, kind, path):
        if day is not None and day.year == year and day not in sources:
            sources[day] = Source(day=day, kind=kind, path=path)

    for path in sorted(Path(cfg.archive_dir).glob("station_*.um980")):
        add(_raw_day(path), ARCHIVE, path)

    if external is not None:
        for path in sorted((external / "Raw").glob("station_*.um980")):
            add(_raw_day(path), EXTERNAL_RAW, path)

        pattern = re.compile(LEGACY_RINEX.format(code9=re.escape(station.code9.upper())))
        products = external / "Products"
        if products.is_dir():
            for path in sorted(products.rglob("*")):
                match = pattern.match(path.name.upper())
                if not match or not path.is_file():
                    continue
                file_year, doy = int(match.group(1)), int(match.group(2))
                try:
                    day = date(file_year, 1, 1) + timedelta(days=doy - 1)
                except (OverflowError, ValueError):
                    continue
                if day.year == file_year:
                    add(day, EXTERNAL_RINEX, path)

    return sources


def days_in_raw_dir(cfg) -> set[date]:
    """Days whose raw file is still in raw/ (the pipeline's to process)."""

    return {
        day for day in (_raw_day(p) for p in Path(cfg.raw_dir).glob("station_*.um980"))
        if day is not None
    }


def _empty(folder: Path) -> None:
    """Remove everything in `folder`, keeping the folder."""
    for entry in folder.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            pass


def _remove_stale_scratch(root: Path, now: float) -> None:
    """Scratch folders of earlier runs that were killed (SIGKILL, power loss)."""
    for folder in root.glob(SCRATCH_PREFIX + "*"):
        try:
            if folder.is_dir() and now - folder.stat().st_mtime > STALE_SCRATCH_S:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def _fresh(path: Path, since: float) -> bool:
    try:
        return path.stat().st_mtime >= since - FRESH_SLACK_S
    except OSError:
        return False


class Recovery:
    """
    Recovers one day at a time. The processors are built on first
    use (gnssrefl's is slow to initialize, and writes the station's
    gnssir json), and can be replaced in tests: rinex_factory(scratch)
    returns a converter writing into `scratch`, gnssir_factory() a
    GNSS-IR processor.
    """

    def __init__(self, cfg, station: Station, scratch: Path,
                 rinex_factory=None, gnssir_factory=None):
        self.cfg = cfg
        self.station = station
        self.scratch = scratch
        self._rinex_factory = rinex_factory or self._real_rinex
        self._gnssir_factory = gnssir_factory or self._real_gnssir
        self._rinex = None
        self._gnssir = None
        # The last day's GNSS-IR result, for main() to say which orbit
        # made it (None when the day did not get that far).
        self.last_result = None

    def _real_rinex(self, scratch: Path):
        from rinex_processor import RinexProcessor

        # The pipeline's converter and flags, writing into the scratch
        # folder instead of rinex/.
        scratch_cfg = copy.copy(self.cfg)
        scratch_cfg.rinex_dir = scratch
        processor = RinexProcessor(cfg=scratch_cfg)
        processor.initialize()
        return processor

    def _real_gnssir(self):
        from gnssrefl_processor import GnssIrProcessor

        processor = GnssIrProcessor(cfg=self.cfg)
        processor.initialize()
        return processor

    def recover(self, source: Source) -> tuple[str, str]:
        """Process one day. Returns (outcome, detail); see the module docstring."""

        day = source.day
        results = self.station.results_file(day)
        started = time.time()
        converted: list[Path] = []
        self.last_result = None

        try:
            if source.is_raw:
                if self._rinex is None:
                    self._rinex = self._rinex_factory(self.scratch)
                conversion = self._rinex.convert(source.path)
                converted = [Path(p) for p in (conversion.observation_file,
                                               conversion.navigation_file,
                                               conversion.sbas_file) if p]
                if not conversion.success:
                    return "failed", f"convbin: {conversion.message}"
                observation_file = Path(conversion.observation_file)
            else:
                observation_file = source.path

            if self._gnssir is None:
                self._gnssir = self._gnssir_factory()
            # No retry_count: for a day more than 72 h old, its age alone
            # lets process() try the fallback orbits.
            result = self._gnssir.process(observation_file, day=day)
            self.last_result = result

        finally:
            for path in converted:
                try:
                    path.unlink()
                except OSError:
                    pass
            # Whatever else the day left in the scratch folder (the
            # working directory): rinex2snr's copy of the RINEX.
            _empty(self.scratch)

        if has_results(results) and _fresh(results, started):
            marker = self.station.no_data_marker(day)
            if marker.exists():
                marker.unlink()
            return "recovered", result.message

        if str(result.message).startswith(STAGE_FAILED):
            # rinex2snr raised with every orbit tried, or gnssir raised: a
            # gnssrefl error, not a missing orbit. Checked before the SNR
            # file, which a raising rinex2snr never leaves (the processor
            # removes whatever such an attempt wrote), and before the
            # outcome, which is "no_snr" for it (the pipeline's hand-off
            # rule treats it as a day without an SNR file).
            return "failed", result.message

        if any(_fresh(p, started) for p in self.station.snr_files(day)):
            # What the processor says it was, when it says (a stand-in
            # may not): a fresh SNR file alone is not enough, since one
            # orbit's attempt may have left one while another made none.
            outcome = getattr(result, "outcome", "")
            if outcome in (OUTCOME_NO_SNR, OUTCOME_NO_ARCS_GPS_ONLY):
                return "not yet", result.message
            if results.exists() and not _fresh(results, started):
                return "failed", ("gnssir did not replace the existing "
                                  f"results file: {result.message}")
            if outcome and outcome not in (OUTCOME_NO_ARCS, OUTCOME_OK):
                # gnssir stopped before its quality control: not a
                # lack of data, never a marker
                return "failed", result.message
            write_no_data_marker(results.parent, day)
            return "no data", result.message

        return "not yet", result.message


def _fallback_orbit(result) -> str:
    """The fallback orbit that made the day's SNR file, or "" (the
    configured orbit, or no result)."""

    tried = tuple(getattr(result, "orbits_tried", ()) or ())
    used = getattr(result, "orbit_used", "") or ""
    return used if used and len(tried) > 1 and used != tried[0] else ""


def _parse_since(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        print(f"  Ignoring --since {text!r}: not YYYY-MM-DD.")
        return None


def _interrupt(signum, frame):
    # KeyboardInterrupt, not SystemExit: gnssrefl_processor.py catches
    # SystemExit around rinex2snr and gnssir and would carry on with
    # the next day. The scratch folder is removed on the way out.
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv=None, cfg=None, now: datetime | None = None,
         rinex_factory=None, gnssir_factory=None,
         lock_wait_s: float | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doys", nargs="*", type=int,
                        help="days of year to recover (default: find them)")
    parser.add_argument("--year", type=int, default=None,
                        help="year of those days (default: this UTC year)")
    parser.add_argument("--since", default=None,
                        help="automatic mode: skip days before YYYY-MM-DD")
    parser.add_argument("--external", default=None,
                        help="external storage folder (default: station.json's "
                             "external_storage_path)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the days and where each would come from")
    args = parser.parse_args(argv)

    now = now or datetime.now(timezone.utc)
    today = now.date()
    year = args.year or today.year

    if cfg is None:
        try:
            from config import Config
            cfg = Config()
        except Exception as exc:
            print(f"  Could not load station.json: {exc}")
            return 1

    station = station_from_config(cfg)
    section = getattr(cfg, "station", {}) or {}
    external_text = args.external or section.get("external_storage_path") or ""
    external = Path(external_text) if external_text else None

    print(f"  Station: {station.code4} ({station.code9}), year {year}")
    print(f"  Results: {station.refl_code / str(year) / 'results' / station.code4}")

    conf_code = archive_conf_station_code(Path(getattr(cfg, "project_root", PROJECT_DIR)))
    if conf_code and conf_code.lower() != station.code4:
        print(f"  WARNING: archive.conf STATION_CODE={conf_code!r} differs from "
              f"station.json's {station.code4!r}; the S3 uploads read the "
              f"results of {conf_code!r}.")

    if external is None:
        print("  No external_storage_path in station.json: looking in archive/ only.")
    elif not external.is_dir():
        print(f"  External storage not available at {external}: looking in archive/ only.")
        external = None
    else:
        print(f"  External storage: {external}")

    sources = find_sources(cfg, station, external, year)
    in_raw = days_in_raw_dir(cfg)
    since = _parse_since(args.since)

    todo: list[Source] = []
    if args.doys:
        print(f"  Days given: {' '.join(str(d) for d in args.doys)}")
        unrecoverable = []
        for doy in args.doys:
            try:
                day = date(year, 1, 1) + timedelta(days=doy - 1)
            except (OverflowError, ValueError):
                day = None
            if day is None or day.year != year or doy < 1:
                print(f"  {doy}: not a day of {year}.")
                unrecoverable.append(doy)
            elif day >= today:
                print(f"  {doy} ({day}): still being recorded (UTC) -- not recovered.")
            elif day in in_raw:
                print(f"  {doy} ({day}): still in raw/, the pipeline's to process.")
            elif day not in sources:
                print(f"  {doy} ({day}): no raw file in archive/ or external Raw/, "
                      f"and no RINEX in external Products/.")
                unrecoverable.append(doy)
            else:
                todo.append(sources[day])
    else:
        print("  No days given -- looking for days with data but no results...")
        skipped_before_since = 0
        for day in sorted(sources):
            if day >= today or day in in_raw:
                continue
            if since is not None and day < since:
                skipped_before_since += 1
                continue
            if has_results(station.results_file(day)):
                continue
            if station.no_data_marker(day).exists():
                continue
            todo.append(sources[day])
        if skipped_before_since:
            print(f"  Skipped {skipped_before_since} day(s) of {year} before "
                  f"{args.since} (gnss_record_first_day).")
        if not sources:
            print("  No raw files or RINEX for this year in archive/ or external storage.")

    if not todo:
        print("  Nothing to recover.")
        return 0

    print(f"  {len(todo)} day(s) to recover:")
    for source in todo:
        doy = source.day.timetuple().tm_yday
        print(f"    {doy:3d} ({source.day})  from {source.kind}: {source.path}")

    if args.dry_run:
        print("  DRY RUN -- nothing was processed.")
        return 0

    from pipeline import PIPELINE_LOCK_WAIT_S, PipelineBusyError, run_lock

    project_root = Path(getattr(cfg, "project_root", PROJECT_DIR))
    _remove_stale_scratch(project_root, time.time())
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=str(project_root)))
    outcomes: dict[str, list[int]] = {}
    with_fallback: list[str] = []
    previous_handlers = {}
    previous_cwd = os.getcwd()
    held = contextlib.ExitStack()
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, _interrupt)
        # The pipeline's run lock, for every day: see ONE WRITER AT A
        # TIME in the module docstring.
        try:
            held.enter_context(run_lock(
                getattr(cfg, "logs_dir", project_root / "logs"),
                PIPELINE_LOCK_WAIT_S if lock_wait_s is None else lock_wait_s,
                logging.getLogger("recover_missing_days")))
        except PipelineBusyError as exc:
            print(f"  !! Stopped: {exc}; tried again on the next run.")
            return 1
        # gnssrefl drops working copies into the current directory
        # and leaves them: keep them in scratch, which recover()
        # empties after every day.
        os.chdir(str(scratch))

        recovery = Recovery(cfg, station, scratch, rinex_factory, gnssir_factory)
        for source in todo:
            doy = source.day.timetuple().tm_yday
            print("")
            print(f"  Day {doy} ({source.day}) from {source.kind}")
            try:
                outcome, detail = recovery.recover(source)
            except Exception as exc:
                # The processors report every per-day failure in their
                # result; an exception is a setup failure (convbin or
                # gnssrefl missing, the gnssir json not writable), and
                # every other day would fail the same way.
                print(f"  !! Stopped: {type(exc).__name__}: {exc}")
                return 1
            print(f"     {outcome}: {detail}")
            outcomes.setdefault(outcome, []).append(doy)
            fallback = _fallback_orbit(recovery.last_result)
            if fallback and outcome in ("recovered", "no data"):
                print(f"     made with fallback orbit {fallback}")
                with_fallback.append(f"{doy} ({fallback})")

    except KeyboardInterrupt as exc:
        print(f"\n  Interrupted ({exc or 'Ctrl+C'}); the scratch folder is removed.")
        return 130

    finally:
        held.close()
        os.chdir(previous_cwd)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        shutil.rmtree(scratch, ignore_errors=True)

    print("")
    print("  Summary")
    for outcome, label in (
        ("recovered", "Recovered"),
        ("no data", "Processed, no usable retrievals (marker written)"),
        ("not yet", "No SNR file with any orbit tried (orbit not published yet, "
                    "no network, or no usable RINEX), tried again next run"),
        ("failed", "Failed, tried again next run"),
    ):
        days = outcomes.get(outcome)
        if days:
            print(f"    {label}: {' '.join(str(d) for d in days)}")
    if with_fallback:
        print(f"    Made with a fallback orbit (rapid: no BeiDou; nav: GPS only, "
              f"about half the tracks): {' '.join(with_fallback)}")
    if outcomes.get("recovered"):
        print("  The next process_and_plot.sh run (nightly, via daily_gnss.sh)")
        print("  refits the record with the recovered day(s).")

    return 0


if __name__ == "__main__":
    # The processors report convbin, rinex2snr and gnssir outcomes
    # through logging, as in station_manager.py.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    sys.exit(main())
