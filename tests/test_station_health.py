"""
tests/test_station_health.py

The health checks that make silent failures visible in the morning
mail: a 7_day_plot.png that stopped being redrawn (process_and_plot.sh
reports success when the plot fails), and a tide prediction
spreadsheet that has run out (the predicted tide and the departure
markers then vanish from the plot without a word).
"""

from __future__ import annotations

import json
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
import gnss_record as gr  # noqa: E402

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


class TestArcStats(HealthTest):
    """Storm amplitude damping: arcs drop below gnssir's limits."""

    def write_results(self, day: datetime, n: int, amp: float, pknoise: float = 4.0) -> None:
        doy = day.timetuple().tm_yday
        path = (self.proj / "products" / "refl_code" / str(day.year) / "results"
                / STA / f"{doy:03d}.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "% year doy RH sat UTCtime Azim Amp eminO emaxO NumbOf freq rise EdotF "
            "PkNoise DelT MJD refr-appl\n"
            + "".join(f"{day.year} {doy} 19.000 {i % 32 + 1} {24 * i / n:.3f} 80.00 "
                      f"{amp + (i % 3) - 1:.2f} 5.05 14.95 200 1 1 0.7 {pknoise:.2f} 30.0 "
                      f"61000.0 1\n" for i in range(n)))

    def week(self, last_n: int, last_amp: float) -> None:
        for k in range(8):
            day = datetime(2026, 10, 12) + timedelta(days=k)
            if k < 7:
                self.write_results(day, n=400, amp=12.0)
            else:
                self.write_results(day, n=last_n, amp=last_amp, pknoise=3.1)
        gr.write_status(self.files, ok=True)

    def test_status_holds_per_day_arc_statistics(self) -> None:
        self.week(last_n=390, last_amp=11.0)
        st = json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())
        self.assertEqual(len(st["arc_stats"]), 8)
        self.assertEqual(st["arc_stats"][0],
                         {"day": "2026-10-12", "arcs": 400, "median_amp": 12.0,
                          "median_pknoise": 4.0})
        self.assertEqual(st["arc_stats"][-1]["day"], "2026-10-19")
        self.assertEqual(st["arc_stats"][-1]["median_pknoise"], 3.1)

    def test_steady_day_is_ok(self) -> None:
        self.week(last_n=390, last_amp=11.0)
        sh.check_arc_stats(STA)
        self.assertEqual([s for s, _ in self.findings("arcs")], ["OK"])

    def test_arc_count_drop_warns(self) -> None:
        self.week(last_n=150, last_amp=11.0)
        sh.check_arc_stats(STA)
        (status, message), = self.findings("arcs")
        self.assertEqual(status, "WARN")
        self.assertIn("drop in arcs", message)

    def test_amplitude_drop_warns(self) -> None:
        self.week(last_n=390, last_amp=7.0)
        sh.check_arc_stats(STA)
        (status, message), = self.findings("arcs")
        self.assertEqual(status, "WARN")
        self.assertIn("amplitude", message)

    def test_too_few_days_says_nothing(self) -> None:
        self.write_results(datetime(2026, 10, 19), n=10, amp=5.0)
        gr.write_status(self.files, ok=True)
        sh.check_arc_stats(STA)
        self.assertEqual(self.findings("arcs"), [])

    def test_no_status_file_says_nothing(self) -> None:
        sh.check_arc_stats(STA)
        self.assertEqual(self.findings("arcs"), [])


class TestGapSupport(HealthTest):
    """Levels drawn across gaps in the arcs subdaily kept (support file
    and gap_support in record_status.json, written by gnss_record.py)."""

    SETTINGS = "-rhdot True -knots 8 -azim1 35 -azim2 125"

    def write_support(self, arcs_at, settings=SETTINGS, days=8,
                      last=datetime(2026, 10, 19, 23, 30)) -> None:
        """A support file for 30-minute levels over `days` days to `last`,
        from kept arcs at the UTC hours arcs_at(day) gives for each day."""
        t = last - timedelta(days=days)
        lines, arcs = [], []
        while t <= last:
            m = (t - MJD0).total_seconds() / 86400
            lines.append(f"{m:15.7f}      5.000 {t.year:4d} {t.month:3d} {t.day:3d} {t.hour:3d} "
                         f"{t.minute:3d}   0      0.500\n")
            t += timedelta(minutes=30)
        day = (last - timedelta(days=days + 1)).replace(hour=0, minute=0)
        while day <= last:
            arcs += [(day - MJD0).total_seconds() / 86400 + h / 24 for h in arcs_at(day)]
            day += timedelta(days=1)
        (self.files / f"{STA}_spline_support.txt").write_text("".join(
            gr.support_text(STA, settings, "test", gr.support_lines(lines, sorted(arcs)))))
        gr.write_status(self.files, ok=True)

    @staticmethod
    def every_half_hour(day, skip=()):
        return [k / 2 + 0.25 for k in range(48)
                if not any(a < k / 2 + 0.25 < b for a, b in skip)]

    def test_well_supported_week_is_ok(self) -> None:
        self.write_support(self.every_half_hour)
        sh.check_gap_support(STA)
        (status, message), = self.findings("support")
        self.assertEqual(status, "OK")
        self.assertIn("newest 7 days: 0 of 337 levels (0.0 %) more than 2 h", message)

    def test_many_levels_far_from_any_arc_warn(self) -> None:
        # two 5.0 h gaps a day (none over 5 h): 4 levels a day over 2 h, 8 %
        self.write_support(lambda d: self.every_half_hour(d, [(1, 5.5), (13, 17.5)]))
        st = json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())["gap_support"]
        self.assertEqual((st["unsupported_rows"], st["rows_in_gaps_over_5h"]), (28, 0))
        sh.check_gap_support(STA)
        (status, message), = self.findings("support")
        self.assertEqual(status, "WARN")
        self.assertIn("over 5 % of the week is spline across gaps in the arcs", message)
        self.assertNotIn("fake data", message)

    def test_band_left_by_a_smaller_gap_min_val_is_said(self) -> None:
        # gap_min_val is 5 already: a 5.5 h gap is left only next to the newest arc
        def arcs_at(day):
            hours = self.every_half_hour(day)
            return [h for h in hours if h < 18] + [23.75] if day.day == 19 else hours
        self.write_support(arcs_at, settings=self.SETTINGS + " -gap_min_val 5")
        st = json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())["gap_support"]
        self.assertEqual(st["gap_min_val_hours"], 5.0)
        self.assertGreater(st["rows_in_gaps_over_5h"], 0)
        sh.check_gap_support(STA)
        (status, message), = self.findings("support")
        self.assertEqual(status, "WARN")
        self.assertIn("next to the newest arc", message)
        self.assertNotIn("subdaily_gap_min_val 5 in station.json", message)

    def test_status_without_gap_support_says_nothing(self) -> None:
        gr.write_status(self.files, ok=True)                  # no support file yet
        self.assertIsNone(json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())
                          ["gap_support"])
        sh.check_gap_support(STA)
        (self.files / "fit" / gr.STATUS_NAME).write_text(json.dumps({"ok": True}))  # older status
        sh.check_gap_support(STA)
        self.assertEqual(self.findings("support"), [])


if __name__ == "__main__":
    unittest.main()
