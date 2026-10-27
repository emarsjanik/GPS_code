"""
test_station_manager.py

Unit tests for station/station_manager.py.

No real hardware, convbin, or gnssrefl is required: StationManager's
_make_receiver() is deliberately overridable (a small, private hook
added specifically for this), so tests substitute a fake Receiver
directly rather than needing to fake the `serial` module at import
time. A real database.Database (backed by a temp file) is used for
_health_check() tests, since that's exercising a real, existing
database.py method (save_system_health()) that had never been wired
into anything until this module.

Run with:

    python3 -m unittest discover -s tests -p "test_station_manager.py" -v
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))


class _FakeSerialException(Exception):
    """Stand-in for serial.SerialException."""


def _install_fake_serial_module() -> None:
    """
    station_manager.py imports receiver.py, which requires pyserial
    at import time. No real serial port is used anywhere in these
    tests (StationManager._make_receiver() is overridden with
    FakeReceiver instead), so a minimal fake is enough just to let
    the import succeed.
    """

    fake_module = types.ModuleType("serial")
    fake_module.SerialException = _FakeSerialException
    fake_module.Serial = None
    sys.modules["serial"] = fake_module


_install_fake_serial_module()

from config import Config  # noqa: E402
from database import Database  # noqa: E402
from pipeline import PipelineSummary  # noqa: E402
from receiver import ReceiverError  # noqa: E402
import station_manager  # noqa: E402


# ------------------------------------------------------------
# Fakes
# ------------------------------------------------------------

class FakeReceiver:
    """
    Stand-in for receiver.Receiver, substituted via
    StationManager._make_receiver(). Records every record_raw() call
    for assertions, and can be configured to fail its next N calls.
    """

    record_calls: list[dict] = []
    fail_next_n: int = 0

    def __init__(self, cfg=None):
        self.cfg = cfg

    def __enter__(self) -> "FakeReceiver":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def record_raw(self, filename, duration, append=False, enable_logging=False, **kwargs):
        FakeReceiver.record_calls.append(
            {
                "filename": Path(filename),
                "duration": duration,
                "append": append,
                "enable_logging": enable_logging,
            }
        )

        if FakeReceiver.fail_next_n > 0:
            FakeReceiver.fail_next_n -= 1
            raise ReceiverError("simulated recording failure")

        return SimpleNamespace(
            successful=True, bytes_written=1000, messages_written=10
        )

    @classmethod
    def reset(cls) -> None:
        cls.record_calls = []
        cls.fail_next_n = 0


class FakePipeline:
    """Stand-in for pipeline.Pipeline, injected via StationManager(pipeline=...)."""

    def __init__(self):
        self.run_calls = 0
        self.fail_next_n = 0
        self.initialized = False
        self.shutdown_called = False

    def initialize(self) -> str:
        self.initialized = True
        return "READY"

    def run(self) -> PipelineSummary:
        self.run_calls += 1

        if self.fail_next_n > 0:
            self.fail_next_n -= 1
            raise RuntimeError("simulated pipeline failure")

        return PipelineSummary(files_found=1, files_processed=1)

    def shutdown(self) -> None:
        self.shutdown_called = True


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------

class StationManagerTestCase(unittest.TestCase):

    def setUp(self) -> None:
        FakeReceiver.reset()

        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

        self.cfg = Config.__new__(Config)
        self.cfg.project_root = self.root
        self.cfg.station_dir = self.root / "station"
        self.cfg.resource_dir = self.cfg.station_dir / "resources"
        self.cfg.station_json = self.cfg.resource_dir / "station.json"
        self.cfg.receiver_port = "/dev/USB_GPS"
        self.cfg.receiver_baud = 115200
        self.cfg.receiver_timeout = 2.0
        self.cfg.raw_dir = self.root / "raw"
        self.cfg.rinex_dir = self.root / "rinex"
        self.cfg.archive_dir = self.root / "archive"
        self.cfg.products_dir = self.root / "products"
        self.cfg.logs_dir = self.root / "logs"
        self.cfg.reports_dir = self.root / "reports"
        self.cfg.database_dir = self.root / "database"
        self.cfg.station = {}
        for directory in (
            self.cfg.raw_dir,
            self.cfg.rinex_dir,
            self.cfg.archive_dir,
            self.cfg.products_dir,
            self.cfg.logs_dir,
            self.cfg.reports_dir,
            self.cfg.database_dir,
            self.cfg.resource_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.db = Database(
            path=str(self.cfg.database_dir / "station.db"), log_file=None
        )
        self.db.connect()

        self.fake_pipeline = FakePipeline()

        self.manager = station_manager.StationManager(
            cfg=self.cfg, db=self.db, pipeline=self.fake_pipeline
        )
        self.manager._make_receiver = lambda: FakeReceiver(cfg=self.cfg)

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()


# ------------------------------------------------------------
# initialize()
# ------------------------------------------------------------

class TestInitialize(StationManagerTestCase):

    def test_initializes_and_uses_injected_pipeline(self) -> None:
        result = self.manager.initialize()

        self.assertEqual(result, "READY")
        self.assertTrue(self.fake_pipeline.initialized)
        self.assertIs(self.manager._pipeline, self.fake_pipeline)

    def test_reads_configured_chunk_and_retry_settings(self) -> None:
        self.cfg.station["record_raw_chunk_seconds"] = 120
        self.cfg.station["manager_retry_delay_seconds"] = 5

        self.manager.initialize()

        self.assertEqual(self.manager._chunk_seconds, 120.0)
        self.assertEqual(self.manager._retry_delay_seconds, 5.0)

    def test_defaults_used_when_not_configured(self) -> None:
        self.manager.initialize()

        self.assertEqual(self.manager._chunk_seconds, 3600.0)
        self.assertEqual(self.manager._retry_delay_seconds, 60.0)

    def test_status_reflects_successful_initialize(self) -> None:
        self.manager.initialize()

        status = self.manager.status()

        self.assertTrue(status.initialized)
        self.assertFalse(status.running)

    def test_export_retry_window_defaults_to_three_days(self) -> None:
        self.manager.initialize()

        self.assertEqual(self.manager._export_retry_window_days, 3)

    def test_export_retry_window_reads_configured_value(self) -> None:
        self.cfg.station["export_retry_window_days"] = 5

        self.manager.initialize()

        self.assertEqual(self.manager._export_retry_window_days, 5)


# ------------------------------------------------------------
# Chunked recording
# ------------------------------------------------------------

class TestChunkedRecording(StationManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.manager.initialize()
        # Fast, deterministic chunking for tests: pretend there's
        # ample time left in the day, so chunk duration is driven
        # purely by _chunk_seconds (set small below).
        self.manager._seconds_until_next_utc_midnight = lambda: 9999.0
        self.manager._chunk_seconds = 0.0

    def _run_n_chunks_then_rollover(self, day: date, n: int) -> None:
        calls = {"count": 0}

        def fake_today():
            calls["count"] += 1
            if calls["count"] <= n:
                return day
            return day + timedelta(days=1)

        self.manager._today = fake_today

    def test_first_chunk_does_not_append_later_chunks_do(self) -> None:
        today = date(2026, 7, 9)
        self._run_n_chunks_then_rollover(today, n=3)

        self.manager._record_day(today)

        self.assertEqual(len(FakeReceiver.record_calls), 3)
        self.assertFalse(FakeReceiver.record_calls[0]["append"])
        self.assertTrue(FakeReceiver.record_calls[1]["append"])
        self.assertTrue(FakeReceiver.record_calls[2]["append"])

    def test_all_chunks_use_the_same_filename(self) -> None:
        today = date(2026, 7, 9)
        self._run_n_chunks_then_rollover(today, n=3)

        self.manager._record_day(today)

        filenames = {call["filename"] for call in FakeReceiver.record_calls}
        self.assertEqual(len(filenames), 1)
        self.assertIn("20260709", str(filenames.pop()))

    def test_restart_mid_day_appends_to_the_day_s_file(self) -> None:
        # An earlier run (before a restart) recorded part of the day:
        # the first chunk of this run must not truncate it.
        today = date(2026, 7, 9)
        raw = self.manager._raw_file_for_day(today)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\xaa" * 5000)
        self._run_n_chunks_then_rollover(today, n=2)

        self.manager._record_day(today)

        self.assertTrue(all(c["append"] for c in FakeReceiver.record_calls))
        self.assertEqual(raw.stat().st_size, 5000)

    def test_an_empty_day_file_is_started_fresh(self) -> None:
        today = date(2026, 7, 9)
        raw = self.manager._raw_file_for_day(today)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"")
        self._run_n_chunks_then_rollover(today, n=2)

        self.manager._record_day(today)

        self.assertFalse(FakeReceiver.record_calls[0]["append"])
        self.assertTrue(FakeReceiver.record_calls[1]["append"])

    def test_enable_logging_passed_on_every_chunk(self) -> None:
        today = date(2026, 7, 9)
        self._run_n_chunks_then_rollover(today, n=2)

        self.manager._record_day(today)

        self.assertTrue(all(c["enable_logging"] for c in FakeReceiver.record_calls))

    def test_chunks_recorded_today_counter_increments(self) -> None:
        today = date(2026, 7, 9)
        self._run_n_chunks_then_rollover(today, n=4)

        self.manager._record_day(today)

        self.assertEqual(self.manager._chunks_recorded_today, 4)

    def test_stop_requested_ends_recording_before_rollover(self) -> None:
        today = date(2026, 7, 9)
        # Never actually rolls over on its own; only stop() should end it.
        self.manager._today = lambda: today

        original_record_chunk = self.manager._record_one_chunk

        def stop_after_two(raw_file, duration):
            original_record_chunk(raw_file, duration)
            if len(FakeReceiver.record_calls) >= 2:
                self.manager.stop()

        self.manager._record_one_chunk = stop_after_two

        self.manager._record_day(today)

        self.assertEqual(len(FakeReceiver.record_calls), 2)
        self.assertTrue(self.manager._stop_requested)

    def test_chunk_failure_is_logged_and_recording_continues(self) -> None:
        today = date(2026, 7, 9)
        self._run_n_chunks_then_rollover(today, n=3)

        FakeReceiver.fail_next_n = 1

        self.manager._retry_delay_seconds = 0.01

        self.manager._record_day(today)

        # All 3 chunks were still attempted despite one failing.
        self.assertEqual(len(FakeReceiver.record_calls), 3)


# ------------------------------------------------------------
# Pipeline integration
# ------------------------------------------------------------

# ------------------------------------------------------------
# _make_receiver() -- confirmed against real hardware
# ------------------------------------------------------------

class TestMakeReceiver(StationManagerTestCase):
    """
    Regression test for a real bug found during live hardware
    testing: _make_receiver() originally called
    Receiver(cfg=self.cfg), but the real Receiver class doesn't
    accept a cfg argument at all -- it takes device/baudrate/timeout
    directly (confirmed against station.py's own real, working
    usage). The other test classes in this file override
    _make_receiver() entirely with FakeReceiver, which happily
    accepted whatever construction call they were given -- exactly
    why this specific bug slipped past unit testing and was only
    caught by running against real hardware. This test exercises
    the real _make_receiver() method itself, unmocked, so a future
    regression here would be caught by the test suite instead.
    """

    def test_make_receiver_constructs_with_device_baudrate_timeout(self) -> None:
        self.manager.initialize()

        # Restore the real _make_receiver() -- setUp() overrides it
        # with FakeReceiver for every other test in this file.
        real_manager = station_manager.StationManager(
            cfg=self.cfg, db=self.db, pipeline=self.fake_pipeline
        )
        real_manager._initialized = True

        with unittest.mock.patch("station_manager.Receiver") as mock_receiver_cls:
            real_manager._make_receiver()

            mock_receiver_cls.assert_called_once_with(
                device=self.cfg.receiver_port,
                baudrate=self.cfg.receiver_baud,
                timeout=self.cfg.receiver_timeout,
            )


class TestPipelineIntegration(StationManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.manager.initialize()

    def test_pipeline_run_called_and_summary_recorded(self) -> None:
        self.manager._run_pipeline_safely()

        self.assertEqual(self.fake_pipeline.run_calls, 1)
        self.assertIsNotNone(self.manager._last_summary)
        self.assertEqual(self.manager._last_summary.files_processed, 1)

    def test_pipeline_failure_does_not_raise(self) -> None:
        self.fake_pipeline.fail_next_n = 1

        try:
            self.manager._run_pipeline_safely()
        except Exception as exc:  # pragma: no cover - explicit failure message
            self.fail(f"_run_pipeline_safely() raised: {exc}")

        self.assertEqual(self.fake_pipeline.run_calls, 1)


# ------------------------------------------------------------
# Health check
# ------------------------------------------------------------

class TestHealthCheck(StationManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.manager.initialize()

    def test_health_check_writes_a_real_row_via_database_api(self) -> None:
        self.manager._health_check()

        latest = self.db.latest_system_health()

        self.assertIsNotNone(latest)
        self.assertIsNotNone(latest.disk_free)

    def test_health_check_updates_last_health_check_timestamp(self) -> None:
        self.assertIsNone(self.manager._last_health_check)

        self.manager._health_check()

        self.assertIsNotNone(self.manager._last_health_check)

    def test_health_check_never_raises_even_if_database_call_fails(self) -> None:
        def broken_save_system_health(*args, **kwargs):
            raise RuntimeError("simulated database failure")

        self.db.save_system_health = broken_save_system_health

        try:
            self.manager._health_check()
        except Exception as exc:  # pragma: no cover
            self.fail(f"_health_check() raised: {exc}")

    def test_latest_name_handles_none(self) -> None:
        self.assertEqual(station_manager.StationManager._latest_name(None), "")


# ------------------------------------------------------------
# Station code derivation
# ------------------------------------------------------------

class TestStationCode(StationManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.manager.initialize()

    def test_derived_from_station_id_when_not_explicitly_set(self) -> None:
        self.cfg.station["station_id"] = "USGS001"

        self.assertEqual(self.manager._station_code(), "usgs")

    def test_explicit_override_takes_precedence(self) -> None:
        self.cfg.station["station_id"] = "USGS001"
        self.cfg.station["gnssrefl_station_code"] = "wh01"

        self.assertEqual(self.manager._station_code(), "wh01")

    def test_empty_when_station_id_not_set(self) -> None:
        self.assertEqual(self.manager._station_code(), "")


# ------------------------------------------------------------
# Export retry gap -- the real, confirmed fix
# ------------------------------------------------------------

class TestExportRetryGap(StationManagerTestCase):
    """
    Confirmed, real gap this class tests the fix for: the automatic
    daily export used to run regardless of whether a day's GNSS-IR
    processing actually succeeded, which meant any day whose orbit
    product wasn't published yet by export time (routinely a 1-2 day
    delay) was permanently lost -- its raw data got shipped off to
    external storage, and products/ (including the SNR file) got
    wiped, before there was ever a real chance to retry once the
    orbit became available.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg.station["station_id"] = "USGS001"
        self.manager.initialize()

    def _write_results(self, day: date, content: str) -> None:
        doy = day.timetuple().tm_yday
        results_dir = self.cfg.products_dir / str(day.year) / "results" / "usgs"
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / f"{doy}.txt").write_text(content)

    def test_has_real_results_false_when_file_missing(self) -> None:
        day = date(2026, 7, 23)  # doy 204

        self.assertFalse(self.manager._has_real_results_for_day(day))

    def test_has_real_results_false_when_only_comments(self) -> None:
        day = date(2026, 7, 23)
        self._write_results(day, "% header comment\n% another comment\n")

        self.assertFalse(self.manager._has_real_results_for_day(day))

    def test_has_real_results_true_with_real_data_line(self) -> None:
        day = date(2026, 7, 23)
        self._write_results(day, "% header\n2026 204 1.500 100 12.5 1\n")

        self.assertTrue(self.manager._has_real_results_for_day(day))

    def test_day_with_real_results_is_ready_regardless_of_age(self) -> None:
        day = date(2026, 7, 23)
        self._write_results(day, "% header\n2026 204 1.500 100 12.5 1\n")
        self.manager._today = lambda: day  # zero age -- results alone should be enough

        self.assertTrue(self.manager._day_is_ready_to_export(day))

    def test_day_without_results_within_window_is_not_ready(self) -> None:
        # This is the actual bug this whole fix targets: a day one
        # day old, no results yet (orbit likely not published),
        # should NOT be exported yet.
        day = date(2026, 7, 26)
        self.manager._today = lambda: date(2026, 7, 27)  # 1 day old

        self.assertFalse(self.manager._day_is_ready_to_export(day))

    def test_day_without_results_past_window_exports_anyway(self) -> None:
        # Bounds local storage growth: give up waiting eventually.
        day = date(2026, 7, 20)
        self.manager._today = lambda: date(2026, 7, 27)  # 7 days old, past default 3-day window

        self.assertTrue(self.manager._day_is_ready_to_export(day))

    def test_day_exactly_at_window_boundary_is_ready(self) -> None:
        day = date(2026, 7, 24)
        self.manager._today = lambda: date(2026, 7, 27)  # exactly 3 days old

        self.assertTrue(self.manager._day_is_ready_to_export(day))

    def test_export_actually_skipped_for_recent_day_without_results(self) -> None:
        # Full integration: confirms _export_day_to_external_storage
        # itself (not just the helper methods) leaves data in place.
        self.external_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.external_root, ignore_errors=True)
        self.cfg.station["external_storage_path"] = str(self.external_root)
        self.manager.initialize()

        day = date(2026, 7, 26)
        self.manager._today = lambda: date(2026, 7, 27)  # 1 day old, no results

        raw_file = self.cfg.raw_dir / "station_20260726.um980"
        raw_file.write_bytes(b"real raw data, not yet processed")

        self.manager._export_day_to_external_storage(day)

        # Nothing moved -- left in place for a future retry.
        self.assertTrue(raw_file.exists())
        exported = self.external_root / "Raw" / "station_20260726.um980"
        self.assertFalse(exported.exists())

    def test_export_proceeds_once_real_results_exist(self) -> None:
        self.external_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.external_root, ignore_errors=True)
        self.cfg.station["external_storage_path"] = str(self.external_root)
        self.manager.initialize()

        day = date(2026, 7, 26)
        self.manager._today = lambda: day  # zero age -- results alone should suffice
        self._write_results(day, "% header\n2026 207 1.500 100 12.5 1\n")

        raw_file = self.cfg.raw_dir / "station_20260726.um980"
        raw_file.write_bytes(b"real raw data, now processed")

        self.manager._export_day_to_external_storage(day)

        exported = self.external_root / "Raw" / "station_20260726.um980"
        self.assertTrue(exported.exists())
        self.assertFalse(raw_file.exists())


# ------------------------------------------------------------
# run() / stop() / status() / shutdown() lifecycle
# ------------------------------------------------------------

# ------------------------------------------------------------
# External storage export
# ------------------------------------------------------------

class TestExternalStorageExport(StationManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.external_root = Path(tempfile.mkdtemp())
        self.cfg.station["external_storage_path"] = str(self.external_root)
        self.manager.initialize()
        # Confirmed necessary after adding the export-retry-gap fix:
        # these tests are all about export *mechanics* (does the
        # right file move to the right place), not the new gating
        # logic itself (that's covered by TestExportRetryGap above).
        # None of them set up a real results file, so without this,
        # every one of them would depend on whatever the real
        # current date happens to be relative to the hardcoded
        # 2026-07-09 test day -- fragile and wrong. Pinning "today"
        # comfortably past the default 3-day retry window makes
        # every day used below always old enough to export
        # regardless of results, restoring the exact old,
        # unconditional-export behavior these tests were written
        # against.
        self.manager._today = lambda: date(2026, 7, 20)

    def tearDown(self) -> None:
        shutil.rmtree(self.external_root, ignore_errors=True)
        super().tearDown()

    def test_disabled_by_default(self) -> None:
        disabled_manager = station_manager.StationManager(
            cfg=self.cfg, db=self.db, pipeline=self.fake_pipeline
        )
        # A fresh cfg without the config key set, matching real
        # "not configured at all" behavior.
        disabled_manager.cfg.station = dict(self.cfg.station)
        del disabled_manager.cfg.station["external_storage_path"]
        disabled_manager.initialize()

        raw_file = self.cfg.raw_dir / "station_20260709.um980"
        raw_file.write_bytes(b"fake raw data")

        disabled_manager._export_day_to_external_storage(date(2026, 7, 9))

        # Nothing moved -- the raw file is still exactly where it was.
        self.assertTrue(raw_file.exists())

    def test_raw_file_exported_from_raw_dir(self) -> None:
        raw_file = self.cfg.raw_dir / "station_20260709.um980"
        raw_file.write_bytes(b"fake raw data")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = self.external_root / "Raw" / "station_20260709.um980"
        self.assertTrue(exported.exists())
        self.assertFalse(raw_file.exists())  # moved, not copied
        self.assertEqual(exported.read_bytes(), b"fake raw data")

    def test_raw_file_exported_from_archive_dir_when_already_processed(self) -> None:
        # Simulates pipeline.py having already archived a
        # successfully-processed raw file before export runs.
        archived_file = self.cfg.archive_dir / "station_20260709.um980"
        archived_file.write_bytes(b"already archived data")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = self.external_root / "Raw" / "station_20260709.um980"
        self.assertTrue(exported.exists())
        self.assertFalse(archived_file.exists())

    def test_missing_raw_file_is_not_an_error(self) -> None:
        try:
            self.manager._export_day_to_external_storage(date(2026, 7, 9))
        except Exception as exc:  # pragma: no cover
            self.fail(f"_export_day_to_external_storage() raised: {exc}")

    def test_products_directory_exported_and_flattened(self) -> None:
        # A file directly under refl_code/ with no recognized
        # category in its path at all falls back to "other/",
        # preserving its full original relative path rather than
        # being silently dropped.
        self.cfg.products_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.products_dir / "refl_code").mkdir(parents=True, exist_ok=True)
        (self.cfg.products_dir / "refl_code" / "marker.txt").write_text("real data")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = (
            self.external_root / "Products" / "20260709"
            / "other" / "refl_code" / "marker.txt"
        )
        self.assertTrue(exported.exists())
        self.assertEqual(exported.read_text(), "real data")
        # Copied, not moved: products/ is the whole water-level record.
        self.assertEqual(
            (self.cfg.products_dir / "refl_code" / "marker.txt").read_text(),
            "real data",
        )

    def test_export_never_moves_or_deletes_the_record(self) -> None:
        # products/ used to be moved file by file and then rmtree'd:
        # the next night gnss_record found one day of results, refused
        # to shrink the record, and the frozen years were gone.
        refl = self.cfg.products_dir / "refl_code"
        record_inputs = [
            refl / "2025" / "results" / "usgs" / "300.txt",
            refl / "2026" / "results" / "usgs" / "190.txt",
            refl / "Files" / "usgs" / "usgs_spline_out.txt",
            refl / "Files" / "usgs" / "usgs_2025_spline_frozen.txt",
            refl / "Files" / "usgs" / "fit" / "record_status.json",
            refl / "input" / "usgs.json",
            refl / "2026" / "snr" / "usgs" / "usgs1900.26.snr66",
        ]
        for path in record_inputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.name)

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        for path in record_inputs:
            with self.subTest(path=path.relative_to(refl)):
                self.assertEqual(path.read_text(), path.name)
        exported = self.external_root / "Products" / "20260709"
        self.assertEqual(
            (exported / "Files" / "usgs" / "usgs_spline_out.txt").read_text(),
            "usgs_spline_out.txt",
        )
        self.assertEqual(
            (exported / "results" / "2026" / "usgs" / "190.txt").read_text(),
            "190.txt",
        )

    def _by_mtime(self):
        # File times are set by hand in these tests, and a ctime cannot
        # be: judge "changed" by mtime alone (the ctime half is tested
        # in test_file_put_in_place_with_an_old_mtime_is_exported).
        return unittest.mock.patch.object(
            station_manager.StationManager, "_changed_time",
            staticmethod(lambda st: st.st_mtime))

    def _marker_time(self) -> float:
        marker = self.external_root / "Products" / self.manager._EXPORT_MARKER
        return float(marker.read_text())

    def test_next_export_copies_only_what_changed(self) -> None:
        # products/ now stays, so a daily export copies only the files
        # changed since the last one, not the whole record again.
        results = self.cfg.products_dir / "refl_code" / "2026" / "results" / "usgs"
        results.mkdir(parents=True, exist_ok=True)
        old_day = results / "189.txt"
        old_day.write_text("day 189")

        self.manager._export_day_to_external_storage(date(2026, 7, 8))

        # The marker on the external storage holds when that export
        # started: a file last changed well before it is not copied
        # again, one changed after it is.
        started = self._marker_time()
        os.utime(old_day, (started - 3600, started - 3600))
        new_day = results / "190.txt"
        new_day.write_text("day 190")
        os.utime(new_day, (started + 1, started + 1))

        with self._by_mtime():
            self.manager._export_day_to_external_storage(date(2026, 7, 9))

        second = self.external_root / "Products" / "20260709" / "results" / "2026" / "usgs"
        self.assertTrue((second / "190.txt").exists())
        self.assertFalse((second / "189.txt").exists())
        self.assertTrue(
            (self.external_root / "Products" / "20260708" / "results"
             / "2026" / "usgs" / "189.txt").exists()
        )
        self.assertFalse(
            (self.external_root / "Products" / "20260709" / "other").exists()
        )

    def test_change_just_before_the_last_export_is_copied_again(self) -> None:
        # File times lag time.time() (the kernel's coarse clock), some
        # filesystems keep whole seconds, and the clock can be stepped
        # back: a file changed just after an export started can carry a
        # time just before it. It must be copied again, not missed.
        results = self.cfg.products_dir / "refl_code" / "2026" / "results" / "usgs"
        results.mkdir(parents=True, exist_ok=True)
        day = results / "190.txt"
        day.write_text("first version")

        self.manager._export_day_to_external_storage(date(2026, 7, 8))

        day.write_text("rewritten during the export")
        just_before = self._marker_time() - 0.005
        os.utime(day, (just_before, just_before))

        with self._by_mtime():
            self.manager._export_day_to_external_storage(date(2026, 7, 9))

        self.assertEqual(
            (self.external_root / "Products" / "20260709" / "results" / "2026"
             / "usgs" / "190.txt").read_text(),
            "rewritten during the export",
        )

    def test_marker_ahead_of_the_clock_copies_everything(self) -> None:
        # The clock was an hour ahead when the last export ran (a wrong
        # clock at boot, before NTP) and has been set back since: by
        # that marker every file changed in the hour since is skipped,
        # and nothing would copy them again.
        results = self.cfg.products_dir / "refl_code" / "2026" / "results" / "usgs"
        results.mkdir(parents=True, exist_ok=True)
        day = results / "190.txt"
        day.write_text("day 190")
        marker = self.external_root / "Products" / self.manager._EXPORT_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time() + 3600:.3f}\n")

        with self._by_mtime(), self.assertLogs("station.manager", level="WARNING") as logs:
            self.manager._export_day_to_external_storage(date(2026, 7, 9))

        self.assertEqual(
            (self.external_root / "Products" / "20260709" / "results" / "2026"
             / "usgs" / "190.txt").read_text(),
            "day 190",
        )
        self.assertIn("after the clock now", "\n".join(logs.output))
        self.assertLessEqual(self._marker_time(), time.time())

    def test_file_put_in_place_with_an_old_mtime_is_exported(self) -> None:
        # A file moved in (a rename keeps its mtime), restored with
        # cp -p / rsync -a, or copied with shutil.copy2 (as the RINEX
        # staging does) has an mtime from before the last export, but
        # it has never been exported. Its ctime is fresh.
        results = self.cfg.products_dir / "refl_code" / "2025" / "results" / "usgs"
        results.mkdir(parents=True, exist_ok=True)
        (results / "001.txt").write_text("day 1")

        self.manager._export_day_to_external_storage(date(2026, 7, 8))

        restored = results / "300.txt"
        restored.write_text("restored from a backup")
        long_ago = self._marker_time() - 300 * 86400
        os.utime(restored, (long_ago, long_ago))

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        self.assertEqual(
            (self.external_root / "Products" / "20260709" / "results" / "2025"
             / "usgs" / "300.txt").read_text(),
            "restored from a backup",
        )

    def test_new_external_disk_gets_the_whole_record(self) -> None:
        # What was last exported is kept on the external storage, not
        # in products/: an empty (new, swapped, wiped) disk is given
        # the whole record, not only what changed since the old disk
        # was last written.
        spline = self.cfg.products_dir / "refl_code" / "Files" / "usgs" / "usgs_spline_out.txt"
        spline.parent.mkdir(parents=True, exist_ok=True)
        spline.write_text("the whole record")

        self.manager._export_day_to_external_storage(date(2026, 7, 8))

        new_disk = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, new_disk, True)
        self.manager._external_storage_path = new_disk
        with self._by_mtime():
            self.manager._export_day_to_external_storage(date(2026, 7, 9))

        self.assertEqual(
            (new_disk / "Products" / "20260709" / "Files" / "usgs"
             / "usgs_spline_out.txt").read_text(),
            "the whole record",
        )

    def test_file_vanishing_during_the_export_is_skipped(self) -> None:
        # A file listed by os.walk and gone before it is copied (a
        # temporary file, a staged RINEX file being cleaned up; here a
        # dangling symlink) used to abort the whole export, so the
        # files after it were not copied and the marker not written.
        refl = self.cfg.products_dir / "refl_code"
        (refl / "2026" / "rinex" / "usgs").mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.root / "gone.rnx"),
                   str(refl / "2026" / "rinex" / "usgs" / "staged.rnx"))
        results = refl / "2026" / "results" / "usgs"
        results.mkdir(parents=True, exist_ok=True)
        (results / "190.txt").write_text("day 190")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = self.external_root / "Products" / "20260709"
        self.assertEqual(
            (exported / "results" / "2026" / "usgs" / "190.txt").read_text(), "day 190"
        )
        self.assertFalse((exported / "rinex" / "2026" / "usgs" / "staged.rnx").exists())
        self.assertTrue(
            (self.external_root / "Products" / self.manager._EXPORT_MARKER).exists()
        )

    def test_flattening_keeps_the_year_and_drops_refl_code(self) -> None:
        # Confirmed against a real, live products/ directory: results
        # normally nest as <year>/results/<station>/<file>. The
        # refl_code/ level goes; the year stays (a results file's name
        # carries no year), and so does the station code, so files from
        # different years or station codes never collide.
        results_dir = (
            self.cfg.products_dir / "refl_code" / "2026" / "results" / "usgs"
        )
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "190.txt").write_text("day 190 results")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = (
            self.external_root / "Products" / "20260709"
            / "results" / "2026" / "usgs" / "190.txt"
        )
        self.assertTrue(exported.exists())
        self.assertEqual(exported.read_text(), "day 190 results")

    def test_same_day_of_year_in_two_years_both_exported(self) -> None:
        # Collapsing the year let 2025's 292.txt and 2026's 292.txt
        # land on the same results/usgs/292.txt: with products/ now
        # copied whole, the first export kept one year of every day of
        # year the record has twice.
        refl = self.cfg.products_dir / "refl_code"
        for year in ("2025", "2026"):
            path = refl / year / "results" / "usgs" / "292.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{year} day 292")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = self.external_root / "Products" / "20260709" / "results"
        for year in ("2025", "2026"):
            with self.subTest(year=year):
                self.assertEqual(
                    (exported / year / "usgs" / "292.txt").read_text(),
                    f"{year} day 292",
                )

    def test_same_day_check_spline_does_not_replace_the_record(self) -> None:
        # analysis_tools/ultra_rapid_check.py keeps its own REFL_CODE
        # beside refl_code/, with its own few-day
        # Files/usgs/usgs_spline_out.txt. Flattened to the same
        # Files/usgs/ it overwrote the whole-record spline in the
        # export (whichever os.walk reached last won).
        for tree, text in (("refl_code", "the whole record"),
                           ("refl_code_ultra", "four days, ultra-rapid")):
            path = self.cfg.products_dir / tree / "Files" / "usgs" / "usgs_spline_out.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = self.external_root / "Products" / "20260709" / "Files"
        self.assertEqual(
            (exported / "usgs" / "usgs_spline_out.txt").read_text(), "the whole record"
        )
        self.assertEqual(
            (exported / "refl_code_ultra" / "usgs" / "usgs_spline_out.txt").read_text(),
            "four days, ultra-rapid",
        )

    def test_multiple_station_codes_kept_separate_not_collided(self) -> None:
        # Confirmed real scenario: if a station's own code was ever
        # changed, gnssrefl's working directory can end up with
        # results for more than one station code side by side. Both
        # must be kept, not overwritten.
        base = self.cfg.products_dir / "refl_code" / "2026" / "results"
        (base / "usgs").mkdir(parents=True, exist_ok=True)
        (base / "wh01").mkdir(parents=True, exist_ok=True)
        (base / "usgs" / "197.txt").write_text("usgs results")
        (base / "wh01" / "197.txt").write_text("wh01 results")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported_root = self.external_root / "Products" / "20260709" / "results" / "2026"
        self.assertEqual(
            (exported_root / "usgs" / "197.txt").read_text(), "usgs results"
        )
        self.assertEqual(
            (exported_root / "wh01" / "197.txt").read_text(), "wh01 results"
        )

    def test_failqc_subfolder_preserved(self) -> None:
        failqc_dir = (
            self.cfg.products_dir / "refl_code" / "2026" / "results"
            / "usgs" / "failQC"
        )
        failqc_dir.mkdir(parents=True, exist_ok=True)
        (failqc_dir / "bad_arc.txt").write_text("rejected arc")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = (
            self.external_root / "Products" / "20260709"
            / "results" / "2026" / "usgs" / "failQC" / "bad_arc.txt"
        )
        self.assertTrue(exported.exists())

    def test_non_station_specific_folders_kept_as_is(self) -> None:
        # orbits/ and exe/ are shared, reusable resources, not
        # per-station -- confirmed real structure has no station code
        # under either, so flattening should leave their internal
        # structure alone beyond the category name itself.
        orbits_dir = self.cfg.products_dir / "refl_code" / "orbits" / "2026" / "sp3"
        orbits_dir.mkdir(parents=True, exist_ok=True)
        (orbits_dir / "orbit.sp3").write_text("orbit data")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = (
            self.external_root / "Products" / "20260709"
            / "orbits" / "2026" / "sp3" / "orbit.sp3"
        )
        self.assertTrue(exported.exists())

    def test_unrecognized_future_category_not_silently_dropped(self) -> None:
        # If gnssrefl ever adds a new category this code doesn't
        # already know about, it must still be preserved (under
        # other/, with its original relative path intact) rather than
        # silently lost.
        future_dir = (
            self.cfg.products_dir / "refl_code" / "some_new_category" / "usgs"
        )
        future_dir.mkdir(parents=True, exist_ok=True)
        (future_dir / "future.txt").write_text("future data")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        exported = (
            self.external_root / "Products" / "20260709"
            / "other" / "refl_code" / "some_new_category" / "usgs" / "future.txt"
        )
        self.assertTrue(exported.exists())
        self.assertEqual(exported.read_text(), "future data")

    def test_missing_products_directory_is_not_an_error(self) -> None:
        try:
            self.manager._export_day_to_external_storage(date(2026, 7, 9))
        except Exception as exc:  # pragma: no cover
            self.fail(f"_export_day_to_external_storage() raised: {exc}")

    def test_unavailable_external_storage_does_not_crash_or_lose_data(self) -> None:
        # Simulates the external mount being temporarily unavailable
        # (e.g. unmounted) -- confirmed by pointing at a path that
        # does not exist as a directory.
        self.manager._external_storage_path = self.external_root / "not_mounted"

        raw_file = self.cfg.raw_dir / "station_20260709.um980"
        raw_file.write_bytes(b"fake raw data")

        try:
            self.manager._export_day_to_external_storage(date(2026, 7, 9))
        except Exception as exc:  # pragma: no cover
            self.fail(f"_export_day_to_external_storage() raised: {exc}")

        # Local data preserved -- nothing was lost.
        self.assertTrue(raw_file.exists())

    def test_existing_destination_is_not_overwritten(self) -> None:
        self.cfg.products_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.products_dir / "marker.txt").write_text("todays data")

        collision_dir = self.external_root / "Products" / "20260709"
        collision_dir.mkdir(parents=True, exist_ok=True)
        (collision_dir / "old.txt").write_text("yesterdays data, still here")

        self.manager._export_day_to_external_storage(date(2026, 7, 9))

        # The pre-existing export was not overwritten or deleted.
        self.assertTrue((collision_dir / "old.txt").exists())
        # And since we refused to overwrite, the local copy is
        # preserved too, rather than silently lost.
        self.assertTrue(self.cfg.products_dir.exists())

    def test_run_pipeline_and_health_check_all_happen_before_export(self) -> None:
        # Confirms the real ordering in run()'s daily cycle: pipeline
        # runs, then health check, then export -- not export first,
        # which could move data pipeline.py still needed. Recording
        # itself is irrelevant to this test: the first day ends at
        # once, to reach the ordering-relevant calls immediately, and
        # the next one records until stopped, as the real recorder
        # does while the pipeline runs in the background.
        self.manager._today = lambda: date(2026, 7, 9)
        days_recorded = []

        def record_day(day):
            if days_recorded:
                deadline = time.monotonic() + 5.0
                while not self.manager._stop_requested and time.monotonic() < deadline:
                    time.sleep(0.01)
            days_recorded.append(day)

        self.manager._record_day = record_day

        raw_file = self.cfg.raw_dir / "station_20260709.um980"
        raw_file.write_bytes(b"fake raw data")

        call_order = []
        original_pipeline = self.manager._run_pipeline_safely
        original_health = self.manager._health_check
        original_export = self.manager._export_day_to_external_storage

        def track_pipeline():
            call_order.append("pipeline")
            original_pipeline()

        def track_health():
            call_order.append("health")
            original_health()

        def track_export(day):
            call_order.append("export")
            self.manager.stop()  # stop after first cycle for this test
            original_export(day)

        self.manager._run_pipeline_safely = track_pipeline
        self.manager._health_check = track_health
        self.manager._export_day_to_external_storage = track_export

        self.manager.run()

        self.assertEqual(call_order, ["pipeline", "health", "export"])


class TestLifecycle(StationManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.manager.initialize()
        self.manager._seconds_until_next_utc_midnight = lambda: 9999.0
        self.manager._chunk_seconds = 0.0

    def test_run_raises_if_not_initialized(self) -> None:
        fresh = station_manager.StationManager(
            cfg=self.cfg, db=self.db, pipeline=self.fake_pipeline
        )

        with self.assertRaises(RuntimeError):
            fresh.run()

    def test_run_processes_two_days_then_stops(self) -> None:
        day1 = date(2026, 7, 9)
        day2 = date(2026, 7, 10)
        day3 = date(2026, 7, 11)

        # Tied to observable chunk-count state (not exact call-count
        # assumptions about _today(), which would be fragile): 1
        # chunk on day1, roll to day2, 1 chunk on day2, roll to day3
        # -- by which point the pipeline-run-triggered stop() below
        # should already have fired.
        def fake_today():
            chunks_so_far = len(FakeReceiver.record_calls)
            if chunks_so_far < 1:
                return day1
            if chunks_so_far < 2:
                return day2
            return day3

        self.manager._today = fake_today

        original_run_pipeline = self.manager._run_pipeline_safely
        pipeline_runs = {"count": 0}

        def run_pipeline_and_maybe_stop():
            original_run_pipeline()
            pipeline_runs["count"] += 1
            if pipeline_runs["count"] >= 2:
                self.manager.stop()

        self.manager._run_pipeline_safely = run_pipeline_and_maybe_stop

        self.manager.run()

        self.assertEqual(pipeline_runs["count"], 2)
        # day3 keeps recording while day2's pipeline runs in the
        # background, until that run asks for the stop: at least the
        # two chunks of day1 and day2, usually more.
        self.assertGreaterEqual(len(FakeReceiver.record_calls), 2)
        self.assertFalse(self.manager._running)
        self.assertTrue(self.manager._stop_requested)

    def test_stop_sets_flag(self) -> None:
        self.assertFalse(self.manager._stop_requested)

        self.manager.stop()

        self.assertTrue(self.manager._stop_requested)

    def test_status_reflects_current_day_and_chunks(self) -> None:
        self.manager._current_day = date(2026, 7, 9)
        self.manager._chunks_recorded_today = 3

        status = self.manager.status()

        self.assertEqual(status.current_day, "2026-07-09")
        self.assertEqual(status.chunks_recorded_today, 3)

    def test_shutdown_shuts_down_pipeline(self) -> None:
        self.manager.shutdown()

        self.assertTrue(self.fake_pipeline.shutdown_called)
        self.assertFalse(self.manager._initialized)


# ------------------------------------------------------------
# Recording continues while the pipeline runs (review B8)
# ------------------------------------------------------------

class BlockingPipeline(FakePipeline):
    """
    A FakePipeline whose run() holds until `release` is set (or a
    timeout), counting the chunks of `watch_day` that the recorder
    wrote meanwhile. Tracks how many runs overlap.
    """

    def __init__(self, watch_day: date, hold_s: float = 5.0):
        super().__init__()
        self.watch = f"{watch_day:%Y%m%d}"
        self.hold_s = hold_s
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.chunks_at_start = None
        self.chunks_during_run = None
        self.active = 0
        self.max_active = 0
        self._count_lock = threading.Lock()

    def watched_chunks(self) -> int:
        return sum(
            self.watch in call["filename"].name
            for call in list(FakeReceiver.record_calls)
        )

    def run(self) -> PipelineSummary:
        with self._count_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.run_calls == 0:
                self.chunks_at_start = self.watched_chunks()
                self.started.set()
                self.release.wait(self.hold_s)
                self.chunks_during_run = (
                    self.watched_chunks() - self.chunks_at_start
                )
            return super().run()
        finally:
            with self._count_lock:
                self.active -= 1
            self.finished.set()


class TestPipelineInBackground(StationManagerTestCase):

    day1 = date(2026, 7, 9)
    day2 = date(2026, 7, 10)
    day3 = date(2026, 7, 11)

    def setUp(self) -> None:
        super().setUp()
        self.manager._seconds_until_next_utc_midnight = lambda: 9999.0

    def _use(self, pipeline, station=None) -> None:
        self.cfg.station = dict(station or {})
        self.manager = station_manager.StationManager(
            cfg=self.cfg, db=self.db, pipeline=pipeline
        )
        self.manager._make_receiver = lambda: FakeReceiver(cfg=self.cfg)
        self.manager.initialize()
        self.manager._seconds_until_next_utc_midnight = lambda: 9999.0
        self.manager._chunk_seconds = 0.0
        self.manager._retry_delay_seconds = 0.0

    def _days_by_chunk_count(self, *days_and_chunks) -> None:
        """
        _today() follows the chunks recorded: the first n1 chunks are
        day 1, the next n2 day 2, ...; the last day never ends.
        """

        def fake_today():
            done = len(FakeReceiver.record_calls)
            for day, chunks in days_and_chunks[:-1]:
                if done < chunks:
                    return day
                done -= chunks
            return days_and_chunks[-1][0]

        self.manager._today = fake_today

    def _stop_when(self, predicate, timeout_s: float = 10.0) -> None:
        """Stop between chunks once `predicate()` holds (or on timeout)."""

        original = self.manager._record_one_chunk
        deadline = time.monotonic() + timeout_s

        def record(raw_file, duration):
            original(raw_file, duration)
            if predicate() or time.monotonic() > deadline:
                self.manager.stop()
            else:
                time.sleep(0.001)

        self.manager._record_one_chunk = record

    def test_defaults_to_background(self) -> None:
        self._use(FakePipeline())
        self.assertTrue(self.manager._pipeline_in_background)

    def test_station_json_can_restore_inline_order(self) -> None:
        self._use(FakePipeline(), {"manager_pipeline_in_background": False})
        self.assertFalse(self.manager._pipeline_in_background)

    def test_next_day_records_while_pipeline_runs(self) -> None:
        # The fix itself: the pipeline started at day1's rollover
        # holds until the recorder has written a chunk of day2. With
        # the old inline run nothing records until run() returns, so
        # the pipeline would hold for its full timeout and count none.
        pipeline = BlockingPipeline(watch_day=self.day2)
        self._use(pipeline)
        self._days_by_chunk_count((self.day1, 1), (self.day2, 1))

        def released_and_done():
            if (
                pipeline.started.is_set()
                and pipeline.watched_chunks() > pipeline.chunks_at_start
            ):
                pipeline.release.set()
            return pipeline.finished.is_set()

        self._stop_when(released_and_done)

        self.manager.run()

        self.assertEqual(pipeline.run_calls, 1)
        self.assertGreaterEqual(pipeline.chunks_during_run, 1)
        day2_files = {
            c["filename"].name for c in FakeReceiver.record_calls
            if "20260710" in c["filename"].name
        }
        self.assertEqual(day2_files, {"station_20260710.um980"})

    def test_inline_mode_records_nothing_while_pipeline_runs(self) -> None:
        # The way back ("manager_pipeline_in_background": false) is
        # the old order: no chunk of day2 while day1's pipeline runs.
        pipeline = BlockingPipeline(watch_day=self.day2, hold_s=0.2)
        self._use(pipeline, {"manager_pipeline_in_background": False})
        self._days_by_chunk_count((self.day1, 1), (self.day2, 1))
        self._stop_when(lambda: pipeline.finished.is_set())

        self.manager.run()

        self.assertEqual(pipeline.chunks_during_run, 0)
        self.assertEqual(len(FakeReceiver.record_calls), 2)

    def test_health_and_export_follow_the_pipeline_in_the_thread(self) -> None:
        pipeline = BlockingPipeline(watch_day=self.day2)
        self._use(pipeline)
        self._days_by_chunk_count((self.day1, 1), (self.day2, 1))

        order = []
        original_health = self.manager._health_check
        original_export = self.manager._export_day_to_external_storage

        def health():
            order.append(("health", pipeline.finished.is_set()))
            original_health()

        def export(day):
            order.append(("export", day))
            original_export(day)

        self.manager._health_check = health
        self.manager._export_day_to_external_storage = export

        def released_and_done():
            if pipeline.started.is_set():
                pipeline.release.set()
            return len(order) >= 2

        self._stop_when(released_and_done)

        self.manager.run()

        self.assertEqual(order, [("health", True), ("export", self.day1)])

    def test_stop_waits_for_the_pipeline_in_progress(self) -> None:
        # A stop asked for while the background run is busy must not
        # let run() return (and shutdown() close the database) under it.
        pipeline = BlockingPipeline(watch_day=self.day2)
        self._use(pipeline)
        self._days_by_chunk_count((self.day1, 1), (self.day2, 1))

        def stop_then_release_late():
            if not pipeline.started.is_set():
                return False
            # Released only after run() has seen the stop.
            threading.Timer(0.3, pipeline.release.set).start()
            return True

        self._stop_when(stop_then_release_late)

        self.manager.run()

        self.assertTrue(pipeline.finished.is_set())
        self.assertIsNone(self.manager._rollover_thread)
        self.manager.shutdown()
        self.assertTrue(pipeline.shutdown_called)

    def test_a_busy_pipeline_delays_the_next_day_never_overlaps(self) -> None:
        # day1's run holds past day2's rollover; day2's run waits for
        # it, so the two never run at once, and both days are exported
        # in order.
        pipeline = BlockingPipeline(watch_day=self.day3)
        self._use(pipeline)
        self._days_by_chunk_count((self.day1, 1), (self.day2, 1), (self.day3, 1))

        exported = []
        self.manager._export_day_to_external_storage = exported.append

        def release_on_day3_then_done():
            if pipeline.watched_chunks() >= 1:
                pipeline.release.set()
            return len(exported) >= 2

        self._stop_when(release_on_day3_then_done)

        self.manager.run()

        self.assertEqual(pipeline.run_calls, 2)
        self.assertEqual(pipeline.max_active, 1)
        self.assertEqual(exported, [self.day1, self.day2])

    def test_system_exit_in_the_pipeline_is_logged_recording_goes_on(self) -> None:
        # gnssrefl calls sys.exit() on some error paths. In the thread
        # that must not end the day's work silently, nor reach the
        # recorder; the next day's work still runs.
        class ExitingPipeline(FakePipeline):
            def run(self):
                if self.run_calls == 0:
                    self.run_calls += 1
                    raise SystemExit("gnssrefl gave up")
                return super().run()

        pipeline = ExitingPipeline()
        self._use(pipeline)
        self._days_by_chunk_count((self.day1, 1), (self.day2, 1), (self.day3, 1))
        self._stop_when(lambda: pipeline.run_calls >= 2 and len(FakeReceiver.record_calls) >= 3)

        with self.assertLogs("station.manager", level="ERROR") as logs:
            self.manager.run()

        self.assertEqual(pipeline.run_calls, 2)
        self.assertTrue(any("gnssrefl gave up" in line for line in logs.output))
        self.assertFalse(self.manager._running)


class _WritingReceiver(FakeReceiver):
    """A FakeReceiver that really appends bytes, as record_raw() does."""

    def record_raw(self, filename, duration, append=False, enable_logging=False, **kwargs):
        result = super().record_raw(filename, duration, append, enable_logging)
        with open(filename, "ab" if append else "wb") as handle:
            handle.write(b"\xaa" * 1000)
        return result


class TestRestartKeepsRecordedBytes(StationManagerTestCase):
    """A restart mid-day, with a receiver that really writes: the bytes the
    earlier run recorded are still at the start of the day's file."""

    def test_bytes_before_the_restart_survive(self) -> None:
        today = date(2026, 7, 9)
        raw = self.manager._raw_file_for_day(today)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x55" * 3000)
        self.manager._make_receiver = lambda: _WritingReceiver()
        calls = {"n": 0}

        def fake_today():
            calls["n"] += 1
            return today if calls["n"] <= 2 else today + timedelta(days=1)

        self.manager._today = fake_today

        self.manager._record_day(today)

        data = raw.read_bytes()
        self.assertEqual(data[:3000], b"\x55" * 3000)
        self.assertEqual(len(data), 3000 + 2 * 1000)


class TestRealPipelineInBackground(StationManagerTestCase):
    """
    The real Pipeline and a real SQLite Database, shared with the
    manager, run in the background thread (only convbin and gnssrefl
    are faked), while the recorder appends to today's raw file.
    """

    def test_real_pipeline_processes_the_closed_day_while_today_records(self) -> None:
        from pipeline import Pipeline

        closed_day = date(2020, 1, 1)
        today = datetime.now(timezone.utc).date()
        closed_file = self.cfg.raw_dir / f"station_{closed_day:%Y%m%d}.um980"
        today_file = self.cfg.raw_dir / f"station_{today:%Y%m%d}.um980"

        def today_chunks() -> int:
            return sum(
                c["filename"] == today_file for c in list(FakeReceiver.record_calls)
            )

        seen = {"today_chunks_during_convert": None, "converted": []}
        converted = threading.Event()
        root = self.root

        class FakeRinex:
            def convert(self, raw_path):
                seen["converted"].append(raw_path.name)
                before = today_chunks()
                deadline = time.monotonic() + 5.0
                while today_chunks() <= before and time.monotonic() < deadline:
                    time.sleep(0.005)
                seen["today_chunks_during_convert"] = today_chunks() - before
                converted.set()
                return SimpleNamespace(
                    observation_file=root / "rinex" / f"{raw_path.stem}.obs",
                    navigation_file="", sbas_file="", success=True,
                    convbin_version="fake", message="ok",
                )

            def verify(self, result):
                return True

        class FakeGnssir:
            def process(self, observation_file, day):
                return SimpleNamespace(
                    reflector_height=19.0, soil_moisture=None, snow_depth=None,
                    quality_score=1.0, output_directory=str(root / "products"),
                    success=True, runtime_seconds=0.0, message="ok",
                )

            def verify(self, result):
                return True

        class TestPipeline(Pipeline):
            def _initialize_processors(self):
                self._rinex = FakeRinex()
                self._gnssir = FakeGnssir()

        pipeline = TestPipeline(cfg=self.cfg, db=self.db)
        manager = station_manager.StationManager(
            cfg=self.cfg, db=self.db, pipeline=pipeline
        )
        manager._make_receiver = lambda: _WritingReceiver(cfg=self.cfg)
        manager.initialize()
        manager._seconds_until_next_utc_midnight = lambda: 9999.0
        manager._chunk_seconds = 0.0
        manager._today = lambda: closed_day if not FakeReceiver.record_calls else today

        original = manager._record_one_chunk
        deadline = time.monotonic() + 10.0

        def record(raw_file, duration):
            original(raw_file, duration)
            if raw_file == closed_file:
                # Closed long ago, so the pipeline's "written in the
                # last 15 min" rule (for files without today's date)
                # does not hold it back in this test.
                old = time.time() - 3600
                os.utime(closed_file, (old, old))
            if (converted.is_set() and manager._last_summary is not None
                    and today_chunks() >= 2) or time.monotonic() > deadline:
                manager.stop()
            else:
                time.sleep(0.001)

        manager._record_one_chunk = record

        manager.run()

        # The closed day went through the real pipeline and was archived ...
        self.assertEqual(seen["converted"], [closed_file.name])
        self.assertTrue((self.cfg.archive_dir / closed_file.name).exists())
        self.assertFalse(closed_file.exists())
        self.assertEqual(manager._last_summary.files_processed, 1)
        # ... while today's file kept growing in raw/, never touched.
        self.assertGreaterEqual(seen["today_chunks_during_convert"], 1)
        self.assertTrue(today_file.exists())
        self.assertEqual(today_file.stat().st_size, 1000 * today_chunks())
        # The health snapshot ran in the thread on the shared database.
        self.assertIsNotNone(manager._last_health_check)


# ------------------------------------------------------------
# Signal handling
# ------------------------------------------------------------

class TestSignalHandling(StationManagerTestCase):

    def test_handle_signal_calls_stop(self) -> None:
        self.manager.initialize()

        self.assertFalse(self.manager._stop_requested)

        self.manager._handle_signal(15, None)  # 15 = SIGTERM

        self.assertTrue(self.manager._stop_requested)


if __name__ == "__main__":
    unittest.main()
