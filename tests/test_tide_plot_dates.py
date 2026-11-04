"""
tests/test_tide_plot_dates.py

The tide comparison plot (plot_gnssir_vs_tide.py) labels its date axis
more finely than matplotlib's automatic choice. date_ticks picks the
step from DATE_TICKS by the plotted span (record or window, plus
matplotlib's margins).

  * StepTests -- for a span inside each row of DATE_TICKS, the exact
    labelled and unlabelled ticks, and the label format (with the hour
    under 15 days).
  * SweepTests -- from about an hour to 200 years: never more than 30
    labels, never fewer than matplotlib's automatic choice gave on the
    same span (the plot before this change), the 1st of every month in
    view labelled (of every year up to 30 years), and no matplotlib
    warning (too many ticks) once the figure is drawn.
  * MainTests -- main() itself, margins included: the station's record
    (7 July to 1 December) is labelled on the 1st, 8th, 15th and 22nd
    with a tick every day, and a one-month --start-date/--end-date
    window every other day.

Run with:
    python3 -m unittest discover -s tests -p "test_tide_plot_dates.py" -v
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
sys.path.insert(0, str(ROOT / "tests"))

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import plot_gnssir_vs_tide as pgt  # noqa: E402
except ImportError:  # pragma: no cover
    plt = None

T0 = datetime(2026, 7, 7, 3, 17)   # off any tick, so no step gets a free label at the start


def to_dt(x: float) -> datetime:
    return mdates.num2date(x).replace(tzinfo=None, microsecond=0)


def axis(days: float):
    """A plot axis over T0..T0+days with date_ticks applied: (fig, ax)."""
    fig, ax = plt.subplots(figsize=(16, 6))
    ax.set_xlim(mdates.date2num(T0), mdates.date2num(T0 + timedelta(days=days)))
    pgt.date_ticks(ax)
    return fig, ax


def in_view(ax, locs):
    lo, hi = ax.get_xlim()
    return [to_dt(x) for x in locs if lo <= x <= hi]


def grid(days: float, step: timedelta):
    """Every whole multiple of `step` (from midnight) in T0..T0+days."""
    t = datetime(T0.year, T0.month, T0.day)
    out = []
    while t <= T0 + timedelta(days=days):
        if t >= T0:
            out.append(t)
        t += step
    return out


def month_starts(days: float):
    out, y, m = [], T0.year, T0.month
    while True:
        t = datetime(y, m, 1)
        if t > T0 + timedelta(days=days):
            return out
        if t >= T0:
            out.append(t)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


HOUR, DAY = timedelta(hours=1), timedelta(days=1)


@unittest.skipIf(plt is None, "numpy or matplotlib not installed")
class StepTests(unittest.TestCase):

    # span in days: (candidates, is_labelled, is_minor, label has the hour)
    CASES = {
        1.0: (lambda d: grid(d, timedelta(minutes=15)),
              lambda t: t.minute == 0, lambda t: t.minute in (15, 30, 45), True),
        2.0: (lambda d: grid(d, HOUR), lambda t: t.hour % 2 == 0, lambda t: True, True),
        3.0: (lambda d: grid(d, HOUR), lambda t: t.hour % 3 == 0, lambda t: True, True),
        6.0: (lambda d: grid(d, HOUR), lambda t: t.hour % 6 == 0, lambda t: t.hour % 3 == 0, True),
        12.0: (lambda d: grid(d, HOUR), lambda t: t.hour in (0, 12), lambda t: t.hour % 3 == 0, True),
        25.0: (lambda d: grid(d, HOUR), lambda t: t.hour == 0, lambda t: t.hour in (6, 12, 18), False),
        45.0: (lambda d: grid(d, DAY), lambda t: t.day % 2 == 1 and t.day < 30, lambda t: True, False),
        90.0: (lambda d: grid(d, DAY), lambda t: t.day in (1, 4, 8, 11, 15, 18, 22, 25),
               lambda t: True, False),
        160.0: (lambda d: grid(d, DAY), lambda t: t.day in (1, 8, 15, 22), lambda t: True, False),
        300.0: (lambda d: grid(d, DAY), lambda t: t.day in (1, 15),
                lambda t: t.day in (1, 8, 15, 22), False),
        700.0: (lambda d: grid(d, DAY), lambda t: t.day == 1, lambda t: t.day in (1, 15), False),
        1500.0: (month_starts, lambda t: t.month % 2 == 1, lambda t: True, False),
        2500.0: (month_starts, lambda t: t.month in (1, 4, 7, 10), lambda t: True, False),
        4000.0: (month_starts, lambda t: t.month in (1, 7), lambda t: t.month in (1, 4, 7, 10), False),
        8000.0: (month_starts, lambda t: t.month == 1, lambda t: t.month in (1, 7), False),
    }

    def labels_shown(self, fig, ax):
        """(tick time, label text) of each labelled tick in view."""
        fig.canvas.draw()
        lo, hi = ax.get_xlim()
        return [(to_dt(x), t.get_text()) for x, t in zip(ax.xaxis.get_majorticklocs(),
                                                         ax.get_xticklabels())
                if lo <= x <= hi]

    def test_each_step(self) -> None:
        for days, (cands, labelled, minor_ok, hour) in self.CASES.items():
            with self.subTest(days=days):
                fig, ax = axis(days)
                try:
                    c = cands(days)
                    want_major = [t for t in c if labelled(t)]
                    want_minor = [t for t in c if minor_ok(t) and not labelled(t)]
                    self.assertEqual(in_view(ax, ax.xaxis.get_majorticklocs()), want_major)
                    self.assertEqual(in_view(ax, ax.xaxis.get_minorticklocs()), want_minor)
                    shown = self.labels_shown(fig, ax)
                    self.assertTrue(shown)
                    fmt = "%Y-%m-%d %H:%M" if hour else "%Y-%m-%d"
                    for t, text in shown:
                        self.assertEqual(text, f"{t:{fmt}}")
                finally:
                    plt.close(fig)

    def test_under_half_a_day_is_automatic_with_the_time(self) -> None:
        fig, ax = axis(0.3)
        try:
            self.assertIsInstance(ax.xaxis.get_major_locator(), mdates.AutoDateLocator)
            shown = self.labels_shown(fig, ax)
            self.assertGreaterEqual(len(shown), 8)
            for t, text in shown:
                self.assertEqual(text, f"{t:%Y-%m-%d %H:%M}")
            minor = in_view(ax, ax.xaxis.get_minorticklocs())
            self.assertTrue(minor)
            self.assertFalse([t for t in minor if t.minute % 5 or t.second])
        finally:
            plt.close(fig)

    def test_over_30_years_is_automatic(self) -> None:
        fig, ax = axis(40 * 365.25)
        try:
            self.assertIsInstance(ax.xaxis.get_major_locator(), mdates.AutoDateLocator)
            shown = self.labels_shown(fig, ax)
            self.assertTrue(8 <= len(shown) <= 30, len(shown))
            for t, text in shown:
                self.assertEqual(text, f"{t:%Y-%m-%d}")
        finally:
            plt.close(fig)


@unittest.skipIf(plt is None, "numpy or matplotlib not installed")
class SweepTests(unittest.TestCase):

    def test_never_crowded_never_coarser_than_before(self) -> None:
        warned = []
        handler = logging.Handler(logging.WARNING)
        handler.emit = lambda record: warned.append(record.getMessage())
        log = logging.getLogger("matplotlib")
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        days = 0.05
        while days < 200 * 366:
            with self.subTest(days=round(days, 3)):
                fig, ax = axis(days)
                try:
                    fig.canvas.draw()
                    self.assertEqual(warned, [])
                    major = in_view(ax, ax.xaxis.get_majorticklocs())
                    self.assertLessEqual(len(major), 30)
                    ax.xaxis.set_major_locator(mdates.AutoDateLocator())   # the plot before
                    before = in_view(ax, ax.xaxis.get_majorticklocs())
                    self.assertGreaterEqual(len(major), len(before))
                    if 1.2 <= days <= 900:
                        for t in month_starts(days):
                            self.assertIn(t, major)
                    elif 900 < days <= 10800:
                        for t in month_starts(days):
                            if t.month == 1:
                                self.assertIn(t, major)
                finally:
                    plt.close(fig)
            days *= 1.03


@unittest.skipIf(plt is None, "numpy or matplotlib not installed")
class MainTests(unittest.TestCase):

    def setUp(self) -> None:
        import test_tide_history as th
        if th.openpyxl is None:
            self.skipTest("openpyxl not installed")
        self.th = th
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(plt.close, "all")
        d = self.dir = Path(self._tmp.name)
        (d / "station" / "resources").mkdir(parents=True)
        self.station_json = d / "station" / "resources" / "station.json"
        self.station_json.write_text(json.dumps({"vertical_datum": "NAVD88 (GEOID18)",
                                                 "gnssrefl_orthometric_height": th.sd.NAVD88_HORTHO_M,
                                                 "tide_model_navd88_offset": 0.0}))
        tt = th.span(datetime(2026, 7, 15), datetime(2026, 12, 1, 23, 50))
        th.write_tides(d / "tides.xlsx", tt, [th.tide(t) for t in tt])
        hours = th.span(datetime(2026, 7, 7), datetime(2026, 11, 4), timedelta(hours=1))
        th.write_spline(d / "spline.txt", hours, [th.tide(t) + 0.1 for t in hours])

    def labels(self, *extra):
        argv = ["plot_gnssir_vs_tide.py", "--spline-file", str(self.dir / "spline.txt"),
                "--tide-file", str(self.dir / "tides.xlsx"), "--tide-value-col", self.th.COL,
                "--output", str(self.dir / "out.png"), *extra]
        with unittest.mock.patch.object(self.th.sd, "STATION_JSON", self.station_json), \
                unittest.mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            pgt.main()
        ax = plt.gcf().axes[0]
        lo, hi = ax.get_xlim()
        major = [x for x in ax.xaxis.get_majorticklocs() if lo <= x <= hi]
        every = np.diff(np.unique(np.r_[major, [x for x in ax.xaxis.get_minorticklocs()
                                                if lo <= x <= hi]]))
        return [f"{to_dt(x):%Y-%m-%d}" for x in major], every

    def test_station_record(self) -> None:
        labels, every = self.labels()
        for day in ("2026-07-08", "2026-08-01", "2026-09-15", "2026-10-22", "2026-12-01"):
            self.assertIn(day, labels)
        self.assertFalse([x for x in labels if int(x[-2:]) not in (1, 8, 15, 22)])
        self.assertTrue(np.allclose(every, 1.0))   # a tick every day

    def test_one_month_window(self) -> None:
        labels, every = self.labels("--start-date", "2026-10-01", "--end-date", "2026-11-01")
        self.assertIn("2026-10-01", labels)
        self.assertIn("2026-11-01", labels)
        self.assertFalse([x for x in labels if int(x[-2:]) % 2 == 0])
        self.assertGreaterEqual(len(labels), 16)   # matplotlib's own choice gave 9
        self.assertTrue(np.allclose(every, 1.0))


if __name__ == "__main__":
    unittest.main()
