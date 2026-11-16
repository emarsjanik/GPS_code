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
The fake rinex2snr() likewise writes the day's SNR file where gnssrefl
4.1.5 does ($REFL_CODE/<year>/snr/<sta4>/<sta4><doy>0.<yy>.snr66.gz),
unless told the orbit is missing, and can copy the staged RINEX into
the working directory as the real one does (`cp <staged> .`).

Run with:
    python3 -m unittest discover -s tests -p "test_gnssrefl_processor.py" -v
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
import types
import time
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone
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
    # orbits (rinex2snr's `orb`, None = gnssrefl's default) for which
    # rinex2snr finds no orbit: it prints and returns, no SNR file
    "orbits_without_snr": set(),
    # orbits for which rinex2snr raises
    "orbits_raising": set(),
    # orbits for which rinex2snr writes the SNR file, then raises
    "orbits_raising_after_snr": set(),
    # copy the staged RINEX into the working directory, as gnssrefl does
    "cwd_copy": False,
}


def _reset_behavior() -> None:
    _behavior["make_gnssir_input"] = "success"
    _behavior["rinex2snr"] = "success"
    _behavior["gnssir"] = "success"
    _behavior["num_tracks"] = 3
    _behavior["orbits_without_snr"] = set()
    _behavior["orbits_raising"] = set()
    _behavior["orbits_raising_after_snr"] = set()
    _behavior["cwd_copy"] = False


def _snr_path(station4: str, year: int, doy: int) -> Path:
    """Where gnssrefl 4.1.5 leaves the day's SNR file (gzipped by default)."""
    return (Path(os.environ["REFL_CODE"]) / str(year) / "snr" / station4
            / f"{station4}{doy:03d}0.{year % 100:02d}.snr66.gz")


calls: dict[str, list] = {"make_gnssir_input": [], "rinex2snr": [], "gnssir": []}


# The frequencies gnssrefl 4.1.5 writes for allfreq=True, in its order.
FAKE_FREQS = [1, 5, 20, 101, 102, 201, 205, 206, 207, 208, 301, 302, 305, 306, 307, 308]


def _fake_make_gnssir_input(station, lat=0, lon=0, height=0, **kwargs):
    calls["make_gnssir_input"].append(
        {"station": station, "lat": lat, "lon": lon, "height": height, **kwargs}
    )

    mode = _behavior["make_gnssir_input"]

    if mode == "raise":
        raise RuntimeError("simulated make_gnssir_input failure")
    if mode == "sys_exit":
        raise SystemExit(1)

    # the json, where and as the real one writes it (json.dump, indent=4):
    # one reqAmp per frequency, all the same, and the subdaily_* keys
    path = Path(os.environ["REFL_CODE"]) / "input" / station / f"{station}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    strategy = {"station": station, "freqs": FAKE_FREQS,
                "reqAmp": [kwargs.get("ampl", 5.0)] * len(FAKE_FREQS)}
    for key in ("subdaily_alt_sigma", "subdaily_spline_outlier1", "subdaily_spline_outlier2"):
        strategy[key] = kwargs.get(key)
    with open(path, "w+") as f:
        json.dump(strategy, f, indent=4)


def _fake_rinex2snr(station, year, doy, **kwargs):
    call = {"station": station, "year": year, "doy": doy, **kwargs}
    calls["rinex2snr"].append(call)

    if _behavior["cwd_copy"]:
        # rinex2snr.py:239-249: use the file in the working directory,
        # else `cp $REFL_CODE/<y>/rinex/<sta4>/<r3> .`
        r3 = (f"{station.upper()}_R_{year}{doy:03d}0000_01D_"
              f"{kwargs.get('samplerate', 1):02d}S_MO.rnx")
        local = Path.cwd() / r3
        staged = Path(os.environ["REFL_CODE"]) / str(year) / "rinex" / station[:4].lower() / r3
        call["cwd_existed"] = local.exists()
        if not local.exists() and staged.exists():
            shutil.copy(staged, local)
        call["cwd_content"] = local.read_text() if local.exists() else None

    mode = _behavior["rinex2snr"]

    if mode == "raise" or kwargs.get("orb") in _behavior["orbits_raising"]:
        raise RuntimeError("simulated rinex2snr failure")
    if mode == "sys_exit":
        raise SystemExit(1)
    if kwargs.get("orb") in _behavior["orbits_without_snr"]:
        # gnssrefl prints "The orbit file you requested does not exist."
        # (to stdout only) and returns
        return

    snr = _snr_path(station[:4].lower(), year, doy)
    snr.parent.mkdir(parents=True, exist_ok=True)
    snr.write_bytes(b"fake snr\n")
    if kwargs.get("orb") in _behavior["orbits_raising_after_snr"]:
        raise OSError(28, "No space left on device")


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

    # retrieve_rh.py:248-255: the rejected arcs, written whenever the
    # quality control ran (header-only when it rejected none)
    failqc = results_dir / "failQC" / f"{doy:03d}.txt"
    failqc.parent.mkdir(parents=True, exist_ok=True)
    failqc.write_text("% year, doy, RH, sat,UTCtime, Azim, Amp ...\n")

    if mode == "no_arcs":
        # retrieve_rh.py:240-245: no arc kept, the results file is removed
        if results_file.exists():
            results_file.unlink()
        return

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

        # gnssrefl works in the current working directory (and leaves
        # its RINEX copy there): keep that inside the temporary folder.
        self._old_cwd = os.getcwd()
        os.chdir(self.root)

        self.cfg = FakeConfig(products_dir=self.root / "products")

        self.observation_file = self.root / "test_20260708.obs"
        self.observation_file.write_text("fake RINEX observation content\n")

        self.processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)

    def tearDown(self) -> None:
        self._metadata_patcher.stop()
        _uninstall_fake_gnssrefl_package()
        os.chdir(self._old_cwd)
        self.tmpdir.cleanup()


# ------------------------------------------------------------
# initialize()
# ------------------------------------------------------------

class TestInitialize(GnssIrProcessorTestCase):

    def test_initialize_returns_ready(self) -> None:
        result = self.processor.initialize()

        self.assertEqual(result, "READY")

    def test_initialize_sets_environment_variables(self) -> None:
        self.processor.initialize()

        self.assertEqual(
            os.environ["REFL_CODE"], str(self.processor._refl_code)
        )
        self.assertEqual(os.environ["ORBITS"], str(self.processor._orbits))
        self.assertEqual(os.environ["EXE"], str(self.processor._exe))

    def test_initialize_creates_required_directories(self) -> None:
        self.processor.initialize()

        self.assertTrue(self.processor._refl_code.is_dir())
        self.assertTrue(self.processor._orbits.is_dir())
        self.assertTrue(self.processor._exe.is_dir())
        self.assertTrue((self.processor._refl_code / "Files").is_dir())

    def test_refl_code_defaults_under_products_dir(self) -> None:
        self.processor.initialize()

        self.assertEqual(
            self.processor._refl_code, self.cfg.products_dir / "refl_code"
        )

    def test_refl_code_override_from_station_json(self) -> None:
        override = self.root / "custom_refl_code"
        self.cfg.station["gnssrefl_refl_code"] = str(override)

        self.processor.initialize()

        self.assertEqual(self.processor._refl_code, override)

    def test_station_code_derived_from_station_id_when_not_configured(self) -> None:
        self.processor.initialize()

        self.assertEqual(self.processor._station_code, "usgs")

    def test_station_code_explicit_override(self) -> None:
        self.cfg.station["gnssrefl_station_code"] = "abcd"

        self.processor.initialize()

        self.assertEqual(self.processor._station_code, "abcd")

    def test_station_code_too_short_falls_back_to_placeholder(self) -> None:
        self.cfg.station_id = "AB"

        self.processor.initialize()

        self.assertEqual(len(self.processor._station_code), 4)

    def test_station_code_9ch_built_from_monument_and_country(self) -> None:
        self.processor.initialize()

        self.assertEqual(self.processor._station_code_9ch, "usgs00usa")

    def test_station_code_9ch_respects_configured_monument_and_country(self) -> None:
        self.cfg.station["gnssrefl_monument_number"] = "01"
        self.cfg.station["gnssrefl_country_code"] = "gbr"

        self.processor.initialize()

        self.assertEqual(self.processor._station_code_9ch, "usgs01gbr")

    def test_gnssrefl_not_importable_raises(self) -> None:
        # Setting a module to None in sys.modules is the standard,
        # documented way to force ImportError on the next `import`
        # of that name, regardless of whether a real package is
        # actually installed on disk. Confirmed necessary: simply
        # uninstalling the fake package (as an earlier version of
        # this test did) is not sufficient on a machine with the
        # real gnssrefl genuinely pip-installed (as any real
        # station's own venv will have, including this project's
        # own) -- Python's import machinery just falls back to the
        # real package instead of failing, silently triggering real
        # network calls (a real EGM96 download) from inside a unit
        # test, and the test then fails for an unrelated reason
        # (make_gnssir_input succeeding, not the intended "gnssrefl
        # itself is not importable" scenario) -- caught directly on
        # real hardware, not assumed.
        _uninstall_fake_gnssrefl_package()

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "gnssrefl": None,
                "gnssrefl.gnssir_input": None,
                "gnssrefl.rinex2snr_cl": None,
                "gnssrefl.gnssir_cl": None,
            },
        ):
            with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
                self.processor.initialize()

    def test_make_gnssir_input_failure_raises(self) -> None:
        _behavior["make_gnssir_input"] = "raise"

        with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
            self.processor.initialize()

    def test_make_gnssir_input_sys_exit_raises_processor_error(self) -> None:
        _behavior["make_gnssir_input"] = "sys_exit"

        with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
            self.processor.initialize()

    def test_make_gnssir_input_called_with_real_coordinates(self) -> None:
        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["lat"], self.cfg.latitude)
        self.assertEqual(call["lon"], self.cfg.longitude)
        self.assertEqual(call["height"], self.cfg.height)

    def test_make_gnssir_input_called_with_allfreq_true_by_default(self) -> None:
        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertTrue(call["allfreq"])

    def test_allfreq_can_be_disabled_via_station_json(self) -> None:
        self.cfg.station["gnssrefl_all_frequencies"] = False

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertFalse(call["allfreq"])

    def test_elevation_mask_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_elevation_min"] = 5.0
        self.cfg.station["gnssrefl_elevation_max"] = 15.0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["e1"], 5.0)
        self.assertEqual(call["e2"], 15.0)

    def test_elevation_mask_not_passed_when_unconfigured(self) -> None:
        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertNotIn("e1", call)
        self.assertNotIn("e2", call)

    def test_reflector_height_range_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_reflector_height_min"] = -0.5
        self.cfg.station["gnssrefl_reflector_height_max"] = 5.0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["h1"], -0.5)
        self.assertEqual(call["h2"], 5.0)

    def test_nr1_nr2_set_when_both_h1_and_h2_configured(self) -> None:
        # Confirmed, real bug this guards against: gnssir's
        # noise-floor estimation reads nr1/nr2 as SEPARATE config
        # fields from h1/h2, not automatically derived from them --
        # confirmed directly via inspect.signature(make_gnssir_input)
        # against the real installed package. Without nr1/nr2 set,
        # every arc's noise-floor sample comes back empty and every
        # single arc silently fails quality control.
        self.cfg.station["gnssrefl_reflector_height_min"] = -0.5
        self.cfg.station["gnssrefl_reflector_height_max"] = 5.0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["nr1"], -0.5)
        self.assertEqual(call["nr2"], 5.0)
        # And NOT a combined "NReg" keyword -- confirmed the real
        # parameter names are nr1/nr2, not a single "NReg" list; an
        # earlier version of this fix assumed the latter, which is
        # not a valid parameter on the real function and raised
        # TypeError on every call.
        self.assertNotIn("NReg", call)

    def test_nr1_nr2_not_set_when_only_h1_configured(self) -> None:
        # Deliberately left unset when only one of h1/h2 is given,
        # so gnssrefl's own default RH search behavior (presumably
        # self-consistent with its own default nr1/nr2) applies to
        # the whole reflector-height configuration together, rather
        # than mixing one explicit bound with unrelated defaults.
        self.cfg.station["gnssrefl_reflector_height_min"] = 2.0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertNotIn("nr1", call)
        self.assertNotIn("nr2", call)

    def test_nr1_nr2_not_set_when_neither_h1_nor_h2_configured(self) -> None:
        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertNotIn("nr1", call)
        self.assertNotIn("nr2", call)

    def test_azimuth_regions_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_azimuth_regions"] = [353, 360, 0, 173]

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["azlist2"], [353.0, 360.0, 0.0, 173.0])

    def test_orthometric_height_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_orthometric_height"] = 19.014

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        # Hortho MUST be a list, not a bare float. gnssrefl declares
        # it as a list (several values with matching Hdates, for
        # antennas that get moved) and guards the assignment with
        # `if type(Hortho) == list`. A float fails that check and is
        # discarded WITHOUT ANY ERROR, after which gnssrefl quietly
        # substitutes its own EGM96-derived height.
        #
        # Not hypothetical: this test previously asserted a bare
        # float and passed, while production discarded the station's
        # surveyed 18.665 m and used 18.625 m -- making every
        # published water level 4 cm low, invisibly, for months.
        #
        # Do not "simplify" this back to a float.
        self.assertEqual(call["Hortho"], [19.014])
        self.assertIsInstance(call["Hortho"], list)

    def test_refraction_model_passed_through_when_configured(self) -> None:
        # gnssrefl 4.1.5: refraction is the on/off bool, refr_model the
        # model as a string. A model number in `refraction` is truthy
        # and leaves refr_model at "1", so 6 (MPF) silently ran Bennett.
        self.cfg.station["gnssrefl_refraction_model"] = 6

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["refr_model"], "6")
        self.assertNotIn("refraction", call)

    def test_refraction_model_zero_turns_refraction_off(self) -> None:
        self.cfg.station["gnssrefl_refraction_model"] = 0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertIs(call["refraction"], False)
        self.assertEqual(call["refr_model"], "0")

    def test_refraction_model_by_name_reaches_gnssrefl(self) -> None:
        # gnssrefl takes NITE and MPF by name; int() used to stop
        # initialize() with a bare ValueError.
        self.cfg.station["gnssrefl_refraction_model"] = "MPF"

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["refr_model"], "MPF")
        self.assertNotIn("refraction", call)

    def test_refraction_model_not_a_model_is_a_processor_error(self) -> None:
        self.cfg.station["gnssrefl_refraction_model"] = "Bennett"

        with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
            self.processor.initialize()

    def test_refraction_left_to_gnssrefl_when_unset(self) -> None:
        self.cfg.station.pop("gnssrefl_refraction_model", None)

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertNotIn("refraction", call)
        self.assertNotIn("refr_model", call)

    def test_max_arc_minutes_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_max_arc_minutes"] = 40.0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["delTmax"], 40.0)

    def test_elevation_span_tolerance_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_elevation_span_tolerance"] = 1.0

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["ediff"], 1.0)

    def test_direct_signal_poly_order_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_direct_signal_poly_order"] = 2

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertEqual(call["polyV"], 2)

    # ---- opt-in arc-level QC ------------------------------------------

    def _gnssir_json_path(self) -> Path:
        return self.root / "products" / "refl_code" / "input" / "usgs" / "usgs.json"

    def test_arc_qc_left_to_gnssrefl_when_unset(self) -> None:
        # Unset (the default): not one more argument, so gnssrefl writes
        # exactly the json it wrote before these keys existed.
        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        for key in ("subdaily_alt_sigma", "subdaily_spline_outlier1",
                    "subdaily_spline_outlier2"):
            self.assertNotIn(key, call)
        written = json.loads(self._gnssir_json_path().read_text())
        self.assertEqual(written["reqAmp"], [5.0] * len(FAKE_FREQS))

    def test_subdaily_arc_qc_passed_through_when_configured(self) -> None:
        self.cfg.station["gnssrefl_subdaily_alt_sigma"] = True
        self.cfg.station["gnssrefl_subdaily_spline_outlier1_m"] = 0.4
        self.cfg.station["gnssrefl_subdaily_spline_outlier2_m"] = "0.3"

        self.processor.initialize()

        call = calls["make_gnssir_input"][0]
        self.assertIs(call["subdaily_alt_sigma"], True)
        self.assertEqual(call["subdaily_spline_outlier1"], 0.4)
        self.assertEqual(call["subdaily_spline_outlier2"], 0.3)

    def test_alt_sigma_as_a_quoted_false_is_false(self) -> None:
        # bool("false") is True: a quoted value must be read, not coerced
        self.cfg.station["gnssrefl_subdaily_alt_sigma"] = "false"

        self.processor.initialize()

        self.assertIs(calls["make_gnssir_input"][0]["subdaily_alt_sigma"], False)

    def test_bad_arc_qc_values_are_refused(self) -> None:
        for key, value in (("gnssrefl_subdaily_alt_sigma", "maybe"),
                           ("gnssrefl_subdaily_spline_outlier1_m", 0),
                           ("gnssrefl_subdaily_spline_outlier2_m", "three sigma"),
                           ("gnssrefl_amplitude_min_by_frequency", [6.0, 5.0]),
                           ("gnssrefl_amplitude_min_by_frequency", {"1": -2})):
            with self.subTest(key=key, value=value):
                self.cfg.station = {key: value}
                processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)
                with self.assertRaises(gnssrefl_processor.GnssIrProcessorError):
                    processor.initialize()

    def test_amplitude_by_frequency_rewrites_req_amp_in_frequency_order(self) -> None:
        # make_gnssir_input can only write one value for every frequency;
        # gnssir applies reqAmp[i] to freqs[i], so the listed frequencies
        # get theirs at their own position and the rest keep gnssrefl's.
        self.cfg.station["gnssrefl_amplitude_min"] = 5.0
        self.cfg.station["gnssrefl_amplitude_min_by_frequency"] = {
            "1": 7.5, "20": "4.5", 308: 6,
        }

        self.processor.initialize()

        written = json.loads(self._gnssir_json_path().read_text())
        expected = [5.0] * len(FAKE_FREQS)
        expected[FAKE_FREQS.index(1)] = 7.5
        expected[FAKE_FREQS.index(20)] = 4.5
        expected[FAKE_FREQS.index(308)] = 6.0
        self.assertEqual(written["reqAmp"], expected)
        self.assertEqual(written["freqs"], FAKE_FREQS)
        self.assertEqual(written["station"], "usgs")
        # gnssrefl's own layout: the same file, rewritten whole
        self.assertTrue(self._gnssir_json_path().read_text().startswith('{\n    "station"'))

    def test_amplitude_for_a_frequency_not_analysed_is_ignored(self) -> None:
        # GPS L2P (2) is not in gnssrefl's allfreq list
        self.cfg.station["gnssrefl_amplitude_min_by_frequency"] = {"2": 9.0, "1": 6.0}

        with self.assertLogs("station.gnssir", level="WARNING") as logs:
            self.processor.initialize()

        written = json.loads(self._gnssir_json_path().read_text())
        self.assertEqual(written["reqAmp"][0], 6.0)
        self.assertEqual(len(written["reqAmp"]), len(FAKE_FREQS))
        self.assertTrue(any("[2]" in line for line in logs.output))

    # ---- orbit fallbacks ------------------------------------------------

    def test_orbit_fallbacks_default_to_rapid_then_nav(self) -> None:
        self.processor.initialize()

        self.assertEqual(self.processor._orbit_fallbacks, ("rapid", "nav"))

    def test_orbit_fallbacks_are_trimmed_lowercased_and_deduplicated(self) -> None:
        # more lenient than gnssrefl, which compares exactly: the names
        # are normalized here, before rinex2snr ever sees them
        for value, want in ((["Rapid", " nav "], ("rapid", "nav")),
                            (["gfr", "gfr"], ("gfr",)),
                            ([], ())):
            with self.subTest(value=value):
                self.cfg.station = {"gnssrefl_orbit_fallbacks": value}
                processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)
                processor.initialize()
                self.assertEqual(processor._orbit_fallbacks, want)

    def test_bad_orbit_fallbacks_are_refused_by_name(self) -> None:
        # rinex2snr only prints for an orbit it does not know, and
        # "rapid,nav" would be a list of letters
        for value in ("rapid,nav", "rapid", 3, {"a": 1}, ["rapid", "bogus"], [None], [7]):
            with self.subTest(value=value):
                self.cfg.station = {"gnssrefl_orbit_fallbacks": value}
                processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)
                with self.assertRaises(gnssrefl_processor.GnssIrProcessorError) as caught:
                    processor.initialize()
                self.assertIn("gnssrefl_orbit_fallbacks", str(caught.exception))

    def test_an_orbit_source_gnssrefl_does_not_know_is_said(self) -> None:
        # not refused (an old setting), but rinex2snr refuses "Rapid" as given
        self.cfg.station["gnssrefl_orbit_source"] = "Rapid"

        with self.assertLogs("station.gnssir", level="WARNING") as logs:
            self.processor.initialize()

        self.assertTrue(any("'Rapid' is not an orbit" in line for line in logs.output))

    def test_status_before_initialize(self) -> None:
        status = self.processor.status()

        self.assertFalse(status.initialized)

    def test_status_after_initialize(self) -> None:
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
        # Tests _stage_rinex_file() directly, in isolation, rather
        # than via process(): process() now removes its staged file
        # once done with it (see
        # test_staged_rinex_file_is_cleaned_up_after_processing
        # below), so checking staging behavior through process()
        # itself would only ever observe an already-deleted file.
        staged_path = self.processor._stage_rinex_file(
            self.observation_file, day=date(2026, 7, 8)
        )

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

        self.assertEqual(staged_path, expected)
        self.assertTrue(expected.exists())
        self.assertEqual(
            expected.read_text(), self.observation_file.read_text()
        )

    def test_staged_rinex_file_is_cleaned_up_after_processing(self) -> None:
        # Confirmed, real bug this guards against: without this
        # cleanup, the staged RINEX copy (~350-400MB per day on real
        # hardware, since it's a full copy of the observation file)
        # was never removed afterward -- the only existing cleanup
        # (_stage_rinex_file() removing a same-named stale file
        # before staging a new one) never triggers for a day that's
        # processed exactly once, which is the normal case. Confirmed
        # directly on real hardware: 29 accumulated files, 9.3GB,
        # after several weeks of ordinary, non-reprocessing
        # operation, with no upper bound on further growth.
        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        staged = (
            self.processor._refl_code
            / "2026"
            / "rinex"
            / "usgs"
            / "USGS00USA_R_20261890000_01D_01S_MO.rnx"
        )

        self.assertFalse(staged.exists())

    def test_stale_file_in_working_directory_is_removed_before_staging(
        self,
    ) -> None:
        # Regression: confirmed against real hardware that gnssrefl
        # silently reuses any same-named file already sitting in the
        # current working directory instead of the properly staged
        # one, with no error -- this is the exact bug that cost
        # hours to track down. A stale file with the expected staged
        # name must be removed before every conversion.
        _behavior["cwd_copy"] = True
        stale = Path.cwd() / "USGS00USA_R_20261890000_01D_01S_MO.rnx"
        stale.write_text("stale content from an earlier run")

        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        # rinex2snr read the day just staged, not the stale file ...
        self.assertEqual(calls["rinex2snr"][0]["cwd_content"],
                         self.observation_file.read_text())
        # ... and its own copy is gone afterwards too
        self.assertFalse(stale.exists())

    def test_stale_compressed_forms_are_removed_before_staging(self) -> None:
        # rinex2snr unpacks <name>.gz and <name minus .rnx>.crx.gz into
        # the name it reads, before looking for it (rinex2snr.py:231-237)
        stale = [Path.cwd() / "USGS00USA_R_20261890000_01D_01S_MO.rnx.gz",
                 Path.cwd() / "USGS00USA_R_20261890000_01D_01S_MO.crx.gz"]
        for path in stale:
            path.write_text("stale")

        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        for path in stale:
            self.assertFalse(path.exists(), path.name)

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

    def test_staged_rinex_file_is_cleaned_up_even_when_rinex2snr_fails(
        self,
    ) -> None:
        # The more important half of the cleanup guarantee: some of
        # the 29 leaked files found on real hardware came from
        # failed/retried runs, not just successful ones, since the
        # original code never cleaned up regardless of outcome. The
        # staged file must be removed even when processing fails
        # immediately after staging.
        _behavior["rinex2snr"] = "raise"

        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        staged = (
            self.processor._refl_code
            / "2026"
            / "rinex"
            / "usgs"
            / "USGS00USA_R_20261890000_01D_01S_MO.rnx"
        )

        self.assertFalse(staged.exists())

    def test_gnssir_failure_never_raises(self) -> None:
        _behavior["gnssir"] = "raise"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("gnssir failed", result.message)
        # an "error", not a lack of data: retried on every run and never
        # handed to recover_missing_days.py, whose exit status would hide it
        self.assertEqual(result.outcome, gnssrefl_processor.OUTCOME_ERROR)
        self.assertEqual(result.orbit_used, "gnss (gnssrefl default)")

    def test_gnssir_sys_exit_never_crashes_process(self) -> None:
        _behavior["gnssir"] = "sys_exit"

        result = self.processor.process(
            self.observation_file, day=date(2026, 7, 8)
        )

        self.assertFalse(result.success)
        self.assertIn("gnssir failed", result.message)
        self.assertEqual(result.outcome, gnssrefl_processor.OUTCOME_ERROR)
        self.assertEqual(result.orbit_used, "gnss (gnssrefl default)")

    def test_staged_rinex_file_is_cleaned_up_even_when_gnssir_fails(self) -> None:
        _behavior["gnssir"] = "raise"

        self.processor.process(self.observation_file, day=date(2026, 7, 8))

        staged = (
            self.processor._refl_code
            / "2026"
            / "rinex"
            / "usgs"
            / "USGS00USA_R_20261890000_01D_01S_MO.rnx"
        )

        self.assertFalse(staged.exists())

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


# ------------------------------------------------------------
# Orbit fallback: a day whose orbit never comes (2026-11-05)
# ------------------------------------------------------------

DAY = date(2026, 7, 8)                                          # day of year 189
DAY_END = datetime(2026, 7, 9, tzinfo=timezone.utc)
YOUNG = DAY_END + timedelta(hours=12)
MID = DAY_END + timedelta(hours=48)              # the midnight run of the 3rd attempt
OLD = DAY_END + timedelta(hours=72)
FIVE_DAYS = DAY_END + timedelta(days=5)
PAST_5_DAYS = FIVE_DAYS + timedelta(seconds=1)
DEFAULT = "gnss (gnssrefl default)"


class FallbackTestCase(GnssIrProcessorTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.processor.initialize()
        refl = self.processor._refl_code
        self.R = refl / "2026" / "results" / "usgs" / "189.txt"
        self.G = refl / "logs" / "usgs" / "2026" / "189_translation.txt.gen"
        self.F = refl / "2026" / "results" / "usgs" / "failQC" / "189.txt"
        self.staged = refl / "2026" / "rinex" / "usgs" / "USGS00USA_R_20261890000_01D_01S_MO.rnx"

    def process(self, now: datetime, retry_count: int = 0, processor=None, day: date = DAY):
        processor = processor or self.processor
        with unittest.mock.patch.object(gnssrefl_processor, "_utcnow", return_value=now):
            return processor.process(self.observation_file, day=day, retry_count=retry_count)

    def orbs(self) -> list:
        return [c["orb"] for c in calls["rinex2snr"]]

    def fresh_processor(self, **station):
        self.cfg.station = dict(station)
        processor = gnssrefl_processor.GnssIrProcessor(cfg=self.cfg)
        processor.initialize()
        calls["rinex2snr"].clear()
        return processor


class TestOrbitFallback(FallbackTestCase):

    def test_a_young_day_gets_its_own_orbit_only(self) -> None:
        _behavior["orbits_without_snr"] = {None}

        result = self.process(YOUNG)

        self.assertEqual(self.orbs(), [None])
        self.assertEqual(calls["gnssir"], [])
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "no_snr")
        self.assertEqual(result.orbits_tried, (DEFAULT,))
        self.assertTrue(result.message.startswith(
            f"No results file produced: {self.R} (no SNR file with orbit {DEFAULT}: "
            f"the orbit may not be published yet"), result.message)
        self.assertIn(str(self.G), result.message)
        self.assertIn("fallback orbit(s) not tried yet: rapid (from the 3rd attempt at "
                      "least 36 h after the day, or 72 h after it), nav (GPS only: once "
                      "the day is more than 5 days old)", result.message)
        self.assertNotIn("last orbit tried only", result.message)

    def test_the_3rd_attempt_tries_rapid_48_h_after_the_day(self) -> None:
        _behavior["orbits_without_snr"] = {None}

        self.process(MID, retry_count=1)
        self.assertEqual(self.orbs(), [None])

        calls["rinex2snr"].clear()
        with self.assertLogs("station.gnssir", level="INFO") as logs:
            result = self.process(MID, retry_count=2)

        self.assertEqual(self.orbs(), [None, "rapid"])
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.orbit_used, "rapid")
        self.assertEqual(result.orbits_tried, (DEFAULT, "rapid"))
        self.assertEqual(len(calls["gnssir"]), 1)
        self.assertTrue(result.message.endswith(
            f"; orbit rapid (fallback: {DEFAULT} made no SNR file; GPS, GLONASS and "
            f"Galileo; no BeiDou)"), result.message)
        # a day made with a lesser orbit is said louder than a success
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertTrue(any("with fallback orbit rapid" in r.getMessage() for r in warnings))
        self.assertTrue(any("trying fallback orbit rapid (attempt 3" in line
                            for line in logs.output))

    def test_a_high_count_on_a_young_day_is_not_enough(self) -> None:
        # failures that never reached gnssrefl (convbin, gnssrefl not
        # initialized, reruns) raise the count: not before 36 h
        _behavior["orbits_without_snr"] = {None}

        self.process(YOUNG, retry_count=5)
        self.process(DAY_END + timedelta(hours=36) - timedelta(seconds=1), retry_count=5)
        self.assertEqual(self.orbs(), [None, None])

        self.process(DAY_END + timedelta(hours=36), retry_count=2)
        self.assertEqual(self.orbs(), [None, None, None, "rapid"])

    def test_72_h_after_the_day_whatever_the_count(self) -> None:
        _behavior["orbits_without_snr"] = {None}

        self.process(OLD - timedelta(seconds=1))
        self.assertEqual(self.orbs(), [None])

        calls["rinex2snr"].clear()
        self.process(OLD)
        self.assertEqual(self.orbs(), [None, "rapid"])

    def test_gps_only_nav_only_once_the_day_is_more_than_5_days_old(self) -> None:
        _behavior["orbits_without_snr"] = {None, "rapid"}

        result = self.process(FIVE_DAYS, retry_count=9)
        self.assertEqual(self.orbs(), [None, "rapid"])
        self.assertEqual(result.outcome, "no_snr")
        self.assertIn("nav (GPS only: once the day is more than 5 days old)", result.message)
        self.assertIn("which shows the last orbit tried only", result.message)

        calls["rinex2snr"].clear()
        result = self.process(PAST_5_DAYS)
        self.assertEqual(self.orbs(), [None, "rapid", "nav"])
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.orbit_used, "nav")
        self.assertIn("GPS only, about half the tracks", result.message)

    def test_no_orbit_at_all(self) -> None:
        _behavior["orbits_without_snr"] = {None, "rapid", "nav"}

        result = self.process(PAST_5_DAYS, retry_count=2)

        self.assertEqual(self.orbs(), [None, "rapid", "nav"])
        self.assertEqual(result.outcome, "no_snr")
        self.assertEqual(result.orbits_tried, (DEFAULT, "rapid", "nav"))
        self.assertTrue(result.message.startswith(
            f"No results file produced: {self.R} (no SNR file with orbits {DEFAULT}, "
            f"rapid, nav: no orbit for this day"), result.message)
        self.assertTrue(result.message.endswith(
            f"see {self.G}, which shows the last orbit tried only)"), result.message)
        self.assertEqual(calls["gnssir"], [])
        self.assertFalse(self.staged.exists())

    def test_fallbacks_turned_off(self) -> None:
        processor = self.fresh_processor(gnssrefl_orbit_fallbacks=[])
        _behavior["orbits_without_snr"] = {None}

        result = self.process(PAST_5_DAYS, retry_count=5, processor=processor)

        self.assertEqual(self.orbs(), [None])
        self.assertIn("no fallback orbits (gnssrefl_orbit_fallbacks is [])", result.message)

    def test_a_gps_only_station_needs_no_gate(self) -> None:
        processor = self.fresh_processor(gnssrefl_orbit_fallbacks=["nav"])
        _behavior["orbits_without_snr"] = {None}
        self.process(OLD, processor=processor)
        self.assertEqual(self.orbs(), [None])

        processor = self.fresh_processor(gnssrefl_orbit_source="nav",
                                         gnssrefl_orbit_fallbacks=["nav-sopac"])
        _behavior["orbits_without_snr"] = {"nav"}
        self.process(OLD, processor=processor)
        self.assertEqual(self.orbs(), ["nav", "nav-sopac"])

    def test_a_fallback_that_is_the_same_product_is_skipped(self) -> None:
        processor = self.fresh_processor(gnssrefl_orbit_source="rapid",
                                         gnssrefl_orbit_fallbacks=["gfr", "rapid", "nav"])
        _behavior["orbits_without_snr"] = {"rapid", "gfr", "nav"}
        self.process(PAST_5_DAYS, processor=processor)
        self.assertEqual(self.orbs(), ["rapid", "nav"])

        processor = self.fresh_processor(gnssrefl_orbit_fallbacks=["gnss", "gbm", "rapid"])
        _behavior["orbits_without_snr"] = {None, "rapid"}
        self.process(OLD, processor=processor)
        self.assertEqual(self.orbs(), [None, "rapid"])

    def test_gnss2_is_another_source_not_an_alias(self) -> None:
        processor = self.fresh_processor(gnssrefl_orbit_fallbacks=["gnss2"])
        _behavior["orbits_without_snr"] = {None}
        self.process(OLD, processor=processor)
        self.assertEqual(self.orbs(), [None, "gnss2"])

    def test_a_source_rinex2snr_refuses_keeps_the_working_fallback(self) -> None:
        # "Rapid" is not "rapid" to rinex2snr: it must not remove rapid
        processor = self.fresh_processor(gnssrefl_orbit_source="Rapid")
        _behavior["orbits_without_snr"] = {"Rapid"}
        result = self.process(OLD, processor=processor)
        self.assertEqual(self.orbs(), ["Rapid", "rapid"])
        self.assertEqual(result.orbit_used, "rapid")

    def test_before_2025_the_default_is_rapid_itself(self) -> None:
        day = date(2023, 7, 8)
        _behavior["orbits_without_snr"] = {None, "rapid", "nav"}

        result = self.process(datetime(2023, 7, 20, tzinfo=timezone.utc), day=day)

        self.assertEqual(self.orbs(), [None, "nav"])
        self.assertEqual(result.orbits_tried, ("rapid (gnssrefl default)", "nav"))

    def test_a_stale_snr_file_is_not_this_attempts(self) -> None:
        snr = _snr_path("usgs", 2026, 189)
        snr.parent.mkdir(parents=True, exist_ok=True)
        snr.write_bytes(b"old")
        hour_ago = time.time() - 3600
        os.utime(snr, (hour_ago, hour_ago))
        _behavior["orbits_without_snr"] = {None}

        result = self.process(YOUNG)
        self.assertEqual(result.outcome, "no_snr")
        self.assertEqual(calls["gnssir"], [])

        _behavior["orbits_without_snr"] = {None, "rapid", "nav"}
        calls["rinex2snr"].clear()
        result = self.process(PAST_5_DAYS)
        self.assertEqual(len(calls["rinex2snr"]), 3)
        self.assertEqual(result.outcome, "no_snr")

    def test_an_orbit_that_raises_moves_on_to_the_next(self) -> None:
        _behavior["orbits_raising"] = {None}

        result = self.process(MID, retry_count=2)

        self.assertEqual(self.orbs(), [None, "rapid"])
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.orbit_used, "rapid")

    def test_every_orbit_raising_keeps_the_rinex2snr_failed_message(self) -> None:
        # recover_missing_days.py STAGE_FAILED keys on "rinex2snr failed:"
        _behavior["rinex2snr"] = "raise"

        result = self.process(PAST_5_DAYS, retry_count=2)

        self.assertEqual(len(calls["rinex2snr"]), 3)
        self.assertTrue(result.message.startswith(
            "rinex2snr failed: simulated rinex2snr failure"), result.message)
        self.assertIn(f"(orbits tried: {DEFAULT}, rapid, nav)", result.message)
        self.assertEqual(result.outcome, "no_snr")

    def test_an_snr_file_left_by_a_raising_attempt_is_removed(self) -> None:
        # e.g. the disk filled while gzipping: never gnssir's input, nor
        # recover_missing_days.py's evidence that gnssir ran
        _behavior["orbits_raising_after_snr"] = {None}
        _behavior["orbits_without_snr"] = {"rapid", "nav"}

        result = self.process(PAST_5_DAYS)

        self.assertEqual(result.outcome, "no_snr")
        self.assertFalse(_snr_path("usgs", 2026, 189).exists())
        self.assertEqual(calls["gnssir"], [])
        self.assertTrue(result.message.startswith("No results file produced: "), result.message)
        self.assertIn(f"rinex2snr raised with {DEFAULT}: [Errno 28] No space left on device",
                      result.message)

    def test_no_arc_kept(self) -> None:
        _behavior["gnssir"] = "no_arcs"

        result = self.process(YOUNG)

        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "no_arcs")
        self.assertEqual(result.orbit_used, DEFAULT)
        self.assertEqual(result.message, (
            f"No results file produced: {self.R} (SNR file made with orbit {DEFAULT}, "
            f"but gnssir kept no arc; see {self.F})"))

    def test_no_arc_kept_with_a_gps_only_fallback_is_not_no_data(self) -> None:
        _behavior["orbits_without_snr"] = {None, "rapid"}
        _behavior["gnssir"] = "no_arcs"

        result = self.process(PAST_5_DAYS)

        self.assertEqual(result.outcome, "no_arcs_gps_only")
        self.assertEqual(result.orbit_used, "nav")
        self.assertIn("GPS-only fallback orbit nav", result.message)
        self.assertTrue(result.message.startswith(f"No results file produced: {self.R} ("))

        # the station's own choice of nav is a no-data day as any other
        processor = self.fresh_processor(gnssrefl_orbit_source="nav")
        result = self.process(PAST_5_DAYS, processor=processor)
        self.assertEqual(result.outcome, "no_arcs")

    def test_gnssir_stopping_before_its_quality_control_is_an_error(self) -> None:
        _behavior["gnssir"] = "no_output"

        result = self.process(YOUNG)

        self.assertEqual(result.outcome, "error")
        self.assertTrue(result.message.startswith(f"No results file produced: {self.R} ("))
        self.assertIn("wrote no failQC/189.txt either", result.message)

    def test_a_failqc_file_left_by_an_earlier_run_is_not_no_arcs(self) -> None:
        # gnssrefl writes failQC/<doy>.txt on every quality-control run,
        # successful ones too. One from an earlier run must not turn this
        # run's early stop (no failQC written now) into "no_arcs", which
        # pipeline.py answers by marking the day as having no data for good.
        self.F.parent.mkdir(parents=True, exist_ok=True)
        self.F.write_text("% year, doy, RH, sat,UTCtime, Azim, Amp ...\n")
        day_ago = time.time() - 86400
        os.utime(self.F, (day_ago, day_ago))
        _behavior["gnssir"] = "no_output"

        result = self.process(YOUNG)

        self.assertEqual(result.outcome, "error")
        self.assertIn("wrote no failQC/189.txt either", result.message)

    def test_a_results_file_gnssir_did_not_replace_is_an_error(self) -> None:
        self.R.parent.mkdir(parents=True, exist_ok=True)
        self.R.write_text("% header\n2026 189 19.0 4 1.0 306.5 9.2 5.3 25.0 3131 1 1 0.7 3.1 52.2 61230.0 1\n")
        hour_ago = time.time() - 3600
        os.utime(self.R, (hour_ago, hour_ago))
        _behavior["gnssir"] = "no_output"

        result = self.process(YOUNG)

        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "error")
        self.assertTrue(result.message.startswith(
            "gnssir did not replace the existing results file"), result.message)

    def test_an_ordinary_day_names_its_orbit(self) -> None:
        result = self.process(YOUNG)

        self.assertTrue(result.success)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.orbit_used, DEFAULT)
        self.assertEqual(result.orbits_tried, (DEFAULT,))
        self.assertTrue(result.message.endswith(f"; orbit {DEFAULT}"), result.message)

    def test_process_without_a_retry_count(self) -> None:
        # recover_missing_days.py and ultra_rapid_check.py call it so
        result = self.processor.process(self.observation_file, day=DAY)

        self.assertTrue(result.success, result.message)
        self.assertEqual(len(calls["rinex2snr"]), 1)


class TestOrbitHelpers(unittest.TestCase):

    def test_gnssrefl_default_orbit_by_date(self) -> None:
        # rinex2snr_cl.py:453-472, strictly after each date
        doy = lambda y, d: date(y, 1, 1) + timedelta(days=d - 1)   # noqa: E731
        self.assertEqual(gnssrefl_processor.gnssrefl_default_orbit(doy(2025, 168)), "rapid")
        self.assertEqual(gnssrefl_processor.gnssrefl_default_orbit(doy(2025, 169)), "gnss")
        self.assertEqual(gnssrefl_processor.gnssrefl_default_orbit(doy(2021, 137)), "nav")
        self.assertEqual(gnssrefl_processor.gnssrefl_default_orbit(doy(2021, 138)), "rapid")

    def test_no_data_marker_name_is_not_zero_padded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = gnssrefl_processor.write_no_data_marker(Path(tmp) / "results", date(2026, 1, 5))
            self.assertEqual(marker, Path(tmp) / "results" / "5.no_data")
            self.assertRegex(marker.read_text(),
                             r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ -- no usable retrievals\n$")
        # "" and "." are the working directory: refused, and run from a
        # temporary one, so that a regression writes nothing into the
        # checkout (the test runner's working directory)
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                for empty in ("", "."):
                    with self.assertRaises(ValueError):
                        gnssrefl_processor.write_no_data_marker(empty, date(2026, 1, 5))
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(old_cwd)


# ------------------------------------------------------------
# gnssrefl's copy of the RINEX in the working directory
# ------------------------------------------------------------

class TestWorkingDirectoryCopy(FallbackTestCase):
    """rinex2snr copies the staged RINEX into the working directory and
    never removes it: 29 GB of them in station/ and $HOME by Nov 2026."""

    COPY = "USGS00USA_R_20261890000_01D_01S_MO.rnx"

    def setUp(self) -> None:
        super().setUp()
        _behavior["cwd_copy"] = True

    def test_the_copy_is_removed_after_a_success(self) -> None:
        result = self.process(YOUNG)

        self.assertTrue(result.success)
        self.assertFalse(calls["rinex2snr"][0]["cwd_existed"])
        self.assertEqual(calls["rinex2snr"][0]["cwd_content"], self.observation_file.read_text())
        self.assertFalse((Path.cwd() / self.COPY).exists())

    def test_the_copy_is_removed_when_rinex2snr_raises(self) -> None:
        _behavior["rinex2snr"] = "raise"

        self.process(YOUNG)

        self.assertIsNotNone(calls["rinex2snr"][0]["cwd_content"])
        self.assertFalse((Path.cwd() / self.COPY).exists())

    def test_one_copy_serves_every_orbit_and_is_removed_once(self) -> None:
        _behavior["orbits_without_snr"] = {None, "rapid"}

        self.process(PAST_5_DAYS)

        self.assertEqual([c["cwd_existed"] for c in calls["rinex2snr"]], [False, True, True])
        self.assertFalse((Path.cwd() / self.COPY).exists())

    def test_nothing_else_in_the_working_directory_is_touched(self) -> None:
        other_day = Path.cwd() / "USGS00USA_R_20261880000_01D_01S_MO.rnx"
        notes = Path.cwd() / "notes.txt"
        other_day.write_text("another day")
        notes.write_text("notes")

        self.process(YOUNG)

        self.assertEqual(other_day.read_text(), "another day")
        self.assertEqual(notes.read_text(), "notes")

    def test_an_observation_file_in_the_working_directory_is_kept(self) -> None:
        # processed from the working directory under the staged name:
        # neither before staging nor afterwards is it deleted
        observation = Path.cwd() / self.COPY
        observation.write_text("the day's own RINEX\n")
        self.observation_file = observation

        result = self.process(YOUNG)

        self.assertTrue(result.success, result.message)
        self.assertEqual(observation.read_text(), "the day's own RINEX\n")

    def test_a_working_directory_that_is_gone_is_not_an_error(self) -> None:
        gone = self.root / "gone"
        gone.mkdir()
        os.chdir(gone)
        gone.rmdir()

        self.processor._remove_cwd_copy(self.staged, self.observation_file)   # no exception


if __name__ == "__main__":
    unittest.main()
