"""
test_gnss_record.py

The year boundary: 31 December 2026 to mid-January 2027, night by night.

analysis_tools/gnss_record.py keeps one whole-record water-level spline
(<sta>_spline_out.txt) across years; the bug it replaces fitted only
the calendar year, so on 2 January the record became one day long.

Two parts:
  * GnssRecordTests -- no gnssrefl needed. Results, spline files and a
    stand-in `subdaily` (a small script on PATH that writes a spline for
    the days it is asked for, in gnssrefl's format) are faked, so the
    planning, freezing, splicing and every guard run for real.
  * RealSubdailyTests -- the same nights with gnssrefl's real subdaily,
    on synthetic results. Skipped when gnssrefl is not installed.

Run with:
    python3 -m unittest discover -s tests -p "test_gnss_record.py" -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
import gnss_record as gr  # noqa: E402

STA = "usgs"
SETTINGS = "-rhdot True -knots 8 -azim1 35 -azim2 125"
FALLBACK = "-rhdot True -knots 8"
MJD0 = date(1858, 11, 17)
HEADER = textwrap.dedent("""\
    %  station usgs gnssrefl v4.2.3
    %  TIME TAGS ARE IN UTC/999 values mean there was a large gap and no spline value is available.
    %  This is NOT observational data - be careful when interpreting it.
    %  If the data are not well represented by the spline functions, you will
    %  have a very poor representation of the data. I am also writing out station
    %  orthometric height minus RH, where Hortho (m) is {h:10.3f}
    %  MJD              RH(m)  YYYY  MM  DD  HH  MM  SS   quasi-sea-level(m)
    %  (1)               (2)   (3)  (4) (5)  (6) (7) (8)    (9)
    """)


def spline_text(first: date, last: date, hortho: float, rh_offset: float = 0.0) -> str:
    """A gnssrefl spline, 30-minute rows from first 00:30 to last 23:30."""
    out = [HEADER.format(h=hortho)]
    t = datetime(first.year, first.month, first.day, 0, 30)
    end = datetime(last.year, last.month, last.day, 23, 30)
    while t <= end:
        m = (t.date() - MJD0).days + (t.hour * 60 + t.minute) / 1440
        rh = 5.0 + 0.001 * (m % 100) + rh_offset
        out.append(f"{m:15.7f} {rh:11.3f} {t.year:4d} {t.month:3d} {t.day:3d} {t.hour:3d}"
                   f" {t.minute:3d} {0:3d} {hortho - rh:10.3f} \n")
        t += timedelta(minutes=30)
    return "".join(out)


# A stand-in for gnssrefl's subdaily: reads the same arguments and writes
# <sta>_spline_out.txt and the per-year edit files into $REFL_CODE/Files/<subdir>,
# covering exactly the results days it was asked for. FAKE_SUBDAILY=silent
# exits 0 without writing (what subdaily does when it finds no results);
# FAKE_SUBDAILY=crash_azim exits 1 when an azimuth window is given.
FAKE_SUBDAILY = r'''#!/usr/bin/env python3
import os, sys
from datetime import date, timedelta
sys.path.insert(0, os.environ["TEST_DIR"])
from test_gnss_record import spline_text
a = sys.argv[1:]
sta, y1 = a[0], int(a[1])
opt = {a[i]: a[i + 1] for i in range(2, len(a) - 1) if a[i].startswith("-")}
mode = os.environ.get("FAKE_SUBDAILY", "")
with open(os.environ["FAKE_SUBDAILY_LOG"], "a") as f:
    f.write(" ".join(a) + "\n")
if mode == "silent" or (mode == "crash_azim" and "-azim1" in opt):
    sys.exit(1 if mode == "crash_azim" else 0)
y2 = int(opt.get("-year_end", y1))
first = date(y1, 1, 1) + timedelta(days=int(opt["-doy1"]) - 1)
last = date(y2, 1, 1) + timedelta(days=int(opt["-doy2"]) - 1)
refl = os.environ["REFL_CODE"]
days = []
d = first
while d <= last:
    if os.path.exists(f"{refl}/{d.year}/results/{sta}/{d.timetuple().tm_yday:03d}.txt"):
        days.append(d)
    d += timedelta(days=1)
out = os.path.join(refl, "Files", opt.get("-subdir", sta))
os.makedirs(out, exist_ok=True)
h = float(os.environ.get("FAKE_HORTHO", "19.014"))
have = {(d.year, d.month, d.day) for d in days}
text = spline_text(days[0], days[-1], h, float(os.environ.get("FAKE_RH_OFFSET", "0")))
with open(f"{out}/{sta}_spline_out.txt", "w") as f:
    f.writelines(l for l in text.splitlines(True)
                 if l.startswith("%") or tuple(int(x) for x in l.split()[2:5]) in have)
for y in sorted({d.year for d in days}):
    with open(f"{out}/{sta}_{y}_subdaily_edit.txt", "w") as f:
        f.write("% edit\n" + "".join(f"{d.year} {d.timetuple().tm_yday} 5.0\n" for d in days if d.year == y))
'''


def write_result(refl: Path, d: date, n: int = 40, az: float = 80.0, rh: float = 5.0) -> Path:
    """A gnssir results file: n arcs, all at azimuth az (column 6)."""
    p = refl / str(d.year) / "results" / STA / f"{d.timetuple().tm_yday:03d}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    m0 = (d - MJD0).days
    p.write_text("% year doy RH sat UTCtime Azim Amp eminO emaxO NumbOf freq rise EdotF PkNoise"
                 " DelT MJD refr-appl\n"
                 + "".join(f"{d.year} {d.timetuple().tm_yday} {rh + i / 1000:.3f} {i % 32 + 1} "
                           f"{24 * i / n:.3f} {az:.2f} 12.0 5.05 14.95 200 1 1 0.7 4.0 45.0 "
                           f"{m0 + i / n:.6f} 1\n" for i in range(n)))
    return p


def results_range(refl: Path, first: date, last: date) -> None:
    d = first
    while d <= last:
        write_result(refl, d)
        d += timedelta(days=1)


def rows(path: Path):
    """[(MJD, y, m, d, H, M, level, RH)] of a spline file."""
    out = []
    for line in path.read_text().splitlines():
        if line.startswith("%") or not line.strip():
            continue
        c = line.split()
        out.append((float(c[0]), int(c[2]), int(c[3]), int(c[4]), int(c[5]), int(c[6]),
                    float(c[8]), float(c[1])))
    return out


class GnssRecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.refl = self.tmp / "refl_code"
        self.files = self.refl / "Files" / STA
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "subdaily").write_text(FAKE_SUBDAILY)
        (bindir / "subdaily").chmod(0o755)
        self.calls = self.tmp / "subdaily_calls.txt"
        self.env = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "REFL_CODE": str(self.refl),
            "TEST_DIR": str(Path(__file__).resolve().parent),
            "FAKE_SUBDAILY_LOG": str(self.calls), "FAKE_SUBDAILY": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)

    def night(self, settings=SETTINGS, fallback=FALLBACK, quiet=True):
        if quiet:
            with unittest.mock.patch("builtins.print"):
                return gr.cmd_update(self.refl, STA, settings, fallback, False)
        return gr.cmd_update(self.refl, STA, settings, fallback, False)

    def last_call(self):
        return self.calls.read_text().splitlines()[-1]

    def record(self):
        return rows(self.files / f"{STA}_spline_out.txt")

    def assert_whole_record(self, first: date, last: date):
        r = self.record()
        self.assertEqual((r[0][1], r[0][2], r[0][3]), (first.year, first.month, first.day))
        self.assertEqual((r[-1][1], r[-1][2], r[-1][3]), (last.year, last.month, last.day))
        steps = {round((b[0] - a[0]) * 1440) for a, b in zip(r, r[1:])}
        self.assertEqual(steps, {30}, "one row every 30 minutes, none twice, none missing")

    # ---- results and ranges -------------------------------------------

    def test_results_days_every_year_skipping_empty_files(self):
        write_result(self.refl, date(2026, 12, 30))
        write_result(self.refl, date(2026, 12, 31))
        (self.refl / "2027" / "results" / STA).mkdir(parents=True)   # gnssir makes it early
        empty = self.refl / "2026" / "results" / STA / "300.txt"
        empty.write_text("% header only\n")
        self.assertEqual(list(gr.results_days(self.refl, STA)),
                         [date(2026, 12, 30), date(2026, 12, 31)])

    def test_subdaily_range_spans_the_year_and_refuses_an_empty_year(self):
        self.assertEqual(gr.subdaily_range([date(2026, 12, 30), date(2027, 1, 1)]),
                         (2026, 364, 2027, 1))
        self.assertEqual(gr.subdaily_range([date(2027, 1, 1), date(2027, 1, 2)]),
                         (2027, 1, 2027, 2))
        with self.assertRaises(ValueError):
            gr.subdaily_range([date(2025, 12, 31), date(2027, 1, 1)])

    # ---- the nights around 1 January ------------------------------------

    def test_new_year_nights(self):
        results_range(self.refl, date(2026, 7, 8), date(2026, 12, 30))

        # 31 Dec 22:30 local (results to 30 Dec): an ordinary 2026 fit
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 189 -year_end 2026 -doy2 364 -subdir usgs/fit", self.last_call())
        self.assert_whole_record(date(2026, 7, 8), date(2026, 12, 30))

        # 1 Jan: 31 Dec processed, this year's folder exists but is empty.
        # The calendar-year fit stopped here with "No results found yet".
        write_result(self.refl, date(2026, 12, 31))
        (self.refl / "2027" / "results" / STA).mkdir(parents=True)
        self.assertEqual(self.night(), 0)
        self.assert_whole_record(date(2026, 7, 8), date(2026, 12, 31))

        # 2 Jan: the first 2027 day. The calendar-year fit made the record
        # one day long here; now it spans the boundary.
        write_result(self.refl, date(2027, 1, 1))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 189 -year_end 2027 -doy2 1 ", self.last_call())
        self.assert_whole_record(date(2026, 7, 8), date(2027, 1, 1))
        for y in (2026, 2027):
            self.assertTrue((self.files / f"{STA}_{y}_subdaily_edit.txt").exists())

        # 13 days into 2027: not frozen yet
        results_range(self.refl, date(2027, 1, 2), date(2027, 1, 13))
        self.assertEqual(self.night(), 0)
        self.assertFalse(gr.frozen_path(self.files, STA, 2026).exists())

        # 14 days in: 2026 frozen from tonight's fit
        write_result(self.refl, date(2027, 1, 14))
        self.assertEqual(self.night(), 0)
        frozen = gr.frozen_path(self.files, STA, 2026)
        self.assertTrue(frozen.exists())
        fr = rows(frozen)
        self.assertEqual({r[1] for r in fr}, {2026})
        before = self.record()

        # next night: only 2027 is fitted, from 7 days before 1 January
        write_result(self.refl, date(2027, 1, 15))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 359 -year_end 2027 -doy2 15 ", self.last_call())
        self.assert_whole_record(date(2026, 7, 8), date(2027, 1, 15))
        after = self.record()
        self.assertEqual([r for r in after if r[1] == 2026], [r for r in before if r[1] == 2026])
        edit_2026 = (self.files / f"{STA}_2026_subdaily_edit.txt").read_text()
        self.assertIn("2026 189 ", edit_2026, "the whole-year edit file is not replaced by the pad")
        header = [l for l in (self.files / f"{STA}_spline_out.txt").read_text().splitlines()
                  if l.startswith("%")]
        self.assertEqual(sum("Hortho (m) is" in l for l in header), 1)
        self.assertTrue(any(l.startswith("%  record: 2026 frozen") for l in header))

        # a frozen year whose result changes later (a recovered day) is refitted
        old = frozen.stat().st_mtime - 100
        os.utime(frozen, (old, old))
        write_result(self.refl, date(2026, 12, 16), n=5)
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 189 -year_end 2027", self.last_call())
        self.assertEqual(self.night(), 0)                       # and frozen again
        self.assertIn("usgs 2026 -doy1 359 ", self.last_call())

        # so is one made with other subdaily settings
        self.assertEqual(self.night(settings=SETTINGS.replace("35", "30")), 0)
        self.assertIn("usgs 2026 -doy1 189 ", self.last_call())

    def test_frozen_year_with_another_antenna_height_is_put_on_the_fits(self):
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 20))
        p = gr.frozen_path(self.files, STA, 2026)
        self.files.mkdir(parents=True)
        lines = spline_text(date(2026, 12, 1), date(2026, 12, 31), 18.665).splitlines(True)
        head = [l for l in lines if l.startswith("%")]
        body = [l for l in lines if not l.startswith("%")]
        fp = gr.fingerprint(gr.results_days(self.refl, STA)[d] for d in gr.results_days(self.refl, STA)
                            if d.year == 2026)
        p.write_text("".join(gr.frozen_text(head, SETTINGS, fp, body, "2026 frozen (test)")))
        self.assertEqual(self.night(), 0)          # fit written with 19.014 (NAVD88)
        dec = [r for r in self.record() if r[1] == 2026]
        self.assertEqual(len(dec), 31 * 48 - 1)
        for r in dec:
            self.assertAlmostEqual(r[6], round(19.014 - r[7], 3), places=6)

    # ---- guards: the record is never replaced by a worse one ------------

    def test_subdaily_writing_nothing_leaves_the_record(self):
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 1))
        self.assertEqual(self.night(), 0)
        good = (self.files / f"{STA}_spline_out.txt").read_text()
        write_result(self.refl, date(2027, 1, 2))
        os.environ["FAKE_SUBDAILY"] = "silent"
        self.assertEqual(self.night(), 1)
        self.assertEqual((self.files / f"{STA}_spline_out.txt").read_text(), good)

    def test_fallback_fit_is_used_but_never_frozen(self):
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 20))
        os.environ["FAKE_SUBDAILY"] = "crash_azim"
        self.assertEqual(self.night(), 0)
        self.assertTrue(self.last_call().endswith(FALLBACK))
        self.assert_whole_record(date(2026, 12, 1), date(2027, 1, 20))
        self.assertFalse(gr.frozen_path(self.files, STA, 2026).exists())

    def test_record_that_would_start_later_is_refused(self):
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 3))
        self.assertEqual(self.night(), 0)
        good = (self.files / f"{STA}_spline_out.txt").read_text()
        shutil.rmtree(self.refl / "2026")               # e.g. a wrong REFL_CODE
        self.assertEqual(self.night(), 1)
        self.assertEqual((self.files / f"{STA}_spline_out.txt").read_text(), good)

    def test_record_written_atomically_with_ordinary_permissions(self):
        results_range(self.refl, date(2026, 12, 20), date(2027, 1, 2))
        old = os.umask(0o022)
        try:
            self.assertEqual(self.night(), 0)
        finally:
            os.umask(old)
        rec = self.files / f"{STA}_spline_out.txt"
        self.assertEqual(rec.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.files.glob(".*.tmp")), [])

    # ---- the other readers on 1-2 January --------------------------------

    def test_station_health_on_new_years_morning(self):
        sys.path.insert(0, str(ROOT / "diagnostics"))
        import station_health as sh
        results_range(self.refl, date(2026, 12, 1), date(2026, 12, 30))
        (self.refl / "2027" / "results" / STA).mkdir(parents=True)
        now = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)
        (self.tmp / "products").mkdir()
        (self.tmp / "products" / "refl_code").symlink_to(self.refl)
        with unittest.mock.patch.object(sh, "PROJECT_DIR", self.tmp), \
             unittest.mock.patch.object(sh, "utcnow", return_value=now):
            sh.findings.clear()
            sh.check_processing_currency(STA)
            self.assertEqual([f[0] for f in sh.findings], ["OK"], sh.findings)
            msg = "No results file produced: /x/refl_code/{}/results/usgs/{}.txt"
            self.assertTrue(sh._is_future_day_message(msg.format(2026, 365)))
            self.assertFalse(sh._is_future_day_message(msg.format(2026, 350)))

    def test_s3_upload_on_2_january_includes_december(self):
        proj = self.tmp / "proj"
        (proj / "maintenance").mkdir(parents=True)
        for f in ("s3UploadTimeseries.sh", "filter_month.py"):
            shutil.copy(ROOT / "maintenance" / f, proj / "maintenance" / f)
        shutil.copytree(ROOT / "analysis_tools", proj / "analysis_tools")
        products = proj / "products" / "refl_code" / "Files" / STA
        products.mkdir(parents=True)
        (products / f"{STA}_spline_out.txt").write_text(
            spline_text(date(2026, 11, 20), date(2027, 1, 1), 19.014))
        for y in (2026, 2027):
            (products / f"{STA}_{y}_subdaily_edit.txt").write_text("% edit\n")
        bindir = self.tmp / "s3bin"
        bindir.mkdir()
        (bindir / "aws").write_text(f"#!/bin/sh\necho \"$@\" >> {self.tmp}/aws.log\n")
        (bindir / "date").write_text(           # the clock reads 2 Jan 2027 03:30 UTC
            "#!/bin/bash\nargs=()\nhave_d=0\nwhile [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = -d ]; then args+=(-d \"2027-01-02 03:30 UTC $2\"); have_d=1; shift 2\n"
            "  else args+=(\"$1\"); shift; fi\ndone\n"
            "[ $have_d = 1 ] || args+=(-d \"2027-01-02 03:30 UTC\")\n"
            "exec /bin/date \"${args[@]}\"\n")
        for f in ("aws", "date"):
            (bindir / f).chmod(0o755)
        conf = self.tmp / "archive.conf"
        conf.write_text(f"S3_BASE=s3://bucket/GPS\nSTATION_CODE={STA}\nAWS_CLI={bindir}/aws\n")
        env = dict(os.environ, ARCHIVE_CONF=str(conf), PROJECT_DIR=str(proj),
                   TIMESERIES_COUNT_FILE=str(self.tmp / "count.txt"),
                   PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        subprocess.run(["bash", str(proj / "maintenance" / "s3UploadTimeseries.sh")],
                       env=env, capture_output=True, text=True)
        log = (self.tmp / "aws.log").read_text()
        self.assertIn("timeseries/december2026/usgs_spline_out_december2026.txt", log)
        self.assertIn("timeseries/january2027/usgs_spline_out_january2027.txt", log)
        self.assertNotIn("november2026", log)
        (self.tmp / "aws.log").unlink()
        subprocess.run(["bash", str(proj / "maintenance" / "s3UploadTimeseries.sh"), "--backfill"],
                       env=env, capture_output=True, text=True)
        log = (self.tmp / "aws.log").read_text()
        for label in ("november2026", "december2026", "january2027"):
            self.assertIn(f"timeseries/{label}/usgs_spline_out_{label}.txt", log)

    # ---- review: edge years, flips, holes, fingerprints, guards ----------

    def test_thin_first_january_waits_a_night_instead_of_a_whole_record_fallback(self):
        # subdaily crashes when its azimuth window empties a year of the range;
        # a short 1 January outside the window did that, and the fallback then
        # refitted the WHOLE live year without the window
        results_range(self.refl, date(2026, 7, 8), date(2026, 12, 31))
        write_result(self.refl, date(2027, 1, 1), n=6, az=250.0)
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 189 -year_end 2026 -doy2 365 ", self.last_call())
        self.assertTrue(self.last_call().endswith("-azim2 125"), "primary settings, no fallback")
        self.assert_whole_record(date(2026, 7, 8), date(2026, 12, 31))
        write_result(self.refl, date(2027, 1, 2))
        self.assertEqual(self.night(), 0)
        self.assertIn("-year_end 2027 -doy2 2 ", self.last_call())

    def test_thin_pad_is_dropped(self):
        results_range(self.refl, date(2026, 12, 1), date(2026, 12, 24))
        write_result(self.refl, date(2026, 12, 31), n=6, az=250.0)
        results_range(self.refl, date(2027, 1, 1), date(2027, 1, 16))
        self.assertEqual(self.night(), 0)                     # freezes 2026
        self.assertTrue(gr.frozen_path(self.files, STA, 2026).exists())
        write_result(self.refl, date(2027, 1, 17))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2027 -doy1 1 -year_end 2027 -doy2 17 ", self.last_call())
        self.assertTrue(self.last_call().endswith("-azim2 125"))

    def test_refreezing_an_earlier_year_does_not_flip_a_later_one(self):
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 14))
        self.night()                                          # freezes 2026
        results_range(self.refl, date(2027, 1, 15), date(2028, 1, 15))
        self.night()                                          # freezes 2027
        write_result(self.refl, date(2026, 12, 16), n=5)      # a recovered 2026 day
        os.environ["FAKE_RH_OFFSET"] = "0.050"                # tonight's refit differs
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 335 -year_end 2028", self.last_call())
        a = [r for r in self.record() if r[1] == 2027]
        os.environ["FAKE_RH_OFFSET"] = "0.060"
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2027 -doy1 359 ", self.last_call())
        b = [r for r in self.record() if r[1] == 2027]
        self.assertTrue(b == a, f"2027 rows changed between two nights with no new 2027 data "
                                f"({sum(x != y for x, y in zip(a, b))} of {len(a)} differ)")

    def test_truncated_frozen_file_is_refitted_not_used(self):
        results_range(self.refl, date(2026, 7, 8), date(2027, 1, 14))
        self.night()
        f = gr.frozen_path(self.files, STA, 2026)
        f.write_text("".join(l for l in f.read_text().splitlines(True)
                             if l.startswith("%") or int(l.split()[3]) < 10))   # Oct-Dec gone
        write_result(self.refl, date(2027, 1, 15))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 189 ", self.last_call())
        self.assert_whole_record(date(2026, 7, 8), date(2027, 1, 15))

    def test_restored_day_with_an_old_mtime_still_refits_its_frozen_year(self):
        results_range(self.refl, date(2026, 12, 1), date(2026, 12, 9))
        results_range(self.refl, date(2026, 12, 11), date(2027, 1, 14))
        self.night()
        self.assertTrue(gr.frozen_path(self.files, STA, 2026).exists())
        p = write_result(self.refl, date(2026, 12, 10))        # e.g. cp -p from a backup
        os.utime(p, (1e9, 1e9))
        write_result(self.refl, date(2027, 1, 15))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2026 -doy1 335 ", self.last_call())
        self.assertTrue(any(r[1:4] == (2026, 12, 10) for r in self.record()))

    def test_record_that_would_lose_days_is_refused(self):
        results_range(self.refl, date(2026, 11, 1), date(2027, 1, 5))
        self.assertEqual(self.night(), 0)
        good = (self.files / f"{STA}_spline_out.txt").read_text()
        for k in range(10, 16):                               # six days of results vanish
            (self.refl / "2026" / "results" / STA / f"{date(2026, 12, k).timetuple().tm_yday:03d}.txt").unlink()
        self.assertEqual(self.night(), 1)
        self.assertEqual((self.files / f"{STA}_spline_out.txt").read_text(), good)
        with unittest.mock.patch("builtins.print"):
            self.assertEqual(gr.cmd_update(self.refl, STA, SETTINGS, FALLBACK, True), 0)

    def test_record_is_0644_whatever_the_umask(self):
        results_range(self.refl, date(2026, 12, 20), date(2027, 1, 2))
        old = os.umask(0o077)
        try:
            self.assertEqual(self.night(), 0)
        finally:
            os.umask(old)
        self.assertEqual((self.files / f"{STA}_spline_out.txt").stat().st_mode & 0o777, 0o644)

    def test_second_update_does_not_run_alongside_the_first(self):
        import fcntl
        results_range(self.refl, date(2026, 12, 20), date(2027, 1, 2))
        (self.files / "fit").mkdir(parents=True)
        with open(self.files / "fit" / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.night(), 1)
        self.assertFalse((self.files / f"{STA}_spline_out.txt").exists())
        self.assertEqual(self.night(), 0)

    def test_leap_year_2028(self):
        # 2028 has 366 days: 31 December is doy 366, and 25 December
        # (the start of the pad once 2028 is frozen) is doy 360.
        results_range(self.refl, date(2028, 12, 1), date(2028, 12, 30))

        # 31 Dec 2028 22:30 local (results to 30 Dec, doy 365)
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2028 -doy1 336 -year_end 2028 -doy2 365 ", self.last_call())
        self.assert_whole_record(date(2028, 12, 1), date(2028, 12, 30))

        # 1 Jan 2029: 31 Dec (doy 366) processed, the 2029 folder empty
        write_result(self.refl, date(2028, 12, 31))
        (self.refl / "2029" / "results" / STA).mkdir(parents=True)
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2028 -doy1 336 -year_end 2028 -doy2 366 ", self.last_call())
        self.assert_whole_record(date(2028, 12, 1), date(2028, 12, 31))

        # 2 Jan 2029: the first 2029 day, fitted across the boundary
        write_result(self.refl, date(2029, 1, 1))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2028 -doy1 336 -year_end 2029 -doy2 1 ", self.last_call())
        self.assert_whole_record(date(2028, 12, 1), date(2029, 1, 1))
        self.assertTrue(any(r[1:4] == (2028, 12, 31) for r in self.record()), "doy 366 kept")

        # 14 days in: 2028 frozen; the next night fits 2029 from 25 Dec (doy 360)
        results_range(self.refl, date(2029, 1, 2), date(2029, 1, 14))
        self.assertEqual(self.night(), 0)
        self.assertTrue(gr.frozen_path(self.files, STA, 2028).exists())
        write_result(self.refl, date(2029, 1, 15))
        self.assertEqual(self.night(), 0)
        self.assertIn("usgs 2028 -doy1 360 -year_end 2029 -doy2 15 ", self.last_call())
        self.assert_whole_record(date(2028, 12, 1), date(2029, 1, 15))

    def test_results_before_the_record_start_are_left_out(self):
        write_result(self.refl, date(2023, 5, 1))             # an installation test
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 2))
        self.assertEqual(self.night(), 1)                     # 2024-2025 empty: refused, loudly
        st = json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())
        self.assertFalse(st["ok"])
        with unittest.mock.patch("builtins.print"):
            self.assertEqual(gr.cmd_update(self.refl, STA, SETTINGS, FALLBACK, False,
                                           since=date(2026, 7, 1)), 0)
        self.assert_whole_record(date(2026, 12, 1), date(2027, 1, 2))

    def test_station_health_reports_the_record(self):
        sys.path.insert(0, str(ROOT / "diagnostics"))
        import station_health as sh
        (self.tmp / "products").mkdir()
        (self.tmp / "products" / "refl_code").symlink_to(self.refl)
        results_range(self.refl, date(2026, 12, 1), date(2027, 1, 2))
        self.assertEqual(self.night(), 0)

        def check():
            sh.findings.clear()
            with unittest.mock.patch.object(sh, "PROJECT_DIR", self.tmp):
                sh.check_water_level_record(STA)
            return [(f[0]) for f in sh.findings if f[1] == "record"]
        self.assertEqual(check(), ["OK"])
        results_range(self.refl, date(2027, 1, 3), date(2027, 1, 7))   # the fit stops updating
        self.assertIn("FAIL", check())
        self.assertEqual(self.night(), 0)
        os.environ["FAKE_SUBDAILY"] = "crash_azim"
        write_result(self.refl, date(2027, 1, 8))
        self.assertEqual(self.night(), 0)
        self.assertEqual(check(), ["WARN"])                   # fallback fit in use
        self.assertIn("FALLBACK", (self.files / f"{STA}_spline_out.txt").read_text())

    def test_frozen_year_is_uploaded_once_more_in_full(self):
        results_range(self.refl, date(2026, 11, 20), date(2027, 1, 14))
        self.assertEqual(self.night(), 0)                     # freezes 2026
        mark = self.files / "fit" / "s3_upload_year_2026"
        self.assertTrue(mark.exists())
        proj = self.tmp / "proj"
        (proj / "maintenance").mkdir(parents=True)
        for f in ("s3UploadTimeseries.sh", "filter_month.py"):
            shutil.copy(ROOT / "maintenance" / f, proj / "maintenance" / f)
        shutil.copytree(ROOT / "analysis_tools", proj / "analysis_tools")
        (proj / "products").mkdir()
        (proj / "products" / "refl_code").symlink_to(self.refl)
        bindir = self.tmp / "s3bin"
        bindir.mkdir()
        (bindir / "aws").write_text(f"#!/bin/sh\necho \"$@\" >> {self.tmp}/aws.log\n")
        (bindir / "date").write_text(
            "#!/bin/bash\nargs=()\nhave_d=0\nwhile [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = -d ]; then args+=(-d \"2027-01-17 03:30 UTC $2\"); have_d=1; shift 2\n"
            "  else args+=(\"$1\"); shift; fi\ndone\n"
            "[ $have_d = 1 ] || args+=(-d \"2027-01-17 03:30 UTC\")\n"
            "exec /bin/date \"${args[@]}\"\n")
        for f in ("aws", "date"):
            (bindir / f).chmod(0o755)
        conf = self.tmp / "archive.conf"
        conf.write_text(f"S3_BASE=s3://bucket/GPS\nSTATION_CODE={STA}\nAWS_CLI={bindir}/aws\n")
        env = dict(os.environ, ARCHIVE_CONF=str(conf), PROJECT_DIR=str(proj),
                   TIMESERIES_COUNT_FILE=str(self.tmp / "count.txt"),
                   PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        r = subprocess.run(["bash", str(proj / "maintenance" / "s3UploadTimeseries.sh")],
                           env=env, capture_output=True, text=True)
        log = (self.tmp / "aws.log").read_text()
        for label in ("november2026", "december2026", "january2027"):
            self.assertIn(f"timeseries/{label}/usgs_spline_out_{label}.txt", log, r.stdout)
        self.assertFalse(mark.exists(), "done once")
        self.assertTrue((self.tmp / "count.txt").exists())



def _gnssrefl_installed() -> bool:
    return shutil.which("subdaily") is not None and importlib.util.find_spec("gnssrefl") is not None


@unittest.skipUnless(_gnssrefl_installed(), "gnssrefl (subdaily) not installed")
class RealSubdailyTests(unittest.TestCase):
    """The new-year nights with gnssrefl's own subdaily on synthetic results:
    a tide (M2+S2+K1) seen as reflector heights, 150 arcs a day, with the
    rate-of-change term gnssir's heights carry."""

    HORTHO = 11.0

    @staticmethod
    def truth(mjd):
        import numpy as np
        t = np.asarray(mjd, dtype=float)
        return (1.30 * np.cos(2 * np.pi * (t - 61400.10) / (12.4206 / 24))
                + 0.20 * np.cos(2 * np.pi * (t - 61400.30) / (12.0 / 24))
                + 0.10 * np.cos(2 * np.pi * (t - 61400.00) / (23.9345 / 24)))

    def setUp(self):
        import numpy as np
        self.np = np
        self.tmp = Path(tempfile.mkdtemp())
        self.refl = self.tmp / "refl_code"
        self.files = self.refl / "Files" / STA
        self.env = unittest.mock.patch.dict(os.environ, {"REFL_CODE": str(self.refl),
                                                         "MPLBACKEND": "Agg"})
        self.env.start()
        from gnssrefl.gnssir_input import make_gnssir_input
        with unittest.mock.patch("builtins.print"):
            make_gnssir_input(STA, lat=41.8915, lon=-69.9618, height=-17.0,
                              Hortho=[self.HORTHO], allfreq=True, e1=5, e2=15, h1=2, h2=14)
        self.rng = np.random.default_rng(1)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)

    def results(self, first: date, last: date, arcs: int = 150, az=(40, 120), hours=(0, 24)):
        np = self.np
        d = first
        while d <= last:
            utc = np.sort(self.rng.uniform(*hours, arcs))
            mjd = (d - MJD0).days + utc / 24
            doy = d.timetuple().tm_yday
            # gnssir's RH carries EdotF * dRH/dt, which subdaily's -rhdot step removes
            rise = self.rng.choice([-1, 1], arcs)
            edotf = rise * self.rng.uniform(0.5, 0.9, arcs)
            rhdot = -(self.truth(mjd + 1e-4) - self.truth(mjd - 1e-4)) / 2e-4 / 24   # m/h
            rh = self.HORTHO - self.truth(mjd) + edotf * rhdot + self.rng.normal(0, 0.03, arcs)
            rows_ = np.column_stack([
                np.full(arcs, d.year), np.full(arcs, doy), rh, self.rng.integers(1, 33, arcs), utc,
                self.rng.uniform(*az, arcs), self.rng.uniform(8, 20, arcs),
                np.full(arcs, 5.05), np.full(arcs, 14.95), self.rng.integers(150, 260, arcs),
                self.rng.choice([1, 20, 5, 101, 102, 201, 205, 207, 208], arcs),
                rise, edotf, self.rng.uniform(3, 6, arcs),
                self.rng.uniform(30, 60, arcs), mjd, np.ones(arcs)])
            p = self.refl / str(d.year) / "results" / STA / f"{doy:03d}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            np.savetxt(p, rows_, fmt="%4.0f %3.0f %6.3f %3.0f %6.3f %6.2f %6.2f %6.2f %6.2f %4.0f"
                       "  %3.0f  %2.0f %8.5f %6.2f %7.2f %12.6f %2.0f", header=" gnssir results",
                       comments="%")
            d += timedelta(days=1)

    def night(self):
        with unittest.mock.patch("builtins.print"):
            return gr.cmd_update(self.refl, STA, SETTINGS, FALLBACK, False)

    def check(self, first: date, last: date, new_year: date | None = date(2027, 1, 1)):
        np = self.np
        a = np.loadtxt(self.files / f"{STA}_spline_out.txt", comments="%")
        days = [date(int(r[2]), int(r[3]), int(r[4])) for r in (a[0], a[-1])]
        self.assertEqual(days, [first, last])
        steps = np.round(np.diff(a[:, 0]) * 1440)
        self.assertTrue(np.all(steps >= 30) and np.all(steps % 30 == 0), "30-min grid, no repeats")
        err = a[:, 8] - self.truth(a[:, 0])
        self.assertLess(np.sqrt(np.mean(err ** 2)), 0.08)
        if new_year is None:                                  # the boundary is the record's end
            return a
        near = np.abs(a[:, 0] - (new_year - MJD0).days) <= 0.5   # +-12 h of 1 January 00Z
        if near.any():
            self.assertLess(np.sqrt(np.mean(err[near] ** 2)), 0.08)
        return a

    def test_new_year_nights_with_real_subdaily(self):
        self.results(date(2026, 12, 1), date(2026, 12, 30))
        self.assertEqual(self.night(), 0)                         # 31 Dec
        self.check(date(2026, 12, 1), date(2026, 12, 30))
        self.results(date(2026, 12, 31), date(2026, 12, 31))
        (self.refl / "2027" / "results" / STA).mkdir(parents=True)
        self.assertEqual(self.night(), 0)                         # 1 Jan
        self.check(date(2026, 12, 1), date(2026, 12, 31))
        self.results(date(2027, 1, 1), date(2027, 1, 1))
        self.assertEqual(self.night(), 0)                         # 2 Jan
        self.check(date(2026, 12, 1), date(2027, 1, 1))
        self.results(date(2027, 1, 2), date(2027, 1, 14))
        self.assertEqual(self.night(), 0)                         # 16 Jan: freeze
        frozen_night = self.check(date(2026, 12, 1), date(2027, 1, 14))
        self.assertTrue(gr.frozen_path(self.files, STA, 2026).exists())
        self.results(date(2027, 1, 15), date(2027, 1, 15))
        self.assertEqual(self.night(), 0)                         # 17 Jan: 2027 only
        a = self.check(date(2026, 12, 1), date(2027, 1, 15))
        n2026 = int((a[:, 0] < 61406.0).sum())
        self.np.testing.assert_array_equal(a[:n2026], frozen_night[:n2026])
        i = n2026                                                 # the splice at 1 Jan 00:00
        jump = (a[i, 8] - a[i - 1, 8]) - (self.truth(a[i, 0]) - self.truth(a[i - 1, 0]))
        self.assertLess(abs(jump), 0.05)

    def test_thin_first_january_with_real_subdaily(self):
        """A short 1 January whose arcs all lie outside 35-125 deg made the
        -year_end fit crash (ValueError in apply_new_constraints), and the
        fallback refitted the whole record without the window."""
        self.results(date(2026, 12, 1), date(2026, 12, 31))
        self.results(date(2027, 1, 1), date(2027, 1, 1), arcs=6, az=(200, 300), hours=(0, 2))
        self.assertEqual(self.night(), 0)
        st = json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())
        self.assertFalse(st["fallback"], st)
        self.check(date(2026, 12, 1), date(2026, 12, 31))
        self.results(date(2027, 1, 2), date(2027, 1, 2))
        self.assertEqual(self.night(), 0)
        self.check(date(2026, 12, 1), date(2027, 1, 2))
        self.assertFalse(json.loads((self.files / "fit" / gr.STATUS_NAME).read_text())["fallback"])

    def test_leap_year_boundary_with_real_subdaily(self):
        """31 December 2028 is doy 366: the nights of 1 and 2 January 2029."""
        self.results(date(2028, 12, 10), date(2028, 12, 31))
        (self.refl / "2029" / "results" / STA).mkdir(parents=True)
        np = self.np
        mjd_2029 = (date(2029, 1, 1) - MJD0).days
        self.assertEqual(self.night(), 0)                         # 1 Jan 2029
        # 31 Dec is the record's newest end tonight: its last hours are
        # provisional (mirrored at the edge; up to ~0.2 m off, refitted the
        # next night), so only the first half of doy 366 is held to the
        # interior accuracy here
        a = self.check(date(2028, 12, 10), date(2028, 12, 31), None)
        doy366 = (a[:, 0] >= mjd_2029 - 1) & (a[:, 0] < mjd_2029 - 0.5)
        err = a[doy366, 8] - self.truth(a[doy366, 0])
        self.assertGreater(doy366.sum(), 20)
        self.assertLess(np.sqrt(np.mean(err ** 2)), 0.08)
        self.results(date(2029, 1, 1), date(2029, 1, 1))
        self.assertEqual(self.night(), 0)                         # 2 Jan 2029
        a = self.check(date(2028, 12, 10), date(2029, 1, 1), date(2029, 1, 1))
        self.assertTrue(np.any(np.isclose(a[:, 0], mjd_2029 - 1 / 48, rtol=0, atol=1e-4)),
                        "23:30 on doy 366")
        self.assertTrue(np.any(np.isclose(a[:, 0], mjd_2029, rtol=0, atol=1e-4)), "00:00 on 1 January")

    def test_ultra_rapid_window_on_1_january(self):
        self.results(date(2026, 12, 30), date(2027, 1, 1))
        days = [d for d in gr.results_days(self.refl, STA)
                if date(2026, 12, 30) <= d <= date(2027, 1, 1)]
        y1, d1, y2, d2 = gr.subdaily_range(days)
        self.assertEqual((y1, d1, y2, d2), (2026, 364, 2027, 1))
        subprocess.run(["subdaily", STA, str(y1), "-doy1", str(d1), "-year_end", str(y2),
                        "-doy2", str(d2)] + SETTINGS.split(), capture_output=True)
        a = self.np.loadtxt(self.files / f"{STA}_spline_out.txt", comments="%")
        self.assertTrue((a[:, 2] == 2027).any(), "today's (1 Jan) levels are in the spline")


if __name__ == "__main__":
    unittest.main()
