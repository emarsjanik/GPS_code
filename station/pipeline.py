"""
pipeline.py

USGS GNSS Reference Station
Prototype 1.0

The station's operations manager.

pipeline.py answers exactly one question: "What work needs to be
done next?" It never:

    * talks directly to the receiver (no `import receiver`)
    * writes SQL (only calls database.py's public API)
    * parses RINEX (that's rinex_processor.py)
    * performs GNSS-IR calculations (that's gnssrefl_processor.py)
    * reads station.json directly (only via config.Config())
    * knows the SQLite schema

Instead it coordinates the modules that already know how to do those
jobs:

    station.py
        |
        v
    pipeline.py
        |
        +---- scan raw directory
        +---- determine work to perform
        +---- queue jobs
        +---- convert RINEX            (rinex_processor.py)
        +---- run GNSS-IR              (gnssrefl_processor.py)
        +---- save products            (database.py)
        +---- update database          (database.py)
        +---- archive files
        +---- generate summary

Public API
----------
    Pipeline.initialize()      -> "READY" or raises
    Pipeline.run()              -> PipelineSummary
    Pipeline.shutdown()          -> None
    Pipeline.status()             -> PipelineStatus
    Pipeline.queue_status()        -> database.ProcessingStatistics

Everything else on Pipeline is private.

Database usage
--------------
The processing_queue lifecycle goes exclusively through:

    db.queue_file()
    db.start_processing()
    db.finish_processing()
    db.log_error()
    db.processing_statistics()

Recording *results* (Step 9, "Database updates") additionally uses
the same database.py methods station.py already relies on --
db.add_raw_file(), db.update_raw_file(), db.pending_raw_files(),
db.save_rinex(), db.save_gnssir_product(), and
db.generate_daily_summary() -- all public API, never raw SQL, and
pipeline.py still never has to know a single table or column name to
use any of them.

RINEX processor usage
----------------------
Only rinex_processor.convert() and rinex_processor.verify().
rinex_processor.py is still an empty module as of this writing;
pipeline.py calls these two functions assuming the following
contract, which rinex_processor.py should implement:

    convert(raw_path: Path, output_dir: Path, cfg: Config) -> RinexConversionResult
        where RinexConversionResult has:
            .observation_file, .navigation_file, .sbas_file (Path | str)
            .success (bool)
            .runtime (float, seconds)
            .convbin_version (str)
            .notes (str)

    verify(observation_file: Path) -> bool

Until rinex_processor.py implements these, every conversion attempt
raises AttributeError, which _process_one_job() catches like any
other per-file failure: it's logged via db.log_error(), the job is
marked FAILED, and the pipeline moves on to the next file rather
than crashing. Nothing about pipeline.py needs to change once
rinex_processor.py is implemented; it will simply start succeeding.

GNSS-IR usage
-------------
Only gnssrefl_processor.process() and gnssrefl_processor.verify(),
under the same "not implemented yet, fails gracefully" arrangement,
assuming:

    process(observation_file: Path, output_dir: Path, cfg: Config) -> GnssirResult
        where GnssirResult has:
            .success (bool)
            .runtime (float, seconds)
            .output_directory (Path | str)
            .products (list[str])
            .reflector_height, .soil_moisture, .snow_depth,
            .quality_score (float | None)
            .notes (str)

    verify(output_directory: Path, expected_products: list[str]) -> bool
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
import logging
import os
import shutil
import time
import traceback

from rinex_processor import RinexProcessor
from gnssrefl_processor import GnssIrProcessor
from config import Config
from database import Database, DatabaseError, ProcessingStatistics


# The version of this module's interface/contract (the convert()/
# verify()/process()/verify() calling conventions documented above),
# as opposed to the station application's own version (station/
# version.py). Bump this if the Pipeline public API or the
# RinexConversionResult/GnssirResult contracts change in a
# backward-incompatible way. Displayed on station.py's dashboard.
PIPELINE_VERSION = "1.0"


# ----------------------------------------------------------------
# State machine
# ----------------------------------------------------------------

class JobState(str, Enum):
    """Every raw file exists in exactly one of these states."""

    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RINEX_COMPLETE = "RINEX_COMPLETE"
    GNSSIR_COMPLETE = "GNSSIR_COMPLETE"
    ARCHIVED = "ARCHIVED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


# ----------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------

@dataclass
class ProcessingJob:
    """In-memory tracking for one file moving through the pipeline."""

    filename: str
    date: str
    priority: int = 0
    status: JobState = JobState.DISCOVERED
    retries: int = 0
    started: str | None = None
    finished: str | None = None
    raw_path: str = ""
    rinex_path: str = ""
    product_path: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineSummary:
    """The "Pipeline Summary" report generated at the end of run()."""

    files_found: int = 0
    files_processed: int = 0
    files_failed: int = 0
    rinex_created: int = 0
    products_created: int = 0
    average_runtime: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineStatus:
    """Snapshot returned by Pipeline.status()."""

    initialized: bool = False
    database_connected: bool = False
    jobs_tracked: int = 0
    last_run: PipelineSummary | None = None


# ----------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------

@dataclass
class Pipeline:
    """
    The station's operations manager.

    Parameters
    ----------
    cfg:
        A config.Config instance. If not given, initialize() builds
        one itself.
    db:
        A database.Database instance. If not given, initialize()
        builds and connects one itself (Database.from_config(cfg)).
        Passing one in lets station.py (or a test) share a single
        connection with Pipeline instead of opening a second one --
        SQLite's WAL mode supports either.

    Typical standalone usage:

        pipeline = Pipeline()
        pipeline.initialize()
        summary = pipeline.run()
        pipeline.shutdown()
    """

    cfg: Config | None = None
    db: Database | None = None

    _owns_cfg: bool = field(default=False, init=False, repr=False)
    _owns_db: bool = field(default=False, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _jobs: dict[str, ProcessingJob] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_summary: PipelineSummary | None = field(
        default=None, init=False, repr=False
    )
    _rinex: RinexProcessor | None = field(default=None, init=False, repr=False)
    _gnssir: GnssIrProcessor | None = field(default=None, init=False, repr=False)
    _run_stats: dict = field(default_factory=dict, init=False, repr=False)
    _logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("station.pipeline"),
        init=False,
        repr=False,
    )

    # ==========================================================
    # Public API
    # ==========================================================

    def initialize(self) -> str:
        """
        Connect the database, load configuration, verify directories
        and external executables, and prepare the RINEX/GNSS-IR
        processors.

        Returns "READY" on success.

        Raises
        ------
        DatabaseError
            If the database cannot be connected.
        FileNotFoundError
            If a required directory is missing.
        """

        self._logger.info("Initializing pipeline")

        if self.cfg is None:
            self.cfg = Config()
            self._owns_cfg = True

        if self.db is None:
            self.db = Database.from_config(self.cfg)
            self.db.connect()
            self._owns_db = True
        elif not self.db.is_connected():
            self.db.connect()
            self._owns_db = True

        self._verify_directories()
        self._verify_external_executables()
        self._initialize_processors()

        self._initialized = True

        self._logger.info("Pipeline initialized: READY")

        return "READY"

    def run(self) -> PipelineSummary:
        """
        Run one full pass: scan for new raw files, queue whatever
        needs work (new discoveries and previous failures alike),
        process the queue file by file, update the database, and
        return a PipelineSummary.

        A failure on any single file is caught, logged, and recorded
        against that file; it never aborts the run. Call initialize()
        first.
        """

        if not self._initialized:
            raise RuntimeError("Pipeline.initialize() must be called before run()")

        self._logger.info("Pipeline run starting")

        self._reset_run_stats()

        scanned = self._scan_raw_directory()
        new_files = self._find_new_files(scanned)

        for path in new_files:
            if self._verify_raw_file(path):
                self._queue_processing(path)
            else:
                self._logger.warning("Skipping invalid raw file: %s", path)

        self._process_queue()

        self._update_database()

        summary = self._generate_summary()
        self._log_statistics(summary)

        self._last_summary = summary

        self._logger.info("Pipeline run complete")

        return summary

    def shutdown(self) -> None:
        """Close the database connection, if this Pipeline opened it."""

        self._shutdown()

    def status(self) -> PipelineStatus:
        """Return a snapshot of the pipeline's current state."""

        return PipelineStatus(
            initialized=self._initialized,
            database_connected=self.db.is_connected() if self.db else False,
            jobs_tracked=len(self._jobs),
            last_run=self._last_summary,
        )

    def queue_status(self) -> ProcessingStatistics:
        """Return the current processing_queue counts, via the database's own API."""

        assert self.db is not None
        return self.db.processing_statistics()

    # ==========================================================
    # Private: initialize()
    # ==========================================================

    def _verify_directories(self) -> None:
        assert self.cfg is not None

        required = (
            self.cfg.raw_dir,
            self.cfg.rinex_dir,
            self.cfg.archive_dir,
            self.cfg.products_dir,
        )

        for directory in required:
            if not directory.is_dir():
                raise FileNotFoundError(f"Required directory is missing: {directory}")

    def _verify_external_executables(self) -> None:
        """
        Check for the external tools RinexProcessor and
        GnssIrProcessor depend on (RTKLIB's convbin, and the
        gnssrefl package). Missing tools are logged as warnings, not
        raised: a station with no raw files queued yet has no
        immediate need for either, and _initialize_processors()
        (which runs next) does its own, more precise check for
        gnssrefl specifically -- this is just an early, lightweight
        heads-up.
        """

        if shutil.which("convbin") is None:
            self._logger.warning(
                "convbin not found on PATH; RINEX conversion will fail "
                "until RTKLIB (demo5+) is installed or convbin_path is "
                "set in station.json"
            )

        try:
            import importlib.util

            if importlib.util.find_spec("gnssrefl") is None:
                self._logger.warning(
                    "gnssrefl package not importable; GNSS-IR "
                    "processing will fail until it is installed "
                    "(pip install gnssrefl)"
                )
        except (ImportError, ValueError):
            # find_spec() can raise ValueError (not just return None)
            # for a module already present in sys.modules without a
            # proper __spec__ -- an edge case, but this check must
            # never be able to crash initialize() over it either way.
            self._logger.warning("Could not check for the gnssrefl package")

    def _initialize_processors(self) -> None:
        """
        Construct and initialize this Pipeline's RinexProcessor and
        GnssIrProcessor, sharing this Pipeline's own cfg.

        RinexProcessor failing to initialize (convbin genuinely
        missing, output directory uncreatable) is treated as fatal,
        the same as a database or directory failure -- RINEX
        conversion is this pipeline's core, load-bearing capability.

        GnssIrProcessor failing to initialize (gnssrefl not
        installed, or its analysis-strategy setup failing) is
        treated as non-fatal: RINEX conversion and archiving can
        still proceed and be useful even without GNSS-IR processing
        available yet. self._gnssir stays None in that case, and
        _run_gnssrefl() reports a clear per-file failure for it
        rather than pipeline.py failing to initialize at all.
        """

        assert self.cfg is not None

        self._rinex = RinexProcessor(cfg=self.cfg)
        self._rinex.initialize()

        self._gnssir = GnssIrProcessor(cfg=self.cfg)

        try:
            self._gnssir.initialize()
        except Exception as exc:
            self._logger.warning(
                "GnssIrProcessor could not be initialized (GNSS-IR "
                "processing will fail per-file until this is "
                "resolved): %s",
                exc,
            )
            self._gnssir = None

    # ==========================================================
    # Private: run() / discovery
    # ==========================================================

    def _reset_run_stats(self) -> None:
        self._run_stats = {
            "files_found": 0,
            "files_processed": 0,
            "files_failed": 0,
            "rinex_created": 0,
            "products_created": 0,
            "runtimes": [],
            "errors": [],
        }

    def _scan_raw_directory(self) -> list[Path]:
        """Discover raw files on disk. Does not touch the database."""

        assert self.cfg is not None

        paths = sorted(self.cfg.raw_dir.glob("*.um980"))

        self._logger.debug("Scanned %s: %d file(s)", self.cfg.raw_dir, len(paths))

        return paths

    def _find_new_files(self, scanned: list[Path]) -> list[Path]:
        """
        Register any raw file on disk that the database has never
        seen before (db.add_raw_file() itself enforces "never seen
        before" via its UNIQUE(filename) constraint -- a
        DatabaseError here just means it's already known, which
        isn't an error from the pipeline's point of view).

        Files that are already known but still need work (a previous
        failure, or a fresh registration from this same call) are
        picked up afterward via db.pending_raw_files() in
        _process_queue() -- so "new" here specifically means
        "first time we've ever recorded this filename", not
        "not yet processed".
        """

        assert self.db is not None

        new_files: list[Path] = []

        for path in scanned:

            file_date = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).date()

            try:
                self.db.add_raw_file(
                    path.name, file_date, size=path.stat().st_size
                )
            except DatabaseError:
                # Already registered; not a new file.
                continue

            self._jobs[path.name] = ProcessingJob(
                filename=path.name,
                date=file_date.isoformat(),
                raw_path=str(path),
                status=JobState.DISCOVERED,
            )
            new_files.append(path)

        self._run_stats["files_found"] += len(new_files)

        if new_files:
            self._logger.info("Discovered %d new raw file(s)", len(new_files))

        return new_files

    def _verify_raw_file(self, path: Path) -> bool:
        """
        Check that a raw file is exists, is non-empty, and is
        readable, before queuing it for processing.

        Checksum verification is intentionally not implemented yet
        (a later phase); when it is, this is where it belongs.
        """

        if not path.exists():
            self._logger.warning("Raw file vanished before verification: %s", path)
            return False

        if path.stat().st_size == 0:
            self._logger.warning("Raw file is empty: %s", path)
            return False

        if not os.access(path, os.R_OK):
            self._logger.warning("Raw file is not readable: %s", path)
            return False

        # TODO: checksum verification (documented as a later phase).

        return True

    def _queue_processing(self, path: Path) -> None:
        assert self.db is not None

        self.db.queue_file(path.name)

        job = self._jobs.get(path.name)
        if job is not None:
            job.status = JobState.QUEUED

    # ==========================================================
    # Private: queue processing
    # ==========================================================

    def _process_queue(self) -> None:
        """
        Process every filename the processing_queue table currently
        considers unfinished (waiting, running -- e.g. left over
        from an ungraceful shutdown -- or previously failed), one at
        a time. A single file's failure is caught and logged; it
        never stops the rest of the queue.

        Deliberately sourced from processing_queue itself rather
        than pending_raw_files(): pending_raw_files() only tracks
        "no successful RINEX conversion yet", so a file that fails
        at the *GNSS-IR* stage (RINEX already succeeded) would never
        be picked up again if this used that instead. processing_
        queue's waiting/running/completed/failed flags are the
        actual authoritative queue state for retry purposes.
        """

        assert self.db is not None

        entries = self.db.processing_history()
        to_process = [entry.filename for entry in entries if not entry.completed]

        self._logger.info("Processing queue: %d file(s) pending", len(to_process))

        for filename in to_process:
            self._process_one_job(filename)

    def _process_one_job(self, filename: str) -> None:
        assert self.db is not None
        assert self.cfg is not None

        raw_path = self.cfg.raw_dir / filename

        # The file's date is only needed for save_gnssir_product()'s
        # `date` field. A file still in the queue hasn't reached
        # _archive() yet, so it should still be sitting in raw_dir;
        # fall back to "today" only in the unlikely case it's gone
        # (e.g. removed out-of-band), rather than raising before
        # we've even had a chance to log the failure properly.
        if raw_path.exists():
            file_date = datetime.fromtimestamp(
                raw_path.stat().st_mtime, tz=timezone.utc
            ).date().isoformat()
        else:
            file_date = datetime.now(timezone.utc).date().isoformat()

        job = self._jobs.setdefault(
            filename,
            ProcessingJob(filename=filename, date=file_date, raw_path=str(raw_path)),
        )

        self._logger.info("START %s", filename)

        started_at = time.monotonic()
        job.started = datetime.now(timezone.utc).isoformat()

        self.db.start_processing(filename)
        job.status = JobState.PROCESSING

        try:
            rinex_result = self._convert_rinex(raw_path, job)
            self._verify_rinex(rinex_result)
            job.status = JobState.RINEX_COMPLETE

            gnssir_result = self._run_gnssrefl(rinex_result, job)
            self._verify_products(gnssir_result)
            job.status = JobState.GNSSIR_COMPLETE

            self._archive(raw_path, job)
            job.status = JobState.ARCHIVED

            self.db.finish_processing(filename, success=True)
            job.status = JobState.FINISHED
            job.finished = datetime.now(timezone.utc).isoformat()

            elapsed = time.monotonic() - started_at
            self._run_stats["runtimes"].append(elapsed)
            self._run_stats["files_processed"] += 1

            self._logger.info("SUCCESS %s (%.2f sec)", filename, elapsed)

        except Exception as exc:

            elapsed = time.monotonic() - started_at
            message = str(exc)

            self._logger.error("FAILED %s: %s (%.2f sec)", filename, message, elapsed)

            job.status = JobState.FAILED
            job.retries += 1
            job.errors.append(message)

            self.db.log_error(
                "pipeline",
                "ERROR",
                exception=type(exc).__name__,
                description=message,
                stack_trace=traceback.format_exc(),
                notes=f"day={job.date}",
            )

            self.db.finish_processing(filename, success=False, error_message=message)

            self._run_stats["files_failed"] += 1
            self._run_stats["errors"].append(f"{filename}: {message}")

        finally:
            self._cleanup(job)

    # ==========================================================
    # Private: processing steps
    # ==========================================================

    def _convert_rinex(self, raw_path: Path, job: ProcessingJob):
        """
        Step 5: convert to RINEX via RinexProcessor.convert(), then
        record the attempt with db.save_rinex() regardless of
        success, so failed conversions remain visible in
        rinex_history() rather than disappearing.

        Returns the full ConversionResult (not just the observation
        file path), since _verify_rinex() needs the whole result to
        call RinexProcessor.verify(result).
        """

        assert self.db is not None
        assert self._rinex is not None

        result = self._rinex.convert(raw_path)

        self.db.save_rinex(
            raw_filename=raw_path.name,
            observation_file=str(result.observation_file),
            navigation_file=str(result.navigation_file),
            sbas_file=str(result.sbas_file),
            conversion_success=result.success,
            convbin_version=result.convbin_version,
            processing_notes=result.message,
        )

        if not result.success:
            raise RuntimeError(f"RINEX conversion failed: {result.message}")

        self._run_stats["rinex_created"] += 1

        job.rinex_path = str(result.observation_file)

        return result

    def _verify_rinex(self, rinex_result) -> None:
        """Step 6: verify the RINEX observation file RinexProcessor produced."""

        assert self._rinex is not None

        if not self._rinex.verify(rinex_result):
            raise RuntimeError(
                f"RINEX verification failed: {rinex_result.observation_file}"
            )

    def _run_gnssrefl(self, rinex_result, job: ProcessingJob):
        """
        Step 7: run GNSS-IR via GnssIrProcessor.process(), then
        record the result with db.save_gnssir_product() regardless
        of success, for the same reason save_rinex() always records.
        """

        assert self.db is not None

        if self._gnssir is None:
            raise RuntimeError(
                "GNSS-IR processor is not available (gnssrefl is not "
                "installed, or failed to initialize -- see the "
                "warning logged at pipeline startup)"
            )

        day = date.fromisoformat(job.date)

        result = self._gnssir.process(rinex_result.observation_file, day=day)

        self.db.save_gnssir_product(
            date_=job.date,
            rinex_file=str(rinex_result.observation_file),
            reflector_height=result.reflector_height,
            soil_moisture=result.soil_moisture,
            snow_depth=result.snow_depth,
            quality_score=result.quality_score,
            output_directory=str(result.output_directory),
            processing_success=result.success,
            runtime=result.runtime_seconds,
            notes=result.message,
        )

        if not result.success:
            raise RuntimeError(f"GNSS-IR processing failed: {result.message}")

        self._run_stats["products_created"] += 1

        job.product_path = str(result.output_directory)

        return result

    def _verify_products(self, gnssir_result) -> None:
        """Step 8: verify the products GnssIrProcessor claims to have made."""

        assert self._gnssir is not None

        if not self._gnssir.verify(gnssir_result):
            raise RuntimeError(
                f"GNSS-IR product verification failed: "
                f"{gnssir_result.output_directory}"
            )

    def _archive(self, raw_path: Path, job: ProcessingJob) -> None:
        """
        Step 10: move the raw file into the archive directory once
        it has been successfully converted and processed, and mark
        it archived in the database.

        RINEX and product retention policy is left for a future
        revision (the source vision for this module doesn't specify
        exact rules); only the raw file is moved today, since it's
        the one large, no-longer-needed-in-place artifact at this
        point in the pipeline.
        """

        assert self.cfg is not None
        assert self.db is not None

        if not raw_path.exists():
            # Already archived/moved by a previous attempt; nothing
            # to do, but don't treat it as a failure.
            self._logger.debug("Raw file already absent, skipping archive: %s", raw_path)
        else:
            destination = self.cfg.archive_dir / raw_path.name
            shutil.move(str(raw_path), str(destination))
            self._logger.debug("Archived %s -> %s", raw_path, destination)

        self.db.update_raw_file(
            raw_path.name, archived=True, notes="archived after successful processing"
        )

    def _cleanup(self, job: ProcessingJob) -> None:
        """
        Reserved for cleaning up intermediate/temp files a processor
        module leaves behind (e.g. convbin scratch files). Neither
        processor module exists yet, so there is nothing to clean up
        today; called unconditionally (success or failure) so it's
        wired in once there is.
        """

        pass

    # ==========================================================
    # Private: reporting
    # ==========================================================

    def _update_database(self) -> None:
        """
        Run-level (as opposed to per-file) database bookkeeping:
        regenerate today's daily_summary so it reflects everything
        this run just did. Per-file updates (raw_files, rinex_files,
        gnssir_products, processing_queue) already happened inline
        as each step completed.
        """

        assert self.db is not None

        try:
            self.db.generate_daily_summary()
        except DatabaseError as exc:
            self._logger.warning("Could not update daily_summary: %s", exc)

    def _generate_summary(self) -> PipelineSummary:
        runtimes = self._run_stats["runtimes"]
        average_runtime = sum(runtimes) / len(runtimes) if runtimes else 0.0

        return PipelineSummary(
            files_found=self._run_stats["files_found"],
            files_processed=self._run_stats["files_processed"],
            files_failed=self._run_stats["files_failed"],
            rinex_created=self._run_stats["rinex_created"],
            products_created=self._run_stats["products_created"],
            average_runtime=average_runtime,
            errors=list(self._run_stats["errors"]),
        )

    def _log_statistics(self, summary: PipelineSummary) -> None:
        self._logger.info(
            "Summary: found=%d processed=%d failed=%d rinex=%d "
            "products=%d avg_runtime=%.2fs",
            summary.files_found,
            summary.files_processed,
            summary.files_failed,
            summary.rinex_created,
            summary.products_created,
            summary.average_runtime,
        )

        print("=" * 48)
        print("Pipeline Summary")
        print("=" * 48)
        print(f"Files Found          : {summary.files_found}")
        print(f"Files Processed      : {summary.files_processed}")
        print(f"Files Failed         : {summary.files_failed}")
        print(f"RINEX Created        : {summary.rinex_created}")
        print(f"Products Created     : {summary.products_created}")
        print(f"Average Runtime      : {summary.average_runtime:.2f} sec")
        print(f"Errors               : {len(summary.errors)}")
        for error in summary.errors:
            print(f"    - {error}")
        print("=" * 48)

    # ==========================================================
    # Private: shutdown
    # ==========================================================

    def _shutdown(self) -> None:
        self._logger.info("Shutting down pipeline")

        if self.db is not None and self._owns_db:
            self.db.close()

        self._initialized = False

    # ==========================================================
    # Future expansion (placeholders)
    # ==========================================================

    def _health_check(self) -> None:
        pass

    def _send_notifications(self) -> None:
        pass

    def _upload_products(self) -> None:
        pass

    def _download_orbits(self) -> None:
        pass

    def _cleanup_old_logs(self) -> None:
        pass

    def _generate_daily_report(self) -> None:
        pass
