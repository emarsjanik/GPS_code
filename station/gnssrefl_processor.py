"""
gnssrefl_processor.py

USGS GNSS Reference Station
Prototype 1.0

Single responsibility: run gnssrefl's GNSS-IR pipeline (rinex2snr ->
gnssir) against one day's RINEX observation file, and report the
outcome. Mirrors rinex_processor.py's proven pattern: a class with
initialize()/process()/verify()/status()/shutdown(), duck-typed
config access, and a process() that never raises for an ordinary
processing failure.

Confirmed against gnssrefl's own documentation (gnssrefl.readthedocs.io)
------------------------------------------------------------------------
    * Three environment variables are required: REFL_CODE, ORBITS,
      EXE. They must be set before gnssrefl is imported.
    * gnssrefl.gnssir_input.make_gnssir_input(station, lat, lon,
      height, ...) -- one-time per-station analysis-strategy setup,
      writes the json gnssir reads. Uses the 4-character station
      code, confirmed against real hardware (not the 9-character
      RINEX-3 code -- see below).
    * gnssrefl.rinex2snr_cl.rinex2snr(station, year, doy, ...,
      nolook=True, ...) -- RINEX -> SNR conversion. `nolook=True` is
      required to use a locally-supplied RINEX file instead of
      fetching one from a remote archive.
    * gnssrefl.gnssir_cl.gnssir(station, year, doy, ...) -- the main
      reflector-height estimation driver. Uses the 4-character
      station code too.
    * File/directory conventions (confirmed against real output,
      gnssrefl 4.1.5):
        $REFL_CODE/input/<station>/<station>.json   analysis strategy
        $REFL_CODE/<year>/rinex/<4-char station>/   staged input RINEX
        $REFL_CODE/<year>/snr/<4-char station>/     SNR files
        $REFL_CODE/<year>/results/<4-char station>/<doy>.txt   RH results
    * rinex_processor.py's convbin output is always RINEX 3 (never
      RINEX 2.11) -- and rinex2snr() decides which format to expect
      purely by the *length* of the `station` argument passed to it:
      4 characters means "assume RINEX 2.11", 9 characters means
      "assume RINEX 3". This module therefore maintains *two*
      station codes: the 4-character one (used everywhere else) and
      a 9-character one built from it plus a configurable monument
      number and country code, used only as rinex2snr()'s `station`
      argument and in the staged filename. Confirmed the hard way:
      passing the 4-character code against real RINEX 3 data
      produces no error at all, just a file with zero usable SNR
      observables.
    * The RINEX 3 long filename convention rinex2snr() searches for
      is "{STATION9}_R_{YYYY}{DOY:03d}0000_01D_{RATE:02d}S_MO.rnx"
      (uppercase) -- the time segment is always "0000" (midnight),
      regardless of the observation file's real first-epoch time,
      since rinex2snr(year, doy) always looks for a nominal
      whole-day file for the requested day.
    * Confirmed, real upstream bugs in gnssrefl 4.1.5, both worked
      around here rather than requiring manual intervention every
      time:
        - gps.checkEGM() calls `subprocess.call('mkdir', localdir)`
          -- passing a directory path as subprocess.call()'s second
          positional argument, which is actually `bufsize`, raising
          TypeError the first time $REFL_CODE/Files doesn't already
          exist. Worked around by creating that directory ourselves
          in initialize(), so that code path never executes.
        - rinex2snr()'s own file-discovery logic checks the current
          working directory for a same-named file *before* checking
          the properly staged directory, and silently reuses
          whatever it finds there forever after -- never refreshing
          it, even if a newer, correct version has since been staged
          properly. Worked around by removing any same-named file
          from the current working directory immediately before
          staging (_stage_rinex_file()).

What this does, and what it deliberately does NOT do
--------------------------------------------------------
    * Reflector height and a peak-to-noise-based quality score ARE
      parsed from the results .txt file (GnssIrResult.reflector_height,
      quality_score), as the mean across all rows that parse cleanly.
      This is confirmed against a real results file's own
      self-documenting header comment (gnssrefl 4.1.5):

          %(1)  (2)   (3) (4)  (5)     (6)   (7)    (8)    (9)  ...
          % year, doy, RH, sat,UTCtime, Azim, Amp,  eminO, emaxO ...

      Column (3) is RH (reflector height, meters); column (14) is
      PkNoise (peak-to-noise ratio). Only reflector_height/
      quality_score wait on this real-data confirmation --
      num_tracks (a count of data rows, independent of column
      meaning) has always been available.
    * Soil moisture / snow depth. Those come from separate, later
      gnssrefl modules (vwc, snow-specific tools) that operate on an
      *accumulated, multi-day* reflector-height history, not a
      single day's gnssir run. GnssIrResult.soil_moisture/snow_depth
      are always None here; that's not a placeholder for a bug, it's
      genuinely out of scope for a single day's processing.
    * Fetching orbits over the internet by default is gnssrefl's own
      behavior, not something this module adds. Leave "gnssrefl_orbit_source"
      unset (the default, and the confirmed-correct choice for this
      station) to use gnssrefl's own default multi-GNSS SP3 orbit
      fetch from CDDIS.

      IMPORTANT, confirmed against real hardware: "gnssrefl_orbit_source":
      "nav" (GPS-only broadcast ephemeris) silently produces GPS-only
      results, even with allfreq=True set on make_gnssir_input().
      Without real orbit data for GLONASS/Galileo/BeiDou satellites,
      rinex2snr() cannot include their observations in the SNR file at
      all, regardless of what SNR data actually exists in the RINEX
      file -- confirmed directly: switching from "nav" to the default
      multi-GNSS orbit fetch took the same 5.5-hour recording from 20
      tracks (GPS only) to 48 tracks (GPS + GLONASS + Galileo +
      BeiDou), more than doubling data density with no change to the
      underlying recording at all.

      The earlier concern that motivated "nav" in the first place --
      the default multi-GNSS fetch failing with "orbit file does not
      exist" -- turned out to be a same-day publication-timing issue,
      not a genuine CDDIS/EarthData Login authentication requirement:
      reprocessing the exact same day's data a few days later, once
      the rapid multi-GNSS orbit product had actually been published,
      succeeded with no login or credentials involved. "nav" remains
      available as a genuine fallback specifically for same-day/
      real-time processing before that day's multi-GNSS orbit product
      has been published yet, or for a station with no reliable
      internet at all -- not as a general-purpose default.

This module knows nothing about SQLite, the receiver, station.py, or
pipeline decisions -- same boundaries as rinex_processor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
import logging
import os
import shutil
import time

from config import Config


# ----------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------

class GnssIrProcessorError(Exception):
    """
    Raised only by initialize(), for problems that mean the
    processor cannot run at all (gnssrefl not importable, required
    directories cannot be created, the per-station analysis strategy
    cannot be set up). process() never raises this or anything else
    -- see the module docstring.
    """


# ----------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------

@dataclass
class GnssIrResult:
    """The outcome of one process() call."""

    success: bool
    observation_file: Path
    day: date
    station_code: str
    reflector_height: float | None
    soil_moisture: float | None
    snow_depth: float | None
    quality_score: float | None
    num_tracks: int
    output_directory: Path
    message: str
    runtime_seconds: float
    gnssrefl_version: str


@dataclass
class GnssIrStatistics:
    """Running totals across every process() call this processor has made."""

    files_processed: int = 0
    successful: int = 0
    failed: int = 0
    average_runtime: float = 0.0
    last_processing: datetime | None = None


@dataclass
class GnssIrStatus:
    """Returned by status(): a snapshot for pipeline.py/dashboards."""

    initialized: bool = False
    gnssrefl_importable: bool = False
    gnssrefl_version: str = "unknown"
    station_code: str = ""
    files_processed: int = 0
    successful: int = 0
    failed: int = 0
    last_runtime: float | None = None


# ----------------------------------------------------------------
# GnssIrProcessor
# ----------------------------------------------------------------

@dataclass
class GnssIrProcessor:
    """
    Runs gnssrefl's rinex2snr -> gnssir pipeline for one day's RINEX
    observation file.

    Parameters
    ----------
    cfg:
        A config.Config instance (or any object exposing `latitude`,
        `longitude`, `height`, `station_id`, `products_dir`, and a
        `station` dict). If not given, initialize() builds one
        itself.

    Typical usage:

        processor = GnssIrProcessor()
        processor.initialize()
        result = processor.process(observation_file, day=date(2026, 7, 8))
        if result.success:
            ...
        processor.shutdown()
    """

    cfg: Config | None = None

    _initialized: bool = field(default=False, init=False, repr=False)
    _gnssrefl_importable: bool = field(default=False, init=False, repr=False)
    _gnssrefl_version: str = field(default="unknown", init=False, repr=False)
    _refl_code: Path | None = field(default=None, init=False, repr=False)
    _orbits: Path | None = field(default=None, init=False, repr=False)
    _exe: Path | None = field(default=None, init=False, repr=False)
    _station_code: str = field(default="", init=False, repr=False)
    _station_code_9ch: str = field(default="", init=False, repr=False)
    _sample_rate: int = field(default=1, init=False, repr=False)
    _orbit_source: str | None = field(default=None, init=False, repr=False)
    _stats: GnssIrStatistics = field(
        default_factory=GnssIrStatistics, init=False, repr=False
    )
    _runtimes: list[float] = field(default_factory=list, init=False, repr=False)
    _logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("station.gnssir"),
        init=False,
        repr=False,
    )

    # ==========================================================
    # Public API
    # ==========================================================

    def initialize(self) -> str:
        """
        Set up the three required environment variables (REFL_CODE,
        ORBITS, EXE), verify gnssrefl is importable, and set up this
        station's one-time GNSS-IR analysis strategy.

        Returns "READY" on success.

        Raises
        ------
        GnssIrProcessorError
            If gnssrefl cannot be imported, a required directory
            cannot be created, or the per-station analysis strategy
            cannot be set up.
        """

        if self.cfg is None:
            self.cfg = Config()

        self._refl_code, self._orbits, self._exe = self._resolve_directories()

        # $REFL_CODE/Files must be created proactively: confirmed
        # against a real install (gnssrefl 4.1.5) that its own EGM96
        # download helper (gps.checkEGM()) calls
        # `subprocess.call('mkdir', localdir)` -- a genuine upstream
        # bug (subprocess.call()'s second positional argument is
        # bufsize, not a second command-line argument) that raises
        # TypeError if that directory doesn't already exist. Creating
        # it ourselves means that buggy code path never executes.
        for directory in (
            self._refl_code,
            self._orbits,
            self._exe,
            self._refl_code / "Files",
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise GnssIrProcessorError(
                    f"Could not create gnssrefl directory {directory}: {exc}"
                ) from exc

        # Must be set before gnssrefl is imported -- several of its
        # modules read these at import time, not lazily.
        os.environ["REFL_CODE"] = str(self._refl_code)
        os.environ["ORBITS"] = str(self._orbits)
        os.environ["EXE"] = str(self._exe)

        station_section = getattr(self.cfg, "station", {}) or {}

        self._station_code = self._resolve_station_code(station_section)
        self._station_code_9ch = self._resolve_station_code_9ch(
            station_section, self._station_code
        )
        self._sample_rate = int(station_section.get("gnssrefl_sample_rate", 1))
        self._orbit_source = station_section.get("gnssrefl_orbit_source") or None

        self._verify_gnssrefl_importable()
        self._setup_station_strategy()

        self._initialized = True

        self._logger.info(
            "GnssIrProcessor initialized: station=%s (%s) REFL_CODE=%s "
            "gnssrefl=%s orbit_source=%s sample_rate=%ds",
            self._station_code,
            self._station_code_9ch,
            self._refl_code,
            self._gnssrefl_version,
            self._orbit_source or "(gnssrefl default)",
            self._sample_rate,
        )

        return "READY"

    def process(self, observation_file: Path, day: date) -> GnssIrResult:
        """
        Run rinex2snr then gnssir against `observation_file` for
        `day`. Never raises: every failure mode (not initialized,
        missing input, rinex2snr failing, gnssir failing) comes back
        as a GnssIrResult with success=False and a clear message.

        Parameters
        ----------
        observation_file:
            A RINEX observation file, as produced by
            rinex_processor.py.
        day:
            The UTC calendar day this observation file covers.
            gnssrefl's API is year/day-of-year based, not filename-
            based, so this is required explicitly rather than parsed
            out of `observation_file`'s name.
        """

        observation_file = Path(observation_file)

        self._logger.info("START %s (day=%s)", observation_file.name, day)

        started = time.monotonic()

        if not self._initialized:
            return self._record(
                self._failure_result(
                    observation_file,
                    day,
                    "GnssIrProcessor.initialize() was not called",
                    time.monotonic() - started,
                )
            )

        problem = self._validate_input(observation_file)

        if problem:
            elapsed = time.monotonic() - started
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", observation_file.name, problem, elapsed
            )
            return self._record(
                self._failure_result(observation_file, day, problem, elapsed)
            )

        try:
            self._stage_rinex_file(observation_file, day)
        except OSError as exc:
            elapsed = time.monotonic() - started
            message = f"Could not stage RINEX file for gnssrefl: {exc}"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", observation_file.name, message, elapsed
            )
            return self._record(
                self._failure_result(observation_file, day, message, elapsed)
            )

        year = day.year
        doy = day.timetuple().tm_yday

        try:
            self._run_rinex2snr(year, doy)
        except (Exception, SystemExit) as exc:
            # Catches SystemExit too: gnssrefl is research-grade code
            # that may call sys.exit() internally on some error
            # paths rather than raising cleanly. "Never crash" means
            # never crash even if the external library tries to.
            elapsed = time.monotonic() - started
            message = f"rinex2snr failed: {exc}"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", observation_file.name, message, elapsed
            )
            return self._record(
                self._failure_result(observation_file, day, message, elapsed)
            )

        try:
            self._run_gnssir(year, doy)
        except (Exception, SystemExit) as exc:
            elapsed = time.monotonic() - started
            message = f"gnssir failed: {exc}"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", observation_file.name, message, elapsed
            )
            return self._record(
                self._failure_result(observation_file, day, message, elapsed)
            )

        elapsed = time.monotonic() - started

        ok, num_tracks, notes, reflector_height, quality_score = self._read_results(
            year, doy
        )

        output_directory = self._refl_code / str(year) / "results" / self._station_code

        result = GnssIrResult(
            success=ok,
            observation_file=observation_file,
            day=day,
            station_code=self._station_code,
            reflector_height=reflector_height,
            soil_moisture=None,  # see module docstring -- genuinely out of scope
            snow_depth=None,  # see module docstring -- genuinely out of scope
            quality_score=quality_score,
            num_tracks=num_tracks,
            output_directory=output_directory,
            message=notes,
            runtime_seconds=elapsed,
            gnssrefl_version=self._gnssrefl_version,
        )

        if ok:
            self._logger.info(
                "SUCCESS %s (%.2f sec, %d track(s))",
                observation_file.name,
                elapsed,
                num_tracks,
            )
        else:
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", observation_file.name, notes, elapsed
            )

        return self._record(result)

    def verify(self, result: GnssIrResult) -> bool:
        """
        Independently re-verify a GnssIrResult's output (the results
        file still exists and is non-empty), rather than trusting
        result.success as given.
        """

        results_file = self._results_file_path(result.day)

        return results_file.exists() and results_file.stat().st_size > 0

    def status(self) -> GnssIrStatus:
        """Return a snapshot of processor health and running totals."""

        return GnssIrStatus(
            initialized=self._initialized,
            gnssrefl_importable=self._gnssrefl_importable,
            gnssrefl_version=self._gnssrefl_version,
            station_code=self._station_code,
            files_processed=self._stats.files_processed,
            successful=self._stats.successful,
            failed=self._stats.failed,
            last_runtime=self._runtimes[-1] if self._runtimes else None,
        )

    def shutdown(self) -> None:
        """Reset state for a clean re-initialize() if reused."""

        self._logger.info("Shutting down GnssIrProcessor")
        self._initialized = False

    # ==========================================================
    # Private: initialize()
    # ==========================================================

    def _resolve_directories(self) -> tuple[Path, Path, Path]:
        """
        Compute REFL_CODE/ORBITS/EXE. REFL_CODE defaults to a
        "refl_code" subdirectory of the station's products
        directory, unless station.json's "gnssrefl_refl_code"
        overrides it. ORBITS and EXE are always subdirectories of
        REFL_CODE, for simplicity (gnssrefl doesn't require any
        particular relationship between the three; this just keeps
        everything gnssrefl-related in one place under products/).
        """

        station_section = getattr(self.cfg, "station", {}) or {}
        configured = station_section.get("gnssrefl_refl_code", "")

        if configured:
            refl_code = Path(configured)
        else:
            products_dir = getattr(self.cfg, "products_dir", None)
            base = Path(products_dir) if products_dir else Path.cwd() / "products"
            refl_code = base / "refl_code"

        return refl_code, refl_code / "orbits", refl_code / "exe"

    def _resolve_station_code(self, station_section: dict) -> str:
        """
        gnssrefl's RINEX 2.11 convention requires an exactly
        4-character station code, which won't generally match
        station_id (e.g. "USGS001"). Prefer an explicit
        "gnssrefl_station_code" in station.json; otherwise derive a
        best-effort default from station_id, clearly documented as a
        fallback rather than a considered choice.
        """

        configured = station_section.get("gnssrefl_station_code", "")

        if configured:
            code = str(configured)
        else:
            station_id = getattr(self.cfg, "station_id", "") or ""
            code = station_id[:4] if station_id else ""
            if len(code) < 4:
                self._logger.warning(
                    "No gnssrefl_station_code configured and station_id "
                    "%r is too short to derive one from; falling back "
                    "to a generic placeholder. Set "
                    "\"gnssrefl_station_code\" in station.json (exactly "
                    "4 characters) for a real station identifier.",
                    station_id,
                )
                code = (code + "gps1")[:4]

        return code.lower()

    def _resolve_station_code_9ch(
        self, station_section: dict, station_code_4ch: str
    ) -> str:
        """
        gnssrefl's rinex2snr() decides whether to parse a file as
        RINEX 2.11 or RINEX 3 purely by the *length* of the `station`
        argument passed to it -- 4 characters means "assume RINEX
        2.11", 9 characters means "assume RINEX 3" (confirmed against
        real hardware: rinex_processor.py's convbin output is always
        RINEX 3, and passing the 4-character code caused rinex2snr()
        to mis-parse it as RINEX 2.11, silently producing a file with
        no usable SNR data despite -os being set correctly).

        This 9-character code is used *only* as rinex2snr()'s
        `station` argument and in the staged filename itself
        (_stage_rinex_file()) -- make_gnssir_input() and gnssir()
        both still use the 4-character code, confirmed against real
        hardware to be what they actually expect.

        Built from station_code_4ch + a 2-digit monument number +
        a 3-character country code, per the RINEX 3 long filename
        convention (e.g. "wh01" + "00" + "usa" -> "wh0100usa").
        Both the monument number and country code are configurable
        via station.json ("gnssrefl_monument_number",
        "gnssrefl_country_code"); "00"/"usa" are used as defaults
        otherwise, since this project is explicitly a USGS station,
        but are not assumed to be correct for every deployment.
        """

        monument = str(station_section.get("gnssrefl_monument_number", "00"))
        country = str(station_section.get("gnssrefl_country_code", "usa"))

        code = f"{station_code_4ch}{monument}{country}".lower()

        if len(code) != 9:
            self._logger.warning(
                "Constructed 9-character RINEX 3 station code %r is not "
                "exactly 9 characters (station=%r, monument=%r, "
                "country=%r); padding/truncating, but this should be "
                "corrected via station.json's gnssrefl_monument_number "
                "and gnssrefl_country_code.",
                code,
                station_code_4ch,
                monument,
                country,
            )
            code = (code + "00usa")[:9]

        return code

    def _verify_gnssrefl_importable(self) -> None:
        try:
            import gnssrefl
            from gnssrefl import gnssir_cl, gnssir_input, rinex2snr_cl  # noqa: F401
        except ImportError as exc:
            raise GnssIrProcessorError(
                f"gnssrefl is not importable (pip install gnssrefl?): {exc}"
            ) from exc

        self._gnssrefl_importable = True
        self._gnssrefl_version = self._detect_gnssrefl_version(gnssrefl)

    @staticmethod
    def _detect_gnssrefl_version(gnssrefl_module) -> str:
        """
        Confirmed against a real install (gnssrefl 4.1.5): the
        package does not expose `gnssrefl.__version__` at all (an
        AttributeError, not just an empty string) -- so this reads
        installed package metadata first, which is the reliable
        source of truth regardless of what the package itself does
        or doesn't define. `__version__` is kept as a fallback purely
        in case some other installed version does define it.
        """

        try:
            import importlib.metadata

            return importlib.metadata.version("gnssrefl")
        except importlib.metadata.PackageNotFoundError:
            pass
        except Exception:
            pass

        return getattr(gnssrefl_module, "__version__", "unknown")

    def _setup_station_strategy(self) -> None:
        """
        One-time (per station, re-run harmlessly on every
        initialize()) setup of gnssrefl's analysis strategy: which
        elevation angles, reflector height range, and frequencies to
        use. Always uses this station's real coordinates from
        config, never gnssrefl's online coordinate lookup.

        allfreq=True is confirmed necessary here, not optional:
        make_gnssir_input()'s own default is allfreq=False, which
        restricts analysis to GPS frequencies only. Confirmed against
        a real results file that every one of 20 real retrievals came
        from GPS alone, despite the underlying RINEX data containing
        real, confirmed SNR observables from GLONASS, Galileo, QZSS,
        and BeiDou as well -- meaning most of the station's actual
        captured data was silently going unused for GNSS-IR analysis.

        Fine-tuning parameters (elevation mask, reflector height
        range, azimuth mask, QC thresholds, orthometric height
        reference, refraction model, maximum arc length, arc
        elevation-span tolerance) are real
        make_gnssir_input() inputs that were never previously exposed
        here at all -- confirmed via gnssrefl's own documentation. Each is only
        passed through if explicitly set in station.json; left unset,
        gnssrefl's own internal defaults apply exactly as before this
        change, so upgrading never silently alters existing behavior.
        This matters most for a *different* future site, where this
        station's implicit defaults (tuned for Woods Hole) could be
        wrong -- e.g. a site with a partially obstructed view needs
        its own azimuth mask, or a site with a much taller antenna
        needs a wider reflector height search range.
        """

        from gnssrefl.gnssir_input import make_gnssir_input

        latitude = getattr(self.cfg, "latitude", 0.0) or 0.0
        longitude = getattr(self.cfg, "longitude", 0.0) or 0.0
        height = getattr(self.cfg, "height", 0.0) or 0.0

        station_section = getattr(self.cfg, "station", {}) or {}
        all_frequencies = bool(
            station_section.get("gnssrefl_all_frequencies", True)
        )

        kwargs = dict(
            station=self._station_code,
            lat=latitude,
            lon=longitude,
            height=height,
            allfreq=all_frequencies,
        )

        # Elevation angle mask (gnssrefl's own params: e1, e2)
        e1 = station_section.get("gnssrefl_elevation_min")
        e2 = station_section.get("gnssrefl_elevation_max")
        if e1 is not None:
            kwargs["e1"] = float(e1)
        if e2 is not None:
            kwargs["e2"] = float(e2)

        # Reflector height search range (gnssrefl's own params: h1, h2)
        h1 = station_section.get("gnssrefl_reflector_height_min")
        h2 = station_section.get("gnssrefl_reflector_height_max")
        if h1 is not None:
            kwargs["h1"] = float(h1)
        if h2 is not None:
            kwargs["h2"] = float(h2)

        # Azimuth mask (gnssrefl's own param: azlist2 -- a list of
        # region boundaries, e.g. [0, 360] or [0, 150, 180, 360])
        azimuth_regions = station_section.get("gnssrefl_azimuth_regions")
        if azimuth_regions:
            kwargs["azlist2"] = [float(a) for a in azimuth_regions]

        # Quality-control thresholds (gnssrefl's own params:
        # peak2noise, ampl)
        peak2noise = station_section.get("gnssrefl_peak2noise")
        amplitude_min = station_section.get("gnssrefl_amplitude_min")
        if peak2noise is not None:
            kwargs["peak2noise"] = float(peak2noise)
        if amplitude_min is not None:
            kwargs["ampl"] = float(amplitude_min)

        # Orthometric height reference (gnssrefl's own param: Hortho)
        # -- needed to report real, absolute water level rather than
        # just relative reflector height. Never set for this station;
        # a real, meaningful value for a future site.
        orthometric_height = station_section.get("gnssrefl_orthometric_height")
        if orthometric_height is not None:
            kwargs["Hortho"] = float(orthometric_height)

        # Refraction model (gnssrefl's own param: refraction) -- 1 is
        # the Bennett correction (gnssrefl's own default); matters
        # more for very tall or very short sites.
        refraction_model = station_section.get("gnssrefl_refraction_model")
        if refraction_model is not None:
            kwargs["refraction"] = int(refraction_model)

        # Maximum arc length in minutes (gnssrefl's own param: delTmax)
        # -- library default is 75 minutes, documented as too long for
        # sites with a fast tidal rate of change: a single satellite
        # arc's reflector height estimate gets blurred across
        # whatever real water-level change happens during that whole
        # window. Only passed through if explicitly configured, so
        # gnssrefl's own default applies otherwise.
        max_arc_minutes = station_section.get("gnssrefl_max_arc_minutes")
        if max_arc_minutes is not None:
            kwargs["delTmax"] = float(max_arc_minutes)

        # Arc elevation-span quality control (gnssrefl's own param:
        # ediff) -- requires every arc to span at least
        # (e1+ediff) to (e2-ediff) degrees. Library default is 2,
        # documented as too strict for a narrow elevation mask like
        # ours (5-15 degrees is the documentation's own worked
        # example for "you might want to make that a little
        # stricter... an ediff of 1"). Confirmed directly against our
        # own real pipeline output that this is actively rejecting
        # real arcs at the default value. Only passed through if
        # explicitly configured, so gnssrefl's own default applies
        # otherwise.
        elevation_span_tolerance = station_section.get("gnssrefl_elevation_span_tolerance")
        if elevation_span_tolerance is not None:
            kwargs["ediff"] = float(elevation_span_tolerance)

        try:
            make_gnssir_input(**kwargs)
        except (Exception, SystemExit) as exc:
            raise GnssIrProcessorError(
                f"Could not set up gnssrefl analysis strategy for "
                f"station {self._station_code!r}: {exc}"
            ) from exc

    # ==========================================================
    # Private: process()
    # ==========================================================

    def _validate_input(self, observation_file: Path) -> str | None:
        """Return an error message if `observation_file` isn't usable, else None."""

        if not observation_file.exists():
            return f"Observation file does not exist: {observation_file}"

        if not observation_file.is_file():
            return f"Observation file is not a regular file: {observation_file}"

        if observation_file.stat().st_size == 0:
            return f"Observation file is empty: {observation_file}"

        return None

    def _stage_rinex_file(self, observation_file: Path, day: date) -> Path:
        """
        Copy `observation_file` into gnssrefl's expected RINEX 3 long
        filename convention and directory, so
        rinex2snr(nolook=True) can find it. A copy, not a move: the
        original stays under rinex_processor.py's own output
        directory.

        Confirmed against real hardware:

        * The filename convention is
          "{STATION9}_R_{YYYY}{DDD}0000_01D_{RATE:02d}S_MO.rnx"
          (uppercase). The time segment is always "0000" (midnight)
          regardless of the observation file's actual first-epoch
          time -- rinex2snr(year, doy) always looks for a nominal
          whole-day filename for the requested day, not one matching
          the data's real, possibly-partial time span.
        * Despite the filename using the 9-character station code,
          the staging *directory* uses the plain 4-character code
          ($REFL_CODE/<year>/rinex/<4-char station>/) -- gnssrefl
          internally reduces to the 4-character name for directory
          lookups even when told to expect RINEX 3.
        * gnssrefl's own file-discovery logic (confirmed by reading
          its source, gnssrefl/rinex2snr.py) checks the *current
          working directory* for a file with this exact bare name
          FIRST, and -- critically -- only looks in the properly
          staged directory (copying it into the working directory
          itself) if no same-named file is already sitting there.
          A stale file left over from an earlier run in the working
          directory is silently reused forever after, never
          refreshed, with no error or warning. To make this
          impossible to hit, any same-named file in the current
          working directory is removed before staging.
        """

        assert self._refl_code is not None

        year = day.year
        doy = day.timetuple().tm_yday

        staged_name = (
            f"{self._station_code_9ch.upper()}_R_{year}{doy:03d}0000"
            f"_01D_{self._sample_rate:02d}S_MO.rnx"
        )

        # Defend against gnssrefl's confirmed stale-file-reuse bug:
        # remove any same-named file sitting in the current working
        # directory before staging, so it can never be silently
        # reused instead of the file we're about to stage.
        stale_cwd_file = Path.cwd() / staged_name
        if stale_cwd_file.exists():
            self._logger.warning(
                "Removing stale file from the current working "
                "directory that would otherwise be silently reused "
                "by gnssrefl instead of today's staged file: %s",
                stale_cwd_file,
            )
            stale_cwd_file.unlink()

        staged_dir = self._refl_code / str(year) / "rinex" / self._station_code
        staged_dir.mkdir(parents=True, exist_ok=True)

        staged_path = staged_dir / staged_name
        shutil.copy2(observation_file, staged_path)

        return staged_path

    def _run_rinex2snr(self, year: int, doy: int) -> None:
        from gnssrefl.rinex2snr_cl import rinex2snr

        rinex2snr(
            station=self._station_code_9ch,
            year=year,
            doy=doy,
            nolook=True,
            overwrite=True,
            orb=self._orbit_source,
            samplerate=self._sample_rate,
            quiet=True,
        )

    def _run_gnssir(self, year: int, doy: int) -> None:
        from gnssrefl.gnssir_cl import gnssir

        gnssir(
            station=self._station_code,
            year=year,
            doy=doy,
            nooverwrite=False,
            screenstats=False,
        )

    def _results_file_path(self, day: date) -> Path:
        assert self._refl_code is not None

        doy = day.timetuple().tm_yday

        return (
            self._refl_code
            / str(day.year)
            / "results"
            / self._station_code
            / f"{doy:03d}.txt"
        )

    # Confirmed directly against a real results file's own header
    # comment (gnssrefl 4.1.5): "%(1) (2) (3) (4) (5) (6) (7) (8)
    # (9) (10) (11) (12) (13) (14) (15) (16) (17)" / "% year, doy,
    # RH, sat, UTCtime, Azim, Amp, eminO, emaxO, NumbOf, freq, rise,
    # EdotF, PkNoise, DelT, MJD, refr". 0-indexed column positions:
    _RH_COLUMN = 2  # column (3), reflector height, meters
    _PK_NOISE_COLUMN = 13  # column (14), peak-to-noise ratio

    def _read_results(
        self, year: int, doy: int
    ) -> tuple[bool, int, str, float | None, float | None]:
        """
        Parse the results file: confirm it exists and is non-empty,
        count its data rows, and compute mean reflector height (RH)
        and mean peak-to-noise ratio (PkNoise) across all rows that
        parse cleanly. A row that doesn't parse (wrong number of
        columns, non-numeric RH) is skipped rather than failing the
        whole result -- gnssrefl's own output format, not something
        this module should be strict about beyond what it needs.

        Returns (success, num_tracks, message, reflector_height,
        quality_score). reflector_height/quality_score are None if
        no row could be parsed, even if num_tracks > 0 (a distinct,
        rarer failure mode from "no rows exist at all").
        """

        results_file = (
            self._refl_code
            / str(year)
            / "results"
            / self._station_code
            / f"{doy:03d}.txt"
        )

        if not results_file.exists():
            return False, 0, f"No results file produced: {results_file}", None, None

        if results_file.stat().st_size == 0:
            return False, 0, f"Results file is empty: {results_file}", None, None

        try:
            lines = results_file.read_text().splitlines()
        except OSError as exc:
            return (
                False,
                0,
                f"Could not read results file {results_file}: {exc}",
                None,
                None,
            )

        data_rows = [
            line for line in lines if line.strip() and not line.strip().startswith("%")
        ]

        if not data_rows:
            return False, 0, f"Results file has no data rows: {results_file}", None, None

        reflector_heights: list[float] = []
        peak_to_noise_values: list[float] = []

        for row in data_rows:
            columns = row.split()

            try:
                reflector_heights.append(float(columns[self._RH_COLUMN]))
            except (IndexError, ValueError):
                continue

            try:
                peak_to_noise_values.append(float(columns[self._PK_NOISE_COLUMN]))
            except (IndexError, ValueError):
                pass

        if not reflector_heights:
            return (
                True,
                len(data_rows),
                f"{len(data_rows)} track(s), but none had a parseable "
                f"reflector height column: {results_file}",
                None,
                None,
            )

        mean_reflector_height = sum(reflector_heights) / len(reflector_heights)
        mean_quality_score = (
            sum(peak_to_noise_values) / len(peak_to_noise_values)
            if peak_to_noise_values
            else None
        )

        return (
            True,
            len(data_rows),
            f"{len(data_rows)} track(s); mean reflector height "
            f"{mean_reflector_height:.3f} m",
            mean_reflector_height,
            mean_quality_score,
        )

    def _collect_statistics(self, result: GnssIrResult) -> None:
        self._stats.files_processed += 1

        if result.success:
            self._stats.successful += 1
        else:
            self._stats.failed += 1

        self._runtimes.append(result.runtime_seconds)
        self._stats.average_runtime = sum(self._runtimes) / len(self._runtimes)
        self._stats.last_processing = datetime.now(timezone.utc)

    def _record(self, result: GnssIrResult) -> GnssIrResult:
        """Run _collect_statistics() and return the same result, for one-line call sites."""

        self._collect_statistics(result)
        return result

    def _failure_result(
        self,
        observation_file: Path,
        day: date,
        message: str,
        runtime_seconds: float,
    ) -> GnssIrResult:
        return GnssIrResult(
            success=False,
            observation_file=observation_file,
            day=day,
            station_code=self._station_code,
            reflector_height=None,
            soil_moisture=None,
            snow_depth=None,
            quality_score=None,
            num_tracks=0,
            output_directory=(
                self._refl_code / str(day.year) / "results" / self._station_code
                if self._refl_code is not None
                else Path("")
            ),
            message=message,
            runtime_seconds=runtime_seconds,
            gnssrefl_version=self._gnssrefl_version,
        )
