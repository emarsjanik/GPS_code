#!/usr/bin/env python3
"""
marconi_candidate_fresnel_validation.py

Controlled geometry validation for the strongest repeated GPS L1 tracks
from the current Marconi 17-23 m GNSS-IR experiment.

Targets (initially):
    PRN 26 ~90.9 deg
    PRN 21 ~92.9 deg
    PRN 16 ~96.4 deg

The script:
  1. Reads the actual saved gnssrefl result rows from the isolated
     ocean17_23_l1_e5_13 strategy.
  2. Selects repeated observations for the target tracks.
  3. Uses each arc's ACTUAL recovered RH and azimuth.
  4. Builds Fresnel ellipses with gnssrefl.makeEllipse_latlon().
  5. Builds diagnostic 5°, 10°, and 13° footprints for each arc.
  6. Quantifies footprint location relative to the current confirmed
     station-to-water reference line:
         bearing = 83.06 degrees
         distance = 71.78 m
     using a local shoreline half-plane.
  7. Writes a KML with every candidate arc and reference lines.
  8. Writes a CSV with geometry diagnostics.

IMPORTANT:
  This is a DIAGNOSTIC geometry validation, not a production ocean mask.
  The current repository does not contain a digitized shoreline polygon
  in the sources used here, so the water/land fraction is based on the
  repository's confirmed bearing/distance half-plane, not a coastline
  polygon. The KML is therefore the key artifact for visual validation.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import numpy as np
import simplekml

from gnssrefl.refl_zones import makeEllipse_latlon


# ---------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------

RESULT_DIR = Path(
    "products/refl_code/2026/results/usgs/ocean17_23_l1_e5_13"
)

OUT_CSV = Path(
    "marconi_candidate_fresnel_validation.csv"
)

OUT_KML = Path(
    "marconi_candidate_fresnel_validation.kml"
)

OUT_SUMMARY = Path(
    "marconi_candidate_fresnel_validation_summary.txt"
)

LAT = 41.8928243333
LON = -69.9633227139

FREQUENCY = 1

# Strongest scientifically interesting repeated tracks from the
# four-day tide comparison.
TARGET_TRACKS = {
    (26, 1): "PRN26_AZ91",
    (21, 1): "PRN21_AZ93",
    (16, 1): "PRN16_AZ96",
}

DOYS = [204, 205, 206, 207]

# Use actual observation-range endpoints and a representative middle
# elevation. The production experiment used 5-13 degrees.
TEST_ELEVATIONS = [5.0, 9.0, 13.0]

# Repository-confirmed diagnostic water reference:
WATER_BEARING_DEG = 83.06
WATER_DISTANCE_M = 71.78

# "Water side" of the diagnostic shoreline half-plane.
OPEN_WATER_FRACTION = 0.80
LAND_FRACTION = 0.20


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def utc_hours_to_datetime(year: int, doy: int, utc_hours: float):
    day = (
        datetime(year, 1, 1)
        + timedelta(days=doy - 1)
    )

    return day + timedelta(
        hours=float(utc_hours)
    )


def local_xy_from_lonlat(lon, lat):
    """
    Local EN approximation around the Marconi station.
    """
    R = 6371000.0

    east = (
        math.radians(lon - LON)
        * R
        * math.cos(math.radians(LAT))
    )

    north = (
        math.radians(lat - LAT)
        * R
    )

    return east, north


def shoreline_signed_distance(east_m, north_m):
    """
    Positive = water side of the confirmed reference shoreline line.
    """
    b = math.radians(WATER_BEARING_DEG)

    along = (
        east_m * math.sin(b)
        + north_m * math.cos(b)
    )

    return along - WATER_DISTANCE_M


def polygon_area(points):
    """
    Polygon points in local EN meters.
    """
    if len(points) < 3:
        return 0.0

    total = 0.0

    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]

        total += (
            x1 * y2
            - x2 * y1
        )

    return abs(total) * 0.5


def clip_polygon_water_side(points):
    """
    Sutherland-Hodgman clipping to:
        signed_distance >= 0
    """
    if not points:
        return []

    result = []

    def val(p):
        return shoreline_signed_distance(
            p[0],
            p[1],
        )

    for i, current in enumerate(points):

        previous = points[i - 1]

        vc = val(current)
        vp = val(previous)

        inside_c = vc >= 0
        inside_p = vp >= 0

        if inside_p and inside_c:
            result.append(current)

        elif inside_p and not inside_c:

            den = vp - vc

            if abs(den) > 1e-12:

                t = vp / den

                result.append(
                    (
                        previous[0]
                        + t * (
                            current[0]
                            - previous[0]
                        ),
                        previous[1]
                        + t * (
                            current[1]
                            - previous[1]
                        ),
                    )
                )

        elif not inside_p and inside_c:

            den = vp - vc

            if abs(den) > 1e-12:

                t = vp / den

                result.append(
                    (
                        previous[0]
                        + t * (
                            current[0]
                            - previous[0]
                        ),
                        previous[1]
                        + t * (
                            current[1]
                            - previous[1]
                        ),
                    )
                )

            result.append(current)

    return result


def polygon_geometry_metrics(points):
    """
    Convert lon/lat polygon to local EN, then quantify its position
    relative to the reference water half-plane.
    """
    en = [
        local_xy_from_lonlat(
            lon,
            lat,
        )
        for lon, lat in points
    ]

    area = polygon_area(en)

    if area <= 0:
        return {
            "area_m2": math.nan,
            "water_fraction": math.nan,
            "center_signed_m": math.nan,
            "min_signed_m": math.nan,
            "max_signed_m": math.nan,
            "center_e_m": math.nan,
            "center_n_m": math.nan,
        }

    water_poly = clip_polygon_water_side(
        en
    )

    water_area = polygon_area(
        water_poly
    )

    center_e = float(
        np.mean(
            [p[0] for p in en]
        )
    )

    center_n = float(
        np.mean(
            [p[1] for p in en]
        )
    )

    signed = np.asarray(
        [
            shoreline_signed_distance(
                p[0],
                p[1],
            )
            for p in en
        ],
        dtype=float,
    )

    return {
        "area_m2": area,
        "water_fraction": (
            water_area / area
        ),
        "center_signed_m": (
            shoreline_signed_distance(
                center_e,
                center_n,
            )
        ),
        "min_signed_m": float(
            np.min(signed)
        ),
        "max_signed_m": float(
            np.max(signed)
        ),
        "center_e_m": center_e,
        "center_n_m": center_n,
    }


def classify_fraction(frac):
    if not math.isfinite(frac):
        return "INVALID"

    if frac >= OPEN_WATER_FRACTION:
        return "OPEN_WATER_CANDIDATE"

    if frac <= LAND_FRACTION:
        return "LAND_LIKELY"

    return "MIXED_LAND_WATER"


def build_fresnel_polygon(freq, elevation, rh, azimuth):
    """
    Use the exact geometry implementation from the installed gnssrefl
    package and the same makeEllipse_latlon() interface already used by
    the current repository's geometry scripts.
    """
    lng, lat = makeEllipse_latlon(
        int(freq),
        float(elevation),
        float(rh),
        float(azimuth),
        LAT,
        LON,
    )

    return [
        (float(x), float(y))
        for x, y in zip(lng, lat)
    ]


def load_rows():
    rows = []

    for doy in DOYS:

        path = (
            RESULT_DIR
            / f"{doy}.txt"
        )

        if not path.exists():
            raise RuntimeError(
                f"Missing result file: {path}"
            )

        for line in path.read_text(
            errors="replace"
        ).splitlines():

            line = line.strip()

            if (
                not line
                or line.startswith("%")
            ):
                continue

            cols = line.split()

            if len(cols) < 17:
                continue

            try:
                year = int(
                    float(cols[0])
                )
                doy2 = int(
                    float(cols[1])
                )
                rh = float(
                    cols[2]
                )
                sat = int(
                    float(cols[3])
                )
                utc_hours = float(
                    cols[4]
                )
                az = float(
                    cols[5]
                )
                amp = float(
                    cols[6]
                )
                emin = float(
                    cols[7]
                )
                emax = float(
                    cols[8]
                )
                nobs = int(
                    float(cols[9])
                )
                freq = int(
                    float(cols[10])
                )
                rise = int(
                    float(cols[11])
                )
                pkn = float(
                    cols[13]
                )
                delt = float(
                    cols[14]
                )

            except (
                ValueError,
                TypeError,
            ):
                continue

            if (
                sat,
                freq,
            ) not in TARGET_TRACKS:
                continue

            dt = utc_hours_to_datetime(
                year,
                doy2,
                utc_hours,
            )

            rows.append(
                {
                    "year": year,
                    "doy": doy2,
                    "dt": dt,
                    "sat": sat,
                    "freq": freq,
                    "rh": rh,
                    "az": az,
                    "amp": amp,
                    "pkn": pkn,
                    "emin": emin,
                    "emax": emax,
                    "nobs": nobs,
                    "rise": rise,
                    "delt": delt,
                }
            )

    rows.sort(
        key=lambda r: (
            r["sat"],
            r["freq"],
            r["dt"],
        )
    )

    return rows


# ---------------------------------------------------------------------
# KML STYLES
# ---------------------------------------------------------------------

def build_styles(kml):
    colors = {
        "PRN26_AZ91": simplekml.Color.red,
        "PRN21_AZ93": simplekml.Color.yellow,
        "PRN16_AZ96": simplekml.Color.green,
    }

    styles = {}

    for label, color in colors.items():

        line = simplekml.Style()
        line.linestyle.color = color
        line.linestyle.width = 3
        line.polystyle.color = (
            simplekml.Color.changealphaint(
                45,
                color,
            )
        )

        styles[label] = line

    # Reference line styles
    ref = simplekml.Style()
    ref.linestyle.color = simplekml.Color.white
    ref.linestyle.width = 5
    styles["reference"] = ref

    return styles


def add_reference_geometry(kml, styles):
    station = kml.newpoint(
        name="USGS Marconi GNSS station"
    )

    station.coords = [
        (LON, LAT)
    ]

    station.style.iconstyle.icon.href = (
        "http://maps.google.com/"
        "mapfiles/kml/shapes/placemark_circle.png"
    )

    # True bearing reference line.
    R = 6371000.0
    b = math.radians(
        WATER_BEARING_DEG
    )

    dlat = (
        WATER_DISTANCE_M
        * math.cos(b)
        / R
    )

    dlon = (
        WATER_DISTANCE_M
        * math.sin(b)
        / (
            R
            * math.cos(
                math.radians(LAT)
            )
        )
    )

    end_lat = (
        LAT
        + math.degrees(dlat)
    )

    end_lon = (
        LON
        + math.degrees(dlon)
    )

    line = kml.newlinestring(
        name=(
            "Confirmed station-to-water "
            f"bearing {WATER_BEARING_DEG:.2f}° "
            f"distance {WATER_DISTANCE_M:.2f} m"
        )
    )

    line.coords = [
        (LON, LAT),
        (end_lon, end_lat),
    ]

    line.style = styles["reference"]


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main():

    print()
    print("=" * 90)
    print(
        "MARCONI CANDIDATE FRESNEL / SHORELINE VALIDATION"
    )
    print("=" * 90)

    print(
        f"Result directory: {RESULT_DIR}"
    )

    print(
        "Target tracks:"
    )

    for key, label in TARGET_TRACKS.items():
        print(
            f"  SAT={key[0]} FREQ={key[1]} -> {label}"
        )

    print()
    print(
        "Reference water bearing:",
        f"{WATER_BEARING_DEG:.2f}°"
    )

    print(
        "Reference shoreline distance:",
        f"{WATER_DISTANCE_M:.2f} m"
    )

    print()
    print(
        "Test elevations:",
        TEST_ELEVATIONS
    )

    rows = load_rows()

    if not rows:
        raise SystemExit(
            "No target-track observations found."
        )

    print()
    print(
        f"Target observations found: {len(rows)}"
    )

    # Prepare KML.
    kml = simplekml.Kml()
    styles = build_styles(kml)

    add_reference_geometry(
        kml,
        styles,
    )

    csv_rows = []

    # Build per-track folders.
    folders = {}

    for (
        sat,
        freq
    ), label in TARGET_TRACKS.items():

        folders[
            (sat, freq)
        ] = kml.newfolder(
            name=(
                f"{label} "
                f"(SAT {sat} F{freq})"
            )
        )

    # -----------------------------------------------------------------
    # Each actual arc
    # -----------------------------------------------------------------

    for r in rows:

        label = TARGET_TRACKS[
            (r["sat"], r["freq"])
        ]

        folder = folders[
            (r["sat"], r["freq"])
        ]

        print()
        print(
            f"SAT={r['sat']} "
            f"Az={r['az']:.2f} "
            f"RH={r['rh']:.3f} "
            f"UTC={r['dt']}"
        )

        # One folder per observation.
        obs_folder = folder.newfolder(
            name=(
                f"{r['dt'].strftime('%Y-%m-%d %H:%M:%S')} "
                f"RH={r['rh']:.3f}m "
                f"Az={r['az']:.2f}°"
            )
        )

        # Placemark at station.
        point = obs_folder.newpoint(
            name="Station"
        )
        point.coords = [
            (LON, LAT)
        ]

        for elevation in TEST_ELEVATIONS:

            poly = build_fresnel_polygon(
                r["freq"],
                elevation,
                r["rh"],
                r["az"],
            )

            metrics = polygon_geometry_metrics(
                poly
            )

            classification = classify_fraction(
                metrics[
                    "water_fraction"
                ]
            )

            # KML
            p = obs_folder.newpolygon(
                name=(
                    f"EL={elevation:.0f}° "
                    f"water={metrics['water_fraction']:.3f} "
                    f"{classification}"
                )
            )

            p.outerboundaryis = poly
            p.style = styles[label]

            csv_rows.append(
                {
                    "datetime_utc":
                        r["dt"].isoformat(),

                    "doy":
                        r["doy"],

                    "sat":
                        r["sat"],

                    "freq":
                        r["freq"],

                    "track":
                        label,

                    "az_deg":
                        r["az"],

                    "RH_m":
                        r["rh"],

                    "PkNoise":
                        r["pkn"],

                    "Amp":
                        r["amp"],

                    "eminO_deg":
                        r["emin"],

                    "emaxO_deg":
                        r["emax"],

                    "rise":
                        r["rise"],

                    "DelT_min":
                        r["delt"],

                    "test_elevation_deg":
                        elevation,

                    "footprint_area_m2":
                        metrics[
                            "area_m2"
                        ],

                    "water_fraction":
                        metrics[
                            "water_fraction"
                        ],

                    "classification":
                        classification,

                    "center_signed_m":
                        metrics[
                            "center_signed_m"
                        ],

                    "min_signed_m":
                        metrics[
                            "min_signed_m"
                        ],

                    "max_signed_m":
                        metrics[
                            "max_signed_m"
                        ],
                }
            )

    # -----------------------------------------------------------------
    # Write CSV
    # -----------------------------------------------------------------

    fields = list(
        csv_rows[0].keys()
    )

    with open(
        OUT_CSV,
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        writer.writeheader()
        writer.writerows(
            csv_rows
        )

    # -----------------------------------------------------------------
    # Track summary
    # -----------------------------------------------------------------

    summary_lines = []

    summary_lines.append(
        "MARCONI CANDIDATE FRESNEL / SHORELINE VALIDATION"
    )
    summary_lines.append(
        "=" * 90
    )
    summary_lines.append(
        ""
    )

    summary_lines.append(
        "IMPORTANT: water fraction is computed against the"
    )
    summary_lines.append(
        "repository-confirmed 83.06° / 71.78 m shoreline"
    )
    summary_lines.append(
        "half-plane, not a digitized shoreline polygon."
    )
    summary_lines.append(
        ""
    )

    summary_lines.append(
        "TRACK SUMMARY"
    )
    summary_lines.append(
        "-" * 90
    )

    for key, label in TARGET_TRACKS.items():

        track = [
            row for row in csv_rows
            if (
                row["sat"] == key[0]
                and row["freq"] == key[1]
            )
        ]

        for elevation in TEST_ELEVATIONS:

            subset = [
                row for row in track
                if row[
                    "test_elevation_deg"
                ] == elevation
            ]

            wf = np.array(
                [
                    row[
                        "water_fraction"
                    ]
                    for row in subset
                ],
                dtype=float,
            )

            signed = np.array(
                [
                    row[
                        "center_signed_m"
                    ]
                    for row in subset
                ],
                dtype=float,
            )

            summary_lines.append(
                f"{label} "
                f"EL={elevation:.0f}° "
                f"N={len(subset)} "
                f"water_fraction_mean="
                f"{np.mean(wf):.3f} "
                f"min={np.min(wf):.3f} "
                f"max={np.max(wf):.3f} "
                f"center_signed_mean="
                f"{np.mean(signed):+.2f} m"
            )

    # -----------------------------------------------------------------
    # Save
    # -----------------------------------------------------------------

    kml.save(
        str(OUT_KML)
    )

    OUT_SUMMARY.write_text(
        "\n".join(
            summary_lines
        )
        + "\n"
    )

    print()
    print("=" * 90)
    print("OUTPUTS")
    print("=" * 90)

    print(
        f"CSV     : {OUT_CSV.resolve()}"
    )

    print(
        f"KML     : {OUT_KML.resolve()}"
    )

    print(
        f"Summary : {OUT_SUMMARY.resolve()}"
    )

    print()
    print(
        "DONE"
    )


if __name__ == "__main__":
    main()
