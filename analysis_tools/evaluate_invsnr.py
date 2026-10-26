#!/usr/bin/env python3
"""
evaluate_invsnr.py

Offline evaluation of gnssrefl's inverse modelling (invsnr) against
the water level this station publishes now (the subdaily spline), the
NOAA tide gauge at Chatham and the tide model. Nothing in the nightly
processing changes: this is an opt-in tool, run by hand on the NUC for
chosen windows, and it writes only into its own folder (--out-dir).

WHY

The published water level is spectral. gnssir finds one reflector
height per satellite arc from the peak of a Lomb-Scargle periodogram,
and subdaily fits a cubic spline through those heights, correcting
each arc for the tide moving while it was observed (RHdot; Larson et
al. 2013, 2017). Inverse modelling fits the SNR oscillations
themselves, with the reflector height as a cubic spline in time
(Strandberg et al. 2016). A moving surface is then part of the model
rather than a correction to it, and every frequency and constellation
constrains one shared curve. Published agreement with tide gauges is
1.4-3.1 cm (Strandberg et al. 2016; 1.5-3.3 cm post-processed in
Strandberg et al. 2019), against about 2-5 cm for spectral spline
series (Geremia-Nievinski et al. 2020). Two cautions from the same
literature decide what this tool measures:

  - The gain is in single retrievals and sub-daily detail. For daily
    and monthly means the spectral and inverse methods are
    equivalent, and the inverse error budget is dominated by the
    moving sea surface, i.e. by waves (Purnell et al. 2020). So the
    report gives daily-mean agreement too, and splits everything by
    sea state.
  - The published inverse results come from sites with far smaller
    tides and calmer water than Marconi (Onsala, Spring Bay), and with
    a wide view of the water: Purnell et al. (2021) ask for at least
    140 deg of azimuth and 30 deg of elevation over water. Marconi's
    usable window is 90 deg (35-125) by 10 deg (5-15), so each knot
    interval sees fewer arcs. Whether the method helps on an exposed
    beach with storm waves of 3 m is the question, not a given.

WHAT IT DOES

  run      For each UTC day of each --window it runs gnssrefl 4.1.5's
           invsnr (gnssrefl.invsnr_cl.invsnr) on that day plus
           --pad-days either side, once per --knot-space. Each run is a
           child process (gnssrefl calls sys.exit()/exit() on ordinary
           failures) working on decimated COPIES of the station's SNR
           files in a private REFL_CODE under --out-dir. Only the core
           day of each run is kept, so every kept value has a day of
           data either side of it. invsnr's gap rule covers the whole
           padded run, so a run that stops on a gap lying in a pad day
           is run once more without that pad day (the core day then
           loses its edge knot spacing on that side, as at a missing
           SNR day); a gap wholly inside the core day stops it. A run
           that finished (a water level, or gnssrefl's gap or no-arcs
           stop) is reused while its settings and the station's SNR
           files are unchanged, unless --rerun; a crash, a timeout or
           any other gnssrefl stop (e.g. a file it could not find) is
           tried again.
  compare  Every series is put on one 30-minute grid (the spline's own
           step) and compared with
             - the Chatham gauge (8447435), through the transfer
               water level ~ a * gauge(t - lag) + b that
               water_level_qc.py uses, fitted to EACH series on its
               own calm, supported readings (three parameters each, so
               neither series is judged on the other's shape);
             - the tide model on NAVD88 (model + tide_model_navd88_offset),
               with no fitted parameter at all;
             - each other ("invsnr minus spline").
           For each it gives n, bias, RMSE, the de-meaned SD, a robust
           SD (1.4826 x MAD) and the correlation; coverage; the share of
           readings water_level_qc.run_qc would fail; daily-mean
           agreement; the values written inside data gaps; all of it
           for every reading and split by sea state from buoy 44008
           (calm < --rough-hs <= rough < --storm-hs <= storm).

  A reading is SUPPORTED when a retrieval of its own method lies within
  SUPPORT_S (1 h) of it: for the spline, gnssir's arcs in the subdaily
  azimuth window; for invsnr, its own arcs (the ones that pass its
  peak-to-noise test, which its gap rule counts; with --constel
  withBeidou these include BeiDou B1C/B2a arcs that the SNR fit leaves
  out, see SETTINGS). Both curves are smooth functions that write a
  value across a gap, so a value is not evidence of data. The headline
  numbers judge each invsnr series against the spline on the epochs
  where BOTH are supported, so the two are judged on the same water and
  one knot spacing's failed days never change another's numbers or
  verdict; the spline's own rows use the epochs where it and any
  invsnr series are supported.

  In storms the GNSS-IR level legitimately exceeds the harbour gauge by
  wave setup, and in the biggest storms reads the total water level at
  the shore (see water_level_qc.py). The storm rows therefore measure
  how the methods DIFFER in surf, not an error against truth.

SETTINGS (defaults, and why)

  RH 16-24 m         The antenna is 19.0 m above NAVD88. Tides span
                     about +/-1.5 m and the storm total water level
                     reaches ~3 m (RH ~16 m). invsnr rejects an arc whose
                     periodogram peak sits at either end of the window,
                     so the window is kept wider than the RH expected.
  elevation 5-15     As gnssir uses here. At 15 deg the reflection point
                     is ~70 m out and reaches the beach at low tide
                     (gnssir_reflection_audit.py in caco05-waterline).
  azimuth 35-125     process_and_plot.sh's subdaily window
                     (SUBDAILY_AZIM1/2): arcs nearer the 353/173 deg
                     edges run along the shore and reflect off the beach.
  knots 2 h          invsnr's default is 3 h. 2 h resolves M2 and its
                     first overtide with room to spare. invsnr stops on
                     any gap longer than 1.05 x the spacing anywhere in
                     the run, pad days included, so a shorter spacing
                     rejects more days: compare several, e.g.
                     --knot-space 1 1.5 2 3 (3 = invsnr's own, as a
                     reference). A stop caused by a pad day is retried
                     without it (WHAT IT DOES), so 'days ran' measures
                     the core days; PER DAY gives the largest gap that
                     touches the core day next to the whole run's.
                     --risky lets a short spacing fit across the gaps;
                     the curve is then poorly held there
                     (invsnr's own words: "risk of instabilities"), and
                     the report's IN GAPS row shows what it wrote.
  L1+L2+L5, GPS+GLONASS+Galileo
                     invsnr's default constellations on its three main
                     bands: GPS L1/L2C/L5, GLONASS L1/L2, Galileo
                     E1/E5a. gnssir here also uses Galileo E6/E5b/E5 and
                     BeiDou (allfreq). --signal ALL adds the E6 and E5b
                     columns: the same satellites, so no new pass times.
                     --constel withBeidou adds BeiDou's satellites, i.e.
                     new passes, which is what shortens the gaps that
                     stop invsnr, and keeps the GEO/IGSO satellites
                     gnssir leaves out. With L1+L2+L5 the SNR fit then
                     models BeiDou on B1I (gnssrefl's L2) only; B3 and
                     B2b (L6/L7) need --signal ALL. BeiDou's B1C and B2a
                     (L1/L5) arcs pass the peak-to-noise test and count
                     for the gap rule and for support, but are never
                     fitted: kristine_dictionary has no C1 or C5
                     (spline_functions.py:1389-1437) and the fit loops
                     over its list (:623). Both are worth one
                     comparison run.
  dec 5 s            The SNR files are 1 Hz (gnssrefl_sample_rate).
                     Nyquist: at RH 19 m and 5-15 deg the L1 SNR
                     oscillation has a period of about 35 s, so 5 s keeps
                     7 samples per cycle (largest resolvable RH ~60 m)
                     and the least-squares fit gets five times less data.
  peak2noise 3       invsnr_input's own default. The json value
                     overrides invsnr's -peak2noise, and only arcs that
                     pass it enter the SNR fit.
  rough_in 0.1       invsnr's starting value for the roughness term. On
                     the synthetic days of tests/test_evaluate_invsnr.py
                     (rho = 0.002 m^2) gnssrefl 4.1.5 starts from it and
                     returns 0.0020, with the level within 1 mm RMS.
  refraction on      invsnr's default, and the only setting that works:
                     refraction=False crashes 4.1.5 (below). It matters
                     here: on the same synthetic signals, refracted by
                     Bennett's formula, leaving the refraction out of
                     the model (set_refraction_model patched to let it
                     run) raised the level by 0.20 m at this antenna
                     height.
  output 300 s, pad 1 day each side.

WHAT invsnr 4.1.5 ACTUALLY DOES (read from the source; line numbers are
gnssrefl 4.1.5)

  - Signature (invsnr_cl.py:53-58): invsnr(station, year, doy, signal,
    peak2noise=2.5, constel=None, screenstats=False, dec=1, polydeg=2,
    snrfit=True, plt=True, doy_end=None, lspfigs=False, snrfigs=False,
    knot_space=3, rough_in=0.1, risky=False, snr=66, outfile_type='txt',
    outfile_name='', outlier_limit=0.5, no_dots=False, delta_out=300,
    refraction=True, json_override=False, lastday_seconds=0).
  - It reads $REFL_CODE/input/<sta>.inv.json (:197-216): precision,
    azilims, elvlims, rhlims, l2c_only (:221-225), and pktnlim, which
    REPLACES the peak2noise argument (:226-228). The refraction reads
    lat, lon, ht from it (spline_functions.py:1673-1678). This tool
    writes it with exactly the keys invsnr_input writes
    (invsnr_input.py:106-141), without invsnr_input's online UNR lookup.
  - An arc is accepted when its periodogram peak, searched strictly
    inside rhlims, is not at either end of that range and its peak
    over the MEAN power in rhlims exceeds pktnlim (spline_functions.py
    :504-517). The elevation and azimuth limits are strict
    inequalities (:363-369). gnssir computes its peak2noise on its own
    periodogram over its noise region (NReg), so the two numbers are
    not comparable.
  - polydeg is accepted but never passed on (:276-281): the direct
    signal is always removed with a degree-2 polynomial
    (spline_functions.py:246, 494).
  - One azimuth window, not a list (spline_functions.py:365-368).
  - knot_space is an integer on the command line (:30) but a float in
    Python (:268), so 1.5 h works through the Python call used here.
  - The file is written only when snrfit is True (spline_functions.py
    :987-1033), columns YYYY MM DD HH MM SS RH(m) doy MJD Nretrievals
    (:1112, :1026-1027), every delta_out from 00:00 of doy to the end of
    doy_end, last epoch dropped (:1013). Knots are evenly spaced, with
    an extra knot at each end of the run half a spacing beyond the
    last one (:833-838, "dont use these points"): the curve there is
    held from one side only; hence the padding. outfile_type 'csv'
    raises an IndexError (its format string asks for ten fields and
    gets nine, :1029-1030), so 'txt' is used.
  - It stops with sys.exit() -- exit status 0 -- when any gap between
    accepted arcs, counting the start and end of the run, exceeds 1.05 x
    the knot spacing, unless risky (:887-898); and with exit() when no
    arc passes (:861-863, :878-880). The return code says nothing, so
    the child catches SystemExit, the output file is checked, and the
    run's log tells the gap stop and the no-arcs stop from any other
    (a missing SNR or refraction file, refraction.py:350-352,
    spline_functions.py:1171-1172).
  - ONE roughness parameter per run (:927-929), damping the modelled
    oscillation by exp(-4 k^2 rho sin^2 e) (:667-669): rho is in m^2,
    so sqrt(rho) is an effective surface-height scatter in metres. It is
    returned to snr2spline's caller only, never written to a file; the
    child captures it. Each value is fitted over the whole padded run
    (3 days) and given to its core day, so neighbouring days share two
    thirds of their data and the n of the report's roughness-vs-Hs
    correlation overstates the independent samples.
  - Refraction is Bennett's elevation bending with GPT2w pressure and
    temperature (:737-746, :811-815; refraction.py:453-478). The fixed
    MJD 59580 (:745) does not matter: gpt2_1w is called with it=1, the
    static branch (refraction.py:181-182). refraction=False is broken:
    snr2spline still calls set_refraction_model, which then returns e,
    Tm and la that only exist when the model is on (:1669-1681), an
    UnboundLocalError.
  - Side effects, all confined here: it gzips each SNR file after
    reading it (:87), gunzips/unxz's in place (:1160-1169), looks for
    the SNR file in the CURRENT directory first (:1154), makes plots/ in
    the current directory for every accepted arc (:1201-1203), writes
    my_lsp.txt, outliers_lsp.txt and spline_out.png into
    $REFL_CODE/Files/<sta>/ (:404, :1002, :1050), and on its first run
    copies gnssrefl's gpt_1wA.pickle (21 MB) into $REFL_CODE/input/ and
    writes <sta>_refr.txt there (refraction.py:308-452, 481-568).
    Hence the private REFL_CODE and a scratch current directory per run.
  - dec keeps rows whose time since the FIRST row is a multiple of dec
    (:333). The copies are instead decimated on the clock (seconds of
    day divisible by --dec) and invsnr is given dec=1: the same rows
    for any file that starts on the grid, and copies 5x smaller.
  - Recent matplotlib (3.11 here; deprecated since 3.9) has no
    pyplot.plot_date, which invsnr calls BEFORE writing its file
    (:983-984, :1344-1387); the child supplies a two-line stand-in when
    it is missing. The SNR time tags are GPS seconds of day labelled as
    UTC, an 18 s offset that does not matter at 300 s output.

DATUM. invsnr writes RH; the level is NAVD88 Hortho - RH, with the
same antenna height as the spline (station_datum.navd88_hortho). Both
bend the elevation angles by Bennett's formula with GPT2w (gnssir's
refr_model 1, gnssrefl's default, unless gnssrefl_refraction_model in
station.json says otherwise), but subdaily aligns the frequencies with
each other (its inter-frequency bias step) and corrects each arc for
RHdot, while invsnr fits one curve to all frequencies with neither
step. A constant offset between the two is therefore possible; it is
reported ("invsnr minus spline"), not removed.

OUTPUT (--out-dir, default <project>/invsnr_eval/, git-ignored)
  runs/<label>/<day>/   invsnr.txt (gnssrefl's own file), result.json
                        (status, arcs, roughness, fit diagnostics),
                        run.log, spline_out.png (gnssrefl's own plot)
  report_<windows>.txt  the report printed to the screen
  summary_<windows>.json  the same numbers, machine-readable
  series_<windows>.csv  every series on the grid, for plotting
  refl_code_dec<N>/     the private REFL_CODE: SNR copies, input/ (the
                        inv.json, gnssrefl's refraction files)
  cache/                the copies of the gauge and wave caches
  The live REFL_CODE, the spline and the gauge/wave caches are only
  read; the caches are copied before anything is downloaded into them.
  The SNR copies are deleted at the end unless --keep-snr.

HOW TO ADOPT (only if the evaluation says so)
  1. Evidence first: at least two calm weeks and the 25-27 Sep 2026
     storm, with the best knot spacing. Adopt only if, on the common
     calm epochs, invsnr beats the spline against the gauge AND against
     the tide model (de-meaned SD) by at least 1 cm or 10 %, its
     supported coverage is not lower, its QC fail share in rough and
     storm seas is not higher, and at least 95 % of the days with SNR
     data ran (no gap exit, the least-squares fit converged). Accuracy
     is judged in calm water, where the gauge transfer holds; storms
     judge robustness. The report applies this rule to its own windows
     (ADOPTION RULE) and says what evidence it had.
  2. Then it becomes a SEPARATE nightly product, e.g.
     Files/<sta>/<sta>_invsnr_out.txt in the spline's column layout so
     every loader reads it, made by this script's run step, behind an
     opt-in station.json switch that is off by default. It must lag a
     day to match what was evaluated: the newest complete day has no
     day after it, so its run would lose its last knot spacing and hold
     its end from one side only. Publish day D-1 from D-2..D, or re-run
     the previous day each night. <sta>_spline_out.txt and the public
     plot stay as they are; moving them to invsnr is a separate owner
     decision and a datum change by the "invsnr minus spline" bias.
  3. Carry over the settings in the report header (also in
     summary_*.json); they are this run's, not a recommendation.

USAGE (on the NUC; invsnr needs the gnssrefl venv)

    cd /home/argus_user/GNSS/v4.1 && source gnssrefl_venv/bin/activate
    python3 analysis_tools/evaluate_invsnr.py \\
        --window 2026-09-20:2026-09-30 --window 2026-10-05:2026-10-11 \\
        --knot-space 1 1.5 2 3
    # again later, without re-running invsnr:
    python3 analysis_tools/evaluate_invsnr.py --window 2026-09-20:2026-09-30 \\
        --knot-space 2 --compare-only

REFERENCES
  Strandberg, Hobiger, Haas (2016) Radio Science 51, 1286-1296.
  Strandberg, Hobiger, Haas (2019) GPS Solutions 23(3).
  Purnell et al. (2020) IEEE JSTARS 13, 4419-4428.
  Purnell, Gomez, Minarik, Porter, Langston (2021) Earth Surf. Dynam.
  9, 673-685.
  Geremia-Nievinski et al. (2020) J. Geodesy 94(8).
  Larson et al. (2013) Adv. Space Res.; Larson, Ray, Williams (2017)
  J. Atmos. Oceanic Technol. 34(2), 295-307.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from station_datum import (load_station, navd88_hortho, spline_hortho,  # noqa: E402
                           spline_shift, tide_model_to_navd88)

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATION_JSON = PROJECT_DIR / "station" / "resources" / "station.json"
DEFAULT_OUT_DIR = PROJECT_DIR / "invsnr_eval"

# invsnr settings for Marconi (see SETTINGS in the module docstring)
RH_LIMITS = (16.0, 24.0)              # m
ELEVATION_LIMITS = (5.0, 15.0)        # deg
AZIMUTH_LIMITS = (35.0, 125.0)        # deg, as process_and_plot.sh SUBDAILY_AZIM1/2
KNOT_SPACE_H = (2.0,)
SIGNAL = "L1+L2+L5"
DECIMATE_S = 5
PEAK2NOISE = 3.0                      # invsnr_input's default pktnlim
ROUGH_IN = 0.1                        # invsnr's default
DELTA_OUT_S = 300                     # invsnr's default
PAD_DAYS = 1
PRECISION_M = 0.005                   # invsnr_input's LSP precision
SNR_ENDING = 66
RUN_TIMEOUT_S = 2 * 3600

# comparison
GRID_S = 1800                         # the spline's own step
SUPPORT_S = 3600                      # a retrieval within 1 h supports a reading
ROUGH_HS = 1.5                        # m; gnssir_reflection_audit.py's "rough"
STORM_HS = 3.0                        # m
MIN_FIT_POINTS = 48                   # as water_level_qc.fit_reference
MIN_DAY_FRACTION = 0.8                # a daily mean needs 80 % of the day
CLASSES = ("all", "calm", "rough", "storm")
RESULTS_UTC_COL, RESULTS_AZ_COL = 4, 5   # gnssir results: year doy RH sat UTC Azim ...
INV_JSON_KEYS = ("station", "lat", "lon", "ht", "rhlims", "elvlims", "azilims",
                 "pktnlim", "precision", "l2c_only")   # what invsnr_input writes

# the adoption rule (HOW TO ADOPT, step 1)
ADOPT_MIN_GAIN_M = 0.01               # invsnr's SD must be lower by 1 cm ...
ADOPT_MIN_GAIN_FRAC = 0.10            # ... or by 10 %, whichever is larger
ADOPT_MIN_DAYS_RAN = 0.95


# ---------------------------------------------------------------------
# SETTINGS AND RUN PLAN
# ---------------------------------------------------------------------

@dataclass
class InvsnrSettings:
    """One invsnr configuration; every value is passed on as named."""

    station: str
    knot_space_h: float = KNOT_SPACE_H[0]
    signal: str = SIGNAL
    constel: str | None = None
    rh_limits: tuple = RH_LIMITS
    elevation_limits: tuple = ELEVATION_LIMITS
    azimuth_limits: tuple = AZIMUTH_LIMITS
    decimate_s: int = DECIMATE_S
    peak2noise: float = PEAK2NOISE
    rough_in: float = ROUGH_IN
    risky: bool = False
    refraction: bool = True
    delta_out_s: int = DELTA_OUT_S
    pad_days: int = PAD_DAYS
    l2c_only: bool = True
    precision_m: float = PRECISION_M
    snr_ending: int = SNR_ENDING
    outlier_limit_m: float = 0.5

    def label(self) -> str:
        """Folder name: the knot spacing, plus a short hash of everything
        else, so runs with different settings never share a folder."""
        rest = {k: v for k, v in asdict(self).items() if k != "knot_space_h"}
        digest = hashlib.sha1(json.dumps(rest, sort_keys=True).encode()).hexdigest()[:8]
        return f"k{self.knot_space_h:g}h-{digest}"

    def name(self) -> str:
        return f"invsnr k{self.knot_space_h:g}h"


def inv_json(settings: InvsnrSettings, lat: float, lon: float, ht: float) -> dict:
    """The $REFL_CODE/input/<sta>.inv.json invsnr reads, with exactly the
    keys gnssrefl 4.1.5's invsnr_input writes (invsnr_input.py:106-141)."""
    return {
        "station": settings.station,
        "lat": float(lat), "lon": float(lon), "ht": float(ht),
        "rhlims": [float(settings.rh_limits[0]), float(settings.rh_limits[1])],
        "elvlims": [float(settings.elevation_limits[0]), float(settings.elevation_limits[1])],
        "azilims": [float(settings.azimuth_limits[0]), float(settings.azimuth_limits[1])],
        "pktnlim": float(settings.peak2noise),
        "precision": float(settings.precision_m),
        "l2c_only": bool(settings.l2c_only),
    }


def invsnr_kwargs(settings: InvsnrSettings, first: date, last: date, outfile_stem: Path) -> dict:
    """Keyword arguments for gnssrefl.invsnr_cl.invsnr (signature in the
    module docstring). dec is 1 because the SNR copies are decimated
    already; the plots and the screen output are off."""
    return {
        "station": settings.station,
        "year": first.year,
        "doy": first.timetuple().tm_yday,
        "doy_end": last.timetuple().tm_yday,
        "signal": settings.signal,
        "peak2noise": float(settings.peak2noise),
        "constel": settings.constel,
        "screenstats": False,
        "dec": 1,
        "snrfit": True,
        "plt": False,
        "lspfigs": False,
        "snrfigs": False,
        "knot_space": float(settings.knot_space_h),
        "rough_in": float(settings.rough_in),
        "risky": bool(settings.risky),
        "snr": int(settings.snr_ending),
        "outfile_type": "txt",
        "outfile_name": str(outfile_stem),
        "outlier_limit": float(settings.outlier_limit_m),
        "no_dots": False,
        "delta_out": int(settings.delta_out_s),
        "refraction": bool(settings.refraction),
    }


@dataclass
class Run:
    """One invsnr call: `day` is the core day kept, first..last the days
    handed to invsnr (the core day plus padding, within one year)."""

    day: date
    first: date
    last: date


def parse_window(text: str) -> tuple[date, date]:
    """'2026-09-20:2026-09-30' (inclusive) or a single day '2026-09-20'."""
    parts = text.split(":")
    if len(parts) not in (1, 2):
        raise ValueError(f"window {text!r}: expected START[:END]")
    a = date.fromisoformat(parts[0])
    b = date.fromisoformat(parts[-1])
    if b < a:
        raise ValueError(f"window {text!r} ends before it starts")
    return a, b


def window_days(windows) -> list[date]:
    days = set()
    for a, b in windows:
        d = a
        while d <= b:
            days.add(d)
            d += timedelta(days=1)
    return sorted(days)


def plan_runs(windows, pad_days: int) -> list[Run]:
    """One run per day of the windows. invsnr cannot cross a year
    (doy..doy_end of one year), so the padding stops at 1 Jan / 31 Dec;
    that side of the core day then loses one knot spacing at its edge
    (see invsnr_levels)."""
    runs = []
    for d in window_days(windows):
        first = max(d - timedelta(days=pad_days), date(d.year, 1, 1))
        last = min(d + timedelta(days=pad_days), date(d.year, 12, 31))
        runs.append(Run(d, first, last))
    return runs


def day_start(d: date) -> float:
    return float(calendar.timegm(d.timetuple()))


# ---------------------------------------------------------------------
# SNR COPIES (the live files are only read)
# ---------------------------------------------------------------------

def snr_name(sta: str, d: date, snr_ending: int = SNR_ENDING) -> str:
    """gnssrefl's SNR file name (spline_functions.define_inputfile)."""
    return f"{sta}{d.timetuple().tm_yday:03d}0.{d.year % 100:02d}.snr{snr_ending}"


def find_live_snr(refl_code: Path, sta: str, d: date, snr_ending: int = SNR_ENDING) -> Path | None:
    base = Path(refl_code) / str(d.year) / "snr" / sta / snr_name(sta, d, snr_ending)
    for p in (base, base.with_name(base.name + ".gz"), base.with_name(base.name + ".xz")):
        if p.is_file():
            return p
    return None


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    if path.name.endswith(".xz"):
        return lzma.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def stage_snr(live_refl: Path, private_refl: Path, sta: str, d: date,
              decimate_s: int, snr_ending: int = SNR_ENDING) -> Path | None:
    """A decimated copy of one day's SNR file in the private REFL_CODE:
    rows whose seconds of day (column 4) are divisible by decimate_s.
    Reused if it is already there (plain, or gzipped by invsnr itself).
    It is made again when the station's file has changed since (name,
    size or time in <copy>.src.json), and when there is both a plain
    and a gzipped copy: a run was then stopped while gzip or gunzip was
    working, so either may be cut short. None when the station has no
    SNR file for that day."""
    dest_dir = Path(private_refl) / str(d.year) / "snr" / sta
    dest = dest_dir / snr_name(sta, d, snr_ending)
    gz = dest.with_name(dest.name + ".gz")
    sidecar = dest.with_name(dest.name + ".src.json")
    src = find_live_snr(live_refl, sta, d, snr_ending)
    if src is None:
        return None
    st = src.stat()
    signature = {"name": src.name, "size": int(st.st_size), "mtime": int(st.st_mtime),
                 "decimate_s": int(decimate_s)}
    try:
        current = json.loads(sidecar.read_text()) == signature
    except (OSError, ValueError):
        current = False
    if not current or (dest.is_file() and gz.is_file()):
        for stale in (dest, gz):
            if stale.is_file():
                stale.unlink()
    for existing in (dest, gz):
        if existing.is_file() and existing.stat().st_size > 0:
            return existing
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    kept = 0
    with _open_text(src) as fin, open(tmp, "w") as fout:
        for line in fin:
            c = line.split()
            if len(c) < 5:
                continue
            try:
                sod = float(c[3])
            except ValueError:
                continue
            if decimate_s <= 1 or int(round(sod)) % decimate_s == 0:
                fout.write(line)
                kept += 1
    if kept == 0:
        tmp.unlink()
        return None
    tmp.replace(dest)
    sidecar.write_text(json.dumps(signature))
    return dest


# ---------------------------------------------------------------------
# THE invsnr CALL (child process)
# ---------------------------------------------------------------------

def _plot_date_standin(x, y, fmt="o", **kwargs):
    """pyplot.plot_date for matplotlib >= 3.11, where it was removed
    (deprecated in 3.9); invsnr 4.1.5 calls it before writing its
    output file."""
    import matplotlib.pyplot as plt
    kwargs.pop("tz", None)
    kwargs.pop("xdate", None)
    kwargs.pop("ydate", None)
    return plt.plot(x, y, fmt, **kwargs)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return _jsonable(x.tolist())
    if isinstance(x, (np.floating, float)):
        return float(x) if np.isfinite(x) else None
    if isinstance(x, (np.integer, int)) and not isinstance(x, bool):
        return int(x)
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    return x if x is None or isinstance(x, str) else str(x)


def gps_seconds(d: date) -> float:
    """invsnr's time origin for a run starting on d (spline_functions.py
    :820-824): GPS seconds of 00:00 UTC, from astropy as gnssrefl does,
    else from the Unix epoch and the 18 s GPS-UTC offset in force since
    2017."""
    try:
        from astropy.time import Time
        return float(Time(f"{d.year}:{d.timetuple().tm_yday:03d}:00:00:00", format="yday").gps)
    except Exception:
        return day_start(d) - 315964800.0 + 18.0


def _captured_results(captured: dict, first: date) -> dict:
    """What snr2arcs, snr2spline and the two least-squares fits returned,
    with invsnr's 'GPS seconds' (gbase + seconds of day of the first day)
    turned into Unix epochs."""
    out = {"arcs": None, "n_snr_obs": None, "fspec": None, "roughness": None,
           "amplitudes": None, "knots_unix": None, "kval_js": None, "fits": []}
    gbase = captured.get("gbase")
    to_unix = (lambda t: day_start(first) + (np.asarray(t, float) - gbase)) if gbase is not None else None
    arcs = captured.get("arcs")
    if arcs is not None:
        rh_arr, snrdt_arr, fspecdict = arcs
        a = np.asarray(rh_arr, dtype=float).reshape(-1, 12) if len(rh_arr) else np.empty((0, 12))
        out["arcs"] = {
            "unix": to_unix(a[:, 0]) if to_unix else None,
            "rh": a[:, 1], "sat": a[:, 2], "azim": a[:, 6],
            "pk2noise": a[:, 10], "freq": a[:, 11],
        }
        out["n_snr_obs"] = int(len(snrdt_arr))
        out["fspec"] = [k for k, v in fspecdict.items() if v]   # = smarterWay(fspecdict)
    invout = captured.get("invout")
    if isinstance(invout, dict):
        if "outparams_js" in invout:
            params = np.asarray(invout["outparams_js"], float)
            out["roughness"] = float(params[-1]) if len(params) else None
            out["amplitudes"] = params[:-1]
        if "knots" in invout and to_unix:
            out["knots_unix"] = to_unix(invout["knots"])
        if "kval_js" in invout:
            out["kval_js"] = invout["kval_js"]
    for res in captured.get("fits", []):
        fun = np.asarray(getattr(res, "fun", []), float)
        out["fits"].append({
            "success": bool(getattr(res, "success", False)),
            "status": int(getattr(res, "status", 0)),
            "message": str(getattr(res, "message", "")),
            "nfev": int(getattr(res, "nfev", 0) or 0),
            "cost": float(getattr(res, "cost", np.nan)),
            "m": int(fun.size),
            "n": int(np.asarray(getattr(res, "x", []), float).size),
            "rms": float(np.sqrt(np.mean(fun ** 2))) if fun.size else None,
        })
    return _jsonable(out)


def worker_main(spec_path: str) -> int:
    """Child process: run invsnr once and write result.json. The public
    invsnr() is called as it is; snr2arcs, snr2spline and
    least_squares are wrapped only to keep what they return (the arcs,
    the roughness, the fit diagnostics), which invsnr itself discards."""
    spec = json.loads(Path(spec_path).read_text())
    first = date.fromisoformat(spec["first"])
    started = time.monotonic()
    result = {"status": "error", "message": "", "gnssrefl_version": None}
    captured = {"fits": []}
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if not hasattr(plt, "plot_date"):
            plt.plot_date = _plot_date_standin
        import gnssrefl.invsnr_cl as invsnr_cl
        import gnssrefl.spline_functions as sf
        try:
            from importlib.metadata import version
            result["gnssrefl_version"] = version("gnssrefl")
        except Exception:
            pass
        captured["gbase"] = gps_seconds(first)
        real_arcs, real_spline, real_ls = sf.snr2arcs, sf.snr2spline, sf.least_squares

        def keep_arcs(*a, **k):
            captured["arcs"] = real_arcs(*a, **k)
            return captured["arcs"]

        def keep_spline(*a, **k):
            captured["invout"] = real_spline(*a, **k)
            return captured["invout"]

        def keep_fit(*a, **k):
            res = real_ls(*a, **k)
            captured["fits"].append(res)
            return res

        sf.snr2arcs, sf.snr2spline, sf.least_squares = keep_arcs, keep_spline, keep_fit
        invsnr_cl.invsnr(**spec["invsnr_kwargs"])
        result["status"] = "ok"
    except SystemExit as exc:
        result["status"] = "exit"
        result["message"] = f"gnssrefl stopped (sys.exit/exit, code {exc.code!r}); see run.log"
    except Exception:
        result["message"] = traceback.format_exc(limit=6)
    result["runtime_s"] = round(time.monotonic() - started, 1)
    try:
        result.update(_captured_results(captured, first))
    except Exception:
        result["capture_error"] = traceback.format_exc(limit=4)
    out = Path(spec["result_path"])
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(out)
    return 0


def gnssrefl_python(explicit: str | None = None) -> str:
    """The interpreter that has gnssrefl: --python, else this one if it
    can import gnssrefl, else the project's gnssrefl_venv."""
    if explicit:
        return explicit
    import importlib.util
    if importlib.util.find_spec("gnssrefl") is not None:
        return sys.executable
    venv = PROJECT_DIR / "gnssrefl_venv" / "bin" / "python3"
    return str(venv) if venv.exists() else sys.executable


@dataclass
class RunContext:
    """Where the runs read and write."""

    live_refl: Path
    out_dir: Path
    lat: float
    lon: float
    ht: float
    python: str = field(default_factory=lambda: sys.executable)
    timeout_s: float = RUN_TIMEOUT_S
    rerun: bool = False

    def private_refl(self, settings: InvsnrSettings) -> Path:
        return self.out_dir / f"refl_code_dec{settings.decimate_s}"

    def run_dir(self, settings: InvsnrSettings, run: Run) -> Path:
        return self.out_dir / "runs" / settings.label() / run.day.isoformat()


def _same_spec(old: dict, new: dict) -> bool:
    keys = ("invsnr_kwargs", "inv_json", "first", "last", "decimate_s", "sources")
    return all(old.get(k) == new.get(k) for k in keys)


def live_sources(live_refl: Path, sta: str, first: date, last: date,
                 snr_ending: int = SNR_ENDING) -> dict:
    """{day: [file name, size, mtime]} of the station's SNR files for
    first..last, so a day the station has reprocessed since is run
    again rather than reused."""
    out = {}
    d = first
    while d <= last:
        p = find_live_snr(live_refl, sta, d, snr_ending)
        if p is not None:
            st = p.stat()
            out[d.isoformat()] = [p.name, int(st.st_size), int(st.st_mtime)]
        d += timedelta(days=1)
    return out


# gnssrefl's own stops, told apart by what it printed (spline_functions.py
# :861-863, :878-880, :893-898). Any other stop is 'other'.
EXIT_REASONS = (("gap", "Gap in data bigger than node spacing"),
                ("no_arcs", "no reflector height data"),
                ("no_arcs", "not enough data"))
# Outcomes that gnssrefl reaches again on the same files and settings:
# a run that ended in one of these is reused unless --rerun. A crash,
# a timeout or another stop (e.g. a file it could not find) is tried
# again, as its cause may have been fixed.
REUSED_EXITS = ("gap", "no_arcs")


def exit_reason(log_text: str) -> tuple[str, str]:
    """(reason, the line that says so) for a run gnssrefl stopped: 'gap',
    'no_arcs' or 'other' (then the log's last line)."""
    lines = [x.strip() for x in log_text.splitlines() if x.strip()]
    for reason, text in EXIT_REASONS:
        for line in lines:
            if text in line:
                return reason, line
    return "other", lines[-1] if lines else ""


def finished(res: dict) -> bool:
    """A run that would end the same way again (see REUSED_EXITS)."""
    return res.get("status") == "ok" or (res.get("status") == "exit"
                                         and res.get("reason") in REUSED_EXITS)


def gaps_over(arc_unix, first: date, last: date, limit_s: float) -> list:
    """invsnr's gap rule (spline_functions.py:883-898): the gaps between
    accepted arcs, counting the run's start and end, longer than
    limit_s, as (start, end) epochs."""
    t = np.sort(np.array([a for a in arc_unix if a is not None], float))
    edges = np.concatenate(([day_start(first)], t, [day_start(last) + 86400.0]))
    i = np.flatnonzero(np.diff(edges) > limit_s)
    return [(float(edges[k]), float(edges[k + 1])) for k in i]


def pad_sides_to_drop(res: dict, run: Run, first: date, last: date, knot_space_h: float) -> list:
    """The pad sides ('before', 'after') to run again without, when a
    run stopped on the gap rule only because of gaps reaching into its
    pad days. [] when a gap lies wholly inside the core day (the core
    day itself fails) or nothing points at a pad day."""
    arcs = (res.get("arcs") or {}).get("unix")
    if res.get("status") != "exit" or res.get("reason") != "gap" or not arcs:
        return []
    d0 = day_start(run.day)
    d1 = d0 + 86400.0
    sides = set()
    for a, b in gaps_over(arcs, first, last, 1.05 * knot_space_h * 3600.0):
        if a >= d0 and b <= d1:
            return []
        if a < d0 and first < run.day:
            sides.add("before")
        if b > d1 and last > run.day:
            sides.add("after")
    return sorted(sides)


def execute_run(run: Run, settings: InvsnrSettings, ctx: RunContext) -> dict:
    """Run invsnr for one core day and return its result (also kept in
    run_dir/result.json). A pad day without an SNR file shortens the
    run on that side; a core day without one is 'no_snr'. A run that
    stops on the gap rule only because of a gap in a pad day is run
    again without that pad day ('pad_dropped'; the padded attempt is
    kept in 'padded_run' and its log in run_padded.log). A run that
    finished (see finished()) with the same settings, span and SNR
    files is reused."""
    run_dir = ctx.run_dir(settings, run)
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    private = ctx.private_refl(settings)
    sta = settings.station

    # the days that have SNR data, contiguous around the core day
    if find_live_snr(ctx.live_refl, sta, run.day, settings.snr_ending) is None:
        res = {"status": "no_snr", "message": f"no SNR file for {run.day}"}
        result_path.write_text(json.dumps(res))
        return res
    first, last = run.day, run.day
    while first > run.first and find_live_snr(ctx.live_refl, sta, first - timedelta(days=1),
                                              settings.snr_ending):
        first -= timedelta(days=1)
    while last < run.last and find_live_snr(ctx.live_refl, sta, last + timedelta(days=1),
                                            settings.snr_ending):
        last += timedelta(days=1)

    def make_spec(a: date, b: date) -> dict:
        return {
            "invsnr_kwargs": invsnr_kwargs(settings, a, b, run_dir / "invsnr"),
            "inv_json": inv_json(settings, ctx.lat, ctx.lon, ctx.ht),
            "first": a.isoformat(), "last": b.isoformat(),
            "decimate_s": settings.decimate_s,
            "sources": live_sources(ctx.live_refl, sta, a, b, settings.snr_ending),
            "result_path": str(result_path),
        }

    spec = make_spec(first, last)
    if not ctx.rerun and result_path.exists():
        try:
            old = json.loads(result_path.read_text())
        except (OSError, ValueError):
            old = {}
        planned = (old.get("padded_run") or old).get("spec", {})
        if (finished(old) and _same_spec(planned, spec)
                and (old["status"] != "ok" or (run_dir / "invsnr.txt").exists())):
            old["reused"] = True
            return old

    d = first
    while d <= last:
        if stage_snr(ctx.live_refl, private, sta, d, settings.decimate_s, settings.snr_ending) is None:
            res = {"status": "no_snr", "message": f"SNR file for {d} could not be copied", "spec": spec}
            result_path.write_text(json.dumps(res))
            return res
        d += timedelta(days=1)
    (private / "input").mkdir(parents=True, exist_ok=True)
    (private / "input" / f"{sta}.inv.json").write_text(json.dumps(spec["inv_json"], indent=4))
    if (run_dir / "run_padded.log").exists():
        (run_dir / "run_padded.log").unlink()

    res = _run_child(spec, run_dir, private, sta, ctx)
    drop = pad_sides_to_drop(res, run, first, last, settings.knot_space_h)
    if drop:
        padded = {k: res.get(k) for k in ("status", "reason", "message", "spec", "wall_s")}
        padded["max_gap_min"] = max_gap_minutes((res.get("arcs") or {}).get("unix"), first, last)
        (run_dir / "run.log").replace(run_dir / "run_padded.log")
        res = _run_child(make_spec(run.day if "before" in drop else first,
                                   run.day if "after" in drop else last),
                         run_dir, private, sta, ctx)
        res["pad_dropped"] = drop
        res["padded_run"] = padded
        result_path.write_text(json.dumps(res))
    return res


def _run_child(spec: dict, run_dir: Path, private: Path, sta: str, ctx: RunContext) -> dict:
    """One invsnr call in a child process (worker_main), in a scratch
    current directory; returns the result it wrote, with the spec, the
    wall time and, for a gnssrefl stop, its reason from run.log."""
    result_path = Path(spec["result_path"])
    files_dir = private / "Files" / sta
    files_dir.mkdir(parents=True, exist_ok=True)
    extras = ("spline_out.png", "outliers_lsp.txt")           # gnssrefl's own, moved to run_dir
    for stale in ([run_dir / "invsnr.txt", result_path] + [run_dir / n for n in extras]
                  + [files_dir / n for n in extras]):
        if stale.exists():
            stale.unlink()

    cwd = run_dir / "cwd"           # invsnr looks for SNR files and makes plots/ here
    cwd.mkdir(exist_ok=True)
    spec_path = run_dir / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=2))
    env = dict(os.environ)
    env.update(REFL_CODE=str(private), ORBITS=str(private / "orbits"), EXE=str(private / "exe"),
               MPLBACKEND="Agg", PYTHONUNBUFFERED="1")
    started = time.monotonic()
    with open(run_dir / "run.log", "w") as log:
        try:
            # stdin closed: gnssrefl's gzip/gunzip calls must fail, not
            # wait for an answer at the terminal
            subprocess.run([ctx.python, str(Path(__file__).resolve()), "--worker", str(spec_path)],
                           cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log,
                           stderr=subprocess.STDOUT, timeout=ctx.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            res = {"status": "timeout", "message": f"invsnr ran longer than {ctx.timeout_s:.0f} s"}
            res["spec"] = spec
            result_path.write_text(json.dumps(res))
            return res
    for name in extras:
        src = files_dir / name
        if src.exists():
            shutil.move(str(src), str(run_dir / name))
    shutil.rmtree(cwd, ignore_errors=True)
    try:
        res = json.loads(result_path.read_text())
    except (OSError, ValueError):
        res = {"status": "error", "message": "the child wrote no result; see run.log"}
    if res.get("status") == "ok" and not (run_dir / "invsnr.txt").exists():
        res["status"] = "error"
        res["message"] = "invsnr returned without writing its output file; see run.log"
    if res.get("status") == "exit":
        try:
            log_text = (run_dir / "run.log").read_text(errors="replace")
        except OSError:
            log_text = ""
        res["reason"], line = exit_reason(log_text)
        res["message"] = f"gnssrefl stopped ({res['reason']}): {line}"
    res["spec"] = spec
    res["wall_s"] = round(time.monotonic() - started, 1)
    result_path.write_text(json.dumps(res))
    return res


def load_run(run: Run, settings: InvsnrSettings, ctx: RunContext) -> dict:
    """A run's stored result, for --compare-only; status 'missing' if none."""
    try:
        return json.loads((ctx.run_dir(settings, run) / "result.json").read_text())
    except (OSError, ValueError):
        return {"status": "missing", "message": "not run yet"}


# ---------------------------------------------------------------------
# READING THE SERIES
# ---------------------------------------------------------------------

def read_invsnr_txt(path: Path):
    """invsnr's output (YYYY MM DD HH MM SS RH(m) doy MJD Nretrievals):
    (epochs, RH, retrievals in that hour)."""
    ep, rh, nret = [], [], []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            c = line.split()
            if len(c) < 7:
                continue
            try:
                t = datetime(int(float(c[0])), int(float(c[1])), int(float(c[2])),
                             int(float(c[3])), int(float(c[4])), int(float(c[5])),
                             tzinfo=timezone.utc)
                v = float(c[6])
                n = float(c[9]) if len(c) > 9 else np.nan
            except (ValueError, IndexError):
                continue
            ep.append(t.timestamp())
            rh.append(v)
            nret.append(n)
    return np.array(ep, float), np.array(rh, float), np.array(nret, float)


def invsnr_levels(runs_results, settings: InvsnrSettings, ctx: RunContext, hortho: float):
    """The core days of the finished runs as (epochs, NAVD88 levels,
    arc epochs). A core day whose run stops at 1 Jan / 31 Dec (or at a
    missing SNR day) has no padding on that side, so its first or last
    knot spacing, constrained from one side only, is left out. Arc
    epochs fall back to invsnr's hourly retrieval counts when the run
    kept no arcs."""
    eps, lvs, arcs = [], [], []
    edge = settings.knot_space_h * 3600.0
    for run, res in runs_results:
        if res.get("status") != "ok":
            continue
        path = ctx.run_dir(settings, run) / "invsnr.txt"
        if not path.exists():
            continue
        ep, rh, nret = read_invsnr_txt(path)
        spec = res.get("spec", {})
        r0 = day_start(date.fromisoformat(spec.get("first", run.first.isoformat())))
        r1 = day_start(date.fromisoformat(spec.get("last", run.last.isoformat()))) + 86400.0
        d0 = day_start(run.day)
        d1 = d0 + 86400.0
        keep = (ep >= d0) & (ep < d1) & (ep >= r0 + edge) & (ep <= r1 - edge)
        eps.append(ep[keep])
        lvs.append(hortho - rh[keep])
        a = (res.get("arcs") or {}).get("unix")
        if a:
            a = np.array([x for x in a if x is not None], float)
        else:          # hour bins with retrievals, at the middle of the hour
            hours = ep[np.isfinite(nret) & (nret > 0)]
            a = np.unique(np.floor(hours / 3600.0) * 3600.0 + 1800.0)
        arcs.append(a[(a >= d0 - SUPPORT_S) & (a < d1 + SUPPORT_S)])
    cat = (lambda parts: np.concatenate(parts) if parts else np.array([], float))
    ep, lv, ar = cat(eps), cat(lvs), cat(arcs)
    order = np.argsort(ep)
    return ep[order], lv[order], np.unique(ar)


def load_spline_series(path: Path, cfg: dict | None = None):
    """The published spline on NAVD88 (plot_7day.load_spline, which also
    drops gnssrefl's 999 gap rows) as (epochs, levels). load_spline
    shifts the file to the project's station.json; with cfg (the
    --station-json in use) it is shifted to that one instead, so the
    spline and invsnr share one antenna height."""
    from plot_7day import load_spline
    times, values = load_spline(Path(path))
    ep = np.array([t.replace(tzinfo=timezone.utc).timestamp() for t in times], float)
    shift = 0.0
    if cfg is not None and len(ep):
        shift = spline_shift(path, cfg, quiet=True) - spline_shift(path, quiet=True)
    return ep, np.asarray(values, float) + shift


def results_arc_epochs(refl_code: Path, sta: str, first: date, last: date, azimuth=None):
    """Epochs of gnssir's arcs (results/<doy>.txt, UTC hours in column 5)
    from the day before `first` to the day after `last`, inside the
    subdaily azimuth window when one is given: the spline's support."""
    out = []
    d = first - timedelta(days=1)
    while d <= last + timedelta(days=1):
        f = Path(refl_code) / str(d.year) / "results" / sta / f"{d.timetuple().tm_yday:03d}.txt"
        if f.is_file():
            t0 = day_start(d)
            with open(f, errors="replace") as fh:
                for s in fh:
                    if not s.strip() or s.startswith("%"):
                        continue
                    c = s.split()
                    try:
                        utc_h, az = float(c[RESULTS_UTC_COL]), float(c[RESULTS_AZ_COL])
                    except (IndexError, ValueError):
                        continue
                    if azimuth is None or azimuth[0] <= az <= azimuth[1]:
                        out.append(t0 + utc_h * 3600.0)
        d += timedelta(days=1)
    return np.unique(np.array(out, float))


def _read_gauge_cache(path: Path):
    import water_level_qc as wq
    rows = wq._read_cache(Path(path))
    if not rows:
        return None
    ep = np.array(sorted(rows), float)
    return ep, np.array([rows[int(e)] for e in ep])


def _read_wave_cache(path: Path):
    rows = {}
    if Path(path).exists():
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows[int(float(r["epoch"]))] = (float(r["wvht_m"]), float(r["dpd_s"]))
                except (KeyError, ValueError):
                    continue
    if not rows:
        return None
    ep = np.array(sorted(rows), float)
    return ep, np.array([rows[int(e)][0] for e in ep]), np.array([rows[int(e)][1] for e in ep])


def load_references(live_gauge: Path, live_waves: Path, cache_dir: Path,
                    start: datetime, end: datetime, offline: bool = False):
    """(gauge, waves) as water_level_qc.py loads them, from COPIES of the
    station's caches in cache_dir (downloads are merged into the copies,
    never into the live files). Either may be None."""
    import water_level_qc as wq
    cache_dir.mkdir(parents=True, exist_ok=True)
    g_copy, w_copy = cache_dir / Path(live_gauge).name, cache_dir / Path(live_waves).name
    for src, dst in ((live_gauge, g_copy), (live_waves, w_copy)):
        if Path(src).is_file():
            shutil.copy2(src, dst)
    if offline:
        return _read_gauge_cache(g_copy), _read_wave_cache(w_copy)
    return wq.load_gauge(g_copy, start, end), wq.load_waves(w_copy)


def load_tide_model(path, time_col: str, value_col: str, offset: float):
    """The tide model on NAVD88 as (epochs, levels); times are UTC, as in
    plot_7day.py and compare_to_tide_deviation.py. None if unreadable."""
    from plot_7day import load_tide
    times, values = load_tide(Path(path), time_col, value_col)
    if not times:
        return None
    ep = np.array([t.replace(tzinfo=timezone.utc).timestamp() for t in times], float)
    order = np.argsort(ep)
    return ep[order], np.asarray(values, float)[order] + offset


# ---------------------------------------------------------------------
# COMPARISON
# ---------------------------------------------------------------------

def native_step(ep) -> float:
    """A series' own sampling step (median spacing), s."""
    d = np.diff(np.sort(np.asarray(ep, float)))
    d = d[d > 0]
    return float(np.median(d)) if len(d) else float(GRID_S)


def sample_on_grid(ep, val, grid, max_step_s: float):
    """Linear interpolation of a series onto the grid, NaN outside it and
    across any gap wider than max_step_s (missing rows, e.g. the spline's
    999s), except at a grid epoch that IS a sample."""
    ep = np.asarray(ep, float)
    val = np.asarray(val, float)
    grid = np.asarray(grid, float)
    ok = np.isfinite(ep) & np.isfinite(val)
    ep, val = ep[ok], val[ok]
    order = np.argsort(ep)
    ep, val = ep[order], val[order]
    out = np.full(len(grid), np.nan)
    if len(ep) == 0:
        return out
    if len(ep) == 1:
        return np.where(np.abs(grid - ep[0]) <= 1.0, val[0], np.nan)
    out = np.interp(grid, ep, val, left=np.nan, right=np.nan)
    j = np.clip(np.searchsorted(ep, grid, side="right"), 1, len(ep) - 1)
    lo, hi = ep[j - 1], ep[j]
    on_sample = (np.abs(grid - lo) <= 1.0) | (np.abs(grid - hi) <= 1.0)
    out[(hi - lo > max_step_s) & ~on_sample] = np.nan
    return out


def supported_mask(grid, arc_ep, support_s: float = SUPPORT_S):
    """True where a retrieval lies within support_s of the grid epoch."""
    grid = np.asarray(grid, float)
    arc_ep = np.sort(np.asarray(arc_ep, float))
    if len(arc_ep) == 0:
        return np.zeros(len(grid), bool)
    j = np.clip(np.searchsorted(arc_ep, grid), 0, len(arc_ep) - 1)
    jm = np.clip(j - 1, 0, len(arc_ep) - 1)
    near = np.minimum(np.abs(arc_ep[j] - grid), np.abs(arc_ep[jm] - grid))
    return near <= support_s


def stats(x, ref) -> dict:
    """x minus ref where both exist: n, bias (mean), RMSE, SD (the RMSE
    with the bias removed: RMSE^2 = bias^2 + SD^2), robust SD
    (1.4826 x MAD) and the correlation of x with ref."""
    x = np.asarray(x, float)
    ref = np.asarray(ref, float)
    m = np.isfinite(x) & np.isfinite(ref)
    n = int(m.sum())
    if n == 0:
        return {"n": 0}
    d = x[m] - ref[m]
    out = {"n": n, "bias": float(np.mean(d)), "rmse": float(np.sqrt(np.mean(d ** 2))),
           "sd": float(np.std(d)),
           "nmad": float(1.4826 * np.median(np.abs(d - np.median(d))))}
    if n > 2 and np.std(x[m]) > 0 and np.std(ref[m]) > 0:
        out["r"] = float(np.corrcoef(x[m], ref[m])[0, 1])
    return out


def sea_state(hs, rough_hs: float = ROUGH_HS, storm_hs: float = STORM_HS):
    """'calm' / 'rough' / 'storm' by buoy Hs; 'unknown' without a record."""
    hs = np.asarray(hs, float)
    out = np.full(len(hs), "unknown", dtype=object)
    known = np.isfinite(hs)
    h = np.where(known, hs, 0.0)          # no NaN comparisons (numpy 1.17 warns)
    out[known & (h < rough_hs)] = "calm"
    out[known & (h >= rough_hs) & (h < storm_hs)] = "rough"
    out[known & (h >= storm_hs)] = "storm"
    return out


def class_masks(sea) -> dict:
    sea = np.asarray(sea, dtype=object)
    return {c: (np.ones(len(sea), bool) if c == "all" else sea == c) for c in CLASSES}


def gauge_transfer(grid, level, use, gauge):
    """water_level_qc.fit_reference's robust fit of
    level ~ a * gauge(t - lag) + b on the readings in `use`, and the level
    it then predicts on the whole grid. (None, NaNs) without a fit.
    fit_reference uses at most the last REF_FIT_DAYS (30) of the
    readings it is given, so in the combined report over windows
    further apart than that the transfer comes from the latest 30 days;
    each window's own report fits its own."""
    import water_level_qc as wq
    grid = np.asarray(grid, float)
    nan = np.full(len(grid), np.nan)
    if gauge is None or len(gauge[0]) < 2:
        return None, nan
    m = np.asarray(use, bool) & np.isfinite(level)
    if m.sum() < MIN_FIT_POINTS:
        return None, nan
    fit = wq.fit_reference(grid[m], np.asarray(level, float)[m], gauge[0], gauge[1],
                           np.ones(int(m.sum()), bool))
    if fit is None:
        return None, nan
    a, lag_s, b, sigma = fit
    pred = a * wq.reference_at(gauge[0], gauge[1], grid - lag_s) + b
    return {"a": a, "lag_min": lag_s / 60.0, "b": b, "sigma": sigma}, pred


def qc_fail_share(grid, level, gauge, waves, masks) -> dict:
    """Share of a series' readings water_level_qc.run_qc would fail, by
    sea state (the station's own QC, so a method that writes fewer
    unusable readings in surf shows it here)."""
    import water_level_qc as wq
    ok = np.isfinite(level)
    out = {c: None for c in masks}
    if ok.sum() < 2:
        return out
    flags, _, _ = wq.run_qc(np.asarray(grid, float)[ok], np.asarray(level, float)[ok], gauge, waves)
    fail = np.zeros(len(grid), bool)
    fail[np.flatnonzero(ok)] = flags >= wq.FAIL
    for c, m in masks.items():
        n = int((m & ok).sum())
        out[c] = {"n": n, "fail": float((fail & m).sum() / n) if n else None}
    return out


def daily_mean_rms(grid, resid, use, min_fraction: float = MIN_DAY_FRACTION, step_s: float = GRID_S):
    """RMS of the UTC daily means of a residual, over days where `use`
    holds for at least min_fraction of the day's grid epochs."""
    grid = np.asarray(grid, float)
    resid = np.asarray(resid, float)
    use = np.asarray(use, bool) & np.isfinite(resid)
    days = np.floor(grid / 86400.0)
    means = []
    for dkey in np.unique(days):
        m = (days == dkey) & use
        if m.sum() >= min_fraction * 86400.0 / step_s:
            means.append(float(np.mean(resid[m])))
    if not means:
        return {"days": 0}
    return {"days": len(means), "rms": float(np.sqrt(np.mean(np.square(means))))}


def evaluate(grid, levels: dict, supported: dict, gauge=None, tide=None, waves=None,
             spline_name: str = "spline", rough_hs: float = ROUGH_HS,
             storm_hs: float = STORM_HS, arrays: dict | None = None) -> dict:
    """
    The whole comparison on one grid.
    grid: epochs (s, UTC). levels: {name: level on the grid, NaN = none}.
    supported: {name: bool on the grid}. gauge: (epochs, levels) or None.
    tide: the tide model on the grid (NAVD88) or None. waves: (epochs, hs,
    tp) or None. Returns plain dicts (see format_report); `arrays`, if
    given, receives the grid arrays behind them (hs, sea, gauge_pred).
    'common' is per series: an invsnr series with the spline, on the
    epochs both are supported ('spline_common' is the spline there);
    the spline on the epochs it and any invsnr series are supported.
    So each series' numbers, and its adoption verdict, do not depend on
    which other series are in the same call.
    """
    import water_level_qc as wq
    grid = np.asarray(grid, float)
    names = list(levels)
    hs = wq.waves_at(grid, waves)[0] if waves is not None else np.full(len(grid), np.nan)
    sea = sea_state(hs, rough_hs, storm_hs)
    masks = class_masks(sea)
    tide = np.asarray(tide, float) if tide is not None else np.full(len(grid), np.nan)

    sup = {n: np.isfinite(levels[n]) & np.asarray(supported[n], bool) for n in names}
    others = [n for n in names if n != spline_name]
    ref_sup = sup.get(spline_name, np.ones(len(grid), bool))
    common = {n: ref_sup & sup[n] for n in others}
    if spline_name in sup:
        any_other = np.zeros(len(grid), bool)
        for n in others:
            any_other |= sup[n]
        common[spline_name] = ref_sup & any_other if others else ref_sup
    calm = masks["calm"]

    out = {"names": names, "n_grid": int(len(grid)), "spline_name": spline_name,
           "sea_counts": {c: int((sea == c).sum()) for c in ("calm", "rough", "storm", "unknown")},
           "series": {}, "per_day": {}}
    days = np.floor(grid / 86400.0)
    fits, preds = {}, {}
    for n in names:
        fit_use = sup[n] & calm
        if fit_use.sum() < MIN_FIT_POINTS:          # no wave record, or too few calm readings
            fit_use = sup[n]
        fits[n], preds[n] = gauge_transfer(grid, np.asarray(levels[n], float), fit_use, gauge)

    def against(lv, pred, use):
        return {"gauge": {c: stats(lv[use & m], pred[use & m]) for c, m in masks.items()},
                "tide": {c: stats(lv[use & m], tide[use & m]) for c, m in masks.items()}}

    for n in names:
        lv = np.asarray(levels[n], float)
        pred, cm = preds[n], common[n]
        s = {
            "coverage": {c: {"values": float(np.isfinite(lv)[m].mean()) if m.any() else None,
                             "supported": float(sup[n][m].mean()) if m.any() else None}
                         for c, m in masks.items()},
            "gauge_fit": fits[n],
            "common_n": int(cm.sum()),
            "common_by_class": {c: int((cm & m).sum()) for c, m in masks.items()},
            "common": against(lv, pred, cm),
            "own": against(lv, pred, sup[n]),
            "unsupported": {"gauge": stats(lv[~sup[n]], pred[~sup[n]]),
                            "tide": stats(lv[~sup[n]], tide[~sup[n]])},
            "qc": qc_fail_share(grid, lv, gauge, waves, masks),
            "daily_gauge": daily_mean_rms(grid, lv - pred, cm),
            "daily_tide": daily_mean_rms(grid, lv - tide, cm),
        }
        if n != spline_name and spline_name in levels:
            ref = np.asarray(levels[spline_name], float)
            s["minus_spline"] = {c: stats(lv[cm & m], ref[cm & m]) for c, m in masks.items()}
            s["spline_common"] = against(ref, preds[spline_name], cm)
        out["series"][n] = s
        for dkey in np.unique(days):
            m = (days == dkey) & sup[n]
            iso = datetime.fromtimestamp(dkey * 86400.0, tz=timezone.utc).date().isoformat()
            row = out["per_day"].setdefault(iso, {})
            st = stats(lv[m], pred[m])
            row[n] = st.get("rmse")
            dm = days == dkey
            row["hs_mean"] = float(np.nanmean(hs[dm])) if np.isfinite(hs[dm]).any() else None
            row["hs_max"] = float(np.nanmax(hs[dm])) if np.isfinite(hs[dm]).any() else None
    if arrays is not None:
        arrays.update(hs=hs, sea=sea, gauge_pred=preds)
    return out


def rank_correlation(x, y):
    """Spearman's rank correlation and n (ties get their mean rank)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 3:
        return None, int(m.sum())

    def ranks(v):
        order = np.argsort(v, kind="stable")
        r = np.empty(len(v), float)
        r[order] = np.arange(len(v), dtype=float)
        for u in np.unique(v):
            t = v == u
            if t.sum() > 1:
                r[t] = r[t].mean()
        return r

    rx, ry = ranks(x[m]), ranks(y[m])
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None, int(m.sum())
    return float(np.corrcoef(rx, ry)[0, 1]), int(m.sum())


def adoption_check(result: dict, runs: dict) -> dict:
    """
    HOW TO ADOPT step 1 applied to this report. For each invsnr series
    in `runs` ({name: run_table rows}): a list of
    (test, invsnr value, spline value or limit, passed), where passed is
    None when the windows cannot answer it (e.g. no storm readings).
    Accuracy is the de-meaned SD on the calm epochs the series shares
    with the spline, against the gauge transfer and against the tide
    model; invsnr must be lower than the spline's there by
    ADOPT_MIN_GAIN_M or ADOPT_MIN_GAIN_FRAC of it, whichever is larger.
    """
    ser = result["series"]
    sp = ser.get(result["spline_name"])
    out = {}
    if sp is None:
        return out

    def lower_by_enough(x, ref):
        if x is None or ref is None:
            return None
        return x <= ref - max(ADOPT_MIN_GAIN_M, ADOPT_MIN_GAIN_FRAC * ref)

    def not_worse(x, ref, higher_is_better):
        if x is None or ref is None:
            return None
        return x >= ref if higher_is_better else x <= ref

    for name, rows in runs.items():
        s = ser.get(name)
        if s is None or "spline_common" not in s:
            continue
        tests = []
        for ref, label in (("gauge", "gauge SD, calm common epochs"),
                           ("tide", "tide-model SD, calm common epochs")):
            x, y = s["common"][ref]["calm"].get("sd"), s["spline_common"][ref]["calm"].get("sd")
            tests.append((label, x, y, lower_by_enough(x, y)))
        x, y = s["coverage"]["all"]["supported"], sp["coverage"]["all"]["supported"]
        tests.append(("supported coverage", x, y, not_worse(x, y, True)))
        for c in ("rough", "storm"):
            x, y = (s["qc"].get(c) or {}).get("fail"), (sp["qc"].get(c) or {}).get("fail")
            tests.append((f"QC fail share, {c}", x, y, not_worse(x, y, False)))
        tried = [r for r in rows if r["status"] != "no_snr"]
        ran = [r for r in tried if r["status"] == "ok" and r.get("snr_fit_ok") is not False]
        frac = len(ran) / len(tried) if tried else None
        tests.append(("days ran (of days with SNR data)", frac, ADOPT_MIN_DAYS_RAN,
                      None if frac is None else frac >= ADOPT_MIN_DAYS_RAN))
        out[name] = tests
    return out


def adoption_verdict(tests) -> str:
    """One word for a series' adoption tests: 'meets', 'fails' or
    'undecided' (a test the windows could not answer, and none failed)."""
    if any(t[3] is False for t in tests):
        return "fails"
    if any(t[3] is None for t in tests):
        return "undecided"
    return "meets"


def max_gap_minutes(arc_unix, first: date, last: date, day: date | None = None):
    """invsnr's gap rule quantity (spline_functions.py:883-892): the
    largest gap between accepted arcs, counting the run's start and end.
    With `day`, only the gaps that reach into that day."""
    if arc_unix is None:
        return None
    t = np.sort(np.array([a for a in arc_unix if a is not None], float))
    edges = np.concatenate(([day_start(first)], t, [day_start(last) + 86400.0]))
    a, b = edges[:-1], edges[1:]
    if day is not None:
        d0 = day_start(day)
        touch = (b > d0) & (a < d0 + 86400.0)
        a, b = a[touch], b[touch]
    return float(np.max(b - a) / 60.0) if len(a) else None


def run_table(runs_results, settings: InvsnrSettings) -> list[dict]:
    """One row per core day: how its invsnr run went."""
    rows = []
    for run, res in runs_results:
        spec = res.get("spec", {})
        first = date.fromisoformat(spec.get("first", run.first.isoformat()))
        last = date.fromisoformat(spec.get("last", run.last.isoformat()))
        arcs = (res.get("arcs") or {}).get("unix")
        d0 = day_start(run.day)
        n_core = (sum(1 for a in arcs if a is not None and d0 <= a < d0 + 86400.0)
                  if arcs else None)
        fits = res.get("fits") or []
        snr_fit = fits[-1] if len(fits) >= 2 else None     # the second fit is the SNR fit
        rho = res.get("roughness")
        message = (res.get("message") or "").strip().splitlines()
        rows.append({
            "day": run.day.isoformat(), "status": res.get("status"), "reason": res.get("reason"),
            "message": message[-1] if message else "",
            "span": f"{first.isoformat()}..{last.isoformat()}",
            "pad_dropped": res.get("pad_dropped") or [],
            "arcs_core_day": n_core,
            "max_gap_min": max_gap_minutes(arcs, first, last) if arcs else None,
            "core_gap_min": max_gap_minutes(arcs, first, last, run.day) if arcs else None,
            "gap_limit_min": 1.05 * settings.knot_space_h * 60.0,
            "roughness_m2": rho,
            "roughness_sigma_m": float(np.sqrt(rho)) if rho is not None and rho > 0 else None,
            "snr_fit_rms": snr_fit.get("rms") if snr_fit else None,
            "snr_fit_ok": snr_fit.get("success") if snr_fit else None,
            "nfev": snr_fit.get("nfev") if snr_fit else None,
            "runtime_s": res.get("runtime_s"),
        })
    return rows


# ---------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------

def _f(v, fmt="{:.3f}", none="-"):
    return none if v is None or (isinstance(v, float) and not np.isfinite(v)) else fmt.format(v)


def _pct(v):
    return _f(v, "{:.0%}")


def format_report(title: str, result: dict, runs: dict, settings_list, notes=()) -> str:
    """The evaluation as text. runs: {series name: run_table rows}."""
    L = []
    bar = "=" * 78
    L += [bar, f"  {title}", bar]
    sc = result["sea_counts"]
    L.append(f"  grid {result['n_grid']} epochs ({GRID_S // 60} min); sea state (buoy Hs): calm {sc['calm']}, "
             f"rough {sc['rough']}, storm {sc['storm']}, unknown {sc['unknown']}")
    for s in settings_list:
        L.append(f"  {s.name()}: {s.signal} {s.constel or 'G+R+E'}, RH {s.rh_limits[0]:g}-{s.rh_limits[1]:g} m, "
                 f"e {s.elevation_limits[0]:g}-{s.elevation_limits[1]:g}, az {s.azimuth_limits[0]:g}-"
                 f"{s.azimuth_limits[1]:g}, knots {s.knot_space_h:g} h, dec {s.decimate_s} s, "
                 f"peak2noise {s.peak2noise:g}, rough_in {s.rough_in:g}, risky {s.risky}, "
                 f"refraction {s.refraction}, pad {s.pad_days} d")
    for n in notes:
        L.append(f"  {n}")
    names = result["names"]
    ser = result["series"]
    w = max(len(n) for n in names) + 2

    L += ["", "  COVERAGE (share of the grid with a value / with a retrieval within 1 h)",
          f"    {'':<{w}}" + "".join(f"{c:>17}" for c in CLASSES)]
    for n in names:
        cov = ser[n]["coverage"]
        L.append(f"    {n:<{w}}" + "".join(
            f"{_pct(cov[c]['values']):>8} /{_pct(cov[c]['supported']):>7}" for c in CLASSES))

    L += ["", "  AGAINST THE CHATHAM GAUGE, a * gauge(t - lag) + b fitted to each series",
          "  (calm supported readings), on the COMMON epochs: each invsnr series where it and the",
          "  spline are both supported; the spline where it and any invsnr series are:",
          f"    {'':<{w}}{'a':>6}{'lag':>6}{'b':>8}  " + "".join(f"{c + ' RMSE':>12}" for c in CLASSES)
          + f"{'daily-mean RMS':>16}{'own supported RMSE':>22}"]
    for n in names:
        fit = ser[n]["gauge_fit"] or {}
        g = ser[n]["common"]["gauge"]
        dm = ser[n]["daily_gauge"]
        L.append(f"    {n:<{w}}{_f(fit.get('a'), '{:.2f}'):>6}{_f(fit.get('lag_min'), '{:+.0f}'):>6}"
                 f"{_f(fit.get('b'), '{:+.3f}'):>8}  "
                 + "".join(f"{_f(g[c].get('rmse')):>7} n{g[c]['n']:<4}" for c in CLASSES)
                 + f"{_f(dm.get('rms')):>10} ({dm['days']} d)"
                 + f"{_f(ser[n]['own']['gauge']['all'].get('rmse')):>13} n{ser[n]['own']['gauge']['all']['n']}")
    L.append("    bias by sea state (level above the calm-fitted gauge transfer; setup shows here):")
    for n in names:
        g = ser[n]["common"]["gauge"]
        L.append(f"    {n:<{w}}" + "".join(f"{c}: {_f(g[c].get('bias'), '{:+.3f}')}  " for c in CLASSES))

    L += ["", "  AGAINST THE TIDE MODEL on NAVD88 (no fitted parameter), common epochs:",
          f"    {'':<{w}}" + "".join(f"{c + ' bias/RMSE/SD':>24}" for c in CLASSES) + f"{'r':>8}"]
    for n in names:
        t = ser[n]["common"]["tide"]
        L.append(f"    {n:<{w}}" + "".join(
            f"{_f(t[c].get('bias'), '{:+.3f}'):>8}{_f(t[c].get('rmse')):>7}{_f(t[c].get('sd')):>7}  "
            for c in CLASSES) + f"{_f(t['all'].get('r'), '{:.3f}'):>8}")

    others = [n for n in names if n != result["spline_name"] and "minus_spline" in ser[n]]
    if others:
        L += ["", "  INVSNR MINUS SPLINE, common epochs (bias = the datum step adoption would make):"]
        for n in others:
            m = ser[n]["minus_spline"]
            L.append(f"    {n:<{w}}" + "".join(
                f"{c}: {_f(m[c].get('bias'), '{:+.3f}')} sd {_f(m[c].get('sd'))}  " for c in CLASSES))

    L += ["", "  QC: share of each series' readings water_level_qc.run_qc would fail",
          f"    {'':<{w}}" + "".join(f"{c:>12}" for c in CLASSES)]
    for n in names:
        q = ser[n]["qc"]
        L.append(f"    {n:<{w}}" + "".join(
            f"{_pct((q[c] or {}).get('fail')):>6} n{(q[c] or {}).get('n', 0):<5}" for c in CLASSES))

    L += ["", "  IN GAPS: values written with no retrieval within 1 h (against each own gauge transfer)"]
    for n in names:
        u = ser[n]["unsupported"]["gauge"]
        L.append(f"    {n:<{w}} n {u['n']:<5} bias {_f(u.get('bias'), '{:+.3f}')}  RMSE {_f(u.get('rmse'))}")

    for n, rows in runs.items():
        L += ["", f"  PER DAY, {n}: run status, accepted arcs in the day, largest gap reaching into the "
                  f"day / in the whole run (invsnr stops",
              "  above the limit unless --risky), roughness rho (m^2) and sqrt(rho) (m), SNR-fit RMS, "
              "buoy Hs, and each series' RMSE",
              "  against its own gauge transfer on its OWN supported readings that day",
              f"    {'day':<11}{'status':<8}{'arcs':>5}{'gap day/run/limit':>20}{'rho':>9}{'sqrt':>7}"
              f"{'fitRMS':>8}{'Hs mean/max':>13}{'RMSE spline':>12}{'RMSE invsnr':>12}"]
        for r in rows:
            day_row = result["per_day"].get(r["day"], {})
            L.append(f"    {r['day']:<11}{str(r['status']):<8}{_f(r['arcs_core_day'], '{:d}'):>5}"
                     f"{_f(r.get('core_gap_min'), '{:.0f}'):>7}/{_f(r['max_gap_min'], '{:.0f}'):>5}"
                     f"/{r['gap_limit_min']:<6.0f}"
                     f"{_f(r['roughness_m2'], '{:.4f}'):>9}{_f(r['roughness_sigma_m'], '{:.3f}'):>7}"
                     f"{_f(r['snr_fit_rms'], '{:.2f}'):>8}"
                     f"{_f(day_row.get('hs_mean'), '{:.1f}'):>7}/{_f(day_row.get('hs_max'), '{:.1f}'):<5}"
                     f"{_f(day_row.get(result['spline_name'])):>12}{_f(day_row.get(n)):>12}")
            if r.get("pad_dropped"):
                L.append(f"      ran {r['span']} without the pad day {' and '.join(r['pad_dropped'])}: "
                         f"the padded run stopped on a gap there")
            if r["status"] != "ok" and r["message"]:
                L.append(f"      {r['message'][:100]}")
        rho = [r["roughness_m2"] for r in rows if r["status"] == "ok"]
        hs = [result["per_day"].get(r["day"], {}).get("hs_mean") for r in rows if r["status"] == "ok"]
        rc, nc = rank_correlation([np.nan if v is None else v for v in rho],
                                  [np.nan if v is None else v for v in hs])
        ok_days = sum(1 for r in rows if r["status"] == "ok")
        L.append(f"    {ok_days} of {len(rows)} days ran; daily roughness vs buoy Hs: Spearman "
                 f"{_f(rc, '{:+.2f}')} over {nc} days (a usable sea-state term would be clearly positive;")
        L.append("    each rho is fitted over its whole padded run, so neighbouring days are not independent)")

    adopt = adoption_check(result, runs)
    if adopt:
        L += ["", "  ADOPTION RULE (HOW TO ADOPT, step 1) on these windows alone; the rule asks for at "
                  "least two calm weeks and a storm.",
              "  Each invsnr series against the spline on the epochs both cover; evidence = those epochs."]
        for n, tests in adopt.items():
            cc = ser[n]["common_by_class"]
            L.append(f"    {n}: {adoption_verdict(tests).upper()} (evidence: common epochs calm {cc['calm']} "
                     f"({cc['calm'] * GRID_S / 86400:.1f} d), rough {cc['rough']}, storm {cc['storm']})")
            for label, x, y, ok in tests:
                fmt = _pct if ("share" in label or "coverage" in label or "days" in label) else _f
                L.append(f"      {label:<36}{fmt(x):>8}  vs {fmt(y):>7}   "
                         f"{'pass' if ok else ('FAIL' if ok is False else 'cannot tell')}")

    L += ["", "  Reading it: compare rows on the COMMON epochs; RMSE^2 = bias^2 + SD^2. Storm rows",
          "  measure how the methods differ in surf (setup, runup), not an error against truth.",
          "  The adoption rule is in this script's docstring (HOW TO ADOPT)."]
    return "\n".join(L)


def write_series_csv(path: Path, grid, levels: dict, supported: dict, gauge_pred: dict, tide, hs, sea):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        names = list(levels)
        head = ["time_utc", "epoch", "hs_m", "sea_state", "tide_navd88"]
        for n in names:
            key = n.replace(" ", "_")
            head += [f"{key}_level", f"{key}_supported", f"{key}_gauge_fit"]
        w.writerow(head)
        for i, e in enumerate(grid):
            row = [datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"), int(e),
                   _f(hs[i], "{:.2f}", ""), sea[i], _f(tide[i], "{:.3f}", "")]
            for n in names:
                row += [_f(levels[n][i], "{:.3f}", ""), int(bool(supported[n][i])),
                        _f(gauge_pred[n][i], "{:.3f}", "")]
            w.writerow(row)


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def build_grid(windows) -> np.ndarray:
    """GRID_S epochs covering every window day (00:00 to the last 23:30)."""
    pts = []
    for d in window_days(windows):
        t0 = day_start(d)
        pts.append(t0 + np.arange(0, 86400, GRID_S, dtype=float))
    return np.unique(np.concatenate(pts)) if pts else np.array([], float)


def compare(windows, settings_list, results_by_label, ctx: RunContext, args, cfg, hortho, tag) -> str:
    """Build the series for the windows and write the report files."""
    grid = build_grid(windows)
    first, last = window_days(windows)[0], window_days(windows)[-1]
    levels, supported, notes = {}, {}, []

    s_ep, s_lv = load_spline_series(Path(args.spline_file), cfg)
    in_win = (s_ep >= grid[0] - 86400) & (s_ep <= grid[-1] + 86400)
    if in_win.sum() == 0:
        notes.append(f"the spline has no rows in the window ({args.spline_file})")
    levels["spline"] = sample_on_grid(s_ep[in_win], s_lv[in_win], grid, 1.5 * native_step(s_ep[in_win]))
    s_arcs = results_arc_epochs(ctx.live_refl, args.station, first, last, tuple(args.spline_azimuth))
    supported["spline"] = supported_mask(grid, s_arcs)
    notes.append(f"spline: {args.spline_file}; support from {len(s_arcs)} gnssir arcs in azimuth "
                 f"{args.spline_azimuth[0]:g}-{args.spline_azimuth[1]:g}")

    runs = {}
    in_windows = set(window_days(windows))
    for s in settings_list:
        rr = [(r, res) for r, res in results_by_label[s.label()] if r.day in in_windows]
        i_ep, i_lv, i_arcs = invsnr_levels(rr, s, ctx, hortho)
        levels[s.name()] = sample_on_grid(i_ep, i_lv, grid, 1.5 * s.delta_out_s)
        supported[s.name()] = supported_mask(grid, i_arcs) & np.isfinite(levels[s.name()])
        runs[s.name()] = run_table(rr, s)
    notes.append(f"invsnr RH -> NAVD88 level with Hortho {hortho:.3f} m")

    start = datetime.fromtimestamp(grid[0] - 2 * 86400, tz=timezone.utc)
    end = datetime.fromtimestamp(grid[-1] + 2 * 86400, tz=timezone.utc)
    gauge, waves = load_references(Path(args.gauge_cache), Path(args.wave_cache),
                                   ctx.out_dir / "cache", start, end, offline=args.offline)
    if gauge is None:
        notes.append("no Chatham gauge data: the gauge comparison is empty")
    if waves is None:
        notes.append("no buoy wave data: every reading is 'unknown' sea state")
    tide_on_grid = np.full(len(grid), np.nan)
    if args.tide_file and args.tide_value_col:
        offset = args.tide_datum_offset if args.tide_datum_offset is not None else tide_model_to_navd88(cfg)
        tm = load_tide_model(args.tide_file, args.tide_time_col, args.tide_value_col, offset)
        if tm is not None:
            tide_on_grid = sample_on_grid(tm[0], tm[1], grid, 1.5 * native_step(tm[0]))
            notes.append(f"tide model {Path(args.tide_file).name} [{args.tide_value_col}] {offset:+.3f} m onto NAVD88")
    else:
        notes.append("no tide model configured: the tide comparison is empty")

    arrays = {}
    result = evaluate(grid, levels, supported, gauge, tide_on_grid, waves,
                      rough_hs=args.rough_hs, storm_hs=args.storm_hs, arrays=arrays)
    title = "INVERSE MODELLING (invsnr) vs THE PUBLISHED SPLINE -- " + ", ".join(
        f"{a.isoformat()}..{b.isoformat()}" for a, b in windows)
    text = format_report(title, result, runs, settings_list, notes)

    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    (ctx.out_dir / f"report_{tag}.txt").write_text(text + "\n")
    summary = {"windows": [[a.isoformat(), b.isoformat()] for a, b in windows],
               "settings": [asdict(s) for s in settings_list], "hortho": hortho,
               "result": result, "runs": runs, "notes": notes,
               "adoption": {n: {"verdict": adoption_verdict(t),
                                "tests": [{"test": a, "invsnr": x, "spline_or_limit": y, "passed": ok}
                                          for a, x, y, ok in t]}
                            for n, t in adoption_check(result, runs).items()},
               "made": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    (ctx.out_dir / f"summary_{tag}.json").write_text(json.dumps(_jsonable(summary), indent=1))
    write_series_csv(ctx.out_dir / f"series_{tag}.csv", grid, levels, supported, arrays["gauge_pred"],
                     tide_on_grid, arrays["hs"], arrays["sea"])
    return text


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--worker"]:
        return worker_main(argv[1])

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--window", action="append", required=True, type=parse_window,
                   help="UTC days START[:END], inclusive, e.g. 2026-09-20:2026-09-30 (repeatable)")
    p.add_argument("--station", default=None, help="gnssrefl 4-character code (default: station.json)")
    p.add_argument("--station-json", default=str(STATION_JSON))
    p.add_argument("--refl-code", default=None,
                   help="the station's REFL_CODE, only read (default: gnssrefl_refl_code in station.json, "
                        "else $REFL_CODE, else products/refl_code)")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="everything this writes (default: %(default)s)")
    p.add_argument("--knot-space", type=float, nargs="+", default=list(KNOT_SPACE_H),
                   help="invsnr knot spacing(s) in hours, one set of runs each (default: 2)")
    p.add_argument("--signal", default=SIGNAL, help="invsnr signal (default: %(default)s)")
    p.add_argument("--constel", default=None,
                   help="invsnr's -constel: G, R, E or C alone, GPS+Gal, or withBeidou (G+R+E+C) "
                        "(default: G+R+E, invsnr's)")
    p.add_argument("--rh", type=float, nargs=2, default=list(RH_LIMITS), metavar=("MIN", "MAX"))
    p.add_argument("--elevation", type=float, nargs=2, default=list(ELEVATION_LIMITS), metavar=("MIN", "MAX"))
    p.add_argument("--azimuth", type=float, nargs=2, default=list(AZIMUTH_LIMITS), metavar=("MIN", "MAX"))
    p.add_argument("--dec", type=int, default=DECIMATE_S, help="SNR decimation, s (default: %(default)s)")
    p.add_argument("--peak2noise", type=float, default=PEAK2NOISE)
    p.add_argument("--rough-in", type=float, default=ROUGH_IN, help="starting roughness (default: %(default)s)")
    p.add_argument("--delta-out", type=int, default=DELTA_OUT_S)
    p.add_argument("--pad-days", type=int, default=PAD_DAYS)
    p.add_argument("--risky", action="store_true", help="let invsnr fit across gaps longer than the knot spacing")
    p.add_argument("--python", default=None, help="interpreter with gnssrefl (default: this one, else gnssrefl_venv)")
    p.add_argument("--timeout", type=float, default=RUN_TIMEOUT_S, help="seconds per invsnr run")
    p.add_argument("--rerun", action="store_true", help="run again even where a finished run exists")
    p.add_argument("--compare-only", action="store_true", help="do not run invsnr; compare the runs there are")
    p.add_argument("--keep-snr", action="store_true", help="keep the decimated SNR copies")
    p.add_argument("--spline-file", default=None, help="default: Files/<sta>/<sta>_spline_out.txt")
    p.add_argument("--spline-azimuth", type=float, nargs=2, default=list(AZIMUTH_LIMITS), metavar=("MIN", "MAX"),
                   help="subdaily's azimuth window, for the spline's support (default: 35 125)")
    p.add_argument("--gauge-cache", default=None, help="default: gauge_8447435.csv next to the spline")
    p.add_argument("--wave-cache", default=None, help="default: waves_44008.csv next to the gauge cache")
    p.add_argument("--offline", action="store_true", help="use the cached gauge/wave data only")
    p.add_argument("--tide-file", default=None, help="default: tide_model_file in station.json")
    p.add_argument("--tide-value-col", default=None, help="default: tide_model_value_column")
    p.add_argument("--tide-time-col", default=None, help="default: tide_model_time_column, else 'time'")
    p.add_argument("--tide-datum-offset", type=float, default=None,
                   help="m added to the tide model (default: tide_model_navd88_offset, else +0.09)")
    p.add_argument("--hortho", type=float, default=None, help="NAVD88 antenna height (default: station_datum)")
    p.add_argument("--rough-hs", type=float, default=ROUGH_HS)
    p.add_argument("--storm-hs", type=float, default=STORM_HS)
    args = p.parse_args(argv)

    cfg = load_station(args.station_json)
    import water_level_qc as wq
    args.tide_file = args.tide_file or cfg.get("tide_model_file")
    args.tide_value_col = args.tide_value_col or cfg.get("tide_model_value_column")
    args.tide_time_col = args.tide_time_col or cfg.get("tide_model_time_column") or "time"
    args.station = (args.station or cfg.get("gnssrefl_station_code") or "").lower()
    if len(args.station) != 4:
        p.error("need the 4-character gnssrefl station code (--station, or gnssrefl_station_code in station.json)")
    args.refl_code = (args.refl_code or cfg.get("gnssrefl_refl_code") or os.environ.get("REFL_CODE")
                      or str(PROJECT_DIR / "products" / "refl_code"))
    refl = Path(args.refl_code).resolve()
    files_dir = refl / "Files" / args.station
    args.spline_file = args.spline_file or str(files_dir / f"{args.station}_spline_out.txt")
    args.gauge_cache = args.gauge_cache or str(Path(args.spline_file).parent / f"gauge_{wq.GAUGE_STATION}.csv")
    args.wave_cache = args.wave_cache or str(Path(args.gauge_cache).parent / f"waves_{wq.WAVE_STATION}.csv")
    out_dir = Path(args.out_dir).resolve()
    if out_dir == refl or refl in out_dir.parents:
        p.error("--out-dir must be outside the live REFL_CODE (gnssrefl would see the copies)")

    hortho = args.hortho
    if hortho is None:
        hortho = navd88_hortho(cfg)
    if hortho is None and Path(args.spline_file).exists():
        h_file = spline_hortho(args.spline_file)
        hortho = None if h_file is None else h_file + spline_shift(args.spline_file, cfg, quiet=True)
    if hortho is None:
        p.error("no antenna height: set gnssrefl_orthometric_height in station.json or pass --hortho")
    lat, lon, ht = cfg.get("latitude"), cfg.get("longitude"), cfg.get("height")
    if None in (lat, lon, ht):
        if not args.compare_only:
            p.error("station.json needs latitude, longitude and (ellipsoidal) height for invsnr's refraction")
        lat, lon, ht = 0.0, 0.0, 0.0

    settings_list = [InvsnrSettings(
        station=args.station, knot_space_h=k, signal=args.signal.upper(), constel=args.constel,
        rh_limits=tuple(args.rh), elevation_limits=tuple(args.elevation), azimuth_limits=tuple(args.azimuth),
        decimate_s=args.dec, peak2noise=args.peak2noise, rough_in=args.rough_in, risky=args.risky,
        delta_out_s=args.delta_out, pad_days=args.pad_days)
        for k in args.knot_space]
    for s in settings_list:
        if abs(24.0 / s.knot_space_h - round(24.0 / s.knot_space_h)) > 1e-9:
            print(f"  note: {s.knot_space_h:g} h does not divide a day; invsnr spaces the knots evenly anyway")
    ctx = RunContext(live_refl=refl, out_dir=out_dir, lat=float(lat), lon=float(lon), ht=float(ht),
                     python=gnssrefl_python(args.python), timeout_s=args.timeout, rerun=args.rerun)
    windows = sorted(args.window)
    runs = plan_runs(windows, args.pad_days)

    results_by_label = {}
    for s in settings_list:
        done = []
        for i, run in enumerate(runs, 1):
            if args.compare_only:
                res = load_run(run, s, ctx)
            else:
                print(f"  [{i}/{len(runs)}] {s.name()} {run.day} (days {run.first}..{run.last}) ...",
                      end="", flush=True)
                res = execute_run(run, s, ctx)
                print(f" {res.get('status')} " + ("(reused; --rerun to run again)" if res.get("reused")
                                                  else f"({res.get('wall_s', res.get('runtime_s', 0))} s)"))
            done.append((run, res))
        results_by_label[s.label()] = done
    if not args.compare_only and not args.keep_snr:
        for s in settings_list:
            for ydir in ctx.private_refl(s).glob("[0-9][0-9][0-9][0-9]"):
                shutil.rmtree(ydir / "snr", ignore_errors=True)

    tag = "_".join(f"{a:%Y%m%d}-{b:%Y%m%d}" for a, b in windows)
    text = compare(windows, settings_list, results_by_label, ctx, args, cfg, hortho, tag)
    print(text)
    if len(windows) > 1:
        for w in windows:
            wtag = f"{w[0]:%Y%m%d}-{w[1]:%Y%m%d}"
            print(compare([w], settings_list, results_by_label, ctx, args, cfg, hortho, wtag))
    print(f"\n  Written to {out_dir} (report_{tag}.txt, summary_{tag}.json, series_{tag}.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
