"""
test_rinex.py

Unit tests for station/rinex_processor.py.

No real RTKLIB/convbin installation is required: setUp() writes a
small, real, executable "fake convbin" script to a temp directory
and points RinexProcessor at it via a duck-typed config object's
station["convbin_path"]. Because it's a genuine executable invoked
through subprocess.run() exactly like the real thing, these tests
exercise the actual subprocess/verification code paths, not a mock
of them -- only the external binary itself is faked, matching how
tests/test_receiver.py fakes pyserial rather than mocking
Receiver's own methods.

The fake convbin's behavior is controlled by the FAKE_CONVBIN_MODE
environment variable, which subprocess.run() inherits from this
process by default:

    success        -- (default) writes valid RINEX obs/nav/sbas files, exits 0
    bad_exit        -- writes nothing, exits 1 with a stderr message
    garbage_output   -- exits 0, but writes obs/nav files with no RINEX header
    empty_output      -- exits 0, but writes zero-byte obs/nav files
    no_output          -- exits 0, but writes no files at all
    bad_bytes           -- exits 0, writes valid output, but also emits a
                            non-UTF-8 byte on stdout (regression test)

Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))

import rinex_processor  # noqa: E402
from rinex_processor import RinexProcessor, RinexProcessorError  # noqa: E402


# ------------------------------------------------------------
# Fake convbin
# ------------------------------------------------------------

_FAKE_CONVBIN_SOURCE = '''\
#!/usr/bin/env python3
"""Fake convbin for test_rinex.py -- not part of the delivered station software."""
import os
import sys

VALID_HEADER = "     3.05           OBSERVATION DATA    M (MIXED)          RINEX VERSION / TYPE\\n"
INVALID_HEADER = "this is not a rinex file at all\\n"

mode = os.environ.get("FAKE_CONVBIN_MODE", "success")

if len(sys.argv) == 1:
    # No-args "version banner" call, matching real convbin's behavior.
    sys.stderr.write("fake-convbin ver.2.4.3-test\\n")
    sys.exit(2)

args = sys.argv[1:]
obs_path = args[args.index("-o") + 1]
nav_path = args[args.index("-n") + 1]

if mode == "bad_exit":
    sys.stderr.write("simulated convbin failure: bad raw data\\n")
    sys.exit(1)

if mode == "no_output":
    sys.exit(0)

if mode == "empty_output":
    open(obs_path, "w").close()
    open(nav_path, "w").close()
    sys.exit(0)

if mode == "garbage_output":
    with open(obs_path, "w") as f:
        f.write(INVALID_HEADER)
    with open(nav_path, "w") as f:
        f.write(INVALID_HEADER)
    sys.exit(0)

if mode == "bad_bytes":
    # Regression: confirmed against a real overnight recording (built
    # from 34 separately-started append-mode chunks) that convbin can
    # write a non-UTF-8 byte to stdout while processing certain raw
    # files. subprocess.run(..., text=True) with no errors= handling
    # crashed on this with UnicodeDecodeError before our own code ever
    # got to inspect convbin's exit code.
    sys.stdout.buffer.write(b"scanning: 2026/07/09 \\x88 some binary garbage\\n")
    with open(obs_path, "w") as f:
        f.write(VALID_HEADER)
        f.write("fake observation records\\n")
    with open(nav_path, "w") as f:
        f.write(VALID_HEADER)
        f.write("fake navigation records\\n")
    sys.exit(0)

# "success" (default)
with open(obs_path, "w") as f:
    f.write(VALID_HEADER)
    f.write("fake observation records\\n")
with open(nav_path, "w") as f:
    f.write(VALID_HEADER)
    f.write("fake navigation records\\n")
sys.exit(0)
'''


def _write_fake_convbin(directory: Path) -> Path:
    path = directory / "fake_convbin.py"
    path.write_text(_FAKE_CONVBIN_SOURCE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class FakeConfig:
    """
    Minimal duck-typed stand-in for config.Config: RinexProcessor
    reads .raw_dir, .rinex_dir, .station, and (for RINEX header
    metadata) .station_id/.observer/.agency/.receiver_model/
    .receiver_firmware/.latitude/.longitude/.height from it.
    """

    def __init__(self, rinex_dir: Path, convbin_path: Path):
        self.raw_dir = rinex_dir.parent / "raw"
        self.rinex_dir = rinex_dir
        self.station = {
            "convbin_path": str(convbin_path),
            "rinex_version": "3.05",
            "log_level": "DEBUG",
            "marker_name": "USGS001",
            "marker_number": "USGS001",
            "antenna": {
                "model": "TRM115000.10",
                "serial": "12345678",
                "height": 0.05,
                "east_offset": 0.001,
                "north_offset": 0.002,
            },
        }
        self.station_id = "USGS001"
        self.observer = "USGS Woods Hole Coastal and Marine Science Center"
        self.agency = "USGS"
        self.receiver_model = "Unicore UM980"
        self.receiver_firmware = "R4.10Build11833"
        self.latitude = 41.8928336813
        self.longitude = -69.9633123013
        self.height = 21.774


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

class RinexProcessorTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)

        raw_dir = root / "raw"
        rinex_dir = root / "rinex"
        raw_dir.mkdir()

        self.fake_convbin = _write_fake_convbin(root)
        self.cfg = FakeConfig(rinex_dir, self.fake_convbin)

        self.raw_file = raw_dir / "test_20260707.um980"
        self.raw_file.write_bytes(b"fake raw UM980 binary data")

        os.environ.pop("FAKE_CONVBIN_MODE", None)

        self.processor = RinexProcessor(cfg=self.cfg)

    def tearDown(self) -> None:
        os.environ.pop("FAKE_CONVBIN_MODE", None)
        self.tmpdir.cleanup()


class TestInitialize(RinexProcessorTestCase):

    def test_initializes_and_finds_convbin(self) -> None:
        result = self.processor.initialize()

        self.assertEqual(result, "READY")

        status = self.processor.status()
        self.assertTrue(status.initialized)
        self.assertTrue(status.convbin_found)

    def test_creates_rinex_directory_if_missing(self) -> None:
        self.assertFalse(self.cfg.rinex_dir.exists())

        self.processor.initialize()

        self.assertTrue(self.cfg.rinex_dir.is_dir())

    def test_raises_if_convbin_path_is_not_executable(self) -> None:
        not_executable = Path(self.tmpdir.name) / "not_executable.txt"
        not_executable.write_text("not a real executable")
        self.cfg.station["convbin_path"] = str(not_executable)

        with self.assertRaises(RinexProcessorError):
            self.processor.initialize()

    def test_raises_if_convbin_not_found_anywhere(self) -> None:
        self.cfg.station["convbin_path"] = ""

        # Force shutil.which() to find nothing, regardless of what's
        # actually installed on the machine running this test suite.
        original_which = rinex_processor.shutil.which
        rinex_processor.shutil.which = lambda name: None
        try:
            with self.assertRaises(RinexProcessorError):
                self.processor.initialize()
        finally:
            rinex_processor.shutil.which = original_which


class TestConvertSuccess(RinexProcessorTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.processor.initialize()

    def test_convert_known_file_succeeds(self) -> None:
        result = self.processor.convert(self.raw_file)

        self.assertTrue(result.success, msg=result.message)
        self.assertEqual(result.raw_file, self.raw_file)

    def test_output_files_exist_with_expected_names(self) -> None:
        result = self.processor.convert(self.raw_file)

        self.assertEqual(result.observation_file.name, "test_20260707.obs")
        self.assertEqual(result.navigation_file.name, "test_20260707.nav")
        self.assertTrue(result.observation_file.exists())
        self.assertTrue(result.navigation_file.exists())

    def test_rinex_header_is_valid(self) -> None:
        result = self.processor.convert(self.raw_file)

        header = result.observation_file.read_text().splitlines()[0]
        self.assertIn("RINEX VERSION / TYPE", header)

    def test_runtime_is_reported(self) -> None:
        result = self.processor.convert(self.raw_file)

        self.assertIsInstance(result.runtime_seconds, float)
        self.assertGreaterEqual(result.runtime_seconds, 0.0)

    def test_convbin_version_is_reported(self) -> None:
        result = self.processor.convert(self.raw_file)

        self.assertNotEqual(result.convbin_version, "")

    def test_status_reflects_processed_counts(self) -> None:
        self.processor.convert(self.raw_file)

        status = self.processor.status()

        self.assertEqual(status.files_processed, 1)
        self.assertEqual(status.successful, 1)
        self.assertEqual(status.failed, 0)
        self.assertIsNotNone(status.last_runtime)

    def test_verify_reconfirms_a_successful_result(self) -> None:
        result = self.processor.convert(self.raw_file)

        self.assertTrue(self.processor.verify(result))

    def test_shuts_down_cleanly(self) -> None:
        self.processor.shutdown()

        self.assertFalse(self.processor.status().initialized)


class TestStationMetadataInCommand(RinexProcessorTestCase):
    """
    Confirms _build_command() embeds station metadata into the
    convbin header options, rather than leaving them blank.
    """

    def setUp(self) -> None:
        super().setUp()
        self.processor.initialize()

    def test_snr_flag_always_included(self) -> None:
        # Regression: confirmed via real hardware + gnssrefl testing
        # that omitting this flag produces a RINEX file with zero
        # SNR-type observation codes -- convbin's default -- which
        # silently passes every RINEX-level check (real header, real
        # data, valid checksums) but is entirely unusable for
        # GNSS-IR, whose whole premise is analyzing SNR variation.
        command = self._command_for_raw_file()

        self.assertIn("-os", command)

    def _command_for_raw_file(self) -> list[str]:
        obs, nav, sbas = self.processor._create_output_names(self.raw_file)
        return self.processor._build_command(self.raw_file, obs, nav, sbas)

    def test_marker_name_and_number_included(self) -> None:
        command = self._command_for_raw_file()

        self.assertIn("-hm", command)
        self.assertEqual(command[command.index("-hm") + 1], "USGS001")
        self.assertIn("-hn", command)
        self.assertEqual(command[command.index("-hn") + 1], "USGS001")

    def test_observer_and_agency_included(self) -> None:
        command = self._command_for_raw_file()

        value = command[command.index("-ho") + 1]
        self.assertEqual(
            value,
            "USGS Woods Hole Coastal and Marine Science Center/USGS",
        )

    def test_receiver_info_included(self) -> None:
        command = self._command_for_raw_file()

        value = command[command.index("-hr") + 1]
        self.assertEqual(value, "/Unicore UM980/R4.10Build11833")

    def test_antenna_info_and_delta_included(self) -> None:
        command = self._command_for_raw_file()

        antenna_value = command[command.index("-ha") + 1]
        self.assertEqual(antenna_value, "12345678/TRM115000.10")

        delta_value = command[command.index("-hd") + 1]
        self.assertEqual(delta_value, "0.05/0.001/0.002")

    def test_approx_position_is_plausible_ecef(self) -> None:
        command = self._command_for_raw_file()

        value = command[command.index("-hp") + 1]
        x_str, y_str, z_str = value.split("/")
        x, y, z = float(x_str), float(y_str), float(z_str)

        # A real point on Earth's surface: WGS84 semi-major axis is
        # ~6378137m, so the ECEF vector's magnitude should be close
        # to that (within a few km, accounting for latitude/height).
        magnitude = (x ** 2 + y ** 2 + z ** 2) ** 0.5
        self.assertAlmostEqual(magnitude, 6378137.0, delta=25000)

    def test_comment_includes_station_id(self) -> None:
        command = self._command_for_raw_file()

        self.assertIn("-hc", command)
        self.assertEqual(command[command.index("-hc") + 1], "USGS001")

    def test_missing_cfg_attributes_degrade_to_blank_not_crash(self) -> None:
        # A minimal duck-typed cfg that doesn't define the optional
        # metadata attributes at all must still work, just with
        # blank/zero header fields -- not raise AttributeError.
        class BareConfig:
            def __init__(self, raw_dir, rinex_dir, convbin_path):
                self.raw_dir = raw_dir
                self.rinex_dir = rinex_dir
                self.station = {"convbin_path": str(convbin_path)}

        bare_processor = None
        try:
            from rinex_processor import RinexProcessor

            bare_cfg = BareConfig(
                self.cfg.raw_dir, self.cfg.rinex_dir, self.fake_convbin
            )
            bare_processor = RinexProcessor(cfg=bare_cfg)
            bare_processor.initialize()

            obs, nav, sbas = bare_processor._create_output_names(self.raw_file)
            command = bare_processor._build_command(
                self.raw_file, obs, nav, sbas
            )

            self.assertIn("-hm", command)
            self.assertEqual(command[command.index("-hm") + 1], "")
        finally:
            if bare_processor is not None:
                bare_processor.shutdown()


class TestGeodeticToEcef(unittest.TestCase):
    """
    Verified against fundamental, unambiguous WGS84 geometry (not
    receiver- or firmware-dependent), independent of any hardware.
    """

    def test_equator_prime_meridian_sea_level(self) -> None:
        x, y, z = RinexProcessor._geodetic_to_ecef(0.0, 0.0, 0.0)

        self.assertAlmostEqual(x, 6378137.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=3)
        self.assertAlmostEqual(z, 0.0, places=3)

    def test_north_pole_sea_level(self) -> None:
        x, y, z = RinexProcessor._geodetic_to_ecef(90.0, 0.0, 0.0)

        self.assertAlmostEqual(x, 0.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=3)
        self.assertAlmostEqual(z, 6356752.314245, places=3)

    def test_equator_90_east_sea_level(self) -> None:
        x, y, z = RinexProcessor._geodetic_to_ecef(0.0, 90.0, 0.0)

        self.assertAlmostEqual(x, 0.0, places=3)
        self.assertAlmostEqual(y, 6378137.0, places=3)
        self.assertAlmostEqual(z, 0.0, places=3)

    def test_height_adds_radially_at_equator(self) -> None:
        x, _, _ = RinexProcessor._geodetic_to_ecef(0.0, 0.0, 100.0)

        self.assertAlmostEqual(x, 6378137.0 + 100.0, places=3)


class TestConvertFailureModes(RinexProcessorTestCase):
    """
    "Do not assume success because convbin exited successfully" is
    the central warning in the source spec; these tests exist
    specifically to prove that warning is honored.
    """

    def setUp(self) -> None:
        super().setUp()
        self.processor.initialize()

    def test_missing_input_file_never_raises(self) -> None:
        missing = Path(self.tmpdir.name) / "raw" / "does_not_exist.um980"

        result = self.processor.convert(missing)

        self.assertFalse(result.success)
        self.assertIn("does not exist", result.message)

    def test_empty_input_file_is_rejected_before_running_convbin(self) -> None:
        empty_file = Path(self.tmpdir.name) / "raw" / "empty.um980"
        empty_file.write_bytes(b"")

        result = self.processor.convert(empty_file)

        self.assertFalse(result.success)
        self.assertIn("empty", result.message)

    def test_convbin_nonzero_exit_is_a_failure(self) -> None:
        os.environ["FAKE_CONVBIN_MODE"] = "bad_exit"

        result = self.processor.convert(self.raw_file)

        self.assertFalse(result.success)
        self.assertIn("convbin exited", result.message)

    def test_convbin_success_exit_but_no_output_is_still_a_failure(self) -> None:
        os.environ["FAKE_CONVBIN_MODE"] = "no_output"

        result = self.processor.convert(self.raw_file)

        self.assertFalse(result.success)
        self.assertIn("was not created", result.message)

    def test_convbin_success_exit_but_empty_output_is_still_a_failure(self) -> None:
        os.environ["FAKE_CONVBIN_MODE"] = "empty_output"

        result = self.processor.convert(self.raw_file)

        self.assertFalse(result.success)
        self.assertIn("empty", result.message)

    def test_convbin_success_exit_but_no_rinex_header_is_still_a_failure(self) -> None:
        # The single most important case: a zero exit code with
        # output files that exist and are non-empty, but aren't
        # actually valid RINEX.
        os.environ["FAKE_CONVBIN_MODE"] = "garbage_output"

        result = self.processor.convert(self.raw_file)

        self.assertFalse(result.success)
        self.assertIn("RINEX header", result.message)

    def test_non_utf8_bytes_from_convbin_do_not_crash_conversion(self) -> None:
        # Regression: confirmed against a real overnight recording
        # (built from 34 separately-started append-mode chunks) that
        # convbin can write a non-UTF-8 byte to stdout while
        # processing certain raw files. subprocess.run(...,
        # text=True) with no errors= handling crashed with
        # UnicodeDecodeError before convbin's actual exit code or
        # output could even be inspected -- this cost a real,
        # successful day of autonomous recording its GNSS-IR
        # processing entirely.
        os.environ["FAKE_CONVBIN_MODE"] = "bad_bytes"

        result = self.processor.convert(self.raw_file)

        self.assertTrue(result.success, msg=result.message)

    def test_convert_before_initialize_never_raises(self) -> None:
        fresh_processor = RinexProcessor(cfg=self.cfg)

        result = fresh_processor.convert(self.raw_file)

        self.assertFalse(result.success)
        self.assertIn("initialize()", result.message)

    def test_failed_conversions_are_counted_too(self) -> None:
        os.environ["FAKE_CONVBIN_MODE"] = "bad_exit"

        self.processor.convert(self.raw_file)

        status = self.processor.status()

        self.assertEqual(status.files_processed, 1)
        self.assertEqual(status.successful, 0)
        self.assertEqual(status.failed, 1)


if __name__ == "__main__":
    unittest.main()
