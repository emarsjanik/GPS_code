"""
tests/test_station_health.py

The health checks that make silent failures visible in the morning
mail: a 7_day_plot.png that stopped being redrawn (process_and_plot.sh
reports success when the plot fails), and a tide prediction
spreadsheet that has run out (the predicted tide and the departure
markers then vanish from the plot without a word).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "diagnostics"))
import station_health as sh  # noqa: E402

STA = "usgs"
MJD0 = datetime(1858, 11, 17)

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None


class HealthTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        self.files = self.proj / "products" / "refl_code" / "Files" / STA
        self.files.mkdir(parents=True)
        patch = unittest.mock.patch.object(sh, "PROJECT_DIR", self.proj)
        patch.start()
        self.addCleanup(patch.stop)
        sh.findings.clear()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def findings(self, check: str):
        return [(f[0], f[2]) for f in sh.findings if f[1] == check]

    def write_spline(self, newest: datetime, mtime: float | None = None) -> Path:
        path = self.files / f"{STA}_spline_out.txt"
        lines = ["% fake spline\n"]
        t = newest - timedelta(days=8)
        while t <= newest:
            m = (t - MJD0).total_seconds() / 86400
            lines.append(f"{m:.6f} 0 {t.year} {t.month} {t.day} {t.hour} {t.minute} 0 0.500\n")
            t += timedelta(minutes=30)
        path.write_text("".join(lines))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def write_plot(self, age_h: float) -> Path:
        path = self.files / "7_day_plot.png"
        path.write_bytes(b"png")
        t = time.time() - age_h * 3600
        os.utime(path, (t, t))
        return path


class TestPublicPlot(HealthTest):

    def test_fresh_plot_is_ok(self) -> None:
        self.write_spline(datetime(2026, 10, 19), mtime=time.time() - 9 * 3600)
        self.write_plot(age_h=9)
        sh.check_public_plot(STA)
        self.assertEqual([s for s, _ in self.findings("plot")], ["OK"])

    def test_stale_plot_warns(self) -> None:
        self.write_spline(datetime(2026, 10, 19), mtime=time.time() - 3 * 86400)
        self.write_plot(age_h=3 * 24)
        sh.check_public_plot(STA)
        (status, message), = self.findings("plot")
        self.assertEqual(status, "WARN")
        self.assertIn("not updating", message)

    def test_plot_older_than_the_record_warns(self) -> None:
        # The fit ran tonight, the plot did not: within 36 h, but wrong.
        self.write_spline(datetime(2026, 10, 19), mtime=time.time() - 9 * 3600)
        self.write_plot(age_h=33)
        sh.check_public_plot(STA)
        (status, message), = self.findings("plot")
        self.assertEqual(status, "WARN")
        self.assertIn("last night's plot failed", message)

    def test_missing_plot_warns(self) -> None:
        sh.check_public_plot(STA)
        self.assertEqual([s for s, _ in self.findings("plot")], ["WARN"])


@unittest.skipIf(openpyxl is None, "openpyxl not installed")
class TestTideCoverage(HealthTest):

    def write_tide(self, first: datetime, last: datetime) -> Path:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["time", "tide_m"])
        t = first
        while t <= last:
            ws.append([t, 0.5])
            t += timedelta(minutes=10)
        path = self.proj / "tides.xlsx"
        wb.save(path)
        return path

    def cfg(self) -> dict:
        return {"tide_model_file": str(self.proj / "tides.xlsx"),
                "tide_model_value_column": "tide_m"}

    def test_covering_prediction_is_ok(self) -> None:
        self.write_spline(datetime(2026, 10, 19, 23, 30))
        self.write_tide(datetime(2026, 10, 1), datetime(2026, 10, 31))
        sh.check_tide_coverage(STA, self.cfg())
        self.assertEqual([s for s, _ in self.findings("tide")], ["OK"])

    def test_prediction_ending_within_two_days_warns(self) -> None:
        self.write_spline(datetime(2026, 10, 19, 23, 30))
        self.write_tide(datetime(2026, 10, 1), datetime(2026, 10, 21))
        sh.check_tide_coverage(STA, self.cfg())
        (status, message), = self.findings("tide")
        self.assertEqual(status, "WARN")
        self.assertIn("within 2 days", message)

    def test_prediction_already_ended_warns(self) -> None:
        self.write_spline(datetime(2026, 10, 19, 23, 30))
        self.write_tide(datetime(2026, 10, 1), datetime(2026, 10, 15))
        sh.check_tide_coverage(STA, self.cfg())
        (status, message), = self.findings("tide")
        self.assertEqual(status, "WARN")
        self.assertIn("already missing", message)

    def test_prediction_starting_inside_the_window_warns(self) -> None:
        self.write_spline(datetime(2026, 10, 19, 23, 30))
        self.write_tide(datetime(2026, 10, 16), datetime(2026, 10, 31))
        sh.check_tide_coverage(STA, self.cfg())
        self.assertEqual([s for s, _ in self.findings("tide")], ["WARN"])

    def test_missing_file_warns(self) -> None:
        sh.check_tide_coverage(STA, self.cfg())
        self.assertEqual([s for s, _ in self.findings("tide")], ["WARN"])

    def test_no_tide_model_configured_is_not_a_warning(self) -> None:
        sh.check_tide_coverage(STA, {})
        self.assertEqual([s for s, _ in self.findings("tide")], ["OK"])

    def test_reader_without_openpyxl_agrees(self) -> None:
        # cron runs station_health.py under the system python, which may
        # not have openpyxl: the xlsx is then read directly.
        path = self.write_tide(datetime(2026, 10, 1, 0, 0), datetime(2026, 10, 31, 12, 0))
        expected = sh._xlsx_column_range(path, "time")
        got = sh._xlsx_column_range_stdlib(path, "time")
        self.assertEqual(expected, (datetime(2026, 10, 1), datetime(2026, 10, 31, 12)))
        self.assertEqual([abs((g - e).total_seconds()) < 1 for g, e in zip(got, expected)],
                         [True, True])


if __name__ == "__main__":
    unittest.main()
