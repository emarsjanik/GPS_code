#!/usr/bin/env python3
"""
station.py

USGS GNSS Reference Station
Version 2.0

The station controller. This module is the conductor, not one of
the musicians: it starts the application, loads configuration,
connects the receiver/database, updates station status, displays a
dashboard, handles failures gracefully, and shuts down cleanly.

It deliberately contains very little business logic. In particular,
this file never:

    * runs a SQL query           (that's database.py)
    * sends a serial command      (that's receiver.py)
    * parses RINEX                (that's rinex_processor.py)
    * runs a GNSS-IR algorithm     (that's gnssrefl_processor.py)
    * parses a file format         (that belongs in its own module)

Program flow
------------
    main()
      -> configure logging
      -> load configuration            (Config)
      -> verify configuration          (fail fast if invalid)
      -> connect database               (Database)
      -> verify database                (integrity_check)
      -> connect receiver                (Receiver, as a context manager)
      -> read receiver                    (version, position)
      -> save receiver information         (db.save_receiver_version)
      -> save position                      (db.save_position)
      -> update station status               (db.save_receiver_status)
      -> display dashboard
      -> close receiver                       (automatic: `with` block)
      -> close database                        (finally:)
      -> exit

Exit codes
----------
    0   success
    1   configuration error
    2   receiver error
    3   database error
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from database import Database, DatabaseError, DatabaseStatistics, CURRENT_SCHEMA_VERSION
from exceptions import ConfigurationError
from receiver import (
    Receiver,
    ReceiverError,
    VersionInfo,
    PositionInfo,
    CommandStats,
    API_VERSION as RECEIVER_API_VERSION,
)
from version import __version__

# Only the version constant is imported from pipeline.py -- station.py
# does not construct or run a Pipeline; that stays a separate concern
# (triggered by station_manager.py or a scheduled job), not something
# this dashboard drives. Everything else shown in the "Pipeline"
# section below is read from the database directly, so it reflects
# reality across process boundaries rather than this process's own
# (nonexistent) Pipeline state.
from pipeline import PIPELINE_VERSION

# ----------------------------------------------------------------
# Constants
# ----------------------------------------------------------------

APPLICATION_NAME = "USGS GNSS Reference Station"

EXIT_SUCCESS = 0
EXIT_CONFIG_ERROR = 1
EXIT_RECEIVER_ERROR = 2
EXIT_DATABASE_ERROR = 3

_BAR = "=" * 58

_START_TIME = time.monotonic()

_logger = logging.getLogger("station.controller")


# ----------------------------------------------------------------
# Logging
# ----------------------------------------------------------------

def configure_logging(cfg: Config) -> logging.Logger:
    """
    Configure the "station" logger (parent of "station.controller",
    "station.receiver", and "station.database") with a console
    handler and a file handler writing to logs/station.log.

    database.py additionally attaches its own dedicated handler to
    logs/database.log; because loggers propagate to their parents by
    default, database errors show up in *both* files, which is
    intentional -- one dedicated log per module, plus one combined
    log for the whole station.
    """

    logger = logging.getLogger("station")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        # Already configured (e.g. called twice in one process);
        # don't stack duplicate handlers.
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_path = cfg.logs_dir / "station.log"
    file_handler = logging.FileHandler(str(log_path))
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


# ----------------------------------------------------------------
# Configuration verification
# ----------------------------------------------------------------

def verify_configuration(cfg: Config) -> None:
    """
    Sanity-check the already-loaded configuration.

    Config() itself raises FileNotFoundError/json.JSONDecodeError for
    a missing or malformed station.json, and already creates every
    required directory in its own constructor. This is a defensive
    second check -- that the values which *did* load are actually
    usable, and that the directories it created still exist -- so a
    bad station.json (e.g. an empty station_id) or a permissions
    problem is caught here, immediately, rather than surfacing later
    as a confusing failure deep in receiver.py or database.py.

    Raises
    ------
    ConfigurationError
        Listing every problem found, if any.
    """

    problems = []

    if not cfg.station_id:
        problems.append("station_id is empty")

    if not cfg.receiver_port:
        problems.append("receiver_port is empty")

    if not isinstance(cfg.receiver_baud, int) or cfg.receiver_baud <= 0:
        problems.append(f"receiver_baud is invalid: {cfg.receiver_baud!r}")

    required_directories = {
        "raw_dir": cfg.raw_dir,
        "rinex_dir": cfg.rinex_dir,
        "archive_dir": cfg.archive_dir,
        "products_dir": cfg.products_dir,
        "logs_dir": cfg.logs_dir,
        "database_dir": cfg.database_dir,
        "reports_dir": cfg.reports_dir,
        "scripts_dir": cfg.scripts_dir,
    }

    for name, path in required_directories.items():
        if not path.is_dir():
            problems.append(f"{name} does not exist: {path}")

    if problems:
        raise ConfigurationError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
        )


# ----------------------------------------------------------------
# Small display helpers
# ----------------------------------------------------------------

def _human_bytes(size: int | None) -> str:
    """Format a byte count as e.g. '128.3 MB'. None/negative -> 'N/A'."""

    if size is None or size < 0:
        return "N/A"

    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024

    return f"{value:.1f} PB"  # pragma: no cover (unreachable in practice)


def _human_duration(seconds: float) -> str:
    """Format a duration in seconds as 'HH:MM:SS'."""

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _read_cpu_load() -> str:
    """
    Best-effort 1-minute load average, Linux only. Returns "N/A" on
    any other platform or if it can't be read -- this is explicitly
    a "can be added properly later" field, not a hard requirement.
    """

    try:
        return f"{os.getloadavg()[0]:.2f}"
    except (OSError, AttributeError):
        return "N/A"


def _read_memory_percent() -> str:
    """
    Best-effort memory-used percentage, Linux only (reads
    /proc/meminfo directly, no extra dependency). Returns "N/A"
    anywhere that file doesn't exist or isn't parseable.
    """

    try:
        fields = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                fields[key.strip()] = int(rest.strip().split()[0])

        total = fields.get("MemTotal")
        available = fields.get("MemAvailable")

        if not total:
            return "N/A"

        used_percent = 100.0 * (total - available) / total
        return f"{used_percent:.1f}%"
    except (OSError, KeyError, ValueError, IndexError):
        return "N/A"


def _print_section(title: str) -> None:
    print()
    print(title)


def _print_field(label: str, value) -> None:
    print(f"    {label:<24}: {value}")


# ----------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------

def display_dashboard(
    cfg: Config,
    db: Database,
    rx: Receiver,
    version: VersionInfo,
    position: PositionInfo,
    stats: dict[str, CommandStats],
) -> None:
    """
    Render the full startup dashboard to stdout. Every value shown
    here was already gathered by main() via the public receiver.py /
    database.py APIs (or plain os/platform/shutil calls for system
    info); this function only formats and prints.
    """

    startup_elapsed = time.monotonic() - _START_TIME

    print(_BAR)
    print(APPLICATION_NAME)
    print()
    print(f"Version {__version__}")
    print(_BAR)
    print(f"Application Version : {__version__}")
    print(f"Database Schema      : {CURRENT_SCHEMA_VERSION}")
    print(f"Receiver API         : {RECEIVER_API_VERSION}")
    print(f"Pipeline             : {PIPELINE_VERSION}")
    print(_BAR)

    db_stats = db.database_statistics()
    station_stats = db.station_statistics()
    queue_stats = db.processing_statistics()
    today = datetime.now(timezone.utc).date().isoformat()
    today_summary = db.daily_statistics(today)

    _print_section("Station")
    _print_field("ID", station_stats.station_id)
    _print_field("Name", station_stats.station_name)

    _print_section("Receiver")
    _print_field("Model", version.model)
    _print_field("Firmware", version.firmware)
    _print_field("Hardware", version.hardware)
    _print_field("Serial Number", version.psn)
    _print_field("Compile Date", version.compile_date)

    _print_section("GNSS")
    _print_field("Solution", position.solution)
    _print_field("Latitude", f"{position.latitude:.10f}")
    _print_field("Longitude", f"{position.longitude:.10f}")
    _print_field("Height", f"{position.height:.3f} m")
    _print_field("Undulation", f"{position.undulation:.3f} m")
    _print_field("Datum", position.datum)
    _print_field("Tracked Satellites", position.num_svs_tracked)
    _print_field("Satellites in Solution", position.num_svs_in_solution)
    _print_field("Differential Age", f"{position.differential_age:.1f} s")
    _print_field("Solution Age", f"{position.solution_age:.1f} s")

    _print_section("Accuracy")
    _print_field("Latitude Std Dev", f"{position.latitude_stdev:.4f} m")
    _print_field("Longitude Std Dev", f"{position.longitude_stdev:.4f} m")
    _print_field("Height Std Dev", f"{position.height_stdev:.4f} m")

    # Merged "Database" + "Database Statistics" into one section.
    _print_section("Database")
    _print_field("Connected", db.is_connected())
    _print_field("Schema", db_stats.schema_version)
    _print_field("Tables", db_stats.total_tables)
    _print_field("Records", db_stats.total_records)
    _print_field("Size", _human_bytes(db_stats.size_bytes))
    _print_field("Last Backup", db_stats.last_backup or "None yet")

    _print_section("Files")
    _print_field("Raw Directory", cfg.raw_dir)
    _print_field("RINEX Directory", cfg.rinex_dir)
    _print_field("Product Directory", cfg.products_dir)

    newest_raw = db.latest_raw_file()
    newest_rinex = db.latest_rinex()
    newest_products = db.gnssir_history(limit=1)

    _print_section("Newest Files")
    _print_field("Raw", newest_raw.filename if newest_raw else "None yet")
    _print_field(
        "RINEX", newest_rinex.observation_file if newest_rinex else "None yet"
    )
    _print_field(
        "Product",
        newest_products[0].output_directory if newest_products else "None yet",
    )

    # New: lets the operator see at a glance whether the processing
    # pipeline is idle, has work queued, or appears to be mid-run
    # (a "RUNNING" row left over from an interrupted process counts
    # as running here too -- that's a legitimate thing to want to
    # know about). All of this is read straight from processing_queue
    # via the database, not from a live Pipeline object, since
    # whatever last touched the queue was very likely a different
    # process than this one.
    if queue_stats.running > 0:
        pipeline_status = "RUNNING"
    elif queue_stats.waiting > 0:
        pipeline_status = "WORK QUEUED"
    else:
        pipeline_status = "IDLE"

    _print_section("Pipeline")
    _print_field("Status", pipeline_status)
    _print_field("Queue Length", queue_stats.waiting + queue_stats.running)
    _print_field("Last Run", queue_stats.last_attempt or "Never")
    _print_field("Last Successful Run", queue_stats.last_successful_attempt or "Never")
    _print_field("Files Waiting", queue_stats.waiting)
    _print_field("Files Failed", queue_stats.failed)

    _print_section("Runtime Statistics")
    best_pos_stats = stats.get("BESTPOSA")
    total_queries = sum(entry.count for entry in stats.values())
    total_time = sum(entry.total_time for entry in stats.values())
    total_retries = sum(entry.failures for entry in stats.values())
    overall_average = total_time / total_queries if total_queries else 0.0
    _print_field(
        "Receiver Query Time",
        f"{best_pos_stats.average_time * 1000:.1f} ms" if best_pos_stats else "N/A",
    )
    _print_field("Average Response Time", f"{overall_average * 1000:.1f} ms")
    _print_field("Retry Count", total_retries)

    _print_section("Health")
    _print_field("Receiver Connected", rx.is_connected())
    _print_field("Database Connected", db.is_connected())
    disk_usage = shutil.disk_usage(str(cfg.project_root))
    _print_field("Disk Free", _human_bytes(disk_usage.free))
    _print_field("CPU (1m load avg)", _read_cpu_load())
    _print_field("Memory Used", _read_memory_percent())

    # New: a complete operational overview near the bottom, per file
    # type and per pipeline table, distinct from "Pipeline" above
    # (which is about queue/retry mechanics) -- this is about
    # totals. "Completed Today"/"Failed Today" come from
    # daily_summary, which pipeline.py regenerates via
    # db.generate_daily_summary() at the end of every run(); if the
    # pipeline hasn't run yet today, both read as 0 rather than
    # failing. "Failed Today" is every error logged today
    # (error_log has no per-error "was this a processing failure"
    # flag to filter on more narrowly than that).
    _print_section("Processing")
    _print_field("Raw Files", station_stats.total_raw_files)
    _print_field("RINEX Files", station_stats.total_rinex_files)
    _print_field("Products", station_stats.total_gnssir_products)
    _print_field("Pending", station_stats.pending_files)
    _print_field(
        "Completed Today", today_summary.files_processed if today_summary else 0
    )
    _print_field("Failed Today", today_summary.errors if today_summary else 0)

    _print_section("System")
    _print_field(
        "Current Time (UTC)",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
    _print_field("Uptime", _human_duration(startup_elapsed))
    _print_field("Python Version", platform.python_version())
    _print_field("Operating System", platform.platform())

    print()
    print("Status")
    print("    READY")
    print()
    print(f"    Startup Time            : {startup_elapsed:.2f} sec")
    print(_BAR)


# ----------------------------------------------------------------
# Future hooks
#
# Each of these is a placeholder for work that belongs in its own
# module (or, eventually, pipeline.py orchestrating several of
# them). Creating the functions now, even empty, gives the
# application a stable structure to grow into without station.py
# accumulating business logic of its own later.
# ----------------------------------------------------------------

def process_raw_files(cfg: Config, db: Database) -> None:
    """Placeholder: find and register newly-captured raw files."""
    pass


def convert_rinex(cfg: Config, db: Database) -> None:
    """Placeholder: convert pending raw files to RINEX (rinex_processor.py)."""
    pass


def run_gnssrefl(cfg: Config, db: Database) -> None:
    """Placeholder: run gnssrefl against converted RINEX (gnssrefl_processor.py)."""
    pass


def archive_old_files(cfg: Config, db: Database) -> None:
    """Placeholder: archive/prune old raw and RINEX files."""
    pass


def daily_maintenance(cfg: Config, db: Database) -> None:
    """Placeholder: nightly housekeeping (generate_daily_summary, backups, vacuum)."""
    pass


def health_check(cfg: Config, db: Database) -> None:
    """
    Placeholder: an automated, periodic watchdog -- distinct from
    display_dashboard()'s one-time startup snapshot -- that would
    monitor health continuously and log/alert on problems.
    """
    pass


# ----------------------------------------------------------------
# main
# ----------------------------------------------------------------

def main() -> int:
    """
    Run the station controller once: start up, read the receiver,
    record what it saw, show the dashboard, and shut down cleanly.

    Returns an exit code (see the module docstring); never raises --
    every exception is caught, logged as "Application Error", and
    translated into the appropriate code.
    """

    db: Database | None = None
    exit_code = EXIT_SUCCESS

    try:
        # ------------------------------------------------
        # Configuration
        # ------------------------------------------------

        try:
            cfg = Config()
            verify_configuration(cfg)
        except (ConfigurationError, FileNotFoundError, ValueError) as exc:
            # No logger is configured yet (it needs cfg.logs_dir), so
            # this one failure mode is reported straight to stderr.
            print(f"Configuration error: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

        logger = configure_logging(cfg)
        logger.info("Application Started")

        # ------------------------------------------------
        # Database
        # ------------------------------------------------

        try:
            db = Database.from_config(cfg)
            db.connect()

            if not db.integrity_check():
                raise DatabaseError(
                    "Database failed integrity_check() after connect()"
                )
        except DatabaseError as exc:
            logger.error("Application Error: %s", exc, exc_info=True)
            return EXIT_DATABASE_ERROR

        db.save_station_info(
            station_id=cfg.station_id,
            station_name=cfg.station_name,
            organization=cfg.agency,
            latitude=cfg.latitude,
            longitude=cfg.longitude,
            height=cfg.height,
        )

        # ------------------------------------------------
        # Receiver
        # ------------------------------------------------

        try:
            with Receiver(
                device=cfg.receiver_port,
                baudrate=cfg.receiver_baud,
                timeout=cfg.receiver_timeout,
            ) as rx:

                version = rx.version()
                position = rx.best_position()

                db.save_receiver_version(version, software_version=__version__)
                db.save_position(position)

                stats = rx.stats()
                db.save_command_statistics(stats)

                best_pos_stats = stats.get("BESTPOSA")
                previous_status = db.get_receiver_status()

                db.save_receiver_status(
                    position,
                    connected=rx.is_connected(),
                    last_command="BESTPOSA",
                    last_response_time=(
                        best_pos_stats.average_time if best_pos_stats else None
                    ),
                    communication_errors=(
                        previous_status.communication_errors
                        + sum(entry.failures for entry in stats.values())
                    ),
                )

                display_dashboard(cfg, db, rx, version, position, stats)

        except ReceiverError as exc:
            logger.error("Application Error: %s", exc, exc_info=True)
            db.log_error(
                "receiver", "ERROR", exception=type(exc).__name__,
                description=str(exc), stack_trace=traceback.format_exc(),
            )
            return EXIT_RECEIVER_ERROR

        except DatabaseError as exc:
            logger.error("Application Error: %s", exc, exc_info=True)
            return EXIT_DATABASE_ERROR

        return EXIT_SUCCESS

    except Exception as exc:  # noqa: BLE001 -- last-resort safety net
        logging.getLogger("station").error(
            "Application Error: %s", exc, exc_info=True
        )
        if db is not None:
            try:
                db.log_error(
                    "station", "CRITICAL", exception=type(exc).__name__,
                    description=str(exc), stack_trace=traceback.format_exc(),
                )
            except DatabaseError:
                pass  # the database itself may be the problem; don't mask it
        exit_code = EXIT_DATABASE_ERROR if db is not None else EXIT_CONFIG_ERROR
        return exit_code

    finally:
        if db is not None:
            db.close()
        logging.getLogger("station").info("Application Stopped")


if __name__ == "__main__":
    sys.exit(main())
