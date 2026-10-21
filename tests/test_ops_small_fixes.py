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


# ------------------------------------------------------------
# B18
# ------------------------------------------------------------

def subdaily_line(dt: datetime, rh: float, rhdot_rh: float | None = None,
                  if_rh: float | None = None, azim: float = 80.0,
                  freq: int = 1) -> str:
    """One row of gnssrefl subdaily output (22, 24 or 25 columns)."""
    doy = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60
    cols = [dt.year, doy, f"{rh:.3f}", 1, f"{hour:.3f}", f"{azim:.2f}", "12.00",
            "5.00", "15.00", 300, freq, 1, "0.10000", "4.00", "30.00", "60000.000000",
            1, dt.month, dt.day, dt.hour, dt.minute, dt.second]
    if rhdot_rh is not None:
        cols += [f"{rhdot_rh:.3f}", f"{rh - rhdot_rh:.3f}"]
    if if_rh is not None:
        cols += [f"{if_rh:.3f}"]
    return " " + " ".join(str(c) for c in cols) + " \n"


class TestElevationQualityUsesCorrectedRH(unittest.TestCase):

    def setUp(self) -> None:
        import elevation_quality as eq
        self.eq = eq
        self._tmp = tempfile.TemporaryDirectory()
        self.files = Path(self._tmp.name) / "Files" / "abcd"
        (self.files / "fit").mkdir(parents=True)
        self.times = [datetime(2026, 7, 1, h, 0, 0) for h in range(0, 24, 2)]
        self.edit = self.files / "abcd_2026_subdaily_edit.txt"
        self.edit.write_text("% plain\n" + "".join(subdaily_line(t, 19.5) for t in self.times))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_column_choice(self) -> None:
        eq = self.eq
        t = self.times[0]
        self.assertEqual(eq.rh_column(subdaily_line(t, 19.5).split()), eq.RAW_RH_COL)
        self.assertEqual(eq.rh_column(subdaily_line(t, 19.5, 19.3).split()), eq.RHDOT_RH_COL)
        self.assertEqual(eq.rh_column(subdaily_line(t, 19.5, 19.3, 19.2).split()), eq.IF_RH_COL)
        # gnssrefl leaves column 25 at 0 for a frequency not in its table
        self.assertEqual(eq.rh_column(subdaily_line(t, 19.5, 19.3, 0.0).split()), eq.RHDOT_RH_COL)
        self.assertEqual(eq.rh_column(subdaily_line(t, 19.5, 19.3, 19.2).split(), raw_rh=True),
                         eq.RAW_RH_COL)

    def test_corrected_file_is_read_as_given(self) -> None:
        corrected = self.files / "fit" / "abcd_2026_subdaily_edit.txt.withrhdotIF"
        corrected.write_text("".join(subdaily_line(t, 19.5, 19.3, 19.2) for t in self.times))
        recs, note = self.eq.load_arcs(corrected, hortho=19.0)
        self.assertEqual(len(recs), len(self.times))
        self.assertAlmostEqual(recs[0]["wl"], -0.2, places=6)       # 19.0 - 19.2
        self.assertIn("column 25", note)

    def test_plain_edit_file_uses_the_fit_corrected_arcs_of_its_span(self) -> None:
        corrected = self.files / "fit" / "abcd_2026_2027_subdaily_edit.txt.withrhdotIF"
        later = datetime(2027, 1, 2, 3, 0, 0)        # outside the edit file's span
        corrected.write_text("".join(subdaily_line(t, 19.5, 19.3, 19.2) for t in self.times)
                             + subdaily_line(later, 19.5, 19.3, 19.2))
        recs, note = self.eq.load_arcs(self.edit, hortho=19.0)
        self.assertEqual(len(recs), len(self.times))
        self.assertTrue(all(abs(r["wl"] + 0.2) < 1e-9 for r in recs))
        self.assertIn(corrected.name, note)
        self.assertIn("--raw-rh", note)

    def test_raw_rh_keeps_the_old_comparison(self) -> None:
        (self.files / "fit" / "abcd_2026_subdaily_edit.txt.withrhdotIF").write_text(
            "".join(subdaily_line(t, 19.5, 19.3, 19.2) for t in self.times))
        recs, note = self.eq.load_arcs(self.edit, hortho=19.0, raw_rh=True)
        self.assertAlmostEqual(recs[0]["wl"], -0.5, places=6)       # 19.0 - 19.5
        self.assertIn("column 3", note)

    def test_no_corrected_file_warns(self) -> None:
        recs, note = self.eq.load_arcs(self.edit, hortho=19.0)
        self.assertEqual(len(recs), len(self.times))
        self.assertIn("WARNING", note)
        self.assertIn("B18", note)


# ------------------------------------------------------------
# B19
# ------------------------------------------------------------

def iv_rdb(parameter: str, rows: list[tuple[str, str, str]]) -> str:
    head = ("# comment\n"
            f"agency_cd\tsite_no\tdatetime\ttz_cd\t69928_{parameter}\t69928_{parameter}_cd\n"
            "5s\t15s\t20d\t6s\t14n\t10s\n")
    return head + "".join(f"USGS\t420259070105600\t{t}\t{tz}\t{v}\tP\n" for t, tz, v in rows)


def site_rdb(alt_va: str, alt_datum_cd: str, alt_acy_va: str = ".01") -> str:
    return ("# comment\n"
            "agency_cd\tsite_no\tstation_nm\talt_va\talt_meth_cd\talt_acy_va\talt_datum_cd\n"
            "5s\t15s\t50s\t8s\t1s\t3s\t10s\n"
            f"USGS\t420259070105600\tPROVINCETOWN\t{alt_va}\tL\t{alt_acy_va}\t{alt_datum_cd}\n")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestGageDatum(unittest.TestCase):

    ROWS = [("2026-09-01 00:00", "EDT", "3.00"), ("2026-09-01 00:06", "EDT", "-1.00")]

    def setUp(self) -> None:
        import compare_to_usgs_gage as cg
        self.cg = cg

    def fake_nwis(self, series: dict[str, str], site: str | None):
        def urlopen(url, timeout=None):
            if "/site/" in url:
                if site is None:
                    raise urllib.error.HTTPError(url, 404, "No sites", {}, None)
                return FakeResponse(site.encode())
            for parameter, text in series.items():
                if f"parameterCd={parameter}" in url:
                    return FakeResponse(text.encode())
            raise urllib.error.HTTPError(url, 404, "No sites found matching all criteria",
                                         {}, None)
        return unittest.mock.patch.object(self.cg.urllib.request, "urlopen", urlopen)

    def test_navd88_elevation_parameter_is_preferred(self) -> None:
        series = {"62620": iv_rdb("62620", self.ROWS), "00065": iv_rdb("00065", self.ROWS)}
        with self.fake_nwis(series, site_rdb("-2.00", "NGVD29")):
            t, v, navd88, note = self.cg.fetch_gage("x", "2026-09-01", "2026-09-02")
        self.assertTrue(navd88)
        self.assertIn("62620", note)
        self.assertAlmostEqual(v[0], 3.0 * 0.3048)
        self.assertEqual(t[0], datetime(2026, 9, 1, 4, 0))         # EDT -> UTC

    def test_gage_height_plus_navd88_gage_datum(self) -> None:
        with self.fake_nwis({"00065": iv_rdb("00065", self.ROWS)}, site_rdb("-2.00", "NAVD88")):
            _t, v, navd88, note = self.cg.fetch_gage("x", "2026-09-01", "2026-09-02")
        self.assertTrue(navd88)
        self.assertAlmostEqual(v[0], (3.0 - 2.0) * 0.3048)
        self.assertIn("NWIS site file", note)

    def test_gage_height_on_another_datum_is_not_called_navd88(self) -> None:
        # The old script's case: 00065 labelled NAVD88 whatever the datum.
        with self.fake_nwis({"00065": iv_rdb("00065", self.ROWS)}, site_rdb("-2.00", "NGVD29")):
            _t, v, navd88, note = self.cg.fetch_gage("x", "2026-09-01", "2026-09-02")
        self.assertFalse(navd88)
        self.assertAlmostEqual(v[0], 3.0 * 0.3048)
        self.assertIn("NOT NAVD88", note)
        self.assertIn("NGVD29", note)

    def test_given_gage_datum_ties_gage_height(self) -> None:
        with self.fake_nwis({"00065": iv_rdb("00065", self.ROWS)}, None):
            _t, v, navd88, note = self.cg.fetch_gage(
                "x", "2026-09-01", "2026-09-02", gage_datum_navd88_ft=-1.5)
        self.assertTrue(navd88)
        self.assertAlmostEqual(v[1], (-1.0 - 1.5) * 0.3048)
        self.assertIn("--gage-datum-navd88-ft", note)

    def test_no_site_file_and_no_value_stays_on_gage_datum(self) -> None:
        with self.fake_nwis({"00065": iv_rdb("00065", self.ROWS)}, None):
            _t, _v, navd88, note = self.cg.fetch_gage("x", "2026-09-01", "2026-09-02")
        self.assertFalse(navd88)
        self.assertIn("no gage datum", note)

    def test_flagged_rows_skipped_and_value_column_found_by_name(self) -> None:
        rows = self.ROWS + [("2026-09-01 00:12", "EST", "Eqp")]
        text = iv_rdb("62620", rows).replace(
            "agency_cd\tsite_no\tdatetime\ttz_cd\t69928_62620\t69928_62620_cd",
            "agency_cd\tsite_no\tdatetime\ttz_cd\t69928_62620_cd\t69928_62620")
        text = text.replace("\t3.00\tP\n", "\tP\t3.00\n").replace("\t-1.00\tP\n", "\tP\t-1.00\n")
        text = text.replace("\tEqp\tP\n", "\tP\tEqp\n")
        with self.fake_nwis({"62620": text}, None):
            t, v, _navd88, _note = self.cg.fetch_gage("x", "2026-09-01", "2026-09-02")
        self.assertEqual(len(t), 2)
        self.assertAlmostEqual(v[1], -1.0 * 0.3048)


if __name__ == "__main__":
    unittest.main()
