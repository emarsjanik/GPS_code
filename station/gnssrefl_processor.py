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
          staging (_stage_rinex_file()), and its .rnx.gz and
          .crx.gz forms too: rinex2snr unpacks those into that very
          name first (rinex2snr.py:231-237).
        - gnssir's own noise-floor estimation reads two config
          fields, nr1/nr2, defining the region of the reflector-
          height spectrum used to estimate background noise -- NOT
          automatically derived from h1/h2 (the actual RH search
          region). If nr1/nr2 don't cover the same range as h1/h2,
          every arc's noise-floor sample comes back empty, the
          resulting Noise value silently defaults to 0.0, and every
          single arc fails quality control with no indication why
          beyond a RuntimeWarning ("divide by zero encountered in
          scalar divide"). Confirmed directly by tracing gnssrefl's
          own source (retrieve_rh.py) after a real run silently
          produced zero saved arcs despite dozens of arcs passing
          every earlier quality-control check. quickLook (a separate
          gnssrefl code path) does not have this problem: it sets
          nr1/nr2 = minH/maxH automatically on every run, which is
          why quickLook and gnssir can disagree on the exact same
          underlying data even with identical h1/h2 settings.
          Confirmed the real parameter names directly via
          inspect.signature(make_gnssir_input) against the installed
          package (nr1/nr2, NOT a combined 'NReg' keyword -- an
          earlier version of this fix assumed the latter, which is
          not a valid parameter name and raised TypeError on every
          call; caught via a live pipeline run and corrected).
        - Every day processed left two copies of its RINEX
          (350-400 MB each) behind. _stage_rinex_file() copies the
          file into $REFL_CODE/<year>/rinex/<4-char station>/ so
          rinex2snr(nolook=True) can find it; rinex2snr then finds
          no such file in the current working directory and copies
          it there itself (`cp <staged file> .`, gnssrefl
          rinex2snr.py:243-244). On the RINEX 3 path nothing ever
          removes that second copy: only conv2snr's RINEX 2 branch
          deletes its input (rinex2snr.py:496-499), and gnssrefl
          never changes directory. The "29 files, 9.3GB" first found
          in station/ over weeks of ordinary, never-reprocessed
          operation were these working-directory copies -- station/
          is station_manager's working directory; the staged folder
          is under products/. Removing the staged copy after every
          day (done first) was right, but did not stop the growth:
          by November 2026, 29 GB had piled up in station/ and in
          $HOME (cron's working directory). process() now removes
          both, in a finally block, success or failure
          (_remove_cwd_copy(), _cleanup_staged_file()); both are
          only ever needed while rinex2snr() reads them.

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
      internet at all -- not as a general-purpose default. It is GPS
      broadcast ephemeris that gnssrefl downloads (SOPAC/CDDIS,
      gps.py getnavfile()), not the station's own navigation file.

      A day whose orbit never comes, confirmed on the station in
      November 2026: GFZ never published its multi-GNSS rapid orbit
      (GFZ0MGXRAP, what gnssrefl's default 'gnss' fetches as 'gbm'
      from CDDIS) for 2026-11-05, DOY 309 -- CDDIS gps/products/2443
      lists 308 and 310 only. rinex2snr printed "The orbit file you
      requested does not exist." (to stdout only, not to its .gen
      log) and returned without an SNR file; gnssir found no SNR
      file; and the day failed on every run, 20 retries and three
      FAILs in every morning's health mail, until it was processed
      by hand with "rapid": GFZ0OPSRAP from isdc-data.gfz.de, which
      was there (GPS, GLONASS and Galileo, no BeiDou; 139 tracks
      against 148 and 158 for the days either side).

      Hence "gnssrefl_orbit_fallbacks" (default ["rapid", "nav"]):
      when the configured or default orbit made no SNR file,
      process() tries those orbits in order, in the same call. Only
      once the normal wait is over, since the default orbit
      routinely takes 1-2 days and a fallback result is kept for
      good (recover_missing_days.py skips days with results): a
      multi-GNSS fallback from the day's 3rd attempt (at least 36 h
      after the day) or 72 h after the day, a GPS-only one (nav)
      only 5 days after the day, when pipeline.py is about to give
      the day up anyway (_orbit_plan()). A fallback that is the same
      product as an orbit already in the list, after rinex2snr's own
      renames (rapid=gfr, gnss=gbm, gps=nav, gps+glo=jax), is
      skipped. The orbit that made the SNR file is in the result's
      orbit_used and message, so the log and gnssir_products.notes
      say which days were made with a fallback.

This module knows nothing about SQLite, the receiver, station.py, or
pipeline decisions -- same boundaries as rinex_processor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import os
import shutil
import time

from config import Config


# ----------------------------------------------------------------
# Orbits (see "A day whose orbit never comes" in the module docstring)
# ----------------------------------------------------------------

# gnssrefl 4.1.5's orbit_list (rinex2snr_cl.py:526-528). Any other name
# makes rinex2snr print "You picked an orbit type I do not recognize"
# and return without an SNR file, so a fallback must be one of these.
GNSSREFL_ORBITS = frozenset({
    "gps", "gps+glo", "gnss", "nav", "igs", "igr", "jax", "gbm", "grg",
    "wum", "wum2", "gfr", "esa", "ultra", "rapid", "gnss2", "nav-sopac",
    "nav-esa", "nav-cddis", "gnss3", "gnss-gfz",
})

# rinex2snr's own renames before it fetches anything (rinex2snr_cl.py
# 536-564): "rapid" IS gfr, "gnss" IS gbm. gnss2 also ends at gbm but
# fetches it from IGN, another source, so it is not an alias here.
_ORBIT_ALIASES = {"gps": "nav", "rapid": "gfr", "gnss": "gbm", "gps+glo": "jax"}

# GPS-only products, by their name after _ORBIT_ALIASES (gnssrefl's own
# rinex2snr documentation: nav is GPS broadcast, igs/igr IGS GPS only).
# "nav" took a 5.5 h recording of this station from 48 tracks to 20.
GPS_ONLY_ORBITS = frozenset({"nav", "nav-sopac", "nav-esa", "nav-cddis", "igs", "igr"})

# What a fallback costs against the multi-GNSS default, said wherever a
# day made with it is reported.
_ORBIT_COVERAGE = {
    "gfr": "GPS, GLONASS and Galileo; no BeiDou",
    "jax": "GPS and GLONASS only",
}
_GPS_ONLY_COVERAGE = "GPS only, about half the tracks"

DEFAULT_ORBIT_FALLBACKS = ("rapid", "nav")

# When a fallback may be tried for a day whose orbit made no SNR file.
# Not on a day's first attempts: the default orbit routinely takes 1-2
# days, and a day made with a fallback is kept for good. The pipeline
# first tries a day 24 h after its end (pipeline.py ORBIT_WAIT_S; the
# midnight run, then the cron's 02:30 UTC), so its 3rd attempt is the
# midnight run 48 h after it. FALLBACK_RETRY_MIN_AGE_S keeps a
# retry_count raised by failures that never reached gnssrefl (a
# RINEX conversion failure, gnssrefl not initialized, reruns by hand)
# from bringing that forward to the attempts at 24 and 26.5 h.
FALLBACK_MIN_RETRIES = 2                  # earlier failed attempts of the day
FALLBACK_RETRY_MIN_AGE_S = 36 * 3600      # ... and at least this long after it
FALLBACK_MIN_AGE_S = 72 * 3600            # or this long after the day, whatever the count
# A GPS-only fallback (when the orbit tried first is not GPS-only) only
# once the day is more than this old: the last resort before
# pipeline.py hands the day over (its HANDOFF_AGE_S is this value).
GPS_ONLY_FALLBACK_AGE_S = 5 * 86400

# Slack on "written during this attempt", for file systems that keep
# modification times to the second only (as recover_missing_days.py).
FRESH_SLACK_S = 2.0

# GnssIrResult.outcome
OUTCOME_OK = "ok"                         # a results file with data rows
OUTCOME_NO_SNR = "no_snr"                 # no SNR file with any orbit tried
OUTCOME_NO_ARCS = "no_arcs"               # SNR file made, gnssir kept no arc
OUTCOME_NO_ARCS_GPS_ONLY = "no_arcs_gps_only"   # the same, from a GPS-only fallback
OUTCOME_ERROR = "error"                   # anything else

# The marker recover_missing_days.py has always written for a day with
# no usable retrievals, next to the results, named by the UNPADDED day
# of year (<doy>.no_data).
NO_DATA_MARKER_FORMAT = "%Y-%m-%dT%H:%M:%SZ -- no usable retrievals\n"


def _utcnow() -> datetime:
    """The clock of the fallback rules (a test sets it)."""

    return datetime.now(timezone.utc)


def day_age_s(day: date, now: datetime | None = None) -> float:
    """Seconds since the end (24:00 UTC) of `day`; negative before it."""

    day_end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    return ((now or _utcnow()) - day_end).total_seconds()


def gnssrefl_default_orbit(day: date) -> str:
    """
    The orbit rinex2snr picks for `day` when given none, exactly as
    gnssrefl 4.1.5 does (rinex2snr_cl.py:453-472): 'gnss' after
    2025 + 168/365.25, 'rapid' after 2021 + 137/365.25, else 'nav'.
    (A station json with an 'orb' key would win over this, but
    make_gnssir_input() writes that key only when given one, and this
    module never gives it.)
    """

    t = day.year + day.timetuple().tm_yday / 365.25
    if t > 2025 + 168 / 365.25:
        return "gnss"
    if t > 2021 + 137 / 365.25:
        return "rapid"
    return "nav"


def _canonical_orbit(orb, day: date) -> str:
    """
    The product rinex2snr fetches for `orb` on `day`, after its own
    renames. A name it does not know (e.g. "Rapid": rinex2snr compares
    as given, and refuses it) is returned as it is, so it never
    matches -- and never removes -- a fallback that would work.
    """

    name = orb if orb else gnssrefl_default_orbit(day)
    if not isinstance(name, str) or name not in GNSSREFL_ORBITS:
        return str(name)
    return _ORBIT_ALIASES.get(name, name)


def _orbit_label(orb, day: date) -> str:
    """How an orbit is named in messages: as configured, or e.g.
    "gnss (gnssrefl default)" when none is."""

    return str(orb) if orb else f"{gnssrefl_default_orbit(day)} (gnssrefl default)"


def _orbit_coverage(orb, day: date) -> str:
    """What `orb` covers, when that is less than the default's; else ""."""

    canonical = _canonical_orbit(orb, day)
    if canonical in GPS_ONLY_ORBITS:
        return _GPS_ONLY_COVERAGE
    return _ORBIT_COVERAGE.get(canonical, "")


def _fresh(path: Path, since: float) -> bool:
    """Written at or after `since` (time.time()), as recover_missing_days.py."""

    try:
        return path.stat().st_mtime >= since - FRESH_SLACK_S
    except OSError:
        return False


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def no_data_marker_path(results_dir, day: date) -> Path:
    """<results_dir>/<doy>.no_data -- the day of year NOT zero-padded
    (309.no_data, 5.no_data), unlike the results file (005.txt): the
    name recover_missing_days.py, clean_processing_queue.py and
    station_health.py read."""

    return Path(results_dir) / f"{day.timetuple().tm_yday}.no_data"


def write_no_data_marker(results_dir, day: date) -> Path:
    """
    Mark `day` as processed with no usable retrievals: the same name,
    place and text recover_missing_days.py writes, so its automatic
    search, clean_processing_queue.py and station_health.py's
    currency and error checks all read it. Returns the marker's path.

    Raises ValueError when no results folder is given (an empty path
    would put the marker in the working directory), OSError when it
    cannot be written.
    """

    if str(Path(results_dir)) in ("", "."):
        raise ValueError("no results folder to write the no-data marker in")
    marker = no_data_marker_path(results_dir, day)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).strftime(NO_DATA_MARKER_FORMAT))
    return marker


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
    """
    The outcome of one process() call.

    outcome says what kind of outcome it was, for a caller that must
    tell a day that is only late from one that can never give results
    (pipeline.py's bounded retries, recover_missing_days.py's markers):

        "ok"                a results file with data rows (success).
        "no_snr"            no fresh SNR file with any orbit tried,
                            whether rinex2snr returned or raised: the
                            orbit is not published (yet), or the RINEX
                            has no usable data. gnssir was not run.
        "no_arcs"           a fresh SNR file; gnssir ran through its
                            quality control (a fresh failQC/<doy>.txt)
                            and kept no arc, so it wrote no results
                            file. The day has no usable retrievals.
        "no_arcs_gps_only"  the same, but the SNR file was made with a
                            GPS-only fallback orbit: a multi-GNSS orbit
                            may still give arcs, so it is not a no-data
                            day yet.
        "error"             everything else: not initialized, bad
                            input, staging, gnssir raised or stopped
                            before its quality control, a results file
                            it did not replace, empty, or without data
                            rows. A gnssrefl or setup problem, not a
                            lack of data.

    orbit_used is the orbit whose rinex2snr made the SNR file, and
    orbits_tried every orbit tried, in order, as named in messages
    ("gnss (gnssrefl default)" when none is configured). The three
    default to empty, for results built before they existed; read
    them with getattr() where a stand-in may lack them.
    """

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
    outcome: str = ""
    orbit_used: str = ""
    orbits_tried: tuple = ()


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
    _orbit_fallbacks: tuple = field(default=(), init=False, repr=False)
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
        self._orbit_fallbacks = self._resolve_orbit_fallbacks(station_section)
        if self._orbit_source is not None and not (
            isinstance(self._orbit_source, str)
            and self._orbit_source in GNSSREFL_ORBITS
        ):
            # Not refused (that would be new behaviour for an old
            # setting), but said: rinex2snr compares the name as given.
            self._logger.warning(
                "station.json gnssrefl_orbit_source %r is not an orbit "
                "gnssrefl 4.1.5 knows (%s): rinex2snr will refuse it and "
                "make no SNR file, so only gnssrefl_orbit_fallbacks can "
                "process a day",
                self._orbit_source,
                ", ".join(sorted(GNSSREFL_ORBITS)),
            )

        self._verify_gnssrefl_importable()
        self._setup_station_strategy()

        self._initialized = True

        self._logger.info(
            "GnssIrProcessor initialized: station=%s (%s) REFL_CODE=%s "
            "gnssrefl=%s orbit_source=%s orbit_fallbacks=%s sample_rate=%ds",
            self._station_code,
            self._station_code_9ch,
            self._refl_code,
            self._gnssrefl_version,
            self._orbit_source or "(gnssrefl default)",
            ", ".join(self._orbit_fallbacks) or "(none)",
            self._sample_rate,
        )

        return "READY"

    def process(
        self, observation_file: Path, day: date, retry_count: int = 0
    ) -> GnssIrResult:
        """
        Run rinex2snr then gnssir against `observation_file` for
        `day`. Never raises: every failure mode (not initialized,
        missing input, rinex2snr failing, gnssir failing) comes back
        as a GnssIrResult with success=False, an outcome and a clear
        message.

        When the configured (or gnssrefl's default) orbit makes no
        SNR file, the orbits of station.json's gnssrefl_orbit_fallbacks
        are tried in turn, once the day's normal wait for its orbit is
        over (_orbit_plan()): never on a day's first attempts, since
        the default orbit routinely takes 1-2 days
        (STATION_JSON_REFERENCE.md, "Orbit source").

        Both copies of the RINEX this call makes are always removed
        once it is done with them, success or failure: the one
        _stage_rinex_file() puts under $REFL_CODE, and the one
        rinex2snr itself copies into the current working directory
        and never removes (_remove_cwd_copy()). Confirmed, real bug
        this fixes: at ~350-400MB a day, those copies had piled up to
        29 GB in station/ and $HOME by November 2026 -- see the
        module docstring.

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
        retry_count:
            How many earlier attempts of this day failed
            (pipeline.py passes processing_queue.retry_count). From 2,
            i.e. the 3rd attempt, a multi-GNSS fallback orbit may be
            tried; left at 0 (recover_missing_days.py,
            ultra_rapid_check.py) only the day's age decides.
        """

        try:
            retry_count = max(0, int(retry_count or 0))
        except (TypeError, ValueError):
            retry_count = 0

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
            staged_path = self._stage_rinex_file(observation_file, day)
        except OSError as exc:
            elapsed = time.monotonic() - started
            message = f"Could not stage RINEX file for gnssrefl: {exc}"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", observation_file.name, message, elapsed
            )
            return self._record(
                self._failure_result(observation_file, day, message, elapsed)
            )

        # Everything from here on operates on the file we just
        # staged (and rinex2snr's own copy of it), so both must be
        # cleaned up afterward regardless of how _process_staged()
        # finishes -- success, a caught rinex2snr/gnssir failure, or
        # (belt-and-suspenders) an uncaught exception. See the
        # "confirmed, real bug" note above and in the module docstring.
        try:
            return self._process_staged(
                observation_file, day, staged_path, started, retry_count
            )
        finally:
            self._remove_cwd_copy(staged_path, observation_file)
            self._cleanup_staged_file(staged_path)

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

    @staticmethod
    def _resolve_orbit_fallbacks(station_section: dict) -> tuple:
        """
        station.json's "gnssrefl_orbit_fallbacks": the orbits tried,
        in order, when the configured or default orbit made no SNR
        file for a day (see the module docstring). Unset means
        DEFAULT_ORBIT_FALLBACKS, ["rapid", "nav"]; [] turns the
        fallback off. Each name is trimmed and lower-cased, then checked
        against gnssrefl 4.1.5's names (GNSSREFL_ORBITS), so " Rapid "
        is read as "rapid". gnssrefl itself compares exactly
        (rinex2snr_cl.py: `orb not in orbit_list`), and
        gnssrefl_orbit_source, passed to it as written, is not
        lower-cased: "Rapid" there draws a warning in initialize() and
        makes no SNR file.

        A wrong value is refused here, naming it, rather than left to
        fail every day: rinex2snr only prints "You picked an orbit
        type I do not recognize" and returns without an SNR file, and
        a string such as "rapid,nav" would otherwise be read as a
        list of letters.
        """

        key = "gnssrefl_orbit_fallbacks"
        value = station_section.get(key)
        if value is None:
            return DEFAULT_ORBIT_FALLBACKS
        if not isinstance(value, (list, tuple)):
            raise GnssIrProcessorError(
                f'station.json {key} must be a list of orbit names, e.g. '
                f'["rapid", "nav"], or [] for none, not {value!r}'
            )
        names: list[str] = []
        for item in value:
            name = item.strip().lower() if isinstance(item, str) else None
            if name not in GNSSREFL_ORBITS:
                raise GnssIrProcessorError(
                    f"station.json {key}: {item!r} is not an orbit gnssrefl "
                    f"knows ({', '.join(sorted(GNSSREFL_ORBITS))})"
                )
            if name not in names:
                names.append(name)
        return tuple(names)

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
        elevation-span tolerance, direct-signal polynomial order) are real
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

        Arc-level QC (opt-in, all unset by default, so the json
        gnssrefl writes is byte for byte what it was before):
        "gnssrefl_subdaily_alt_sigma" and
        "gnssrefl_subdaily_spline_outlier1_m"/"_2_m" go into the
        json's subdaily_* keys, which analysis_tools/gnss_record.py
        puts on subdaily's command line each night; and
        "gnssrefl_amplitude_min_by_frequency" replaces the one-value
        reqAmp list with one threshold per frequency
        (_apply_amplitude_by_frequency()). Try them first with
        analysis_tools/evaluate_qc_variants.py.
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

        # Confirmed, real bug this fixes: gnssrefl's gnssir/rinex2snr
        # pipeline reads nr1/nr2 (the reflector-height region used to
        # estimate the noise floor) as SEPARATE config fields from
        # h1/h2 (the actual RH search region) -- NOT automatically
        # derived from them. If nr1/nr2 do not cover the same range
        # as h1/h2, every arc's noise-floor sample comes back empty,
        # the resulting Noise value silently defaults to 0.0, and
        # every single arc fails quality control -- confirmed
        # directly by tracing gnssrefl's own source (retrieve_rh.py)
        # after a real pipeline run silently produced zero saved arcs
        # despite dozens of arcs passing every earlier
        # quality-control check, accompanied by a real, reproducible
        # "RuntimeWarning: divide by zero encountered in scalar
        # divide" at the exact line that computes maxAmp/Noise.
        # quickLook (gnssrefl's other, separate exploratory tool)
        # does not have this problem: it sets nr1/nr2 = minH/maxH
        # automatically on every single run, which is exactly why
        # quickLook and gnssir can silently disagree on identical
        # underlying data even with identical h1/h2 settings --
        # quickLook's noise region always tracks whatever range you
        # ask it to search, gnssir's does not.
        #
        # Confirmed the real parameter names directly via
        # inspect.signature(make_gnssir_input) against the actual
        # installed package (gnssrefl 4.1.5): the function takes
        # nr1/nr2 as two separate float keyword arguments and builds
        # its own internal NReg=[nr1, nr2] -- there is no 'NReg'
        # keyword argument on the function itself. An earlier version
        # of this fix assumed a combined 'NReg' keyword by analogy
        # with h1/h2's naming, without checking -- that assumption
        # was wrong and raised TypeError on every single call,
        # caught only via a live pipeline run against real hardware
        # and corrected to nr1/nr2 as shown below.
        #
        # Only set here when BOTH h1 and h2 are explicitly configured
        # above, matching this method's existing philosophy
        # throughout: gnssrefl's own internal default applies
        # whenever a value isn't explicitly configured, rather than
        # this code silently overriding it. If only one of h1/h2 is
        # set, nr1/nr2 are deliberately left unset too, so gnssrefl's
        # own default RH search behavior (which is presumably already
        # internally self-consistent with its own default nr1/nr2)
        # applies to the whole reflector-height configuration
        # together, instead of mixing one explicit bound with
        # unrelated default noise-region bounds that may not match it.
        if "h1" in kwargs and "h2" in kwargs:
            kwargs["nr1"] = kwargs["h1"]
            kwargs["nr2"] = kwargs["h2"]

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
        # just relative reflector height.
        #
        # MUST BE A LIST, not a float. gnssrefl declares Hortho as a
        # list (it supports several values with matching Hdates, for
        # sites whose antenna height changes over time) and guards
        # the assignment with `if type(Hortho) == list`. A float
        # fails that check and is discarded with no warning, after
        # which gnssrefl substitutes its own EGM96-derived value.
        #
        # That silently cost this station 4 cm on every published
        # water level: the configured height (then 18.665 m) was
        # dropped for gnssrefl's EGM96 fallback, 18.625 m.
        #
        # The height must be NAVD88 (this system's standard; NGS OPUS
        # with GEOID18 gives 19.014 m here). 18.665 m was the CSRS-PPP
        # CGVD2013 height and left every water level 0.349 m low --
        # see analysis_tools/station_datum.py.
        orthometric_height = station_section.get("gnssrefl_orthometric_height")
        if orthometric_height is not None:
            kwargs["Hortho"] = [float(orthometric_height)]

        # Refraction model (gnssrefl's own params: refraction and
        # refr_model) -- 1 is the Bennett correction (gnssrefl's own
        # default); matters more for very tall or very short sites.
        #
        # In gnssrefl 4.1.5 `refraction` is only the on/off switch and
        # the model number is the separate STRING argument refr_model.
        # Passing the number as `refraction` left any non-zero model
        # truthy and refr_model at "1": asking for 6 (MPF) silently
        # kept Bennett. 0 turns the correction off.
        #
        # gnssrefl also takes the names NITE (5) and MPF (6), which
        # int() turned into a bare ValueError that stopped initialize()
        # outside the error handling below; a name is now passed as
        # it is, for gnssrefl to check like a number.
        refraction_model = station_section.get("gnssrefl_refraction_model")
        if refraction_model is not None:
            try:
                model = str(int(refraction_model))
            except (TypeError, ValueError):
                model = str(refraction_model).strip()
                if model.upper() not in ("NITE", "MPF"):
                    raise GnssIrProcessorError(
                        f"gnssrefl_refraction_model {refraction_model!r} is not "
                        f"a model number (0-6) or NITE/MPF"
                    )
            kwargs["refr_model"] = model
            if model == "0":
                kwargs["refraction"] = False

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

        # Direct-signal-removal polynomial order (gnssrefl's own
        # param: polyV) -- library default is 4. Confirmed directly
        # relevant to us: our narrow elevation mask (5-15 degrees)
        # gives each arc a much smaller window of raw SNR data to fit
        # against than the wider 5-25 degree default, and our own
        # real pipeline output has repeatedly shown numpy's
        # "RankWarning: The fit may be poorly conditioned" for
        # exactly this fit. Confirmed via general numerical-analysis
        # references (not just gnssrefl's own docs) that fitting a
        # lower-order polynomial is the standard, documented fix for
        # this exact warning. Only passed through if explicitly
        # configured, so gnssrefl's own default applies otherwise.
        direct_signal_poly_order = station_section.get("gnssrefl_direct_signal_poly_order")
        if direct_signal_poly_order is not None:
            kwargs["polyV"] = int(direct_signal_poly_order)

        # Arc-level QC in subdaily (gnssrefl's own params:
        # subdaily_alt_sigma, subdaily_spline_outlier1/2 -- stored in
        # the json). analysis_tools/gnss_record.py reads them back and
        # puts them on subdaily's command line: confirmed against the
        # 4.1.5 source and a run, subdaily never takes alt_sigma from
        # the json (subdaily() defaults it to False, and the json is
        # only consulted when it is None), and on the command line a
        # change refits the frozen years once. Left unset, subdaily's
        # defaults hold:
        #   * part 1 drops arcs more than 2.5 sigma from that UTC
        #     day's mean, sigma a plain standard deviation. alt_sigma
        #     makes it the median and 1.4826 x the median absolute
        #     deviation, the robust definition gnssrefl credits to
        #     Felipe Geremia-Nievinski. A day's spread is mostly tide,
        #     so this clip rarely fires either way.
        #   * the two spline clips drop arcs more than 3 standard
        #     deviations (of their residuals over the WHOLE fit) from a
        #     spline; spline_outlier1/2 replace that by a fixed limit in
        #     metres. The first clip tests the RHdot-corrected RH (no
        #     inter-frequency correction yet) against the first spline,
        #     which is fitted to the UNCORRECTED RH -- so its residual
        #     carries that spline's misfit and each signal's bias and is
        #     wider than the final scatter (a limit sized on the final
        #     arcs is several times too tight there). The second tests
        #     the RHdot- and inter-frequency-corrected RH against the
        #     final spline (gnssrefl's subdaily, Larson 2024; the RHdot
        #     correction, Larson et al. 2013, 2017). Only the first one
        #     changes the published spline: the final spline is refitted
        #     on every arc that survived it.
        # In a synthetic reproduction of the Sep 2026 storm with
        # gnssrefl's real subdaily, fixed limits of 0.30/0.25 m with
        # alt_sigma left a sustained +1.7 m storm bias where it was (the
        # limits are relative to a spline that follows the bias) and
        # cost 12% of calm arcs (0.30 m was 1.5 x the scatter the first
        # clip tested there). That is why these are opt-in and evaluated
        # on the station's own results first
        # (analysis_tools/evaluate_qc_variants.py).
        alt_sigma = station_section.get("gnssrefl_subdaily_alt_sigma")
        if alt_sigma is not None:
            kwargs["subdaily_alt_sigma"] = self._as_bool(
                alt_sigma, "gnssrefl_subdaily_alt_sigma"
            )
        for key, param in (
            ("gnssrefl_subdaily_spline_outlier1_m", "subdaily_spline_outlier1"),
            ("gnssrefl_subdaily_spline_outlier2_m", "subdaily_spline_outlier2"),
        ):
            value = station_section.get(key)
            if value is None:
                continue
            try:
                limit = float(value)
            except (TypeError, ValueError):
                limit = float("nan")
            if not limit > 0:
                raise GnssIrProcessorError(
                    f"station.json {key} must be a positive number of "
                    f"metres, not {value!r}"
                )
            kwargs[param] = limit

        try:
            make_gnssir_input(**kwargs)
        except (Exception, SystemExit) as exc:
            raise GnssIrProcessorError(
                f"Could not set up gnssrefl analysis strategy for "
                f"station {self._station_code!r}: {exc}"
            ) from exc

        amplitude_by_frequency = station_section.get(
            "gnssrefl_amplitude_min_by_frequency"
        )
        if amplitude_by_frequency is not None:
            self._apply_amplitude_by_frequency(amplitude_by_frequency)

    @staticmethod
    def _as_bool(value, key: str) -> bool:
        """A station.json switch: true/false (or the strings "true",
        "false", "yes", "no", "1", "0"). bool("false") is True, so a
        quoted value is read rather than coerced."""

        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1"):
            return True
        if text in ("false", "no", "0"):
            return False
        raise GnssIrProcessorError(
            f"station.json {key} must be true or false, not {value!r}"
        )

    def _gnssir_json_path(self) -> Path:
        """The json make_gnssir_input() just wrote (gnssrefl 4.1.5:
        $REFL_CODE/input/<station>/<station>.json, no extension)."""

        return (
            self._refl_code
            / "input"
            / self._station_code
            / f"{self._station_code}.json"
        )

    def _apply_amplitude_by_frequency(self, thresholds) -> None:
        """
        One LSP amplitude threshold per frequency in the json gnssir
        reads, from station.json's "gnssrefl_amplitude_min_by_frequency",
        e.g. {"1": 6.5, "20": 5.0, "101": 7.0}.

        make_gnssir_input() has no argument for this: it writes
        reqAmp = [ampl] * len(freqs), one value for every frequency,
        and gnssir applies reqAmp[i] to freqs[i] (an arc is kept only
        if its amplitude is ABOVE it). The amplitudes of different
        signals are not comparable -- transmit power, antenna gain at
        low elevation and receiver tracking differ by constellation
        and band; gnssrefl's own documentation calls the threshold
        "receiver and elevation angle region dependent" -- so this
        rewrites that list after make_gnssir_input() has written the
        file: a listed frequency gets its own value, every other one
        keeps the one gnssrefl wrote. It matters in storms: a rough sea
        lowers the coherent reflected power (the roughness damping of
        Strandberg et al.'s inverse SNR model, 2016, 2019; Sepulveda
        et al. 2023 see errors rise abruptly at high waves), and of a
        strong signal the damped, doubtful arcs still clear one shared
        threshold. A listed frequency that is not in the json's freqs
        is ignored with a warning. Done on every initialize(), like
        make_gnssir_input() itself.

        It applies to days gnssir processes from now on; results
        already written keep the threshold they were made with until
        those days are processed again. analysis_tools/
        amplitude_thresholds.py derives values from each frequency's
        own amplitude histogram; analysis_tools/evaluate_qc_variants.py
        scores them against the Chatham gauge first.
        """

        key = "gnssrefl_amplitude_min_by_frequency"
        if not isinstance(thresholds, dict):
            raise GnssIrProcessorError(
                f"station.json {key} must map frequency codes to amplitudes, "
                f'e.g. {{"1": 6.5, "20": 5.0}}, not {thresholds!r}'
            )
        wanted = {}
        for freq, amplitude in thresholds.items():
            try:
                code, value = int(freq), float(amplitude)
            except (TypeError, ValueError):
                raise GnssIrProcessorError(
                    f"station.json {key}: {freq!r}: {amplitude!r} is not a "
                    f"frequency code and an amplitude"
                ) from None
            if not value > 0:
                raise GnssIrProcessorError(
                    f"station.json {key}: the amplitude for frequency {code} "
                    f"must be positive, not {amplitude!r}"
                )
            wanted[code] = value

        path = self._gnssir_json_path()
        try:
            strategy = json.loads(path.read_text())
            freqs = [int(f) for f in strategy["freqs"]]
            req_amp = list(strategy["reqAmp"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GnssIrProcessorError(
                f"Could not read the gnssir json {path} to set per-frequency "
                f"amplitudes: {exc}"
            ) from exc

        unknown = sorted(set(wanted) - set(freqs))
        if unknown:
            self._logger.warning(
                "%s lists frequencies %s that this station's gnssir json does "
                "not analyse (freqs %s); ignored.",
                key,
                unknown,
                freqs,
            )
        default = req_amp[0] if req_amp else None
        strategy["reqAmp"] = [
            wanted.get(f, req_amp[i] if i < len(req_amp) else default)
            for i, f in enumerate(freqs)
        ]

        # the same layout gnssrefl writes (json.dump, indent=4), in one step
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(strategy, f, indent=4)
            os.replace(tmp, path)
        except OSError as exc:
            raise GnssIrProcessorError(
                f"Could not write per-frequency amplitudes to {path}: {exc}"
            ) from exc
        self._logger.info(
            "Per-frequency amplitude thresholds (reqAmp) in %s: %s",
            path,
            dict(zip(freqs, strategy["reqAmp"])),
        )

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
          working directory is removed before staging -- and its
          .rnx.gz and .crx.gz forms, which rinex2snr unpacks into
          that name before looking (rinex2snr.py:231-237) -- unless
          it is the observation file itself (a RINEX processed from
          the working directory under this name), which would
          otherwise be deleted before it is copied.

        The path returned here is the same one process() now passes
        to _cleanup_staged_file() once it's done being used -- see
        that method's docstring for the real disk-growth bug this
        closes.
        """

        assert self._refl_code is not None

        year = day.year

        staged_name = self._staged_name(day)

        # Defend against gnssrefl's confirmed stale-file-reuse bug:
        # remove any same-named file sitting in the current working
        # directory before staging, so it can never be silently
        # reused instead of the file we're about to stage.
        cwd = Path.cwd()
        for name in (
            staged_name,
            staged_name + ".gz",
            staged_name[: -len(".rnx")] + ".crx.gz",
        ):
            stale_cwd_file = cwd / name
            if stale_cwd_file.exists() and not _same_file(
                stale_cwd_file, observation_file
            ):
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

    def _staged_name(self, day: date) -> str:
        """The RINEX 3 long name rinex2snr looks for (see _stage_rinex_file())."""

        doy = day.timetuple().tm_yday
        return (
            f"{self._station_code_9ch.upper()}_R_{day.year}{doy:03d}0000"
            f"_01D_{self._sample_rate:02d}S_MO.rnx"
        )

    def _remove_cwd_copy(self, staged_path: Path, observation_file: Path) -> None:
        """
        Remove the copy of the day's RINEX that gnssrefl's rinex2snr
        made in the current working directory, once process() is done.

        Confirmed in gnssrefl 4.1.5's source: with nolook=True and a
        9-character station, rinex2snr looks for the RINEX 3 file in
        the current working directory and, when it is not there, runs
        `cp $REFL_CODE/<year>/rinex/<sta4>/<file> .` (rinex2snr.py:
        243-244). conv2snr's RINEX 3 branch then removes nothing --
        only its RINEX 2 branch deletes its input (496-499) -- and
        gnssrefl never changes directory. So every day processed left
        a 350-400 MB copy in whatever directory the caller ran from:
        29 GB had piled up in station/ (station_manager's working
        directory) and $HOME (cron's) by November 2026.

        Only that exact name (_staged_name()) is touched, and never
        when it is the observation file itself (a RINEX processed
        from the working directory under that name). When the working
        directory is the staging folder, this is the staged file,
        which _cleanup_staged_file() removes next anyway. Every
        process that runs rinex2snr in a shared working directory
        holds pipeline.py's run lock, so the copy is this call's.

        Called from a finally block in process(). Never raises: a
        leftover copy is logged, not a processing failure.
        """

        try:
            copy = Path.cwd() / staged_path.name
        except OSError as exc:
            self._logger.warning(
                "Could not check the working directory for gnssrefl's "
                "copy of %s: %s", staged_path.name, exc
            )
            return

        try:
            if not copy.exists() or _same_file(copy, observation_file):
                return
            copy.unlink()
            self._logger.debug("Removed gnssrefl's working copy %s", copy)
        except OSError as exc:
            self._logger.warning(
                "Could not remove gnssrefl's working copy %s: %s", copy, exc
            )

    def _cleanup_staged_file(self, staged_path: Path) -> None:
        """
        Remove the RINEX file staged into gnssrefl's working
        directory by _stage_rinex_file(), once process() is done
        using it.

        Without this, the staged copy (~350-400MB per day, since
        it's a copy of the full raw RINEX observation file) was never
        cleaned up afterward. The only existing cleanup --
        _stage_rinex_file() itself removing a same-named stale file
        before staging a new one -- only triggers when a day is
        staged a SECOND time under the exact same filename, which
        never happens for a day processed exactly once (the normal,
        expected case). This was first taken for the "29 files,
        9.3GB" found in station/; those were gnssrefl's own copies in
        the working directory, which _remove_cwd_copy() removes (see
        the module docstring). The staged copies, under
        $REFL_CODE/<year>/rinex/<sta4>/, would have grown the same way.

        Called from a finally block in process(), so this runs
        whether processing succeeded, failed partway through, or
        raised -- the staged copy is only ever needed transiently,
        for rinex2snr() to read during this one process() call.

        Never raises: a failure to clean up a temporary file is
        logged, not treated as a processing failure in its own
        right.
        """

        try:
            if staged_path.exists():
                staged_path.unlink()
        except OSError as exc:
            self._logger.warning(
                "Could not remove staged RINEX file %s: %s", staged_path, exc
            )

    def _run_rinex2snr(self, year: int, doy: int, orb) -> None:
        from gnssrefl.rinex2snr_cl import rinex2snr

        rinex2snr(
            station=self._station_code_9ch,
            year=year,
            doy=doy,
            nolook=True,
            overwrite=True,
            orb=orb,
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

    def _process_staged(
        self,
        observation_file: Path,
        day: date,
        staged_path: Path,
        started: float,
        retry_count: int = 0,
    ) -> GnssIrResult:
        """
        Runs rinex2snr and gnssir against a file that has already
        been staged, and reports the outcome. Split out from
        process() itself purely so process() can wrap the call in a
        try/finally that always cleans up `staged_path` afterward
        (see _cleanup_staged_file()), without deeply re-indenting
        this entire body inside that try block. `staged_path` is not
        otherwise used here -- gnssrefl locates the file itself via
        year/doy.

        1. rinex2snr, once per orbit of _orbit_plan(), until one
           leaves a FRESH SNR file: one written during that attempt
           (its mtime), so a file from an earlier run is never taken
           for this one's. rinex2snr says nothing when it has no
           orbit -- it prints "The orbit file you requested does not
           exist." to stdout and returns -- so the SNR file is the
           only evidence. An attempt that raised counts as having
           made none, and whatever SNR file it may have left is
           removed, so the next step never runs on a broken one.
        2. No SNR file with any orbit: outcome "no_snr", and gnssir
           is not run (it could only print "SNR file not found", and
           would leave any older results file where it is).
        3. gnssir, then the results file, which must have been
           written by this gnssir run. Without one, a fresh
           failQC/<doy>.txt (gnssrefl 4.1.5 writes it whenever its
           quality control ran, header-only when it kept nothing, and
           deletes <doy>.txt when no arc passed) means "no_arcs"; no
           fresh failQC means gnssir stopped before its quality
           control (e.g. an RH range under 5 m, a station-code
           mismatch), an "error".

        The message always starts as it did ("No results file
        produced: <results file>", "rinex2snr failed: ...", "gnssir
        failed: ..."): station_health.py and recover_missing_days.py
        key on those starts and on the path as the first word after
        them. What happened follows the path, in brackets.
        """

        year = day.year
        doy = day.timetuple().tm_yday
        name = observation_file.name

        plan, pending, why = self._orbit_plan(day, retry_count)
        tried: list[str] = []
        raised: list[tuple[str, BaseException]] = []
        made_index = None

        for index, orb in enumerate(plan):
            label = _orbit_label(orb, day)
            if index:
                self._logger.info(
                    "%s: no SNR file with orbit %s; trying fallback orbit %s (%s)",
                    name, tried[-1], label, why,
                )
            attempt_started = time.time()   # wall clock: `started` is monotonic
            tried.append(label)
            try:
                self._run_rinex2snr(year, doy, orb)
            except (Exception, SystemExit) as exc:
                # Catches SystemExit too: gnssrefl is research-grade code
                # that may call sys.exit() internally on some error
                # paths rather than raising cleanly. "Never crash" means
                # never crash even if the external library tries to.
                # (Never KeyboardInterrupt: recover_missing_days.py
                # stops on it.)
                raised.append((label, exc))
                self._logger.warning(
                    "%s: rinex2snr raised with orbit %s: %s", name, label, exc
                )
                self._remove_fresh_snr_files(day, attempt_started)
                continue
            if self._snr_file_made(day, attempt_started):
                made_index = index
                break

        if made_index is None:
            elapsed = time.monotonic() - started
            message = self._no_snr_message(day, tried, raised, pending)
            self._logger.warning(
                "NO SNR FILE %s (day %s): %s (%.2f sec)", name, day, message, elapsed
            )
            return self._record(
                self._failure_result(
                    observation_file, day, message, elapsed,
                    outcome=OUTCOME_NO_SNR, orbits_tried=tuple(tried),
                )
            )

        made_with = tried[made_index]
        made_orb = plan[made_index]
        fallback = made_index > 0
        coverage = _orbit_coverage(made_orb, day) if fallback else ""

        gnssir_started = time.time()
        try:
            self._run_gnssir(year, doy)
        except (Exception, SystemExit) as exc:
            elapsed = time.monotonic() - started
            message = f"gnssir failed: {exc} (SNR file made with orbit {made_with})"
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", name, message, elapsed
            )
            return self._record(
                self._failure_result(
                    observation_file, day, message, elapsed,
                    orbit_used=made_with, orbits_tried=tuple(tried),
                )
            )

        elapsed = time.monotonic() - started

        ok, num_tracks, notes, reflector_height, quality_score = self._read_results(
            year, doy, since=gnssir_started
        )

        results_file = self._results_file_path(day)

        if ok:
            outcome = OUTCOME_OK
            if fallback:
                notes += (
                    f"; orbit {made_with} (fallback: {', '.join(tried[:made_index])} "
                    f"made no SNR file" + (f"; {coverage}" if coverage else "") + ")"
                )
            else:
                notes += f"; orbit {made_with}"
        elif results_file.exists():
            # stale, empty or without data rows: _read_results() says which
            outcome = OUTCOME_ERROR
            notes += f" (SNR file made with orbit {made_with})"
        elif _fresh(self._failqc_path(day), gnssir_started):
            if fallback and self._gps_only_gate(made_orb, day):
                outcome = OUTCOME_NO_ARCS_GPS_ONLY
                notes = (
                    f"No results file produced: {results_file} (SNR file made with "
                    f"GPS-only fallback orbit {made_with}, but gnssir kept no arc; a "
                    f"multi-GNSS orbit may still give arcs, so the day is not taken "
                    f"as having no usable data; see {self._failqc_path(day)})"
                )
            else:
                outcome = OUTCOME_NO_ARCS
                notes = (
                    f"No results file produced: {results_file} (SNR file made with "
                    f"orbit {made_with}, but gnssir kept no arc; see "
                    f"{self._failqc_path(day)})"
                )
        else:
            outcome = OUTCOME_ERROR
            notes = (
                f"No results file produced: {results_file} (SNR file made with "
                f"orbit {made_with}, but gnssir wrote no failQC/{doy:03d}.txt "
                f"either: it stopped before its quality control; see the "
                f"station log)"
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
            outcome=outcome,
            orbit_used=made_with,
            orbits_tried=tuple(tried),
        )

        if ok and fallback:
            # Said louder than an ordinary success: the day is kept as
            # made with a lesser orbit, and only a person can redo it.
            self._logger.warning(
                "SUCCESS %s (%.2f sec, %d track(s)) with fallback orbit %s%s",
                name, elapsed, num_tracks, made_with,
                f" ({coverage})" if coverage else "",
            )
        elif ok:
            self._logger.info(
                "SUCCESS %s (%.2f sec, %d track(s), orbit %s)",
                name, elapsed, num_tracks, made_with,
            )
        elif outcome in (OUTCOME_NO_ARCS, OUTCOME_NO_ARCS_GPS_ONLY):
            self._logger.warning(
                "NO ARCS %s (day %s): %s (%.2f sec)", name, day, notes, elapsed
            )
        else:
            self._logger.error(
                "FAILED %s: %s (%.2f sec)", name, notes, elapsed
            )

        return self._record(result)

    # ----------------------------------------------------------
    # Orbit fallback (see the module docstring)
    # ----------------------------------------------------------

    def _gps_only_gate(self, orb, day: date) -> bool:
        """True for a GPS-only fallback when the orbit tried first is
        not GPS-only itself: one that would halve the day's tracks."""

        return (
            _canonical_orbit(orb, day) in GPS_ONLY_ORBITS
            and _canonical_orbit(self._orbit_source, day) not in GPS_ONLY_ORBITS
        )

    def _fallback_due(self, orb, day: date, retry_count: int, age_s: float) -> bool:
        """
        Whether fallback `orb` may be tried on this attempt of `day`.

        A multi-GNSS one: from the day's 3rd attempt (two earlier
        failures) once it is at least FALLBACK_RETRY_MIN_AGE_S after
        the day -- the pipeline's midnight run 48 h after it -- or
        from FALLBACK_MIN_AGE_S (72 h) after the day whatever the
        count. Never earlier: the default orbit routinely takes 1-2
        days, and the day would be kept with the lesser orbit.

        A GPS-only one (_gps_only_gate()): only once the day is more
        than GPS_ONLY_FALLBACK_AGE_S old, when pipeline.py would give
        it up anyway. A storm day, when arcs are fewest and matter
        most, must not be left with half its tracks while a
        multi-GNSS orbit can still come.
        """

        if self._gps_only_gate(orb, day):
            return age_s > GPS_ONLY_FALLBACK_AGE_S
        if age_s >= FALLBACK_MIN_AGE_S:
            return True
        return retry_count >= FALLBACK_MIN_RETRIES and age_s >= FALLBACK_RETRY_MIN_AGE_S

    def _fallback_schedule(self, orb, day: date) -> str:
        """When fallback `orb` becomes due, for messages."""

        if self._gps_only_gate(orb, day):
            return (
                f"GPS only: once the day is more than "
                f"{GPS_ONLY_FALLBACK_AGE_S / 86400:.0f} days old"
            )
        return (
            f"from the 3rd attempt at least {FALLBACK_RETRY_MIN_AGE_S / 3600:.0f} h "
            f"after the day, or {FALLBACK_MIN_AGE_S / 3600:.0f} h after it"
        )

    def _orbit_plan(self, day: date, retry_count: int) -> tuple[list, list, str]:
        """
        The orbits to try for `day`, in order: the configured one
        (None: gnssrefl's default), then each due fallback
        (_fallback_due()). A fallback that is the same product as an
        orbit already listed, after rinex2snr's renames, is left out
        (with no orbit configured, "rapid" after 2025/168 is a
        different product from the default "gnss"; before, it is the
        default itself).

        Returns (plan, pending, why): pending is the fallbacks not due
        yet, why a few words on the attempt for the log.
        """

        primary = self._orbit_source
        plan = [primary]
        pending: list[str] = []
        seen = {_canonical_orbit(primary, day)}
        age_s = day_age_s(day)

        for orb in self._orbit_fallbacks:
            canonical = _canonical_orbit(orb, day)
            if canonical in seen:
                continue
            seen.add(canonical)
            if self._fallback_due(orb, day, retry_count, age_s):
                plan.append(orb)
            else:
                pending.append(orb)

        why = f"attempt {retry_count + 1}, the day ended {age_s / 3600:.0f} h ago"
        return plan, pending, why

    def _no_snr_message(
        self,
        day: date,
        tried: list[str],
        raised: list,
        pending: list[str],
    ) -> str:
        """
        What to say when no orbit tried made an SNR file. It names
        every orbit tried itself: gnssrefl says "The orbit file you
        requested does not exist." on stdout only, and its .gen log
        is rewritten by every rinex2snr call, so it shows the last
        orbit tried only.
        """

        if raised and len(raised) == len(tried):
            # Every attempt raised: the message rinex2snr failures have
            # always had, which recover_missing_days.py (STAGE_FAILED)
            # reports as "failed" -- a gnssrefl error -- although the
            # outcome is "no_snr" for the pipeline's hand-off rule.
            message = f"rinex2snr failed: {raised[-1][1]}"
            if len(tried) > 1:
                message += f" (orbits tried: {', '.join(tried)})"
            return message

        plural = "s" if len(tried) > 1 else ""
        head = f"no SNR file with orbit{plural} {', '.join(tried)}"
        if pending:
            parts = [
                f"{head}: the orbit may not be published yet, or the RINEX "
                f"has no usable data",
                "fallback orbit(s) not tried yet: " + ", ".join(
                    f"{orb} ({self._fallback_schedule(orb, day)})" for orb in pending
                ),
            ]
        elif not self._orbit_fallbacks:
            parts = [
                f"{head}: the orbit may not be published yet, or the RINEX "
                f"has no usable data",
                "no fallback orbits (gnssrefl_orbit_fallbacks is [])",
            ]
        else:
            parts = [f"{head}: no orbit for this day, or the RINEX has no usable data"]
        for label, exc in raised:
            parts.append(f"rinex2snr raised with {label}: {exc}")
        see = f"see {self._gen_log_path(day)}"
        if len(tried) > 1:
            see += ", which shows the last orbit tried only"
        parts.append(see)

        return (
            f"No results file produced: {self._results_file_path(day)} ("
            + "; ".join(parts) + ")"
        )

    def _snr_files(self, day: date) -> list[Path]:
        """The day's SNR files: gnssrefl gps.snr_name(),
        <sta4><DDD>0.<YY>.snr<type>, gzipped afterwards by default --
        the same names recover_missing_days.py looks for."""

        assert self._refl_code is not None
        doy = day.timetuple().tm_yday
        folder = self._refl_code / str(day.year) / "snr" / self._station_code
        return sorted(folder.glob(f"{self._station_code}{doy:03d}0.{day:%y}.snr*"))

    def _snr_file_made(self, day: date, since: float) -> bool:
        return any(_fresh(p, since) for p in self._snr_files(day))

    def _remove_fresh_snr_files(self, day: date, since: float) -> None:
        """After a rinex2snr attempt that raised: any SNR file it wrote
        may be incomplete (e.g. the disk filled while it gzipped), and
        must not be taken by gnssir, or by recover_missing_days.py as
        evidence that gnssir ran."""

        for path in self._snr_files(day):
            if _fresh(path, since):
                try:
                    path.unlink()
                except OSError as exc:
                    self._logger.warning(
                        "Could not remove SNR file %s left by a failed "
                        "rinex2snr: %s", path, exc
                    )

    def _gen_log_path(self, day: date) -> Path:
        """rinex2snr's log of the day (gps.py:6746-6752), rewritten by every call."""

        assert self._refl_code is not None
        return (
            self._refl_code / "logs" / self._station_code / str(day.year)
            / f"{day.timetuple().tm_yday:03d}_translation.txt.gen"
        )

    def _failqc_path(self, day: date) -> Path:
        """The arcs gnssir's quality control rejected (gnssrefl utils.py:142)."""

        assert self._refl_code is not None
        return (
            self._refl_code / str(day.year) / "results" / self._station_code
            / "failQC" / f"{day.timetuple().tm_yday:03d}.txt"
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
        self, year: int, doy: int, since: float | None = None
    ) -> tuple[bool, int, str, float | None, float | None]:
        """
        Parse the results file: confirm it exists and is non-empty,
        count its data rows, and compute mean reflector height (RH)
        and mean peak-to-noise ratio (PkNoise) across all rows that
        parse cleanly. A row that doesn't parse (wrong number of
        columns, non-numeric RH) is skipped rather than failing the
        whole result -- gnssrefl's own output format, not something
        this module should be strict about beyond what it needs.

        With `since` (time.time() before gnssir ran), a results file
        older than that is not this run's: gnssir leaves an existing
        one untouched when it returns early (no SNR file; an RH range
        under 5 m), and reading it as fresh would report success for
        a run that made nothing.

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

        if since is not None and not _fresh(results_file, since):
            return (
                False,
                0,
                f"gnssir did not replace the existing results file {results_file}",
                None,
                None,
            )

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
        outcome: str = OUTCOME_ERROR,
        orbit_used: str = "",
        orbits_tried: tuple = (),
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
            outcome=outcome,
            orbit_used=orbit_used,
            orbits_tried=orbits_tried,
        )
