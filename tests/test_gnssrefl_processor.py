"""
test_gnssrefl_processor.py

Unit tests for station/gnssrefl_processor.py.

No real gnssrefl installation is required: fake gnssrefl.gnssir_input,
gnssrefl.rinex2snr_cl, and gnssrefl.gnssir_cl modules are installed
into sys.modules before each test, matching how tests/test_rinex.py
fakes convbin -- only the external package is faked, so the actual
staging/environment/error-handling code in gnssrefl_processor.py is
exercised for real, not mocked around.

The fake gnssir() reads the REFL_CODE environment variable itself
(exactly like the real one would) to decide where to write its
results file, so it stays a faithful stand-in rather than a shortcut.

Run with:

    python3 -m unittest discover -s tests -p "test_gnssrefl_processor.py" -v
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))


# ------------------------------------------------------------
# Fake gnssrefl package
# ------------------------------------------------------------

# Shared, resettable behavior control for the fake functions below.
_behavior = {
    "make_gnssir_input": "success",
    "rinex2snr": "success",
    "gnssir": "success",
    "num_tracks": 3,
}


def _reset_behavior() -> None:
    _behavior["make_gnssir_input"] = "success"
    _behavior["rinex2snr"] = "success"
    _behavior["gnssir"] = "success"
    _behavior["num_tracks"] = 3


calls: dict[str, list] = {"make_gnssir_input": [], "rinex2snr": [], "gnssir": []}


def _fake_make_gnssir_input(station, lat=0, lon=0, height=0, **kwargs):
    calls["make_gnssir_input"].append(
        {"station": station, "lat": lat, "lon": lon, "height": height, **kwargs}
    )

    mode = _behavior["make_gnssir_input"]

    if mode == "raise":
        raise RuntimeError("simulated make_gnssir_input failure")
    if mode == "sys_exit":
        raise SystemExit(1)


def _fake_rinex2snr(station, year, doy, **kwargs):
    calls["rinex2snr"].append({"station": station, "year": year, "doy": doy, **kwargs})

    mode = _behavior["rinex2snr"]

    if mode == "raise":
        raise RuntimeError("simulated rinex2snr failure")
    if mode == "sys_exit":
        raise SystemExit(1)


def _fake_gnssir(station, year, doy, **kwargs):
    calls["gnssir"].append({"station": station, "year": year, "doy": doy, **kwargs})

    mode = _behavior["gnssir"]

    if mode == "raise":
        raise RuntimeError("simulated gnssir failure")
    if mode == "sys_exit":
        raise SystemExit(1)
    if mode == "no_output":
        return

    refl_code = Path(os.environ["REFL_CODE"])
    results_dir = refl_code / str(year) / "results" / station
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / f"{doy:03d}.txt"

    if mode == "empty_output":
        results_file.write_text("")
        return

    num_tracks = _behavior["num_tracks"]
    lines = [
        "% station fake https://github.com/kristinemlarson/gnssrefl v0.0.0",
        "% Phase Center corrections have NOT been applied",
        "%(1)  (2)   (3) (4)  (5)     (6)   (7)    (8)    (9)   (10)  (11) (12) (13)    (14)     (15)    (16) (17)",
        "% year, doy, RH, sat,UTCtime, Azim, Amp,  eminO, emaxO,NumbOf,freq,rise,EdotF, PkNoise,  DelT,    MJD, refr",
    ]
    for i in range(num_tracks):
        rh = 5.0 + i  # distinct, predictable RH per row for exact mean assertions
        pk_noise = 3.0 + (0.1 * i)  # distinct, predictable PkNoise per row
        lines.append(
            f"{year} {doy:3d} {rh:6.3f}   4  1.003 306.53   9.25   5.34  25.00 "
            f"3131    1   1  0.68255 {pk_noise:6.2f}   52.20 61230.041782  1"
        )
    results_file.write_text("\n".join(lines) + "\n")


def _install_fake_gnssrefl_package() -> None:
    fake_gnssrefl = types.ModuleType("gnssrefl")
    fake_gnssrefl.__version__ = "fake-1.0.0"

    fake_gnssir_input = types.ModuleType("gnssrefl.gnssir_input")
    fake_gnssir_input.make_gnssir_input = _fake_make_gnssir_input

    fake_rinex2snr_cl = types.ModuleType("gnssrefl.rinex2snr_cl")
    fake_rinex2snr_cl.rinex2snr = _fake_rinex2snr

    fake_gnssir_cl = types.ModuleType("gnssrefl.gnssir_cl")
    fake_gnssir_cl.gnssir = _fake_gnssir

    fake_gnssrefl.gnssir_input = fake_gnssir_input
    fake_gnssrefl.rinex2snr_cl = fake_rinex2snr_cl
    fake_gnssrefl.gnssir_cl = fake_gnssir_cl

    sys.modules["gnssrefl"] = fake_gnssrefl
    sys.modules["gnssrefl.gnssir_input"] = fake_gnssir_input
    sys.modules["gnssrefl.rinex2snr_cl"] = fake_rinex2snr_cl
    sys.modules["gnssrefl.gnssir_cl"] = fake_gnssir_cl


def _uninstall_fake_gnssrefl_package() -> None:
    for name in (
        "gnssrefl",
        "gnssrefl.gnssir_input",
        "gnssrefl.rinex2snr_cl",
        "gnssrefl.gnssir_cl",
    ):
        sys.modules.pop(name, None)


import gnssrefl_processor  # noqa: E402


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------

class FakeConfig:
    """Minimal duck-typed stand-in for config.Config."""

    def __init__(self, products_dir: Path):
        self.products_dir = products_dir
        self.station_id = "USGS001"
        self.latitude = 41.8928336813
        self.longitude = -69.9633123013
        self.height = 21.774
        self.station: dict = {}


class GnssIrProcessorTestCase(unittest.TestCase):

    def setUp(self) -> None:
        _install_fake_gnssrefl_package()
        _reset_behavior()
        calls["make_gnssir_input"].clear()
        calls["rinex2snr"].clear()
        calls["gnssir"].clear()

        # _detect_gnssrefl_version() prefers real installed package
        # metadata over the module's own __version__ attribute (the
        # correct behavior in production -- confirmed necessary
        # against a real gnssrefl install, which doesn't define
        # __version__ at all). That means these tests need real
        # metadata lookups forced to "not installed" so they
        # deterministically exercise the fake module's __version__
        # fallback instead, regardless of whether a real gnssrefl
        # actually happens to be pip-installed in whatever
        # environment runs this suite.
        self._metadata_patcher = unittest.mock.patch(
            "importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError,
        )
        self._metadata_patcher.start()

        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

        self.cfg = FakeConfig(products_dir=self.root / "products")

        self.observation_file = self.root / "test_20260708.obs"
        self.observation_file.write_text("fake RINEX observation content\n")

        self.processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)

    def tearDown(self) -> None:
        self._metadata_patcher.stop()
        _uninstall_fake_gnssrefl_package()
        self.tmpdir.cleanup()


# ------------------------------------------------------------
# initialize()
# ------------------------------------------------------------

class TestInitialize(GnssIrProcessorTestCase):

    def test_initializes_and_sets_environment_variables(self) -> None:
        result = self.processor.initialize()

        self.assertEqual(result, "READY")
        self.assertEqual(os.environ["REFL_CODE"], str(self.processor._refl_code))
        self.assertEqual(os.environ["ORBITS"], str(self.processor._orbits))
        self.assertEqual(os.environ["EXE"], str(self.processor._exe))

    def test_creates_required_directories(self) -> None:
        self.processor.initialize()

        self.assertTrue(self.processor._refl_code.is_dir())
        self.assertTrue(self.processor._orbits.is_dir())
        self.assertTrue(self.processor._exe.is_dir())

    def test_calls_make_gnssir_input_with_real_station_coordinates(self) -> None:
        self.processor.initialize()

        self.assertEqual(len(calls["make_gnssir_input"]), 1)
        call = calls["make_gnssir_input"][0]
        self.assertAlmostEqual(call["lat"], 41.8928336813)
        self.assertAlmostEqual(call["lon"], -69.9633123013)
        self.assertAlmostEqual(call["height"], 21.774)

    def test_all_frequencies_enabled_by_default(self) -> None:
        # Regression: confirmed against a real results file that
        # every one of 20 real retrievals came from GPS alone,
        # despite the RINEX data containing real SNR observables
        # from GLONASS, Galileo, QZSS, and BeiDou too --
        # make_gnssir_input()'s own default (allfreq=False) was
        # silently restricting analysis to GPS only.
        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertTrue(call["allfreq"])

    def test_all_frequencies_configurable_to_false(self) -> None:
        self.cfg.station["gnssrefl_all_frequencies"] = False

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertFalse(call["allfreq"])

    def test_station_code_derived_from_station_id_when_not_configured(self) -> None:
        self.processor.initialize()

        self.assertEqual(self.processor._station_code, "usgs")

    def test_explicit_station_code_takes_precedence(self) -> None:
        self.cfg.station["gnssrefl_station_code"] = "wh01"

        self.processor.initialize()

        self.assertEqual(self.processor._station_code, "wh01")

    def test_raises_if_gnssrefl_not_importable(self) -> None:
        _uninstall_fake_gnssrefl_package()

        # Merely removing sys.modules entries only makes the
        # subsequent `import gnssrefl` fail if the real package
        # isn't *also* genuinely installed on whatever machine runs
        # this test -- which held in initial testing, but not on a
        # real machine with gnssrefl actually pip-installed (where
        # Python would just fall through to importing the real
        # package, defeating this test's purpose entirely, and
        # incidentally explained a mysterious stray gnssrefl EGM96/
        # json-writing side effect that had shown up unexplained in
        # earlier full-suite runs). Setting sys.modules entries to
        # None is the documented, robust way to force ImportError on
        # the next import regardless of what's actually installed.
        poisoned = unittest.mock.patch.dict(
            sys.modules,
            {
                "gnssrefl": None,
                "gnssrefl.gnssir_input": None,
                "gnssrefl.rinex2snr_cl": None,
                "gnssrefl.gnssir_cl": None,
            },
        )

        with poisoned:
            with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
                self.processor.initialize()

    def test_raises_if_make_gnssir_input_fails(self) -> None:
        _behavior["make_gnssir_input"] = "raise"

        with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
            self.processor.initialize()

    def test_raises_if_make_gnssir_input_calls_sys_exit(self) -> None:
        # "never crash" applies to initialize() too -- a SystemExit
        # from research-grade external code must not actually exit
        # the process; it should surface as our own exception type.
        _behavior["make_gnssir_input"] = "sys_exit"

        with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
            self.processor.initialize()

    def test_status_reflects_successful_initialize(self) -> None:
        self.processor.initialize()

        status = self.processor.status()

        self.assertTrue(status.initialized)
        self.assertTrue(status.gnssrefl_importable)
        self.assertEqual(status.gnssrefl_version, "fake-1.0.0")
        self.assertEqual(status.station_code, "usgs")


# ------------------------------------------------------------
# process() -- success path
# ------------------------------------------------------------

class TestProcessSuccess(GnssIrProcessorTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.processor.initialize()

    def test_process_succeeds(self) -> None:
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertTrue(result.success, msg=result.message)
        self.assertEqual(result.station_code, "usgs")
        self.assertEqual(result.day, date(2026, 7, 8))

    def test_rinex_file_staged_with_correct_name_and_location(self) -> None:
        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        # 2026-07-08 is day of year 189. Staged under the 4-char
        # directory ("usgs"), but named with the 9-char station code
        # ("usgs00usa") per the RINEX 3 long filename convention,
        # confirmed against real hardware -- including the "0000"
        # (midnight) time segment, which is always used regardless
        # of the observation file's actual first-epoch time.
        expected = (
            self.processor._refl_code
            / "2026"
            / "rinex"
            / "usgs"
            / "USGS00USA_R_20261890000_01D_01S_MO.rnx"
        )

        self.assertTrue(expected.exists())
        self.assertEqual(
            expected.read_text(), self.observation_file.read_text()
        )

    def test_stale_file_in_working_directory_is_removed_before_staging(
        self,
    ) -> None:
        # Regression: confirmed against real hardware that gnssrefl
        # silently reuses any same-named file already sitting in the
        # current working directory instead of the properly staged
        # one, with no error -- this is the exact bug that cost
        # hours to track down. A stale file with the expected staged
        # name must be removed before every conversion.
        stale = Path.cwd() / "USGS00USA_R_20261890000_01D_01S_MO.rnx"
        stale.write_text("stale content from an earlier run")

        try:
            self.processor.process(self.observation_file, day=date(2026, 7, 8))
            self.assertFalse(stale.exists())
        finally:
            if stale.exists():
                stale.unlink()

    def test_original_observation_file_is_not_moved_or_deleted(self) -> None:
        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        self.assertTrue(self.observation_file.exists())

    def test_rinex2snr_called_with_nolook_true(self) -> None:
        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        self.assertEqual(len(calls["rinex2snr"]), 1)
        call = calls["rinex2snr"][0]
        # The 9-character code, not the 4-character one: confirmed
        # against real hardware that rinex2snr() determines RINEX
        # 2.11 vs RINEX 3 purely by this argument's length.
        self.assertEqual(call["station"], "usgs00usa")
        self.assertEqual(call["year"], 2026)
        self.assertEqual(call["doy"], 189)
        self.assertTrue(call["nolook"])
        self.assertEqual(call["samplerate"], 1)

    def test_gnssir_called_with_correct_station_year_doy(self) -> None:
        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        self.assertEqual(len(calls["gnssir"]), 1)
        call = calls["gnssir"][0]
        # The 4-character code: confirmed against real hardware that
        # gnssir() (unlike rinex2snr()) expects this one, not the
        # 9-character RINEX 3 code.
        self.assertEqual(call["station"], "usgs")
        self.assertEqual(call["year"], 2026)
        self.assertEqual(call["doy"], 189)

    def test_num_tracks_counted_correctly(self) -> None:
        _behavior["num_tracks"] = 7

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertEqual(result.num_tracks, 7)

    def test_reflector_height_is_parsed_as_mean_across_rows(self) -> None:
        # Confirmed real format: column (3) is RH, meters. Fake rows
        # use RH = 5.0, 6.0, 7.0 for num_tracks=3 -> mean 6.0.
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertAlmostEqual(result.reflector_height, 6.0, places=3)

    def test_quality_score_is_parsed_as_mean_peak_to_noise(self) -> None:
        # Confirmed real format: column (14) is PkNoise. Fake rows
        # use PkNoise = 3.0, 3.1, 3.2 for num_tracks=3 -> mean 3.1.
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertAlmostEqual(result.quality_score, 3.1, places=3)

    def test_reflector_height_scales_with_num_tracks(self) -> None:
        _behavior["num_tracks"] = 5
        # RH = 5.0, 6.0, 7.0, 8.0, 9.0 -> mean 7.0

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertAlmostEqual(result.reflector_height, 7.0, places=3)

    def test_soil_moisture_and_snow_depth_are_honestly_none(self) -> None:
        # Genuinely out of scope for a single day's gnssir run --
        # see module docstring. Not a parsing gap like
        # reflector_height/quality_score were.
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertIsNone(result.soil_moisture)
        self.assertIsNone(result.snow_depth)

    def test_malformed_row_is_skipped_not_fatal(self) -> None:
        # A row with too few columns to contain an RH value at all
        # should be skipped, not crash parsing or the whole result.
        refl_code = self.processor._refl_code
        results_dir = refl_code / "2026" / "results" / "usgs"
        results_dir.mkdir(parents=True, exist_ok=True)

        # First, a normal successful process() to get the real
        # results file written, then corrupt one line and re-read.
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )
        self.assertTrue(result.success)

        results_file = results_dir / "189.txt"
        lines = results_file.read_text().splitlines()
        lines.insert(4, "garbled incomplete row")  # too few columns
        results_file.write_text("\n".join(lines) + "\n")

        ok, num_tracks, message, rh, quality = self.processor._read_results(2026, 189)

        self.assertTrue(ok)
        self.assertEqual(num_tracks, 4)  # 3 real rows + 1 malformed
        self.assertIsNotNone(rh)  # still computed from the 3 valid rows

    def test_runtime_is_reported(self) -> None:
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertIsInstance(result.runtime_seconds, float)
        self.assertGreaterEqual(result.runtime_seconds, 0.0)

    def test_gnssrefl_version_is_reported(self) -> None:
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertEqual(result.gnssrefl_version, "fake-1.0.0")

    def test_status_reflects_processed_counts(self) -> None:
        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        status = self.processor.status()

        self.assertEqual(status.files_processed, 1)
        self.assertEqual(status.successful, 1)
        self.assertEqual(status.failed, 0)
        self.assertIsNotNone(status.last_runtime)

    def test_verify_reconfirms_a_successful_result(self) -> None:
        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertTrue(self.processor.verify(result))

    def test_shuts_down_cleanly(self) -> None:
        self.processor.shutdown()

        self.assertFalse(self.processor.status().initialized)

    def test_orbit_source_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_orbit_source"] = "nav"

        offline_processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)
        offline_processor.initialize()
        offline_processor.process(self.observation_file, day=date(2026, 7, 8))

        self.assertEqual(calls["rinex2snr"][-1]["orb"], "nav")


# ------------------------------------------------------------
# process() -- failure modes ("do not assume success" throughout)
# ------------------------------------------------------------

class TestProcessFailureModes(GnssIrProcessorTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.processor.initialize()

    def test_process_before_initialize_never_raises(self) -> None:
        fresh = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)

        result = fresh.process(self.observation_file, day=date(2026, 7, 8))

        self.assertFalse(result.success)
        self.assertIn("initialize()", result.message)

    def test_missing_input_file_never_raises(self) -> None:
        missing = self.root / "does_not_exist.obs"

        result = self.processor.process(missing, day=date(2026, 7, 8))

        self.assertFalse(result.success)
        self.assertIn("does not exist", result.message)

    def test_empty_input_file_is_rejected(self) -> None:
        empty_file = self.root / "empty.obs"
        empty_file.write_text("")

        result = self.processor.process(empty_file, day=date(2026, 7, 8))

        self.assertFalse(result.success)
        self.assertIn("empty", result.message)

    def test_rinex2snr_failure_never_raises(self) -> None:
        _behavior["rinex2snr"] = "raise"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("rinex2snr failed", result.message)

    def test_rinex2snr_sys_exit_never_crashes_process(self) -> None:
        _behavior["rinex2snr"] = "sys_exit"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("rinex2snr failed", result.message)

    def test_gnssir_failure_never_raises(self) -> None:
        _behavior["gnssir"] = "raise"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("gnssir failed", result.message)

    def test_gnssir_sys_exit_never_crashes_process(self) -> None:
        _behavior["gnssir"] = "sys_exit"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("gnssir failed", result.message)

    def test_gnssir_success_but_no_results_file_is_still_a_failure(self) -> None:
        # The exact "do not assume success" principle from
        # rinex_processor.py: gnssir() returning without raising
        # doesn't mean it actually produced anything.
        _behavior["gnssir"] = "no_output"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("No results file", result.message)

    def test_gnssir_success_but_empty_results_file_is_still_a_failure(self) -> None:
        _behavior["gnssir"] = "empty_output"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("empty", result.message)

    def test_failed_processing_is_counted_too(self) -> None:
        _behavior["gnssir"] = "raise"

        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        status = self.processor.status()

        self.assertEqual(status.files_processed, 1)
        self.assertEqual(status.successful, 0)
        self.assertEqual(status.failed, 1)


if __name__ == "__main__":
    unittest.main()
