"""
tests/test_tide_history.py

The tide comparison plot (plot_gnssir_vs_tide.py, Step 6 of
process_and_plot.sh) joins the earlier tide files that station.json
tide_model_history names with tide_model_file, so its tide does not
start where the current spreadsheet starts.

  * ExpandTests -- the patterns: globs sorted, each file once, never
    the current file (not even through a link), and the patterns that
    match nothing reported.
  * JoinTests -- load_tide_series on real .xlsx files: the current
    file wins over its whole period, among the earlier files the one
    that starts later, whatever order they are given in; a file
    without the height column, an unreadable one or an empty one is
    left out and named; a disagreement where two files share
    timestamps is printed.
  * MainTests -- main() on three months of synthetic tide and a
    water level 0.5 m above it: the orange line runs from the first
    earlier file to the end of the current one, breaks across a two-day
    gap, takes the current file's values where it overlaps an earlier
    one, and the offset (0.500 m) is not pulled by water levels inside
    the gap. Without --tide-history the plot is the current file alone.
  * PipelineTests -- process_and_plot.sh passes each tide_model_history
    entry (a list, or one string) to the plot as --tide-history,
    unexpanded and in order, and nothing when the key is absent.

Run with:
    python3 -m unittest discover -s tests -p "test_tide_history.py" -v
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
sys.path.insert(0, str(ROOT / "tests"))

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import openpyxl
    import station_datum as sd  # noqa: E402
    import plot_gnssir_vs_tide as pgt  # noqa: E402
except ImportError:  # pragma: no cover
    openpyxl = None

STEP = timedelta(minutes=10)
MJD0 = date(1858, 11, 17)
COL = "EOT20_heightm"


def tide(t: datetime) -> float:
    hours = (t - datetime(2026, 9, 1)).total_seconds() / 3600
    return 0.6 * math.sin(2 * math.pi * hours / 12.42) + 0.1 * math.sin(2 * math.pi * hours / 25.8)


def span(first: datetime, last: datetime, step: timedelta = STEP):
    t, out = first, []
    while t <= last:
        out.append(t)
        t += step
    return out


def write_tides(path: Path, times, values, header=("time", COL)) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for t, v in zip(times, values):
        ws.append([t, v])
    wb.save(path)
    return path


def write_spline(path: Path, times, levels) -> Path:
    """A gnssrefl spline file on NAVD88 (its Hortho is the NAVD88 one,
    so station_datum moves nothing)."""
    h = sd.NAVD88_HORTHO_M
    lines = ["% station usgs gnssrefl v4.1.5\n",
             f"% orthometric height minus RH, where Hortho (m) is {h:10.3f}\n",
             "%  MJD              RH(m)  YYYY  MM  DD  HH  MM  SS   quasi-sea-level(m)\n"]
    for t, v in zip(times, levels):
        m = (t.date() - MJD0).days + (t.hour * 60 + t.minute) / 1440
        lines.append(f"{m:15.7f} {h - v:11.3f} {t.year:4d} {t.month:3d} {t.day:3d}"
                     f" {t.hour:3d} {t.minute:3d} {0:3d} {v:10.6f} \n")
    path.write_text("".join(lines))
    return path


@unittest.skipIf(openpyxl is None, "numpy, matplotlib or openpyxl not installed")
class Files(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


class ExpandTests(Files):

    def test_globs_sorted_once_never_the_current_file(self) -> None:
        d = self.dir / "tides"
        d.mkdir()
        current = d / "marconi_tides_sherwood.xlsx"
        for f in ("marconi_tides_sherwood.xlsx", "marconi_tides_sherwood_20260901-20261031.xlsx",
                  "marconi_tides_sherwood_20260715-20260831.xlsx"):
            (d / f).write_text("x")
        link = self.dir / "link.xlsx"
        link.symlink_to(current)
        # tide_model_file given through a link: the glob's match of the
        # file itself, and the link, are both the current file
        files, unmatched = pgt.expand_tide_history(
            [str(d / "marconi_tides_sherwood*.xlsx"),
             str(d / "marconi_tides_sherwood_20260715-20260831.xlsx"),   # already matched
             str(link),
             str(d / "nothing_*.xlsx"), str(d / "missing.xlsx")], link)
        self.assertEqual([f.name for f in files],
                         ["marconi_tides_sherwood_20260715-20260831.xlsx",
                          "marconi_tides_sherwood_20260901-20261031.xlsx"])
        self.assertEqual(unmatched, [str(d / "nothing_*.xlsx"), str(d / "missing.xlsx")])

    def test_home_is_expanded(self) -> None:
        (self.dir / "old.xlsx").write_text("x")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            files, unmatched = pgt.expand_tide_history(["~/old.xlsx", "~/*.xlsx"],
                                                       self.dir / "current.xlsx")
        self.assertEqual(files, [self.dir / "old.xlsx"])
        self.assertEqual(unmatched, [])


class JoinTests(Files):

    def setUp(self) -> None:
        super().setUp()
        self.sep = span(datetime(2026, 9, 1), datetime(2026, 9, 30, 23, 50))
        self.octo = span(datetime(2026, 10, 1), datetime(2026, 11, 2, 23, 50))
        self.nov = span(datetime(2026, 11, 1), datetime(2026, 11, 10, 23, 50))
        self.current = write_tides(self.dir / "current.xlsx", self.nov, [tide(t) for t in self.nov])
        # the October file is 2 cm high where it overlaps the current one
        self.oct_file = write_tides(self.dir / "oct.xlsx", self.octo,
                                    [tide(t) + (0.02 if t >= self.nov[0] else 0) for t in self.octo])
        self.sep_file = write_tides(self.dir / "sep.xlsx", self.sep, [tide(t) for t in self.sep])

    def join(self, history):
        return pgt.load_tide_series(self.current, history, "time", COL)

    def test_current_wins_its_period_and_order_does_not_matter(self) -> None:
        times, values, notes = self.join([self.oct_file, self.sep_file])
        self.assertEqual(times, self.sep + [t for t in self.octo if t < self.nov[0]] + self.nov)
        np.testing.assert_allclose(values, [tide(t) for t in times], rtol=0, atol=1e-12)
        again, again_v, _ = self.join([self.sep_file, self.oct_file])
        self.assertEqual(again, times)
        np.testing.assert_array_equal(again_v, values)
        text = "\n".join(notes)
        self.assertIn(f"{self.oct_file}: 2026-10-01 00:00 to 2026-11-02 23:50, "
                      f"{31 * 144} of {33 * 144} rows used\n"
                      f"    {2 * 144} timestamps in common with a file that wins there: "
                      "largest difference 0.0200 m  WARNING", text)
        self.assertIn(f"{self.current}: 2026-11-01 00:00 to 2026-11-10 23:50, "
                      f"{10 * 144} of {10 * 144} rows used", text)

    def test_rows_in_a_winning_period_are_dropped_not_interleaved(self) -> None:
        # an earlier file on the 5-minute marks, across the current file's start
        offset = span(datetime(2026, 10, 25, 0, 5), datetime(2026, 11, 5, 0, 5))
        f = write_tides(self.dir / "offset.xlsx", offset, [tide(t) + 0.3 for t in offset])
        times, _, notes = self.join([f])
        self.assertEqual(times, [t for t in offset if t < self.nov[0]] + self.nov)
        self.assertFalse([n for n in notes if "in common" in n])   # no shared timestamps

    def test_the_earlier_file_that_starts_later_wins(self) -> None:
        # a merged Sep-Oct file under the October one: October wins from 1 Oct
        merged_t = self.sep + [t for t in self.octo if t < self.nov[0]]
        merged = write_tides(self.dir / "merged.xlsx", merged_t, [tide(t) + 0.05 for t in merged_t])
        for history in ([merged, self.oct_file], [self.oct_file, merged]):
            with self.subTest(order=[p.name for p in history]):
                times, values, notes = self.join(history)
                self.assertEqual(times, merged_t + self.nov)
                expect = [tide(t) + (0.05 if t < self.octo[0] else 0) for t in times]
                np.testing.assert_allclose(values, expect, rtol=0, atol=1e-12)
                self.assertIn(f"{merged}: 2026-09-01 00:00 to 2026-10-31 23:50, "
                              f"{30 * 144} of {61 * 144} rows used\n    {31 * 144} timestamps "
                              "in common", "\n".join(notes))

    def test_agreeing_overlap_is_not_a_warning(self) -> None:
        same = write_tides(self.dir / "same.xlsx", self.octo, [tide(t) for t in self.octo])
        _, _, notes = self.join([same])
        line, = [n for n in notes if "in common" in n]
        self.assertIn("largest difference 0.0000 m", line)
        self.assertNotIn("WARNING", line)

    def test_bad_earlier_files_are_left_out_and_named(self) -> None:
        other = write_tides(self.dir / "fes.xlsx", self.sep, [0.0] * len(self.sep),
                            header=("time", "FES_heightm"))
        broken = self.dir / "broken.xlsx"
        broken.write_text("not a workbook")
        empty = write_tides(self.dir / "empty.xlsx", [], [])
        times, values, notes = self.join([other, broken, empty, self.sep_file])
        self.assertEqual(times, self.sep + self.nov)
        text = "\n".join(notes)
        self.assertIn(f"left out {other}: no 'time' or '{COL}' column", text)
        self.assertIn(f"left out {broken}: could not read it", text)
        self.assertIn(f"left out {empty}: no rows with a time and a '{COL}' value", text)

    def test_the_current_file_must_load(self) -> None:
        bad = write_tides(self.dir / "bad_current.xlsx", self.nov, [0.0] * len(self.nov),
                          header=("time", "FES_heightm"))
        with self.assertRaises(ValueError):
            pgt.load_tide_series(bad, [self.sep_file], "time", COL)


class MainTests(Files):

    OFFSET = 0.5

    def setUp(self) -> None:
        super().setUp()
        res = self.dir / "station" / "resources"
        res.mkdir(parents=True)
        station_json = res / "station.json"
        station_json.write_text(json.dumps({"vertical_datum": "NAVD88 (GEOID18)",
                                            "gnssrefl_orthometric_height": sd.NAVD88_HORTHO_M,
                                            "tide_model_navd88_offset": 0.0}))
        patch = unittest.mock.patch.object(sd, "STATION_JSON", station_json)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(plt.close, "all")
        # September, then a gap from 1 to 3 October, then October into
        # November (2 cm high where the current November file overlaps it)
        self.sep = span(datetime(2026, 9, 1), datetime(2026, 9, 30, 23, 50))
        self.octo = span(datetime(2026, 10, 3), datetime(2026, 11, 2, 23, 50))
        self.nov = span(datetime(2026, 11, 1), datetime(2026, 11, 10, 23, 50))
        self.current = write_tides(self.dir / "marconi_tides_sherwood.xlsx", self.nov,
                                   [tide(t) for t in self.nov])
        write_tides(self.dir / "marconi_tides_sherwood_20260901-20260930.xlsx", self.sep,
                    [tide(t) for t in self.sep])
        write_tides(self.dir / "marconi_tides_sherwood_20261003-20261102.xlsx", self.octo,
                    [tide(t) + (0.02 if t >= self.nov[0] else 0) for t in self.octo])
        # hourly water level 0.5 m above the tide; 5 m above it in the gap,
        # where a line drawn across the gap would count it
        hours = span(datetime(2026, 9, 1), datetime(2026, 11, 10, 23), timedelta(hours=1))
        gap = (datetime(2026, 10, 1), datetime(2026, 10, 3))
        self.spline = write_spline(self.dir / "usgs_spline_out.txt", hours,
                                   [tide(t) + (5.0 if gap[0] <= t < gap[1] else self.OFFSET)
                                    for t in hours])

    def run_main(self, *extra) -> str:
        argv = ["plot_gnssir_vs_tide.py", "--spline-file", str(self.spline),
                "--tide-file", str(self.current), "--tide-value-col", COL,
                "--output", str(self.dir / "vs_tide.png"), *extra]
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            pgt.main()
        return out.getvalue()

    def orange_and_label(self):
        ax = plt.gcf().axes[0]
        orange, = [ln for ln in ax.get_lines() if ln.get_color() == "tab:orange"]
        x = [v if isinstance(v, datetime) else matplotlib.dates.num2date(v).replace(tzinfo=None)
             for v in orange.get_xdata()]
        return x, np.asarray(orange.get_ydata(), float), ax.get_legend().get_texts()[0].get_text()

    def test_earlier_months_are_drawn(self) -> None:
        printed = self.run_main("--tide-history",
                                str(self.dir / "marconi_tides_sherwood_*.xlsx"))
        self.assertIn("Tide model from marconi_tides_sherwood.xlsx and 2 earlier file(s):", printed)
        self.assertIn("WARNING: they disagree", printed)
        x, y, label = self.orange_and_label()
        self.assertEqual(label, f"GNSS-IR water level (shifted {-self.OFFSET:+.3f} m)")
        # one break, across the gap
        nan = np.flatnonzero(np.isnan(y))
        self.assertEqual(len(nan), 1)
        self.assertEqual((x[nan[0] - 1], x[nan[0] + 1]), (self.sep[-1], self.octo[0]))
        keep = ~np.isnan(y)
        times = [t for t, k in zip(x, keep) if k]
        expect_t = self.sep + [t for t in self.octo if t < self.nov[0]] + self.nov
        self.assertEqual(len(times), len(expect_t))
        for got, want in zip(times, expect_t):
            self.assertLess(abs((got - want).total_seconds()), 1.0)
        # the current file's values where it overlaps October's
        np.testing.assert_allclose(y[keep], [tide(t) for t in expect_t], rtol=0, atol=1e-9)

    def test_without_history_the_current_file_alone(self) -> None:
        printed = self.run_main()
        self.assertNotIn("earlier file", printed)
        x, y, label = self.orange_and_label()
        self.assertFalse(np.isnan(y).any())
        self.assertLess(abs((x[0] - self.nov[0]).total_seconds()), 1.0)
        self.assertLess(abs((x[-1] - self.nov[-1]).total_seconds()), 1.0)
        np.testing.assert_allclose(y, [tide(t) for t in self.nov], rtol=0, atol=1e-9)
        self.assertEqual(label, f"GNSS-IR water level (shifted {-self.OFFSET:+.3f} m)")

    def test_unmatched_pattern_is_named_and_harmless(self) -> None:
        printed = self.run_main("--tide-history", str(self.dir / "nothing_*.xlsx"))
        self.assertIn(f"Earlier tide files: nothing matches {self.dir / 'nothing_*.xlsx'}", printed)
        _, y, _ = self.orange_and_label()
        self.assertEqual(len(y), len(self.nov))


@unittest.skipIf(openpyxl is None, "numpy, matplotlib or openpyxl not installed")
class PipelineTests(unittest.TestCase):

    def setUp(self) -> None:
        import test_run_status as trs
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.proj = trs.make_project(self.root)
        self.argv_log = self.root / "plot_argv.json"
        (self.proj / "analysis_tools" / "plot_gnssir_vs_tide.py").write_text(
            "import json, sys\n"
            f"open({str(self.argv_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("STUB_") and k != "REFL_CODE"}
        self.env["PATH"] = f"{self.root / 'bin'}:{self.env.get('PATH', '/usr/bin:/bin')}"
        self.station_json = self.proj / "station" / "resources" / "station.json"
        # a folder with a space, holding a file the pattern matches: the
        # shell must hand the pattern on as it is, not expand or split it
        self.spaced = self.root / "old tides"
        self.spaced.mkdir()
        (self.spaced / "marconi_tides_a.xlsx").write_text("x")

    def plot_argv(self, **station) -> list:
        cfg = {"station_id": "USGS00USA", "tide_model_file": str(self.proj / "tides.xlsx"),
               "tide_model_value_column": COL, **station}
        self.station_json.write_text(json.dumps(cfg))
        r = subprocess.run(["bash", str(self.proj / "process_and_plot.sh")], cwd=self.proj,
                           env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(self.argv_log.read_text()), r.stdout

    def history_args(self, argv) -> list:
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--tide-history"]

    def test_list_passed_unexpanded_in_order(self) -> None:
        pattern = str(self.spaced / "marconi_tides_*.xlsx")
        argv, out = self.plot_argv(tide_model_history=[pattern, "/data/old.xlsx", "", 3])
        self.assertEqual(self.history_args(argv), [pattern, "/data/old.xlsx"])
        self.assertEqual(argv[argv.index("--tide-file") + 1], str(self.proj / "tides.xlsx"))
        self.assertIn("The plot adds the earlier tide files in tide_model_history.", out)

    def test_one_string(self) -> None:
        argv, _ = self.plot_argv(tide_model_history="/data/old.xlsx")
        self.assertEqual(self.history_args(argv), ["/data/old.xlsx"])

    def test_absent_or_null_passes_nothing(self) -> None:
        for station in ({}, {"tide_model_history": None}, {"tide_model_history": []}):
            with self.subTest(station=station):
                argv, out = self.plot_argv(**station)
                self.assertNotIn("--tide-history", argv)
                self.assertNotIn("tide_model_history", out)


if __name__ == "__main__":
    unittest.main()
