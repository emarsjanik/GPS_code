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

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))

from pipeline import _date_from_filename  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
