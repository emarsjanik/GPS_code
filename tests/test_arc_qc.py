"""
test_arc_qc.py

Opt-in arc-level QC: per-frequency amplitude thresholds
(analysis_tools/amplitude_thresholds.py) and the scoring of QC variants
against the Chatham gauge before any is switched on
(analysis_tools/evaluate_qc_variants.py).

Four parts:
  * AmplitudeThresholdTests -- which failQC arcs failed on amplitude
    alone, select_day() raising and lowering thresholds exactly as gnssir
    would, and the median rule.
  * EvaluateTests -- the pieces (settings, staging, scores, the rule) and
    a whole run on a synthetic station with a stand-in `subdaily` on PATH
    (it averages the staged arcs into a 30-minute "spline"), so staging,
    fitting, scoring and the recommendation run for real without gnssrefl.
  * FirstSplineTests -- gnssrefl's first spline rebuilt (flipit3, the
    RHdot correction, the residual the first clip tests) and the check
    against the .withrhdot file; the rebuild needs scipy.
  * RealSubdailyEvaluateTests -- whole runs with gnssrefl's real
    subdaily: the rebuilt first spline, abs_outliers on a storm-free
    span, and the baseline reproducing the record in a frozen year and
    on the day after a freeze; skipped when gnssrefl is not installed.

Run with:
    python3 -m unittest discover -s tests -p "test_arc_qc.py" -v
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HAVE_SCIPY = importlib.util.find_spec("scipy") is not None
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
import amplitude_thresholds as at  # noqa: E402
import evaluate_qc_variants as ev  # noqa: E402
import gnss_record as gr  # noqa: E402
import water_level_qc as wq  # noqa: E402

STA = "usgs"
H = 19.0
MJD0 = date(1858, 11, 17)
SETTINGS = "-rhdot True -knots 8 -azim1 35 -azim2 125"
FMT = ("{:4.0f} {:3.0f} {:6.3f} {:3.0f} {:6.3f} {:6.2f} {:6.2f} {:6.2f} {:6.2f} {:4.0f}  {:3.0f}"
       "  {:2.0f} {:8.5f} {:6.2f} {:7.2f} {:12.6f} {:2.0f}\n")
HEAD = "% year, doy, RH, sat,UTCtime, Azim, Amp,  eminO, emaxO,NumbOf,freq,rise,EdotF, PkNoise,  DelT,     MJD,   refr\n"
CFG = {"station": STA, "freqs": [1, 20], "reqAmp": [5.0, 5.0], "minH": 14.0, "maxH": 24.0,
       "PkNoise": 2.8, "Hortho": [H], "subdaily_alt_sigma": None,
       "subdaily_spline_outlier1": None, "subdaily_spline_outlier2": None}


def line(mjd, rh=19.0, amp=10.0, freq=1, pkn=4.0, az=80.0):
    d = MJD0 + timedelta(days=int(mjd))
    return FMT.format(d.year, d.timetuple().tm_yday, rh, 5, (mjd % 1) * 24, az, amp, 5.05, 14.95,
                      200, freq, 1, 0.7, pkn, 30.0, mjd, 1)


def truth(m):
    t = (np.asarray(m, dtype=float) - 61000.0) * 24.0
    return 1.0 * np.cos(2 * np.pi * t / 12.4206) + 0.2 * np.cos(2 * np.pi * t / 12.0 + 1.3)


def build_station(refl: Path, first: date, last: date, storm, seed: int = 3, rhdot: bool = False,
                  cfg: dict | None = None) -> None:
    """Results and failQC files, the gnssir json and the gauge and wave
    caches of a synthetic station. In the storm every arc's amplitude is
    damped by u in 0.25..1 and, around high water, the arcs damped below
    half read 6 m high -- what a rough sea does (the arcs that survive it
    are the biased ones), made plain enough to stand out above the
    wave-setup allowance."""
    rng = np.random.default_rng(seed)
    s0, s1 = (storm[0] - MJD0).days, (storm[1] - MJD0).days + 1
    d = first
    while d <= last:
        n = 96
        m = np.sort((d - MJD0).days + rng.uniform(0, 1, n))
        fr = rng.choice([1, 20], n)
        amp = np.where(fr == 1, rng.normal(14, 2.5, n), rng.normal(8, 2.0, n))
        u = np.where((m >= s0) & (m < s1), rng.uniform(0.25, 1.0, n), 1.0)
        amp = amp * u
        err = rng.normal(0, 0.08, n) + np.where((u < 0.5) & (truth(m) > 0.3), 6.0, 0.0)
        rise = rng.choice([-1, 1], n)
        edotf = rise * rng.uniform(0.5, 0.9, n)
        drh = -(truth(m + 1e-4) - truth(m - 1e-4)) / 2e-4 / 24 if rhdot else np.zeros(n)
        rh = H - (truth(m) + err) + edotf * drh
        az = rng.uniform(25, 135, n)
        pkn = rng.uniform(3, 6, n)
        doy = d.timetuple().tm_yday
        rows = [(FMT.format(d.year, doy, rh[i], 1 + i % 32, (m[i] % 1) * 24, az[i], amp[i], 5.05,
                            14.95, 200, fr[i], rise[i], edotf[i], pkn[i], 30.0, m[i], 1), amp[i] > 5)
                for i in range(n)]
        folder = refl / str(d.year) / "results" / STA
        (folder / "failQC").mkdir(parents=True, exist_ok=True)
        (folder / f"{doy:03d}.txt").write_text(HEAD + "".join(r for r, ok in rows if ok))
        (folder / "failQC" / f"{doy:03d}.txt").write_text(HEAD + "".join(r for r, ok in rows if not ok))
        d += timedelta(days=1)
    if cfg is None:
        p = refl / "input" / STA / f"{STA}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(CFG, indent=4))
    files = refl / "Files" / STA
    files.mkdir(parents=True, exist_ok=True)
    t0 = ev.epoch(first - timedelta(days=35))
    t1 = ev.epoch(last + timedelta(days=2))
    with open(files / f"gauge_{wq.GAUGE_STATION}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "epoch", "level_navd88"])
        for e in np.arange(t0, t1, 360.0):
            w.writerow(["", int(e), f"{float(truth(e / 86400 + ev.MJD_UNIX)):.3f}"])
    with open(files / f"waves_{wq.WAVE_STATION}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "epoch", "wvht_m", "dpd_s"])
        for e in np.arange(t0, t1, 3600.0):
            m = e / 86400 + ev.MJD_UNIX
            stormy = s0 - 0.5 <= m < s1 + 0.5
            w.writerow(["", int(e), 2.0 if stormy else round(0.8 + 0.3 * math.sin(m / 2.0), 2),
                        7.0 if stormy else 8.0])


# A stand-in for gnssrefl's subdaily: reads the staged results of its
# -extension and writes, as subdaily names them, the per-year edit file,
# the .withrhdot and .withrhdotIF files and <sta>_spline_out.txt -- the
# level every 30 minutes being the mean of the arcs within 45 minutes,
# after dropping any arc more than -spline_outlier1 m from the median of
# those within an hour. -alt_sigma changes nothing here. The .withrhdot
# file carries the RHdot-corrected RH of gnssrefl's first spline, rebuilt
# with the evaluator's first_spline (checked against the real subdaily in
# RealSubdailyEvaluateTests), so the evaluator's check of it passes;
# without scipy, the raw RH.
FAKE_SUBDAILY = r'''#!/usr/bin/env python3
import glob, os, sys
from datetime import date, datetime, timedelta
import numpy as np
a = sys.argv[1:]
sta = a[0]
opt = {a[i]: a[i + 1] for i in range(2, len(a) - 1) if a[i].startswith("-")}
refl, ext = os.environ["REFL_CODE"], opt["-extension"]
with open(os.environ["FAKE_SUBDAILY_LOG"], "a") as f:
    f.write(" ".join(a) + "\n")
lo, hi = float(opt.get("-azim1", 0)), float(opt.get("-azim2", 360))
rows = []
for p in glob.glob(f"{refl}/*/results/{sta}/{ext}/*.txt"):
    for s in open(p):
        if s.startswith("%") or not s.strip():
            continue
        c = [float(x) for x in s.split()]
        if lo <= c[5] <= hi:
            rows.append(c)
rows.sort(key=lambda c: c[15])
m = np.array([c[15] for c in rows])
lv = 19.0 - np.array([c[2] for c in rows])
keep = np.ones(len(m), bool)
if "-spline_outlier1" in opt:
    lim = float(opt["-spline_outlier1"])
    for i in range(len(m)):
        near = np.abs(m - m[i]) <= 1 / 24
        keep[i] = abs(lv[i] - np.median(lv[near])) <= lim
out = os.path.join(refl, "Files", opt["-subdir"], ext)
os.makedirs(out, exist_ok=True)
y1 = int(a[1])
def put(path, sel, extra):
    with open(path, "w") as f:
        f.write("% fake\n")
        for c in (r for r, k in zip(rows, sel) if k):
            f.write(" ".join(f"{x:.6f}" for x in c + [0.0] * 5 + extra(c)) + "\n")
for y in sorted({int(c[0]) for c in rows}):
    put(f"{out}/{sta}_{y}_subdaily_edit.txt", [int(c[0]) == y for c in rows], lambda c: [])
corrected, key = {}, lambda c: None
try:
    sys.path.insert(0, os.environ["FAKE_SUBDAILY_TOOLS"])
    import evaluate_qc_variants as ev
    got = ev.first_spline(sorted(glob.glob(f"{out}/{sta}_[0-9][0-9][0-9][0-9]_subdaily_edit.txt")),
                          int(opt.get("-knots", 8)))
    corrected, key = {ev._arc_key(c): x for c, x in zip(got[0], got[1])}, ev._arc_key
except ImportError:
    pass
put(f"{out}/{sta}_{y1}_subdaily_edit.txt.withrhdot", keep,
    lambda c: [corrected.get(key(c), c[2]), c[2] - corrected.get(key(c), c[2])])
put(f"{out}/{sta}_{y1}_subdaily_edit.txt.withrhdotIF", keep, lambda c: [c[2], 0.0, c[2]])
with open(f"{out}/{sta}_spline_out.txt", "w") as f:
    f.write("% fake spline\n%  orthometric height minus RH, where Hortho (m) is     19.000\n")
    t = np.floor(m.min())
    while t < np.ceil(m.max()):
        near = keep & (np.abs(m - t) <= 0.75 / 24)
        if near.any():
            level = float(np.mean(lv[near]))
            dt = datetime(1858, 11, 17) + timedelta(days=float(t))
            f.write(f"{t:15.7f} {19.0 - level:11.3f} {dt.year:4d} {dt.month:3d} {dt.day:3d} "
                    f"{dt.hour:3d} {dt.minute:3d} {0:3d} {level:10.3f}\n")
        t += 1 / 48
'''


class AmplitudeThresholdTests(unittest.TestCase):

    REQ = {1: 5.0, 20: 5.0}

    def test_failqc_arc_rejected_on_amplitude_alone(self):
        def c(**kw):
            return at.parse(line(61300.5, **kw))
        self.assertTrue(at.failed_only_on_amplitude(c(amp=4.0), CFG, self.REQ))
        self.assertTrue(at.failed_only_on_amplitude(c(amp=5.0), CFG, self.REQ),
                        "gnssir rejects amp <= reqAmp")
        for why, kw in (("also fails peak2noise", dict(amp=4.0, pkn=2.5)),
                        ("peak at the RH limit (tooclose)", dict(amp=4.0, rh=14.05)),
                        ("failed on something else", dict(amp=6.0, pkn=2.5)),
                        ("frequency not analysed", dict(amp=4.0, freq=5))):
            with self.subTest(why):
                self.assertFalse(at.failed_only_on_amplitude(c(**kw), CFG, self.REQ))
        nan = at.parse(line(61300.5, amp=4.0).replace("  4.00 ", "   nan ", 1))
        self.assertTrue(math.isnan(nan[at.AMP_COL]))
        self.assertFalse(at.failed_only_on_amplitude(nan, CFG, self.REQ))

    def arcs(self):
        kept = [line(61300.40, amp=9.0), line(61300.10, amp=5.0), line(61300.20, amp=5.5),
                line(61300.30, amp=7.0, freq=20)]
        failed = [line(61300.25, amp=4.5), line(61300.35, amp=3.0, freq=20)]
        return ([(s, at.parse(s), False) for s in kept]
                + [(s, at.parse(s), True) for s in failed])

    def mjds(self, lines):
        return [round(at.parse(s)[at.MJD_COL], 2) for s in lines]

    def test_select_day_is_what_gnssir_would_keep(self):
        cur = self.REQ
        # unchanged: what gnssir kept (the 5.00 printed for a 5.00x arc too), in time order
        self.assertEqual(self.mjds(at.select_day(self.arcs(), {}, cur)),
                         [61300.1, 61300.2, 61300.3, 61300.4])
        self.assertEqual(self.mjds(at.select_day(self.arcs(), {1: 5.0}, cur)),
                         [61300.1, 61300.2, 61300.3, 61300.4])
        # raised for L1: its weak arcs go, L2C is untouched
        self.assertEqual(self.mjds(at.select_day(self.arcs(), {1: 6.0}, cur)),
                         [61300.3, 61300.4])
        # lowered for L1: its amplitude-only failure above the new value comes back
        self.assertEqual(self.mjds(at.select_day(self.arcs(), {1: 4.0}, cur)),
                         [61300.1, 61300.2, 61300.25, 61300.3, 61300.4])
        self.assertEqual(self.mjds(at.select_day(self.arcs(), {1: 4.0, 20: 3.5}, cur)),
                         [61300.1, 61300.2, 61300.25, 61300.3, 61300.4])

    def test_median_rule_keeps_todays_overall_strictness(self):
        amps = {1: {"kept": [10.0] * 40 + [20.0] * 40, "failqc": []},
                20: {"kept": [6.0] * 50, "failqc": [4.0] * 10},
                5: {"kept": [7.0] * 5, "failqc": []}}
        cur = {1: 5.0, 20: 5.0, 5: 5.0}
        r = at.derive(amps, cur)
        self.assertAlmostEqual(r["pooled_median"], 10.0)
        self.assertAlmostEqual(r["fraction"], 0.5)          # 5 / 10
        self.assertFalse(r["fraction_given"])
        self.assertEqual(r["thresholds"], {1: 7.5, 20: 3.0}, "5 has too few arcs: not listed")
        rows = {x["freq"]: x for x in r["table"]}
        self.assertEqual(rows[5]["proposed"], 5.0)
        self.assertEqual((rows[20]["kept_now"], rows[20]["kept_proposed"]), (50, 60))
        self.assertEqual((rows[1]["kept_now"], rows[1]["kept_proposed"]), (80, 80))
        r = at.derive(amps, cur, fraction=0.9)
        self.assertTrue(r["fraction_given"])
        self.assertEqual(r["thresholds"], {1: 13.5, 20: 5.4})
        self.assertEqual(at.station_json_entry(r["thresholds"]),
                         {"gnssrefl_amplitude_min_by_frequency": {"1": 13.5, "20": 5.4}})

    def test_histogram_reads_results_and_failqc_in_the_azimuth_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            refl = Path(tmp)
            build_station(refl, date(2026, 9, 1), date(2026, 9, 3), (date(2026, 9, 25),) * 2)
            days = at.window_days(refl, STA, date(2026, 9, 1), date(2026, 9, 2))
            self.assertEqual(days, [date(2026, 9, 1), date(2026, 9, 2)])
            every = at.amplitudes(refl, STA, days, CFG, None)
            inside = at.amplitudes(refl, STA, days, CFG, (35, 125))
            self.assertGreater(len(every[20]["failqc"]), 0, "weak L2C arcs below 5 are found")
            for f in (1, 20):
                self.assertLess(len(inside[f]["kept"]), len(every[f]["kept"]))
                self.assertTrue(all(a <= 5.0 for a in every[f]["failqc"]))
            no_fail = at.amplitudes(refl, STA, days, CFG, None, failqc=False)
            self.assertEqual(no_fail[20]["failqc"], [])


class EvaluateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.refl = self.tmp / "refl_code"
        self.work = self.tmp / "work"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        # this interpreter, so the stand-in has the same numpy/scipy as the test
        (bindir / "subdaily").write_text(f"#!{sys.executable}\n" + FAKE_SUBDAILY.split("\n", 1)[1])
        (bindir / "subdaily").chmod(0o755)
        self.calls = self.tmp / "subdaily_calls.txt"
        self.env = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SUBDAILY_LOG": str(self.calls),
            "FAKE_SUBDAILY_TOOLS": str(ROOT / "analysis_tools")})
        self.env.start()
        # no network in tests: the gauge and buoy come from the caches
        self.net = unittest.mock.patch.object(wq.urllib.request, "urlopen",
                                              side_effect=OSError("offline (test)"))
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        shutil.rmtree(self.tmp)

    # ---- pieces ---------------------------------------------------------

    def test_settings_carry_the_variants_arc_qc_on_the_command_line(self):
        last_night = SETTINGS + " -alt_sigma True -spline_outlier1 0.5 -spline_outlier2 0.45"
        self.assertEqual(ev.strip_options(last_night), SETTINGS)
        opted = dict(CFG, subdaily_alt_sigma=True, subdaily_spline_outlier1=0.5,
                     subdaily_spline_outlier2=0.45)
        base = ev.Variant("baseline", "")
        self.assertEqual(ev.variant_settings(SETTINGS, base, opted), last_night,
                         "the baseline runs exactly what the nightly fit ran")
        off = ev.Variant("x", "", alt_sigma=False, outlier1=0.3)
        self.assertEqual(ev.variant_settings(SETTINGS, off, opted),
                         SETTINGS + " -spline_outlier1 0.3 -spline_outlier2 0.45",
                         "alt_sigma false is subdaily's default: nothing to add")
        self.assertEqual(ev.variant_settings(SETTINGS, base, CFG), SETTINGS)
        amp = ev.Variant("a", "", amp={20: 4.0})
        self.assertEqual(ev.variant_json(amp, CFG)["reqAmp"], [5.0, 4.0])
        self.assertEqual(amp.station_json(),
                         {"gnssrefl_amplitude_min_by_frequency": {"20": 4.0}})
        self.assertEqual(off.station_json(), {"gnssrefl_subdaily_alt_sigma": False,
                                              "gnssrefl_subdaily_spline_outlier1_m": 0.3})

    def test_staging_links_the_results_or_writes_what_gnssir_would_keep(self):
        build_station(self.refl, date(2026, 9, 1), date(2026, 9, 2), (date(2026, 9, 25),) * 2)
        days = [date(2026, 9, 1), date(2026, 9, 2)]
        ev.stage(self.refl, self.work, STA, ev.Variant("baseline", ""), days, CFG)
        staged = self.work / "2026" / "results" / STA / "qc_baseline" / "244.txt"
        src = self.refl / "2026" / "results" / STA / "244.txt"
        self.assertEqual(staged.read_bytes(), src.read_bytes())
        self.assertEqual(json.loads((self.work / "input" / STA / "qc_baseline" / f"{STA}.json")
                                    .read_text()), CFG)
        strict = ev.Variant("strict", "", amp={1: 12.0}, alt_sigma=True)
        ev.stage(self.refl, self.work, STA, strict, days, CFG)
        out = (self.work / "2026" / "results" / STA / "qc_strict" / "244.txt").read_text()
        kept = [at.parse(s) for s in out.splitlines(True) if at.parse(s)]
        self.assertTrue(out.startswith("%"))
        self.assertTrue(all(c[at.AMP_COL] > 12.0 for c in kept if c[at.FREQ_COL] == 1))
        self.assertEqual(sum(1 for c in kept if c[at.FREQ_COL] == 20),
                         sum(1 for s in src.read_text().splitlines()
                             if at.parse(s) and at.parse(s)[at.FREQ_COL] == 20))
        self.assertEqual([c[at.MJD_COL] for c in kept], sorted(c[at.MJD_COL] for c in kept))
        vj = json.loads((self.work / "input" / STA / "qc_strict" / f"{STA}.json").read_text())
        self.assertEqual((vj["subdaily_alt_sigma"], vj["reqAmp"]), (True, [12.0, 5.0]))
        self.assertTrue(src.exists(), "production results are never touched")

    def test_scores_and_the_rule(self):
        ep = ev.epoch(date(2026, 9, 25)) + 1800.0 * np.arange(144)
        grid = np.arange(ep[0] - 7200, ep[-1] + 7200, 360.0)     # a 6-minute gauge
        ref = (grid, np.zeros(len(grid)))
        lv = np.zeros(len(ep))
        lv[10:20] = 0.5
        keep = np.ones(len(ep), bool)
        keep[50:60] = False                                    # a 5.5-hour hole
        s = ev.score_window(ep[keep], lv[keep], ref, (1.0, 0.0, 0.0, 0.03), None,
                            (date(2026, 9, 25), date(2026, 9, 27)))
        self.assertEqual((s["readings"], s["slots"]), (134, 144))
        self.assertAlmostEqual(s["longest_gap_h"], 5.5)
        self.assertAlmostEqual(s["bias"], 5.0 / 134)
        self.assertAlmostEqual(s["beyond_setup_rms"], s["rmse"], msg="no waves: nothing explained")

        def result(calm_rmse=0.05, arcs=1000, readings=144, beyond=0.30, gap=0.5):
            return {"ok": True, "calm": {"rmse": calm_rmse, "readings": 336, "longest_gap_h": 0.5},
                    "storm": {"readings": readings, "beyond_setup_rms": beyond,
                              "high_water_bias": 1.0, "longest_gap_h": gap},
                    "arcs": {"calm": {"stage1": arcs}, "storm": {"stage1": 300}}}
        results = {"baseline": result(),
                   "same": result(),
                   "costly": result(calm_rmse=0.06, beyond=0.10),
                   "greedy": result(arcs=850, beyond=0.10),
                   "gappy": result(readings=120, beyond=0.10),
                   "broken_line": result(beyond=0.10, gap=6.5),
                   "good": result(calm_rmse=0.054, arcs=950, beyond=0.20),
                   "better": result(beyond=0.15),
                   "broken": {"ok": False}}
        best, why = ev.recommend(results)
        self.assertEqual(best, "better")
        self.assertEqual(why["good"], [])
        self.assertIn("10% lower", why["same"][0])
        self.assertIn("calm RMSE", why["costly"][0])
        self.assertIn("calm arcs", why["greedy"][0])
        self.assertIn("new gaps", why["gappy"][0])
        self.assertEqual(len(why["broken_line"]), 1)
        self.assertIn("6.5-hour gap in the storm window", why["broken_line"][0])
        self.assertEqual(why["broken"], ["subdaily wrote no spline"])
        self.assertEqual(ev.recommend({"baseline": result(), "same": result()})[0], None)

    def test_calm_week_is_the_calmest_away_from_the_storm(self):
        days = [date(2026, 9, 1) + timedelta(days=k) for k in range(40)]
        storm = (date(2026, 9, 25), date(2026, 9, 27))
        t = np.arange(ev.epoch(date(2026, 9, 1)), ev.epoch(date(2026, 10, 11)), 3600.0)
        hs = np.full(len(t), 1.5)
        quiet = (t >= ev.epoch(date(2026, 9, 10))) & (t < ev.epoch(date(2026, 9, 17)))
        hs[quiet] = 0.5
        hs[(t >= ev.epoch(date(2026, 9, 22))) & (t < ev.epoch(date(2026, 9, 25)))] = 0.1  # too near
        w, why = ev.pick_calm(days, storm, (t, hs, np.full(len(t), 8.0)))
        self.assertEqual(w, (date(2026, 9, 10), date(2026, 9, 16)))
        self.assertIn("calmest", why)
        w, why = ev.pick_calm(days, storm, None)
        self.assertEqual(w, (date(2026, 9, 15), date(2026, 9, 21)))
        self.assertIn("check they were calm", why)

    def test_wave_setup_explains_a_reading_only_up_to_the_allowance(self):
        storm = (date(2026, 9, 25), date(2026, 9, 25))
        ep = ev.epoch(storm[0]) + 1800.0 * np.arange(48)
        grid = np.arange(ep[0] - 7200, ep[-1] + 7200, 360.0)
        ref = (grid, np.zeros(len(grid)))
        t = np.arange(ep[0] - 7200, ep[-1] + 7200, 3600.0)
        waves = (t, np.full(len(t), 3.0), np.full(len(t), 10.0))
        setup = wq.setup_allowance(ep, waves)
        self.assertTrue(np.all(setup > 0.1), setup)
        lv = np.where(np.arange(48) < 24, 0.5 * setup, setup + 0.2)    # half within, half not
        s = ev.score_window(ep, lv, ref, (1.0, 0.0, 0.0, 0.03), waves, storm)
        self.assertAlmostEqual(s["beyond_setup_rms"], math.sqrt(0.5 * 0.2 ** 2), places=6)
        self.assertGreater(s["rmse"], s["beyond_setup_rms"])
        s = ev.score_window(ep, -lv, ref, (1.0, 0.0, 0.0, 0.03), waves, storm)
        self.assertAlmostEqual(s["beyond_setup_rms"], s["rmse"], msg="setup never lowers a level")

    def test_the_reference_carries_its_lag_and_high_water_has_its_own_bias(self):
        w = (date(2026, 9, 10), date(2026, 9, 16))
        ep = ev.epoch(w[0]) + 1800.0 * np.arange(7 * 48)
        grid = np.arange(ep[0] - 86400, ep[-1] + 86400, 360.0)
        ref = (grid, truth(grid / 86400 + ev.MJD_UNIX))
        lv = truth((ep - 1800.0) / 86400 + ev.MJD_UNIX)                # 30 minutes behind
        fit = wq.fit_reference(ep, lv, ref[0], ref[1], np.ones(len(ep), bool))
        self.assertAlmostEqual(fit[1], 1800.0, delta=360.0)
        s = ev.score_window(ep, lv, ref, fit, None, w)
        self.assertLess(s["rmse"], 0.02, "the lag is applied to the reference")
        unlagged = ev.score_window(ep, lv, ref, (fit[0], 0.0, fit[2], fit[3]), None, w)
        self.assertGreater(unlagged["rmse"], 5 * s["rmse"])
        pred = wq.reference_at(ref[0], ref[1], ep)             # as score_window reads it
        high = pred >= np.percentile(pred, 75)
        s = ev.score_window(ep, pred + np.where(high, 0.2, 0.0), ref, (1.0, 0.0, 0.0, 0.03),
                            None, w)
        self.assertAlmostEqual(s["high_water_bias"], 0.2, places=6)
        self.assertAlmostEqual(s["bias"], 0.2 * high.mean(), places=6)

    def test_second_clip_scale_is_of_the_corrected_rh_about_the_spline(self):
        v = ev.Variant("baseline", "")
        out = ev.out_dir(self.work, STA, v)
        out.mkdir(parents=True)
        w = (date(2026, 9, 10), date(2026, 9, 10))
        m0 = (w[0] - MJD0).days
        with open(out / f"{STA}_spline_out.txt", "w") as f:
            f.write("% spline\n")
            for t in m0 - 0.5 + np.arange(96) / 48:
                f.write(f"{t:15.7f} {19.0:11.3f} 2026 9 10 0 0 0 {0.0:10.3f}\n")
        rng = np.random.default_rng(1)
        r = rng.normal(0, 0.05, 200)
        with open(out / f"{STA}_2026_subdaily_edit.txt.withrhdotIF", "w") as f:
            for i, t in enumerate(m0 + np.linspace(0.01, 0.99, 200)):
                c = at.parse(line(t, rh=25.0))                  # raw RH: far off
                f.write(" ".join(f"{x:.6f}" for x in c + [0.0] * 5 + [25.0, 0.0, 19.0 + r[i]]) + "\n")
        got = ev.arc_scatter(self.work, STA, v, w)
        self.assertAlmostEqual(got, 1.4826 * np.median(np.abs(r)), places=3)

    def test_amp_json_with_no_thresholds_is_a_note_not_a_crash(self):
        empty, entry, bare, bad = (self.tmp / n for n in ("e.json", "s.json", "b.json", "x.json"))
        empty.write_text(json.dumps({"fraction": 0.5, "thresholds": {}, "station_json": {
            "gnssrefl_amplitude_min_by_frequency": {}}, "window": ["2026-09-08", "2026-09-14"]}))
        entry.write_text(json.dumps({"gnssrefl_amplitude_min_by_frequency": {"20": 4.5}}))
        bare.write_text(json.dumps({"1": 7, "20": "4.5"}))
        bad.write_text(json.dumps({"fraction": 0.5}))
        self.assertEqual(ev.read_amp_json(empty), {})
        self.assertEqual(ev.read_amp_json(entry), {20: 4.5})
        self.assertEqual(ev.read_amp_json(bare), {1: 7.0, 20: 4.5})
        with self.assertRaises(ValueError):
            ev.read_amp_json(bad)
        build_station(self.refl, date(2026, 9, 2), date(2026, 10, 1),
                      (date(2026, 9, 25), date(2026, 9, 27)))
        rc, out = self.run_main("--span", "windows", "--variants", "amp_by_freq,amp_half",
                                "--amp-json", str(empty))
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: amp_by_freq not run", out)
        self.assertIn("lists no threshold", out)
        self.assertIn("amp_half", json.loads((self.work / "qc_eval_summary.json").read_text())
                      ["variants"])
        # a file it cannot use stops it before the first (minutes-long) fit
        self.calls.unlink()
        rc, out = self.run_main("--span", "windows", "--amp-json", str(bad))
        self.assertEqual(rc, 1)
        self.assertIn("--amp-json", out)
        self.assertFalse(self.calls.exists(), "no subdaily run")

    def test_production_span_is_the_fit_that_wrote_the_windows(self):
        files = self.refl / "Files" / STA
        (files / gr.FIT_SUBDIR).mkdir(parents=True)
        days = {date(2025, 12, 1) + timedelta(days=k): None for k in range(60)}
        plan = gr.Plan(days, max(days), [2025], 2026, date(2025, 12, 25), max(days))
        head = ["% gnssrefl spline\n"]
        rows = [f"{(date(2025, 12, 10) - MJD0).days:15.7f} 19.0\n"]
        gr.write_atomic(gr.frozen_path(files, STA, 2025), gr.frozen_text(
            head, SETTINGS, "fp", rows, "2025 frozen 2026-01-15 03:00 UTC from the fit of "
                                        "2025-12-01..2026-01-15"))
        calm, storm = (date(2025, 12, 10), date(2025, 12, 16)), (date(2026, 1, 5), date(2026, 1, 7))
        self.assertEqual(ev.record_fit(self.refl, STA, plan, calm)[0],
                         (date(2025, 12, 1), date(2026, 1, 15)), "from the frozen header")
        self.assertEqual(ev.record_fit(self.refl, STA, plan, storm),
                         ((plan.start, plan.end), "the span tonight's fit would cover"))
        status = files / gr.FIT_SUBDIR / gr.STATUS_NAME
        status.write_text(json.dumps({"ok": True, "fallback": False, "fit_start": "2025-12-01",
                                      "fit_end": "2026-01-20"}))
        self.assertEqual(ev.record_fit(self.refl, STA, plan, storm)[0],
                         (date(2025, 12, 1), date(2026, 1, 20)),
                         "the day after a freeze: the fit that wrote the record, not tonight's")
        status.write_text(json.dumps({"ok": True, "fallback": True, "fit_start": "2025-12-01",
                                      "fit_end": "2026-01-20"}))
        self.assertEqual(ev.record_fit(self.refl, STA, plan, storm)[0], (plan.start, plan.end))
        gr.write_atomic(gr.frozen_path(files, STA, 2025), gr.frozen_text(
            head, SETTINGS, "fp", rows, "2025 frozen 2026-01-15 03:00 UTC from a fit of 2025 by "
                                        "itself with '-rhdot True' (3 arc(s) in the azimuth window)"))
        self.assertEqual(ev.record_fit(self.refl, STA, plan, calm)[0],
                         (date(2025, 12, 1), date(2025, 12, 31)))
        fits = [(date(2025, 12, 1), date(2026, 1, 15)), (date(2025, 12, 1), date(2026, 1, 20))]
        self.assertEqual(ev.eval_span("production", plan, (calm, storm), max(days), fits),
                         (date(2025, 12, 1), date(2026, 1, 20)))
        late = (date(2026, 1, 25), date(2026, 1, 27))
        self.assertEqual(ev.eval_span("production", plan, (calm, late), max(days), fits[:1]),
                         (date(2025, 12, 1), date(2026, 1, 27)), "widened to the windows")
        self.assertEqual(ev.eval_span("windows", None, (calm, storm), max(days)),
                         (date(2025, 12, 7), date(2026, 1, 10)))

    # ---- a whole run ----------------------------------------------------

    def run_main(self, *args):
        argv = ["evaluate_qc_variants.py", "--refl-code", str(self.refl), "--station", STA,
                "--work", str(self.work), "--settings", SETTINGS, *args]
        out = io.StringIO()
        # whatever station.json this machine has stays out of it
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                unittest.mock.patch.object(gr, "record_first_day", return_value=None), \
                unittest.mock.patch.object(ev, "spline_shift", return_value=0.0):
            rc = ev.main()
        return rc, out.getvalue()

    def test_whole_run_scores_every_variant_and_recommends_by_the_rule(self):
        build_station(self.refl, date(2026, 9, 2), date(2026, 10, 1),
                      (date(2026, 9, 25), date(2026, 9, 27)))
        before = sorted(p.relative_to(self.refl) for p in self.refl.rglob("*"))
        rc, out = self.run_main("--span", "windows")
        self.assertEqual(rc, 0, out)
        summary = json.loads((self.work / "qc_eval_summary.json").read_text())
        names = [n for n in ev.VARIANT_NAMES
                 if HAVE_SCIPY or n not in ("abs_outliers", "combined")]
        self.assertEqual(sorted(summary["variants"]), sorted(names))
        if HAVE_SCIPY:
            self.assertIn("first spline rebuilt", summary["first_spline_check"])
            s1, s2 = summary["calm_scale_m"]["first_clip"], summary["calm_scale_m"]["second_clip"]
            self.assertEqual(summary["variants"]["abs_outliers"]["station_json"],
                             {"gnssrefl_subdaily_spline_outlier1_m": round(3 * s1, 2),
                              "gnssrefl_subdaily_spline_outlier2_m": round(3 * s2, 2)})
        else:
            self.assertIn("NOTE: abs_outliers not run", out)
            self.assertIn("scipy is not installed", out)
        self.assertTrue(all(v["ok"] for v in summary["variants"].values()), out)
        self.assertEqual(summary["storm"], ["2026-09-25", "2026-09-27"])
        cal = summary["calm"]
        self.assertTrue(date.fromisoformat(cal[1]) < date(2026, 9, 22)
                        or date.fromisoformat(cal[0]) > date(2026, 9, 30), cal)
        self.assertIn("calmest", summary["calm_choice"])
        self.assertEqual(summary["reference"], f"Chatham gauge {wq.GAUGE_STATION}")
        base = summary["variants"]["baseline"]
        self.assertGreater(base["storm"]["beyond_setup_rms"], 0.3, "the storm bias is there")
        self.assertLess(base["calm"]["rmse"], 0.1)
        self.assertGreater(base["storm"]["high_water_bias"], 0.3)
        # alt_sigma changes nothing in the stand-in: it must not be recommended
        self.assertIn("10% lower", summary["reasons"]["alt_sigma"][0])
        # the damped, biased arcs are what the amplitude thresholds (and the
        # stand-in's clip) remove: the recommendation is the rule's own
        best, reasons = ev.recommend(summary["variants"])
        self.assertEqual((summary["recommended"], summary["reasons"]), (best, reasons))
        self.assertIsNotNone(best, out)
        self.assertLess(summary["variants"][best]["storm"]["beyond_setup_rms"],
                        0.9 * base["storm"]["beyond_setup_rms"])
        self.assertIn("RECOMMENDATION", out)
        self.assertIn(f"-> {best}:", out)
        for key in summary["variants"][best]["station_json"]:
            self.assertIn(json.dumps(key), out)
        # each variant ran with its QC on subdaily's command line, in its own extension
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), len(names))
        self.assertTrue(any("-extension qc_alt_sigma " in c and c.endswith(SETTINGS + " -alt_sigma True")
                            for c in calls), calls)
        self.assertTrue(all(" -plt False " in c and "-subdir usgs/eval" in c for c in calls))
        # the production REFL_CODE is only read
        self.assertEqual(sorted(p.relative_to(self.refl) for p in self.refl.rglob("*")), before)

    def test_production_span_is_planned_as_the_nightly_fit_plans_it(self):
        # arc QC already on in production: the nightly fit plans (and checks
        # its frozen years) with the json's options on the command line, so
        # the evaluation must too, or its span reaches back past them
        build_station(self.refl, date(2026, 9, 2), date(2026, 10, 1),
                      (date(2026, 9, 25), date(2026, 9, 27)))
        p = self.refl / "input" / STA / f"{STA}.json"
        p.write_text(json.dumps(dict(CFG, subdaily_alt_sigma=True), indent=4))
        with unittest.mock.patch.object(gr, "make_plan", wraps=gr.make_plan) as plan:
            rc, out = self.run_main("--variants", "baseline")
        self.assertEqual(rc, 0, out)
        self.assertEqual(plan.call_args[0][2], SETTINGS + " -alt_sigma True")
        self.assertEqual(plan.call_args[0][2],
                         gr.with_json_qc(SETTINGS, self.refl, STA),
                         "the settings gnss_record.cmd_update plans with")

    def test_without_gauge_or_tide_model_nothing_is_scored(self):
        build_station(self.refl, date(2026, 9, 2), date(2026, 10, 1),
                      (date(2026, 9, 25), date(2026, 9, 27)))
        (self.refl / "Files" / STA / f"gauge_{wq.GAUGE_STATION}.csv").unlink()
        with unittest.mock.patch.object(ev, "load_station", return_value={}):
            rc, out = self.run_main("--span", "windows", "--variants", "baseline")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to score against", out)

    def test_missing_subdaily_is_said_plainly(self):
        build_station(self.refl, date(2026, 9, 2), date(2026, 9, 5), (date(2026, 9, 3),) * 2)
        with unittest.mock.patch.object(ev.shutil, "which", return_value=None):
            rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("subdaily is not on PATH", out)


def write_edit_file(path: Path, mjds, rh, edotf, sat=None, freq=None) -> None:
    """A part-1 edit file as subdaily writes it (22 columns)."""
    with open(path, "w") as f:
        f.write("% edit file\n")
        for i, m in enumerate(mjds):
            d = MJD0 + timedelta(days=int(m))
            c = [d.year, d.timetuple().tm_yday, rh[i], 1 + i % 32 if sat is None else sat[i],
                 (m % 1) * 24, 80.0, 10.0, 5.05, 14.95, 200, 1 if freq is None else freq[i], 1,
                 edotf[i], 4.0, 30.0, m, 1, d.month, d.day, 0, 0, 0]
            f.write(" ".join(f"{x:.6f}" for x in c) + "\n")


class FirstSplineTests(unittest.TestCase):
    """gnssrefl's first spline, rebuilt (evaluate_qc_variants.first_spline):
    the residual the first spline clip tests."""

    def test_flipit3_mirrors_the_ends_and_bridges_gaps(self):
        t = np.array([10.0, 10.1, 10.2, 11.2, 11.3, 11.4])        # a 24-hour gap after 10.2
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        tn, yn = ev.flipit3(t, y)
        self.assertTrue(np.all(np.diff(tn) >= 0))
        # the first day mirrored before t0 (at 2 t0 - t; the earliest, 9.8, is
        # not kept), then the series itself from t0
        np.testing.assert_allclose(tn[:5], [9.9, 10.0, 10.0, 10.1, 10.2])
        np.testing.assert_allclose(yn[:5], [2.0, 1.0, 1.0, 2.0, 3.0])
        # the last day after the end at t + 1, in reverse order (6 5 4 at
        # 12.2 12.3 12.4; 12.2 ends a gap, so only the bridge reaches it)
        np.testing.assert_allclose(tn[-2:], [12.3, 12.4])
        np.testing.assert_allclose(yn[-2:], [5.0, 4.0])
        # the gap 10.2..11.2 bridged hourly on the straight line; 11.2 itself
        # (the point that ends the gap) not kept
        bridge = (tn > 10.2 + 1e-9) & (tn < 11.3 - 1e-9)
        self.assertGreaterEqual(int(bridge.sum()), 23)
        np.testing.assert_allclose(yn[bridge], 3.0 + (tn[bridge] - 10.2), atol=1e-9)
        self.assertFalse(np.any(np.isclose(tn, 11.2, atol=1e-6) & np.isclose(yn, 4.0)
                                & ~bridge))
        if importlib.util.find_spec("gnssrefl") is not None:
            from gnssrefl.sd_libs import flipit3
            tvd = np.zeros((len(t), 17))
            tvd[:, 15], tvd[:, 2] = t, y
            with contextlib.redirect_stdout(io.StringIO()):
                gt, gy = flipit3(tvd, 3)
            np.testing.assert_array_equal(tn, gt)
            np.testing.assert_array_equal(yn, gy)

    @unittest.skipUnless(HAVE_SCIPY, "scipy not installed")
    def test_first_clip_scale_and_the_check_against_withrhdot(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        work = tmp / "work"
        v = ev.Variant("baseline", "")
        out = ev.out_dir(work, STA, v)
        out.mkdir(parents=True)
        rng = np.random.default_rng(5)
        m = np.sort(61290 + rng.uniform(0, 30, 30 * 96))
        noise = rng.normal(0, 0.05, len(m))
        edotf = rng.choice([-1, 1], len(m)) * rng.uniform(0.5, 0.9, len(m))
        drh = -(truth(m + 1e-4) - truth(m - 1e-4)) / 2e-4 / 24      # RH rate, m per hour
        rh = H - truth(m) + noise + edotf * drh                    # what an arc reads
        sat, freq = rng.integers(1, 33, len(m)), rng.choice([1, 20], len(m))
        write_edit_file(out / f"{STA}_2026_subdaily_edit.txt", m, rh, edotf, sat, freq)
        arcs, corrected, residual = ev.first_spline([out / f"{STA}_2026_subdaily_edit.txt"], 8)
        self.assertEqual(len(arcs), len(m))
        # the RHdot correction takes out most of what the rising/setting
        # geometry put in (0.24 m typical here) ...
        level = H - truth(arcs[:, 15])
        self.assertLess(ev.robust_scale(corrected - level), 1.3 * ev.robust_scale(noise))
        self.assertGreater(ev.robust_scale(arcs[:, 2] - level), 3 * ev.robust_scale(noise))
        # ... but the first clip's residual is about a spline fitted to the
        # UNCORRECTED RH, so it is wider than the arcs' own noise
        w = (date(2026, 9, 10), date(2026, 9, 16))                 # MJD 61293..61299
        sel = (m >= 61293) & (m < 61300)
        self.assertGreater(ev.robust_scale(residual[sel]), 1.2 * ev.robust_scale(noise[sel]))
        self.assertLess(ev.robust_scale(residual[sel]), 2.5 * ev.robust_scale(noise[sel]))

        def withrhdot(shift):
            with open(out / f"{STA}_2026_subdaily_edit.txt.withrhdot", "w") as f:
                f.write("% kept arcs\n")
                for c, x in zip(arcs, corrected):
                    x = round(x, 3) + shift
                    f.write(" ".join(f"{y:.6f}" for y in list(c) + [x, c[2] - x]) + "\n")

        withrhdot(0.0)
        scale, why = ev.first_clip_scatter(work, STA, v, w, SETTINGS, {})
        self.assertAlmostEqual(scale, ev.robust_scale(residual[sel]))
        self.assertIn("first spline rebuilt", why)
        withrhdot(0.01)                   # a gnssrefl that corrects otherwise
        scale, why = ev.first_clip_scatter(work, STA, v, w, SETTINGS, {})
        self.assertTrue(math.isnan(scale))
        self.assertIn("does not reproduce", why)
        scale, why = ev.first_clip_scatter(work, STA, v, w, "-rhdot False -knots 8", {})
        self.assertTrue(math.isnan(scale))
        self.assertIn("-rhdot False", why)

    def test_abs_outliers_take_each_limit_from_its_own_clip(self):
        vs = ev.build_variants({"abs_outliers"}, (0.123, 0.087), None, None)
        self.assertEqual([v.name for v in vs], ["baseline", "abs_outliers"])
        self.assertEqual((vs[1].outlier1, vs[1].outlier2), (0.37, 0.26))
        self.assertEqual([v.name for v in ev.build_variants({"abs_outliers"},
                                                            (float("nan"), 0.087), None, None)],
                         ["baseline"], "no first-clip scale: not derived")
        given = ev.build_variants({"abs_outliers"}, (float("nan"), 0.087), (0.5, 0.4), None)
        self.assertEqual((given[1].outlier1, given[1].outlier2), (0.5, 0.4))
        fixed = ev.build_variants({"fixed_outliers"}, (0.1, 0.1), None, None)[1]
        self.assertEqual((fixed.outlier1, fixed.outlier2), ev.FIXED_OUTLIERS)
        self.assertNotIn("review", fixed.about)


def _gnssrefl_installed() -> bool:
    return shutil.which("subdaily") is not None and importlib.util.find_spec("gnssrefl") is not None


@unittest.skipUnless(_gnssrefl_installed(), "gnssrefl (subdaily) not installed")
class RealSubdailyEvaluateTests(unittest.TestCase):
    """The nightly fit, then the evaluation, with gnssrefl's real subdaily:
    the baseline reproduces the production record, every variant's
    options reach subdaily, and the rebuilt first spline is gnssrefl's."""

    def station(self, first, last, storm):
        """A synthetic station (RHdot in its arcs) after one nightly update."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        refl = tmp / "refl_code"
        env = unittest.mock.patch.dict(os.environ, {"REFL_CODE": str(refl), "MPLBACKEND": "Agg"})
        env.start()
        self.addCleanup(env.stop)
        net = unittest.mock.patch.object(wq.urllib.request, "urlopen",
                                         side_effect=OSError("offline (test)"))
        net.start()
        self.addCleanup(net.stop)
        from gnssrefl.gnssir_input import make_gnssir_input
        with contextlib.redirect_stdout(io.StringIO()):
            make_gnssir_input(STA, lat=41.8915, lon=-69.9618, height=-17.0, Hortho=[H],
                              allfreq=True, e1=5, e2=15, h1=14, h2=24)
        build_station(refl, first, last, storm, rhdot=True, cfg={})
        self.update(refl)
        return tmp, refl

    def update(self, refl):
        with contextlib.redirect_stdout(io.StringIO()), \
                unittest.mock.patch.object(gr, "record_first_day", return_value=None):
            self.assertEqual(gr.cmd_update(refl, STA, SETTINGS, "-rhdot True -knots 8", False), 0)

    def evaluate(self, tmp, refl, *args):
        argv = ["evaluate_qc_variants.py", "--refl-code", str(refl), "--station", STA,
                "--work", str(tmp / "work"), *args]
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                unittest.mock.patch.object(gr, "record_first_day", return_value=None):
            self.assertEqual(ev.main(), 0, out.getvalue())
        return json.loads((tmp / "work" / "qc_eval_summary.json").read_text()), out.getvalue()

    def test_real_subdaily(self):
        tmp, refl = self.station(date(2026, 9, 10), date(2026, 10, 1),
                                 (date(2026, 9, 25), date(2026, 9, 27)))
        summary, out = self.evaluate(tmp, refl, "--variants", "alt_sigma,amp_half",
                                     "--calm", "2026-09-14", "2026-09-20")
        self.assertLess(summary["baseline_vs_record_max_m"], 0.0005, out)
        self.assertTrue(all(v["ok"] for v in summary["variants"].values()), out)
        log = (tmp / "work" / "Files" / STA / "eval" / "qc_alt_sigma" / "subdaily.log").read_text()
        self.assertTrue(log.splitlines()[0].endswith(" -alt_sigma True"))

    def test_abs_outliers_first_clip_drops_what_the_baseline_does_without_a_storm(self):
        # no storm in the span: the whole fit's 3 sigma IS the calm 3 sigma,
        # so abs_outliers' first limit must drop about as many calm arcs
        tmp, refl = self.station(date(2026, 8, 20), date(2026, 10, 10),
                                 (date(2027, 9, 25), date(2027, 9, 27)))
        summary, out = self.evaluate(tmp, refl, "--variants", "abs_outliers",
                                     "--calm", "2026-09-07", "2026-09-13",
                                     "--storm", "2026-09-25", "2026-09-27")
        self.assertIn("first spline rebuilt", summary["first_spline_check"], out)
        self.assertLess(summary["baseline_vs_record_max_m"], 0.0005, out)
        log = (tmp / "work" / "Files" / STA / "eval" / "qc_baseline" / "subdaily.log").read_text()
        whole = float(log.split("RMS w/ RHdot correction (m)")[1].split()[0])
        self.assertAlmostEqual(summary["calm_scale_m"]["first_clip"], whole, delta=0.15 * whole,
                               msg="the calm scale of what the first clip tests")
        loss = {}
        for name in ("baseline", "abs_outliers"):
            arcs = summary["variants"][name]["arcs"]["calm"]
            loss[name] = 1 - arcs["stage1"] / arcs["part1"]
        self.assertLess(abs(loss["abs_outliers"] - loss["baseline"]), 0.01, (loss, out))
        for k in ("calm", "storm"):
            self.assertLess(abs(summary["variants"]["abs_outliers"][k]["rmse"]
                                - summary["variants"]["baseline"][k]["rmse"]), 0.005, out)

    def test_baseline_reproduces_the_record_in_a_frozen_year_and_after_a_freeze(self):
        # the first update freezes 2025 (the fit runs to 20 Jan); the next
        # night's fit would start 25 Dec, but the record was written by the
        # fit from 1 Dec -- and 2025 in it stays that fit's for good
        tmp, refl = self.station(date(2025, 12, 1), date(2026, 1, 20),
                                 (date(2026, 1, 5), date(2026, 1, 7)))
        self.assertTrue(gr.frozen_path(refl / "Files" / STA, STA, 2025).exists())
        for calm, storm in ((("2025-12-10", "2025-12-16"), ("2025-12-20", "2025-12-22")),
                            (("2026-01-11", "2026-01-17"), ("2026-01-05", "2026-01-07"))):
            with self.subTest(calm=calm):
                summary, out = self.evaluate(tmp, refl, "--variants", "baseline",
                                             "--calm", *calm, "--storm", *storm)
                self.assertEqual(summary["span"], ["2025-12-01", "2026-01-20"], out)
                self.assertLess(summary["baseline_vs_record_max_m"], 0.0005, out)
        # a night later the live part is the fit from 25 Dec; 2025 is still
        # the frozen fit's
        self.update(refl)
        summary, out = self.evaluate(tmp, refl, "--variants", "baseline",
                                     "--calm", "2026-01-11", "2026-01-17",
                                     "--storm", "2026-01-05", "2026-01-07")
        self.assertEqual(summary["span"], ["2025-12-25", "2026-01-20"], out)
        self.assertLess(summary["baseline_vs_record_max_m"], 0.0005, out)


if __name__ == "__main__":
    unittest.main()
