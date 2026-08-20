#!/usr/bin/env python3
"""
MARCONI LONG-TERM GNSS-R / TIDE STABILITY V5

Purpose
-------
V5 is a diagnostic continuation of the established Marconi long-term
GNSS-R analysis.  It keeps the existing scientific track definition and
GOOD-track thresholds, but makes the reason for every track rejection
explicit.

It also fixes the GNSS-R rerun command for gnssrefl 4.1.5:

    gnssir usgs YEAR DOY -extension ocean17_23_l1_e5_13 -fr 1

Existing result files are NOT overwritten.  Missing result files are
processed only when --rerun-missing is supplied.

Track identity:
    satellite + frequency + rise/setting + azimuth cluster

Water level:
    GNSS_WL = H_ORTHO_M - RH

GOOD-track criteria:
    observations >= 14
    unique days   >= 14
    tide r        >= 0.90
    slope         0.85 to 1.15
    unit-slope RMS <= 30 cm
    azimuth SD    <= 1 deg

The +0.242 m datum test is diagnostic only and is never used to select
a GOOD track.

Outputs:
    marconi_longterm_track_diagnostics_v5.csv
    marconi_longterm_track_diagnostics_v5_summary.txt
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from openpyxl import load_workbook


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_DIR = Path.home() / "GNSS" / "v4.1"
REFL_CODE = BASE_DIR / "products" / "refl_code"

YEAR = 2026
STATION = "usgs"
GNSSIR_EXTENSION = "ocean17_23_l1_e5_13"

RESULT_DIR = (
    REFL_CODE
    / str(YEAR)
    / "results"
    / STATION
    / GNSSIR_EXTENSION
)

SNR_DIR = (
    REFL_CODE
    / str(YEAR)
    / "snr"
    / STATION
)

TIDE_FILE = BASE_DIR / "marconi_tides_sherwood.xlsx"

H_ORTHO_M = 18.665
PRIMARY_TIDE_MODEL = "EOT20_heightm"
DATUM_OFFSET_M = 0.242

MODELS = [
    "EOT20_heightm",
    "GOT5.5_heightm",
    "GOT5.6_heightm",
    "FES2022_heightm",
]

MIN_TRACK_N = 4
AZ_CLUSTER_TOL_DEG = 3.0

GOOD_MIN_N = 14
GOOD_MIN_DAYS = 14
GOOD_MIN_R = 0.90
GOOD_MIN_SLOPE = 0.85
GOOD_MAX_SLOPE = 1.15
GOOD_MAX_UNIT_RMS_CM = 30.0
GOOD_MAX_AZ_STD_DEG = 1.0

OUT_CSV = BASE_DIR / "marconi_longterm_track_diagnostics_v5.csv"
OUT_SUMMARY = (
    BASE_DIR / "marconi_longterm_track_diagnostics_v5_summary.txt"
)

RINEX_ROOTS = [
    BASE_DIR,
    Path.home(),
    Path("/mnt/I2Rgus_Data"),
]

RINEX_RX = re.compile(
    r"^USGS00USA_R_(?P<year>\d{4})(?P<doy>\d{3})0000_01D_01S_MO\."
    r"(?:rnx|crx)(?:\.(?:gz|Z))?$",
    re.I,
)


# ---------------------------------------------------------------------------
# BASIC UTILITIES
# ---------------------------------------------------------------------------

def finite(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except Exception:
        return None


def utc_hours_to_datetime(year, doy, utc_hours):
    return (
        datetime(year, 1, 1)
        + timedelta(days=doy - 1, hours=float(utc_hours))
    )


def pearson(x, y):
    x = np.asarray(x, float)
    y = np.asarray(y, float)

    if len(x) < 3:
        return math.nan

    if np.std(x) == 0 or np.std(y) == 0:
        return math.nan

    return float(np.corrcoef(x, y)[0, 1])


def rms(x):
    x = np.asarray(x, float)
    return float(np.sqrt(np.mean(x * x))) if len(x) else math.nan


def mae(x):
    x = np.asarray(x, float)
    return float(np.mean(np.abs(x))) if len(x) else math.nan


def circular_az_diff(a, b):
    d = abs(float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)


def banner(text):
    print()
    print("=" * 100)
    print(text)
    print("=" * 100)


# ---------------------------------------------------------------------------
# RINEX DISCOVERY
# ---------------------------------------------------------------------------

def discover_rinex():
    banner("SEARCHING FOR ALL LOCAL USGS RINEX FILES")

    found = {}

    for root in RINEX_ROOTS:
        if not root.exists():
            continue

        print("Searching:", root)

        cmd = [
            "find",
            str(root),
            "-type",
            "f",
            "(",
            "-iname",
            "USGS00USA_R_*_01D_01S_MO.rnx",
            "-o",
            "-iname",
            "USGS00USA_R_*_01D_01S_MO.rnx.gz",
            "-o",
            "-iname",
            "USGS00USA_R_*_01D_01S_MO.rnx.Z",
            "-o",
            "-iname",
            "USGS00USA_R_*_01D_01S_MO.crx",
            "-o",
            "-iname",
            "USGS00USA_R_*_01D_01S_MO.crx.gz",
            "-o",
            "-iname",
            "USGS00USA_R_*_01D_01S_MO.crx.Z",
            ")",
            "-print",
        ]

        try:
            p = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise SystemExit("ERROR: find command is unavailable.")

        if p.stderr.strip():
            for line in p.stderr.splitlines():
                if "Permission denied" in line:
                    continue
                if "lost+found" in line:
                    continue
                print("  find warning:", line)

        for line in p.stdout.splitlines():
            path = Path(line.strip())
            m = RINEX_RX.match(path.name)

            if not m:
                continue

            y = int(m.group("year"))
            doy = int(m.group("doy"))

            if y != YEAR:
                continue

            # Prefer the first discovered file.  Duplicate copies are
            # expected on this NUC.
            found.setdefault(doy, path)

    days = sorted(found)

    print("Unique RINEX days discovered:", len(days))
    print("DOYs:", " ".join(str(x) for x in days))

    for doy in days:
        print(f"  {YEAR} DOY {doy}: {found[doy]}")

    return found


# ---------------------------------------------------------------------------
# GNSS-IR PROCESSING
# ---------------------------------------------------------------------------

def run_missing_gnssir(rinex_days):
    """
    Process only missing result files.

    This is deliberately conservative: existing result files are never
    overwritten.  The command syntax is the verified gnssrefl 4.1.5 form.
    """

    banner("CHECKING GNSS-IR RESULT FILES")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    missing = [
        doy
        for doy in sorted(rinex_days)
        if not (
            RESULT_DIR / f"{doy}.txt"
        ).exists()
    ]

    print("RINEX days:", len(rinex_days))
    print("Missing result files:", len(missing))

    if not missing:
        print("All discovered RINEX days already have result files.")
        return

    for doy in missing:
        result_file = RESULT_DIR / f"{doy}.txt"

        snr_file = SNR_DIR / f"usgs{doy}0.26.snr66.gz"

        banner(f"PROCESSING MISSING GNSS-IR: DOY {doy}")

        print("Expected SNR:", snr_file)

        if not snr_file.exists():
            print("WARNING: SNR file does not exist; skipping.")
            continue

        cmd = [
            "gnssir",
            STATION,
            str(YEAR),
            str(doy),
            "-extension",
            GNSSIR_EXTENSION,
            "-fr",
            "1",
            "-nooverwrite",
            "True",
        ]

        print("COMMAND:", " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd,
                cwd=BASE_DIR,
                text=True,
            )
        except FileNotFoundError:
            print("ERROR: gnssir was not found in PATH.")
            print("Activate gnssrefl_venv before running this script.")
            return

        if result_file.exists() and result_file.stat().st_size > 0:
            print("VERIFIED RESULT:", result_file)
        else:
            print(
                "No result file was created.  This is normal when "
                "gnssrefl finds no retrievals surviving QC."
            )

        if proc.returncode != 0:
            print("gnssir return code:", proc.returncode)


# ---------------------------------------------------------------------------
# TIDE MODEL
# ---------------------------------------------------------------------------

def load_tide_data():
    banner("LOADING MARCONI / SHERWOOD TIDE DATA")

    if not TIDE_FILE.exists():
        raise SystemExit(f"Tide file not found: {TIDE_FILE}")

    wb = load_workbook(TIDE_FILE, data_only=True)
    ws = wb[wb.sheetnames[0]]

    header = [c.value for c in ws[1]]

    if "time" not in header:
        raise SystemExit("Tide workbook has no 'time' column.")

    for model in MODELS:
        if model not in header:
            raise SystemExit(
                f"Tide workbook has no '{model}' column."
            )

    ti = header.index("time")
    mi = {m: header.index(m) for m in MODELS}

    times = []
    values = {m: [] for m in MODELS}

    for row in ws.iter_rows(min_row=2, values_only=True):
        t = row[ti]

        if not isinstance(t, datetime):
            continue

        times.append(t)

        for model in MODELS:
            v = finite(row[mi[model]])
            values[model].append(
                v if v is not None else math.nan
            )

    wb.close()

    if len(times) < 2:
        raise SystemExit("Not enough tide records.")

    x0 = times[0]
    epoch = np.asarray(
        [(t - x0).total_seconds() for t in times],
        dtype=float,
    )

    arrays = {
        m: np.asarray(values[m], dtype=float)
        for m in MODELS
    }

    def tide_at(dt, model):
        x = (dt - x0).total_seconds()

        if x < epoch[0] or x > epoch[-1]:
            return math.nan

        y = arrays[model]
        good = np.isfinite(y)

        if good.sum() < 2:
            return math.nan

        return float(
            np.interp(
                x,
                epoch[good],
                y[good],
            )
        )

    print("Tide sheet:", ws.title)
    print("Tide records:", len(times))
    print("Tide interval:", times[0], "through", times[-1])

    return tide_at, times[0], times[-1]


# ---------------------------------------------------------------------------
# GNSS-R RESULT LOADING
# ---------------------------------------------------------------------------

def result_files():
    if not RESULT_DIR.exists():
        raise SystemExit(
            f"GNSS-R result directory not found: {RESULT_DIR}"
        )

    files = []

    for path in sorted(RESULT_DIR.glob("*.txt")):
        try:
            doy = int(path.stem)
        except ValueError:
            continue

        if 1 <= doy <= 366:
            files.append(path)

    return files


def parse_result(path):
    rows = []

    for line in path.read_text(
        errors="replace"
    ).splitlines():

        line = line.strip()

        if not line or line.startswith("%"):
            continue

        c = line.split()

        if len(c) < 17:
            continue

        try:
            year = int(float(c[0]))
            doy = int(float(c[1]))
            rh = float(c[2])
            sat = int(float(c[3]))
            utc_hours = float(c[4])
            az = float(c[5])
            amp = float(c[6])
            emin = float(c[7])
            emax = float(c[8])
            nobs = int(float(c[9]))
            freq = int(float(c[10]))
            rise = int(float(c[11]))
            pkn = float(c[13])
            delt = float(c[14])
        except (ValueError, TypeError):
            continue

        if freq != 1:
            continue

        dt = utc_hours_to_datetime(
            year,
            doy,
            utc_hours,
        )

        rows.append(
            {
                "year": year,
                "doy": doy,
                "datetime_utc": dt,
                "sat": sat,
                "freq": freq,
                "RH_m": rh,
                "GNSS_WL_m": H_ORTHO_M - rh,
                "az_deg": az,
                "Amp": amp,
                "PkNoise": pkn,
                "eminO_deg": emin,
                "emaxO_deg": emax,
                "NumbOf": nobs,
                "rise": rise,
                "DelT_min": delt,
                "source_file": str(path),
            }
        )

    return rows


def load_results():
    banner("LOADING ESTABLISHED GNSS-IR RESULT FILES")

    files = result_files()

    print("Selected configuration:", GNSSIR_EXTENSION)
    print("Selected directory:", RESULT_DIR)
    print("Daily result files selected:", len(files))

    rows = []

    for path in files:
        parsed = parse_result(path)

        if parsed:
            print(
                f"{path.stem:>4} : {len(parsed):5d} observations"
            )
            rows.extend(parsed)

    # Remove exact duplicate observations.  Duplicate copies can exist
    # because result directories were assembled from multiple runs.
    unique = {}

    for row in rows:
        key = (
            row["datetime_utc"],
            row["sat"],
            row["freq"],
            row["rise"],
            round(row["az_deg"], 6),
            round(row["RH_m"], 6),
        )
        unique[key] = row

    rows = sorted(
        unique.values(),
        key=lambda r: r["datetime_utc"],
    )

    print("Unique GPS L1 GNSS-R observations:", len(rows))

    return rows


# ---------------------------------------------------------------------------
# TIDE MATCHING
# ---------------------------------------------------------------------------

def add_tides(rows, tide_at):
    matched = []

    for row in rows:
        for model in MODELS:
            row[model] = tide_at(
                row["datetime_utc"],
                model,
            )

        row["PRIMARY_TIDE_m"] = row[
            PRIMARY_TIDE_MODEL
        ]

        if math.isfinite(
            row["PRIMARY_TIDE_m"]
        ):
            matched.append(row)

    print(
        "GNSS-R observations matched to tide:",
        len(matched),
    )

    if matched:
        print(
            "Observation interval:",
            min(r["datetime_utc"] for r in matched),
            "through",
            max(r["datetime_utc"] for r in matched),
        )

    return matched


# ---------------------------------------------------------------------------
# TRACK IDENTIFICATION
# ---------------------------------------------------------------------------

def cluster_track_rows(rows):
    """
    Same physical-ish track definition used by the established V2:

        satellite + frequency + rise/setting + azimuth cluster
    """

    base = defaultdict(list)

    for row in rows:
        base[
            (
                row["sat"],
                row["freq"],
                row["rise"],
            )
        ].append(row)

    tracks = []

    for key, group in base.items():
        group = sorted(
            group,
            key=lambda r: r["az_deg"],
        )

        current = []
        prev_az = None

        for row in group:
            if (
                prev_az is None
                or circular_az_diff(
                    row["az_deg"],
                    prev_az,
                ) <= AZ_CLUSTER_TOL_DEG
            ):
                current.append(row)
            else:
                if len(current) >= MIN_TRACK_N:
                    tracks.append(current)
                current = [row]

            prev_az = row["az_deg"]

        if len(current) >= MIN_TRACK_N:
            tracks.append(current)

    return tracks


# ---------------------------------------------------------------------------
# TRACK STATISTICS + EXPLICIT FAILURE FLAGS
# ---------------------------------------------------------------------------

def analyze_track(group, track_id):
    wl = np.asarray(
        [r["GNSS_WL_m"] for r in group],
        dtype=float,
    )

    tide = np.asarray(
        [r["PRIMARY_TIDE_m"] for r in group],
        dtype=float,
    )

    az = np.asarray(
        [r["az_deg"] for r in group],
        dtype=float,
    )

    pkn = np.asarray(
        [r["PkNoise"] for r in group],
        dtype=float,
    )

    amp = np.asarray(
        [r["Amp"] for r in group],
        dtype=float,
    )

    valid = (
        np.isfinite(wl)
        & np.isfinite(tide)
    )

    w = wl[valid]
    t = tide[valid]

    if len(w) >= 3:
        tide_r = pearson(w, t)

        coeff = np.polyfit(
            t,
            w,
            1,
        )

        slope = float(coeff[0])
        intercept = float(coeff[1])

        # Unit-slope calibration constant.
        C = float(np.mean(w - t))

        raw_resid = w - t
        unit_resid = w - C - t
        free_resid = (
            w
            - (
                slope * t
                + intercept
            )
        )

        raw_rms_cm = rms(raw_resid) * 100.0
        unit_rms_cm = rms(unit_resid) * 100.0
        free_rms_cm = rms(free_resid) * 100.0

        raw_mae_cm = mae(raw_resid) * 100.0
        unit_mae_cm = mae(unit_resid) * 100.0

        plus_resid = (
            w
            + DATUM_OFFSET_M
            - t
        )

        plus0242_rms_cm = (
            rms(plus_resid) * 100.0
        )

        plus0242_mae_cm = (
            mae(plus_resid) * 100.0
        )

    else:
        tide_r = math.nan
        slope = math.nan
        intercept = math.nan
        C = math.nan

        raw_rms_cm = math.nan
        unit_rms_cm = math.nan
        free_rms_cm = math.nan

        raw_mae_cm = math.nan
        unit_mae_cm = math.nan

        plus0242_rms_cm = math.nan
        plus0242_mae_cm = math.nan

    n = len(group)

    days = sorted(
        {
            r["doy"]
            for r in group
        }
    )

    n_days = len(days)

    duration_days = (
        group[-1]["datetime_utc"]
        - group[0]["datetime_utc"]
    ).total_seconds() / 86400.0

    az_std = float(
        np.std(az)
    )

    # Explicit pass/fail for every criterion.
    pass_n = n >= GOOD_MIN_N
    pass_days = n_days >= GOOD_MIN_DAYS
    pass_r = (
        math.isfinite(tide_r)
        and tide_r >= GOOD_MIN_R
    )
    pass_slope = (
        math.isfinite(slope)
        and GOOD_MIN_SLOPE <= slope <= GOOD_MAX_SLOPE
    )
    pass_rms = (
        math.isfinite(unit_rms_cm)
        and unit_rms_cm <= GOOD_MAX_UNIT_RMS_CM
    )
    pass_az = (
        math.isfinite(az_std)
        and az_std <= GOOD_MAX_AZ_STD_DEG
    )

    failures = []

    if not pass_n:
        failures.append("N")

    if not pass_days:
        failures.append("DAYS")

    if not pass_r:
        failures.append("R")

    if not pass_slope:
        failures.append("SLOPE")

    if not pass_rms:
        failures.append("RMS")

    if not pass_az:
        failures.append("AZ")

    good = len(failures) == 0

    # Preserve the established long-term score for ranking.
    corr_score = (
        abs(tide_r)
        if math.isfinite(tide_r)
        else 0.0
    )

    slope_score = (
        max(
            0.0,
            1.0
            - min(
                1.0,
                abs(slope - 1.0),
            ),
        )
        if math.isfinite(slope)
        else 0.0
    )

    rms_score = (
        max(
            0.0,
            1.0
            - min(
                1.0,
                unit_rms_cm / 25.0,
            ),
        )
        if math.isfinite(unit_rms_cm)
        else 0.0
    )

    pkn_mean = float(
        np.nanmean(pkn)
    )

    amp_mean = float(
        np.nanmean(amp)
    )

    pkn_score = max(
        0.0,
        min(
            1.0,
            (pkn_mean - 2.8) / 1.2,
        ),
    )

    amp_score = max(
        0.0,
        min(
            1.0,
            amp_mean / 50.0,
        ),
    )

    az_score = max(
        0.0,
        min(
            1.0,
            1.0 - az_std / 2.0,
        ),
    )

    persistence = max(
        0.0,
        min(
            1.0,
            n_days / 20.0,
        ),
    )

    score = (
        0.30 * corr_score
        + 0.20 * slope_score
        + 0.20 * rms_score
        + 0.10 * pkn_score
        + 0.08 * amp_score
        + 0.07 * az_score
        + 0.05 * persistence
    )

    return {
        "track_id": track_id,
        "sat": group[0]["sat"],
        "freq": group[0]["freq"],
        "rise": group[0]["rise"],
        "n": n,
        "n_valid_tide": len(w),
        "n_days": n_days,
        "doy_first": min(days),
        "doy_last": max(days),
        "duration_days": duration_days,
        "az_mean_deg": float(np.mean(az)),
        "az_std_deg": az_std,
        "RH_mean_m": float(np.mean(wl)),
        "RH_sd_m": float(np.std(wl)),
        "PkNoise_mean": pkn_mean,
        "Amp_mean": amp_mean,
        "tide_r": tide_r,
        "tide_slope": slope,
        "tide_intercept_m": intercept,
        "C_unit_slope_m": C,
        "raw_RMS_cm": raw_rms_cm,
        "raw_MAE_cm": raw_mae_cm,
        "unit_slope_RMS_cm": unit_rms_cm,
        "unit_slope_MAE_cm": unit_mae_cm,
        "free_fit_RMS_cm": free_rms_cm,
        "plus0242_RMS_cm": plus0242_rms_cm,
        "plus0242_MAE_cm": plus0242_mae_cm,
        "plus0242_improvement_cm": (
            raw_rms_cm - plus0242_rms_cm
            if (
                math.isfinite(raw_rms_cm)
                and math.isfinite(
                    plus0242_rms_cm
                )
            )
            else math.nan
        ),
        "PASS_N": pass_n,
        "PASS_DAYS": pass_days,
        "PASS_R": pass_r,
        "PASS_SLOPE": pass_slope,
        "PASS_RMS": pass_rms,
        "PASS_AZ": pass_az,
        "good_track": good,
        "failure_count": len(failures),
        "failure_reasons": (
            "PASS"
            if good
            else ",".join(failures)
        ),
        "longterm_score": score,
    }


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def write_csv(records):
    if not records:
        return

    fields = list(records[0].keys())

    with OUT_CSV.open(
        "w",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )
        writer.writeheader()
        writer.writerows(records)


def print_diagnostics(records):
    banner("TRACK-BY-TRACK GOOD-DATA DIAGNOSTICS")

    print(
        "FAILURE CODES: "
        "N=observations, DAYS=unique days, R=tide correlation, "
        "SLOPE=slope, RMS=unit-slope RMS, AZ=azimuth SD"
    )
    print()

    header = (
        "ID  SAT R  N DAYS  AZmean  AZsd   "
        "r       slope   RMScm  FAIL"
    )
    print(header)
    print("-" * len(header))

    for rec in records:
        print(
            f"{rec['track_id']:2d} "
            f"{rec['sat']:3d} "
            f"{rec['rise']:2d} "
            f"{rec['n']:3d} "
            f"{rec['n_days']:4d} "
            f"{rec['az_mean_deg']:7.2f} "
            f"{rec['az_std_deg']:5.2f} "
            f"{rec['tide_r']:7.4f} "
            f"{rec['tide_slope']:7.4f} "
            f"{rec['unit_slope_RMS_cm']:7.2f} "
            f"{rec['failure_reasons']}"
        )

    print()

    counts = Counter()

    for rec in records:
        if rec["good_track"]:
            counts["PASS"] += 1
        else:
            for reason in rec["failure_reasons"].split(","):
                counts[reason] += 1

    banner("FAILURE SUMMARY")

    for key in [
        "N",
        "DAYS",
        "R",
        "SLOPE",
        "RMS",
        "AZ",
        "PASS",
    ]:
        print(
            f"{key:>6}: {counts.get(key, 0):3d}"
        )


def write_summary(records, rows):
    good = [
        r for r in records
        if r["good_track"]
    ]

    ranked = sorted(
        records,
        key=lambda r: r["longterm_score"],
        reverse=True,
    )

    lines = [
        "MARCONI LONG-TERM GNSS-R / TIDE TRACK DIAGNOSTICS V5",
        "=" * 110,
        f"Primary tide model: {PRIMARY_TIDE_MODEL}",
        f"Station H_ortho: {H_ORTHO_M:.3f} m",
        f"Azimuth cluster tolerance: {AZ_CLUSTER_TOL_DEG:.1f} deg",
        "",
        "GOOD TRACK CRITERIA",
        "-" * 110,
        f"Minimum observations: {GOOD_MIN_N}",
        f"Minimum unique days: {GOOD_MIN_DAYS}",
        f"Minimum tide correlation r: {GOOD_MIN_R:.2f}",
        f"Slope range: {GOOD_MIN_SLOPE:.2f} to {GOOD_MAX_SLOPE:.2f}",
        f"Maximum unit-slope RMS: {GOOD_MAX_UNIT_RMS_CM:.1f} cm",
        f"Maximum azimuth SD: {GOOD_MAX_AZ_STD_DEG:.2f} deg",
        "",
        "IMPORTANT",
        "-" * 110,
        "The +0.242 m datum offset is diagnostic only.",
        "It is NOT used to select GOOD tracks.",
        "Failure reasons are reported independently for every criterion.",
        "",
        f"GPS L1 observations matched to tide: {len(rows)}",
        f"Clustered tracks: {len(records)}",
        f"GOOD tracks: {len(good)}",
        "",
        "GOOD TRACKS",
        "-" * 110,
    ]

    if good:
        for i, rec in enumerate(good, 1):
            lines.append(
                f"{i:2d}. SAT={rec['sat']:3d} "
                f"rise={rec['rise']:2d} "
                f"N={rec['n']:3d} "
                f"days={rec['n_days']:2d} "
                f"Az={rec['az_mean_deg']:7.2f}±{rec['az_std_deg']:.2f} "
                f"r={rec['tide_r']:+.4f} "
                f"slope={rec['tide_slope']:+.4f} "
                f"unitRMS={rec['unit_slope_RMS_cm']:.2f}cm"
            )
    else:
        lines.append(
            "No tracks met all GOOD-track criteria."
        )

    lines += [
        "",
        "ALL TRACKS — RANKED BY LONG-TERM SCORE",
        "-" * 110,
    ]

    for i, rec in enumerate(
        ranked[:50],
        1,
    ):
        lines.append(
            f"{i:2d}. "
            f"SAT={rec['sat']:3d} "
            f"rise={rec['rise']:2d} "
            f"N={rec['n']:3d} "
            f"days={rec['n_days']:2d} "
            f"Az={rec['az_mean_deg']:7.2f}±{rec['az_std_deg']:.2f} "
            f"r={rec['tide_r']:+.4f} "
            f"slope={rec['tide_slope']:+.4f} "
            f"unitRMS={rec['unit_slope_RMS_cm']:.2f}cm "
            f"FAIL={rec['failure_reasons']} "
            f"score={rec['longterm_score']:.3f}"
        )

    OUT_SUMMARY.write_text(
        "\n".join(lines) + "\n"
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Marconi long-term GNSS-R track diagnostics V5"
    )

    parser.add_argument(
        "--rerun-missing",
        action="store_true",
        help="Run gnssir for discovered RINEX days without result files.",
    )

    args = parser.parse_args()

    banner("MARCONI LONG-TERM GNSS-R / TIDE PIPELINE V5")

    print("Base directory:", BASE_DIR)
    print("REFL_CODE:", REFL_CODE)
    print("Result directory:", RESULT_DIR)
    print("Primary tide:", PRIMARY_TIDE_MODEL)
    print("GNSSIR config:", GNSSIR_EXTENSION)
    print("Diagnostic offset: +0.242 m")

    print()
    print("GOOD-track criteria:")
    print("  observations >=", GOOD_MIN_N)
    print("  unique days   >=", GOOD_MIN_DAYS)
    print("  tide r        >=", GOOD_MIN_R)
    print(
        "  slope         =",
        GOOD_MIN_SLOPE,
        "to",
        GOOD_MAX_SLOPE,
    )
    print(
        "  unit RMS      <=",
        GOOD_MAX_UNIT_RMS_CM,
        "cm",
    )
    print(
        "  azimuth SD    <=",
        GOOD_MAX_AZ_STD_DEG,
        "deg",
    )

    rinex_days = discover_rinex()

    if args.rerun_missing:
        run_missing_gnssir(rinex_days)
    else:
        print()
        print(
            "GNSS-IR processing not requested. "
            "Use --rerun-missing to process only missing result files."
        )

    tide_at, tide_start, tide_end = load_tide_data()

    rows = load_results()

    if not rows:
        raise SystemExit(
            "No GPS L1 GNSS-R result observations were loaded."
        )

    rows = add_tides(
        rows,
        tide_at,
    )

    if not rows:
        raise SystemExit(
            "No GNSS-R observations fall inside the tide-model interval."
        )

    clustered = cluster_track_rows(rows)

    records = []

    for track_id, group in enumerate(
        clustered,
        start=1,
    ):
        records.append(
            analyze_track(
                group,
                track_id,
            )
        )

    # Ranked diagnostics.  GOOD tracks remain visible even if the score
    # is not the highest.
    records.sort(
        key=lambda r: (
            r["good_track"],
            r["longterm_score"],
        ),
        reverse=True,
    )

    print_diagnostics(records)

    write_csv(records)
    write_summary(records, rows)

    banner("OUTPUTS")

    print("CSV:", OUT_CSV)
    print("Summary:", OUT_SUMMARY)
    print()
    print("DONE")


if __name__ == "__main__":
    main()
