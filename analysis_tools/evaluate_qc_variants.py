#!/usr/bin/env python3
"""
evaluate_qc_variants.py

Arc-level QC settings tried on the station's OWN results and scored
against the Chatham gauge, before any of them is switched on in
station.json -- with a recommendation, by the rule given below.

WHY

The project's QC (water_level_qc.py, and gnssr_qc.py in the waterline
pipeline) sees only gnssrefl's 30-minute spline, never the arcs. Inside
gnssrefl every arc test is relative to the data themselves: a 2.5-sigma
clip per UTC day (mostly tide, so it rarely fires), and a 3-sigma clip
against a first spline, sigma being one plain standard deviation over
the whole ~10-month fit. In the 25-27 Sep 2026 storm the spline read up
to ~3 m above what the Chatham gauge implied: the arcs that survive a
rough sea are the biased ones. gnssrefl has three knobs for this, each
now an opt-in station.json key:

  -alt_sigma         part 1 uses the median and 1.4826 x the median
                     absolute deviation (the robust sigma gnssrefl
                     credits to F. Geremia-Nievinski) instead of the
                     mean and standard deviation
                         -> "gnssrefl_subdaily_alt_sigma"
  -spline_outlier1/2 fixed limits in metres for the two spline clips
                     instead of 3 sigma
                         -> "gnssrefl_subdaily_spline_outlier1_m"/"_2_m"
  reqAmp per frequency
                     one amplitude threshold per signal instead of one
                     for all (amplitude_thresholds.py)
                         -> "gnssrefl_amplitude_min_by_frequency"

A synthetic reproduction of that storm with gnssrefl's real subdaily
found that fixed limits of 0.30/0.25 m with alt_sigma did NOT remove a
sustained storm bias (+1.65 vs +1.68 m at high water): the limits are
relative to a spline that follows the bias. They also cost 12% of calm
arcs, because the residual the first clip tests had a scatter of about
0.2 m there, so 0.30 m was only 1.5 sigma (THE TWO CLIPS). Whether any
of the knobs helps on this station's real arcs is an empirical
question; this script answers it with the station's own results, the
way gnssrefl itself suggests trying strategies -- one -extension per
variant.

THE TWO CLIPS (gnssrefl 4.1.5, subdaily.py rhdot_correction2)

  first   A spline (-knots per day; the series mirrored a day at each
          end and every gap over 5 h bridged, sd_libs.flipit3) is
          fitted to the part-1 arcs' UNCORRECTED RH. Its slope gives
          each arc's RHdot correction (Larson et al. 2013, 2017), and
          an arc is dropped if
              |RHdot-corrected RH - that spline| > limit,
          limit = 3 x the standard deviation of those residuals over the
          whole fit, or -spline_outlier1. There is no inter-frequency
          correction yet, and the spline was fitted before the RHdot
          correction, so this residual carries the first spline's
          misfit and each signal's bias: it is always wider than the
          final arcs' scatter (0.12 m against 0.08 m on a synthetic
          station with 0.08 m of noise). The arcs that pass are what
          the published spline is fitted on.
  second  After the inter-frequency correction a new spline is fitted
          to those arcs, and an arc is dropped if |IF-corrected RH -
          that spline| > 3 x their standard deviation, or
          -spline_outlier2. It decides the edit and outlier files and
          where gaps fall, not the published spline.

  So abs_outliers takes each limit from the residual its own clip
  tests, in the baseline's calm week:
    limit 1  OUTLIER_SIGMAS x the robust scale of the first-clip
             residuals (first_clip_scatter): gnssrefl's first spline
             rebuilt from the baseline's part-1 edit files
             (first_spline), and used only if it gives the RHdot-
             corrected RH of the baseline's .withrhdot file to within
             FIRST_SPLINE_TOL_M -- otherwise no limit is derived;
    limit 2  OUTLIER_SIGMAS x the robust scale of the baseline's final
             arcs about its spline (arc_scatter).
  The robust scale is 1.4826 x the median of |residual|: about zero,
  since both clips test |residual|, and normal-consistent like the
  1.4826 x MAD that -alt_sigma uses in part 1, so a storm in the span
  barely moves it. On a storm-free span, limit 1 then lands near the
  baseline's own 3-sigma limit and drops about as many calm arcs; with
  storms or bad days in the span it is tighter by as much as they
  widen the whole fit's standard deviation.

HOW

  work area  A private REFL_CODE (default products/refl_code_qc_eval;
             remove it with rm -rf). The production REFL_CODE is only
             read. Each variant is a gnssrefl extension in it:
               input/<sta>/<ext>/<sta>.json
                   the production json plus the variant's subdaily_*
                   keys and reqAmp -- what the station.json keys would
                   write there
               <year>/results/<sta>/<ext>/<doy>.txt
                   the production results (links); with per-frequency
                   amplitudes, exactly the arcs gnssir would have kept
                   (amplitude_thresholds.select_day, failQC arcs back in
                   where a threshold is lowered)
  fit        subdaily <sta> <year> ... -extension <ext> -subdir <sta>/eval
             -plt False plus last night's settings (record_status.json,
             else --settings) with the arc-QC options of the variant's
             json in place of last night's -- on the command line, as
             gnss_record.py puts them there (gnssrefl 4.1.5's subdaily
             never reads alt_sigma from its json).
  span       --span production (the default): the span of the nightly
             fit that wrote the windows' rows of the production record
             (record_fit), so the baseline reproduces it:
               a window in a frozen year -- the fit named in that
                 year's <sta>_<year>_spline_frozen.txt header ("from the
                 fit of A..B"; a year fitted by itself: that year);
               a window in the live part -- record_status.json's
                 fit_start..fit_end, the fit that last wrote the record
                 (also right on the day after a freeze, when tonight's
                 fit would start later);
               else (last night's update failed or used the fallback)
                 the span tonight's fit would cover.
             Widened to the windows if they reach past it. If the calm
             week and the storm were written by different fits, one run
             cannot reproduce both: it covers both fits and says so.
             --span windows: the windows +- PAD_DAYS only (faster, but
             subdaily's 3-sigma limits then come from that short span;
             the synthetic reproduction of the storm saw the first-clip
             sigma rise 18% with the storm in a 30-day span against 5%
             in a 122-day one).
             The report prints the baseline's largest difference from
             the production record in the windows: ~0 means it is the
             nightly fit.
  windows    The storm (default 25-27 Sep 2026) and a calm week: --calm,
             or the CALM_DAYS consecutive results days with the lowest
             mean buoy Hs within CALM_SEARCH_DAYS of the storm and at
             least CALM_CLEAR_DAYS away from it.
  reference  level = a * gauge(t - lag) + b, water_level_qc.fit_reference
             on the BASELINE spline in the calm week -- one transfer for
             every variant, so no variant can improve its score by moving
             the reference. Gauge: Chatham 8447435 (water_level_qc.
             load_gauge, the station's caches copied into the work area).
             Without gauge data, the tide model in station.json
             (tide_model_file + tide_model_navd88_offset) is used the
             same way.
  scores     per window: 30-min readings, RMSE and bias against the
             reference, water_level_qc.run_qc's flags (as the nightly QC
             would give them), and the arcs: in the azimuth window ->
             after part 1 -> after the first spline clip (what the
             published spline is fitted on) -> after the second. In the
             storm also: the RMS of what wave setup cannot explain (a
             reading above the reference by no more than the Stockdon et
             al. 2006 setup allowance, water_level_qc.setup_allowance,
             counts as explained), the bias at high water (top quarter
             of the predicted levels), the largest excess and the
             longest stretch without a reading.
  variants   baseline         the production json as it is
             alt_sigma        + alt_sigma
             abs_outliers     + spline_outlier1/2 = OUTLIER_SIGMAS x the
                              robust scale of the residuals each clip
                              tests, in the baseline's calm week (THE
                              TWO CLIPS: what each clip's 3 sigma is in
                              calm weather); --outliers sets them
             fixed_outliers   + 0.30/0.25 m, the fixed limits tested in
                              the synthetic reproduction of the storm
             amp_by_freq      + per-frequency amplitudes from
                              amplitude_thresholds.derive on the calm
                              week: today's overall strictness spread
                              over the signals (--fraction), or the
                              thresholds of an amplitude_thresholds.py
                              --json file (--amp-json)
             amp_half         + per-frequency amplitudes at AMP_HALF of
                              each signal's calm median: an arc whose
                              reflection lost half its calm amplitude
                              (three quarters of its power) is dropped
             combined         alt_sigma + abs_outliers + amp_by_freq
             The amplitude variants are what gnssir would give with
             those thresholds on every day of the span; in production
             they act only on days processed after the change.
  rule       A variant is recommended only if, against the baseline:
               calm RMSE no worse than baseline + CALM_RMSE_TOL_M,
               calm arcs after the first clip >= CALM_ARCS_KEEP x baseline,
               storm readings >= STORM_READINGS_KEEP x baseline,
               no new gap of GAP_VISIBLE_H or more in either window (subdaily
                 drops the rows there: a visible break in the plot),
               storm RMS beyond wave setup <= (1 - STORM_GAIN) x baseline.
             Of those, the one with the lowest storm RMS beyond setup.
             Otherwise: keep the current settings. One storm and one calm
             week are a small sample, and switching a key on changes the
             plotted water levels (never the plot's format) in storms --
             the owner's decision.

Usage (on the station, gnssrefl_venv active; a few minutes per variant
with --span production):
    cd /home/argus_user/GNSS/v4.1 && source gnssrefl_venv/bin/activate
    python3 analysis_tools/evaluate_qc_variants.py --station usgs
    ... --calm 2026-09-08 2026-09-14 --storm 2026-09-25 2026-09-27
    ... --span windows --variants baseline,alt_sigma
The summary is also written to <work>/qc_eval_summary.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import amplitude_thresholds as at  # noqa: E402
import gnss_record as gr  # noqa: E402
import water_level_qc as wq  # noqa: E402
from station_datum import load_station, spline_shift, tide_model_to_navd88  # noqa: E402

DEFAULT_SETTINGS = "-rhdot True -knots 8 -azim1 35 -azim2 125"   # process_and_plot.sh
STORM = (date(2026, 9, 25), date(2026, 9, 27))
CALM_DAYS = 7              # length of an automatic calm window
CALM_SEARCH_DAYS = 60      # ... looked for this close to the storm
CALM_CLEAR_DAYS = 3        # ... and at least this far from it
CALM_WAVE_COVER = 0.8      # ... with buoy Hs for this fraction of its hours
PAD_DAYS = 3               # --span windows: days fitted either side of the windows
WORK_NAME = "refl_code_qc_eval"
EVAL_SUBDIR = "eval"       # subdaily -subdir <sta>/eval
QC_OPTIONS = ("-alt_sigma", "-spline_outlier1", "-spline_outlier2")
OUTLIER_SIGMAS = 3.0
FIXED_OUTLIERS = (0.30, 0.25)   # the fixed limits tested in the synthetic storm reproduction
FIRST_SPLINE_TOL_M = 0.002     # rebuilt first spline vs the .withrhdot file (it prints mm)
DEFAULT_KNOTS = 8              # subdaily's knots per day when neither settings nor json say
CALM_RMSE_TOL_M = 0.005
CALM_ARCS_KEEP = 0.90
STORM_READINGS_KEEP = 0.90
STORM_GAIN = 0.10
GAP_VISIBLE_H = 6.0        # subdaily's gap_min_val: a gap this long is a break in the plot
MJD_UNIX = 40587.0         # MJD of 1970-01-01
AMP_HALF = 0.5             # amp_half: half of each signal's calm median amplitude
VARIANT_NAMES = ("baseline", "alt_sigma", "abs_outliers", "fixed_outliers",
                 "amp_by_freq", "amp_half", "combined")


@dataclass
class Variant:
    """One arc-QC setting: None leaves the production json's value."""
    name: str
    about: str
    alt_sigma: bool | None = None
    outlier1: float | None = None
    outlier2: float | None = None
    amp: dict | None = None             # {frequency: amplitude threshold}

    @property
    def ext(self) -> str:
        return f"qc_{self.name}"

    def station_json(self) -> dict:
        """The station.json keys that switch this variant on."""
        out = {}
        if self.alt_sigma is not None:
            out["gnssrefl_subdaily_alt_sigma"] = self.alt_sigma
        if self.outlier1 is not None:
            out["gnssrefl_subdaily_spline_outlier1_m"] = self.outlier1
        if self.outlier2 is not None:
            out["gnssrefl_subdaily_spline_outlier2_m"] = self.outlier2
        if self.amp:
            out.update(at.station_json_entry(self.amp))
        return out


# ----------------------------------------------------------------
# Settings, windows, span
# ----------------------------------------------------------------

def strip_options(settings: str, names=QC_OPTIONS) -> str:
    """settings without these options and their values."""
    a, out, i = shlex.split(settings), [], 0
    while i < len(a):
        if a[i] in names:
            i += 2
            continue
        out.append(a[i])
        i += 1
    return " ".join(out)


def epoch(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def window_epochs(w) -> tuple[float, float]:
    """[first day 00:00, day after the last 00:00) UTC, in seconds."""
    return epoch(w[0]), epoch(w[1] + timedelta(days=1))


def mean_hs(waves, w) -> tuple[float, float, float]:
    """(mean Hs, max Hs, fraction of the window's hours with a record)."""
    if waves is None or not len(waves[0]):
        return float("nan"), float("nan"), 0.0
    t0, t1 = window_epochs(w)
    m = (waves[0] >= t0) & (waves[0] < t1)
    if not m.any():
        return float("nan"), float("nan"), 0.0
    hours = len(np.unique(np.floor(waves[0][m] / 3600)))
    return float(np.mean(waves[1][m])), float(np.max(waves[1][m])), hours / ((t1 - t0) / 3600)


def pick_calm(days, storm, waves) -> tuple[tuple[date, date], str]:
    """The calmest CALM_DAYS consecutive results days near the storm
    (see "windows" in the module docstring), and why it was chosen."""
    have = set(days)
    lo = storm[0] - timedelta(days=CALM_SEARCH_DAYS)
    hi = storm[1] + timedelta(days=CALM_SEARCH_DAYS)
    best = None
    for d0 in days:
        w = (d0, d0 + timedelta(days=CALM_DAYS - 1))
        if w[0] < lo or w[1] > hi:
            continue
        if any(d0 + timedelta(days=k) not in have for k in range(CALM_DAYS)):
            continue
        if not (w[1] < storm[0] - timedelta(days=CALM_CLEAR_DAYS)
                or w[0] > storm[1] + timedelta(days=CALM_CLEAR_DAYS)):
            continue
        hs, _, cover = mean_hs(waves, w)
        if cover >= CALM_WAVE_COVER and (best is None or hs < best[0]):
            best = (hs, w)
    if best is not None:
        return best[1], f"the calmest {CALM_DAYS} days with buoy data near the storm"
    end = storm[0] - timedelta(days=CALM_CLEAR_DAYS + 1)
    w = (end - timedelta(days=CALM_DAYS - 1), end)
    return w, (f"no buoy record to choose by: the {CALM_DAYS} days ending "
               f"{CALM_CLEAR_DAYS + 1} days before the storm -- check they were calm")


# gnss_record.frozen_text's note on a year frozen from a nightly fit
FROZEN_FIT = re.compile(r"from the fit of (\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


def _status_fit(files_dir: Path) -> tuple[date, date] | None:
    """fit_start..fit_end of the last update, if it succeeded without the
    fallback: the fit that wrote the live part of the record."""
    try:
        st = json.loads((files_dir / gr.FIT_SUBDIR / gr.STATUS_NAME).read_text())
        if st.get("ok") and not st.get("fallback"):
            return date.fromisoformat(st["fit_start"]), date.fromisoformat(st["fit_end"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def record_fit(refl_code: Path, sta: str, plan, w) -> tuple[tuple[date, date], str]:
    """The span of the fit that wrote window w's rows of the production
    record, and where that comes from ("span" in the module docstring):
    a frozen year's header, record_status.json, else tonight's plan."""
    files_dir = Path(refl_code) / "Files" / sta
    year = w[0].year
    if year in plan.frozen:
        head, _ = gr.read_spline(gr.frozen_path(files_dir, sta, year))
        for line in head:
            m = FROZEN_FIT.search(line)
            if m:
                return ((date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))),
                        f"{year} frozen from that fit")
            if f"from a fit of {year} by itself" in line:
                days = [d for d in plan.days if d.year == year]
                return (days[0], days[-1]), f"{year} frozen from a fit of it by itself"
    elif year >= plan.first_live:
        fit = _status_fit(files_dir)
        if fit is not None:
            return fit, "the last nightly update (record_status.json)"
    return (plan.start, plan.end), "the span tonight's fit would cover"


def eval_span(mode, plan, windows, newest, fits=None) -> tuple[date, date]:
    """The days the variants are fitted over. production: the fits that
    wrote the windows (record_fit; tonight's plan without them), widened
    to the windows if they reach past it; windows: the windows +-
    PAD_DAYS."""
    lo, hi = min(w[0] for w in windows), max(w[1] for w in windows)
    if mode == "production" and plan is not None:
        spans = fits or [(plan.start, plan.end)]
        first = min([lo] + [f[0] for f in spans])
        last = max([hi] + [f[1] for f in spans])
    else:
        first, last = lo - timedelta(days=PAD_DAYS), hi + timedelta(days=PAD_DAYS)
    return first, min(last, newest)


# ----------------------------------------------------------------
# One variant: stage, fit, read
# ----------------------------------------------------------------

def out_dir(work: Path, sta: str, v: Variant) -> Path:
    return work / "Files" / sta / EVAL_SUBDIR / v.ext


def variant_json(v: Variant, cfg: dict) -> dict:
    """The production gnssir json with the variant's keys, as the
    station.json keys would write it (gnssrefl_processor.py)."""
    vcfg = json.loads(json.dumps(cfg))
    if v.alt_sigma is not None:
        vcfg["subdaily_alt_sigma"] = v.alt_sigma
    if v.outlier1 is not None:
        vcfg["subdaily_spline_outlier1"] = v.outlier1
    if v.outlier2 is not None:
        vcfg["subdaily_spline_outlier2"] = v.outlier2
    current = at.current_thresholds(cfg)
    if v.amp:
        vcfg["reqAmp"] = [v.amp.get(f, current[f]) for f in current]
    return vcfg


def stage(refl_code: Path, work: Path, sta: str, v: Variant, days, cfg: dict) -> None:
    """The variant's json and results in the work REFL_CODE (see "work
    area" in the module docstring). Last run's staging is replaced."""
    current = at.current_thresholds(cfg)
    jdir = work / "input" / sta / v.ext
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / f"{sta}.json").write_text(json.dumps(variant_json(v, cfg), indent=4))
    for ydir in work.glob("[0-9][0-9][0-9][0-9]"):
        shutil.rmtree(ydir / "results" / sta / v.ext, ignore_errors=True)
    for d in days:
        src, _ = at.day_paths(refl_code, sta, d)
        dst = work / str(d.year) / "results" / sta / v.ext / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not v.amp:
            try:
                os.symlink(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
            continue
        with open(src, errors="replace") as f:
            head = [line for line in f if line.startswith("%")]
        lines = at.select_day(at.day_arcs(refl_code, sta, d, cfg), v.amp, current)
        dst.write_text("".join(head + lines))


def variant_settings(settings: str, v: Variant, cfg: dict) -> str:
    """The subdaily options of one variant: the production settings (no
    arc-QC options) plus its json's, on the command line exactly as
    gnss_record.py puts them there each night -- subdaily 4.1.5 would
    not read alt_sigma from the json (gnss_record.json_qc_options)."""
    qc = gr.qc_options(variant_json(v, cfg))
    return f"{settings} {qc}" if qc else settings


def run_variant(work: Path, sta: str, v: Variant, days, settings: str) -> tuple[bool, float]:
    """subdaily for one variant (settings: variant_settings()); (wrote a
    spline, seconds)."""
    y1, d1, y2, d2 = gr.subdaily_range(days)
    out = out_dir(work, sta, v)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for sub in ("orbits", "exe"):
        (work / sub).mkdir(exist_ok=True)
    cmd = ["subdaily", sta, str(y1), "-doy1", str(d1), "-year_end", str(y2), "-doy2", str(d2),
           "-subdir", f"{sta}/{EVAL_SUBDIR}", "-extension", v.ext, "-plt", "False"
           ] + shlex.split(settings)
    env = dict(os.environ, REFL_CODE=str(work), ORBITS=str(work / "orbits"),
               EXE=str(work / "exe"), MPLBACKEND="Agg")
    t = time.monotonic()
    with open(out / "subdaily.log", "w") as f:
        f.write(" ".join(cmd) + "\n")
        f.flush()
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
    spline = out / f"{sta}_spline_out.txt"
    return spline.exists() and bool(gr.read_spline(spline)[1]), time.monotonic() - t


def read_levels(path: Path):
    """(epochs, levels on NAVD88, spline RH) of a spline file; 999 gaps left out."""
    shift = spline_shift(path, quiet=True)
    ep, lv, rh = [], [], []
    for m, line in gr.read_spline(path)[1]:
        c = line.split()
        v = float(c[8])
        if abs(v) > 900:
            continue
        ep.append((m - MJD_UNIX) * 86400.0)
        lv.append(v + shift)
        rh.append(float(c[1]))
    o = np.argsort(ep)
    return np.asarray(ep)[o], np.asarray(lv)[o], np.asarray(rh)[o]


def _mjd_rows(paths):
    """The numbers of every row of gnssir-format files (results, the
    subdaily edit files; column 16 is the MJD in all of them)."""
    out = []
    for p in paths:
        try:
            with open(p, errors="replace") as f:
                for line in f:
                    c = at.parse(line)
                    if c is not None:
                        out.append(c)
        except OSError:
            continue
    return out


def arc_counts(work: Path, sta: str, v: Variant, w, azim) -> dict:
    """Arcs of the window at each step (see "scores")."""
    t0, t1 = (e / 86400.0 + MJD_UNIX for e in window_epochs(w))
    out_ = out_dir(work, sta, v)
    steps = {
        "window": sorted(work.glob(f"[0-9][0-9][0-9][0-9]/results/{sta}/{v.ext}/[0-9][0-9][0-9].txt")),
        "part1": sorted(out_.glob(f"{sta}_*_subdaily_edit.txt")),
        "stage1": sorted(out_.glob("*_subdaily_edit.txt.withrhdot")),
        "stage2": sorted(out_.glob("*_subdaily_edit.txt.withrhdotIF")),
    }
    counts = {}
    for name, paths in steps.items():
        n = 0
        for c in _mjd_rows(paths):
            if not (t0 <= c[at.MJD_COL] < t1):
                continue
            if name == "window" and azim is not None and not (azim[0] <= c[at.AZ_COL] <= azim[1]):
                continue
            n += 1
        counts[name] = n
    return counts


def robust_scale(r) -> float:
    """1.4826 x the median of |r|: the normal-consistent robust sigma,
    taken about ZERO because both spline clips test |residual| (THE TWO
    CLIPS). NaN for no values."""
    r = np.asarray(r, dtype=float)
    r = r[np.isfinite(r)]
    return float(1.4826 * np.median(np.abs(r))) if len(r) else float("nan")


def arc_scatter(work: Path, sta: str, v: Variant, w) -> float:
    """What the SECOND clip tests, in window w: the robust scale of the
    variant's final arcs (RH with the RHdot and inter-frequency
    corrections, column 25 of the .withrhdotIF file -- not column 3, the
    raw RH) about its spline RH. NaN without arcs."""
    out_ = out_dir(work, sta, v)
    sp = [(m, float(line.split()[1])) for m, line in
          gr.read_spline(out_ / f"{sta}_spline_out.txt")[1] if abs(float(line.split()[8])) < 900]
    if len(sp) < 2:
        return float("nan")
    sm, srh = np.array([s[0] for s in sp]), np.array([s[1] for s in sp])
    t0, t1 = (e / 86400.0 + MJD_UNIX for e in window_epochs(w))
    rows = [c for c in _mjd_rows(out_.glob("*_subdaily_edit.txt.withrhdotIF"))
            if len(c) > 24 and t0 <= c[at.MJD_COL] < t1]
    if not rows:
        return float("nan")
    m = np.array([c[at.MJD_COL] for c in rows])
    rh = np.array([c[24] for c in rows])
    j = np.clip(np.searchsorted(sm, m), 1, len(sm) - 1)
    near = (sm[j] - sm[j - 1]) <= 1.0 / 24 + 1e-9         # no spline gap around the arc
    return robust_scale(rh[near] - np.interp(m[near], sm, srh))


def subdaily_option(settings: str, name: str, default=None):
    """The value given to one subdaily option in a settings string."""
    a = shlex.split(settings)
    for i in range(len(a) - 1):
        if a[i] == name:
            return a[i + 1]
    return default


def _subdaily_bool(value) -> bool:
    """A boolean subdaily option as gnssrefl reads it (utils.str2bool)."""
    return str(value).strip().lower() in ("yes", "true", "t", "y", "1")


def flipit3(t, y):
    """gnssrefl 4.1.5's sd_libs.flipit3, rule for rule: what subdaily fits
    its splines to. The first day's values are mirrored before the series
    (at 2 t0 - t), the last day's put after it in reverse order (at
    t + 1 day), and every gap over 5 hours is bridged with hourly values
    on the straight line across it. As in gnssrefl the earliest point of
    the mirrored series is not kept, nor the point that ends a gap. t
    sorted (MJD)."""
    day0, day_n = t[0], t[-1]
    left, right = t < day0 + 1, t > day_n - 1
    th = np.hstack((np.hstack((-(t[left] - day0), t - day0)), t[right] - day0 + 1))
    h = np.hstack((np.hstack((y[left], y)), np.flip(y[right])))
    o = np.argsort(th)
    th, h = th[o] + day0, h[o]
    fill, gap = 1 / 24, 5 / 24
    tt, yy = [], []
    for i in range(1, len(th)):
        if th[i] - th[i - 1] > gap:
            fake = np.arange(th[i - 1] + fill, th[i], fill)
            tt.extend(fake)
            yy.extend(np.interp(fake, th[i - 1:i + 1], h[i - 1:i + 1]))
        else:
            tt.append(th[i])
            yy.append(h[i])
    tt, yy = np.array(tt), np.array(yy)
    o = np.argsort(tt)
    return tt[o], yy[o]


def first_spline(edit_files, knots: int, apply_rhdot: bool = True):
    """gnssrefl 4.1.5's FIRST spline (subdaily.py rhdot_correction2),
    rebuilt from the part-1 edit files <sta>_<year>_subdaily_edit.txt
    it is fitted on: the arcs stacked in year order and sorted by MJD,
    a least-squares cubic spline with knots per day (the first and last
    15 minutes in) through flipit3 of their UNCORRECTED RH (column 3),
    its slope on a 3-minute grid as RHdot, the correction EdotF x RHdot
    (column 13; Larson et al. 2013), and the residual the first clip
    tests: RHdot-corrected RH - spline (THE TWO CLIPS). Returns
    (arcs, corrected RH, residual), arcs being the stacked rows; None
    without arcs. Needs scipy (part of gnssrefl)."""
    from scipy import interpolate
    rows = _mjd_rows(edit_files)
    if len(rows) < 2:
        return None
    width = min(len(c) for c in rows)
    tvd = np.array([c[:width] for c in rows])
    tvd = tvd[np.argsort(tvd[:, at.MJD_COL]).T, :]
    th, h, xfac = tvd[:, at.MJD_COL], tvd[:, 2], tvd[:, 12]
    tnew, ynew = flipit3(th, h)
    n_days = tnew.max() - tnew.min()
    first_knot = 15 / 60 / 24
    kn = np.linspace(tnew.min() + first_knot, tnew.max() - first_knot, num=int(knots * n_days))
    t, c, k = interpolate.splrep(tnew, ynew, s=0, k=3, t=kn, task=-1)
    spline = interpolate.BSpline(t, c, k, extrapolate=False)
    per_day = 24 * 20                                    # every 3 minutes
    n = int(n_days * per_day)
    xx = np.linspace(tnew.min(), tnew.max(), n)
    rhdot = np.interp(th, xx[1:n], per_day / 24 * np.diff(spline(xx)))
    corrected = h - xfac * rhdot if apply_rhdot else h
    return tvd, corrected, corrected - spline(th)


def _arc_key(c) -> tuple:
    """An arc in every subdaily file: (MJD to the microday, satellite, frequency)."""
    return (int(round(c[at.MJD_COL] * 1e6)), int(c[3]), int(c[at.FREQ_COL]))


def first_clip_scatter(work: Path, sta: str, v: Variant, w, settings: str,
                       cfg: dict) -> tuple[float, str]:
    """What the FIRST clip tests, in window w: the robust scale of the
    residuals of first_spline() on the variant's own part-1 edit files,
    with its knots (-knots, else the json's subdaily_knots, else
    DEFAULT_KNOTS) and -apply_rhdot. The rebuilt spline must give the
    RHdot-corrected RH (column 23) of every arc in the variant's
    .withrhdot file to within FIRST_SPLINE_TOL_M; if it does not -- or
    there is no first clip -- (NaN, why). Else (scale, what was checked)."""
    if not _subdaily_bool(subdaily_option(settings, "-rhdot", "True")):
        return float("nan"), "subdaily runs no spline clips with -rhdot False"
    out_ = out_dir(work, sta, v)
    edits = sorted(out_.glob(f"{sta}_[0-9][0-9][0-9][0-9]_subdaily_edit.txt"))
    kept = _mjd_rows(out_.glob("*_subdaily_edit.txt.withrhdot"))
    if not edits or not kept:
        return float("nan"), f"no part-1 edit or .withrhdot file in {out_}"
    knots = subdaily_option(settings, "-knots") or cfg.get("subdaily_knots") or DEFAULT_KNOTS
    try:
        got = first_spline(edits, int(knots),
                           _subdaily_bool(subdaily_option(settings, "-apply_rhdot", "True")))
    except ImportError:
        return float("nan"), "scipy is not installed (activate gnssrefl_venv)"
    except Exception as exc:              # a spline gnssrefl itself could fit; say why not
        return float("nan"), f"could not rebuild gnssrefl's first spline: {exc}"
    if got is None:
        return float("nan"), "no arcs in the part-1 edit files"
    arcs, corrected, residual = got
    index = {_arc_key(c): i for i, c in enumerate(arcs)}
    pairs = [(index[_arc_key(c)], c[22]) for c in kept if len(c) > 22 and _arc_key(c) in index]
    if len(pairs) < len(kept):
        return float("nan"), (f"{len(kept) - len(pairs)} of the {len(kept)} arcs of the "
                              f".withrhdot file are not in the part-1 edit files")
    diff = max(abs(corrected[i] - x) for i, x in pairs)
    if not diff <= FIRST_SPLINE_TOL_M:
        return float("nan"), (f"the rebuilt first spline does not reproduce gnssrefl's RHdot "
                              f"correction (max |difference| {diff:.3f} m over {len(pairs)} arcs)")
    t0, t1 = (e / 86400.0 + MJD_UNIX for e in window_epochs(w))
    inw = (arcs[:, at.MJD_COL] >= t0) & (arcs[:, at.MJD_COL] < t1)
    return robust_scale(residual[inw]), (f"gnssrefl's first spline rebuilt: RHdot-corrected RH "
                                         f"within {diff * 1000:.1f} mm of the .withrhdot file "
                                         f"for all {len(pairs)} arcs")


# ----------------------------------------------------------------
# Reference and scores
# ----------------------------------------------------------------

def _copy_cache(files_dir: Path, work: Path, name: str) -> Path:
    """A private copy of one of the nightly QC's caches (updated each run),
    so downloads merge into the work area, not production."""
    src, dst = files_dir / name, work / name
    if src.exists():
        shutil.copyfile(src, dst)
    return dst


def load_reference(work: Path, files_dir: Path, start: datetime, end: datetime,
                   station: dict, need) -> tuple:
    """(reference epochs, levels) and its name: the Chatham gauge, else the
    tide model in station.json; (None, why) if neither covers `need`
    (t0, t1)."""
    def covers(ref):
        return ref is not None and bool(np.any((ref[0] >= need[0]) & (ref[0] < need[1])))
    gauge = wq.load_gauge(_copy_cache(files_dir, work, f"gauge_{wq.GAUGE_STATION}.csv"), start, end)
    if covers(gauge):
        return gauge, f"Chatham gauge {wq.GAUGE_STATION}"
    tide_file = station.get("tide_model_file")
    if tide_file and Path(tide_file).exists():
        from plot_7day import load_tide
        t, v = load_tide(Path(tide_file), station.get("tide_model_time_column") or "time",
                         station.get("tide_model_value_column") or "")
        if t:
            ep = np.array([x.replace(tzinfo=timezone.utc).timestamp() for x in t])
            lv = np.asarray(v, dtype=float) + tide_model_to_navd88(station)
            o = np.argsort(ep)
            grid = np.arange(ep[o][0], ep[o][-1] + 1.0, 360.0)   # reference_at gaps > 12 min
            ref = (grid, np.interp(grid, ep[o], lv[o]))
            if covers(ref):
                return ref, f"tide model {Path(tide_file).name} (no gauge data)"
    return None, "no gauge data and no tide model covering the calm week"


def score_window(ep, lv, ref, fit, waves, w) -> dict:
    """Readings, RMSE, bias and the storm measures of one window."""
    t0, t1 = window_epochs(w)
    a, lag, b, _ = fit
    m = (ep >= t0) & (ep < t1)
    pred = np.full(len(ep), np.nan)
    pred[m] = a * wq.reference_at(ref[0], ref[1], ep[m] - lag) + b
    ok = m & np.isfinite(pred)
    r = lv[ok] - pred[ok]
    stamps = np.concatenate([[t0], ep[m], [t1]])
    out = {"readings": int(m.sum()), "slots": int(round((t1 - t0) / 1800)),
           "compared": int(ok.sum()),
           "longest_gap_h": float(np.max(np.diff(stamps)) / 3600.0)}
    if not len(r):
        return dict(out, rmse=float("nan"), bias=float("nan"), max=float("nan"),
                    beyond_setup_rms=float("nan"), high_water_bias=float("nan"))
    setup = wq.setup_allowance(ep[ok], waves)
    beyond = r - np.clip(r, 0.0, setup)                  # what wave setup cannot explain
    high = pred[ok] >= np.percentile(pred[ok], 75)
    return dict(out, rmse=float(np.sqrt(np.mean(r ** 2))), bias=float(np.mean(r)),
                max=float(np.max(r)), beyond_setup_rms=float(np.sqrt(np.mean(beyond ** 2))),
                high_water_bias=float(np.mean(r[high])))


def qc_window(ep, lv, ref, waves, w) -> dict:
    """water_level_qc.run_qc as the nightly QC runs it (the readings of the
    REF_FIT_DAYS before the window's end), counted in the window."""
    t0, t1 = window_epochs(w)
    sel = (ep >= t1 - wq.REF_FIT_DAYS * 86400) & (ep < t1)
    if sel.sum() < 2:
        return {"failed": 0, "runup": 0, "setup": 0, "fit": None}
    flags, reasons, fit = wq.run_qc(ep[sel], lv[sel], ref, waves)
    inw = ep[sel] >= t0
    rs = np.array(reasons, dtype=object)[inw]
    return {"failed": int((flags[inw] >= wq.FAIL).sum()),
            "runup": int(sum("possible_runup" in r for r in rs)),
            "setup": int(sum("possible_setup" in r for r in rs)),
            "fit": None if fit is None else [round(float(x), 4) for x in fit]}


def judge(base: dict, cand: dict) -> list:
    """Why `cand` is not recommended over `base` (THE RULE); [] if it is."""
    why = []
    bc, cc, bs, cs = base["calm"], cand["calm"], base["storm"], cand["storm"]
    if not cc["rmse"] <= bc["rmse"] + CALM_RMSE_TOL_M:
        why.append(f"calm RMSE {cc['rmse']:.3f} m, more than {CALM_RMSE_TOL_M * 1000:.0f} mm "
                   f"worse than {bc['rmse']:.3f}")
    b1, c1 = base["arcs"]["calm"]["stage1"], cand["arcs"]["calm"]["stage1"]
    if not c1 >= CALM_ARCS_KEEP * b1:
        why.append(f"keeps {c1} of the baseline's {b1} calm arcs (under "
                   f"{CALM_ARCS_KEEP:.0%})")
    if not cs["readings"] >= STORM_READINGS_KEEP * bs["readings"]:
        why.append(f"{cs['readings']} storm readings against {bs['readings']} (new gaps)")
    for label, b, c in (("calm", bc, cc), ("storm", bs, cs)):
        gb, gc = b.get("longest_gap_h", 0.0), c.get("longest_gap_h", 0.0)
        if gc >= GAP_VISIBLE_H and gc > gb + 0.01:
            why.append(f"opens a {gc:.1f}-hour gap in the {label} window (baseline {gb:.1f} h): "
                       f"a break in the plot")
    if not bs["beyond_setup_rms"] > 0:
        why.append("the baseline has nothing beyond wave setup to remove")
    elif not cs["beyond_setup_rms"] <= (1 - STORM_GAIN) * bs["beyond_setup_rms"]:
        why.append(f"storm RMS beyond setup {cs['beyond_setup_rms']:.3f} m against "
                   f"{bs['beyond_setup_rms']:.3f} (needs {STORM_GAIN:.0%} lower)")
    return why


def recommend(results: dict) -> tuple:
    """(name of the recommended variant or None, {name: reasons})."""
    base = results["baseline"]
    reasons, ok = {}, []
    for name, r in results.items():
        if name == "baseline":
            continue
        if not r.get("ok"):
            reasons[name] = ["subdaily wrote no spline"]
            continue
        reasons[name] = judge(base, r)
        if not reasons[name]:
            ok.append(name)
    best = min(ok, key=lambda n: results[n]["storm"]["beyond_setup_rms"]) if ok else None
    return best, reasons


# ----------------------------------------------------------------
# Report
# ----------------------------------------------------------------

def _f(x, w=7, p=3, sign=False):
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return " " * (w - 1) + "-"
    return f"{x:+{w}.{p}f}" if sign else f"{x:{w}.{p}f}"


def print_report(results, variants, windows, calm_why, waves, ref_name, fit, span, settings,
                 repro) -> None:
    calm, storm = windows
    hs_c, _, _ = mean_hs(waves, calm)
    hs_s, hs_max, _ = mean_hs(waves, storm)
    print("=" * 78)
    print(f"  ARC-LEVEL QC VARIANTS, scored against the {ref_name}")
    print("=" * 78)
    print(f"  fit   : subdaily over {span[0]}..{span[1]}, {settings}")
    print(f"  calm  : {calm[0]}..{calm[1]} (mean Hs {_f(hs_c, 3, 1).strip()} m; {calm_why})")
    print(f"  storm : {storm[0]}..{storm[1]} (mean Hs {_f(hs_s, 3, 1).strip()} m, "
          f"max {_f(hs_max, 3, 1).strip()} m)")
    a, lag, b, sigma = fit
    print(f"  reference: level = {a:.3f} x reference(t {-lag / 60:+.0f} min) {b:+.3f} m, "
          f"sigma {sigma:.3f} m -- fitted on the baseline in the calm week, used for every variant")
    if repro is not None:
        print(f"  baseline against the production record in the windows: max |difference| "
              f"{repro:.3f} m" + ("" if repro < 0.01 else
                                 "  (not the nightly fit: other settings or results since, "
                                 "or windows from different fits)"))
    print()
    for v in variants:
        print(f"  {v.name:<16} {v.about}")
    for label, key in (("CALM", "calm"), ("STORM", "storm")):
        w = calm if key == "calm" else storm
        print()
        print(f"  {label} {w[0]}..{w[1]}")
        print(f"  {'variant':<16} {'read':>4} {'gap h':>5} {'RMSE':>6} {'bias':>7}"
              + (f" {'>setup':>6} {'HW bias':>7} {'max':>6}" if key == "storm" else "")
              + f" | {'arcs: azim':>10} {'part1':>6} {'clip1':>6} {'clip2':>6}"
              + f" | {'QC fail':>7}" + (f" {'runup':>5} {'setup':>5}" if key == "storm" else ""))
        for v in variants:
            r = results[v.name]
            if not r.get("ok"):
                print(f"  {v.name:<16} subdaily wrote no spline -- see {r.get('log')}")
                continue
            s, c, q = r[key], r["arcs"][key], r["qc"][key]
            print(f"  {v.name:<16} {s['readings']:>4} {s['longest_gap_h']:>5.1f} "
                  f"{_f(s['rmse'], 6)} {_f(s['bias'], 7, 3, True)}"
                  + (f" {_f(s['beyond_setup_rms'], 6)} {_f(s['high_water_bias'], 7, 3, True)}"
                     f" {_f(s['max'], 6, 2, True)}" if key == "storm" else "")
                  + f" | {c['window']:>10} {c['part1']:>6} {c['stage1']:>6} {c['stage2']:>6}"
                  + f" | {q['failed']:>7}" + (f" {q['runup']:>5} {q['setup']:>5}"
                                              if key == "storm" else ""))
    print()
    print("  read = 30-min readings (of " + str(results["baseline"]["calm"]["slots"]) +
          " in the calm week); gap h = longest stretch without one; >setup = RMS of what")
    print("  Stockdon wave setup cannot explain; HW bias = bias at the top quarter of predicted")
    print("  levels; arcs: in the azimuth window -> after part 1 -> after the first spline clip")
    print("  (what the spline is fitted on) -> after the second; QC = water_level_qc.run_qc.")


def print_recommendation(results, variants, best, reasons) -> None:
    print()
    print("  RECOMMENDATION")
    print(f"  (rule: calm RMSE within {CALM_RMSE_TOL_M * 1000:.0f} mm of the baseline, "
          f">= {CALM_ARCS_KEEP:.0%} of its calm arcs, >= {STORM_READINGS_KEEP:.0%} of its storm "
          f"readings, no new gap of {GAP_VISIBLE_H:g} h, storm RMS beyond setup "
          f">= {STORM_GAIN:.0%} lower)")
    for v in variants:
        if v.name in reasons:
            print(f"    {v.name:<16} " + ("meets the rule" if not reasons[v.name]
                                         else "no: " + "; ".join(reasons[v.name])))
    if best is None:
        print("  -> Keep the current settings: no variant meets every condition.")
        return
    v = next(x for x in variants if x.name == best)
    b, c = results["baseline"]["storm"], results[best]["storm"]
    print(f"  -> {best}: storm RMS beyond wave setup {c['beyond_setup_rms']:.3f} m against "
          f"{b['beyond_setup_rms']:.3f}, high-water bias {c['high_water_bias']:+.2f} against "
          f"{b['high_water_bias']:+.2f} m.")
    print("     To switch it on, add to station/resources/station.json:")
    for k, val in v.station_json().items():
        print(f"       {json.dumps(k)}: {json.dumps(val)},")
    print("     It applies from the next nightly run that processes new data (the gnssir json is")
    print("     rewritten then).")
    if v.alt_sigma is not None or v.outlier1 is not None or v.outlier2 is not None:
        print("     The subdaily settings then refit the whole record, frozen years once.")
    if v.amp:
        print("     The amplitude thresholds act in gnssir, so only on days it processes from then")
        print("     on; this score is what they give once every day is processed with them. For the")
        print("     record to match, re-run gnssir on the earlier days (gnssir <sta> <year> <doy>")
        print("     -doy_end <doy>, gnssrefl_venv active, while their SNR files are still there).")
    print("     It changes the plotted water levels in storms, not the plot's format. One storm is")
    print("     a small sample: check the next one with this script before relying on it.")


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def build_variants(names, scatter, outliers, amp, amp_half=None) -> list:
    """The variants asked for, in VARIANT_NAMES order (baseline always).
    scatter = (first clip, second clip) robust scales of the baseline's
    calm week (THE TWO CLIPS); abs_outliers' limits are OUTLIER_SIGMAS x
    each, unless --outliers gives them."""
    s1, s2 = scatter
    derived = outliers is None
    if derived and all(math.isfinite(x) and x > 0 for x in (s1, s2)):
        outliers = (round(OUTLIER_SIGMAS * s1, 2), round(OUTLIER_SIGMAS * s2, 2))
    every = {"baseline": Variant("baseline", "the production json as it is"),
             "alt_sigma": Variant("alt_sigma", "part-1 clip with the median/MAD sigma "
                                  "(-alt_sigma)", alt_sigma=True),
             "fixed_outliers": Variant("fixed_outliers",
                                       "spline clips at fixed %.2f / %.2f m" % FIXED_OUTLIERS,
                                       outlier1=FIXED_OUTLIERS[0], outlier2=FIXED_OUTLIERS[1])}
    if outliers is not None:
        every["abs_outliers"] = Variant(
            "abs_outliers", f"spline clips at {outliers[0]:.2f} / {outliers[1]:.2f} m "
            f"({OUTLIER_SIGMAS:g} x the calm scale each clip tests, {s1:.3f} / {s2:.3f} m)"
            if derived else f"spline clips at {outliers[0]:.2f} / {outliers[1]:.2f} m (given)",
            outlier1=outliers[0], outlier2=outliers[1])
    if amp:
        every["amp_by_freq"] = Variant("amp_by_freq", "per-frequency amplitude thresholds "
                                       "(amplitude_thresholds.py)", amp=amp)
    if amp_half:
        every["amp_half"] = Variant("amp_half", f"per-frequency amplitude thresholds at "
                                    f"{AMP_HALF:g} x each signal's calm median", amp=amp_half)
    if outliers is not None and amp:
        every["combined"] = Variant("combined", "alt_sigma + abs_outliers + amp_by_freq",
                                    alt_sigma=True, outlier1=outliers[0], outlier2=outliers[1],
                                    amp=amp)
    return [every[n] for n in VARIANT_NAMES if n in every and (n == "baseline" or n in names)]


def read_amp_json(path) -> dict:
    """{frequency: threshold} of an amplitude_thresholds.py --json file
    (its "thresholds"), or of a file holding the station.json entry or the
    bare mapping. {} when no frequency had enough arcs for its own value.
    ValueError for anything else."""
    try:
        got = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    key = "gnssrefl_amplitude_min_by_frequency"
    if isinstance(got, dict) and "thresholds" in got:
        got = got["thresholds"]
    elif isinstance(got, dict) and isinstance(got.get("station_json"), dict) \
            and key in got["station_json"]:
        got = got["station_json"][key]
    elif isinstance(got, dict) and key in got:
        got = got[key]
    if not isinstance(got, dict):
        raise ValueError(f"{path} holds no frequency -> amplitude mapping")
    try:
        out = {int(k): float(v) for k, v in got.items()}
    except (TypeError, ValueError):
        raise ValueError(f"{path}: {got!r} is not a frequency -> amplitude mapping") from None
    if any(not v > 0 for v in out.values()):
        raise ValueError(f"{path}: every amplitude must be positive ({got!r})")
    return out


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, (np.floating, np.integer)):
        return _jsonable(x.item())
    return x


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refl-code", default=os.environ.get("REFL_CODE") or None,
                   help="the production REFL_CODE, only read (default: $REFL_CODE, else "
                        "products/refl_code)")
    p.add_argument("--station", required=True, help="gnssrefl 4-character station code")
    p.add_argument("--work", default=None,
                   help=f"private REFL_CODE for the variants (default: products/{WORK_NAME})")
    p.add_argument("--settings", default=None,
                   help="subdaily settings (default: last night's from record_status.json, "
                        f"else \"{DEFAULT_SETTINGS}\")")
    p.add_argument("--calm", type=date.fromisoformat, nargs=2, default=None,
                   metavar=("FIRST", "LAST"), help="calm window, UTC days (default: chosen)")
    p.add_argument("--storm", type=date.fromisoformat, nargs=2, default=list(STORM),
                   metavar=("FIRST", "LAST"), help="storm window (default: 2026-09-25 2026-09-27)")
    p.add_argument("--span", choices=("production", "windows"), default="production",
                   help="fit as the nightly fit does, or only the windows +- %d days" % PAD_DAYS)
    p.add_argument("--variants", default=",".join(VARIANT_NAMES),
                   help="comma-separated, of " + ", ".join(VARIANT_NAMES))
    p.add_argument("--outliers", type=float, nargs=2, default=None, metavar=("M1", "M2"),
                   help="abs_outliers' limits in metres (default: from the calm arc scatter)")
    p.add_argument("--fraction", type=float, default=None,
                   help="amp_by_freq: threshold = fraction x median (amplitude_thresholds.py)")
    p.add_argument("--amp-json", default=None,
                   help="amp_by_freq: thresholds from amplitude_thresholds.py --json")
    p.add_argument("--json", default=None, help="also write the summary here")
    a = p.parse_args()

    refl_code = Path(os.path.abspath(a.refl_code or gr.PROJECT_DIR / "products" / "refl_code"))
    sta = a.station
    work = Path(os.path.abspath(a.work or gr.PROJECT_DIR / "products" / WORK_NAME))
    if work.resolve() == refl_code.resolve():
        p.error("--work must not be the production REFL_CODE")
    names = {n.strip() for n in a.variants.split(",") if n.strip()}
    unknown = names - set(VARIANT_NAMES)
    if unknown:
        p.error(f"unknown variant(s) {sorted(unknown)}; choose from {', '.join(VARIANT_NAMES)}")
    amp_given = None
    if a.amp_json and names & {"amp_by_freq", "combined"}:
        try:                              # before any fit: a bad file must not cost a run
            amp_given = read_amp_json(a.amp_json)
        except ValueError as exc:
            print(f"  ERROR: --amp-json: {exc}")
            return 1
    if shutil.which("subdaily") is None:
        print("  ERROR: gnssrefl's subdaily is not on PATH (source gnssrefl_venv/bin/activate first)")
        return 1
    cfg = at.gnssir_json(refl_code, sta)
    if cfg is None:
        print(f"  ERROR: no gnssir json for {sta} under {refl_code}/input")
        return 1
    files_dir = refl_code / "Files" / sta
    settings = strip_options(a.settings or gr.status_settings(refl_code, sta) or DEFAULT_SETTINGS)
    azim = gr.azimuth_window(settings)
    try:
        since = gr.record_first_day()
    except ValueError:
        since = None
    days = list(gr.results_days(refl_code, sta, since))
    if not days:
        print("  ERROR: no results")
        return 1
    work.mkdir(parents=True, exist_ok=True)
    waves = wq.load_waves(_copy_cache(files_dir, work, f"waves_{wq.WAVE_STATION}.csv"))
    storm = (a.storm[0], a.storm[1])
    if a.calm:
        calm, calm_why = (a.calm[0], a.calm[1]), "given"
    else:
        calm, calm_why = pick_calm(days, storm, waves)
    windows = (calm, storm)
    for label, w in (("calm", calm), ("storm", storm)):
        n = sum(1 for d in days if w[0] <= d <= w[1])
        want = (w[1] - w[0]).days + 1
        if n == 0:
            print(f"  ERROR: no results in the {label} window {w[0]}..{w[1]}")
            return 1
        if n < want:
            print(f"  NOTE: {label} window {w[0]}..{w[1]} has results on {n} of {want} days")
    baseline = Variant("baseline", "the production json as it is")
    # the plan from the settings the nightly fit itself plans with: last
    # night's plus the json's arc QC (gnss_record.cmd_update). Without the
    # QC options, a frozen year made with them would not match, and the
    # span would reach back to the start of the record.
    plan = (gr.make_plan(refl_code, sta, variant_settings(settings, baseline, cfg), since)
            if a.span == "production" else None)
    fits = {}
    if plan is not None:
        for label, w in (("calm", calm), ("storm", storm)):
            fit_w, source = record_fit(refl_code, sta, plan, w)
            fits[label] = fit_w
            print(f"  {label} window {w[0]}..{w[1]}: written by the fit of {fit_w[0]}..{fit_w[1]} "
                  f"({source})")
        if len(set(fits.values())) > 1:
            print("  NOTE: the calm week and the storm were written by different nightly fits; one "
                  "run over both cannot reproduce both -- expect cm-level differences from the "
                  "record (give --calm in the storm's year to avoid it)")
    span = eval_span(a.span, plan, windows, days[-1], list(fits.values()))
    span_days = [d for d in days if span[0] <= d <= span[1]]
    try:
        gr.subdaily_range(span_days)
    except ValueError as exc:
        print(f"  ERROR: {exc}")
        return 1

    station = load_station()
    results, logs = {}, {}

    def run(v):
        print(f"  {v.name}: staging {len(span_days)} days, running subdaily ...", flush=True)
        stage(refl_code, work, sta, v, span_days, cfg)
        ok, secs = run_variant(work, sta, v, span_days, variant_settings(settings, v, cfg))
        logs[v.name] = str(out_dir(work, sta, v) / "subdaily.log")
        print(f"    {'ok' if ok else 'NO SPLINE'} ({secs:.0f} s)")
        results[v.name] = {"ok": ok, "log": logs[v.name], "station_json": v.station_json(),
                           "about": v.about}
        return ok

    if not run(baseline):
        print(f"  ERROR: the baseline fit failed -- see {logs['baseline']}")
        return 1
    # what each spline clip tests, in the baseline's calm week (THE TWO CLIPS)
    scatter1, first_check = first_clip_scatter(work, sta, baseline, calm, variant_settings(
        settings, baseline, cfg), cfg)
    scatter2 = arc_scatter(work, sta, baseline, calm)
    print("  calm scale the first clip tests: "
          + (f"{scatter1:.3f} m ({first_check})" if math.isfinite(scatter1)
             else f"not derived -- {first_check}"))
    print(f"  calm scale the second clip tests: {_f(scatter2, 5).strip()} m")
    amp = amp_half = None
    no_amp = "no frequency has enough arcs in the calm week for its own amplitude threshold"
    if names & {"amp_by_freq", "amp_half", "combined"}:
        calm_days = [d for d in days if calm[0] <= d <= calm[1]]
        amps = at.amplitudes(refl_code, sta, calm_days, cfg, azim)
        current = at.current_thresholds(cfg)
        if amp_given is not None:
            amp = amp_given
            if not amp:
                no_amp = (f"{a.amp_json} lists no threshold: no frequency had enough arcs in "
                          f"its window for its own amplitude threshold")
        else:
            amp = at.derive(amps, current, a.fraction)["thresholds"]
        amp_half = at.derive(amps, current, AMP_HALF)["thresholds"]
    variants = build_variants(names, (scatter1, scatter2),
                              tuple(a.outliers) if a.outliers else None, amp, amp_half)
    for n in sorted(names - {v.name for v in variants}, key=VARIANT_NAMES.index):
        if n == "amp_half":
            why = "no frequency has enough arcs in the calm week for its own amplitude threshold"
        elif n == "amp_by_freq" or (n == "combined" and not amp):
            why = no_amp
        else:
            why = ("no calm scale to set the outlier limits from (give --outliers): "
                   + (first_check if not math.isfinite(scatter1) else
                      "no final arcs about the spline in the calm week"))
        print(f"  NOTE: {n} not run -- {why}")
    for v in variants[1:]:
        run(v)

    # the reference, fitted once on the baseline in the calm week
    t_lo = min(window_epochs(w)[0] for w in windows) - 86400
    t_hi = max(window_epochs(w)[1] for w in windows) + 86400
    ref, ref_name = load_reference(work, files_dir,
                                   datetime.fromtimestamp(t_lo - wq.REF_FIT_DAYS * 86400,
                                                          tz=timezone.utc),
                                   datetime.fromtimestamp(t_hi, tz=timezone.utc),
                                   station, window_epochs(calm))
    if ref is None:
        print(f"  ERROR: {ref_name} -- nothing to score against")
        return 1
    ep, lv, _ = read_levels(out_dir(work, sta, baseline) / f"{sta}_spline_out.txt")
    c0, c1 = window_epochs(calm)
    m = (ep >= c0) & (ep < c1)
    fit = wq.fit_reference(ep[m], lv[m], ref[0], ref[1], np.ones(int(m.sum()), bool)) \
        if m.any() else None
    if fit is None:
        print("  ERROR: too little overlap of the baseline and the reference in the calm week")
        return 1

    repro = None
    record = files_dir / f"{sta}_spline_out.txt"
    if record.exists():
        pe, pl, _ = read_levels(record)
        idx = {int(round(e / 60)): v for e, v in zip(pe, pl)}       # by minute
        diffs = []
        for w in windows:
            w0, w1 = window_epochs(w)
            bm = (ep >= w0) & (ep < w1)
            diffs += [abs(v - idx[int(round(e / 60))]) for e, v in zip(ep[bm], lv[bm])
                      if int(round(e / 60)) in idx]
        repro = max(diffs) if diffs else None

    for v in variants:
        r = results[v.name]
        if not r["ok"]:
            continue
        vep, vlv, _ = read_levels(out_dir(work, sta, v) / f"{sta}_spline_out.txt")
        r["calm"] = score_window(vep, vlv, ref, fit, waves, calm)
        r["storm"] = score_window(vep, vlv, ref, fit, waves, storm)
        r["qc"] = {"calm": qc_window(vep, vlv, ref, waves, calm),
                   "storm": qc_window(vep, vlv, ref, waves, storm)}
        r["arcs"] = {"calm": arc_counts(work, sta, v, calm, azim),
                     "storm": arc_counts(work, sta, v, storm, azim)}

    print_report(results, variants, windows, calm_why, waves, ref_name, fit, span, settings, repro)
    best, reasons = recommend(results)
    print_recommendation(results, variants, best, reasons)

    summary = {"station": sta, "settings": settings, "span": [str(span[0]), str(span[1])],
               "calm": [str(calm[0]), str(calm[1])], "calm_choice": calm_why,
               "storm": [str(storm[0]), str(storm[1])], "reference": ref_name,
               "reference_fit": list(fit),
               "calm_scale_m": {"first_clip": scatter1, "second_clip": scatter2},
               "first_spline_check": first_check,
               "record_fits": {k: [str(f[0]), str(f[1])] for k, f in fits.items()},
               "baseline_vs_record_max_m": repro, "variants": results,
               "recommended": best, "reasons": reasons,
               "rule": {"calm_rmse_tol_m": CALM_RMSE_TOL_M, "calm_arcs_keep": CALM_ARCS_KEEP,
                        "storm_readings_keep": STORM_READINGS_KEEP, "storm_gain": STORM_GAIN,
                        "gap_visible_h": GAP_VISIBLE_H},
               "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    text = json.dumps(_jsonable(summary), indent=1, default=str) + "\n"
    (work / "qc_eval_summary.json").write_text(text)
    if a.json:
        Path(a.json).write_text(text)
    print(f"\n  summary: {work / 'qc_eval_summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
