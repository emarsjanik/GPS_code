"""
test_evaluate_invsnr.py

analysis_tools/evaluate_invsnr.py runs gnssrefl's inverse modelling
(invsnr) on chosen windows and compares it with the published spline,
the Chatham gauge and the tide model.

  * ComparisonTests -- the comparison on synthetic series with known
    errors: interpolation across gaps, support, the statistics, the
    gauge transfer, sea-state classes, evaluate(), the adoption rule
    and the report.
  * PlanAndApiTests -- the run plan (padding, the year boundary), the
    inv.json keys and invsnr arguments against gnssrefl 4.1.5's
    signature, the output parser, the decimated SNR copies (and when
    they are made again).
  * FakeGnssreflRunTests -- the run step end to end with a stand-in
    gnssrefl package on PYTHONPATH (invsnr_cl.invsnr and
    spline_functions shaped as in 4.1.5, with its exits): the private
    REFL_CODE, live files untouched, the gap exit, a crash, which runs
    are reused, a reprocessed SNR day, and the whole command line.
  * RealInvsnrTests -- gnssrefl's real invsnr on synthetic SNR files
    with a known tide. Skipped when gnssrefl is not importable.

Run with:
    python3 -m unittest discover -s tests -p "test_evaluate_invsnr.py" -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import math
import os
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
import evaluate_invsnr as ev  # noqa: E402

STA = "usgs"
HORTHO = 19.014
M2_S = 12.42 * 3600.0
T_REF = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
GPS_UTC_S = 18                       # GPS - UTC since 2017

# gnssrefl 4.1.5, invsnr_cl.py:53-58
INVSNR_415_PARAMS = (
    "station", "year", "doy", "signal", "peak2noise", "constel", "screenstats", "dec",
    "polydeg", "snrfit", "plt", "doy_end", "lspfigs", "snrfigs", "knot_space", "rough_in",
    "risky", "snr", "outfile_type", "outfile_name", "outlier_limit", "no_dots", "delta_out",
    "refraction", "json_override", "lastday_seconds")


def tide(epochs):
    """The synthetic 'true' Marconi water level, NAVD88 (m)."""
    t = np.asarray(epochs, float) - T_REF
    return 1.3 * np.cos(2 * np.pi * t / M2_S) + 0.25 * np.cos(2 * np.pi * t / 43200.0 + 0.7)


def day0(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def grid_for(days) -> np.ndarray:
    return np.concatenate([day0(d) + np.arange(0, 86400, 1800, dtype=float) for d in days])


def tree_digest(path: Path) -> str:
    """Names and contents of every file under path."""
    h = hashlib.sha1()
    for p in sorted(Path(path).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------
# synthetic SNR files (gnssrefl's snr66 layout)
# ---------------------------------------------------------------------

WAVELENGTH = {1: 0.190293673, 2: 0.244210213, 5: 0.254828049}   # GPS L1/L2/L5, Galileo E1/E5a
# invsnr fits ONE amplitude and phase per constellation and frequency for
# the whole run (spline_functions.py:665), so the synthetic arcs share them
PHASE = {(False, 1): 0.4, (False, 2): 1.3, (False, 5): 2.1, (True, 1): 0.9, (True, 5): 2.8}


def bennett_deg(e_deg, pressure_hpa: float = 1013.0, temp_c: float = 10.0):
    """Bennett's refraction (the formula invsnr applies, refraction.py
    corr_el_angles), degrees: what the atmosphere adds to the geometric
    elevation of the signal."""
    e_deg = np.asarray(e_deg, float)
    arcmin = (510.0 / (9.0 / 5.0 * temp_c + 492.0) * pressure_hpa / 1010.16
              / np.tan(np.radians(e_deg + 7.31 / (e_deg + 4.4))))
    return arcmin / 60.0


def synthetic_snr_lines(d: date, rho: float = 0.002, sample_s: int = 5, seed: int = 0,
                        hortho: float = HORTHO, gap=None):
    """One day of SNR rows: sat, elev, azim, sod, edot, S6, S1, S2, S5, S7, S8.
    GPS 1-32 and Galileo 201-230, two passes each half a day apart, the
    passes spread evenly over the day (one starts every ~12 min) and 80 %
    of them over the sea (azimuth 40-120), elevation 0.5-29.5 deg at
    0.007-0.011 deg/s. The SNR (dB-Hz) is 20 log10 of a direct term that
    is quadratic in sin(e) plus the reflection
    A exp(-4 k^2 rho sin^2 e) cos(4 pi RH sin(e) / lambda + PHASE),
    RH = hortho - tide(t), with noise. The file holds the geometric
    elevation e; the reflection sees it refracted (e + bennett_deg(e)), as
    real signals do. `gap` (start, end) in seconds of day leaves rows out."""
    rng = np.random.RandomState(seed + d.toordinal())
    t0 = day0(d)
    rows = []
    sats = list(range(1, 33)) + list(range(201, 231))
    step = 86400.0 / (2 * len(sats))
    for i, sat in enumerate(sats):
        for start in (i * step - 1800 + rng.uniform(0, 300), (i + len(sats)) * step - 1800 + rng.uniform(0, 300)):
            rising = rng.rand() < 0.5
            az = rng.uniform(40, 120) if rng.rand() < 0.8 else rng.uniform(150, 300)
            rate = rng.uniform(0.007, 0.011)
            ts = np.arange(math.ceil(start / sample_s) * sample_s, start + 29.0 / rate, sample_s)
            ts = ts[(ts >= 0) & (ts < 86400)]
            if gap is not None:
                ts = ts[(ts < gap[0]) | (ts >= gap[1])]
            if not len(ts):
                continue
            e = 0.5 + rate * (ts - start) if rising else 29.5 - rate * (ts - start)
            sin_e = np.sin(np.radians(e + bennett_deg(e)))
            rh = hortho - tide(t0 + ts)
            snr = {}
            for f, lam in WAVELENGTH.items():
                if sat > 200 and f == 2:
                    snr[f] = np.zeros(len(ts))
                    continue
                k = 2 * np.pi / lam
                lin = (100.0 * (1 + 2 * sin_e)
                       + 12.0 * np.exp(-4 * k ** 2 * rho * sin_e ** 2)
                       * np.cos(4 * np.pi * rh * sin_e / lam + PHASE[(sat > 200, f)])
                       + rng.normal(0, 1.0, len(ts)))
                snr[f] = 20 * np.log10(np.maximum(lin, 1.0))
            edot = rate if rising else -rate
            for i in range(len(ts)):
                rows.append((ts[i], sat, f"{sat:3d} {e[i]:10.4f} {az:10.4f} {ts[i]:6.0f} {edot:10.6f} "
                                       f"{0:7.2f} {snr[1][i]:7.2f} {snr[2][i]:7.2f} {snr[5][i]:7.2f} "
                                       f"{0:7.2f} {0:7.2f}\n"))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows]


def write_live_snr(refl: Path, d: date, **kw) -> Path:
    p = Path(refl) / str(d.year) / "snr" / STA / ev.snr_name(STA, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(synthetic_snr_lines(d, **kw)))
    return p


# ---------------------------------------------------------------------
# a stand-in gnssrefl package, shaped as 4.1.5
# ---------------------------------------------------------------------

FAKE_SPLINE_FUNCTIONS = r'''
"""Stand-in for gnssrefl 4.1.5 spline_functions: the same names, call
order and exits; RH comes from the formula in FAKE_INVSNR_TRUTH."""
import json, math, os, subprocess, sys
import numpy as np

MODE = os.environ.get("FAKE_INVSNR_MODE", "ok")
TRUTH = json.loads(os.environ.get("FAKE_INVSNR_TRUTH", "{}"))


class _Result:
    def __init__(self, x, fun):
        self.x = np.asarray(x, float); self.fun = np.asarray(fun, float)
        self.cost = 0.5 * float(np.sum(self.fun ** 2)); self.nfev = 7
        self.status = 1; self.success = True; self.message = "fake fit"


def least_squares(fun, x0, **kwargs):
    return _Result(x0, fun(x0))


def smarterWay(a):
    out = [k for k in a if a[k]]
    return out, len(out)


def _unix(gps):
    return gps + 315964800.0 - 18.0


def truth_rh(unix):
    t = np.asarray(unix, float) - TRUTH["t_ref"]
    level = 1.3 * np.cos(2 * np.pi * t / TRUTH["m2_s"]) + 0.25 * np.cos(2 * np.pi * t / 43200.0 + 0.7)
    return TRUTH["hortho"] - level + TRUTH.get("rh_bias", 0.0)


def snr2arcs(station, snrdata, azilims, elvlims, rhlims, precision, year, doy, signal='L1', **kwargs):
    gbase, total, gap = snrdata
    t = gbase + np.arange(600.0, total, 1200.0)
    if gap:
        u = _unix(t)
        t = t[(u < gap[0]) | (u >= gap[1])]
    if MODE == "noarcs":
        t = t[:0]
    rh_arr = np.empty((len(t), 12), dtype=object)
    for i, ti in enumerate(t):
        rh_arr[i] = [ti, float(truth_rh(_unix(ti))), 5, 30.0, 5.2, 14.8, 80.0, 9.0, 1.0, 1100.0, 4.2, 1]
    snrdt = np.zeros((10 * len(t), 5), dtype=object)
    return rh_arr, snrdt, {"G1": True, "G2": False, "E1": True}


def snr2spline(station, year, doy, azilims, elvlims, rhlims, precision, kdt, snrfit=True,
               signal='L1', savefile=False, doplot=True, rough_in=0.1, **kwargs):
    xdir = os.environ["REFL_CODE"]
    doy_end = kwargs.get("doy_end", doy)
    for d in range(doy, doy_end + 1):
        name = "%s%03d0.%02d.snr%d" % (station, d, year % 100, kwargs.get("snr_ending", 66))
        if os.path.isfile(name):
            print("SNR file found in the current directory:", name); sys.exit()
        path = os.path.join(xdir, str(year), "snr", station, name)
        if os.path.isfile(path + ".gz"):
            subprocess.call(["gunzip", path + ".gz"])
        if not os.path.isfile(path):
            print(name, " not found. Exiting"); sys.exit()
        sods = [float(line.split()[3]) for line in open(path)]
        with open(os.path.join(xdir, "Files", station, "fake_snr_read.jsonl"), "a") as f:
            f.write(json.dumps({"file": name, "rows": len(sods),
                                "all_on_grid": all(int(round(s)) % 5 == 0 for s in sods)}) + "\n")
        subprocess.call(["gzip", path])
    import datetime as _dt
    first = _dt.date(year, 1, 1) + _dt.timedelta(days=doy - 1)
    gbase = (_dt.datetime(first.year, first.month, first.day, tzinfo=_dt.timezone.utc).timestamp()
             - 315964800.0 + 18.0)
    total = (doy_end - doy + 1) * 86400.0
    gap = TRUTH.get("gap")
    rh_arr, snrdt_arr, fspec = snr2arcs(station, (gbase, total, gap), azilims, elvlims, rhlims,
                                        precision, year, doy, signal=signal, **kwargs)
    if len(rh_arr) == 0:
        print("no reflector height data - exit"); exit()
    times = np.concatenate(([gbase], np.sort(rh_arr[:, 0].astype(float)), [gbase + total]))
    maxtgap = np.max(np.diff(times))
    print("max gap is " + str(int(maxtgap / 60)) + " minutes")
    if maxtgap > kdt * 1.05 and not kwargs.get("risky", False):
        print("Gap in data bigger than node spacing, which has risk of instabilities.")
        sys.exit()
    knots = np.linspace(gbase, gbase + total, int(total / kdt) + 1)
    least_squares(lambda k: np.zeros(3), np.zeros(len(knots)), method="trf")
    least_squares(lambda k: np.ones(5) * 0.5, np.zeros(len(knots) + 5), method="lm")
    if MODE == "crash":
        raise RuntimeError("fake crash inside the fit")
    invout = {"knots": knots, "kval_spectral": np.zeros(len(knots) - 2),
              "kval_js": truth_rh(_unix(knots))[1:-1],
              "outparams_js": np.array([1.0, 2.0, 3.0, 4.0, TRUTH.get("rough", 0.0031)])}
    if MODE == "nofile":
        return invout
    files = os.path.join(xdir, "Files", station)
    open(os.path.join(files, "spline_out.png"), "wb").write(b"png")
    open(os.path.join(files, "outliers_lsp.txt"), "w").write("")
    os.makedirs("plots", exist_ok=True)
    delta_out = int(kwargs.get("delta_out", 300))
    iout = open(kwargs["outfile_name"] + ".txt", "w+")
    iout.write("% Results for {0:4s} calculated on 2026-10-20 12:00 \n".format(station))
    iout.write("% gnssrefl, https://github.com/kristinemlarson \n")
    iout.write("% YYYY MM DD HH MM SS RH(m) doy MJD Nretrievals\n")
    tplot = np.linspace(gbase, gbase + total, 1 + int(total / delta_out))
    rh = truth_rh(_unix(tplot))
    arc_unix = _unix(rh_arr[:, 0].astype(float))
    for i in range(len(tplot) - 1):
        dt = _dt.datetime.fromtimestamp(_unix(tplot[i]), tz=_dt.timezone.utc)
        hour0 = _unix(tplot[i]) - _unix(tplot[i]) % 3600
        n = int(np.sum((arc_unix >= hour0) & (arc_unix < hour0 + 3600)))
        doy_i = dt.timetuple().tm_yday
        mjd = (dt.date() - _dt.date(1858, 11, 17)).days + (dt.hour * 3600 + dt.minute * 60) / 86400.0
        iout.write(" {0:4.0f} {1:2.0f} {2:2.0f} {3:2.0f} {4:2.0f} {5:2.0f} {6:8.3f} {7:3.0f} {8:13.6f} {9:3.0f}\n".format(
            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, rh[i], doy_i, mjd, n))
    iout.close()
    return invout
'''

FAKE_INVSNR_CL = r'''
"""Stand-in for gnssrefl 4.1.5 invsnr_cl: the same signature and json."""
import json, os, sys
import gnssrefl.spline_functions as spline_functions


def invsnr(station: str, year: int, doy: int, signal: str, peak2noise: float = 2.5, constel: str = None,
        screenstats: bool = False, dec: int = 1, polydeg: int = 2, snrfit: bool = True, plt: bool = True,
        doy_end: int = None, lspfigs: bool = False, snrfigs: bool = False, knot_space: int = 3,
        rough_in: float = 0.1, risky: bool = False, snr: int = 66, outfile_type: str = 'txt',
        outfile_name: str = '', outlier_limit: float = 0.5, no_dots: bool = False, delta_out: int = 300,
           refraction: bool = True, json_override: bool = False, lastday_seconds: int=0):
    xdir = os.environ['REFL_CODE']
    instructions2 = xdir + '/input/' + station + '.inv.json'
    if not os.path.isfile(instructions2):
        print('Instruction file does not exist.', instructions2, ' Exiting.'); sys.exit()
    station_config = json.load(open(instructions2))
    call = dict(locals()); call.pop("station_config")
    call["cwd"] = os.getcwd(); call["json"] = station_config
    call["stdin_devnull"] = os.path.samestat(os.fstat(0), os.stat(os.devnull))
    with open(os.path.join(xdir, "Files", station, "fake_calls.jsonl"), "a") as f:
        f.write(json.dumps(call) + "\n")
    if 'pktnlim' in station_config:
        peak2noise = station_config['pktnlim']
    doy_end = doy if doy_end is None else int(doy_end)
    station_config['refraction'] = bool(refraction)
    spline_functions.snr2spline(station, year, doy, station_config['azilims'], station_config['elvlims'],
                                station_config['rhlims'], station_config['precision'], knot_space * 3600,
                                signal=signal, snrfit=snrfit, doplot=plt, pktnlim=peak2noise,
                                tempres=dec, doy_end=doy_end, rough_in=rough_in, risky=risky,
                                snr_ending=snr, outfile_type=outfile_type, delta_out=delta_out,
                                station_config=station_config, outfile_name=outfile_name)
'''


def install_fake_gnssrefl(where: Path) -> Path:
    pkg = Path(where) / "fake_gnssrefl" / "gnssrefl"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "spline_functions.py").write_text(FAKE_SPLINE_FUNCTIONS)
    (pkg / "invsnr_cl.py").write_text(FAKE_INVSNR_CL)
    return pkg.parent


# ---------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------

class ComparisonTests(unittest.TestCase):

    def test_sample_on_grid_interpolates_and_breaks_at_gaps(self):
        ep = np.array([0, 1800, 3600, 9000, 10800], float)
        val = np.array([0.0, 1.0, 2.0, 5.0, 6.0])
        grid = np.array([900, 1800, 2700, 3600, 5400, 7200, 9000, 9900, 12000], float)
        out = ev.sample_on_grid(ep, val, grid, max_step_s=2700)
        np.testing.assert_allclose(out[:4], [0.5, 1.0, 1.5, 2.0])
        self.assertTrue(np.isnan(out[4]) and np.isnan(out[5]))   # inside the 5400 s gap
        self.assertEqual(out[6], 5.0)                              # a sample at the gap's end
        self.assertAlmostEqual(out[7], 5.5)
        self.assertTrue(np.isnan(out[8]))                          # beyond the series

    def test_sample_on_grid_drops_nan_rows(self):
        out = ev.sample_on_grid([0, 1800, 3600], [1.0, np.nan, 3.0], [1800.0], max_step_s=2700)
        self.assertTrue(np.isnan(out[0]))          # the NaN row leaves a 3600 s gap

    def test_supported_mask(self):
        grid = np.arange(0, 7 * 3600, 1800, dtype=float)
        mask = ev.supported_mask(grid, [3600.0], support_s=3600)
        np.testing.assert_array_equal(np.flatnonzero(mask), [0, 1, 2, 3, 4])
        self.assertFalse(ev.supported_mask(grid, []).any())

    def test_stats_identities(self):
        rng = np.random.RandomState(1)
        ref = rng.normal(0, 1, 2000)
        x = ref + 0.05 + rng.normal(0, 0.03, 2000)
        x[:10] = np.nan
        s = ev.stats(x, ref)
        self.assertEqual(s["n"], 1990)
        self.assertAlmostEqual(s["bias"], 0.05, delta=0.003)
        self.assertAlmostEqual(s["sd"], 0.03, delta=0.003)
        self.assertAlmostEqual(s["rmse"] ** 2, s["bias"] ** 2 + s["sd"] ** 2, places=12)
        self.assertAlmostEqual(s["nmad"], 0.03, delta=0.004)
        self.assertGreater(s["r"], 0.99)
        self.assertEqual(ev.stats([np.nan], [1.0]), {"n": 0})

    def test_sea_state_classes(self):
        sea = ev.sea_state([np.nan, 0.5, 1.5, 2.9, 3.0, 6.0], rough_hs=1.5, storm_hs=3.0)
        self.assertEqual(list(sea), ["unknown", "calm", "rough", "rough", "storm", "storm"])

    def test_gauge_transfer_recovers_the_station_fit(self):
        """level = 1.24 gauge(t + 48 min) - 0.10, as gnssir_reflection_audit
        uses: the robust fit finds it and predicts the level."""
        days = [date(2026, 9, 20) + timedelta(days=i) for i in range(5)]
        grid = grid_for(days)
        g_ep = np.arange(grid[0] - 86400, grid[-1] + 86400, 360.0)
        a, lag_s, b = 1.24, -48 * 60.0, -0.10
        gauge_lv = (tide(g_ep + lag_s) - b) / a          # so a*gauge(t - lag) + b = tide(t)
        rng = np.random.RandomState(2)
        level = tide(grid) + rng.normal(0, 0.02, len(grid))
        fit, pred = ev.gauge_transfer(grid, level, np.ones(len(grid), bool), (g_ep, gauge_lv))
        self.assertAlmostEqual(fit["a"], a, delta=0.02)
        self.assertAlmostEqual(fit["lag_min"], -48, delta=6)
        self.assertAlmostEqual(fit["b"], b, delta=0.02)
        self.assertLess(ev.stats(level, pred)["rmse"], 0.03)
        none, nan = ev.gauge_transfer(grid, level, np.zeros(len(grid), bool), (g_ep, gauge_lv))
        self.assertIsNone(none)
        self.assertTrue(np.isnan(nan).all())

    def test_daily_mean_rms_needs_most_of_the_day(self):
        days = [date(2026, 9, 20), date(2026, 9, 21)]
        grid = grid_for(days)
        resid = np.where(grid < day0(days[1]), 0.02, -0.04)
        use = np.ones(len(grid), bool)
        use[48:48 + 20] = False                 # the second day keeps 28 of 48
        out = ev.daily_mean_rms(grid, resid, use)
        self.assertEqual(out["days"], 1)
        self.assertAlmostEqual(out["rms"], 0.02)

    def _scenario(self):
        """Six days; day 4 is a storm (Hs 3.5 m). The 'spline' is the tide
        + 6 cm noise, +0.40 m in the storm and a 4-hour hole on day 2 that
        it bridges with a straight line; 'invsnr' is the tide + 2 cm noise
        + 3 cm offset and +0.10 m in the storm, and its run of day 5 failed."""
        days = [date(2026, 9, 20) + timedelta(days=i) for i in range(6)]
        grid = grid_for(days)
        truth = tide(grid)
        rng = np.random.RandomState(3)
        storm = (grid >= day0(days[3])) & (grid < day0(days[4]))
        spline = truth + rng.normal(0, 0.06, len(grid)) + np.where(storm, 0.40, 0.0)
        hole = (grid >= day0(days[1]) + 6 * 3600) & (grid < day0(days[1]) + 10 * 3600)
        spline[hole] = np.interp(grid[hole], grid[~hole], spline[~hole])
        inv = truth + 0.03 + rng.normal(0, 0.02, len(grid)) + np.where(storm, 0.10, 0.0)
        failed = (grid >= day0(days[4])) & (grid < day0(days[5]))
        inv[failed] = np.nan
        g_ep = np.arange(grid[0] - 86400, grid[-1] + 86400, 360.0)
        gauge = (g_ep, (tide(g_ep - 48 * 60.0) + 0.10) / 1.24)
        w_ep = grid.copy()                       # one buoy record per reading
        hs = np.where((w_ep >= day0(days[3])) & (w_ep < day0(days[4])), 3.5, 0.8)
        waves = (w_ep, hs, np.full(len(w_ep), 9.0))
        levels = {"spline": spline, "invsnr k2h": inv}
        supported = {"spline": ~hole, "invsnr k2h": np.ones(len(grid), bool)}
        return days, grid, truth, levels, supported, gauge, waves, storm, hole, failed

    def test_evaluate_ranks_the_series_and_splits_by_sea_state(self):
        days, grid, truth, levels, supported, gauge, waves, storm, hole, failed = self._scenario()
        tide_model = truth - 0.02
        arrays = {}
        res = ev.evaluate(grid, levels, supported, gauge, tide_model, waves, arrays=arrays)
        self.assertEqual(res["sea_counts"]["storm"], 48)
        self.assertEqual(res["sea_counts"]["calm"], len(grid) - 48)
        self.assertEqual(res["common_n"], int((~hole & ~failed).sum()))
        sp, iv = res["series"]["spline"], res["series"]["invsnr k2h"]

        # coverage: values everywhere for the spline, its hole unsupported; invsnr lost a day
        self.assertAlmostEqual(sp["coverage"]["all"]["values"], 1.0)
        self.assertAlmostEqual(sp["coverage"]["all"]["supported"], 1 - 8 / len(grid))
        self.assertAlmostEqual(iv["coverage"]["all"]["values"], 5 / 6)

        # the transfer is fitted on calm readings, so it is the true one for both
        for s in (sp, iv):
            self.assertAlmostEqual(s["gauge_fit"]["a"], 1.24, delta=0.03)
            self.assertAlmostEqual(s["gauge_fit"]["lag_min"], -48, delta=6)
        self.assertAlmostEqual(iv["gauge_fit"]["b"], -0.10 + 0.03, delta=0.02)

        # calm: invsnr tracks the gauge ~3x better; storm: the excess shows as bias
        self.assertLess(iv["common"]["gauge"]["calm"]["rmse"], 0.03)
        self.assertGreater(sp["common"]["gauge"]["calm"]["rmse"], 0.05)
        self.assertAlmostEqual(sp["common"]["gauge"]["storm"]["bias"], 0.40, delta=0.03)
        self.assertAlmostEqual(iv["common"]["gauge"]["storm"]["bias"], 0.10, delta=0.02)

        # tide model: no fitted parameter, so the offsets appear as bias
        self.assertAlmostEqual(iv["common"]["tide"]["calm"]["bias"], 0.05, delta=0.01)
        self.assertAlmostEqual(sp["common"]["tide"]["calm"]["bias"], 0.02, delta=0.01)
        self.assertAlmostEqual(iv["common"]["tide"]["calm"]["sd"], 0.02, delta=0.005)

        # invsnr minus spline: +3 cm calm, -27 cm in the storm
        self.assertAlmostEqual(iv["minus_spline"]["calm"]["bias"], 0.03, delta=0.01)
        self.assertAlmostEqual(iv["minus_spline"]["storm"]["bias"], 0.03 + 0.10 - 0.40, delta=0.02)
        self.assertNotIn("minus_spline", sp)

        # what the spline wrote across its hole is counted, separately
        self.assertEqual(sp["unsupported"]["gauge"]["n"], 8)
        self.assertEqual(iv["unsupported"]["gauge"]["n"], 0)

        # daily means: both near zero on calm days
        self.assertGreaterEqual(iv["daily_gauge"]["days"], 3)
        self.assertLess(iv["daily_gauge"]["rms"], 0.1)

        # per-day RMSE and the arrays handed back
        d = days[0].isoformat()
        self.assertLess(res["per_day"][d]["invsnr k2h"], res["per_day"][d]["spline"])
        self.assertAlmostEqual(res["per_day"][days[3].isoformat()]["hs_mean"], 3.5)
        self.assertIsNone(res["per_day"][days[4].isoformat()]["invsnr k2h"])
        self.assertEqual(set(arrays), {"hs", "sea", "gauge_pred"})
        self.assertEqual(len(arrays["gauge_pred"]["spline"]), len(grid))

        # the station's own QC sees the spline's storm excess, not invsnr's
        self.assertIsNotNone(sp["qc"]["storm"])
        json.dumps(ev._jsonable(res))          # summary_*.json can be written

    def test_evaluate_without_references(self):
        days, grid, truth, levels, supported, *_ = self._scenario()
        res = ev.evaluate(grid, levels, supported)
        self.assertEqual(res["sea_counts"]["unknown"], len(grid))
        self.assertIsNone(res["series"]["spline"]["gauge_fit"])
        self.assertEqual(res["series"]["spline"]["common"]["gauge"]["all"], {"n": 0})
        self.assertEqual(res["series"]["spline"]["common"]["tide"]["all"], {"n": 0})
        self.assertIn("minus_spline", res["series"]["invsnr k2h"])

    def test_report_mentions_every_section(self):
        days, grid, truth, levels, supported, gauge, waves, *_ = self._scenario()
        res = ev.evaluate(grid, levels, supported, gauge, truth, waves)
        s = ev.InvsnrSettings(station=STA)
        rows = [{"day": d.isoformat(), "status": "ok" if i != 4 else "exit",
                 "message": "" if i != 4 else "gnssrefl stopped", "span": "", "arcs_core_day": 60,
                 "max_gap_min": 40.0, "gap_limit_min": 126.0, "roughness_m2": 0.002 + 0.001 * (i == 3),
                 "roughness_sigma_m": 0.045, "snr_fit_rms": 2.1, "snr_fit_ok": True, "nfev": 30,
                 "runtime_s": 50} for i, d in enumerate(days)]
        text = ev.format_report("TITLE", res, {"invsnr k2h": rows}, [s], ["a note"])
        for part in ("COVERAGE", "AGAINST THE CHATHAM GAUGE", "AGAINST THE TIDE MODEL",
                     "INVSNR MINUS SPLINE", "QC:", "IN GAPS", "PER DAY", "Spearman", "a note",
                     "5 of 6 days ran", "gnssrefl stopped"):
            self.assertIn(part, text)

    @staticmethod
    def _rows(days, failed=()):
        return [{"day": d.isoformat(), "status": "exit" if i in failed else "ok", "message": "",
                 "snr_fit_ok": None if i in failed else True} for i, d in enumerate(days)]

    def test_adoption_rule(self):
        days, grid, truth, levels, supported, gauge, waves, storm, hole, failed = self._scenario()
        name = "invsnr k2h"
        res = ev.evaluate(grid, levels, supported, gauge, truth, waves)
        tests = {t[0]: t for t in ev.adoption_check(res, {name: self._rows(days, failed=(4,))})[name]}
        # 2 cm against 6 cm noise: both accuracy tests pass by far more than 1 cm
        self.assertIs(tests["gauge SD, calm common epochs"][3], True)
        self.assertIs(tests["tide-model SD, calm common epochs"][3], True)
        # it lost a day, so its coverage is lower, and 5 of 6 days is under 95 %
        self.assertIs(tests["supported coverage"][3], False)
        self.assertAlmostEqual(tests["days ran (of days with SNR data)"][1], 5 / 6)
        self.assertIs(tests["days ran (of days with SNR data)"][3], False)
        self.assertIsNone(tests["QC fail share, rough"][3])        # no rough readings here
        self.assertEqual(ev.adoption_verdict(list(tests.values())), "fails")

        # every day ran: what is left open is the sea state the windows lack
        levels2 = dict(levels)
        levels2[name] = truth + 0.03 + np.random.RandomState(4).normal(0, 0.02, len(grid))
        res2 = ev.evaluate(grid, levels2, supported, gauge, truth, waves)
        t2 = ev.adoption_check(res2, {name: self._rows(days)})[name]
        self.assertEqual(ev.adoption_verdict(t2), "undecided")
        self.assertEqual([t[0] for t in t2 if t[3] is None], ["QC fail share, rough"])
        self.assertEqual(res2["common_by_class"]["storm"], 48)

        # a spline only a few mm worse is not beaten by enough
        levels3 = {"spline": levels2[name] + np.random.RandomState(5).normal(0, 0.012, len(grid)),
                   name: levels2[name]}
        res3 = ev.evaluate(grid, levels3, supported, gauge, truth, waves)
        t3 = {t[0]: t for t in ev.adoption_check(res3, {name: self._rows(days)})[name]}
        self.assertLess(t3["gauge SD, calm common epochs"][2] - t3["gauge SD, calm common epochs"][1], 0.01)
        self.assertIs(t3["gauge SD, calm common epochs"][3], False)

        text = ev.format_report("T", res2, {name: [dict(r, span="", arcs_core_day=60, max_gap_min=40.0,
                                                         gap_limit_min=126.0, roughness_m2=0.002,
                                                         roughness_sigma_m=0.045, snr_fit_rms=2.1,
                                                         nfev=30, runtime_s=50) for r in self._rows(days)]},
                                [ev.InvsnrSettings(station=STA)])
        self.assertIn("ADOPTION RULE", text)
        self.assertIn(f"{name}: UNDECIDED", text)
        self.assertIn("storm 48", text)

    def test_rank_correlation(self):
        r, n = ev.rank_correlation([1, 2, 3, 4, np.nan], [10, 20, 25, 40, 50])
        self.assertEqual(n, 4)
        self.assertAlmostEqual(r, 1.0)
        self.assertEqual(ev.rank_correlation([1, 2], [1, 2]), (None, 2))


# ---------------------------------------------------------------------
# run plan, invsnr API, files
# ---------------------------------------------------------------------

class PlanAndApiTests(unittest.TestCase):

    def test_parse_window(self):
        self.assertEqual(ev.parse_window("2026-09-20:2026-09-22"), (date(2026, 9, 20), date(2026, 9, 22)))
        self.assertEqual(ev.parse_window("2026-09-20"), (date(2026, 9, 20), date(2026, 9, 20)))
        with self.assertRaises(ValueError):
            ev.parse_window("2026-09-22:2026-09-20")

    def test_plan_pads_each_day_but_never_across_a_year(self):
        runs = ev.plan_runs([(date(2026, 12, 30), date(2027, 1, 1)), (date(2026, 12, 31), date(2026, 12, 31))], 1)
        self.assertEqual([r.day for r in runs], [date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 1)])
        self.assertEqual((runs[0].first, runs[0].last), (date(2026, 12, 29), date(2026, 12, 31)))
        self.assertEqual((runs[1].first, runs[1].last), (date(2026, 12, 30), date(2026, 12, 31)))
        self.assertEqual((runs[2].first, runs[2].last), (date(2027, 1, 1), date(2027, 1, 2)))

    def test_inv_json_has_exactly_invsnr_inputs_keys(self):
        s = ev.InvsnrSettings(station=STA)
        j = ev.inv_json(s, 41.89, -69.96, -10.02)
        self.assertEqual(tuple(j), ev.INV_JSON_KEYS)
        self.assertEqual(j["rhlims"], [16.0, 24.0])
        self.assertEqual(j["elvlims"], [5.0, 15.0])
        self.assertEqual(j["azilims"], [35.0, 125.0])
        self.assertEqual(j["pktnlim"], 3.0)
        self.assertIs(j["l2c_only"], True)

    def test_invsnr_kwargs_are_invsnr_415_parameters(self):
        s = ev.InvsnrSettings(station=STA, knot_space_h=1.5)
        kw = ev.invsnr_kwargs(s, date(2026, 9, 19), date(2026, 9, 21), Path("/x/invsnr"))
        self.assertTrue(set(kw) <= set(INVSNR_415_PARAMS), set(kw) - set(INVSNR_415_PARAMS))
        self.assertEqual((kw["year"], kw["doy"], kw["doy_end"]), (2026, 262, 264))
        self.assertEqual(kw["knot_space"], 1.5)
        self.assertEqual(kw["dec"], 1)                  # the copies are decimated already
        self.assertEqual(kw["outfile_type"], "txt")     # 'csv' raises in 4.1.5
        self.assertIs(kw["snrfit"], True)               # no file is written otherwise
        self.assertIs(kw["plt"], False)
        self.assertNotIn("polydeg", kw)                 # accepted but never used in 4.1.5
        if importlib.util.find_spec("gnssrefl") is not None:
            from gnssrefl.invsnr_cl import invsnr
            self.assertEqual(tuple(inspect.signature(invsnr).parameters), INVSNR_415_PARAMS)

    def test_label_changes_with_any_setting(self):
        a = ev.InvsnrSettings(station=STA)
        self.assertEqual(a.label(), ev.InvsnrSettings(station=STA).label())
        self.assertNotEqual(a.label(), ev.InvsnrSettings(station=STA, rough_in=0.01).label())
        self.assertTrue(ev.InvsnrSettings(station=STA, knot_space_h=1.5).label().startswith("k1.5h-"))

    def test_read_invsnr_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "invsnr.txt"
            p.write_text("% Results for usgs calculated on 2026-10-20 12:00 \n"
                         "% gnssrefl, https://github.com/kristinemlarson \n"
                         "% YYYY MM DD HH MM SS RH(m) doy MJD Nretrievals\n"
                         " {0:4.0f} {1:2.0f} {2:2.0f} {3:2.0f} {4:2.0f} {5:2.0f} {6:8.3f} {7:3.0f} {8:13.6f} {9:3.0f}\n"
                         .format(2026, 9, 20, 0, 5, 0, 18.123, 263, 61303.003472, 4))
            ep, rh, n = ev.read_invsnr_txt(p)
        self.assertEqual(ep[0], datetime(2026, 9, 20, 0, 5, tzinfo=timezone.utc).timestamp())
        self.assertAlmostEqual(rh[0], 18.123)
        self.assertEqual(n[0], 4)

    def test_stage_snr_decimates_on_the_clock_and_leaves_the_live_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, private = Path(tmp) / "live", Path(tmp) / "private"
            d = date(2026, 9, 20)
            p = live / "2026" / "snr" / STA / ev.snr_name(STA, d)
            p.parent.mkdir(parents=True)
            rows = [f"  5 {10 + i * 0.001:.4f} 80.0 {i} 0.01 0 45.0 44.0 43.0 0 0\n" for i in range(0, 60)]
            import gzip
            with gzip.open(str(p) + ".gz", "wt") as f:
                f.writelines(rows)
            before = tree_digest(live)
            out = ev.stage_snr(live, private, STA, d, 5)
            self.assertEqual(tree_digest(live), before)
            kept = out.read_text().splitlines()
            self.assertEqual([int(float(r.split()[3])) for r in kept], list(range(0, 60, 5)))
            self.assertEqual(ev.stage_snr(live, private, STA, d, 5), out)       # reused
            self.assertIsNone(ev.stage_snr(live, private, STA, date(2026, 9, 21), 5))
            # a plain and a gzipped copy together: a run was stopped mid-gzip; copied again
            out.with_name(out.name + ".gz").write_bytes(b"cut short")
            again = ev.stage_snr(live, private, STA, d, 5)
            self.assertEqual(again, out)
            self.assertFalse(out.with_name(out.name + ".gz").exists())
            self.assertEqual(len(again.read_text().splitlines()), 12)
            # a different decimation is a different copy
            self.assertEqual(len(ev.stage_snr(live, private, STA, d, 10).read_text().splitlines()), 6)

    def test_max_gap_counts_the_run_edges(self):
        d = date(2026, 9, 20)
        arcs = [day0(d) + 3600.0, day0(d) + 5 * 3600.0]
        self.assertAlmostEqual(ev.max_gap_minutes(arcs, d, d), 19 * 60.0)


# ---------------------------------------------------------------------
# the run step, with a stand-in gnssrefl
# ---------------------------------------------------------------------

class FakeGnssreflRunTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.live = self.tmp / "products" / "refl_code"
        self.out = self.tmp / "invsnr_eval"
        self.days = [date(2026, 9, 19) + timedelta(days=i) for i in range(5)]
        for d in self.days:
            p = self.live / str(d.year) / "snr" / STA / ev.snr_name(STA, d)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(f"  5 10.0 80.0 {s} 0.01 0 45 44 43 0 0\n" for s in range(0, 600, 1)))
        (self.live / "Files" / STA).mkdir(parents=True)
        self.fake = install_fake_gnssrefl(self.tmp)
        self.env = {
            "PYTHONPATH": str(self.fake) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "FAKE_INVSNR_MODE": "ok",
            "FAKE_INVSNR_TRUTH": json.dumps({"t_ref": T_REF, "m2_s": M2_S, "hortho": HORTHO,
                                             "rh_bias": 0.0}),
        }

    def tearDown(self):
        self._tmp.cleanup()

    def ctx(self, **kw):
        return ev.RunContext(live_refl=self.live, out_dir=self.out, lat=41.89, lon=-69.96, ht=-10.02,
                             python=sys.executable, **kw)

    def run_one(self, day, settings=None, env=None, **ctx_kw):
        settings = settings or ev.InvsnrSettings(station=STA)
        run = [r for r in ev.plan_runs([(day, day)], settings.pad_days)][0]
        e = dict(self.env)
        e.update(env or {})
        with unittest.mock.patch.dict(os.environ, e):
            return run, settings, ev.execute_run(run, settings, self.ctx(**ctx_kw))

    def calls(self, settings):
        f = self.out / f"refl_code_dec{settings.decimate_s}" / "Files" / STA / "fake_calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()]

    def test_a_run_in_a_private_refl_code(self):
        before = tree_digest(self.live)
        run, s, res = self.run_one(date(2026, 9, 21))
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(tree_digest(self.live), before)          # the live tree is only read
        rd = self.out / "runs" / s.label() / "2026-09-21"
        self.assertTrue((rd / "invsnr.txt").exists())
        self.assertTrue((rd / "spline_out.png").exists())        # gnssrefl's plot kept with the run
        self.assertFalse((rd / "cwd").exists())                  # its scratch directory is gone
        call = self.calls(s)[-1]
        self.assertEqual(call["json"], ev.inv_json(s, 41.89, -69.96, -10.02))
        self.assertEqual((call["doy"], call["doy_end"]), (263, 265))
        self.assertEqual(call["cwd"], str(rd / "cwd"))
        self.assertEqual(call["dec"], 1)
        read = [json.loads(x) for x in (self.out / "refl_code_dec5" / "Files" / STA /
                                        "fake_snr_read.jsonl").read_text().splitlines()]
        self.assertEqual(len(read), 3)
        self.assertTrue(all(r["all_on_grid"] and r["rows"] == 120 for r in read))
        # what the child kept from inside invsnr
        self.assertAlmostEqual(res["roughness"], 0.0031)
        self.assertEqual(res["fspec"], ["G1", "E1"])
        self.assertEqual(len(res["fits"]), 2)
        self.assertEqual(len(res["arcs"]["unix"]), 3 * 72)
        self.assertAlmostEqual(res["arcs"]["unix"][0], day0(date(2026, 9, 20)) + 600.0, places=3)
        # the series: the core day only, NAVD88 level = Hortho - RH
        ep, lv, arcs = ev.invsnr_levels([(run, res)], s, self.ctx(), HORTHO)
        self.assertEqual(len(ep), 288)
        self.assertEqual(ep[0], day0(date(2026, 9, 21)))
        np.testing.assert_allclose(lv, tide(ep), atol=0.0006)
        self.assertTrue(((arcs >= day0(date(2026, 9, 21)) - 3600) &
                         (arcs < day0(date(2026, 9, 22)) + 3600)).all())

    def test_a_finished_run_is_reused_and_rerun_on_request(self):
        run, s, res = self.run_one(date(2026, 9, 21))
        self.assertEqual(len(self.calls(s)), 1)
        self.assertIs(self.calls(s)[0]["stdin_devnull"], True)    # gzip can never wait for an answer
        _, _, again = self.run_one(date(2026, 9, 21))
        self.assertEqual(len(self.calls(s)), 1)
        self.assertTrue(again["reused"])
        self.run_one(date(2026, 9, 21), rerun=True)
        self.assertEqual(len(self.calls(s)), 2)

    def test_a_gap_exit_is_reused_but_a_crash_is_tried_again(self):
        truth = json.loads(self.env["FAKE_INVSNR_TRUTH"])
        truth["gap"] = [day0(date(2026, 9, 21)) + 6 * 3600, day0(date(2026, 9, 21)) + 9 * 3600]
        env = {"FAKE_INVSNR_TRUTH": json.dumps(truth)}
        _, s, res = self.run_one(date(2026, 9, 21), env=env)
        self.assertEqual(res["status"], "exit")
        self.run_one(date(2026, 9, 21), env=env)
        self.assertEqual(len(self.calls(s)), 1)                  # gnssrefl would stop again
        self.run_one(date(2026, 9, 22), env={"FAKE_INVSNR_MODE": "crash"})
        self.run_one(date(2026, 9, 22), env={"FAKE_INVSNR_MODE": "crash"})
        self.assertEqual(len(self.calls(s)), 3)                  # an error is not reused

    def test_a_reprocessed_snr_day_is_copied_and_run_again(self):
        run, s, res = self.run_one(date(2026, 9, 21))
        live = self.live / "2026" / "snr" / STA / ev.snr_name(STA, date(2026, 9, 22))
        live.write_text("".join(f"  7 11.0 81.0 {t} 0.01 0 45 44 43 0 0\n" for t in range(0, 900, 1)))
        os.utime(live, (time.time() + 5, time.time() + 5))
        _, _, res2 = self.run_one(date(2026, 9, 21))
        self.assertNotIn("reused", res2)
        self.assertEqual(len(self.calls(s)), 2)
        copy = self.out / "refl_code_dec5" / "2026" / "snr" / STA / ev.snr_name(STA, date(2026, 9, 22))
        copy = copy if copy.exists() else copy.with_name(copy.name + ".gz")
        import gzip
        text = (gzip.open(copy, "rt") if copy.name.endswith(".gz") else open(copy)).read()
        self.assertEqual(len(text.splitlines()), 180)            # the new file, decimated

    def test_the_gap_rule_exit_is_recorded_not_raised(self):
        gap = [day0(date(2026, 9, 21)) + 6 * 3600, day0(date(2026, 9, 21)) + 9 * 3600]
        truth = json.loads(self.env["FAKE_INVSNR_TRUTH"])
        truth["gap"] = gap
        run, s, res = self.run_one(date(2026, 9, 21), env={"FAKE_INVSNR_TRUTH": json.dumps(truth)})
        self.assertEqual(res["status"], "exit")
        rows = ev.run_table([(run, res)], s)
        self.assertGreater(rows[0]["max_gap_min"], rows[0]["gap_limit_min"])
        self.assertIn("Gap in data bigger than node spacing",
                      (self.out / "runs" / s.label() / "2026-09-21" / "run.log").read_text())
        # with --risky the same day runs
        risky = ev.InvsnrSettings(station=STA, risky=True)
        _, _, res2 = self.run_one(date(2026, 9, 21), settings=risky,
                                  env={"FAKE_INVSNR_TRUTH": json.dumps(truth)})
        self.assertEqual(res2["status"], "ok")

    def test_failures_are_results(self):
        _, _, res = self.run_one(date(2026, 9, 21), env={"FAKE_INVSNR_MODE": "crash"})
        self.assertEqual(res["status"], "error")
        self.assertIn("fake crash", res["message"])
        _, _, res = self.run_one(date(2026, 9, 22), env={"FAKE_INVSNR_MODE": "noarcs"})
        self.assertEqual(res["status"], "exit")
        _, _, res = self.run_one(date(2026, 9, 23), env={"FAKE_INVSNR_MODE": "nofile"})
        self.assertEqual(res["status"], "error")
        self.assertIn("without writing", res["message"])
        _, _, res = self.run_one(date(2026, 9, 30))
        self.assertEqual(res["status"], "no_snr")

    def test_a_missing_pad_day_shortens_the_run_and_its_edge_is_dropped(self):
        run, s, res = self.run_one(date(2026, 9, 23))            # 24 Sep has no SNR file
        self.assertEqual(res["status"], "ok")
        self.assertEqual((res["spec"]["first"], res["spec"]["last"]), ("2026-09-22", "2026-09-23"))
        ep, lv, _ = ev.invsnr_levels([(run, res)], s, self.ctx(), HORTHO)
        self.assertEqual(ep[-1], day0(date(2026, 9, 24)) - 2 * 3600)   # last knot spacing left out

    def _station_files(self, storm_day: date):
        """station.json, spline, gnssir results, gauge and wave caches."""
        cfg = {"gnssrefl_station_code": STA, "latitude": 41.89, "longitude": -69.96, "height": -10.02,
               "gnssrefl_orthometric_height": HORTHO, "vertical_datum": "NAVD88 (GEOID18)"}
        sj = self.tmp / "station.json"
        sj.write_text(json.dumps(cfg))
        files = self.live / "Files" / STA
        rng = np.random.RandomState(5)
        lines = [textwrap.dedent(f"""\
            %  station usgs gnssrefl v4.1.5
            %  orthometric height minus RH, where Hortho (m) is {HORTHO:10.3f}
            %  MJD              RH(m)  YYYY  MM  DD  HH  MM  SS   quasi-sea-level(m)
            """)]
        t = day0(self.days[0])
        while t < day0(self.days[-1]) + 86400:
            lv = float(tide([t])[0]) + rng.normal(0, 0.06)
            if day0(storm_day) <= t < day0(storm_day) + 86400:
                lv += 0.5
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
            lines.append(f"{40000 + t / 86400:15.7f} {HORTHO - lv:8.3f} {dt.year} {dt.month:2d} {dt.day:2d} "
                         f"{dt.hour:2d} {dt.minute:2d} {dt.second:2d} {lv:8.3f}\n")
            t += 1800
        (files / f"{STA}_spline_out.txt").write_text("".join(lines))
        for d in self.days:
            res = self.live / str(d.year) / "results" / STA / f"{d.timetuple().tm_yday:03d}.txt"
            res.parent.mkdir(parents=True, exist_ok=True)
            res.write_text("".join(f"{d.year} {d.timetuple().tm_yday} 18.0 5 {h:.3f} 80.0 9 5 15 100 1 1 "
                                   f"0 4.0 30 61000 1\n" for h in np.arange(0.25, 24, 0.5)))
        g_ep = np.arange(day0(self.days[0]) - 86400, day0(self.days[-1]) + 2 * 86400, 360.0)
        with open(files / "gauge_8447435.csv", "w") as f:
            f.write("time_utc,epoch,level_navd88\n")
            for e, v in zip(g_ep, (tide(g_ep - 48 * 60.0) + 0.10) / 1.24):
                f.write(f"x,{int(e)},{v:.3f}\n")
        with open(files / "waves_44008.csv", "w") as f:
            f.write("time_utc,epoch,wvht_m,dpd_s\n")
            for e in np.arange(day0(self.days[0]) - 86400, day0(self.days[-1]) + 2 * 86400, 1800.0):
                hs = 3.6 if day0(storm_day) <= e < day0(storm_day) + 86400 else 0.9
                f.write(f"x,{int(e)},{hs},9.0\n")
        return sj

    def test_the_command_line_end_to_end(self):
        storm = date(2026, 9, 21)
        sj = self._station_files(storm)
        before = tree_digest(self.live)
        argv = ["--window", "2026-09-20:2026-09-22", "--station-json", str(sj),
                "--refl-code", str(self.live), "--out-dir", str(self.out), "--python", sys.executable,
                "--offline"]
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env), redirect_stdout(buf):
            self.assertEqual(ev.main(argv), 0)
        text = buf.getvalue()
        self.assertEqual(tree_digest(self.live), before)
        self.assertIn("INVERSE MODELLING (invsnr) vs THE PUBLISHED SPLINE", text)
        self.assertIn("3 of 3 days ran", text)
        self.assertIn("no tide model configured", text)
        tag = "20260920-20260922"
        summary = json.loads((self.out / f"summary_{tag}.json").read_text())
        res = summary["result"]["series"]
        # the stand-in invsnr is the truth; the spline has 6 cm noise and +0.5 m in the storm
        self.assertLess(res["invsnr k2h"]["common"]["gauge"]["calm"]["rmse"], 0.02)
        self.assertGreater(res["spline"]["common"]["gauge"]["calm"]["rmse"], 0.04)
        self.assertAlmostEqual(res["spline"]["common"]["gauge"]["storm"]["bias"], 0.5, delta=0.05)
        self.assertAlmostEqual(res["invsnr k2h"]["minus_spline"]["storm"]["bias"], -0.5, delta=0.05)
        self.assertEqual(summary["result"]["sea_counts"]["storm"], 48)
        adopt = summary["adoption"]["invsnr k2h"]
        self.assertEqual(adopt["verdict"], "undecided")             # no tide model, no rough sea
        self.assertIs(adopt["tests"][0]["passed"], True)            # but it beats the spline's 6 cm
        self.assertIn("ADOPTION RULE", text)
        self.assertTrue((self.out / f"report_{tag}.txt").exists())
        head = (self.out / f"series_{tag}.csv").read_text().splitlines()
        self.assertEqual(len(head), 1 + 3 * 48)
        self.assertIn("invsnr_k2h_level", head[0])
        # the SNR copies are removed afterwards; --compare-only needs no gnssrefl at all
        self.assertFalse(any((self.out / "refl_code_dec5").rglob("*.snr66*")))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ev.main(argv[:-3] + ["--offline", "--compare-only"]), 0)
        self.assertIn("3 of 3 days ran", buf.getvalue())

    def test_out_dir_inside_the_live_refl_code_is_refused(self):
        sj = self._station_files(date(2026, 9, 21))
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                ev.main(["--window", "2026-09-20", "--station-json", str(sj), "--refl-code", str(self.live),
                         "--out-dir", str(self.live / "Files" / "x")])


# ---------------------------------------------------------------------
# gnssrefl's real invsnr
# ---------------------------------------------------------------------

@unittest.skipIf(importlib.util.find_spec("gnssrefl") is None, "gnssrefl is not installed")
class RealInvsnrTests(unittest.TestCase):

    def test_real_invsnr_recovers_a_known_tide(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live = tmp / "refl_code"
            days = [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
            for d in days:
                write_live_snr(live, d)
            before = tree_digest(live)
            s = ev.InvsnrSettings(station=STA, knot_space_h=2.0)
            run = ev.plan_runs([(days[1], days[1])], 1)[0]
            ctx = ev.RunContext(live_refl=live, out_dir=tmp / "out", lat=41.89, lon=-69.96, ht=-10.02,
                                python=sys.executable, timeout_s=1800)
            res = ev.execute_run(run, s, ctx)
            log = (ctx.run_dir(s, run) / "run.log").read_text()
            self.assertEqual(res["status"], "ok", res.get("message", "") + log[-3000:])
            self.assertEqual(tree_digest(live), before)
            ep, lv, arcs = ev.invsnr_levels([(run, res)], s, ctx, HORTHO)
            self.assertEqual(len(ep), 288)
            err = ev.stats(lv, tide(ep))
            self.assertLess(err["rmse"], 0.05, err)
            self.assertGreater(len(arcs), 50)
            self.assertIsNotNone(res["roughness"])
            self.assertTrue(res["fits"][-1]["success"])


if __name__ == "__main__":
    unittest.main()
