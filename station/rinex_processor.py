"""
rinex_processor.py

USGS GNSS Reference Station
Prototype 1.0

Single responsibility: convert a UM980 raw data file into RINEX
observation/navigation/SBAS files using convbin (RTKLIB demo5 or
later, which has native Unicore/UM980 support), and verify the
result actually looks like valid RINEX -- not just that convbin
exited zero.

This module knows nothing about:

    * SQLite (database.py)
    * the GNSS receiver / serial ports (receiver.py)
    * station.py
    * pipeline decisions -- which files to process, retries,
      archiving, or what to do with a ConversionResult once it has
      one (that's all pipeline.py's job)

It receives a file, converts it, verifies it, and reports the
outcome. It never raises out of convert() for an ordinary conversion
failure (missing input, convbin exiting non-zero, bad output, a
timeout, ...); those all come back as a ConversionResult with
success=False and a human-readable message, so pipeline.py's
per-file error handling has one thing to check, not a grab-bag of
exception types to catch.

Overall workflow
-----------------
    pipeline.py
        |
        v
    rinex_processor.py
        |
        +-- validate input
        +-- locate convbin
        +-- build command
        +-- execute convbin
        +-- verify output
        +-- collect statistics
        +-- return ConversionResult

Public API
----------
    RinexProcessor.initialize()       -> "READY" or raises
    RinexProcessor.convert(raw_file)   -> ConversionResult (never raises)
    RinexProcessor.verify(result)       -> bool
    RinexProcessor.status()              -> RinexStatus
    RinexProcessor.shutdown()             -> None

Configuration
-------------
Read from station.json (via a config.Config instance, or any object
exposing the same `raw_dir`, `rinex_dir`, and `station` attributes):

    raw_directory   -> cfg.raw_dir
    rinex_directory  -> cfg.rinex_dir
    convbin_path      -> cfg.station.get("convbin_path")
    rinex_version      -> cfg.station.get("rinex_version")
    log_level            -> cfg.station.get("log_level")

None of these are hardcoded; convbin_path and rinex_version fall
back to sensible defaults (auto-locate on PATH, and "3.05") only if
station.json doesn't specify them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import logging
import math
import os
import shutil
import subprocess
import time

from config import Config


# ----------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------

class RinexProcessorError(Exception):
    """
    Raised only by initialize(), for problems that mean the
    processor cannot run at all (convbin not found, output directory
    cannot be created, ...). convert() never raises this or anything
    else -- see the module docstring.
    """


# ----------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------

@dataclass
class ConversionResult:
    """The outcome of one convert() call."""

    success: bool
    raw_file: Path
    observation_file: Path
    navigation_file: Path
    sbas_file: Path
    message: str
    runtime_seconds: float
    convbin_version: str


@dataclass
class RinexStatistics:
    """Running totals across every convert() call this processor has made."""

    files_processed: int = 0
    successful: int = 0
    failed: int = 0
    average_runtime: float = 0.0
    last_conversion: datetime | None = None


@dataclass
class RinexStatus:
    """Returned by status(): a snapshot for pipeline.py/dashboards."""

    initialized: bool = False
    convbin_found: bool = False
    files_processed: int = 0
    successful: int = 0
    failed: int = 0
    last_runtime: float | None = None


# Real RINEX observation and navigation files both carry this label,
# right-justified, in the header line's columns 61-80. A substring
# check is enough here -- we're confirming convbin actually wrote a
# RINEX file, not fully parsing/validating RINEX format.
_RINEX_HEADER_MARKER = "RINEX VERSION / TYPE"

# How long a single convbin invocation is allowed to run before it's
# treated as a failure rather than left to hang forever.
_DEFAULT_SUBPROCESS_TIMEOUT = 120.0


# ----------------------------------------------------------------
# RinexProcessor
# ----------------------------------------------------------------

@dataclass
class RinexProcessor:
    """
    Converts UM980 raw files to RINEX via convbin and verifies the
    result.

    Parameters
    ----------
    cfg:
        A config.Config instance (or any object exposing `raw_dir`,
        `rinex_dir`, and a `station` dict). If not given,
        initialize() builds one itself.
    subprocess_timeout:
        Seconds to allow a single convbin invocation before treating
        it as a failed conversion.

    Typical usage:

        processor = RinexProcessor()
        processor.initialize()
        result = processor.convert(Path("/raw/test_20260707.um980"))
        if result.success:
            ...
        processor.shutdown()
    """

    cfg: Config | None = None
    subprocess_timeout: float = _DEFAULT_SUBPROCESS_TIMEOUT

    _initialized: bool = field(default=False, init=False, repr=False)
    _convbin_path: str | None = field(default=None, init=False, repr=False)
    _convbin_version: str = field(default="unknown", init=False, repr=False)
    _rinex_directory: Path | None = field(default=None, init=False, repr=False)
    _rinex_version: str = field(default="3.05", init=False, repr=False)
    _configured_convbin_path: str = field(default="", init=False, repr=False)
    _stats: RinexStatistics = field(
        default_factory=RinexStatistics, init=False, repr=False
    )
    _runtimes: list[float] = field(default_factory=list, init=False, repr=False)
    _logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("station.rinex"),
        init=False,
        repr=False,
    )

    # ==========================================================
    # Public API
    # ==========================================================

    def initialize(self) -> str:
        """
        Locate convbin, verify it's executable, verify/create the
        RINEX output directory, and read configuration.

        Returns "READY" on success.

        Raises
        ------
        RinexProcessorError
            If convbin cannot be found, or the output directory
            cannot be created.
        """

        if self.cfg is None:
            self.cfg = Config()

        self._rinex_directory = self.cfg.rinex_dir
        self._rinex_version = self.cfg.station.get("rinex_version", "3.05")
        self._configured_convbin_path = self.cfg.station.get("convbin_path", "")

        log_level_name = str(self.cfg.station.get("log_level", "INFO")).upper()
        self._logger.setLevel(getattr(logging, log_level_name, logging.INFO))

        try:
            self._rinex_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RinexProcessorError(
                f"Could not create RINEX output directory "
                f"{self._rinex_directory}: {exc}"
            ) from exc

        self._convbin_path = self._locate_convbin()
        self._convbin_version = self._detect_convbin_version()

        self._initialized = True

        self._logger.info(
            "RinexProcessor initialized: convbin=%s version=%s output=%s",
            self._convbin_path,
            self._convbin_version,
            self._rinex_directory,
        )

        return "READY"

    def convert(self, raw_file: Path) -> ConversionResult:
        """
        Convert one raw file to RINEX. Never raises: every failure
        mode (not initialized, missing input, convbin failing,
        convbin timing out, bad output) comes back as a
        ConversionResult with success=False and a clear message.
        """

        raw_file = Path(raw_file)

        self._logger.info("START %s", raw_file.name)

        started = time.monotonic()

        if not self._initialized:
            return self._record(
                self._make_result(
                    raw_file,
                    success=False,
                    message="RinexProcessor.initialize() was not called",
                    runtime_seconds=time.monotonic() - started,
                )
            )

        problem = self._validate_input(raw_file)

        observation_file, navigation_file, sbas_file = self._create_output_names(
            raw_file
        )

        if problem:
            elapsed = time.monotonic() - started
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", raw_file.name, problem, elapsed
            )
            return self._record(
                ConversionResult(
                    success=False,
                    raw_file=raw_file,
                    observation_file=observation_file,
                    navigation_file=navigation_file,
                    sbas_file=sbas_file,
                    message=problem,
                    runtime_seconds=elapsed,
                    convbin_version=self._convbin_version,
                )
            )

        command = self._build_command(
            raw_file, observation_file, navigation_file, sbas_file
        )

        try:
            process = self._run_convbin(command)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            message = f"convbin timed out after {self.subprocess_timeout}s"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", raw_file.name, message, elapsed
            )
            return self._record(
                ConversionResult(
                    success=False,
                    raw_file=raw_file,
                    observation_file=observation_file,
                    navigation_file=navigation_file,
                    sbas_file=sbas_file,
                    message=message,
                    runtime_seconds=elapsed,
                    convbin_version=self._convbin_version,
                )
            )
        except OSError as exc:
            elapsed = time.monotonic() - started
            message = f"Could not execute convbin: {exc}"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", raw_file.name, message, elapsed
            )
            return self._record(
                ConversionResult(
                    success=False,
                    raw_file=raw_file,
                    observation_file=observation_file,
                    navigation_file=navigation_file,
                    sbas_file=sbas_file,
                    message=message,
                    runtime_seconds=elapsed,
                    convbin_version=self._convbin_version,
                )
            )

        elapsed = time.monotonic() - started

        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            message = f"convbin exited {process.returncode}: {detail}"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", raw_file.name, message, elapsed
            )
            self._cleanup(raw_file)
            return self._record(
                ConversionResult(
                    success=False,
                    raw_file=raw_file,
                    observation_file=observation_file,
                    navigation_file=navigation_file,
                    sbas_file=sbas_file,
                    message=message,
                    runtime_seconds=elapsed,
                    convbin_version=self._convbin_version,
                )
            )

        # Do not assume success because convbin exited zero -- verify
        # the output actually looks like RINEX.
        ok, verify_message = self._verify_output(observation_file, navigation_file)

        result = ConversionResult(
            success=ok,
            raw_file=raw_file,
            observation_file=observation_file,
            navigation_file=navigation_file,
            sbas_file=sbas_file,
            message=(
                verify_message if ok else f"Output verification failed: {verify_message}"
            ),
            runtime_seconds=elapsed,
            convbin_version=self._convbin_version,
        )

        self._cleanup(raw_file)

        if ok:
            self._logger.info("SUCCESS %s (%.2f sec)", raw_file.name, elapsed)
        else:
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", raw_file.name, result.message, elapsed
            )

        return self._record(result)

    def verify(self, result: ConversionResult) -> bool:
        """
        Independently re-verify a ConversionResult's output files
        (e.g. a caller re-checking a cached/stored result rather than
        trusting result.success as given).
        """

        ok, _ = self._verify_output(result.observation_file, result.navigation_file)
        return ok

    def status(self) -> RinexStatus:
        """Return a snapshot of processor health and running totals."""

        return RinexStatus(
            initialized=self._initialized,
            convbin_found=self._convbin_path is not None,
            files_processed=self._stats.files_processed,
            successful=self._stats.successful,
            failed=self._stats.failed,
            last_runtime=self._runtimes[-1] if self._runtimes else None,
        )

    def shutdown(self) -> None:
        """Release nothing external is held open by this class, but
        reset state for a clean re-initialize() if reused."""

        self._shutdown()

    # ==========================================================
    # Private: initialize()
    # ==========================================================

    def _locate_convbin(self) -> str:
        if self._configured_convbin_path:
            configured = Path(self._configured_convbin_path)
            if configured.is_file() and os.access(configured, os.X_OK):
                return str(configured)
            raise RinexProcessorError(
                f"station.json convbin_path is not an executable file: "
                f"{configured}"
            )

        found = shutil.which("convbin")
        if found is None:
            raise RinexProcessorError(
                "convbin not found on PATH and no convbin_path "
                "configured in station.json. Install RTKLIB (demo5 "
                "build or later, for native Unicore/UM980 support) "
                "or set convbin_path explicitly."
            )

        return found

    def _detect_convbin_version(self) -> str:
        """
        Best-effort version string: convbin run with no arguments
        prints a usage banner (including its version) to stdout/
        stderr and exits non-zero, which is expected here and not
        treated as a failure.
        """

        assert self._convbin_path is not None

        try:
            result = subprocess.run(
                [self._convbin_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"

        output = (result.stdout or "") + (result.stderr or "")
        first_line = output.strip().splitlines()[0] if output.strip() else ""

        return first_line or "unknown"

    # ==========================================================
    # Private: convert()
    # ==========================================================

    def _validate_input(self, raw_file: Path) -> str | None:
        """Return an error message if `raw_file` isn't usable, else None."""

        if not raw_file.exists():
            return f"Raw file does not exist: {raw_file}"

        if not raw_file.is_file():
            return f"Raw file is not a regular file: {raw_file}"

        if raw_file.stat().st_size == 0:
            return f"Raw file is empty: {raw_file}"

        if not os.access(raw_file, os.R_OK):
            return f"Raw file is not readable: {raw_file}"

        return None

    def _create_output_names(self, raw_file: Path) -> tuple[Path, Path, Path]:
        """
        test_20260707.um980 -> rinex/test_20260707.{obs,nav,sbs}

        Computed even when the input turns out to be invalid, so
        every ConversionResult -- success or failure -- carries the
        output paths that were (or would have been) used.
        """

        assert self._rinex_directory is not None

        stem = raw_file.stem
        observation_file = self._rinex_directory / f"{stem}.obs"
        navigation_file = self._rinex_directory / f"{stem}.nav"
        sbas_file = self._rinex_directory / f"{stem}.sbs"

        return observation_file, navigation_file, sbas_file

    @staticmethod
    def _geodetic_to_ecef(
        latitude: float, longitude: float, height: float
    ) -> tuple[float, float, float]:
        """
        Convert WGS84 geodetic coordinates (degrees, degrees, meters)
        to ECEF X/Y/Z (meters), for convbin's -hp option. Standard
        WGS84 ellipsoid conversion; not receiver- or firmware-
        specific, so nothing here needs hardware confirmation.
        """

        a = 6378137.0  # WGS84 semi-major axis, meters
        f = 1 / 298.257223563  # WGS84 flattening
        e2 = f * (2 - f)  # eccentricity squared

        lat_rad = math.radians(latitude)
        lon_rad = math.radians(longitude)

        sin_lat = math.sin(lat_rad)
        n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)

        x = (n + height) * math.cos(lat_rad) * math.cos(lon_rad)
        y = (n + height) * math.cos(lat_rad) * math.sin(lon_rad)
        z = (n * (1 - e2) + height) * sin_lat

        return x, y, z

    def _build_command(
        self,
        raw_file: Path,
        observation_file: Path,
        navigation_file: Path,
        sbas_file: Path,
    ) -> list[str]:
        """
        Isolated in one method so the exact convbin flags can change
        (e.g. adding .sp3/.clk outputs) without touching convert()'s
        control flow.

        Embeds station metadata into the RINEX header via convbin's
        standard -h* options, read from `self.cfg` via getattr() with
        safe defaults -- so a minimal duck-typed cfg (e.g. in tests)
        that doesn't define every one of these still works, just
        with blank/zero header fields, exactly as before this method
        started populating them.
        """

        assert self._convbin_path is not None
        assert self.cfg is not None

        station_id = getattr(self.cfg, "station_id", "") or ""
        observer = getattr(self.cfg, "observer", "") or ""
        agency = getattr(self.cfg, "agency", "") or ""
        receiver_model = getattr(self.cfg, "receiver_model", "") or ""
        receiver_firmware = getattr(self.cfg, "receiver_firmware", "") or ""
        latitude = getattr(self.cfg, "latitude", 0.0) or 0.0
        longitude = getattr(self.cfg, "longitude", 0.0) or 0.0
        height = getattr(self.cfg, "height", 0.0) or 0.0

        station = getattr(self.cfg, "station", {}) or {}
        marker_name = station.get("marker_name", "")
        marker_number = station.get("marker_number", "")
        antenna = station.get("antenna", {}) or {}
        antenna_serial = antenna.get("serial", "")
        antenna_model = antenna.get("model", "")
        antenna_height = antenna.get("height", 0.0)
        antenna_east = antenna.get("east_offset", 0.0)
        antenna_north = antenna.get("north_offset", 0.0)

        x, y, z = self._geodetic_to_ecef(latitude, longitude, height)

        return [
            self._convbin_path,
            "-r", "nov",  # Unicore/UM980 raw format (NovAtel-compatible)
            "-v", self._rinex_version,
            "-os",  # include SNR (signal strength) observables --
                    # convbin omits these by default with no warning;
                    # confirmed via real hardware + gnssrefl testing
                    # that RINEX files without this flag have zero
                    # SNR-type (S1/S2-equivalent) observation codes,
                    # which gnssrefl (and GNSS-IR generally) requires
                    # and cannot function without.
            "-hc", station_id,
            "-hm", marker_name,
            "-hn", marker_number,
            "-ho", f"{observer}/{agency}",
            "-hr", f"/{receiver_model}/{receiver_firmware}",
            "-ha", f"{antenna_serial}/{antenna_model}",
            "-hp", f"{x:.4f}/{y:.4f}/{z:.4f}",
            "-hd", f"{antenna_height}/{antenna_east}/{antenna_north}",
            "-o", str(observation_file),
            "-n", str(navigation_file),
            "-s", str(sbas_file),
            str(raw_file),
        ]

    def _run_convbin(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self.subprocess_timeout,
        )

    def _verify_output(
        self, observation_file: Path, navigation_file: Path
    ) -> tuple[bool, str]:
        """
        Three verification levels, in order, on both the observation
        and navigation files: (1) exists, (2) non-zero size and
        readable, (3) begins with a valid RINEX header. The first
        failure found short-circuits with a specific reason.
        """

        for label, path in (
            ("observation", observation_file),
            ("navigation", navigation_file),
        ):
            if not path.exists():
                return False, f"{label} file was not created: {path}"

            if not os.access(path, os.R_OK):
                return False, f"{label} file is not readable: {path}"

            if path.stat().st_size == 0:
                return False, f"{label} file is empty: {path}"

            try:
                with open(path, "r", errors="ignore") as handle:
                    header = handle.readline()
            except OSError as exc:
                return False, f"Could not read {label} file: {exc}"

            if _RINEX_HEADER_MARKER not in header:
                return False, (
                    f"{label} file is missing a valid RINEX header "
                    f"(no {_RINEX_HEADER_MARKER!r}): {path}"
                )

        return True, "verified"

    def _collect_statistics(self, result: ConversionResult) -> None:
        self._stats.files_processed += 1

        if result.success:
            self._stats.successful += 1
        else:
            self._stats.failed += 1

        self._runtimes.append(result.runtime_seconds)
        self._stats.average_runtime = sum(self._runtimes) / len(self._runtimes)
        self._stats.last_conversion = datetime.now(timezone.utc)

    def _cleanup(self, raw_file: Path) -> None:
        """
        Remove convbin's intermediate trace file, if one was left
        behind (produced when convbin's own tracing is enabled;
        harmless either way, but not something we want accumulating
        in the RINEX output directory).
        """

        if self._rinex_directory is None:
            return

        trace_file = self._rinex_directory / f"{raw_file.stem}.trace"

        if trace_file.exists():
            try:
                trace_file.unlink()
            except OSError as exc:
                self._logger.debug(
                    "Could not remove trace file %s: %s", trace_file, exc
                )

    def _record(self, result: ConversionResult) -> ConversionResult:
        """Run _collect_statistics() and return the same result, for one-line call sites."""

        self._collect_statistics(result)
        return result

    def _make_result(
        self,
        raw_file: Path,
        success: bool,
        message: str,
        runtime_seconds: float,
    ) -> ConversionResult:
        """
        Build a ConversionResult for failures that happen before
        output filenames are even meaningful (e.g. not initialized).
        """

        empty = Path("")

        return ConversionResult(
            success=success,
            raw_file=raw_file,
            observation_file=empty,
            navigation_file=empty,
            sbas_file=empty,
            message=message,
            runtime_seconds=runtime_seconds,
            convbin_version=self._convbin_version,
        )

    # ==========================================================
    # Private: shutdown()
    # ==========================================================

    def _shutdown(self) -> None:
        self._logger.info("Shutting down RinexProcessor")
        self._initialized = False

    # ==========================================================
    # Future expansion (placeholders)
    # ==========================================================

    def convert_directory(self, directory: Path) -> list[ConversionResult]:
        """Placeholder: convert() every raw file found in `directory`."""
        pass

    def convert_all(self) -> list[ConversionResult]:
        """Placeholder: convert() every pending raw file (source TBD -- pipeline.py's job)."""
        pass

    def delete_failed(self) -> None:
        """Placeholder: remove partial/invalid output left behind by failed conversions."""
        pass

    def verify_all(self) -> list[bool]:
        """Placeholder: verify() every previously produced RINEX file."""
        pass

    def cleanup_old_files(self) -> None:
        """Placeholder: prune old RINEX output per a retention policy."""
        pass
