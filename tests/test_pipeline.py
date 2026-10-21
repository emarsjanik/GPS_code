"""
tests/test_pipeline.py

Focused regression tests for the filename-based date extraction fix
in pipeline.py.

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

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))

from pipeline import Pipeline, _date_from_filename, _in_progress_reason  # noqa: E402


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
        # 02:30 UTC is 22:30 EDT: yesterday's UTC file is complete.
        path = self._raw("station_20261019.um980", mtime=self.now.timestamp() - 3600)
        self.assertIsNone(_in_progress_reason(path, now=self.now))

    def test_recently_written_undated_file_is_in_progress(self) -> None:
        path = self._raw("test_recording.um980", mtime=time.time() - 60)
        self.assertIsNotNone(_in_progress_reason(path))
        path = self._raw("test_recording2.um980", mtime=time.time() - 3600)
        self.assertIsNone(_in_progress_reason(path))

    def test_day_just_closed_is_finished_at_the_rollover_run(self) -> None:
        # station_manager runs the pipeline at 00:00 UTC, straight after
        # the day's last chunk: yesterday's file was written seconds ago
        # and is complete. Left as in progress, the inline run (the one
        # that processes the record) did that day a night late.
        rollover = datetime(2026, 10, 20, 0, 0, 5, tzinfo=timezone.utc)
        path = self._raw("station_20261019.um980", mtime=rollover.timestamp() - 3)
        self.assertIsNone(_in_progress_reason(path, now=rollover))
        today = self._raw("station_20261020.um980", mtime=rollover.timestamp() - 3)
        self.assertIsNotNone(_in_progress_reason(today, now=rollover))

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
        self.assertIsNone(_in_progress_reason(path, now=now))

    def test_station_file_dated_after_today_keeps_the_written_time(self) -> None:
        # a clock that was set back since recording began: not a closed day
        path = self._raw("station_20261021.um980", mtime=self.now.timestamp() - 60)
        self.assertIsNotNone(_in_progress_reason(path, now=self.now))

    def test_rollover_run_scans_the_day_just_closed(self) -> None:
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        self._raw(f"station_{yesterday}.um980", mtime=time.time() - 2)
        self._raw(f"station_{now:%Y%m%d}.um980", mtime=time.time() - 2)

        pipeline, _ = self._pipeline()

        self.assertEqual([p.name for p in pipeline._scan_raw_directory()],
                         [f"station_{yesterday}.um980"])

    def test_missing_file_with_old_date_is_not_in_progress(self) -> None:
        self.assertIsNone(_in_progress_reason(self.raw_dir / "station_20261001.um980"))

    def _pipeline(self, queued=()) -> tuple[Pipeline, list[str]]:
        entries = [SimpleNamespace(filename=name, completed=False) for name in queued]
        db = SimpleNamespace(processing_history=lambda filename=None: entries)
        pipeline = Pipeline(cfg=SimpleNamespace(raw_dir=self.raw_dir), db=db)
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


if __name__ == "__main__":
    unittest.main()
