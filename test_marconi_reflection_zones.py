#!/usr/bin/env python3

import numpy as np
from pathlib import Path

import gnssrefl.refl_zones as rz
import simplekml


# ============================================================================
# MARCONI GNSS-IR REFLECTION-ZONE GEOMETRY TEST
# ============================================================================
#
# This is a diagnostic only.
#
# It does NOT modify gnssrefl.
# It does NOT modify usgs.json.
# It does NOT modify production processing.
#
# It uses the installed gnssrefl Fresnel-zone geometry directly.
#
# Station:
#   Marconi Beach, Wellfleet, MA
#
# Production station coordinates:
#   lat = 41.8928243333
#   lon = -69.9633227139
#
# Reference/reflector height established for our analysis:
#   18.665 m
#
# ============================================================================


STATION = "USGS Marconi"

LAT = 41.8928243333
LON = -69.9633227139

REFLECTOR_HEIGHT_M = 18.665

FREQUENCY = 1

AZIMUTHS = [
    90,
    100,
    110,
    120,
    130,
    140,
    150,
    160,
    170,
    180,
    190,
    200,
    210,
    215,
    220,
]

ELEVATIONS = [
    5,
    10,
    15,
]


def add_zone(kml, azimuth, elevation, color):
    """
    Generate one Fresnel-zone polygon using gnssrefl's actual
    makeEllipse_latlon() implementation.
    """

    lng, lat = rz.makeEllipse_latlon(
        FREQUENCY,
        elevation,
        REFLECTOR_HEIGHT_M,
        azimuth,
        LAT,
        LON,
    )

    coords = [
        (float(x), float(y))
        for x, y in zip(lng, lat)
    ]

    polygon = kml.newpolygon(
        name=f"AZ {azimuth} EL {elevation}"
    )

    polygon.outerboundaryis = coords

    polygon.style.linestyle.color = color
    polygon.style.linestyle.width = 2

    polygon.style.polystyle.color = (
        simplekml.Color.changealphaint(
            40,
            color
        )
    )

    return coords


def main():

    output = Path(
        "marconi_reflection_zone_geometry_test.kml"
    )

    print()
    print("=" * 80)
    print("MARCONI REFLECTION-ZONE GEOMETRY TEST")
    print("=" * 80)

    print()
    print(f"Station latitude       : {LAT:.10f}")
    print(f"Station longitude      : {LON:.10f}")
    print(
        f"Reference height      : "
        f"{REFLECTOR_HEIGHT_M:.3f} m"
    )
    print(f"Frequency              : {FREQUENCY}")
    print()
    print("Azimuths:")
    print(AZIMUTHS)
    print()
    print("Elevations:")
    print(ELEVATIONS)

    kml = simplekml.Kml()

    # ------------------------------------------------------------------------
    # Station point
    # ------------------------------------------------------------------------

    point = kml.newpoint(
        name="USGS Marconi GNSS station"
    )

    point.coords = [
        (LON, LAT)
    ]

    point.style.iconstyle.icon.href = (
        "http://maps.google.com/"
        "mapfiles/kml/shapes/placemark_circle.png"
    )

    # ------------------------------------------------------------------------
    # Colors by elevation
    # ------------------------------------------------------------------------

    colors = {
        5: simplekml.Color.yellow,
        10: simplekml.Color.red,
        15: simplekml.Color.blue,
    }

    # ------------------------------------------------------------------------
    # Generate zones
    # ------------------------------------------------------------------------

    for elevation in ELEVATIONS:

        color = colors[elevation]

        print()
        print(
            f"Elevation = {elevation} degrees"
        )

        for azimuth in AZIMUTHS:

            lng, lat = rz.makeEllipse_latlon(
                FREQUENCY,
                elevation,
                REFLECTOR_HEIGHT_M,
                azimuth,
                LAT,
                LON,
            )

            # Approximate center of polygon.
            #
            # This is only for diagnostic reporting. The actual
            # polygon is written below using the gnssrefl geometry.

            center_lon = float(np.mean(lng))
            center_lat = float(np.mean(lat))

            print(
                f"  AZ={azimuth:3d} "
                f"center approx "
                f"lat={center_lat:.6f} "
                f"lon={center_lon:.6f}"
            )

            add_zone(
                kml,
                azimuth,
                elevation,
                color,
            )

    # ------------------------------------------------------------------------
    # Save KML
    # ------------------------------------------------------------------------

    kml.save(str(output))

    print()
    print("=" * 80)
    print("DONE")
    print("=" * 80)
    print()
    print(
        f"KML written to:"
    )
    print(
        f"  {output.resolve()}"
    )
    print()


if __name__ == "__main__":
    main()
