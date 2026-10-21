"""
test_ops_small_fixes.py

Tests for the small operational fixes from the GPS_code review:

  B17  plot_raw_vs_spline.py read results/<doy>.txt unpadded, so days
       1-99 (gnssrefl writes 008.txt) were skipped. Same bug in
       diagnostics/validate_station.py. (station_manager's export
       readiness check is left as it is: it also looks outside
       REFL_CODE, and fixing it arms the export, VERIFIED.md D3/M20.)
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
from datetime import date, datetime, timedelta, timezone
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


# ------------------------------------------------------------
# B18
# ------------------------------------------------------------

def subdaily_line(dt: datetime, rh: float, rhdot_rh: float | None = None,
                  if_rh: float | None = None, azim: float = 80.0,
                  freq: int = 1, emin: float = 5.0, emax: float = 15.0) -> str:
    """One row of gnssrefl subdaily output (22, 24 or 25 columns)."""
    doy = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60
    cols = [dt.year, doy, f"{rh:.3f}", 1, f"{hour:.3f}", f"{azim:.2f}", "12.00",
            f"{emin:.2f}", f"{emax:.2f}", 300, freq, 1, "0.10000", "4.00", "30.00",
            "60000.000000",
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
        self.assertIn("WARNING", note)          # the arcs the fit dropped are missing

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

    def test_arcs_the_fit_dropped_are_kept_and_corrected(self) -> None:
        # Arc 3 is missing from the fit (its 3-sigma edit dropped it):
        # it is kept, with the RHdot term from the spline slope and the
        # fit's IF bias for its frequency (19.3 - 19.2 = 0.1 m).
        (self.files / "fit" / "abcd_2026_subdaily_edit.txt.withrhdotIF").write_text(
            "".join(subdaily_line(t, 19.5, 19.3, 19.2)
                    for i, t in enumerate(self.times) if i != 3))
        t0 = self.times[0]
        spline = ([t0, t0 + timedelta(hours=1)], [0.0, -0.5])   # RH rising 0.5 m/hour
        recs, note = self.eq.load_arcs(self.edit, hortho=19.0, spline=spline)
        self.assertEqual(len(recs), len(self.times))
        self.assertEqual([i for i, r in enumerate(recs) if r["fit_dropped"]], [3])
        # RH 19.5 - EdotF 0.1 h * 0.5 m/h - bias 0.1 = 19.35
        self.assertAlmostEqual(recs[3]["wl"], 19.0 - 19.35, places=6)
        self.assertAlmostEqual(recs[0]["wl"], -0.2, places=6)      # the fit's column 25
        self.assertIn("1 the fit's 3-sigma edits dropped, 0 outside its span", note)

    def test_outlying_arcs_the_fit_dropped_still_count(self) -> None:
        # The top elevation bin has arcs 0.6 m off (land), which the
        # fit's 3-sigma edits removed from its .withrhdotIF. They must
        # still show in that bin and make the verdict "Systematic".
        import elevation_quality as eq
        start = datetime(2026, 7, 1)
        plain, fit = ["% plain\n"], []
        bins = [(5, 7), (7, 9), (9, 11), (11, 13), (13, 15)]
        n = 0
        for lo, hi in bins:
            for i in range(40):
                t = start + timedelta(minutes=3 * n)
                n += 1
                rh = 19.5 + (0.05 if i % 2 else -0.05)
                land = (lo, hi) == (13, 15) and i % 4 == 0
                plain.append(subdaily_line(t, rh - 0.6 if land else rh, emin=lo, emax=hi))
                if not land:
                    fit.append(subdaily_line(t, rh, rh, rh, emin=lo, emax=hi))
        self.edit.write_text("".join(plain))
        (self.files / "fit" / "abcd_2026_subdaily_edit.txt.withrhdotIF").write_text("".join(fit))
        spline = self.files / "abcd_spline_out.txt"
        spline.write_text("".join(
            f"  0 19.5 2026 7 1 {h} {m} 0 -0.5\n" for h in range(24) for m in (0, 30)))

        out = io.StringIO()
        argv = ["elevation_quality.py", "--subdaily", str(self.edit),
                "--spline", str(spline), "--hortho", "19.0"]
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                eq.np.errstate(invalid="ignore"):        # peak-to-noise is constant here
            self.assertEqual(eq.main(), 0)
        text = out.getvalue()
        elevation = text.split("By mean arc elevation")[1].split("By arc upper")[0]
        self.assertIn("worst is 13-15deg. Systematic", elevation)
        self.assertIn("   10 (25.0%)", elevation)
        self.assertIn("200 arcs compared", text)

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


# ------------------------------------------------------------
# B22
# ------------------------------------------------------------

FAKE_DATE = """#!/bin/bash
# `date`, with "now" = $FAKE_NOW for relative dates and for no -d.
args=(); have_d=0
while [ $# -gt 0 ]; do
  if [ "$1" = -d ]; then
    case "$2" in *ago*) args+=(-d "$FAKE_NOW $2");; *) args+=(-d "$2");; esac
    have_d=1; shift 2
  else args+=("$1"); shift; fi
done
[ $have_d = 1 ] || args+=(-d "$FAKE_NOW")
exec /bin/date "${args[@]}"
"""


class ShellDatesTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "date").write_text(FAKE_DATE)
        (self.bin / "date").chmod(0o755)
        self.proj = self.tmp / "proj"
        (self.proj / "maintenance").mkdir(parents=True)
        (self.proj / "rinex").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_script(self, name, *args, now, tz, extra_env=None):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   FAKE_NOW=now, TZ=tz, **(extra_env or {}))
        return subprocess.run(["bash", str(self.proj / "maintenance" / name), *args],
                              env=env, capture_output=True, text=True, timeout=120)


class TestCompressRinexUtcDays(ShellDatesTestCase):

    def setUp(self) -> None:
        super().setUp()
        (self.proj / "maintenance" / "compress_rinex.sh").write_text(
            (ROOT / "maintenance" / "compress_rinex.sh").read_text())
        for day in range(15, 23):
            (self.proj / "rinex" / f"station_202610{day:02d}.obs").write_text("x\n")

    def kept(self, keep_days, now, tz):
        r = self.run_script("compress_rinex.sh", "--keep-days", str(keep_days), now=now, tz=tz)
        self.assertEqual(r.returncode, 0, r.stderr)
        return sorted(line.split()[1] for line in r.stdout.splitlines()
                      if line.strip().startswith("[skip]"))

    def test_window_is_today_utc_and_the_n_days_before(self) -> None:
        # 22:30 EDT on 21 Oct is already 22 Oct UTC
        self.assertEqual(self.kept(1, "2026-10-22 02:30 UTC", "America/New_York"),
                         ["station_20261021", "station_20261022"])
        self.assertEqual(self.kept(3, "2026-10-22 02:30 UTC", "America/New_York"),
                         ["station_20261019", "station_20261020", "station_20261021",
                          "station_20261022"])

    def test_same_days_whatever_the_time_zone(self) -> None:
        now = "2026-10-21 23:59 UTC"
        expected = self.kept(2, now, "UTC")
        for tz in ("America/New_York", "Asia/Tokyo", "Pacific/Kiritimati"):
            with self.subTest(tz=tz):
                self.assertEqual(self.kept(2, now, tz), expected)
        # (22 Oct is in the future here: never compressed)
        self.assertEqual(expected, ["station_20261019", "station_20261020", "station_20261021",
                                    "station_20261022"])


class TestPruneLocalRinexUtcDays(ShellDatesTestCase):

    def setUp(self) -> None:
        super().setUp()
        (self.proj / "maintenance" / "prune_local_rinex.sh").write_text(
            (ROOT / "maintenance" / "prune_local_rinex.sh").read_text())
        aws = self.bin / "aws"
        aws.write_text("#!/bin/sh\nexit 1\n")     # nothing is in S3: nothing pruned
        aws.chmod(0o755)
        (self.proj / "archive.conf").write_text(
            f'S3_BASE="s3://bucket/x"\nSTATION_CODE="abcd"\nAWS_CLI="{aws}"\n')
        sync = self.tmp / "s3SyncRinex.sh"
        sync.write_text("aws s3 sync rinex/ s3://bucket/x/rinex\n")
        self.env = {"SYNC_SCRIPT": str(sync)}
        for day in range(15, 23):
            (self.proj / "rinex" / f"station_202610{day:02d}.tar.gz").write_text("x\n")

    def kept(self, keep_days, now, tz):
        r = self.run_script("prune_local_rinex.sh", "--keep-days", str(keep_days),
                            now=now, tz=tz, extra_env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return sorted(line.split()[1].replace(".tar.gz", "") for line in r.stdout.splitlines()
                      if line.strip().startswith("[keep]"))

    def test_same_rule_as_compress_rinex(self) -> None:
        now = "2026-10-21 23:59 UTC"
        for tz in ("UTC", "America/New_York", "Asia/Tokyo"):
            with self.subTest(tz=tz):
                self.assertEqual(self.kept(2, now, tz),
                                 ["station_20261019", "station_20261020", "station_20261021",
                                  "station_20261022"])


class TestCleanProcessingQueueDayMapping(unittest.TestCase):

    def setUp(self) -> None:
        import clean_processing_queue as cpq
        self.cpq = cpq
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / "station" / "resources").mkdir(parents=True)
        (self.proj / "station" / "resources" / "station.json").write_text(
            json.dumps({"gnssrefl_station_code": "abcd"}))
        (self.proj / "raw").mkdir()
        (self.proj / "database").mkdir()
        self.results = self.proj / "products" / "refl_code" / "2026" / "results" / "abcd"
        self.results.mkdir(parents=True)
        db = sqlite3.connect(str(self.proj / "database" / "station.db"))
        db.execute("CREATE TABLE processing_queue (id INTEGER PRIMARY KEY, filename TEXT, "
                   "retry_count INTEGER, completed INTEGER)")
        for i, name in enumerate(("station_20260108.um980",     # results 008.txt
                                  "station_20260109.um980",     # none; next day has some
                                  "station_20260110.um980")):   # results 010.txt
            db.execute("INSERT INTO processing_queue VALUES (?, ?, 3, 0)", (i + 1, name))
        db.commit()
        db.close()
        (self.results / "008.txt").write_text("x\n")
        (self.results / "010.txt").write_text("x\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_own_utc_day_only_and_zero_padded(self) -> None:
        out = io.StringIO()
        with unittest.mock.patch.object(self.cpq, "PROJECT_DIR", self.proj), \
                unittest.mock.patch.object(sys, "argv", ["clean_processing_queue.py", "--execute"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.cpq.main(), 0)
        db = sqlite3.connect(str(self.proj / "database" / "station.db"))
        left = [r[0] for r in db.execute("SELECT filename FROM processing_queue")]
        db.close()
        # day 9 has no results of its own: kept and reported, although
        # day 10 has results (the old doy+1 rule removed it)
        self.assertEqual(left, ["station_20260109.um980"])
        self.assertIn("no results for 2026-01-09, doy 9", out.getvalue())


if __name__ == "__main__":
    unittest.main()
