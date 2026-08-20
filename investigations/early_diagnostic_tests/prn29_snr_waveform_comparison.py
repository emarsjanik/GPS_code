#!/usr/bin/env python3
"""
PRN 29 / GPS L1 ocean-arc waveform comparison

Reproduces the installed gnssrefl extraction settings used in the
production analysis, then extracts the PRN 29 / frequency 1 rising
ocean-facing arc for DOY 204-207.

Outputs:
    prn29_snr_waveforms.csv
    prn29_snr_waveforms.png
    prn29_snr_spectral_comparison.csv
    prn29_snr_spectral_comparison.png

Run from the gnssrefl environment, with REFL_CODE set.
"""

from pathlib import Path
import csv
import math

import numpy as np
import matplotlib.pyplot as plt
from scipy.signal import find_peaks

from gnssrefl.extract_arcs import read_snr, extract_arcs
from gnssrefl.utils import FileManagement


# ---------------------------------------------------------------------
# USER SETTINGS
# ---------------------------------------------------------------------

STATION = "usgs"
YEAR = 2026
DOYS = range(204, 208)

SNR_TYPE = 66
FREQ = 1
SAT = 29

# Exact production settings established in the project.
E1 = 5.0
E2 = 15.0
AZLIST = [100.0, 130.0, 150.0, 215.0]
POLYV = 2
PELE = [5.0, 30.0]

# The ocean-facing PRN-29 rising arc is the one with
# az_min_ele approximately 113 degrees.
OCEAN_AZ_MIN = 100.0
OCEAN_AZ_MAX = 130.0

# Reference height used in the project.
REFERENCE_HEIGHT_M = 18.665

# GPS L1 wavelength/2.  This is also the scale factor returned by
# gnssrefl for GPS L1 in the current installation.
LAMBDA_L1_M = 0.190293672798365
CF_L1_M = LAMBDA_L1_M / 2.0

OUT_WAVE_CSV = Path("prn29_snr_waveforms.csv")
OUT_WAVE_PNG = Path("prn29_snr_waveforms.png")
OUT_SPEC_CSV = Path("prn29_snr_spectral_comparison.csv")
OUT_SPEC_PNG = Path("prn29_snr_spectral_comparison.png")


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def finite(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def datetime_from_doy_seconds(doy, seconds):
    from datetime import datetime, timedelta

    day = datetime(YEAR, 1, 1) + timedelta(days=doy - 1)
    return day + timedelta(seconds=float(seconds))


def extract_ocean_arc(doy):
    """
    Read the official SNR file and use the installed gnssrefl
    extract_arcs() implementation.

    Returns metadata and data for the PRN 29/F1 rising ocean arc.
    """

    obsfile, exists = FileManagement(
        STATION,
        "snr_file",
        YEAR,
        doy,
        snr_type=SNR_TYPE,
    ).find_snr_file(gzip=True)

    if not exists:
        raise RuntimeError(f"SNR file not found: {obsfile}")

    print()
    print("=" * 80)
    print(f"DOY {doy}")
    print("=" * 80)
    print("SNR:", obsfile)

    all_good, snr_array, _, _ = read_snr(
        obsfile,
        buffer_hours=2,
        screenstats=False,
    )

    if not all_good:
        raise RuntimeError(f"read_snr() failed for DOY {doy}")

    print("SNR shape:", snr_array.shape)

    arcs = extract_arcs(
        snr_array,
        freq=FREQ,
        e1=E1,
        e2=E2,
        azlist=AZLIST,
        polyV=POLYV,
        pele=PELE,
        detrend=True,
        split_arcs=True,
        filter_to_day=True,
        year=YEAR,
        doy=doy,
        dec=1,
    )

    candidates = []

    for meta, data in arcs:
        if meta.get("sat") != SAT:
            continue
        if meta.get("freq") != FREQ:
            continue
        if str(meta.get("arc_type")).lower() != "rising":
            continue

        az_low = finite(meta.get("az_min_ele"))
        if az_low is None:
            continue

        if OCEAN_AZ_MIN <= az_low <= OCEAN_AZ_MAX:
            candidates.append((meta, data))

    if not candidates:
        raise RuntimeError(
            f"No PRN {SAT}/freq {FREQ} rising ocean arc found on DOY {doy}"
        )

    if len(candidates) > 1:
        print("WARNING: multiple candidate ocean arcs found:")
        for meta, _ in candidates:
            print(
                f"  arc={meta.get('arc_num')} "
                f"az_min_ele={meta.get('az_min_ele')} "
                f"start={meta.get('time_start')} "
                f"end={meta.get('time_end')}"
            )

    # Choose the candidate whose low-elevation azimuth is closest to 113 deg.
    meta, data = min(
        candidates,
        key=lambda pair: abs(float(pair[0]["az_min_ele"]) - 113.0),
    )

    print("Selected arc:")
    print("  arc_num     :", meta.get("arc_num"))
    print("  arc_type    :", meta.get("arc_type"))
    print("  az_min_ele  :", meta.get("az_min_ele"))
    print("  az_avg      :", meta.get("az_avg"))
    print("  time_start  :", meta.get("time_start"))
    print("  time_end    :", meta.get("time_end"))
    print("  delT_min    :", meta.get("delT"))
    print("  num_pts     :", meta.get("num_pts"))
    print("  cf           :", meta.get("cf"))

    return meta, data


def interpolate_waveform(meta, data, ngrid=800):
    """
    Convert the detrended SNR samples to a common coordinate:
    sin(elevation).

    The four arcs have nearly identical geometry, so this gives a
    useful direct comparison of the interference waveform.

    Returns:
        x_grid          uniform sin(elevation) coordinate
        snr_grid        interpolated detrended SNR
        snr_norm_grid   zero-mean normalized waveform
    """

    ele = np.asarray(data["ele"], dtype=float)
    snr = np.asarray(data["snr"], dtype=float)

    good = (
        np.isfinite(ele)
        & np.isfinite(snr)
        & (ele >= E1)
        & (ele <= E2)
    )

    ele = ele[good]
    snr = snr[good]

    x = np.sin(np.deg2rad(ele))

    order = np.argsort(x)
    x = x[order]
    snr = snr[order]

    x_unique, unique_idx = np.unique(x, return_index=True)
    snr_unique = snr[unique_idx]

    if len(x_unique) < 20:
        raise RuntimeError("Too few unique elevation samples.")

    x_grid = np.linspace(
        x_unique[0],
        x_unique[-1],
        ngrid,
    )

    snr_grid = np.interp(
        x_grid,
        x_unique,
        snr_unique,
    )

    snr_norm = snr_grid - np.mean(snr_grid)

    std = np.std(snr_norm)
    if std > 0:
        snr_norm /= std

    return x_grid, snr_grid, snr_norm


def spectral_analysis(x_grid, snr_norm):
    """
    Estimate the dominant oscillation frequency in cycles per
    sin(elevation).

    For GNSS-IR:
        RH ~= frequency * lambda/2

    This is intentionally reported as an exploratory waveform
    diagnostic, not as a replacement for gnssrefl's official RH.
    """

    y = np.asarray(snr_norm, dtype=float)
    x = np.asarray(x_grid, dtype=float)

    dx = np.median(np.diff(x))
    if not np.isfinite(dx) or dx <= 0:
        raise RuntimeError("Invalid sin(elevation) grid.")

    n = len(y)

    window = np.hanning(n)
    yw = y * window

    spectrum = np.fft.rfft(yw)
    freq = np.fft.rfftfreq(n, d=dx)
    amp = np.abs(spectrum)

    # Ignore the zero-frequency component and very low frequencies.
    valid = (
        np.isfinite(freq)
        & np.isfinite(amp)
        & (freq > 1.0)
    )

    if not np.any(valid):
        return np.nan, np.nan, np.nan

    f_valid = freq[valid]
    a_valid = amp[valid]

    peaks, _ = find_peaks(a_valid)

    if len(peaks):
        peak_order = np.argsort(a_valid[peaks])[::-1]
        best_local = peaks[peak_order[0]]
    else:
        best_local = int(np.argmax(a_valid))

    dominant_freq = f_valid[best_local]
    dominant_amp = a_valid[best_local]

    # GNSS-IR RH relation.
    estimated_rh = dominant_freq * CF_L1_M

    return dominant_freq, estimated_rh, dominant_amp


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main():

    print()
    print("=" * 80)
    print("PRN 29 / GPS L1 OCEAN WAVEFORM COMPARISON")
    print("=" * 80)
    print()
    print("Exact production extraction settings:")
    print(f"  e1={E1}, e2={E2}")
    print(f"  azlist={AZLIST}")
    print(f"  polyV={POLYV}")
    print(f"  pele={PELE}")
    print(f"  frequency={FREQ}")
    print(f"  satellite={SAT}")
    print()

    records = []
    waveforms = []

    for doy in DOYS:

        meta, data = extract_ocean_arc(doy)

        x_grid, snr_grid, snr_norm = interpolate_waveform(
            meta,
            data,
        )

        dominant_freq, spectral_rh, dominant_amp = (
            spectral_analysis(
                x_grid,
                snr_norm,
            )
        )

        solution_seconds = (
            float(meta["time_start"])
            + 0.5 * float(meta["delT"]) * 60.0
        )

        # Use the midpoint only for a convenient time label.
        solution_dt = datetime_from_doy_seconds(
            doy,
            solution_seconds,
        )

        record = {
            "doy": doy,
            "date": solution_dt.strftime("%Y-%m-%d"),
            "solution_time_utc": solution_dt.strftime("%H:%M:%S"),
            "sat": SAT,
            "freq": FREQ,
            "arc_num": meta.get("arc_num"),
            "arc_type": meta.get("arc_type"),
            "az_min_ele": meta.get("az_min_ele"),
            "az_avg": meta.get("az_avg"),
            "time_start_sec": meta.get("time_start"),
            "time_end_sec": meta.get("time_end"),
            "duration_min": meta.get("delT"),
            "num_pts": meta.get("num_pts"),
            "cf_m": meta.get("cf"),
            "dominant_cycles_per_sin_e": dominant_freq,
            "spectral_rh_m": spectral_rh,
            "spectral_peak_amplitude": dominant_amp,
        }

        records.append(record)

        waveforms.append(
            {
                "doy": doy,
                "date": record["date"],
                "x": x_grid,
                "snr": snr_grid,
                "snr_norm": snr_norm,
            }
        )

        print()
        print(
            f"DOY {doy}: "
            f"dominant spectral frequency = "
            f"{dominant_freq:.3f} cycles/sin(e)"
        )
        print(
            f"DOY {doy}: "
            f"exploratory spectral RH = "
            f"{spectral_rh:.3f} m"
        )

    # -----------------------------------------------------------------
    # CSV 1: waveform samples
    # -----------------------------------------------------------------

    with open(OUT_WAVE_CSV, "w", newline="") as f:

        writer = csv.writer(f)

        writer.writerow(
            [
                "doy",
                "date",
                "sin_elevation",
                "elevation_deg",
                "detrended_snr",
                "normalized_detrended_snr",
            ]
        )

        for wf in waveforms:

            for x, snr, snr_norm in zip(
                wf["x"],
                wf["snr"],
                wf["snr_norm"],
            ):

                elevation_deg = math.degrees(
                    math.asin(
                        max(-1.0, min(1.0, float(x)))
                    )
                )

                writer.writerow(
                    [
                        wf["doy"],
                        wf["date"],
                        f"{x:.10f}",
                        f"{elevation_deg:.6f}",
                        f"{snr:.8f}",
                        f"{snr_norm:.8f}",
                    ]
                )

    # -----------------------------------------------------------------
    # CSV 2: spectral comparison
    # -----------------------------------------------------------------

    with open(OUT_SPEC_CSV, "w", newline="") as f:

        fieldnames = list(records[0].keys())
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(records)

    # -----------------------------------------------------------------
    # Plot 1: actual detrended SNR waveform
    # -----------------------------------------------------------------

    plt.figure(figsize=(11, 7))

    for wf in waveforms:

        elevation_deg = np.degrees(
            np.arcsin(
                np.clip(wf["x"], -1, 1)
            )
        )

        plt.plot(
            elevation_deg,
            wf["snr_norm"],
            label=f"DOY {wf['doy']} ({wf['date']})",
        )

    plt.xlabel("Elevation angle (degrees)")
    plt.ylabel("Normalized detrended SNR")
    plt.title(
        "PRN 29 / GPS L1 Ocean Arc — "
        "Detrended SNR vs Elevation"
    )
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(
        OUT_WAVE_PNG,
        dpi=200,
    )
    plt.close()

    # -----------------------------------------------------------------
    # Plot 2: spectral RH comparison
    # -----------------------------------------------------------------

    doys = [r["doy"] for r in records]
    spectral_rh = [
        r["spectral_rh_m"]
        for r in records
    ]

    plt.figure(figsize=(10, 6))

    plt.plot(
        doys,
        spectral_rh,
        marker="o",
    )

    plt.xlabel("Day of year")
    plt.ylabel("Exploratory spectral RH (m)")
    plt.title(
        "PRN 29 / GPS L1 — Dominant SNR Spectral Peak"
    )
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(
        OUT_SPEC_PNG,
        dpi=200,
    )
    plt.close()

    # -----------------------------------------------------------------
    # Final summary
    # -----------------------------------------------------------------

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    for r in records:

        print(
            f"DOY {r['doy']} "
            f"AzLow={float(r['az_min_ele']):7.3f} "
            f"AzAvg={float(r['az_avg']):7.3f} "
            f"N={int(r['num_pts']):4d} "
            f"duration={float(r['duration_min']):5.1f} min "
            f"spectral_RH={float(r['spectral_rh_m']):7.3f} m"
        )

    print()
    print("Files written:")
    print(" ", OUT_WAVE_CSV)
    print(" ", OUT_WAVE_PNG)
    print(" ", OUT_SPEC_CSV)
    print(" ", OUT_SPEC_PNG)
    print()
    print(
        "IMPORTANT: spectral_RH is an exploratory diagnostic. "
        "The official gnssrefl RH solution remains the production result."
    )
    print("=" * 80)


if __name__ == "__main__":
    main()
