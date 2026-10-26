"""
tests/test_tide_datum.py

The predicted tide is put on the datum of the water levels it is drawn
or compared with in one place, station_datum.tide_model_in_datum: onto
NAVD88 by tide_model_navd88_offset, then, for the public 7-day plot,
down by the mean-sea-level height the water levels are drawn above.
The 7-day plot draws the model raw unless station.json sets
plot_7day_tide_on_msl (opt-in, register L1), so by default every
departure label carries (tide_model_navd88_offset -
water_level_msl_offset): revising the mean-sea-level height moves
every label, and recalibrating the tide offset never reaches the plot.

  * TideModelInDatumTests -- the helper, and that it gives the NAVD88
    products exactly the numbers they computed before.
  * NAVD88ProductTests -- the main() of each NAVD88 product that
    converts the tide (export_timeseries, plot_gnssir_vs_tide,
    filter_month, compare_to_tide_deviation): the tide it writes, draws
    or compares is the raw model + tide_model_navd88_offset (or
    --tide-datum-offset), never the mean-sea-level datum, and
    filter_month takes it from the station.json it read itself.
  * DepartureDatumTests -- plot_7day.main() on a synthetic week, a
    known surge on a known tide, under several station.json datums:
    with plot_7day_tide_on_msl set, each departure is the NAVD88 water
    level minus the NAVD88 tide -- the mean offset
    compare_to_tide_deviation reports -- whatever
    water_level_msl_offset says; unset, the model is drawn raw, as
    published.

Run with:
    python3 -m unittest discover -s tests -p "test_tide_datum.py" -v
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import math
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
sys.path.insert(0, str(ROOT / "maintenance"))
import station_datum as sd  # noqa: E402
import filter_month  # noqa: E402

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import plot_7day  # noqa: E402
    import compare_to_tide_deviation as ctd  # noqa: E402
    import export_timeseries  # noqa: E402
    import plot_gnssir_vs_tide  # noqa: E402
except ImportError:  # pragma: no cover
    np = None

MJD0 = date(1858, 11, 17)
FIRST = datetime(2026, 10, 12, 0, 0)
LAST = datetime(2026, 10, 19, 23, 30)
STEP = timedelta(minutes=30)
# The model is sampled at the water level's times, so each measured high
# and low falls on the same reading as the predicted one and a departure
# is exactly the surge plus the datum terms.
PERIOD_H = 12.5
TRUE_MODEL_ZERO = 0.09   # the model's zero (mean sea level) on NAVD88
BLUE, ORANGE = "#1f6fb4", "#d9822b"


def model_tide(t: datetime) -> float:
    """The raw tide model at t, metres about its own zero (mm steps,
    so the spline file's three decimals hold the level exactly)."""
    hours = (t - FIRST).total_seconds() / 3600
    return round(1.0 * math.cos(2 * math.pi * hours / PERIOD_H), 3)


def plotted_model(n: int):
    """The raw model over the plot's window, the newest n samples."""
    return np.array([model_tide(t) for t in week()])[-n:]


def week():
    t, out = FIRST, []
    while t <= LAST:
        out.append(t)
        t += STEP
    return out


def spline_text(levels_navd88, hortho: float) -> str:
    """A gnssrefl spline file whose header says hortho; levels are
    written in that height's datum, as gnssrefl writes them."""
    lines = [
        "% station usgs gnssrefl v4.1.5\n",
        "% orthometric height minus RH, where Hortho (m) is "
        f"{hortho:10.3f}\n",
        "%  MJD              RH(m)  YYYY  MM  DD  HH  MM  SS   quasi-sea-level(m)\n",
    ]
    shift = sd.NAVD88_HORTHO_M - hortho
    for t, level in zip(week(), levels_navd88):
        m = (t.date() - MJD0).days + (t.hour * 60 + t.minute) / 1440
        v = level - shift
        lines.append(f"{m:15.7f} {hortho - v:11.3f} {t.year:4d} {t.month:3d} {t.day:3d}"
                     f" {t.hour:3d} {t.minute:3d} {0:3d} {v:10.3f} \n")
    return "".join(lines)


class StationFiles(unittest.TestCase):
    """A throwaway project: station/resources/station.json, which both
    station_datum and plot_7day read in place of the real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name)
        (self.proj / "station" / "resources").mkdir(parents=True)
        (self.proj / "analysis_tools").mkdir()
        self.station_json = self.proj / "station" / "resources" / "station.json"
        patch = unittest.mock.patch.object(sd, "STATION_JSON", self.station_json)
        patch.start()
        self.addCleanup(patch.stop)

    def write_station(self, **cfg) -> dict:
        self.station_json.write_text(json.dumps(cfg))
        return cfg


class TideModelInDatumTests(StationFiles):

    def test_navd88_list_is_the_old_per_height_addition(self) -> None:
        # filter_month's old arithmetic; NAVD88ProductTests runs filter_month itself
        cfg = {"tide_model_navd88_offset": 0.07}
        raw = [0.5, -1.25, 0.1, 1.234]
        heights, shift = sd.tide_model_in_datum(raw, "NAVD88", cfg)
        old_shift = sd.tide_model_to_navd88(cfg)
        self.assertEqual(shift, old_shift)
        self.assertEqual(heights, [v + old_shift for v in raw])
        self.assertIsInstance(heights, list)

    @unittest.skipIf(np is None, "numpy not installed")
    def test_navd88_array_is_what_the_comparison_tools_computed(self) -> None:
        self.write_station(tide_model_navd88_offset=0.083)
        raw = np.array([0.5, -1.25, 0.1, 1.234, -0.333])
        for override in (None, 0.2, -0.05):
            heights, shift = sd.tide_model_in_datum(np.asarray(raw, float), "NAVD88",
                                                    model_offset=override)
            old_shift = override if override is not None else sd.tide_model_to_navd88()
            self.assertEqual(shift, old_shift)
            self.assertTrue(np.array_equal(heights, np.asarray(raw, float) + old_shift))
            self.assertIsInstance(heights, np.ndarray)

    def test_navd88_default_without_station_json(self) -> None:
        heights, shift = sd.tide_model_in_datum([0.0], "NAVD88")
        self.assertEqual(shift, sd.DEFAULT_TIDE_MODEL_TO_NAVD88_M)
        self.assertEqual(heights, [sd.DEFAULT_TIDE_MODEL_TO_NAVD88_M])

    def test_msl_goes_through_navd88(self) -> None:
        cfg = {"vertical_datum": "NAVD88 (GEOID18)", "gnssrefl_orthometric_height": 19.014,
               "water_level_msl_offset": 0.10, "tide_model_navd88_offset": 0.09}
        heights, shift = sd.tide_model_in_datum([1.0, -1.0], "MSL", cfg)
        self.assertAlmostEqual(shift, 0.09 - 0.10, places=12)
        self.assertEqual(heights, [1.0 + shift, -1.0 + shift])
        # case does not matter
        self.assertEqual(sd.tide_model_in_datum([1.0], "msl", cfg)[1], shift)

    def test_msl_of_an_unmigrated_station_json(self) -> None:
        # CGVD2013 antenna height, mean sea level stored in the same datum:
        # both are moved onto NAVD88 by the same 0.349 m.
        cfg = {"gnssrefl_orthometric_height": 18.665, "water_level_msl_offset": -0.249,
               "tide_model_navd88_offset": 0.09}
        self.assertAlmostEqual(sd.msl_navd88(cfg), 0.100, places=9)
        self.assertAlmostEqual(sd.tide_model_in_datum([0.0], "MSL", cfg)[1], -0.010, places=9)

    def test_msl_navd88_unset_or_unreadable_is_zero(self) -> None:
        self.assertEqual(sd.msl_navd88({}), 0.0)
        self.assertEqual(sd.msl_navd88({"water_level_msl_offset": "abc"}), 0.0)
        self.assertEqual(sd.msl_navd88(), 0.0)              # no station.json at all
        self.station_json.write_text("{not json")
        self.assertEqual(sd.msl_navd88(), 0.0)

    def test_unknown_datum_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sd.tide_model_in_datum([0.0], "CGVD2013", {})


@unittest.skipIf(np is None, "numpy and matplotlib not installed")
class NAVD88ProductTests(StationFiles):
    """Each NAVD88 product's main() on the synthetic week, its tide
    loader patched to hand it the raw model. The station.json tide
    offset (0.07 m) is not the 0.09 m default and mean sea level is
    set (0.10 m), so a product that fell back to the default, ignored
    --tide-datum-offset or asked for "MSL" would write, draw or
    compare a different tide."""

    TIDE_OFFSET = 0.07
    MSL = 0.10
    SURGE = 0.30

    def setUp(self) -> None:
        super().setUp()
        self.write_station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014,
                           water_level_msl_offset=self.MSL,
                           tide_model_navd88_offset=self.TIDE_OFFSET)
        self.tide_file = self.proj / "marconi_tides.xlsx"
        self.tide_file.write_bytes(b"")      # exists; its rows come from the patched loader
        self.products = self.proj / "products"
        self.products.mkdir()
        self.spline = self.products / "usgs_spline_out.txt"
        self.spline.write_text(spline_text(
            [model_tide(t) + TRUE_MODEL_ZERO + self.SURGE for t in week()], sd.NAVD88_HORTHO_M))
        self.raw = [model_tide(t) for t in week()]
        self.addCleanup(plt.close, "all")

    def run_main(self, module, loader: str, rows, argv) -> str:
        """module.main() with argv, its tide loader returning the week's
        times and rows; what it printed."""
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", [module.__name__ + ".py", *argv]), \
                unittest.mock.patch.object(module, loader, return_value=(week(), rows)), \
                contextlib.redirect_stdout(out):
            module.main()
        return out.getvalue()

    def tide_args(self, override) -> list:
        args = ["--spline-file", str(self.spline), "--tide-file", str(self.tide_file),
                "--tide-value-col", "EOT20_heightm"]
        return args + (["--tide-datum-offset", str(override)] if override is not None else [])

    def assert_tide_column(self, csv_path: Path, shift: float) -> None:
        """The CSV's tide column is the raw model + shift, row for row,
        in the 4 decimals the products write."""
        with open(csv_path, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][2], "tide_model_EOT20_heightm_navd88_m")
        self.assertEqual([r[2] for r in rows[1:]],
                         [f"{v:.4f}" for v in np.asarray(self.raw, float) + shift])

    def test_export_timeseries(self) -> None:
        for override, shift in ((None, self.TIDE_OFFSET), (0.2, 0.2)):
            with self.subTest(tide_datum_offset=override):
                dest = self.proj / "timeseries_export.csv"
                printed = self.run_main(export_timeseries, "load_tide_reference",
                                        np.asarray(self.raw, float),
                                        self.tide_args(override) + ["--output", str(dest)])
                self.assertIn(f"moved {shift:+.3f} m onto NAVD88", printed)
                self.assert_tide_column(dest, shift)

    def test_plot_gnssir_vs_tide(self) -> None:
        for override, shift in ((None, self.TIDE_OFFSET), (0.2, 0.2)):
            with self.subTest(tide_datum_offset=override):
                printed = self.run_main(plot_gnssir_vs_tide, "load_tide_reference",
                                        np.asarray(self.raw, float),
                                        self.tide_args(override)
                                        + ["--output", str(self.proj / "gnssir_vs_tide.png")])
                self.assertIn(f"moved {shift:+.3f} m onto NAVD88", printed)
                ax = plt.gcf().axes[0]
                orange, = [ln for ln in ax.get_lines() if ln.get_color() == "tab:orange"]
                self.assertTrue(np.array_equal(np.asarray(orange.get_ydata(), float),
                                               np.asarray(self.raw, float) + shift))
                # the measured offset it removes is level minus that same tide
                label = ax.get_legend().get_texts()[0].get_text()
                self.assertEqual(label, "GNSS-IR water level (shifted "
                                 f"{-(TRUE_MODEL_ZERO + self.SURGE - shift):+.3f} m)")
                plt.close("all")

    def test_compare_to_tide_deviation(self) -> None:
        for override, shift in ((None, self.TIDE_OFFSET), (0.2, 0.2)):
            with self.subTest(tide_datum_offset=override):
                printed = self.run_main(ctd, "load_tide_reference", np.asarray(self.raw, float),
                                        self.tide_args(override))
                self.assertIn(f"moved {shift:+.3f} m onto NAVD88", printed)
                self.assertIn("Mean offset (GNSS - tide): "
                              f"{TRUE_MODEL_ZERO + self.SURGE - shift:+.3f} m", printed)

    def month_csv(self, outdir: Path) -> Path:
        self.run_main(filter_month, "load_tide", list(self.raw),
                      ["--year", "2026", "--month", "10", "--products-dir", str(self.products),
                       "--output-dir", str(outdir), "--tide-file", str(self.tide_file),
                       "--tide-value-col", "EOT20_heightm"])
        return outdir / "usgs_timeseries_october2026.csv"

    def test_filter_month(self) -> None:
        self.assert_tide_column(self.month_csv(self.proj / "month"), self.TIDE_OFFSET)

    def test_filter_month_uses_the_station_json_it_read(self) -> None:
        # filter_month reads station.json once and takes the antenna
        # height and the tide offset from that one read. Served a
        # different read than the file on disk, the tide follows it.
        cfg = dict(sd.load_station(), tide_model_navd88_offset=0.05)
        with unittest.mock.patch.object(filter_month, "load_station", return_value=cfg):
            dest = self.month_csv(self.proj / "month")
        self.assert_tide_column(dest, 0.05)


@unittest.skipIf(np is None, "numpy and matplotlib not installed")
class DepartureDatumTests(StationFiles):
    """plot_7day as process_and_plot.sh runs it (station.json for the
    tide settings, --no-qc), on a week where the water stands a known
    surge above the true tide at every high and low."""

    def setUp(self) -> None:
        super().setUp()
        self.tide_file = self.proj / "marconi_tides.xlsx"
        self.tide_file.write_bytes(b"")      # exists; its rows come from load_tide below
        self.spline = self.proj / "usgs_spline_out.txt"
        patch = unittest.mock.patch.object(plot_7day, "__file__",
                                           str(self.proj / "analysis_tools" / "plot_7day.py"))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(plt.close, "all")

    def station(self, tide_on_msl=True, **cfg) -> dict:
        """station.json for the run; tide_on_msl None leaves
        plot_7day_tide_on_msl unset."""
        if tide_on_msl is not None:
            cfg[plot_7day.TIDE_ON_MSL_KEY] = tide_on_msl
        return self.write_station(station_name="Test Beach", tide_model_file=str(self.tide_file),
                                  tide_model_value_column="EOT20_heightm", **cfg)

    def run_plot(self, surge: float, hortho: float = sd.NAVD88_HORTHO_M):
        """(departures, legend labels, blue line, orange line) of one
        plot_7day run, the orange line None when no tide is drawn.
        Observed = model + its true NAVD88 zero + surge."""
        times = week()
        observed = [model_tide(t) + TRUE_MODEL_ZERO + surge for t in times]
        self.spline.write_text(spline_text(observed, hortho))
        departures = []
        real = plot_7day.peak_departures

        def spy(*args, **kwargs):
            out = real(*args, **kwargs)
            departures.extend(d for _, _, d in out)
            return out

        argv = ["plot_7day.py", "--spline-file", str(self.spline),
                "--output", str(self.proj / "7_day_plot.png"), "--no-qc"]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(plot_7day, "load_tide",
                                           return_value=(times, [model_tide(t) for t in times])), \
                unittest.mock.patch.object(plot_7day, "peak_departures", spy), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(plot_7day.main(), 0)
        ax = plt.gcf().axes[0]
        labels = [t.get_text() for t in ax.get_legend().get_texts()]
        lines = {ln.get_color(): np.asarray(ln.get_ydata(), float) for ln in ax.get_lines()
                 if ln.get_color() in (BLUE, ORANGE) and len(ln.get_ydata())}
        plt.close("all")
        return departures, labels, lines[BLUE], lines.get(ORANGE)

    def comparison_mean_offset(self) -> float:
        """compare_to_tide_deviation's 'Mean offset (GNSS - tide)' for
        the spline run_plot last wrote."""
        times = week()
        argv = ["compare_to_tide_deviation.py", "--spline-file", str(self.spline),
                "--tide-file", str(self.tide_file), "--tide-value-col", "EOT20_heightm"]
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(ctd, "load_tide_reference",
                                           return_value=(times, np.array([model_tide(t)
                                                                          for t in times]))), \
                contextlib.redirect_stdout(out):
            ctd.main()
        line, = [ln for ln in out.getvalue().splitlines() if ln.startswith("Mean offset")]
        return float(line.split(":", 1)[1].split()[0])

    def assert_departures(self, departures, expected: float) -> None:
        self.assertGreater(len(departures), 20)            # ~13 highs and 13 lows
        for d in departures:
            self.assertAlmostEqual(d, expected, places=9)

    def test_departure_is_navd88_level_minus_navd88_tide(self) -> None:
        self.station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014,
                     water_level_msl_offset=0.10, tide_model_navd88_offset=0.09)
        departures, labels, blue, orange = self.run_plot(surge=0.30)
        # 0.30, not the 0.29 the raw model gave (0.09 - 0.10 in every label)
        self.assert_departures(departures, 0.30)
        self.assertAlmostEqual(self.comparison_mean_offset(), 0.30, places=3)
        # the two curves, then one entry per marked high or low
        self.assertEqual(labels[:2], ["Estimated water level", "Predicted tide"])
        self.assertEqual(len(labels), 2 + plot_7day.MAX_MARKED)
        for label in labels[2:]:
            self.assertRegex(label, r"\n0\.30 m above predicted (high|low) tide$")
        # both curves on local mean sea level: NAVD88 minus 0.10
        raw = plotted_model(len(blue))
        self.assertTrue(np.allclose(orange, raw + 0.09 - 0.10, atol=1e-12))
        self.assertTrue(np.allclose(blue, raw + TRUE_MODEL_ZERO + 0.30 - 0.10, atol=1e-9))

    def test_by_default_the_model_is_drawn_raw_as_published(self) -> None:
        # plot_7day_tide_on_msl unset or false: the orange line is the
        # model as the spreadsheet gives it and every label carries
        # 0.09 - 0.10, as on the published plot (register L1 waits for
        # the owner)
        for setting in (None, False, "false"):
            with self.subTest(plot_7day_tide_on_msl=setting):
                self.station(tide_on_msl=setting, vertical_datum="NAVD88 (GEOID18)",
                             gnssrefl_orthometric_height=19.014,
                             water_level_msl_offset=0.10, tide_model_navd88_offset=0.09)
                departures, labels, blue, orange = self.run_plot(surge=0.30)
                self.assert_departures(departures, 0.29)
                self.assertEqual(len(labels), 2 + plot_7day.MAX_MARKED)
                for label in labels[2:]:
                    self.assertRegex(label, r"\n0\.29 m above predicted (high|low) tide$")
                raw = plotted_model(len(blue))
                self.assertTrue(np.array_equal(orange, raw))
                self.assertTrue(np.allclose(blue, raw + TRUE_MODEL_ZERO + 0.30 - 0.10, atol=1e-9))

    def test_mean_sea_level_moves_both_curves_and_no_departure(self) -> None:
        runs = {}
        for msl in (0.10, 0.20, -0.05):
            self.station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014,
                         water_level_msl_offset=msl, tide_model_navd88_offset=0.09)
            runs[msl] = self.run_plot(surge=0.30)
        for msl, (departures, labels, blue, orange) in runs.items():
            self.assert_departures(departures, 0.30)
            self.assertEqual(labels, runs[0.10][1])
            self.assertTrue(np.allclose(blue, runs[0.10][2] + 0.10 - msl, atol=1e-9))
            self.assertTrue(np.allclose(orange, runs[0.10][3] + 0.10 - msl, atol=1e-9))

    def test_recalibrated_tide_offset_reaches_the_plot(self) -> None:
        # The same water, a tide offset revised from 0.09 to 0.04 m: the
        # departures move by the 0.05 m, as they do in every NAVD88
        # product (the raw model left the public plot unchanged).
        self.station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014,
                     water_level_msl_offset=0.10, tide_model_navd88_offset=0.04)
        departures, _, _, _ = self.run_plot(surge=0.30)
        self.assert_departures(departures, 0.35)
        self.assertAlmostEqual(self.comparison_mean_offset(), 0.35, places=3)

    def test_fresh_install_without_either_offset(self) -> None:
        # install.sh writes neither key: an unshifted plot (zero = NAVD88)
        # and the default tide offset, so the departures are still right
        # (the raw model put them 0.09 m high).
        self.station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014)
        departures, _, blue, orange = self.run_plot(surge=0.30)
        self.assert_departures(departures, 0.30)
        raw = plotted_model(len(blue))
        self.assertTrue(np.allclose(blue, raw + TRUE_MODEL_ZERO + 0.30, atol=1e-9))
        self.assertTrue(np.allclose(orange, raw + sd.DEFAULT_TIDE_MODEL_TO_NAVD88_M, atol=1e-12))

    def test_unmigrated_cgvd2013_station(self) -> None:
        # Old antenna height in station.json and in the spline header, mean
        # sea level stored in that datum: levels and mean sea level both
        # move up 0.349 m, and the departures do not move.
        self.station(gnssrefl_orthometric_height=18.665, water_level_msl_offset=-0.249,
                     tide_model_navd88_offset=0.09)
        departures, _, blue, orange = self.run_plot(surge=0.30, hortho=sd.CGVD2013_HORTHO_M)
        self.assert_departures(departures, 0.30)
        raw = plotted_model(len(blue))
        self.assertTrue(np.allclose(orange, raw + 0.09 - 0.100, atol=1e-9))

    def test_surge_just_over_the_threshold_is_marked(self) -> None:
        # +0.255 m against a 0.25 m threshold, with the two offsets 9 mm
        # apart as they are at the station: the raw model read 0.246 and
        # left every high and low unmarked.
        self.station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014,
                     water_level_msl_offset=0.099, tide_model_navd88_offset=0.09)
        departures, labels, _, _ = self.run_plot(surge=0.255)
        self.assert_departures(departures, 0.255)
        self.assertEqual(len(labels), 2 + plot_7day.MAX_MARKED)

    def test_unusable_tide_offset_costs_the_prediction_not_the_plot(self) -> None:
        # A tide offset that is not a number: no tide drawn on a guessed
        # datum and no departure computed from it, but the water level is
        # still plotted, as when the tide spreadsheet cannot be read.
        self.station(vertical_datum="NAVD88 (GEOID18)", gnssrefl_orthometric_height=19.014,
                     water_level_msl_offset=0.10, tide_model_navd88_offset="0.09 m")
        departures, labels, blue, orange = self.run_plot(surge=0.30)
        self.assertEqual(departures, [])
        self.assertEqual(labels, ["Estimated water level"])
        self.assertIsNone(orange)
        raw = plotted_model(len(blue))
        self.assertTrue(np.allclose(blue, raw + TRUE_MODEL_ZERO + 0.30 - 0.10, atol=1e-9))


if __name__ == "__main__":
    unittest.main()
