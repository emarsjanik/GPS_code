"""
station_manager.py

USGS GNSS Reference Station
Prototype 1.0

The top-level orchestrator for continuous, unattended station
operation. Turns the individually-tested modules already built
(receiver.py, database.py, pipeline.py, rinex_processor.py,
gnssrefl_processor.py) into an actual reference station that keeps
running on its own, day after day, without an operator watching it.

Mirrors the established pattern across this project: a class with
initialize()/run()/stop()/status()/shutdown(), duck-typed config
access, and resilient error handling -- a single failure (a bad
recording chunk, a pipeline error) is logged and the manager keeps
going, rather than crashing the whole unattended process.

Daily cycle
-----------
    StationManager.run()
        |
        v
    for each UTC day:
        |
        +-- record in chunks (default 1 hour each), appending to
        |   one growing raw file for the day (also across a restart),
        |   checking for a requested stop between chunks
        |
        +-- once the day rolls over, hand the day just closed to a
        |   background thread, which runs the processing pipeline
        |   (raw -> RINEX -> GNSS-IR -> archive) over whatever's
        |   accumulated, then records a system health snapshot,
        |   then exports the day (if configured)
        |
        +-- start recording the new day at once, while that runs
        |
        +-- repeat, unless a stop was requested

Recording does not wait for the pipeline
------------------------------------------
The pipeline used to run inline, between the last chunk of one day
and the first chunk of the next. Converting a ~360 MB day and running
rinex2snr and gnssir takes minutes, so every day lost its first
minutes after 00:00 UTC: no data was recorded while it ran. That gap
is under 5 h, so gnssrefl's subdaily spline bridged it with
interpolated values, as if they had been observed (review B8).

The run is not dropped, although the 22:30 cron runs the same
pipeline: it is the run the record depends on. Until verified finding
H1 was fixed, the cron's pipeline step imported pipeline.py by a path
relative to cron's working directory, so with the documented crontab
it never ran, and this was the only run that processed anything. The
two now share Pipeline.run()'s lock (logs/pipeline.lock), so they
never overlap: whichever comes second waits for the other, up to an
hour (pipeline.PIPELINE_LOCK_WAIT_S). Instead of running inline, the
run, the health snapshot and the export go to one background thread,
in that order, and the main thread goes straight back to recording.

A thread rather than a separate process, because everything the run
needs is already safe to share. database.py opens SQLite with
check_same_thread=False and serialises every call with a lock, and
the main thread does not touch the database while it records.
Neither processor changes the working directory or installs a signal
handler, and gnssrefl_processor.py already catches the SystemExit
that gnssrefl raises on some error paths. The recorder writes only
today's raw file, which pipeline.py leaves alone ("still recording"),
and the export moves the closed day's raw file only after the
pipeline has finished with it, in the same thread. Run from a
process, the injected Pipeline (tests, shared orchestration) and the
last summary in status() would be lost.

If the work for one rollover is still running when the next day
closes (a large backlog), the next day's work waits for it, so two
pipeline runs never overlap. A stop request waits for the running
work to finish before run() returns, as it waited for the inline run
before. "manager_pipeline_in_background": false in station.json
restores the old inline order, as a way back if the thread ever
causes trouble on the station (inline, a wait for the cron's run to
release the lock also holds back recording).

Chunked recording, not one call per day
-----------------------------------------
record_raw() has no built-in way to stop early -- only a fixed
`duration`. Calling it once for an entire day would mean a shutdown
signal (systemd stopping the service, a planned power-down) couldn't
take effect until the whole day finished. Recording in configurable
chunks (record_raw_chunk_seconds in station.json, default 3600) and
checking for a stop request *between* chunks bounds the worst-case
shutdown latency to one chunk, and means a single bad chunk (e.g. a
USB hiccup) only risks losing that chunk's data, not the whole day's.
Chunks are stitched into one growing per-day file using record_raw()'s
own `append=True` -- no manual file-concatenation logic needed. The
first chunk of a process appends too when the day's file already holds
data: a restart mid-day (a watchdog restart, a kill, a deploy) used to
open the file fresh and lose every hour recorded before it.

Day boundaries are always UTC, matching RINEX/GNSS convention and how
the rest of this project already treats dates (pipeline.py's own
per-file dates are UTC-based already).

Graceful shutdown
------------------
SIGTERM and SIGINT are both caught and simply set a stop-requested
flag (checked between chunks, and between days) -- a signal handler
must stay minimal, so it does not attempt anything more complex than
that itself.

This module knows nothing about SQLite schema details, RINEX
parsing, or GNSS-IR analysis -- it only calls the already-established
public APIs of database.py, pipeline.py, and receiver.py, the same
way pipeline.py never touches rinex_processor.py's or
gnssrefl_processor.py's internals directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import logging
import os
import shutil
import signal
import threading
import time

from config import Config
from database import Database
from pipeline import Pipeline, PipelineSummary
from receiver import Receiver, ReceiverError


# ----------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------

@dataclass
class StationManagerStatus:
    """Returned by status(): a snapshot for dashboards/monitoring."""

    initialized: bool = False
    running: bool = False
    stop_requested: bool = False
    current_day: str = ""
    chunks_recorded_today: int = 0
    last_pipeline_summary: PipelineSummary | None = None
    last_health_check: datetime | None = None


# ----------------------------------------------------------------
# StationManager
# ----------------------------------------------------------------

@dataclass
class StationManager:
    """
    Orchestrates continuous, unattended station operation.

    Parameters
    ----------
    cfg:
        A config.Config instance. If not given, initialize() builds
        one itself.
    db:
        A database.Database instance, shared with the internal
        Pipeline. If not given, initialize() builds and connects one
        itself.
    pipeline:
        A pre-built Pipeline instance. If not given, initialize()
        builds one itself. Either way, initialize() always calls
        pipeline.initialize() on it (matching Pipeline's own
        defensive handling of an injected db that isn't yet
        connected) -- mainly useful for tests, or for sharing a
        Pipeline across other orchestration.

    Typical usage (this is also this module's own __main__ block):

        manager = StationManager()
        manager.initialize()
        manager.run()   # blocks until stopped (Ctrl+C, SIGTERM, or
                         # manager.stop() called from elsewhere)
        manager.shutdown()
    """

    cfg: Config | None = None
    db: Database | None = None
    pipeline: Pipeline | None = None

    _owns_cfg: bool = field(default=False, init=False, repr=False)
    _owns_db: bool = field(default=False, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _stop_requested: bool = field(default=False, init=False, repr=False)
    _pipeline: Pipeline | None = field(default=None, init=False, repr=False)
    _chunk_seconds: float = field(default=3600.0, init=False, repr=False)
    _retry_delay_seconds: float = field(default=60.0, init=False, repr=False)
    _health_check_every_seconds: float = field(
        default=3600.0, init=False, repr=False
    )
    _export_retry_window_days: int = field(default=3, init=False, repr=False)
    # See "Recording does not wait for the pipeline" in the module
    # docstring.
    _pipeline_in_background: bool = field(default=True, init=False, repr=False)
    _rollover_thread: threading.Thread | None = field(
        default=None, init=False, repr=False
    )
    _external_storage_path: Path | None = field(
        default=None, init=False, repr=False
    )
    _current_day: date | None = field(default=None, init=False, repr=False)
    _chunks_recorded_today: int = field(default=0, init=False, repr=False)
    _last_summary: PipelineSummary | None = field(
        default=None, init=False, repr=False
    )
    _last_health_check: datetime | None = field(
        default=None, init=False, repr=False
    )
    _logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("station.manager"),
        init=False,
        repr=False,
    )

    # ==========================================================
    # Public API
    # ==========================================================

    def initialize(self) -> str:
        """
        Connect the database, initialize the processing pipeline,
        read configuration, and register signal handlers for
        graceful shutdown.

        Returns "READY" on success.
        """

        if self.cfg is None:
            self.cfg = Config()
            self._owns_cfg = True

        if self.db is None:
            self.db = Database.from_config(self.cfg)
            self.db.connect()
            self._owns_db = True
        elif not self.db.is_connected():
            self.db.connect()

        station_section = getattr(self.cfg, "station", {}) or {}

        self._chunk_seconds = float(
            station_section.get("record_raw_chunk_seconds", 3600.0)
        )
        self._retry_delay_seconds = float(
            station_section.get("manager_retry_delay_seconds", 60.0)
        )
        self._health_check_every_seconds = float(
            station_section.get("health_check_interval_seconds", 3600.0)
        )
        self._export_retry_window_days = int(
            station_section.get("export_retry_window_days", 3)
        )
        self._pipeline_in_background = bool(
            station_section.get("manager_pipeline_in_background", True)
        )

        external_storage = station_section.get("external_storage_path")
        self._external_storage_path = (
            Path(external_storage) if external_storage else None
        )

        if self.pipeline is not None:
            self._pipeline = self.pipeline
        else:
            self._pipeline = Pipeline(cfg=self.cfg, db=self.db)

        self._pipeline.initialize()

        self._register_signal_handlers()

        self._initialized = True

        self._logger.info(
            "StationManager initialized: chunk=%.0fs retry_delay=%.0fs "
            "health_check_every=%.0fs export_retry_window=%dd "
            "pipeline_in_background=%s",
            self._chunk_seconds,
            self._retry_delay_seconds,
            self._health_check_every_seconds,
            self._export_retry_window_days,
            self._pipeline_in_background,
        )

        return "READY"

    def run(self) -> None:
        """
        The main loop. Blocks until stop() is called (directly, or
        via a caught SIGTERM/SIGINT), recording each UTC day in
        chunks and running the processing pipeline once each day's
        recording finishes -- in a background thread, while the next
        day records (see the module docstring).

        A failure recording one chunk, or running the pipeline for a
        day, is logged and the loop continues -- this is meant to
        run completely unattended, so a single failure must never
        take down the whole process.

        Before returning, waits for any pipeline run still in
        progress, so shutdown() never closes the database under it.
        """

        if not self._initialized:
            raise RuntimeError(
                "StationManager.initialize() must be called before run()"
            )

        self._running = True
        self._logger.info("StationManager starting main loop")

        while not self._stop_requested:
            self._current_day = self._today()
            self._chunks_recorded_today = 0

            self._logger.info("Beginning recording for %s", self._current_day)

            self._record_day(self._current_day)

            if self._stop_requested:
                break

            self._start_rollover_work(self._current_day)

        self._wait_for_rollover_work()

        self._running = False
        self._logger.info("StationManager main loop stopped")

    def stop(self) -> None:
        """
        Request a graceful stop. Takes effect between recording
        chunks (or between days), not immediately -- see the module
        docstring for why record_raw() can't be interrupted mid-call.
        """

        self._logger.info("Stop requested")
        self._stop_requested = True

    def status(self) -> StationManagerStatus:
        """Return a snapshot of the manager's current state."""

        return StationManagerStatus(
            initialized=self._initialized,
            running=self._running,
            stop_requested=self._stop_requested,
            current_day=self._current_day.isoformat() if self._current_day else "",
            chunks_recorded_today=self._chunks_recorded_today,
            last_pipeline_summary=self._last_summary,
            last_health_check=self._last_health_check,
        )

    def shutdown(self) -> None:
        """Close the database connection, if this manager opened it."""

        self._logger.info("Shutting down StationManager")

        # run() already waited; this covers a run() that was left by
        # an exception (e.g. a KeyboardInterrupt before the signal
        # handlers were registered).
        self._wait_for_rollover_work()

        if self._pipeline is not None:
            self._pipeline.shutdown()

        if self.db is not None and self._owns_db:
            self.db.close()

        self._initialized = False

    # ==========================================================
    # Private: recording
    # ==========================================================

    def _today(self) -> date:
        """The current UTC calendar day. A separate method so tests can override it."""

        return datetime.now(timezone.utc).date()

    def _seconds_until_next_utc_midnight(self) -> float:
        now = datetime.now(timezone.utc)
        tomorrow = now.date() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)

        return (midnight - now).total_seconds()

    def _raw_file_for_day(self, day: date) -> Path:
        assert self.cfg is not None

        return self.cfg.raw_dir / f"station_{day:%Y%m%d}.um980"

    @staticmethod
    def _append_first_chunk(raw_file: Path) -> bool:
        """
        True when the day's raw file already holds data from an earlier
        run, so this process's first chunk must append, not overwrite.
        A restart that cut the earlier run mid-message leaves a partial
        message at the join; RINEX conversion skips to the next sync
        word, so that costs one epoch, not the hours before it.
        """

        try:
            return raw_file.stat().st_size > 0
        except OSError:
            return False

    def _record_day(self, day: date) -> None:
        """
        Record chunks for `day` until the day rolls over to UTC
        midnight, or a stop is requested. Each chunk appends to one
        growing raw file for the day (record_raw()'s own append=True).
        The first chunk creates the file, or appends to it when an
        earlier run already recorded part of the day (see
        _append_first_chunk).
        """

        raw_file = self._raw_file_for_day(day)

        while not self._stop_requested and self._today() == day:
            remaining = self._seconds_until_next_utc_midnight()

            if remaining <= 0:
                break

            chunk_duration = min(self._chunk_seconds, remaining)

            self._record_one_chunk(raw_file, chunk_duration)

            self._chunks_recorded_today += 1

    def _record_one_chunk(self, raw_file: Path, duration: float) -> None:
        """
        Record one chunk. A failure here (receiver disconnected,
        zero bytes captured, anything else) is logged and the caller
        continues to the next chunk after a short delay -- never
        propagated up to crash run()'s main loop.
        """

        append = self._chunks_recorded_today > 0 or self._append_first_chunk(raw_file)

        self._logger.info(
            "Recording chunk #%d for %.0fs (append=%s) -> %s",
            self._chunks_recorded_today + 1,
            duration,
            append,
            raw_file,
        )

        try:
            with self._make_receiver() as rx:
                result = rx.record_raw(
                    raw_file,
                    duration=duration,
                    append=append,
                    enable_logging=True,
                )

            self._logger.info(
                "Chunk complete: %d byte(s), %d message(s)",
                result.bytes_written,
                result.messages_written,
            )

        except Exception as exc:
            self._logger.error(
                "Recording chunk failed, will retry after %.0fs: %s",
                self._retry_delay_seconds,
                exc,
            )
            self._sleep_unless_stopping(self._retry_delay_seconds)

    def _make_receiver(self) -> Receiver:
        """
        Isolated in its own method (rather than inlined in
        _record_one_chunk()) so tests can substitute a fake Receiver
        class without needing to fake serial at the module level.

        Confirmed against station.py's own real, working usage:
        Receiver takes device/baudrate/timeout directly, not a cfg
        object.
        """

        assert self.cfg is not None

        return Receiver(
            device=self.cfg.receiver_port,
            baudrate=self.cfg.receiver_baud,
            timeout=self.cfg.receiver_timeout,
        )

    def _sleep_unless_stopping(self, seconds: float) -> None:
        """
        Sleep in short increments so a stop request is noticed
        promptly rather than only after the full delay elapses.
        """

        deadline = time.monotonic() + seconds

        while not self._stop_requested and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    # ==========================================================
    # Private: pipeline / health
    # ==========================================================

    def _start_rollover_work(self, day: date) -> None:
        """
        Hand the work for the day just closed (pipeline, health
        snapshot, export) to a background thread and return at once,
        so the caller can start recording the new day. With
        manager_pipeline_in_background false, do the work here
        instead, as run() used to.

        The thread waits for the previous rollover's thread first, so
        the work of two days never runs at the same time.
        """

        if not self._pipeline_in_background:
            self._logger.info(
                "Recording for %s complete; running pipeline", day
            )
            self._rollover_work(day, previous=None)
            return

        previous = self._rollover_thread
        if previous is not None and previous.is_alive():
            self._logger.warning(
                "Pipeline work for an earlier day is still running; the "
                "work for %s will start when it finishes",
                day,
            )
        else:
            previous = None

        self._logger.info(
            "Recording for %s complete; running pipeline in the "
            "background while the next day records",
            day,
        )

        thread = threading.Thread(
            target=self._rollover_work,
            args=(day, previous),
            name=f"rollover-{day:%Y%m%d}",
        )
        self._rollover_thread = thread
        thread.start()

    def _rollover_work(self, day: date, previous: threading.Thread | None) -> None:
        """
        The work that follows a day's recording, in its required
        order: the pipeline first, then the health snapshot, then the
        export (which may move the day's raw file, so it must come
        after the pipeline is done with it).

        Never raises. _run_pipeline_safely(), _health_check() and the
        export already catch every Exception; what is left is
        SystemExit, which gnssrefl raises on some error paths
        (gnssrefl_processor.py catches it around rinex2snr and gnssir,
        but not elsewhere). Uncaught in a thread it would end the
        thread with nothing in the station log; inline it used to end
        the whole manager. Here it is logged, the rest of that day's
        work is skipped, and recording carries on.
        """

        try:
            if previous is not None:
                previous.join()

            self._run_pipeline_safely()
            self._health_check()
            self._export_day_to_external_storage(day)

        except (Exception, SystemExit) as exc:
            self._logger.error(
                "Pipeline work for %s failed (non-fatal): %s", day, exc
            )

    def _wait_for_rollover_work(self) -> None:
        """Block until the background pipeline work (if any) is done."""

        thread = self._rollover_thread
        if thread is None or thread is threading.current_thread():
            return

        if thread.is_alive():
            self._logger.info(
                "Waiting for the pipeline run in progress to finish"
            )

        thread.join()
        self._rollover_thread = None

    def _run_pipeline_safely(self) -> None:
        assert self._pipeline is not None

        try:
            self._last_summary = self._pipeline.run()
        except Exception as exc:
            self._logger.error("Pipeline run failed: %s", exc)

    def _health_check(self) -> None:
        """
        Record a periodic system health snapshot via database.py's
        existing (previously unused) save_system_health(). Never
        raises -- a health-check failure must not stop the main loop.
        """

        assert self.cfg is not None
        assert self.db is not None

        try:
            disk_usage = shutil.disk_usage(str(self.cfg.project_root))
            disk_used_percent = (
                (disk_usage.used / disk_usage.total) * 100
                if disk_usage.total
                else None
            )

            self.db.save_system_health(
                cpu_usage=self._read_cpu_load(),
                memory_usage=self._read_memory_percent(),
                disk_usage=disk_used_percent,
                disk_free=disk_usage.free,
                database_size=self.db.database_statistics().size_bytes,
                receiver_connected=False,  # no Receiver held open between chunks
                internet_connected=True,  # not actively checked; see docstring note below
                newest_raw_file=self._latest_name(self.db.latest_raw_file()),
                newest_rinex=self._latest_name(self.db.latest_rinex()),
                newest_product="",
            )

            self._last_health_check = datetime.now(timezone.utc)

            self._logger.info("Health check recorded")

        except Exception as exc:
            self._logger.error("Health check failed (non-fatal): %s", exc)

    def _station_code(self) -> str:
        """
        The 4-character gnssrefl station code -- matches the exact
        same derivation gnssrefl_processor.py uses (explicit override
        via gnssrefl_station_code if set, otherwise the first 4
        characters of station_id, lowercased), so this stays
        consistent with whatever the pipeline actually processed
        under. Duplicated here deliberately rather than imported --
        a few lines of simple, stable logic, safer than adding a
        cross-module dependency into gnssrefl_processor.py's
        internals just for this one small lookup.
        """

        assert self.cfg is not None
        station_section = getattr(self.cfg, "station", {}) or {}

        explicit = station_section.get("gnssrefl_station_code")
        if explicit:
            return explicit

        station_id = station_section.get("station_id", "")
        return station_id[:4].lower()

    def _has_real_results_for_day(self, day: date) -> bool:
        """
        Checks whether a real GNSS-IR results file already exists
        for this day -- i.e. whether the pipeline actually succeeded
        in producing usable output, not just that it was attempted.

        Confirmed against real results files: they always have at
        least one non-comment data line when processing genuinely
        succeeded; a missing file, or one containing only its header
        comments, means processing hasn't succeeded yet -- most
        commonly because that day's official orbit product hasn't
        been published yet (confirmed to routinely take 1-2 days).
        """

        assert self.cfg is not None

        station_code = self._station_code()
        if not station_code:
            return False

        doy = day.timetuple().tm_yday
        results_path = (
            Path(self.cfg.products_dir)
            / str(day.year)
            / "results"
            / station_code
            / f"{doy}.txt"
        )

        if not results_path.exists():
            return False

        try:
            with open(results_path) as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("%"):
                        return True
        except OSError:
            return False

        return False

    def _day_is_ready_to_export(self, day: date) -> bool:
        """
        Confirmed, real gap this exists to close: the automatic daily
        cycle used to export a day's raw data and wipe products/
        immediately after one single processing attempt -- but that
        attempt almost always happens before the day's official
        orbit product is published (routinely a 1-2 day delay), so
        it predictably failed, and the day was then permanently lost
        with nothing ever retrying it.

        A day is ready to export once either:
          - it already has real GNSS-IR results (processing
            genuinely succeeded), or
          - it's older than the configured retry window, in which
            case we give up waiting and export anyway, so local
            storage doesn't grow unbounded waiting on a day whose
            orbit may never publish (or some other permanent problem)

        Until then, the day's raw data and products/ are left in
        place specifically so the pipeline's own existing retry logic
        (it already reprocesses anything still pending in
        processing_queue on every run, provided the underlying raw
        file can still be found) gets further chances to succeed on
        a later cycle, once the orbit becomes available.
        """

        if self._has_real_results_for_day(day):
            return True

        age_days = (self._today() - day).days
        return age_days >= self._export_retry_window_days

    def _export_day_to_external_storage(self, day: date) -> None:
        """
        Move the day's raw capture, and copy what changed in the
        gnssrefl working directory (products/, i.e. $REFL_CODE and
        everything under it), to external storage. products/ itself
        stays where it is: it holds the whole water-level record (see
        _export_products_directory()).

        Disabled by default -- only runs if "external_storage_path"
        is set in station.json.

        Confirmed real gap this now guards against: previously ran
        regardless of whether the day's pipeline processing actually
        succeeded, which permanently lost any day whose orbit product
        wasn't published yet by export time (routinely a 1-2 day
        delay) -- see _day_is_ready_to_export() for the real fix.
        A day without real results yet, but still within the retry
        window, is simply left in place for a future cycle rather
        than exported.

        Never raises. The local raw file is only removed after a
        confirmed-successful move (shutil.move()'s own behavior), so
        a failure here (e.g. external storage temporarily unmounted)
        never loses data -- it just leaves that day's files sitting
        locally, to be picked up by the next successful export
        attempt, or investigated by hand.
        """

        if self._external_storage_path is None:
            return

        assert self.cfg is not None

        if not self._day_is_ready_to_export(day):
            self._logger.info(
                "Delaying export for %s -- no real GNSS-IR results yet "
                "(most likely the orbit product isn't published yet) "
                "and still within the %d-day retry window; leaving "
                "local data in place for a future retry instead of "
                "losing the chance permanently",
                day,
                self._export_retry_window_days,
            )
            return

        try:
            if not self._external_storage_path.is_dir():
                self._logger.error(
                    "External storage path is not available (not "
                    "mounted, or not a directory): %s -- skipping "
                    "export for %s",
                    self._external_storage_path,
                    day,
                )
                return

            raw_destination_dir = self._external_storage_path / "Raw"
            products_destination_dir = self._external_storage_path / "Products"
            raw_destination_dir.mkdir(parents=True, exist_ok=True)
            products_destination_dir.mkdir(parents=True, exist_ok=True)

            self._export_raw_file(day, raw_destination_dir)
            self._export_products_directory(day, products_destination_dir)

        except Exception as exc:
            self._logger.error(
                "Export to external storage failed (non-fatal) for %s: %s",
                day,
                exc,
            )

    def _export_raw_file(self, day: date, destination_dir: Path) -> None:
        """
        Find the day's raw file -- it may still be in raw/ (pipeline
        processing failed or hasn't run) or already in archive/
        (pipeline processing succeeded, pipeline.py's own archiving
        already moved it there) -- and move whichever copy exists to
        external storage. A no-op, not an error, if neither exists
        (e.g. a day with zero successful recording chunks).
        """

        assert self.cfg is not None

        raw_filename = self._raw_file_for_day(day).name
        candidates = [
            self.cfg.raw_dir / raw_filename,
            getattr(self.cfg, "archive_dir", self.cfg.raw_dir) / raw_filename,
        ]

        for candidate in candidates:
            if candidate.exists():
                destination = destination_dir / raw_filename
                shutil.move(str(candidate), str(destination))
                self._logger.info(
                    "Exported raw file to external storage: %s -> %s",
                    candidate,
                    destination,
                )
                return

        self._logger.info(
            "No raw file found for %s in raw/ or archive/ -- nothing to "
            "export",
            day,
        )

    # Recognized gnssrefl category names -- confirmed against the
    # real, actual structure of a live products/ directory (not
    # assumed): results, snr, and input each nest under a year and/or
    # station code; Files and logs sit at slightly different depths;
    # multiple station codes can appear side by side if the
    # station's own code was ever changed, and every one of them
    # needs to be kept, not just whichever is current now.
    _KNOWN_EXPORT_CATEGORIES = (
        "results", "snr", "Files", "logs", "input",
        "phase", "rinex", "orbits", "exe",
    )

    # Written into the external Products/ folder after each export
    # that copied everything it found: the time that export started.
    # Only files changed since then are copied by the next one (see
    # below).
    _EXPORT_MARKER = ".last_external_export"

    # How far before the previous export's start a file's change time
    # may lie and still be copied again. File times come from the
    # kernel's coarse clock (a few ms behind time.time()), some
    # filesystems keep whole seconds, and an NTP step can set the
    # clock back a little: a file changed just after the previous
    # export started can carry a time just before it, and would never
    # be exported. Copying a few files twice costs nothing. A larger
    # step is not covered by this; see the marker check in
    # _export_products_directory().
    _EXPORT_TIME_SLACK_S = 60.0

    @staticmethod
    def _changed_time(stat_result) -> float:
        """
        When a file last changed, for the export: the later of its
        mtime and ctime. A file put in place with an old mtime --
        moved in (a rename keeps the mtime), restored with cp -p,
        rsync -a or tar, or copied with shutil.copy2 as
        gnssrefl_processor._stage_rinex_file does -- has an mtime
        before the last export but a fresh ctime, which no tool can
        set back. By mtime alone such a file was never exported.
        """

        return max(stat_result.st_mtime, stat_result.st_ctime)

    def _export_products_directory(self, day: date, destination_dir: Path) -> None:
        """
        Flatten and COPY products/ to external storage, under a
        per-day folder name (so each day's export doesn't overwrite
        the previous one) -- a no-op, not an error, if products/
        doesn't exist yet (e.g. the very first day, before any
        processing has run at all).

        products/ is never moved or deleted. It holds the whole
        water-level record, not one day's working files:
        refl_code/<every year>/results (what gnss_record refits every
        night), Files/<sta>/ (the whole-record spline, the frozen
        years, the edit files) and input/<sta>.json. This used to
        move every file and rmtree products/, so the first rollover
        whose day had results left gnss_record with one day; its
        shrink guard then refused to write, and the record stopped
        while the frozen years were gone from the station. Copying
        cannot do that.

        To keep a daily per-day copy from re-copying the whole
        record every day, only files changed since the previous
        export started are copied (see _changed_time() and
        _EXPORT_TIME_SLACK_S); the first export copies everything.
        That time is kept in the _EXPORT_MARKER file in
        `destination_dir` -- the external storage, not products/ --
        so a new or emptied external disk gets the whole record
        again rather than only what changed since the last disk was
        written. The marker is only written after an export that
        copied everything it found, so after a failed export the
        next one copies everything changed since the last good one.
        A marker later than the clock now says the clock was ahead
        when it was written (a wrong clock at boot, before NTP set
        it) and has since been set back: by it, every file changed
        since then would be skipped, write-once results/<doy>.txt
        files for good, so that export copies everything instead.
        A clock that ran ahead by less than the time to the next
        export is not caught this way: files changed after it was
        set back and before the marker's time (less the slack) are
        not copied by that export, and not by a later one unless
        they change again.
        A file that disappears while the export walks products/
        (a temporary file, a staged RINEX file being cleaned up) is
        skipped, not a failure: there is nothing left to copy.

        Confirmed necessary to flatten rather than copy the whole
        tree as-is: gnssrefl's own internal layout nests everything
        several levels deep (a year, then a category, then a station
        code, sometimes another year) purely for its own working
        needs. This walks every real file, identifies which known
        category it belongs to from its actual path (not an assumed
        fixed layout), and re-homes it under
        <category>/<what sat above the category>/<station code>/...
        -- the default refl_code/ level is dropped, and so is nothing
        else. In particular the year above results/ and snr/ is KEPT
        (results/<year>/<sta>/<doy>.txt): a results file's name
        carries no year, and collapsing that level let 2025's
        292.txt and 2026's 292.txt overwrite each other in the
        export. Likewise another REFL_CODE beside refl_code/ (the
        same-day check's refl_code_ultra/) keeps its own folder, so
        its 4-day spline cannot land on, and replace, the
        whole-record Files/<sta>/<sta>_spline_out.txt. Files from
        different station codes stay apart by the station code's
        own subfolder level. Anything that doesn't match a known
        category is kept under "other/" with its original relative
        path intact, rather than silently dropped, in case gnssrefl
        ever adds a new category this doesn't already know about.
        """

        assert self.cfg is not None

        products_dir = Path(self.cfg.products_dir)

        if not products_dir.exists():
            self._logger.info(
                "No products/ directory found for %s -- nothing to export",
                day,
            )
            return

        destination = destination_dir / f"{day:%Y%m%d}"

        if destination.exists():
            self._logger.error(
                "Export destination already exists, refusing to overwrite: "
                "%s -- leaving local products/ in place",
                destination,
            )
            return

        marker = destination_dir / self._EXPORT_MARKER
        try:
            since = float(marker.read_text().strip())
        except (OSError, ValueError):
            since = None
        # Taken before the walk: a file changed while it runs is
        # copied again next time rather than missed.
        started = time.time()
        if since is not None and since > started:
            self._logger.warning(
                "Last export marker %s is %.0f s after the clock now "
                "(the clock was ahead when it was written) -- copying "
                "all of products/ this time",
                marker,
                since - started,
            )
            since = None

        files_copied = 0
        files_vanished = 0

        for root, _dirs, files in os.walk(products_dir):
            if not files:
                continue

            root_path = Path(root)
            relative_parts = root_path.relative_to(products_dir).parts

            category = "other"
            remainder_parts = relative_parts
            for index, part in enumerate(relative_parts):
                if part in self._KNOWN_EXPORT_CATEGORIES:
                    category = part
                    above = relative_parts[:index]
                    if above[:1] == ("refl_code",):
                        above = above[1:]
                    remainder_parts = above + relative_parts[index + 1:]
                    break

            dest_subdir = (
                destination / category / Path(*remainder_parts)
                if remainder_parts
                else destination / category
            )

            for filename in files:
                source = root_path / filename
                try:
                    if (since is not None and self._changed_time(source.stat())
                            < since - self._EXPORT_TIME_SLACK_S):
                        continue
                    dest_subdir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(source), str(dest_subdir / filename))
                except FileNotFoundError:
                    # Gone since os.walk listed it (or a dangling
                    # symlink): nothing left to copy.
                    files_vanished += 1
                    continue
                files_copied += 1

        tmp_marker = marker.with_name(marker.name + ".tmp")
        tmp_marker.write_text(f"{started:.3f}\n")
        os.replace(str(tmp_marker), str(marker))

        self._logger.info(
            "Exported and flattened products/ to external storage "
            "(copied, products/ left in place): %s -> %s (%d files%s)",
            products_dir,
            destination,
            files_copied,
            f"; {files_vanished} vanished while copying" if files_vanished else "",
        )

    @staticmethod
    def _latest_name(record) -> str:
        """
        Safely extract a filename from whatever database.py's
        latest_raw_file()/latest_rinex() return (a dataclass, or
        None if nothing exists yet).
        """

        if record is None:
            return ""

        return getattr(record, "filename", None) or getattr(
            record, "observation_file", ""
        )

    @staticmethod
    def _read_cpu_load() -> float | None:
        """1-minute load average, as a percentage-like figure. None on non-Linux."""

        try:
            return os.getloadavg()[0]
        except (OSError, AttributeError):
            return None

    @staticmethod
    def _read_memory_percent() -> float | None:
        """Percentage of memory used, read from /proc/meminfo. None if unavailable."""

        try:
            with open("/proc/meminfo") as handle:
                lines = handle.readlines()
        except OSError:
            return None

        values = {}
        for line in lines:
            parts = line.split(":")
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            value = parts[1].strip().split()[0]
            try:
                values[key] = int(value)
            except ValueError:
                continue

        total = values.get("MemTotal")
        available = values.get("MemAvailable")

        if not total or available is None:
            return None

        return (1 - available / total) * 100

    # ==========================================================
    # Private: signal handling
    # ==========================================================

    def _register_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        # Deliberately minimal: a signal handler must not do
        # anything complex. stop() just sets a flag the main loop
        # checks between chunks/days.
        self._logger.info("Received signal %d; requesting graceful stop", signum)
        self.stop()


# ----------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    manager = StationManager()

    try:
        manager.initialize()
        manager.run()
    except KeyboardInterrupt:
        manager.stop()
    finally:
        manager.shutdown()

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
