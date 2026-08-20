#!/usr/bin/env python3
"""
classify_marconi_fresnel_water.py

Geometry-first diagnostic for the Marconi GNSS-IR station.

IMPORTANT:
This script does NOT claim to know the exact shoreline polygon.
Instead it uses the repository's confirmed reference to water:
    bearing = 83.06 deg
    distance = 71.78 m
as a local shoreline-reference line. The line is perpendicular to the
83.06 deg bearing at 71.78 m from the antenna.

For every individual GNSS-IR arc in gnssir_tide_arc_analysis.csv, the
script:
  * uses the actual arc RH, azimuth, and frequency;
  * generates first Fresnel-zone ellipses at 5, 10 and 15 degrees;
  * converts the ellipse to local EN coordinates;
  * clips each ellipse against the shoreline-reference half-plane;
  * estimates what fraction of the Fresnel-zone footprint lies on the
    "water side" of that reference line;
  * classifies each elevation as LAND / MIXED / OPEN_WATER;
  * assigns an overall arc geometry classification.

This is a conservative diagnostic, NOT a substitute for a digitized
shoreline/coastline polygon or bathymetry.

Outputs:
  marconi_fresnel_geometry_all_arcs.csv
  marconi_fresnel_geometry_kml.kml
  marconi_fresnel_geometry_summary.txt

Run:
  cd ~/GNSS/v4.1
  source ~/GNSS/v4.1/gnssrefl_venv/bin/activate
  python3 classify_marconi_fresnel_water.py
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from gnssrefl.refl_zones import makeEllipse_latlon, get_wavelength


# ---------------------------------------------------------------------
# INPUT / STATION / REFERENCE GEOMETRY
# ---------------------------------------------------------------------

CSV_FILE = Path("gnssir_tide_arc_analysis.csv")

LAT = 41.8928243333
LON = -69.9633227139

# Confirmed reference bearing and measured station-to-water distance
# documented in marconi_ocean_mask_corrected.py in the repository.
WATER_BEARING_DEG = 83.06
SHORELINE_DISTANCE_M = 71.78

# Elevations used to sample the footprint across the production arc.
TEST_ELEVATIONS = [5.0, 10.0, 15.0]

# Classification thresholds for the fraction of ellipse area lying
# on the water side of the reference shoreline line.
OPEN_WATER_THRESHOLD = 0.80
LAND_THRESHOLD = 0.20

# Output
OUT_CSV = Path("marconi_fresnel_geometry_all_arcs.csv")
OUT_KML = Path("marconi_fresnel_geometry_kml.kml")
OUT_SUMMARY = Path("marconi_fresnel_geometry_summary.txt")


# ---------------------------------------------------------------------
# SIMPLE LOCAL GEODESY
# ---------------------------------------------------------------------

EARTH_M = 6371000.0


def latlon_to_en(lat, lon):
    """
    Convert station-relative lat/lon to local east/north meters.
    Accurate enough over this ~100 m site scale.
    """
    lat0 = math.radians(LAT)
    dlat = math.radians(lat - LAT)
    dlon = math.radians(lon - LON)

    north = EARTH_M * dlat
    east = EARTH_M * math.cos(lat0) * dlon

    return east, north


def bearing_distance_point(bearing_deg, distance_m):
    """
    Point at a given bearing/distance from station.
    """
    b = math.radians(bearing_deg)

    north = distance_m * math.cos(b)
    east = distance_m * math.sin(b)

    return east, north


# ---------------------------------------------------------------------
# POLYGON / HALF-PLANE GEOMETRY
# ---------------------------------------------------------------------

def polygon_area(poly):
    """
    Signed polygon area in m^2 for (east,north) coordinates.
    """
    if len(poly) < 3:
        return 0.0

    s = 0.0

    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]

        s += x1 * y2 - x2 * y1

    return abs(s) * 0.5


def line_signed_water_coordinate(east, north):
    """
    Signed coordinate relative to the shoreline-reference line.

    Water side is positive:
        projection along the 83.06 deg bearing - 71.78 m

    This creates a boundary line perpendicular to the confirmed
    station-to-water bearing.
    """
    b = math.radians(WATER_BEARING_DEG)

    # Projection of point onto bearing-from-station axis.
    along = (
        east * math.sin(b)
        + north * math.cos(b)
    )

    return along - SHORELINE_DISTANCE_M


def clip_polygon_water_side(poly):
    """
    Clip polygon to signed-water-coordinate >= 0 using a
    Sutherland-Hodgman-style half-plane clip.
    """

    if not poly:
        return []

    output = []

    def signed(p):
        return line_signed_water_coordinate(
            p[0], p[1]
        )

    for i in range(len(poly)):
        current = poly[i]
        previous = poly[i - 1]

        sc = signed(current)
        sp = signed(previous)

        current_inside = sc >= 0.0
        previous_inside = sp >= 0.0

        if current_inside and previous_inside:
            output.append(current)

        elif previous_inside and not current_inside:
            # Leaving water side.
            denom = sp - sc
            if abs(denom) > 1e-12:
                t = sp / denom
                ix = (
                    previous[0]
                    + t * (current[0] - previous[0])
                )
                iy = (
                    previous[1]
                    + t * (current[1] - previous[1])
                )
                output.append((ix, iy))

        elif not previous_inside and current_inside:
            # Entering water side.
            denom = sp - sc
            if abs(denom) > 1e-12:
                t = sp / denom
                ix = (
                    previous[0]
                    + t * (current[0] - previous[0])
                )
                iy = (
                    previous[1]
                    + t * (current[1] - previous[1])
                )
                output.append((ix, iy))

            output.append(current)

    return output


def ellipse_latlon_to_en(freq, elevation, rh, azimuth):
    """
    Use the installed gnssrefl geometry implementation directly.
    """
    lng, lat = makeEllipse_latlon(
        int(freq),
        float(elevation),
        float(rh),
        float(azimuth),
        LAT,
        LON,
    )

    points = []

    for lo, la in zip(lng, lat):
        east, north = latlon_to_en(
            float(la),
            float(lo),
        )
        points.append((east, north))

    return points, list(zip(lng, lat))


def classify_ellipse(poly):
    """
    Return geometric metrics for the ellipse relative to the
    reference shoreline half-plane.
    """

    total_area = polygon_area(poly)

    if total_area <= 0:
        return {
            "area_m2": 0.0,
            "water_area_m2": 0.0,
            "water_fraction": 0.0,
            "min_signed_m": float("nan"),
            "max_signed_m": float("nan"),
            "center_signed_m": float("nan"),
            "classification": "INVALID",
        }

    water_poly = clip_polygon_water_side(poly)
    water_area = polygon_area(water_poly)

    signed = np.array(
        [
            line_signed_water_coordinate(x, y)
            for x, y in poly
        ],
        dtype=float,
    )

    # Polygon centroid by arithmetic mean of sampled perimeter points.
    center_x = float(
        np.mean([p[0] for p in poly])
    )
    center_y = float(
        np.mean([p[1] for p in poly])
    )

    center_signed = line_signed_water_coordinate(
        center_x,
        center_y,
    )

    frac = water_area / total_area

    if frac >= OPEN_WATER_THRESHOLD:
        classification = "OPEN_WATER_LIKELY"
    elif frac <= LAND_THRESHOLD:
        classification = "LAND_LIKELY"
    else:
        classification = "MIXED_LAND_WATER"

    return {
        "area_m2": total_area,
        "water_area_m2": water_area,
        "water_fraction": frac,
        "min_signed_m": float(np.min(signed)),
        "max_signed_m": float(np.max(signed)),
        "center_signed_m": center_signed,
        "classification": classification,
    }


# ---------------------------------------------------------------------
# INPUT
# ---------------------------------------------------------------------

def finite(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except Exception:
        return None


def load_records():
    rows = []

    with open(
        CSV_FILE,
        newline="",
    ) as f:

        reader = csv.DictReader(f)

        required = [
            "doy",
            "sat",
            "freq",
            "solution_time_utc",
            "RH_m",
            "Azim",
            "Amp",
            "PkNoise",
            "delT_min",
            "eminO",
            "emaxO",
        ]

        missing = [
            x for x in required
            if x not in reader.fieldnames
        ]

        if missing:
            raise RuntimeError(
                f"Missing columns: {missing}"
            )

        for row_number, r in enumerate(
            reader,
            start=2,
        ):

            try:
                doy = int(float(r["doy"]))
                sat = int(float(r["sat"]))
                freq = int(float(r["freq"]))

                rh = finite(r["RH_m"])
                az = finite(r["Azim"])
                amp = finite(r["Amp"])
                pkn = finite(r["PkNoise"])
                delt = finite(r["delT_min"])
                emin = finite(r["eminO"])
                emax = finite(r["emaxO"])

                if any(
                    x is None
                    for x in [
                        rh,
                        az,
                        amp,
                        pkn,
                        delt,
                        emin,
                        emax,
                    ]
                ):
                    continue

                rows.append(
                    {
                        "source_row": row_number,
                        "doy": doy,
                        "sat": sat,
                        "freq": freq,
                        "solution_time_utc":
                            r["solution_time_utc"],
                        "rh": rh,
                        "az": az,
                        "amp": amp,
                        "pkn": pkn,
                        "delt": delt,
                        "emin": emin,
                        "emax": emax,
                    }
                )

            except (
                ValueError,
                TypeError,
                KeyError,
            ):
                continue

    return rows


# ---------------------------------------------------------------------
# KML
# ---------------------------------------------------------------------

def write_kml(items):
    try:
        import simplekml
    except ImportError:
        print(
            "simplekml not installed; KML will not be written."
        )
        return False

    kml = simplekml.Kml()

    styles = {}

    style_defs = {
        "OPEN_WATER_LIKELY": (
            simplekml.Color.green,
            45,
        ),
        "MIXED_LAND_WATER": (
            simplekml.Color.yellow,
            45,
        ),
        "LAND_LIKELY": (
            simplekml.Color.red,
            35,
        ),
    }

    for label, (
        color,
        alpha,
    ) in style_defs.items():

        style = simplekml.Style()
        style.linestyle.color = color
        style.linestyle.width = 2
        style.polystyle.color = (
            simplekml.Color.changealphaint(
                alpha,
                color,
            )
        )

        styles[label] = style

    # Station.
    station = kml.newpoint(
        name="USGS Marconi GNSS station"
    )
    station.coords = [
        (LON, LAT)
    ]

    # Reference shoreline point.
    e, n = bearing_distance_point(
        WATER_BEARING_DEG,
        SHORELINE_DISTANCE_M,
    )

    # Convert local EN point back to lat/lon.
    ref_lat = LAT + math.degrees(
        n / EARTH_M
    )

    ref_lon = LON + math.degrees(
        e /
        (
            EARTH_M
            * math.cos(math.radians(LAT))
        )
    )

    ref = kml.newpoint(
        name=(
            "Reference shoreline point "
            f"{WATER_BEARING_DEG:.2f} deg / "
            f"{SHORELINE_DISTANCE_M:.2f} m"
        )
    )

    ref.coords = [
        (ref_lon, ref_lat)
    ]

    # Draw bearing reference.
    line = kml.newlinestring(
        name="Confirmed station-to-water reference"
    )

    line.coords = [
        (LON, LAT),
        (ref_lon, ref_lat),
    ]

    line.style.linestyle.color = (
        simplekml.Color.white
    )
    line.style.linestyle.width = 4

    for item in items:

        classification = item[
            "overall_classification"
        ]

        folder = kml.newfolder(
            name=(
                f"DOY {item['doy']} "
                f"SAT {item['sat']} "
                f"F{item['freq']}"
            )
        )

        for elev in TEST_ELEVATIONS:

            key = f"el_{elev:g}"

            coords = item[key]["coords"]

            p = folder.newpolygon(
                name=(
                    f"EL {elev:g} "
                    f"{item[key]['classification']} "
                    f"{item[key]['water_fraction']:.2f}"
                )
            )

            p.outerboundaryis = coords

            c = item[key]["classification"]

            if c in styles:
                p.style = styles[c]

    kml.save(str(OUT_KML))
    return True


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main():

    print()
    print("=" * 80)
    print("MARCONI FRESNEL / WATER-SIDE GEOMETRY CLASSIFICATION")
    print("=" * 80)

    print()
    print(f"Input CSV              : {CSV_FILE}")
    print(f"Station                : {LAT}, {LON}")
    print(
        f"Reference water bearing: "
        f"{WATER_BEARING_DEG:.2f} deg"
    )
    print(
        f"Reference shoreline distance: "
        f"{SHORELINE_DISTANCE_M:.2f} m"
    )
    print(
        f"Test elevations        : "
        f"{TEST_ELEVATIONS}"
    )

    if not CSV_FILE.exists():
        raise SystemExit(
            f"ERROR: {CSV_FILE} does not exist."
        )

    rows = load_records()

    print()
    print(
        f"Records loaded: {len(rows)}"
    )

    items = []

    summary = {
        "OPEN_WATER_CANDIDATE": 0,
        "MIXED_LAND_WATER": 0,
        "LAND_LIKELY": 0,
        "INVALID": 0,
    }

    for row in rows:

        item = dict(row)
        classifications = []

        freq = row["freq"]

        for elev in TEST_ELEVATIONS:

            try:
                poly_en, coords = (
                    ellipse_latlon_to_en(
                        freq,
                        elev,
                        row["rh"],
                        row["az"],
                    )
                )

                metrics = classify_ellipse(
                    poly_en
                )

                metrics["coords"] = [
                    (float(lo), float(la))
                    for lo, la in coords
                ]

            except Exception as exc:
                metrics = {
                    "area_m2": math.nan,
                    "water_area_m2": math.nan,
                    "water_fraction": math.nan,
                    "min_signed_m": math.nan,
                    "max_signed_m": math.nan,
                    "center_signed_m": math.nan,
                    "classification": "INVALID",
                    "coords": [],
                    "error": str(exc),
                }

            item[
                f"el_{elev:g}"
            ] = metrics

            classifications.append(
                metrics["classification"]
            )

        # Conservative overall classification.
        #
        # If ANY tested elevation is overwhelmingly on water, retain the
        # arc as an "open water candidate." If no elevation is open water
        # but at least one is mixed, classify the arc as mixed.
        if "OPEN_WATER_LIKELY" in classifications:
            overall = "OPEN_WATER_CANDIDATE"
        elif "MIXED_LAND_WATER" in classifications:
            overall = "MIXED_LAND_WATER"
        elif all(
            c == "LAND_LIKELY"
            for c in classifications
        ):
            overall = "LAND_LIKELY"
        else:
            overall = "INVALID"

        item[
            "overall_classification"
        ] = overall

        summary[overall] += 1
        items.append(item)

    # --------------------------------------------------------------
    # CSV
    # --------------------------------------------------------------

    fields = [
        "source_row",
        "doy",
        "sat",
        "freq",
        "solution_time_utc",
        "rh",
        "az",
        "amp",
        "pkn",
        "delt",
        "emin",
        "emax",
        "overall_classification",
    ]

    for elev in TEST_ELEVATIONS:
        tag = f"el_{elev:g}"
        fields += [
            f"{tag}_classification",
            f"{tag}_water_fraction",
            f"{tag}_area_m2",
            f"{tag}_water_area_m2",
            f"{tag}_min_signed_m",
            f"{tag}_max_signed_m",
            f"{tag}_center_signed_m",
        ]

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

        for item in items:

            out = {
                k: item.get(k)
                for k in fields
                if k in item
            }

            for elev in TEST_ELEVATIONS:
                tag = f"el_{elev:g}"
                m = item[tag]

                out[
                    f"{tag}_classification"
                ] = m["classification"]

                out[
                    f"{tag}_water_fraction"
                ] = (
                    f"{m['water_fraction']:.6f}"
                    if math.isfinite(
                        m["water_fraction"]
                    )
                    else ""
                )

                out[
                    f"{tag}_area_m2"
                ] = (
                    f"{m['area_m2']:.3f}"
                    if math.isfinite(
                        m["area_m2"]
                    )
                    else ""
                )

                out[
                    f"{tag}_water_area_m2"
                ] = (
                    f"{m['water_area_m2']:.3f}"
                    if math.isfinite(
                        m["water_area_m2"]
                    )
                    else ""
                )

                out[
                    f"{tag}_min_signed_m"
                ] = (
                    f"{m['min_signed_m']:.3f}"
                    if math.isfinite(
                        m["min_signed_m"]
                    )
                    else ""
                )

                out[
                    f"{tag}_max_signed_m"
                ] = (
                    f"{m['max_signed_m']:.3f}"
                    if math.isfinite(
                        m["max_signed_m"]
                    )
                    else ""
                )

                out[
                    f"{tag}_center_signed_m"
                ] = (
                    f"{m['center_signed_m']:.3f}"
                    if math.isfinite(
                        m["center_signed_m"]
                    )
                    else ""
                )

            writer.writerow(out)

    # --------------------------------------------------------------
    # KML
    # --------------------------------------------------------------

    kml_written = write_kml(
        items
    )

    # --------------------------------------------------------------
    # Summary
    # --------------------------------------------------------------

    by_freq = {}

    for item in items:
        freq = item["freq"]
        by_freq[freq] = (
            by_freq.get(freq, 0) + 1
        )

    by_sat_freq = {}

    for item in items:
        key = (
            item["sat"],
            item["freq"],
        )

        by_sat_freq[key] = (
            by_sat_freq.get(key, 0) + 1
        )

    with open(
        OUT_SUMMARY,
        "w",
    ) as f:

        f.write(
            "=" * 80 + "\n"
        )
        f.write(
            "MARCONI FRESNEL / WATER-SIDE GEOMETRY CLASSIFICATION\n"
        )
        f.write(
            "=" * 80 + "\n\n"
        )

        f.write(
            f"Input records: {len(rows)}\n"
        )

        f.write(
            f"Water bearing: {WATER_BEARING_DEG:.3f} deg\n"
        )

        f.write(
            f"Reference shoreline distance: "
            f"{SHORELINE_DISTANCE_M:.3f} m\n"
        )

        f.write(
            "NOTE: this is a reference shoreline plane, "
            "not a digitized shoreline polygon.\n\n"
        )

        f.write(
            "OVERALL CLASSIFICATION\n"
        )

        for key, value in summary.items():
            f.write(
                f"  {key}: {value}\n"
            )

        f.write(
            "\nBY FREQUENCY\n"
        )

        for freq, n in sorted(
            by_freq.items()
        ):
            f.write(
                f"  freq={freq}: {n}\n"
            )

        f.write(
            "\nREPEATED SAT/FREQ TRACKS\n"
        )

        for key, n in sorted(
            by_sat_freq.items()
        ):
            if n >= 2:
                f.write(
                    f"  sat={key[0]} "
                    f"freq={key[1]} "
                    f"n={n}\n"
                )

        f.write(
            "\nINTERPRETATION\n"
        )

        f.write(
            "OPEN_WATER_CANDIDATE means at least one sampled "
            "elevation (5, 10, or 15 deg) has >=80% of the "
            "Fresnel-zone polygon on the water side of the "
            "reference shoreline plane.\n"
        )

        f.write(
            "MIXED_LAND_WATER means the footprint intersects both "
            "sides at one or more sampled elevations.\n"
        )

        f.write(
            "LAND_LIKELY means all sampled elevations have <=20% "
            "of the footprint on the water side.\n"
        )

        f.write(
            "These classifications are diagnostic only. A real "
            "shoreline polygon should replace the reference plane "
            "before production masking is changed.\n"
        )

    print()
    print("=" * 80)
    print("OVERALL CLASSIFICATION")
    print("=" * 80)

    for key, value in summary.items():
        print(
            f"{key:24s}: {value}"
        )

    print()
    print("REPEATED SAT/FREQ TRACKS")

    for key, n in sorted(
        by_sat_freq.items()
    ):
        if n >= 2:
            print(
                f"  sat={key[0]:3d} "
                f"freq={key[1]:3d} "
                f"n={n}"
            )

    print()
    print("Outputs:")
    print(f"  {OUT_CSV}")
    if kml_written:
        print(f"  {OUT_KML}")
    print(f"  {OUT_SUMMARY}")

    print()
    print(
        "DONE"
    )


if __name__ == "__main__":
    main()
