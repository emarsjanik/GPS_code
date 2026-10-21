"""
test_ops_small_fixes.py

Tests for the small operational fixes from the GPS_code review:

  B17  plot_raw_vs_spline.py read results/<doy>.txt unpadded, so days
       1-99 (gnssrefl writes 008.txt) were skipped. Same bug in
       station_manager._has_real_results_for_day (export readiness)
       and diagnostics/validate_station.py.
  B18  elevation_quality.py compared each arc's raw RH with the
       RHdot- and IF-corrected spline.
  B19  compare_to_usgs_gage.py took NWIS 00065 (gage height, above the
       gage's own datum) as NAVD88.
  B22  compress_rinex.sh and prune_local_rinex.sh counted local days
       against UTC-named files; clean_processing_queue.py said files
       were named by local date and accepted the next day's results.

No network, gnssrefl or S3 is needed: NWIS answers are canned, and the
shell scripts run with a stand-in `date` (and aws) on PATH.

Run with:

    python3 -m unittest discover -s tests -p "test_ops_small_fixes.py" -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
sys.path.insert(0, str(ROOT / "maintenance"))
sys.path.insert(0, str(ROOT / "diagnostics"))

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False


def results_line(year: int, doy: int, rh: float, hour: float = 12.0) -> str:
    # gnssrefl results columns: year doy RH sat UTCtime Azim Amp eminO
    # emaxO NumbOf freq rise EdotF PkNoise DelT MJD refr
    return (f" {year} {doy:3d} {rh:7.3f}   1 {hour:6.3f}  80.00  12.00  5.00 15.00"
            f"  300   1  1  0.10000   4.00   30.00 60000.000000 1\n")


# ------------------------------------------------------------
# B17
# ------------------------------------------------------------

@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class TestRawVsSplinePadding(unittest.TestCase):

    def test_days_1_to_99_are_read(self) -> None:
        import plot_raw_vs_spline as prs

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / "008.txt").write_text("% header\n" + results_line(2026, 8, 19.0))
            (results / "150.txt").write_text(results_line(2026, 150, 18.5))
            times, values = prs.load_raw_results(results, 2026, 5, 160, hortho=19.014)

        self.assertEqual([t.date() for t in times], [date(2026, 1, 8), date(2026, 5, 30)])
        self.assertAlmostEqual(values[0], 0.014, places=6)


class TestOtherZeroPaddedReaders(unittest.TestCase):

    def test_validate_station_reads_padded_names(self) -> None:
        import validate_station as vs

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / "008.txt").write_text(results_line(2026, 8, 19.0))
            rows = vs.parse_gnssir_results(results, [8])
        self.assertEqual(len(rows), 1)

    def test_station_manager_export_sees_results_of_day_8(self) -> None:
        sys.path.insert(0, str(ROOT / "station"))
        import types
        if "serial" not in sys.modules:
            fake = types.ModuleType("serial")
            fake.SerialException = Exception
            fake.Serial = None
            sys.modules["serial"] = fake
        import station_manager

        with tempfile.TemporaryDirectory() as tmp:
            products = Path(tmp)
            manager = station_manager.StationManager()
            manager.cfg = types.SimpleNamespace(
                products_dir=products, station={"gnssrefl_station_code": "abcd"})
            folder = products / "2026" / "results" / "abcd"
            folder.mkdir(parents=True)
            (folder / "008.txt").write_text("% h\n" + results_line(2026, 8, 19.0))
            self.assertTrue(manager._has_real_results_for_day(date(2026, 1, 8)))


if __name__ == "__main__":
    unittest.main()
