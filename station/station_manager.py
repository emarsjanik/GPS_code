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
        |   one growing raw file for the day, checking for a
        |   requested stop between chunks
        |
        +-- once the day rolls over (or a stop was requested),
        |   run the processing pipeline (raw -> RINEX -> GNSS-IR ->
        |   archive) over whatever's accumulated
        |
        +-- record a periodic system health snapshot
        |
        +-- repeat, unless a stop was requested

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
own `append=True` -- no manual file-concatenation logic needed.

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
            "health_check_every=%.0fs",
            self._chunk_seconds,
            self._retry_delay_seconds,
            self._health_check_every_seconds,
        )

        return "READY"

    def run(self) -> None:
        """
        The main loop. Blocks until stop() is called (directly, or
        via a caught SIGTERM/SIGINT), recording each UTC day in
        chunks and running the processing pipeline once each day's
        recording finishes.

        A failure recording one chunk, or running the pipeline for a
        day, is logged and the loop continues -- this is meant to
        run completely unattended, so a single failure must never
        take down the whole process.
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

            self._logger.info(
                "Recording for %s complete; running pipeline", self._current_day
            )

            self._run_pipeline_safely()
            self._health_check()
            self._export_day_to_external_storage(self._current_day)

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

    def _record_day(self, day: date) -> None:
        """
        Record chunks for `day` until the day rolls over to UTC
        midnight, or a stop is requested. Each chunk appends to one
        growing raw file for the day (record_raw()'s own append=True,
        first chunk overwrites/creates it fresh).
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

        append = self._chunks_recorded_today > 0

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

    def _export_day_to_external_storage(self, day: date) -> None:
        """
        Move the day's raw capture and the entire gnssrefl working
        directory (products/, i.e. $REFL_CODE and everything under
        it) to external storage, then let the next day's processing
        recreate products/ from scratch as needed (initialize()
        already handles missing directories/files gracefully -- this
        just costs a small amount of redundant setup each day, e.g.
        re-downloading the EGM96 file, in exchange for a clean daily
        export).

        Disabled by default -- only runs if "external_storage_path"
        is set in station.json. Runs regardless of whether the day's
        pipeline processing succeeded or failed, since the raw
        capture should be preserved somewhere either way.

        Never raises. Local files are only removed after a
        confirmed-successful move (shutil.move()'s own behavior), so
        a failure here (e.g. external storage temporarily unmounted)
        never loses data -- it just leaves that day's files sitting
        locally, to be picked up by the next successful export
        attempt, or investigated by hand.
        """

        if self._external_storage_path is None:
            return

        assert self.cfg is not None

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

    def _export_products_directory(self, day: date, destination_dir: Path) -> None:
        """
        Flatten and move the entire products/ directory to external
        storage, under a per-day folder name (so each day's export
        doesn't overwrite the previous one) -- a no-op, not an error,
        if products/ doesn't exist yet (e.g. the very first day,
        before any processing has run at all).

        Confirmed necessary to flatten rather than move the whole
        tree as-is: gnssrefl's own internal layout nests everything
        several levels deep (a year, then a category, then a station
        code, sometimes another year) purely for its own working
        needs, not because any of that structure is meaningful once
        archived. This walks every real file, identifies which known
        category it belongs to from its actual path (not an assumed
        fixed layout), and re-homes it under a flat
        <category>/<station code>/... structure instead -- collapsing
        the redundant year/refl_code nesting while still keeping
        files from different station codes safely separated by
        keeping the station code as one subfolder level, so nothing
        from two different station codes can collide under the same
        filename. Anything that doesn't match a known category is
        kept under "other/" with its original relative path intact,
        rather than silently dropped, in case gnssrefl ever adds a
        new category this doesn't already know about.
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

        files_moved = 0

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
                    remainder_parts = relative_parts[index + 1:]
                    break

            dest_subdir = (
                destination / category / Path(*remainder_parts)
                if remainder_parts
                else destination / category
            )
            dest_subdir.mkdir(parents=True, exist_ok=True)

            for filename in files:
                shutil.move(
                    str(root_path / filename), str(dest_subdir / filename)
                )
                files_moved += 1

        shutil.rmtree(products_dir, ignore_errors=True)

        self._logger.info(
            "Exported and flattened products/ to external storage: "
            "%s -> %s (%d files)",
            products_dir,
            destination,
            files_moved,
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
