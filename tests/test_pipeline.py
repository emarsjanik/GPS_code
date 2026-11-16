"""
tests/test_pipeline.py

Focused regression tests for pipeline.py: the filename-based date
extraction fix, the day still being recorded, the run lock, and (from
PipelineRunTestCase on) a whole run of one file through a real SQLite
database with fake converters -- above all a day that can never give
results (no orbit ever published, no arc kept), which must stop
failing every run.

Confirmed against a real backlog of raw files that computing a raw
file's date from its modification time (the original behavior) is
unreliable: mtime drifts for reasons unrelated to which day's data a
file actually represents (repeated failed processing attempts
touching the file, backups, copies). Confirmed directly: every
backlogged file's GNSS-IR processing was silently using the wrong
day-of-year, off by one or more days, because of exactly this.

_date_from_filename() fixes this by parsing the date directly out of
the filename itself, which is stable and unambiguous, falling back
to the old mtime-based behavior only for filenames that don't carry
a recognizable date at all (ad-hoc test recordings).
"""

from __future__ import annotations

import contextlib
import errno
import io
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))

import pipeline as pipeline_module  # noqa: E402
from database import Database, DatabaseError  # noqa: E402
from gnssrefl_processor import GPS_ONLY_FALLBACK_AGE_S  # noqa: E402
from pipeline import (  # noqa: E402
    HANDOFF_AGE_S, HANDOFF_RETRIES, ORBIT_WAIT_S, PIPELINE_LOCK_NAME, Pipeline,
    PipelineBusyError, _date_from_filename, _in_progress_reason, orbit_wait_s,
)


class TestDateFromFilename(unittest.TestCase):

    def test_standard_station_manager_filename(self) -> None:
        # The real, everyday filename pattern station_manager.py
        # produces for its own autonomous daily recordings.
        self.assertEqual(_date_from_filename("station_20260709.um980"), "2026-07-09")
        self.assertEqual(_date_from_filename("station_20260710.um980"), "2026-07-10")
        self.assertEqual(_date_from_filename("station_20260713.um980"), "2026-07-13")

    def test_overnight_recording_filename(self) -> None:
        # Confirmed real filename from overnight_recording.py, which
        # embeds both a date and a start time -- the date portion
        # (the first 8 digits) is what should be extracted.
        self.assertEqual(
            _date_from_filename("overnight_20260708_135223.um980"),
            "2026-07-08",
        )

    def test_ad_hoc_test_filenames_with_no_date_fall_back_to_none(self) -> None:
        # Confirmed real filenames from ad-hoc manual testing earlier
        # in this project, which were never meant to carry a date.
        # None here means "caller should fall back to its own
        # previous behavior" -- not an error.
        for filename in (
            "test_recording.um980",
            "test_recording2.um980",
            "test_recording9.um980",
            "test_binary1.um980",
        ):
            with self.subTest(filename=filename):
                self.assertIsNone(_date_from_filename(filename))

    def test_invalid_date_like_sequence_falls_back_to_none(self) -> None:
        # An 8-digit sequence that isn't a real calendar date (month
        # 13, day 40) must not raise -- just fall back, same as no
        # match at all.
        self.assertIsNone(_date_from_filename("station_20261340.um980"))
        self.assertIsNone(_date_from_filename("weird_99999999_file.um980"))

    def test_date_embedded_anywhere_in_filename_is_found(self) -> None:
        # Confirmed real case: test_20260707.um980 has its date
        # embedded after a prefix, not at a fixed position -- the
        # parser must find it regardless of where it sits.
        self.assertEqual(_date_from_filename("test_20260707.um980"), "2026-07-07")

    def test_no_digits_at_all_returns_none(self) -> None:
        self.assertIsNone(_date_from_filename("plainfilename.um980"))


class TestInProgressDaySkipped(unittest.TestCase):
    """
    The UTC day station_manager is still appending to must not be
    processed: archiving it moved the file out from under the recorder,
    and the rest of the day, recorded into a fresh file of the same
    name, was never processed (the name was already done).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self._tmp.name)
        self.now = datetime(2026, 10, 20, 2, 30, tzinfo=timezone.utc)
        self.old = time.time() - 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _raw(self, name: str, mtime: float | None = None) -> Path:
        path = self.raw_dir / name
        path.write_bytes(b"x")
        mtime = self.old if mtime is None else mtime
        os.utime(path, (mtime, mtime))
        return path

    def test_todays_utc_file_is_in_progress(self) -> None:
        path = self._raw("station_20261020.um980", mtime=self.now.timestamp() - 3600)
        self.assertIsNotNone(_in_progress_reason(path, now=self.now))

    def test_yesterdays_file_is_finished(self) -> None:
        # 02:30 UTC is 22:30 EDT: yesterday's UTC file is complete, and
        # with a same-day orbit (nav, ultra) ready.
        path = self._raw("station_20261019.um980", mtime=self.now.timestamp() - 3600)
        self.assertIsNone(_in_progress_reason(path, now=self.now, orbit_wait=0.0))

    def test_yesterdays_file_waits_for_the_default_orbit(self) -> None:
        # gnssrefl's default (GFZ rapid) orbit is published hours after its
        # day: at the cron's 02:30 UTC the day closed 2.5 h ago waits, not
        # fails; by ORBIT_WAIT_S after its end it is ready.
        path = self._raw("station_20261019.um980", mtime=self.now.timestamp() - 3600)
        reason = _in_progress_reason(path, now=self.now)
        self.assertIsNotNone(reason)
        self.assertIn("waiting for its orbits (9.5 h more)", reason)
        end = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.assertIsNotNone(_in_progress_reason(
            path, now=end + timedelta(seconds=ORBIT_WAIT_S - 1)))
        self.assertIsNone(_in_progress_reason(path, now=end + timedelta(seconds=ORBIT_WAIT_S)))

    def test_orbit_wait_by_source(self) -> None:
        # unset (gnssrefl's default), and every orbit that is not same-day, waits
        for src in (None, "", "gnss", "rapid", "gps+glo", "final"):
            self.assertEqual(orbit_wait_s(src), ORBIT_WAIT_S, src)
        # gnssrefl's broadcast family and the ultra-rapid orbit are same-day
        for src in ("nav", "nav-sopac", "NAV-ESA", "ultra", " Ultra ", "gnss-ultra"):
            self.assertEqual(orbit_wait_s(src), 0.0, src)
        # between the cron's 02:30 UTC (+1 h of run lock) and the next midnight run
        self.assertGreater(ORBIT_WAIT_S, 4.5 * 3600)
        self.assertLess(ORBIT_WAIT_S, 24 * 3600)

    def test_recently_written_undated_file_is_in_progress(self) -> None:
        path = self._raw("test_recording.um980", mtime=time.time() - 60)
        self.assertIsNotNone(_in_progress_reason(path))
        path = self._raw("test_recording2.um980", mtime=time.time() - 3600)
        self.assertIsNone(_in_progress_reason(path))

    def test_day_just_closed_is_finished_at_the_rollover_run(self) -> None:
        # station_manager runs the pipeline at 00:00 UTC, straight after
        # the day's last chunk: yesterday's file was written seconds ago
        # and is complete. With a same-day orbit the midnight run takes it
        # then; with the default orbit it waits, and the next midnight run
        # takes it (its orbit was there 24 h after its day, 20-26 Oct 2026).
        rollover = datetime(2026, 10, 20, 0, 0, 5, tzinfo=timezone.utc)
        path = self._raw("station_20261019.um980", mtime=rollover.timestamp() - 3)
        self.assertIsNone(_in_progress_reason(path, now=rollover, orbit_wait=0.0))
        self.assertIsNotNone(_in_progress_reason(path, now=rollover))
        self.assertIsNone(_in_progress_reason(path, now=rollover + timedelta(days=1)))
        today = self._raw("station_20261020.um980", mtime=rollover.timestamp() - 3)
        self.assertIsNotNone(_in_progress_reason(today, now=rollover, orbit_wait=0.0))

    def test_evening_overnight_recording_is_in_progress(self) -> None:
        # overnight_recording.py names its file by the LOCAL start time:
        # begun 20:30 EDT on 20 Oct (00:30 UTC on the 21st), it is still
        # being written at 02:30 UTC though its date is not today's.
        now = datetime(2026, 10, 21, 2, 30, tzinfo=timezone.utc)
        path = self._raw("overnight_20261020_203000.um980", mtime=now.timestamp() - 30)
        self.assertIsNotNone(_in_progress_reason(path, now=now))
        path = self._raw("overnight_20261020_203000.um980", mtime=now.timestamp() - 5)
        self.assertIsNotNone(_in_progress_reason(path, now=now))
        # finished once nothing has been written to it for a while
        os.utime(path, (now.timestamp() - 3600, now.timestamp() - 3600))
        self.assertIsNone(_in_progress_reason(path, now=now))

    def test_only_station_managers_name_skips_the_written_time(self) -> None:
        now = datetime(2026, 10, 21, 0, 0, 5, tzinfo=timezone.utc)
        for name in ("test_20261020.um980", "station_20261020_extra.um980",
                     "mystation_20261020.um980"):
            path = self._raw(name, mtime=now.timestamp() - 3)
            self.assertIsNotNone(_in_progress_reason(path, now=now), name)
        path = self._raw("station_20261020.um980", mtime=now.timestamp() - 3)
        self.assertIsNone(_in_progress_reason(path, now=now, orbit_wait=0.0))

    def test_station_file_dated_after_today_keeps_the_written_time(self) -> None:
        # a clock that was set back since recording began: not a closed day
        path = self._raw("station_20261021.um980", mtime=self.now.timestamp() - 60)
        self.assertIsNotNone(_in_progress_reason(path, now=self.now))

    def test_rollover_run_scans_the_day_just_closed(self) -> None:
        # with a same-day orbit (station.json gnssrefl_orbit_source "ultra")
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        self._raw(f"station_{yesterday}.um980", mtime=time.time() - 2)
        self._raw(f"station_{now:%Y%m%d}.um980", mtime=time.time() - 2)

        pipeline, _ = self._pipeline(station={"gnssrefl_orbit_source": "ultra"})

        self.assertEqual([p.name for p in pipeline._scan_raw_directory()],
                         [f"station_{yesterday}.um980"])

    def test_default_orbit_scan_and_queue_wait_for_the_day_just_closed(self) -> None:
        # gnssrefl's default orbit: just after midnight the day just closed
        # is neither scanned nor taken from the queue (where a failed
        # attempt left it); the day before it is.
        rollover = datetime(2026, 10, 28, 0, 0, 5, tzinfo=timezone.utc)
        for d in ("20261026", "20261027"):
            self._raw(f"station_{d}.um980", mtime=rollover.timestamp() - 2)
        names = ["station_20261026.um980", "station_20261027.um980"]

        with unittest.mock.patch.object(pipeline_module, "_utcnow", return_value=rollover):
            pipeline, processed = self._pipeline(queued=names)
            self.assertEqual([p.name for p in pipeline._scan_raw_directory()], [names[0]])
            pipeline._process_queue()
        self.assertEqual(processed, [names[0]])

        # the cron's 02:30 UTC run: still waiting; the next midnight run takes it
        for when, want in ((rollover + timedelta(hours=2, minutes=25), [names[0]]),
                           (rollover + timedelta(days=1), names)):
            with unittest.mock.patch.object(pipeline_module, "_utcnow", return_value=when):
                pipeline, processed = self._pipeline(queued=names)
                pipeline._process_queue()
            self.assertEqual(processed, want, when)

    def test_missing_file_with_old_date_is_not_in_progress(self) -> None:
        self.assertIsNone(_in_progress_reason(self.raw_dir / "station_20261001.um980"))

    def _pipeline(self, queued=(), station=None) -> tuple[Pipeline, list[str]]:
        entries = [SimpleNamespace(filename=name, completed=False) for name in queued]
        db = SimpleNamespace(processing_history=lambda filename=None: entries)
        cfg = SimpleNamespace(raw_dir=self.raw_dir)
        if station is not None:
            cfg.station = station
        pipeline = Pipeline(cfg=cfg, db=db)
        processed: list[str] = []
        pipeline._process_one_job = processed.append
        return pipeline, processed

    def test_scan_leaves_out_todays_file(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._raw("station_20200101.um980")
        self._raw(f"station_{today}.um980")

        pipeline, _ = self._pipeline()

        self.assertEqual([p.name for p in pipeline._scan_raw_directory()],
                         ["station_20200101.um980"])

    def test_queue_leaves_todays_file_waiting(self) -> None:
        # Queued by an earlier run the same UTC day, or before the
        # check existed: still not processed while it is recording.
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._raw("station_20200101.um980")
        self._raw(f"station_{today}.um980")

        pipeline, processed = self._pipeline(
            queued=["station_20200101.um980", f"station_{today}.um980"])
        pipeline._process_queue()

        self.assertEqual(processed, ["station_20200101.um980"])

    def test_a_whole_run_leaves_todays_file_alone(self) -> None:
        # Through run() and its lock, as process_and_plot.sh's Step 2
        # and station_manager's midnight run call it (finding H1 + L4):
        # cron now reaches the pipeline, and must still not touch the
        # UTC day being recorded, even when it was queued before.
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._raw("station_20200101.um980")
        self._raw(f"station_{today}.um980", mtime=time.time() - 2)

        queued: list[str] = [f"station_{today}.um980"]
        db = SimpleNamespace(processing_history=lambda filename=None: [
            SimpleNamespace(filename=name, completed=False) for name in queued])
        pipeline = Pipeline(cfg=SimpleNamespace(raw_dir=self.raw_dir,
                                                logs_dir=self.raw_dir / "logs"), db=db)
        pipeline._initialized = True
        pipeline._find_new_files = lambda scanned: scanned
        pipeline._verify_raw_file = lambda path: True
        pipeline._queue_processing = lambda path: queued.append(path.name)
        pipeline._update_database = lambda: None
        processed: list[str] = []
        pipeline._process_one_job = processed.append

        pipeline.run()

        self.assertEqual(processed, ["station_20200101.um980"])
        self.assertNotIn(f"station_{today}.um980", queued[1:])


class TestRunLock(unittest.TestCase):
    """
    station_manager's run at each UTC midnight and the cron's
    (process_and_plot.sh Step 2) never run Pipeline at the same time
    (finding H1): run() holds logs/pipeline.lock for the whole pass.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs = Path(self._tmp.name) / "logs"
        self.lock_path = self.logs / PIPELINE_LOCK_NAME
        poll = unittest.mock.patch.object(pipeline_module, "_LOCK_POLL_S", 0.02)
        poll.start()
        self.addCleanup(poll.stop)

    def _pipeline(self, body=None) -> Pipeline:
        pipeline = Pipeline(cfg=SimpleNamespace(logs_dir=self.logs))
        pipeline._initialized = True
        pipeline._run = body or (lambda: "summary")
        return pipeline

    def _hold_in_another_process(self) -> subprocess.Popen:
        self.logs.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys, time\n"
             "f = open(sys.argv[1], 'a')\n"
             "fcntl.flock(f, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "time.sleep(60)\n",
             str(self.lock_path)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_a_run_held_off_by_another_process_does_nothing(self) -> None:
        holder = self._hold_in_another_process()
        ran: list[str] = []
        pipeline = self._pipeline(lambda: ran.append("ran"))

        with self.assertRaises(PipelineBusyError):
            pipeline.run(lock_wait_s=0.2)
        self.assertEqual(ran, [])

        holder.kill()
        holder.wait()
        pipeline.run(lock_wait_s=5)
        self.assertEqual(ran, ["ran"])

    def test_a_run_waits_for_the_other_to_finish(self) -> None:
        holder = self._hold_in_another_process()
        threading.Timer(0.3, holder.kill).start()

        self.assertEqual(self._pipeline().run(lock_wait_s=30), "summary")

    def test_two_runs_in_one_process_never_overlap(self) -> None:
        # station_manager's background thread and anything else in the
        # same process take the lock through their own open file, so
        # threads are kept apart too
        inside, most, done = [0], [0], []
        guard = threading.Lock()

        def body():
            with guard:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
            time.sleep(0.1)
            with guard:
                inside[0] -= 1
            done.append(1)

        threads = [threading.Thread(target=self._pipeline(body).run, kwargs={"lock_wait_s": 30})
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual((len(done), most[0]), (3, 1))

    def test_the_lock_is_released_when_a_run_raises(self) -> None:
        def crash():
            raise RuntimeError("simulated crash")

        with self.assertRaises(RuntimeError):
            self._pipeline(crash).run(lock_wait_s=0)
        self.assertEqual(self._pipeline().run(lock_wait_s=0), "summary")

    def test_run_before_initialize_still_refuses(self) -> None:
        pipeline = Pipeline(cfg=SimpleNamespace(logs_dir=self.logs))
        with self.assertRaises(RuntimeError):
            pipeline.run()
        self.assertFalse(self.lock_path.exists())

    def test_a_lock_that_cannot_be_taken_never_stops_a_run(self) -> None:
        # Only a lock another run really holds is "busy". A flock the
        # file system refuses (ENOLCK, EINVAL) or a logs/ that cannot
        # hold the lock file costs the lock, not the night's processing.
        for errno_value in (errno.ENOLCK, errno.EINVAL):
            with self.subTest(errno=errno.errorcode[errno_value]):
                ran: list[str] = []
                with unittest.mock.patch.object(
                        pipeline_module.fcntl, "flock",
                        side_effect=OSError(errno_value, os.strerror(errno_value))), \
                        self.assertLogs("station.pipeline", level="WARNING") as logs:
                    pipeline = self._pipeline(lambda: ran.append("ran") or "summary")
                    self.assertEqual(pipeline.run(lock_wait_s=30), "summary")
                self.assertEqual(ran, ["ran"])
                self.assertIn("going ahead without it", logs.output[0])

        self.logs = Path(self._tmp.name) / "logs_is_a_file"
        self.logs.write_text("a file where logs/ should be\n")
        with self.assertLogs("station.pipeline", level="WARNING") as logs:
            self.assertEqual(self._pipeline().run(lock_wait_s=30), "summary")
        self.assertIn("Could not open the run lock", logs.output[0])

    def test_the_cron_initialize_holds_the_lock_for_the_gnssir_json(self) -> None:
        # initialize() rewrites the station's gnssir json (through
        # GnssIrProcessor.initialize()); with lock_wait_s, as the cron
        # calls it, that waits for a run in progress
        done: list[str] = []

        def pipeline() -> Pipeline:
            p = Pipeline(cfg=SimpleNamespace(logs_dir=self.logs),
                         db=SimpleNamespace(is_connected=lambda: True))
            p._verify_directories = lambda: None
            p._verify_external_executables = lambda: None
            p._initialize_processors = lambda: done.append("processors")
            return p

        holder = self._hold_in_another_process()
        with self.assertRaises(PipelineBusyError):
            pipeline().initialize(lock_wait_s=0.2)
        self.assertEqual(done, [])
        # station_manager at startup does not wait
        self.assertEqual(pipeline().initialize(), "READY")
        self.assertEqual(done, ["processors"])

        holder.kill()
        holder.wait()
        self.assertEqual(pipeline().initialize(lock_wait_s=5), "READY")
        self.assertEqual(done, ["processors", "processors"])


# ------------------------------------------------------------------
# A whole run, file by file: a day that can never give results
# ------------------------------------------------------------------

NAME = "station_20261105.um980"          # day of year 309, as on CACO-05
DAY = "2026-11-05"


class FakeRinex:
    """RinexProcessor's convert()/verify(), writing a small .obs file."""

    def __init__(self, rinex_dir: Path):
        self.rinex_dir = rinex_dir
        self.converted: list[str] = []

    def convert(self, raw_path: Path):
        self.converted.append(raw_path.name)
        if not raw_path.exists():
            return SimpleNamespace(
                observation_file="", navigation_file="", sbas_file="", success=False,
                convbin_version="fake", message=f"Raw file does not exist: {raw_path}")
        obs = self.rinex_dir / f"{raw_path.stem}.obs"
        obs.write_text("     3.05           OBSERVATION DATA\n")
        return SimpleNamespace(observation_file=obs, navigation_file="", sbas_file="",
                               success=True, convbin_version="fake", message="ok")

    def verify(self, result) -> bool:
        return True


class FakeGnssir:
    """
    GnssIrProcessor's process()/verify(). `outcome` (or outcomes[day])
    is what process() reports; None returns a result without an
    outcome at all, as a processor or stand-in from before outcomes.
    """

    def __init__(self, products_dir: Path, outcome: str | None = "ok"):
        self.products_dir = products_dir
        self.outcome = outcome
        self.outcomes: dict = {}
        self.output_directory: str | None = None
        self.calls: list[tuple] = []

    def process(self, observation_file, day, retry_count=0):
        self.calls.append((day, retry_count))
        outcome = self.outcomes.get(day, self.outcome)
        output = (self.output_directory if self.output_directory is not None
                  else str(self.products_dir / str(day.year) / "results" / "usgs"))
        result = SimpleNamespace(
            success=outcome == "ok", message=f"fake {outcome}", output_directory=output,
            reflector_height=19.0 if outcome == "ok" else None, soil_moisture=None,
            snow_depth=None, quality_score=None, runtime_seconds=0.0,
            orbit_used="rapid", orbits_tried=("gnss (gnssrefl default)", "rapid", "nav"))
        if outcome is not None:
            result.outcome = outcome
        return result

    def verify(self, result) -> bool:
        return result.success


class PipelineRunTestCase(unittest.TestCase):
    """One real Pipeline and Database; convbin and gnssrefl faked."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Run from the temporary folder: a regression that writes a
        # marker into the working directory must land here, not in the
        # checkout, where it would fail every later run of these tests.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        for sub in ("raw", "rinex", "archive", "products", "logs"):
            (self.root / sub).mkdir()
        self.cfg = SimpleNamespace(
            raw_dir=self.root / "raw", rinex_dir=self.root / "rinex",
            archive_dir=self.root / "archive", products_dir=self.root / "products",
            logs_dir=self.root / "logs", station={})
        self.db = Database(path=str(self.root / "station.db"), log_file=None)
        self.db.connect()
        self.addCleanup(self.db.close)
        # every run after the first re-registers its raw files, which the
        # database refuses (UNIQUE filename) and logs with a traceback
        db_logger = logging.getLogger("station.database")
        self.addCleanup(db_logger.setLevel, db_logger.level)
        db_logger.setLevel(logging.CRITICAL)
        self.rinex = FakeRinex(self.cfg.rinex_dir)
        self.gnssir = FakeGnssir(self.cfg.products_dir)
        self.pipeline = self.new_pipeline()
        self.out = ""

    def new_pipeline(self) -> Pipeline:
        pipeline = Pipeline(cfg=self.cfg, db=self.db)
        pipeline._rinex = self.rinex
        pipeline._gnssir = self.gnssir
        pipeline._initialized = True
        return pipeline

    def raw(self, name: str = NAME, mtime: float | None = None) -> Path:
        path = self.cfg.raw_dir / name
        path.write_bytes(b"x" * 100)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def run_at(self, now: datetime):
        out = io.StringIO()
        with unittest.mock.patch.object(pipeline_module, "_utcnow", return_value=now), \
                contextlib.redirect_stdout(out):
            summary = self.pipeline.run(lock_wait_s=0)
        self.out = out.getvalue()
        return summary

    def entry(self, name: str = NAME):
        return self.db.processing_history(name)[0]

    def errors(self):
        return self.db.recent_errors(module="pipeline")

    def results(self, year: int = 2026) -> Path:
        return self.cfg.products_dir / str(year) / "results" / "usgs"

    def fail_times(self, n: int, name: str = NAME) -> None:
        for _ in range(n):
            self.db.start_processing(name)
            self.db.finish_processing(name, success=False, error_message="x")


NOV7 = datetime(2026, 11, 7, 0, 0, 5, tzinfo=timezone.utc)      # 1 day after the day
NOV11 = datetime(2026, 11, 11, 0, 0, 5, tzinfo=timezone.utc)    # 5 days and 5 s after


class TestDayWithoutResults(PipelineRunTestCase):

    def test_handoff_age_is_the_gps_only_fallback_age(self) -> None:
        # the run that hands a day over has tried every fallback, nav included
        self.assertEqual(HANDOFF_AGE_S, GPS_ONLY_FALLBACK_AGE_S)
        self.assertEqual(HANDOFF_AGE_S, 5 * 86400)
        self.assertEqual(HANDOFF_RETRIES, 10)

    def test_a_late_day_fails_and_is_retried_with_its_count(self) -> None:
        # The first misses of a day are ordinary failures, retried; the
        # processor gets the day's earlier failures as retry_count.
        self.raw()
        self.gnssir.outcome = "no_snr"
        for _ in range(3):
            summary = self.run_at(NOV7)
            self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
        self.assertEqual([c[1] for c in self.gnssir.calls], [0, 1, 2])
        self.assertTrue((self.cfg.raw_dir / NAME).exists())
        self.assertEqual(self.entry().retry_count, 3)
        self.assertFalse(self.entry().completed)
        errors = self.errors()
        self.assertEqual([e.severity for e in errors], ["ERROR"] * 3)
        for e in errors:
            self.assertTrue(e.description.startswith("GNSS-IR processing failed: fake no_snr"))

    def test_success_archives_as_before(self) -> None:
        self.raw()
        summary = self.run_at(NOV7)
        self.assertEqual((summary.files_processed, summary.files_failed), (1, 0))
        self.assertTrue((self.cfg.archive_dir / NAME).exists())
        self.assertTrue(self.entry().completed)
        self.assertIsNone(self.entry().error_message)
        self.assertEqual(self.db.latest_raw_file().notes, "archived after successful processing")
        self.assertEqual(self.db.gnssir_history(day=DAY)[0].notes, "fake ok")

    def test_no_arc_kept_marks_the_day_and_archives_it(self) -> None:
        self.raw()
        self.gnssir.outcome = "no_arcs"
        with self.assertLogs("station.pipeline", level="WARNING") as logs:
            summary = self.run_at(NOV7)

        marker = self.results() / "309.no_data"
        self.assertTrue(marker.exists())
        self.assertRegex(marker.read_text(),
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ -- no usable retrievals\n$")
        self.assertTrue((self.cfg.archive_dir / NAME).exists())
        self.assertFalse((self.cfg.raw_dir / NAME).exists())
        entry = self.entry()
        self.assertTrue(entry.completed)
        self.assertEqual(entry.retry_count, 0)
        self.assertIn("gnssir kept no arc", entry.error_message)
        self.assertIn("309.no_data", entry.error_message)
        self.assertIn("archived without results", self.db.latest_raw_file().notes)
        self.assertEqual((summary.files_failed, summary.files_no_data,
                          summary.files_processed, summary.products_created), (0, 1, 0, 0))
        product = self.db.gnssir_history(day=DAY)[0]
        self.assertEqual(product.notes, "fake no_arcs")
        self.assertFalse(product.processing_success)
        # one ERROR row that the marker does not clear: once is weather,
        # day after day a setting
        (error,) = self.errors()
        self.assertEqual(error.severity, "ERROR")
        self.assertIn("GNSS-IR kept no arc for 2026-11-05", error.description)
        self.assertIn("309.no_data written", error.description)
        self.assertNotIn("No results file produced", error.description)
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("NO DATA", logs.output[0])

        # never converted again
        summary = self.run_at(NOV7 + timedelta(days=1))
        self.assertEqual(self.rinex.converted, [NAME])
        self.assertEqual(summary.files_failed, 0)

    def test_marker_name_is_not_zero_padded(self) -> None:
        self.raw("station_20260105.um980")
        self.gnssir.outcome = "no_arcs"
        self.run_at(datetime(2026, 1, 7, 0, 0, 5, tzinfo=timezone.utc))
        self.assertTrue((self.results() / "5.no_data").exists())
        self.assertFalse((self.results() / "005.no_data").exists())

    def test_overnight_recording_with_no_arc_is_archived_without_a_marker(self) -> None:
        name = "overnight_20261105_120000.um980"
        self.raw(name, mtime=NOV7.timestamp() - 86400)
        self.gnssir.outcome = "no_arcs"
        summary = self.run_at(NOV7)
        self.assertTrue((self.cfg.archive_dir / name).exists())
        self.assertTrue(self.entry(name).completed)
        self.assertEqual(summary.files_no_data, 1)
        self.assertEqual(list(self.results().glob("*.no_data")), [])
        (error,) = self.errors()
        self.assertNotIn("no_data", error.description)

    def test_marker_that_cannot_be_written_is_an_ordinary_failure(self) -> None:
        self.raw()
        self.gnssir.outcome = "no_arcs"
        self.gnssir.output_directory = "."      # no results folder: never the cwd
        summary = self.run_at(NOV7)
        self.assertEqual((summary.files_failed, summary.files_no_data), (1, 0))
        self.assertTrue((self.cfg.raw_dir / NAME).exists())
        self.assertFalse(self.entry().completed)
        self.assertEqual(list(Path.cwd().glob("309.no_data")), [])

    def test_a_day_without_an_snr_file_is_handed_to_step_1_after_5_days(self) -> None:
        self.raw()
        self.gnssir.outcome = "no_snr"
        with self.assertLogs("station.pipeline", level="WARNING") as logs:
            summary = self.run_at(NOV11)

        self.assertTrue((self.cfg.archive_dir / NAME).exists())
        self.assertFalse((self.cfg.raw_dir / NAME).exists())
        entry = self.entry()
        self.assertTrue(entry.completed)
        self.assertIn("recover_missing_days", entry.error_message)
        self.assertIn("gnss (gnssrefl default), rapid, nav", entry.error_message)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (0, 1))
        self.assertEqual(list(self.results().glob("*.no_data")), [])
        # ONE WARNING, not an ERROR: in the station log ...
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertTrue(logs.output[0].startswith(f"WARNING:station.pipeline:HANDED OFF {NAME}"))
        self.assertIn("recover_missing_days", logs.output[0])
        # ... and as a WARNING row, the only trace when Step 2 hands the
        # day off (its output is shown only when the step fails)
        (row,) = self.errors()
        self.assertEqual(row.severity, "WARNING")
        self.assertEqual(row.description, logs.records[0].getMessage())
        self.assertIn("Handed Off           : 1", self.out)
        self.assertIn("No Usable Data       : 0", self.out)

        # the moved file is never converted, nor reported missing
        summary = self.run_at(NOV11 + timedelta(days=1))
        self.assertEqual(self.rinex.converted, [NAME])
        self.assertEqual(summary.files_failed, 0)
        self.assertFalse(any("does not exist" in e.description for e in self.errors()))

    def test_exactly_5_days_is_not_yet_handed_off(self) -> None:
        self.raw()
        self.gnssir.outcome = "no_snr"
        summary = self.run_at(datetime(2026, 11, 11, tzinfo=timezone.utc))
        self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
        self.assertTrue((self.cfg.raw_dir / NAME).exists())

    def test_a_day_is_handed_off_on_its_11th_attempt(self) -> None:
        # reruns by hand: from 72 h after the day, 10 failures are enough
        nov9 = datetime(2026, 11, 9, 0, 0, 5, tzinfo=timezone.utc)     # 72 h and 5 s after
        self.raw()
        self.gnssir.outcome = "no_snr"
        self.run_at(NOV7)                       # retry_count 1
        self.fail_times(8)                      # retry_count 9
        summary = self.run_at(nov9)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
        summary = self.run_at(nov9)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (0, 1))
        self.assertEqual([c[1] for c in self.gnssir.calls], [0, 9, 10])
        self.assertTrue((self.cfg.archive_dir / NAME).exists())

    def test_a_count_raised_while_gnssrefl_was_down_hands_off_no_young_day(self) -> None:
        # failures that never reached gnssrefl raise the count too: a day
        # whose orbit is only late is not given up for them
        self.raw()
        self.gnssir.outcome = "no_snr"
        self.run_at(NOV7)
        self.fail_times(HANDOFF_RETRIES + 5)
        summary = self.run_at(NOV7)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
        self.assertTrue((self.cfg.raw_dir / NAME).exists())

    def test_gps_only_no_arcs_is_handed_off_not_marked(self) -> None:
        # a GPS-only fallback kept no arc: a multi-GNSS orbit still might
        self.raw()
        self.gnssir.outcome = "no_arcs_gps_only"
        summary = self.run_at(NOV11)
        self.assertEqual((summary.files_handed_off, summary.files_no_data), (1, 0))
        self.assertEqual(list(self.results().glob("*.no_data")), [])

    def test_any_other_recording_is_given_up_too_and_retried_by_nobody(self) -> None:
        # recover_missing_days.py knows station day files only, so an
        # overnight_ recording is not handed to it -- but retried here it
        # would fail every run for good: archived, by the same rule
        name = "overnight_20261105_120000.um980"
        self.raw(name, mtime=NOV7.timestamp() - 86400)
        self.gnssir.outcome = "no_snr"
        summary = self.run_at(NOV7)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))

        with self.assertLogs("station.pipeline", level="WARNING") as logs:
            summary = self.run_at(NOV11)

        self.assertEqual((summary.files_failed, summary.files_handed_off), (0, 1))
        self.assertTrue((self.cfg.archive_dir / name).exists())
        self.assertFalse((self.cfg.raw_dir / name).exists())
        entry = self.entry(name)
        self.assertTrue(entry.completed)
        self.assertIn("nothing retries this recording", entry.error_message)
        self.assertNotIn("handed to", entry.error_message)
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertTrue(logs.records[0].getMessage().startswith(f"GIVEN UP {name}"))
        warning, first = self.errors()                  # newest first
        self.assertEqual((warning.severity, first.severity), ("WARNING", "ERROR"))
        self.assertTrue(warning.description.startswith(f"GIVEN UP {name}"))
        self.assertEqual(list(self.results().glob("*.no_data")), [])

        summary = self.run_at(NOV11 + timedelta(days=1))
        self.assertEqual(self.rinex.converted, [name, name])
        self.assertEqual(summary.files_failed, 0)

    def test_any_other_recording_waits_as_long_as_a_station_day(self) -> None:
        name = "overnight_20261105_120000.um980"
        self.raw(name, mtime=NOV7.timestamp() - 86400)
        self.gnssir.outcome = "no_snr"
        # exactly 5 days after the day, on its first attempt
        summary = self.run_at(datetime(2026, 11, 11, tzinfo=timezone.utc))
        self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
        # a raised count on a day not yet 72 h old
        self.fail_times(HANDOFF_RETRIES + 5, name)
        summary = self.run_at(NOV7)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
        self.assertTrue((self.cfg.raw_dir / name).exists())

    def test_a_handoff_whose_warning_row_cannot_be_written_is_still_no_failure(self) -> None:
        self.raw()
        self.gnssir.outcome = "no_snr"
        real_log_error = self.db.log_error

        def log_error(module, severity, *args, **kwargs):
            if severity == "WARNING":
                raise DatabaseError("Database operation failed: database is locked")
            return real_log_error(module, severity, *args, **kwargs)

        with unittest.mock.patch.object(self.db, "log_error", log_error):
            summary = self.run_at(NOV11)
        self.assertEqual((summary.files_failed, summary.files_handed_off), (0, 1))
        self.assertTrue(self.entry().completed)
        self.assertTrue((self.cfg.archive_dir / NAME).exists())

    def test_errors_are_never_handed_off(self) -> None:
        # a gnssrefl or setup problem is not a lack of data
        for outcome in ("error", None):
            with self.subTest(outcome=outcome):
                self.raw()
                self.gnssir.outcome = outcome
                self.run_at(NOV11)
                self.fail_times(HANDOFF_RETRIES)
                summary = self.run_at(NOV11 + timedelta(days=30))
                self.assertEqual((summary.files_failed, summary.files_handed_off), (1, 0))
                self.assertTrue((self.cfg.raw_dir / NAME).exists())
                self.assertEqual(self.errors()[0].severity, "ERROR")

    def test_a_failed_archive_is_an_ordinary_failure_and_the_run_goes_on(self) -> None:
        other = "station_20261104.um980"
        self.raw()
        self.raw(other)
        self.gnssir.outcome = "no_arcs"
        real_archive = Pipeline._archive

        def archive(pipeline, raw_path, job, notes="archived after successful processing"):
            if raw_path.name == other:
                raise OSError("disk full")
            return real_archive(pipeline, raw_path, job, notes=notes)

        with unittest.mock.patch.object(Pipeline, "_archive", archive):
            summary = self.run_at(NOV7)

        self.assertEqual((summary.files_failed, summary.files_no_data), (1, 1))
        self.assertTrue((self.cfg.raw_dir / other).exists())
        self.assertFalse(self.entry(other).completed)
        self.assertTrue(any("disk full" in e.description for e in self.errors()))
        self.assertTrue((self.cfg.archive_dir / NAME).exists())

    def test_a_queued_file_found_in_archive_is_finished_not_failed(self) -> None:
        # moved to archive/ by hand (README, "A day without results"), or
        # by a run stopped before finishing its entry: no "Raw file does
        # not exist" on every run
        path = self.raw()
        self.gnssir.outcome = "no_snr"
        self.run_at(NOV7)
        path.rename(self.cfg.archive_dir / NAME)
        with self.assertLogs("station.pipeline", level="WARNING") as logs:
            summary = self.run_at(NOV7 + timedelta(days=1))
        self.assertEqual((summary.files_failed, summary.files_handed_off), (0, 1))
        self.assertEqual(self.rinex.converted, [NAME])
        self.assertTrue(self.entry().completed)
        self.assertIn("already in archive/", self.entry().error_message)
        # the first attempt's ERROR, then this run's one WARNING
        self.assertEqual([e.severity for e in self.errors()], ["WARNING", "ERROR"])
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("HANDED OFF", logs.output[0])

    def test_an_entry_removed_during_the_run_is_skipped(self) -> None:
        # clean_processing_queue.py --execute does not take the run lock
        self.raw()
        real_start = self.db.start_processing

        def start(filename):
            with self.db._cursor() as cursor:
                cursor.execute("DELETE FROM processing_queue WHERE filename = ?", (filename,))
            return real_start(filename)

        with unittest.mock.patch.object(self.db, "start_processing", start):
            summary = self.run_at(NOV7)
        self.assertEqual((summary.files_failed, summary.files_processed), (0, 0))
        self.assertEqual(self.rinex.converted, [])

    def test_a_database_error_outside_the_steps_does_not_end_the_run(self) -> None:
        other = "station_20261104.um980"
        self.raw()
        self.raw(other)
        real_start = self.db.start_processing

        def start(filename):
            if filename == other:
                raise DatabaseError("Database operation failed: database is locked")
            return real_start(filename)

        with unittest.mock.patch.object(self.db, "start_processing", start):
            summary = self.run_at(NOV7)
        self.assertEqual((summary.files_failed, summary.files_processed), (1, 1))
        self.assertTrue((self.cfg.archive_dir / NAME).exists())
        self.assertIn("Pipeline Summary", self.out)

    def test_an_unreadable_retry_count_is_taken_as_0(self) -> None:
        self.raw()
        real_history = self.db.processing_history

        def history(filename=None):
            if filename is not None:
                raise sqlite3.OperationalError("database is locked")
            return real_history(filename)

        with unittest.mock.patch.object(self.db, "processing_history", history):
            summary = self.run_at(NOV7)
        self.assertEqual(summary.files_processed, 1)
        self.assertEqual(self.gnssir.calls[0][1], 0)


class TestFinishProcessingNote(PipelineRunTestCase):

    def test_a_finished_entry_keeps_its_note(self) -> None:
        for name, note, want in (("a.um980", "why", "why"), ("b.um980", "", None)):
            self.db.queue_file(name)
            self.db.start_processing(name)
            self.db.finish_processing(name, success=True, note=note)
            entry = self.entry(name)
            self.assertTrue(entry.completed)
            self.assertEqual(entry.error_message, want)

        self.db.queue_file("c.um980")
        self.db.start_processing("c.um980")
        self.db.finish_processing("c.um980", success=False, error_message="boom")
        entry = self.entry("c.um980")
        self.assertEqual((entry.retry_count, entry.error_message, entry.completed),
                         (1, "boom", False))


if __name__ == "__main__":
    unittest.main()
