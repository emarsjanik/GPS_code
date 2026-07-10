"""
database.py

USGS GNSS Reference Station
Prototype 1.0

Central, self-initializing SQLite persistence layer for the station.

`database.py` is the *only* module that is allowed to talk to SQLite
directly. Every other module (station.py, receiver.py,
rinex_processor.py, gnssrefl_processor.py, pipeline.py,
station_manager.py) goes through the `Database` class defined here,
using only the Public API described below.

Tables
------
    1. station_info        -- one row, static station identity
    2. receiver_status      -- one row, updated every poll
    3. raw_files            -- one row per raw data file
    4. rinex_files           -- one row per RINEX conversion attempt
    5. gnssir_products       -- one row per gnssrefl processing run
    6. processing_queue      -- one row per filename tracked for work
    7. system_health         -- one row per periodic health snapshot
    8. error_log             -- permanent, one row per logged error
    9. command_statistics    -- one row per receiver command, imported
                                 from receiver.py's CommandStats
    10. daily_summary        -- one row per UTC day, generated at
                                 midnight
    11. position_history     -- one row per recorded position fix

    (position_history isn't in the numbered table list this module
    was specified against, but the Public API calls for
    save_position()/latest_position()/position_history(), which need
    an append-only log distinct from the single-row receiver_status
    table -- see the "Position Methods" section below for how the
    two relate.)

Public API
----------
    Core:       connect, close, initialize, upgrade_schema, backup,
                backup_database, vacuum, integrity_check
    Receiver:   save_receiver_status, get_receiver_status,
                save_receiver_version
    Position:   save_position, latest_position, position_history
    Raw files:  add_raw_file, update_raw_file, delete_raw_file,
                pending_raw_files
    RINEX:      save_rinex, latest_rinex, rinex_history
    Processing: queue_file, start_processing, finish_processing,
                processing_history
    Errors:     log_error, clear_error, recent_errors
    Statistics: save_command_statistics, station_statistics,
                daily_statistics

    Two small groups of methods are added beyond that list because,
    without them, two tables would be unreachable:

        Station info:  save_station_info, get_station_info
        GNSS-IR:       save_gnssir_product, gnssir_history
        Health:        save_system_health, latest_system_health
        Daily summary generation: generate_daily_summary

    Every other module should still only call methods in this file;
    these four additions are simply necessary members of that same
    surface, not an exception to "go through database.py".

Automatic features
-------------------
`connect()` always, with no caller intervention required:
    * creates the database file and its parent directory if missing
    * creates every table
    * creates every index
    * enables foreign keys
    * enables WAL mode
    * runs an integrity check, and attempts recovery (see below) if
      it fails
    * upgrades older schemas via `initialize()` / `upgrade_schema()`
    * records the startup time into station_info.last_startup

Recovery
--------
If `connect()` detects corruption (SQLite reports the file isn't a
valid database, or `integrity_check()` fails):
    1. the error is logged at CRITICAL level
    2. the damaged file (and any -wal/-shm siblings) is copied aside
       to `<path>.corrupt.<timestamp>` -- never deleted
    3. the newest file in `backup_dir` (default:
       `<database directory>/backups/station_*.db`) is copied over
       the live path
    4. the connection is reopened against the restored file and
       re-checked; if it's healthy, the station continues operating
       automatically
    5. `on_corruption(message)` is called, if a callback was supplied
       to the constructor, so station.py (or whatever owns the
       process) can be notified and decide what else to do (alert,
       restart, page someone, ...)

If no backup exists, or the restored copy also fails its integrity
check, `connect()` raises `DatabaseError` rather than silently
pretending the station is healthy.

Logging
-------
Every SQL error is logged through the standard `logging` module,
logger name "station.database". In addition to whatever handlers the
rest of the application has configured, `connect()` attaches a
dedicated `logging.FileHandler` writing to `log_file` (default
"<project_root>/logs/database.log", anchored to this module's own
location rather than the process's working directory), so database
errors are always captured in one place even if the rest of the
app's logging isn't configured yet.

Integration with receiver.py
-----------------------------
This module does not import receiver.py, to avoid forcing a pyserial
dependency onto every caller of the database. Methods like
save_position(), save_receiver_version(), and
save_command_statistics() accept *any* object exposing the right
attributes (duck typing), so they work directly with
receiver.PositionInfo, receiver.VersionInfo, and
receiver.Receiver.stats() output.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
import logging
import shutil
import sqlite3
import threading


# This module lives at <project_root>/station/database.py, so its
# parent's parent is the project root -- the same layout config.py
# uses (project_root/database, project_root/logs, project_root/
# station). Anchoring the defaults below to this location, rather
# than to plain relative strings, means they resolve correctly
# regardless of the current working directory the process happens
# to be started from (e.g. running `python3 station.py` from inside
# `station/` no longer creates a stray `station/database/station.db`).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / "database" / "station.db"
_DEFAULT_LOG_FILE = _PROJECT_ROOT / "logs" / "database.log"


# ----------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------

class DatabaseError(Exception):
    """Base exception for all database failures."""


class DatabaseNotConnectedError(DatabaseError):
    """Raised when an operation is attempted before connect()."""


class SchemaError(DatabaseError):
    """Raised when the schema cannot be initialized or migrated."""


class NotFoundError(DatabaseError):
    """Raised when an update targets a row that does not exist."""


# ----------------------------------------------------------------
# Result dataclasses
#
# Column names in the schema below are deliberately identical to
# these dataclass field names, so a row can be turned into a record
# with a single `Record(**dict(row))` call.
# ----------------------------------------------------------------

@dataclass
class StationInfo:
    """Static station identity (station_info, one row, id=1)."""

    id: int = 1
    station_id: str = ""
    station_name: str = ""
    organization: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    height: float = 0.0
    antenna_model: str = ""
    receiver_model: str = ""
    receiver_serial_number: str = ""
    firmware: str = ""
    installation_date: str = ""
    last_startup: str = ""
    software_version: str = ""


@dataclass
class ReceiverStatus:
    """Live receiver status (receiver_status, one row, id=1)."""

    id: int = 1
    timestamp: str = ""
    connected: bool = False
    solution_status: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    height: float = 0.0
    differential_age: float = 0.0
    solution_age: float = 0.0
    tracked_satellites: int = 0
    solution_satellites: int = 0
    receiver_temperature: float | None = None
    uptime: float | None = None
    last_command: str = ""
    last_response_time: float | None = None
    communication_errors: int = 0


@dataclass
class PositionRecord:
    """One recorded position fix (position_history)."""

    id: int = 0
    timestamp: str = ""
    day: str = ""
    solution_status: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    height: float = 0.0
    differential_age: float = 0.0
    solution_age: float = 0.0
    tracked_satellites: int = 0
    solution_satellites: int = 0
    raw: str = ""


@dataclass
class RawFile:
    """One tracked raw data file (raw_files)."""

    id: int = 0
    filename: str = ""
    date: str = ""
    start_time: str | None = None
    end_time: str | None = None
    size: int | None = None
    checksum: str | None = None
    archived: bool = False
    deleted: bool = False
    notes: str = ""


@dataclass
class RinexFile:
    """One RINEX conversion attempt (rinex_files)."""

    id: int = 0
    raw_filename: str = ""
    observation_file: str = ""
    navigation_file: str = ""
    sbas_file: str = ""
    conversion_success: bool = False
    conversion_time: str = ""
    convbin_version: str = ""
    processing_notes: str = ""


@dataclass
class GnssirProduct:
    """One gnssrefl processing run (gnssir_products)."""

    id: int = 0
    date: str = ""
    rinex_file: str = ""
    reflector_height: float | None = None
    soil_moisture: float | None = None
    snow_depth: float | None = None
    quality_score: float | None = None
    output_directory: str = ""
    processing_success: bool = False
    runtime: float | None = None
    notes: str = ""


@dataclass
class ProcessingQueueEntry:
    """One file's processing status (processing_queue)."""

    id: int = 0
    filename: str = ""
    waiting: bool = True
    running: bool = False
    completed: bool = False
    failed: bool = False
    retry_count: int = 0
    priority: int = 0
    last_attempt: str | None = None
    error_message: str | None = None


@dataclass
class ProcessingStatistics:
    """
    Aggregate view over processing_queue (not a table of its own):
    how many tracked files are in each state right now, how many
    retries have accumulated in total, and when the queue was last
    touched at all vs. last touched *successfully*.

    Intended for callers like pipeline.py's queue_status(), and for
    a completely separate process (e.g. station.py's dashboard) that
    wants to know "is there a pipeline running/queued, and when did
    it last succeed" without holding a live Pipeline object of its
    own -- last_attempt/last_successful_attempt are read straight
    from processing_queue.last_attempt, so they reflect reality
    across process boundaries, not just this process's memory.
    """

    total: int = 0
    waiting: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    total_retries: int = 0
    last_attempt: str | None = None
    last_successful_attempt: str | None = None


@dataclass
class SystemHealth:
    """One system health snapshot (system_health)."""

    id: int = 0
    timestamp: str = ""
    cpu_usage: float | None = None
    memory_usage: float | None = None
    disk_usage: float | None = None
    disk_free: int | None = None
    database_size: int | None = None
    receiver_connected: bool = False
    internet_connected: bool = False
    newest_raw_file: str = ""
    newest_rinex: str = ""
    newest_product: str = ""


@dataclass
class ErrorLogEntry:
    """One logged error (error_log)."""

    id: int = 0
    timestamp: str = ""
    module: str = ""
    severity: str = ""
    exception: str = ""
    description: str = ""
    stack_trace: str = ""
    recovered: bool = False
    notes: str = ""


@dataclass
class CommandStatistic:
    """One receiver command's running stats (command_statistics)."""

    command: str = ""
    count: int = 0
    failures: int = 0
    average_time: float = 0.0
    minimum_time: float | None = None
    maximum_time: float = 0.0
    last_successful_query: str | None = None


@dataclass
class DailySummary:
    """
    One day's automatically generated summary (daily_summary).

    `average_position` is stored as a JSON-encoded string of
    {"latitude": ..., "longitude": ..., "height": ...} rather than
    three separate columns, to match the single "Average Position"
    field as specified. Use `average_position_dict()` to decode it.
    """

    date: str = ""
    hours_running: float | None = None
    files_collected: int = 0
    files_processed: int = 0
    rinex_created: int = 0
    gnssir_completed: int = 0
    errors: int = 0
    average_position: str = ""
    average_satellites: float | None = None
    downtime: float | None = None
    notes: str = ""

    def average_position_dict(self) -> dict:
        import json

        if not self.average_position:
            return {}
        return json.loads(self.average_position)


@dataclass
class StationStatistics:
    """Aggregate, dashboard-style overview (not a table of its own)."""

    station_id: str = ""
    station_name: str = ""
    receiver_model: str = ""
    connected: bool = False
    solution_status: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    height: float = 0.0
    total_raw_files: int = 0
    total_rinex_files: int = 0
    total_gnssir_products: int = 0
    total_errors: int = 0
    pending_files: int = 0
    installation_date: str = ""
    last_startup: str = ""


@dataclass
class DatabaseStatistics:
    """
    Aggregate database-file-level overview for dashboards (not a
    table of its own): file size, table/record counts, schema
    version, and the newest backup on disk, so callers like
    station.py never need to query sqlite_master or the filesystem
    themselves.
    """

    path: str = ""
    schema_version: int = 0
    size_bytes: int = 0
    total_tables: int = 0
    total_records: int = 0
    last_backup: str | None = None


# ----------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------

def _utcnow_iso() -> str:
    """Current UTC time as a sortable, human-readable ISO-8601 string."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utcnow_date_str() -> str:
    """Current UTC calendar day as 'YYYY-MM-DD'."""

    return datetime.now(timezone.utc).date().isoformat()


def _normalize_day(day: str | date) -> str:
    """Normalize a day argument to a validated 'YYYY-MM-DD' string."""

    if isinstance(day, date):
        return day.isoformat()

    if isinstance(day, str):
        date.fromisoformat(day)  # raises ValueError if not a real date
        return day

    raise TypeError(f"day must be a str or datetime.date, got {type(day)!r}")


# ----------------------------------------------------------------
# Schema
# ----------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 1


def _migration_v1(conn: sqlite3.Connection) -> None:
    """Initial schema: every table and index described in the module docstring."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS station_info (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            station_id              TEXT NOT NULL DEFAULT '',
            station_name            TEXT NOT NULL DEFAULT '',
            organization             TEXT NOT NULL DEFAULT '',
            latitude                REAL NOT NULL DEFAULT 0,
            longitude               REAL NOT NULL DEFAULT 0,
            height                  REAL NOT NULL DEFAULT 0,
            antenna_model            TEXT NOT NULL DEFAULT '',
            receiver_model           TEXT NOT NULL DEFAULT '',
            receiver_serial_number   TEXT NOT NULL DEFAULT '',
            firmware                TEXT NOT NULL DEFAULT '',
            installation_date        TEXT NOT NULL DEFAULT '',
            last_startup             TEXT NOT NULL DEFAULT '',
            software_version         TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_station_info_station_id
            ON station_info(station_id);

        CREATE TABLE IF NOT EXISTS receiver_status (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            timestamp                TEXT NOT NULL DEFAULT '',
            connected                INTEGER NOT NULL DEFAULT 0,
            solution_status          TEXT NOT NULL DEFAULT '',
            latitude                REAL NOT NULL DEFAULT 0,
            longitude                REAL NOT NULL DEFAULT 0,
            height                  REAL NOT NULL DEFAULT 0,
            differential_age         REAL NOT NULL DEFAULT 0,
            solution_age             REAL NOT NULL DEFAULT 0,
            tracked_satellites       INTEGER NOT NULL DEFAULT 0,
            solution_satellites      INTEGER NOT NULL DEFAULT 0,
            receiver_temperature     REAL,
            uptime                  REAL,
            last_command             TEXT NOT NULL DEFAULT '',
            last_response_time       REAL,
            communication_errors     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_receiver_status_timestamp
            ON receiver_status(timestamp);

        CREATE TABLE IF NOT EXISTS position_history (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp                TEXT NOT NULL,
            day                     TEXT NOT NULL,
            solution_status          TEXT NOT NULL DEFAULT '',
            latitude                REAL NOT NULL,
            longitude                REAL NOT NULL,
            height                  REAL NOT NULL,
            differential_age         REAL NOT NULL DEFAULT 0,
            solution_age             REAL NOT NULL DEFAULT 0,
            tracked_satellites       INTEGER NOT NULL DEFAULT 0,
            solution_satellites      INTEGER NOT NULL DEFAULT 0,
            raw                     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_position_history_timestamp
            ON position_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_position_history_day
            ON position_history(day);

        CREATE TABLE IF NOT EXISTS raw_files (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filename        TEXT NOT NULL UNIQUE,
            date            TEXT NOT NULL,
            start_time      TEXT,
            end_time        TEXT,
            size            INTEGER,
            checksum        TEXT,
            archived        INTEGER NOT NULL DEFAULT 0,
            deleted         INTEGER NOT NULL DEFAULT 0,
            notes           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_raw_files_date ON raw_files(date);

        CREATE TABLE IF NOT EXISTS rinex_files (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_filename        TEXT NOT NULL,
            observation_file    TEXT NOT NULL DEFAULT '',
            navigation_file     TEXT NOT NULL DEFAULT '',
            sbas_file           TEXT NOT NULL DEFAULT '',
            conversion_success  INTEGER NOT NULL DEFAULT 0,
            conversion_time     TEXT NOT NULL,
            convbin_version     TEXT NOT NULL DEFAULT '',
            processing_notes    TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(raw_filename) REFERENCES raw_files(filename)
        );
        CREATE INDEX IF NOT EXISTS idx_rinex_files_raw_filename
            ON rinex_files(raw_filename);

        CREATE TABLE IF NOT EXISTS gnssir_products (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                TEXT NOT NULL,
            rinex_file          TEXT NOT NULL DEFAULT '',
            reflector_height    REAL,
            soil_moisture       REAL,
            snow_depth          REAL,
            quality_score       REAL,
            output_directory    TEXT NOT NULL DEFAULT '',
            processing_success  INTEGER NOT NULL DEFAULT 0,
            runtime             REAL,
            notes               TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_gnssir_products_date
            ON gnssir_products(date);

        CREATE TABLE IF NOT EXISTS processing_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filename        TEXT NOT NULL UNIQUE,
            waiting         INTEGER NOT NULL DEFAULT 1,
            running         INTEGER NOT NULL DEFAULT 0,
            completed       INTEGER NOT NULL DEFAULT 0,
            failed          INTEGER NOT NULL DEFAULT 0,
            retry_count     INTEGER NOT NULL DEFAULT 0,
            priority        INTEGER NOT NULL DEFAULT 0,
            last_attempt    TEXT,
            error_message   TEXT
        );

        CREATE TABLE IF NOT EXISTS system_health (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp            TEXT NOT NULL,
            cpu_usage            REAL,
            memory_usage         REAL,
            disk_usage           REAL,
            disk_free            INTEGER,
            database_size        INTEGER,
            receiver_connected   INTEGER NOT NULL DEFAULT 0,
            internet_connected   INTEGER NOT NULL DEFAULT 0,
            newest_raw_file      TEXT NOT NULL DEFAULT '',
            newest_rinex         TEXT NOT NULL DEFAULT '',
            newest_product       TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_system_health_timestamp
            ON system_health(timestamp);

        CREATE TABLE IF NOT EXISTS error_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT NOT NULL,
            module          TEXT NOT NULL DEFAULT '',
            severity        TEXT NOT NULL DEFAULT '',
            exception       TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            stack_trace     TEXT NOT NULL DEFAULT '',
            recovered       INTEGER NOT NULL DEFAULT 0,
            notes           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_error_log_timestamp
            ON error_log(timestamp);

        CREATE TABLE IF NOT EXISTS command_statistics (
            command                 TEXT PRIMARY KEY,
            count                   INTEGER NOT NULL DEFAULT 0,
            failures                INTEGER NOT NULL DEFAULT 0,
            average_time             REAL NOT NULL DEFAULT 0,
            minimum_time             REAL,
            maximum_time             REAL NOT NULL DEFAULT 0,
            last_successful_query     TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            date                TEXT PRIMARY KEY,
            hours_running        REAL,
            files_collected      INTEGER NOT NULL DEFAULT 0,
            files_processed      INTEGER NOT NULL DEFAULT 0,
            rinex_created        INTEGER NOT NULL DEFAULT 0,
            gnssir_completed     INTEGER NOT NULL DEFAULT 0,
            errors              INTEGER NOT NULL DEFAULT 0,
            average_position     TEXT NOT NULL DEFAULT '',
            average_satellites   REAL,
            downtime            REAL,
            notes               TEXT NOT NULL DEFAULT ''
        );

        INSERT OR IGNORE INTO station_info (id) VALUES (1);
        INSERT OR IGNORE INTO receiver_status (id) VALUES (1);
        """
    )


# Ordered list of migrations. `_MIGRATIONS[i]` upgrades a database
# from schema version `i` to version `i + 1`. Append new functions
# here (and bump CURRENT_SCHEMA_VERSION) for future schema changes;
# never edit a migration that has already shipped.
_MIGRATIONS: list = [_migration_v1]


# Columns updatable via update_raw_file(), kept as an explicit
# allow-list so dynamically built UPDATE statements never see a
# caller-supplied column name.
_RAW_FILE_UPDATABLE_FIELDS = frozenset(
    {"start_time", "end_time", "size", "checksum", "archived", "deleted", "notes"}
)


# ----------------------------------------------------------------
# Database
# ----------------------------------------------------------------

@dataclass
class Database:
    """
    Central SQLite persistence layer for the station.

    Parameters
    ----------
    path:
        Filesystem path to the SQLite database file. Defaults to
        "<project_root>/database/station.db", where <project_root>
        is this module's own parent directory's parent (i.e.
        station/database.py -> station/ -> project_root/), matching
        the layout config.py uses -- NOT resolved relative to the
        process's current working directory, so it lands in the
        right place regardless of where the station software was
        started from.
    backup_dir:
        Directory holding backups created by backup_database() and
        consulted by automatic corruption recovery. Defaults to a
        "backups" subdirectory next to `path` (so, by default,
        "<project_root>/database/backups").
    log_file:
        Path to the dedicated database error log. Defaults to
        "<project_root>/logs/database.log" (same anchoring as
        `path`, above). Set to None to disable the dedicated file
        handler (e.g. in tests).
    durable:
        If True, use `PRAGMA synchronous = FULL`. Default False uses
        `NORMAL`, the standard recommendation alongside WAL mode.
    busy_timeout:
        Seconds SQLite should wait for a lock before raising
        "database is locked".
    on_corruption:
        Optional callback invoked with a human-readable message if
        automatic corruption recovery runs during connect(), so the
        owning process (station.py) can be notified.
    """

    path: str | Path = _DEFAULT_DB_PATH
    backup_dir: str | Path | None = None
    log_file: str | Path | None = _DEFAULT_LOG_FILE
    durable: bool = False
    busy_timeout: float = 5.0
    on_corruption: Callable[[str], None] | None = None

    _connection: sqlite3.Connection | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )
    _logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("station.database"),
        init=False,
        repr=False,
        compare=False,
    )
    _log_handler: logging.Handler | None = field(
        default=None, init=False, repr=False, compare=False
    )

    # --------------------------------------------------------
    # Construction helpers
    # --------------------------------------------------------

    @classmethod
    def from_config(cls, cfg, **kwargs) -> "Database":
        """
        Build a Database from a station/config.py `Config` instance,
        using `cfg.database_file` as the path. Does not import
        config.py; only reads the attribute.
        """

        return cls(path=getattr(cfg, "database_file"), **kwargs)

    def _resolve_backup_dir(self) -> Path:
        if self.backup_dir is not None:
            return Path(self.backup_dir)
        return Path(self.path).resolve().parent / "backups"

    # --------------------------------------------------------
    # Logging setup
    # --------------------------------------------------------

    def _configure_file_logging(self) -> None:
        if self.log_file is None:
            return

        log_path = Path(self.log_file).resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Guard against attaching a duplicate handler for the same
        # file, whether from an earlier connect() on this same
        # instance or from a *different* Database instance sharing
        # this logger (all Database objects log to the same
        # "station.database" logger name).
        for handler in self._logger.handlers:
            if (
                isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename) == log_path
            ):
                self._log_handler = handler
                return

        handler = logging.FileHandler(str(log_path))
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )

        self._logger.addHandler(handler)
        self._log_handler = handler

        self._logger.info("Database logging to %s", log_path)

    # --------------------------------------------------------
    # Connection management
    # --------------------------------------------------------

    def connect(self) -> None:
        """
        Open the database, creating and/or upgrading it as needed,
        verifying its integrity, and attempting automatic recovery
        (see the module docstring) if corruption is detected.

        Safe to call more than once; subsequent calls are a no-op
        while already connected.
        """

        if self._connection is not None:
            self._logger.debug("Already connected to %s", self.path)
            return

        self._configure_file_logging()

        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._open_and_prepare(db_path)
        except (sqlite3.DatabaseError, SchemaError) as exc:
            if isinstance(exc, SchemaError):
                # A deliberate version-mismatch refusal, not
                # corruption -- do not attempt recovery, just fail.
                raise

            self._logger.critical(
                "Database at %s appears corrupt: %s", db_path, exc
            )
            self._handle_corruption(db_path, str(exc))
            # Retry once against the (hopefully restored) file.
            self._open_and_prepare(db_path)

        if not self.check_integrity():
            self._logger.critical(
                "Database at %s failed integrity check on connect",
                db_path,
            )
            self._handle_corruption(db_path, "integrity_check failed")
            self._open_and_prepare(db_path)

            if not self.check_integrity():
                raise DatabaseError(
                    f"Database at {db_path} is still failing its "
                    f"integrity check after attempting recovery"
                )

        self._record_startup()

        self._logger.info(
            "Database ready at %s (schema version %d)",
            db_path,
            CURRENT_SCHEMA_VERSION,
        )

    def _open_and_prepare(self, db_path: Path) -> None:
        """Open the connection, set PRAGMAs, and initialize the schema."""

        self._logger.info("Opening database at %s", db_path)

        connection = sqlite3.connect(str(db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row

        with self._lock:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                f"PRAGMA synchronous = {'FULL' if self.durable else 'NORMAL'}"
            )
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}"
            )

        self._connection = connection
        self.initialize()

    def close(self) -> None:
        """
        Run `PRAGMA optimize` (SQLite's recommended pre-close
        maintenance step) and close the connection, if open.
        """

        if self._connection is None:
            return

        self._logger.info("Closing database at %s", self.path)

        with self._lock:
            try:
                self._connection.execute("PRAGMA optimize")
            except sqlite3.Error as exc:
                self._logger.warning(
                    "PRAGMA optimize failed on close: %s", exc
                )
            finally:
                self._connection.close()
                self._connection = None

    def is_connected(self) -> bool:
        """Return True if the database connection is open."""

        return self._connection is not None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    # --------------------------------------------------------
    # Transaction helper -- every write in this module goes
    # through this, so every write is logged and rolled back
    # consistently on failure.
    # --------------------------------------------------------

    @contextmanager
    def _cursor(self):
        """
        Yield a cursor inside a lock-protected transaction. Commits
        on normal exit; rolls back and logs+re-raises as
        DatabaseError on any sqlite3.Error.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            cursor = self._connection.cursor()
            try:
                yield cursor
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                self._logger.error(
                    "Database operation failed, rolled back: %s",
                    exc,
                    exc_info=True,
                )
                raise DatabaseError(f"Database operation failed: {exc}") from exc
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def _upsert_singleton(
        self, cursor: sqlite3.Cursor, table: str, values: dict
    ) -> None:
        """Insert-or-update the single row (id=1) of a singleton table."""

        columns = ["id"] + list(values.keys())
        placeholders = ", ".join(["?"] * len(columns))
        assignments = ", ".join(f"{col} = excluded.{col}" for col in values)

        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            [1] + list(values.values()),
        )

    def _get_singleton(self, cursor: sqlite3.Cursor, table: str) -> dict:
        row = cursor.execute(f"SELECT * FROM {table} WHERE id = 1").fetchone()
        return dict(row) if row else {}

    # --------------------------------------------------------
    # Schema management
    # --------------------------------------------------------

    def _get_schema_version(self) -> int:
        assert self._connection is not None

        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()

        return int(row[0]) if row else 0

    def _set_schema_version(self, version: int) -> None:
        assert self._connection is not None

        self._connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )

    def initialize(self) -> None:
        """
        Create the schema if missing, or apply any migrations needed
        to bring an existing database up to CURRENT_SCHEMA_VERSION.
        Idempotent: safe to call repeatedly.

        Raises
        ------
        SchemaError
            If the database reports a schema version newer than this
            code supports, or if a migration fails partway through.
        """

        assert self._connection is not None

        with self._lock:

            version = self._get_schema_version()

            if version > CURRENT_SCHEMA_VERSION:
                raise SchemaError(
                    f"Database schema version {version} is newer "
                    f"than this code supports "
                    f"({CURRENT_SCHEMA_VERSION}); refusing to open "
                    f"it to avoid data loss. Upgrade station "
                    f"software before using this database file."
                )

            for index in range(version, CURRENT_SCHEMA_VERSION):

                migration = _MIGRATIONS[index]

                self._logger.info(
                    "Applying schema migration %d -> %d", index, index + 1
                )

                try:
                    migration(self._connection)
                    self._set_schema_version(index + 1)
                    self._connection.commit()
                except sqlite3.Error as exc:
                    self._connection.rollback()
                    self._logger.error(
                        "Schema migration to version %d failed: %s",
                        index + 1,
                        exc,
                        exc_info=True,
                    )
                    raise SchemaError(
                        f"Schema migration to version {index + 1} "
                        f"failed: {exc}"
                    ) from exc

    def upgrade_schema(self) -> None:
        """
        Public, explicit trigger to apply any pending schema
        migrations. `connect()` already calls this via `initialize()`
        automatically; this is exposed for maintenance scripts that
        want to upgrade a database file without a full station
        startup.
        """

        self.initialize()

    def _record_startup(self) -> None:
        """Record the current UTC time as station_info.last_startup."""

        with self._cursor() as cursor:
            self._upsert_singleton(
                cursor, "station_info", {"last_startup": _utcnow_iso()}
            )

    # --------------------------------------------------------
    # Integrity, backup, recovery
    # --------------------------------------------------------

    def integrity_check(self) -> bool:
        """
        Run SQLite's built-in integrity and foreign-key checks.

        Returns True if `PRAGMA integrity_check` reports "ok" and
        `PRAGMA foreign_key_check` reports no violations; False
        otherwise (with details logged at ERROR level).
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            try:
                integrity_rows = self._connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
            except sqlite3.Error as exc:
                raise DatabaseError(
                    f"Integrity check failed to run: {exc}"
                ) from exc

            ok = len(integrity_rows) == 1 and integrity_rows[0][0] == "ok"

            if not ok:
                self._logger.error(
                    "Database integrity check failed: %s",
                    [tuple(row) for row in integrity_rows],
                )

            try:
                fk_violations = self._connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            except sqlite3.Error as exc:
                raise DatabaseError(
                    f"Foreign key check failed to run: {exc}"
                ) from exc

            if fk_violations:
                ok = False
                self._logger.error(
                    "Foreign key violations detected: %s",
                    [tuple(row) for row in fk_violations],
                )

            return ok

    # check_integrity is kept as an alias: earlier drafts of this
    # module (and any code already written against them) used this
    # name; integrity_check() is the name specified for this version.
    check_integrity = integrity_check

    def backup(self, destination: str | Path) -> None:
        """
        Take a consistent, hot backup of the entire database to
        `destination`, using SQLite's native online backup API (safe
        to call while the station is actively writing).
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger.info("Backing up database to %s", dest_path)

        dest_conn = sqlite3.connect(str(dest_path))

        try:
            with self._lock:
                self._connection.backup(dest_conn)
        except sqlite3.Error as exc:
            self._logger.error(
                "Backup to %s failed: %s", dest_path, exc, exc_info=True
            )
            raise DatabaseError(f"Backup to {dest_path} failed: {exc}") from exc
        finally:
            dest_conn.close()

        self._logger.info("Backup complete: %s", dest_path)

    def backup_database(self) -> Path:
        """
        Convenience wrapper around backup(): writes a dated backup
        named "station_YYYYMMDD.db" into `backup_dir`. Calling this
        more than once on the same UTC day overwrites that day's
        backup file.

        Returns the path written.
        """

        backup_dir = self._resolve_backup_dir()
        destination = backup_dir / f"station_{datetime.now(timezone.utc):%Y%m%d}.db"

        self.backup(destination)

        return destination

    def vacuum(self) -> None:
        """
        Reclaim disk space and defragment the database file. This
        can take a while on a large database and briefly blocks
        other access; run it during a maintenance window.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        self._logger.info("Running VACUUM on %s", self.path)

        with self._lock:
            try:
                self._connection.execute("VACUUM")
            except sqlite3.Error as exc:
                self._logger.error("VACUUM failed: %s", exc, exc_info=True)
                raise DatabaseError(f"VACUUM failed: {exc}") from exc

    def _notify(self, message: str) -> None:
        """Log a critical message and invoke on_corruption(), if set."""

        self._logger.critical(message)

        if self.on_corruption is not None:
            try:
                self.on_corruption(message)
            except Exception:
                self._logger.exception(
                    "on_corruption callback raised an exception"
                )

    def _handle_corruption(self, db_path: Path, reason: str) -> None:
        """
        Implements the five-step recovery procedure described in the
        module docstring. Raises DatabaseError if no usable backup
        exists.
        """

        # 1. Log the error.
        self._logger.critical(
            "Handling suspected corruption of %s: %s", db_path, reason
        )

        # Make sure nothing still holds this file open before we
        # start moving files around.
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None

        # 2. Preserve the damaged database (and WAL/SHM siblings).
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        preserved_path = db_path.with_name(f"{db_path.name}.corrupt.{timestamp}")

        try:
            if db_path.exists():
                shutil.copy2(db_path, preserved_path)
                self._logger.critical(
                    "Preserved damaged database as %s", preserved_path
                )
            for suffix in ("-wal", "-shm"):
                sidecar = db_path.with_name(db_path.name + suffix)
                if sidecar.exists():
                    shutil.copy2(
                        sidecar, preserved_path.with_name(preserved_path.name + suffix)
                    )
        except OSError as exc:
            self._logger.error(
                "Could not preserve damaged database %s: %s", db_path, exc
            )

        # 3. Restore the newest backup.
        backup_dir = self._resolve_backup_dir()
        candidates = sorted(backup_dir.glob("station_*.db")) if backup_dir.exists() else []

        if not candidates:
            self._notify(
                f"Database at {db_path} is corrupt ({reason}) and no "
                f"backup was found in {backup_dir}; manual recovery "
                f"required. The damaged file was preserved at "
                f"{preserved_path}."
            )
            raise DatabaseError(
                f"Corrupt database at {db_path} and no backup available "
                f"in {backup_dir}"
            )

        newest_backup = candidates[-1]

        try:
            shutil.copy2(newest_backup, db_path)
        except OSError as exc:
            self._notify(
                f"Database at {db_path} is corrupt ({reason}); found "
                f"backup {newest_backup} but could not restore it: {exc}"
            )
            raise DatabaseError(
                f"Could not restore backup {newest_backup} to {db_path}: {exc}"
            ) from exc

        self._logger.critical(
            "Restored %s from backup %s", db_path, newest_backup
        )

        # 4. "Continue operation if possible" happens back in
        #    connect(), which retries _open_and_prepare() after this
        #    method returns and re-checks integrity_check().

        # 5. Notify station.py.
        self._notify(
            f"Database at {db_path} was corrupt ({reason}). Restored "
            f"from backup {newest_backup}. Damaged file preserved at "
            f"{preserved_path}."
        )

    # --------------------------------------------------------
    # Station info
    # --------------------------------------------------------

    def save_station_info(self, **fields) -> None:
        """
        Update one or more columns of the single station_info row.
        Accepted keys: station_id, station_name, organization,
        latitude, longitude, height, antenna_model, receiver_model,
        receiver_serial_number, firmware, installation_date,
        software_version. (last_startup is managed by connect().)
        """

        allowed = {
            "station_id",
            "station_name",
            "organization",
            "latitude",
            "longitude",
            "height",
            "antenna_model",
            "receiver_model",
            "receiver_serial_number",
            "firmware",
            "installation_date",
            "software_version",
        }

        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown station_info field(s): {sorted(unknown)}")

        if not fields:
            return

        with self._cursor() as cursor:
            self._upsert_singleton(cursor, "station_info", fields)

    def get_station_info(self) -> StationInfo:
        """Return the station_info row (always exists after connect())."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM station_info WHERE id = 1"
            ).fetchone()

        return StationInfo(**dict(row)) if row else StationInfo()

    # --------------------------------------------------------
    # Receiver methods
    # --------------------------------------------------------

    def save_receiver_status(
        self,
        position=None,
        *,
        connected: bool = True,
        last_command: str = "",
        last_response_time: float | None = None,
        communication_errors: int | None = None,
        receiver_temperature: float | None = None,
        uptime: float | None = None,
    ) -> None:
        """
        Update the single receiver_status row.

        `position`, if given, is any object exposing `solution`,
        `latitude`, `longitude`, `height`, `differential_age`,
        `solution_age`, `num_svs_tracked`, `num_svs_in_solution`
        (i.e. a receiver.PositionInfo). Any keyword argument left at
        its default preserves the previously stored value rather
        than being reset, since this call may be made with only
        connectivity information (e.g. after a failed query) and no
        fresh position.
        """

        with self._cursor() as cursor:
            current = self._get_singleton(cursor, "receiver_status")

            values = {
                "timestamp": _utcnow_iso(),
                "connected": int(connected),
                "solution_status": (
                    getattr(position, "solution", "")
                    if position is not None
                    else current.get("solution_status", "")
                ),
                "latitude": (
                    getattr(position, "latitude", 0.0)
                    if position is not None
                    else current.get("latitude", 0.0)
                ),
                "longitude": (
                    getattr(position, "longitude", 0.0)
                    if position is not None
                    else current.get("longitude", 0.0)
                ),
                "height": (
                    getattr(position, "height", 0.0)
                    if position is not None
                    else current.get("height", 0.0)
                ),
                "differential_age": (
                    getattr(position, "differential_age", 0.0)
                    if position is not None
                    else current.get("differential_age", 0.0)
                ),
                "solution_age": (
                    getattr(position, "solution_age", 0.0)
                    if position is not None
                    else current.get("solution_age", 0.0)
                ),
                "tracked_satellites": (
                    getattr(position, "num_svs_tracked", 0)
                    if position is not None
                    else current.get("tracked_satellites", 0)
                ),
                "solution_satellites": (
                    getattr(position, "num_svs_in_solution", 0)
                    if position is not None
                    else current.get("solution_satellites", 0)
                ),
                "receiver_temperature": (
                    receiver_temperature
                    if receiver_temperature is not None
                    else current.get("receiver_temperature")
                ),
                "uptime": uptime if uptime is not None else current.get("uptime"),
                "last_command": last_command or current.get("last_command", ""),
                "last_response_time": (
                    last_response_time
                    if last_response_time is not None
                    else current.get("last_response_time")
                ),
                "communication_errors": (
                    communication_errors
                    if communication_errors is not None
                    else current.get("communication_errors", 0)
                ),
            }

            self._upsert_singleton(cursor, "receiver_status", values)

    def get_receiver_status(self) -> ReceiverStatus:
        """Return the current receiver_status row."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM receiver_status WHERE id = 1"
            ).fetchone()

        if not row:
            return ReceiverStatus()

        data = dict(row)
        data["connected"] = bool(data["connected"])

        return ReceiverStatus(**data)

    def save_receiver_version(self, info, software_version: str = "") -> None:
        """
        Record a receiver.VersionInfo (or any object exposing model,
        firmware, psn) into station_info's receiver_model /
        receiver_serial_number / firmware columns.

        `software_version`, if given, updates station_info's
        separate software_version column -- this is meant to be
        *this station software's* version (station/version.py), not
        the GNSS receiver's firmware, so it isn't pulled from `info`.
        """

        values = {
            "receiver_model": getattr(info, "model", ""),
            "receiver_serial_number": getattr(info, "psn", ""),
            "firmware": getattr(info, "firmware", ""),
        }

        if software_version:
            values["software_version"] = software_version

        with self._cursor() as cursor:
            self._upsert_singleton(cursor, "station_info", values)

    # --------------------------------------------------------
    # Position methods
    # --------------------------------------------------------

    def save_position(self, info, day: str | date | None = None) -> int:
        """
        Record a receiver.PositionInfo (or any object exposing the
        same attributes) both as a new position_history row and as
        an update to receiver_status's position-related columns
        (leaving connectivity fields like last_command untouched --
        use save_receiver_status() for those).

        Returns the new position_history row id.
        """

        day_str = _normalize_day(day) if day is not None else _utcnow_date_str()
        timestamp = _utcnow_iso()

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO position_history (
                    timestamp, day, solution_status, latitude,
                    longitude, height, differential_age,
                    solution_age, tracked_satellites,
                    solution_satellites, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    day_str,
                    getattr(info, "solution", ""),
                    getattr(info, "latitude", 0.0),
                    getattr(info, "longitude", 0.0),
                    getattr(info, "height", 0.0),
                    getattr(info, "differential_age", 0.0),
                    getattr(info, "solution_age", 0.0),
                    getattr(info, "num_svs_tracked", 0),
                    getattr(info, "num_svs_in_solution", 0),
                    getattr(info, "raw", ""),
                ),
            )

            row_id = int(cursor.lastrowid)

            current = self._get_singleton(cursor, "receiver_status")

            self._upsert_singleton(
                cursor,
                "receiver_status",
                {
                    "timestamp": timestamp,
                    "connected": current.get("connected", 1) or 1,
                    "solution_status": getattr(info, "solution", ""),
                    "latitude": getattr(info, "latitude", 0.0),
                    "longitude": getattr(info, "longitude", 0.0),
                    "height": getattr(info, "height", 0.0),
                    "differential_age": getattr(info, "differential_age", 0.0),
                    "solution_age": getattr(info, "solution_age", 0.0),
                    "tracked_satellites": getattr(info, "num_svs_tracked", 0),
                    "solution_satellites": getattr(
                        info, "num_svs_in_solution", 0
                    ),
                    "receiver_temperature": current.get("receiver_temperature"),
                    "uptime": current.get("uptime"),
                    "last_command": current.get("last_command", ""),
                    "last_response_time": current.get("last_response_time"),
                    "communication_errors": current.get("communication_errors", 0),
                },
            )

            return row_id

    def latest_position(self) -> PositionRecord | None:
        """Return the most recently recorded position, or None."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM position_history ORDER BY id DESC LIMIT 1"
            ).fetchone()

        return PositionRecord(**dict(row)) if row else None

    def position_history(
        self, day: str | date | None = None, limit: int = 100
    ) -> list[PositionRecord]:
        """Return recorded positions, newest first, optionally filtered by day."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            if day is not None:
                rows = self._connection.execute(
                    "SELECT * FROM position_history WHERE day = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (_normalize_day(day), limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM position_history ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        return [PositionRecord(**dict(row)) for row in rows]

    # --------------------------------------------------------
    # Raw file methods
    # --------------------------------------------------------

    def add_raw_file(
        self,
        filename: str,
        date_: str | date,
        start_time: str | None = None,
        end_time: str | None = None,
        size: int | None = None,
        checksum: str | None = None,
        notes: str = "",
    ) -> int:
        """Record a new raw data file. Returns the new row id."""

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO raw_files (
                    filename, date, start_time, end_time, size,
                    checksum, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    filename,
                    _normalize_day(date_),
                    start_time,
                    end_time,
                    size,
                    checksum,
                    notes,
                ),
            )

            return int(cursor.lastrowid)

    def update_raw_file(self, filename: str, **fields) -> None:
        """
        Update one or more columns of an existing raw_files row.

        Allowed keyword arguments: start_time, end_time, size,
        checksum, archived, deleted, notes.

        Raises
        ------
        NotFoundError
            If no raw_files row exists for `filename`.
        ValueError
            If an unsupported field name is given.
        """

        unknown = set(fields) - _RAW_FILE_UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Unknown raw_files field(s): {sorted(unknown)}")

        if not fields:
            return

        if "archived" in fields:
            fields["archived"] = int(bool(fields["archived"]))
        if "deleted" in fields:
            fields["deleted"] = int(bool(fields["deleted"]))

        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = list(fields.values()) + [filename]

        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE raw_files SET {assignments} WHERE filename = ?",
                params,
            )

            if cursor.rowcount == 0:
                raise NotFoundError(f"No raw_files row exists for {filename!r}")

    def delete_raw_file(self, filename: str, notes: str = "") -> None:
        """
        Mark a raw file as deleted (soft delete: the row is kept, so
        rinex_files.raw_filename and processing_queue history remain
        valid). This does not remove the file from disk; callers
        should delete the actual file themselves before or after
        calling this.

        Raises
        ------
        NotFoundError
            If no raw_files row exists for `filename`.
        """

        with self._cursor() as cursor:
            if notes:
                cursor.execute(
                    "UPDATE raw_files SET deleted = 1, notes = ? "
                    "WHERE filename = ?",
                    (notes, filename),
                )
            else:
                cursor.execute(
                    "UPDATE raw_files SET deleted = 1 WHERE filename = ?",
                    (filename,),
                )

            if cursor.rowcount == 0:
                raise NotFoundError(f"No raw_files row exists for {filename!r}")

    def pending_raw_files(self) -> list[RawFile]:
        """
        Return raw files that are not deleted and have no successful
        RINEX conversion recorded yet, oldest first.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT r.* FROM raw_files r
                WHERE r.deleted = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM rinex_files x
                      WHERE x.raw_filename = r.filename
                        AND x.conversion_success = 1
                  )
                ORDER BY r.date ASC, r.id ASC
                """
            ).fetchall()

        results = []
        for row in rows:
            data = dict(row)
            data["archived"] = bool(data["archived"])
            data["deleted"] = bool(data["deleted"])
            results.append(RawFile(**data))

        return results

    def latest_raw_file(self) -> RawFile | None:
        """
        Return the most recently added raw_files row (by id, i.e.
        insertion order), or None if none have been recorded yet.
        Added alongside latest_rinex() so station.py's dashboard can
        show "newest raw file" the same way it shows "newest RINEX"
        without running SQL of its own.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM raw_files ORDER BY id DESC LIMIT 1"
            ).fetchone()

        if not row:
            return None

        data = dict(row)
        data["archived"] = bool(data["archived"])
        data["deleted"] = bool(data["deleted"])

        return RawFile(**data)

    # --------------------------------------------------------
    # RINEX methods
    # --------------------------------------------------------

    def save_rinex(
        self,
        raw_filename: str,
        observation_file: str = "",
        navigation_file: str = "",
        sbas_file: str = "",
        conversion_success: bool = True,
        convbin_version: str = "",
        processing_notes: str = "",
    ) -> int:
        """
        Record a RINEX conversion attempt. Each call appends a new
        row, so retries after a failed conversion remain in the
        history rather than overwriting it. Returns the new row id.
        """

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO rinex_files (
                    raw_filename, observation_file, navigation_file,
                    sbas_file, conversion_success, conversion_time,
                    convbin_version, processing_notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_filename,
                    observation_file,
                    navigation_file,
                    sbas_file,
                    int(conversion_success),
                    _utcnow_iso(),
                    convbin_version,
                    processing_notes,
                ),
            )

            return int(cursor.lastrowid)

    def latest_rinex(self, raw_filename: str | None = None) -> RinexFile | None:
        """
        Return the most recent RINEX conversion attempt, optionally
        restricted to a specific raw file.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            if raw_filename is not None:
                row = self._connection.execute(
                    "SELECT * FROM rinex_files WHERE raw_filename = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (raw_filename,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM rinex_files ORDER BY id DESC LIMIT 1"
                ).fetchone()

        if not row:
            return None

        data = dict(row)
        data["conversion_success"] = bool(data["conversion_success"])

        return RinexFile(**data)

    def rinex_history(
        self, raw_filename: str | None = None, limit: int = 100
    ) -> list[RinexFile]:
        """Return RINEX conversion attempts, newest first, optionally filtered."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            if raw_filename is not None:
                rows = self._connection.execute(
                    "SELECT * FROM rinex_files WHERE raw_filename = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (raw_filename, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM rinex_files ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        results = []
        for row in rows:
            data = dict(row)
            data["conversion_success"] = bool(data["conversion_success"])
            results.append(RinexFile(**data))

        return results

    # --------------------------------------------------------
    # GNSS-IR methods
    # --------------------------------------------------------

    def save_gnssir_product(
        self,
        date_: str | date,
        rinex_file: str = "",
        reflector_height: float | None = None,
        soil_moisture: float | None = None,
        snow_depth: float | None = None,
        quality_score: float | None = None,
        output_directory: str = "",
        processing_success: bool = True,
        runtime: float | None = None,
        notes: str = "",
    ) -> int:
        """Record one gnssrefl processing run. Returns the new row id."""

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO gnssir_products (
                    date, rinex_file, reflector_height, soil_moisture,
                    snow_depth, quality_score, output_directory,
                    processing_success, runtime, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _normalize_day(date_),
                    rinex_file,
                    reflector_height,
                    soil_moisture,
                    snow_depth,
                    quality_score,
                    output_directory,
                    int(processing_success),
                    runtime,
                    notes,
                ),
            )

            return int(cursor.lastrowid)

    def gnssir_history(
        self, day: str | date | None = None, limit: int = 100
    ) -> list[GnssirProduct]:
        """Return gnssrefl processing runs, newest first, optionally filtered by day."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            if day is not None:
                rows = self._connection.execute(
                    "SELECT * FROM gnssir_products WHERE date = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (_normalize_day(day), limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM gnssir_products ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        results = []
        for row in rows:
            data = dict(row)
            data["processing_success"] = bool(data["processing_success"])
            results.append(GnssirProduct(**data))

        return results

    # --------------------------------------------------------
    # Processing queue methods
    # --------------------------------------------------------

    def queue_file(self, filename: str, priority: int = 0) -> int:
        """
        Add `filename` to the processing queue, or reset it to a
        fresh "waiting" state if it was already queued (e.g. for a
        reprocessing request). Returns the row id.
        """

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO processing_queue (
                    filename, waiting, running, completed, failed,
                    priority
                ) VALUES (?, 1, 0, 0, 0, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    waiting = 1,
                    running = 0,
                    completed = 0,
                    failed = 0,
                    priority = excluded.priority
                """,
                (filename, priority),
            )

            row = cursor.execute(
                "SELECT id FROM processing_queue WHERE filename = ?",
                (filename,),
            ).fetchone()

        assert row is not None

        return int(row[0])

    def start_processing(self, filename: str) -> None:
        """
        Mark a queued file as currently being processed.

        Raises
        ------
        NotFoundError
            If `filename` was never queued.
        """

        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE processing_queue SET waiting = 0, running = 1, "
                "last_attempt = ? WHERE filename = ?",
                (_utcnow_iso(), filename),
            )

            if cursor.rowcount == 0:
                raise NotFoundError(f"{filename!r} is not in the processing queue")

    def finish_processing(
        self, filename: str, success: bool = True, error_message: str = ""
    ) -> None:
        """
        Mark a file's processing as finished, successfully or not.
        On failure, increments retry_count and records error_message.

        Raises
        ------
        NotFoundError
            If `filename` was never queued.
        """

        with self._cursor() as cursor:
            if success:
                cursor.execute(
                    "UPDATE processing_queue SET running = 0, "
                    "completed = 1, failed = 0, error_message = NULL "
                    "WHERE filename = ?",
                    (filename,),
                )
            else:
                cursor.execute(
                    "UPDATE processing_queue SET running = 0, failed = 1, "
                    "retry_count = retry_count + 1, error_message = ? "
                    "WHERE filename = ?",
                    (error_message, filename),
                )

            if cursor.rowcount == 0:
                raise NotFoundError(f"{filename!r} is not in the processing queue")

    def processing_history(
        self, filename: str | None = None
    ) -> list[ProcessingQueueEntry]:
        """
        Return processing_queue rows. Nothing is ever deleted from
        this table, so querying it (optionally filtered to one
        filename) doubles as that filename's processing history.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            if filename is not None:
                rows = self._connection.execute(
                    "SELECT * FROM processing_queue WHERE filename = ? "
                    "ORDER BY id ASC",
                    (filename,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM processing_queue ORDER BY id ASC"
                ).fetchall()

        results = []
        for row in rows:
            data = dict(row)
            for flag in ("waiting", "running", "completed", "failed"):
                data[flag] = bool(data[flag])
            results.append(ProcessingQueueEntry(**data))

        return results

    def processing_statistics(self) -> ProcessingStatistics:
        """
        Return an aggregate count of processing_queue rows by state
        (waiting/running/completed/failed), total accumulated
        retries, and the most recent last_attempt overall vs. among
        completed rows only, for dashboard/summary use (e.g.
        pipeline.py's queue_status(), or station.py's dashboard)
        without querying processing_queue directly.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(waiting), 0),
                    COALESCE(SUM(running), 0),
                    COALESCE(SUM(completed), 0),
                    COALESCE(SUM(failed), 0),
                    COALESCE(SUM(retry_count), 0),
                    MAX(last_attempt),
                    MAX(CASE WHEN completed = 1 THEN last_attempt END)
                FROM processing_queue
                """
            ).fetchone()

        (
            total,
            waiting,
            running,
            completed,
            failed,
            total_retries,
            last_attempt,
            last_successful_attempt,
        ) = row

        return ProcessingStatistics(
            total=total,
            waiting=waiting,
            running=running,
            completed=completed,
            failed=failed,
            total_retries=total_retries,
            last_attempt=last_attempt,
            last_successful_attempt=last_successful_attempt,
        )

    # --------------------------------------------------------
    # Error methods
    # --------------------------------------------------------

    def log_error(
        self,
        module: str,
        severity: str,
        exception: str = "",
        description: str = "",
        stack_trace: str = "",
        recovered: bool = False,
        notes: str = "",
    ) -> int:
        """
        Record an application-level error from any part of the
        station software. This is never used for *database* errors
        themselves -- those only ever go through `logging`, to avoid
        recursive failure if the database is the problem.
        """

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO error_log (
                    timestamp, module, severity, exception,
                    description, stack_trace, recovered, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    module,
                    severity,
                    exception,
                    description,
                    stack_trace,
                    int(recovered),
                    notes,
                ),
            )

            return int(cursor.lastrowid)

    def clear_error(self, error_id: int, notes: str = "") -> None:
        """
        Mark a logged error as recovered/resolved. The row is kept
        (error_log is a permanent history), only its `recovered` flag
        (and optionally `notes`) is updated.

        Raises
        ------
        NotFoundError
            If no error_log row with this id exists.
        """

        with self._cursor() as cursor:
            if notes:
                cursor.execute(
                    "UPDATE error_log SET recovered = 1, notes = ? "
                    "WHERE id = ?",
                    (notes, error_id),
                )
            else:
                cursor.execute(
                    "UPDATE error_log SET recovered = 1 WHERE id = ?",
                    (error_id,),
                )

            if cursor.rowcount == 0:
                raise NotFoundError(f"No error_log row with id {error_id}")

    def recent_errors(
        self,
        limit: int = 50,
        module: str | None = None,
        severity: str | None = None,
    ) -> list[ErrorLogEntry]:
        """Return recent errors, newest first, optionally filtered."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        clauses = []
        params: list = []

        if module is not None:
            clauses.append("module = ?")
            params.append(module)

        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM error_log {where} ORDER BY id DESC LIMIT ?",
                params + [limit],
            ).fetchall()

        results = []
        for row in rows:
            data = dict(row)
            data["recovered"] = bool(data["recovered"])
            results.append(ErrorLogEntry(**data))

        return results

    # --------------------------------------------------------
    # System health methods
    # --------------------------------------------------------

    def save_system_health(
        self,
        cpu_usage: float | None = None,
        memory_usage: float | None = None,
        disk_usage: float | None = None,
        disk_free: int | None = None,
        database_size: int | None = None,
        receiver_connected: bool = False,
        internet_connected: bool = False,
        newest_raw_file: str = "",
        newest_rinex: str = "",
        newest_product: str = "",
    ) -> int:
        """Record a periodic system health snapshot. Returns the new row id."""

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO system_health (
                    timestamp, cpu_usage, memory_usage, disk_usage,
                    disk_free, database_size, receiver_connected,
                    internet_connected, newest_raw_file, newest_rinex,
                    newest_product
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    cpu_usage,
                    memory_usage,
                    disk_usage,
                    disk_free,
                    database_size,
                    int(receiver_connected),
                    int(internet_connected),
                    newest_raw_file,
                    newest_rinex,
                    newest_product,
                ),
            )

            return int(cursor.lastrowid)

    def latest_system_health(self) -> SystemHealth | None:
        """Return the most recent system health snapshot, or None."""

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM system_health ORDER BY id DESC LIMIT 1"
            ).fetchone()

        if not row:
            return None

        data = dict(row)
        data["receiver_connected"] = bool(data["receiver_connected"])
        data["internet_connected"] = bool(data["internet_connected"])

        return SystemHealth(**data)

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    def save_command_statistics(self, stats) -> None:
        """
        Record receiver command timing statistics, as returned by
        `receiver.Receiver.stats()` -- a {command_name: CommandStats}
        -like mapping. Each command gets one row, upserted in place
        (this table holds the current cumulative counters imported
        from receiver.py, not a time series).

        `last_successful_query` is set to the current time whenever
        the reported `count` (total successes) is greater than zero,
        i.e. it reflects "as of this snapshot, this command has
        succeeded at least once" -- it is not a per-attempt log.
        """

        now = _utcnow_iso()

        with self._cursor() as cursor:
            for command, entry in stats.items():

                min_time = getattr(entry, "min_time", None)
                if min_time == float("inf"):
                    min_time = None

                count = getattr(entry, "count", 0)

                existing = cursor.execute(
                    "SELECT last_successful_query FROM command_statistics "
                    "WHERE command = ?",
                    (command,),
                ).fetchone()

                last_successful_query = (
                    now if count > 0 else (existing[0] if existing else None)
                )

                cursor.execute(
                    """
                    INSERT INTO command_statistics (
                        command, count, failures, average_time,
                        minimum_time, maximum_time, last_successful_query
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(command) DO UPDATE SET
                        count = excluded.count,
                        failures = excluded.failures,
                        average_time = excluded.average_time,
                        minimum_time = excluded.minimum_time,
                        maximum_time = excluded.maximum_time,
                        last_successful_query = excluded.last_successful_query
                    """,
                    (
                        command,
                        count,
                        getattr(entry, "failures", 0),
                        getattr(entry, "average_time", 0.0),
                        min_time,
                        getattr(entry, "max_time", 0.0),
                        last_successful_query,
                    ),
                )

    def station_statistics(self) -> StationStatistics:
        """
        Return an aggregate, dashboard-style snapshot combining
        station_info, the current receiver_status, and row counts
        from the other tables.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        info = self.get_station_info()
        status = self.get_receiver_status()

        with self._lock:
            total_raw_files = self._connection.execute(
                "SELECT COUNT(*) FROM raw_files"
            ).fetchone()[0]
            total_rinex_files = self._connection.execute(
                "SELECT COUNT(*) FROM rinex_files"
            ).fetchone()[0]
            total_gnssir_products = self._connection.execute(
                "SELECT COUNT(*) FROM gnssir_products"
            ).fetchone()[0]
            total_errors = self._connection.execute(
                "SELECT COUNT(*) FROM error_log"
            ).fetchone()[0]

        return StationStatistics(
            station_id=info.station_id,
            station_name=info.station_name,
            receiver_model=info.receiver_model,
            connected=status.connected,
            solution_status=status.solution_status,
            latitude=status.latitude,
            longitude=status.longitude,
            height=status.height,
            total_raw_files=total_raw_files,
            total_rinex_files=total_rinex_files,
            total_gnssir_products=total_gnssir_products,
            total_errors=total_errors,
            pending_files=len(self.pending_raw_files()),
            installation_date=info.installation_date,
            last_startup=info.last_startup,
        )

    def database_statistics(self) -> DatabaseStatistics:
        """
        Return a file-level overview of the database: on-disk size,
        table and total-record counts, the schema version, and the
        newest backup found in `backup_dir` (if any). Intended for
        dashboards (e.g. station.py) so they never need to query
        sqlite_master or the filesystem themselves.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            table_rows = self._connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT IN "
                "('sqlite_sequence', 'schema_meta')"
            ).fetchall()

            total_records = 0
            for (table_name,) in table_rows:
                total_records += self._connection.execute(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).fetchone()[0]

        db_path = Path(self.path)
        size_bytes = db_path.stat().st_size if db_path.exists() else 0

        backup_dir = self._resolve_backup_dir()
        backups = sorted(backup_dir.glob("station_*.db")) if backup_dir.exists() else []
        last_backup = backups[-1].name if backups else None

        return DatabaseStatistics(
            path=str(db_path),
            schema_version=self._get_schema_version(),
            size_bytes=size_bytes,
            total_tables=len(table_rows),
            total_records=total_records,
            last_backup=last_backup,
        )

    def generate_daily_summary(self, day: str | date | None = None) -> DailySummary:
        """
        Compute and store the daily_summary row for `day` (defaults
        to the current UTC calendar day; typically called for
        *yesterday* right after a midnight rollover).

        `hours_running` and `downtime` are not derivable from this
        table alone (the database has no notion of when the station
        process itself was or wasn't running); pass them in via
        `extra` if the caller (e.g. station_manager.py, which does
        track that) wants them recorded. Everything else is computed
        from raw_files, rinex_files, gnssir_products, error_log, and
        position_history for that day.
        """

        import json

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        day_str = _normalize_day(day) if day is not None else _utcnow_date_str()

        with self._lock:
            files_collected = self._connection.execute(
                "SELECT COUNT(*) FROM raw_files WHERE date = ?", (day_str,)
            ).fetchone()[0]

            rinex_created = self._connection.execute(
                "SELECT COUNT(*) FROM rinex_files "
                "WHERE conversion_time LIKE ? AND conversion_success = 1",
                (f"{day_str}%",),
            ).fetchone()[0]

            files_processed = rinex_created

            gnssir_completed = self._connection.execute(
                "SELECT COUNT(*) FROM gnssir_products "
                "WHERE date = ? AND processing_success = 1",
                (day_str,),
            ).fetchone()[0]

            errors = self._connection.execute(
                "SELECT COUNT(*) FROM error_log WHERE timestamp LIKE ?",
                (f"{day_str}%",),
            ).fetchone()[0]

            position_stats = self._connection.execute(
                "SELECT AVG(latitude), AVG(longitude), AVG(height), "
                "AVG(tracked_satellites) FROM position_history "
                "WHERE day = ?",
                (day_str,),
            ).fetchone()

        avg_lat, avg_lon, avg_height, avg_sats = position_stats

        average_position = (
            json.dumps(
                {
                    "latitude": avg_lat,
                    "longitude": avg_lon,
                    "height": avg_height,
                }
            )
            if avg_lat is not None
            else ""
        )

        summary = DailySummary(
            date=day_str,
            files_collected=files_collected,
            files_processed=files_processed,
            rinex_created=rinex_created,
            gnssir_completed=gnssir_completed,
            errors=errors,
            average_position=average_position,
            average_satellites=avg_sats,
        )

        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO daily_summary (
                    date, hours_running, files_collected,
                    files_processed, rinex_created, gnssir_completed,
                    errors, average_position, average_satellites,
                    downtime, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    files_collected = excluded.files_collected,
                    files_processed = excluded.files_processed,
                    rinex_created = excluded.rinex_created,
                    gnssir_completed = excluded.gnssir_completed,
                    errors = excluded.errors,
                    average_position = excluded.average_position,
                    average_satellites = excluded.average_satellites
                """,
                (
                    summary.date,
                    summary.hours_running,
                    summary.files_collected,
                    summary.files_processed,
                    summary.rinex_created,
                    summary.gnssir_completed,
                    summary.errors,
                    summary.average_position,
                    summary.average_satellites,
                    summary.downtime,
                    summary.notes,
                ),
            )

        return summary

    def daily_statistics(
        self, day: str | date | None = None, limit: int = 30
    ) -> DailySummary | list[DailySummary] | None:
        """
        Return the daily_summary row for `day` if given (or None if
        it hasn't been generated yet), or the `limit` most recent
        rows if `day` is omitted.
        """

        if self._connection is None:
            raise DatabaseNotConnectedError("Database is not connected")

        with self._lock:
            if day is not None:
                row = self._connection.execute(
                    "SELECT * FROM daily_summary WHERE date = ?",
                    (_normalize_day(day),),
                ).fetchone()

                return DailySummary(**dict(row)) if row else None

            rows = self._connection.execute(
                "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [DailySummary(**dict(row)) for row in rows]
