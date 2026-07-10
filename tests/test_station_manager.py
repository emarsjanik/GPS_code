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
import sys
import tempfile
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
# run() / stop() / status() / shutdown() lifecycle
# ------------------------------------------------------------

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
        self.assertEqual(len(FakeReceiver.record_calls), 2)
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
