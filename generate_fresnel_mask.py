#!/usr/bin/env python3
"""
generate_fresnel_mask.py

Interactive Fresnel-zone / ocean-mask KML generator for any GNSS-IR
station.

This is a generalized, reusable version of the proven, bug-fixed
reflection-zone tooling built during the Marconi investigation. It
carries forward two confirmed, important fixes:

1. Reflector height (RH), not orthometric/antenna height, must be
   passed to gnssrefl's makeEllipse_latlon(). Passing the wrong value
   here produces ellipses that are wildly (often >10x) the wrong
   size. This tool always asks for and uses RH directly.

2. simplekml requires explicit, shared Style objects (one per color)
   assigned directly to each feature -- mutating .style per-feature
   causes a real, reproducible "referencing a style that does not
   exist" error in Google Earth once you have more than a handful of
   polygons. This tool uses the shared-style pattern throughout.

It also correctly handles azimuth ranges that wrap through 0/360 deg
(e.g. a center bearing near due north), using modulo arithmetic
rather than a naive min/max range.

WHAT THIS TOOL ANSWERS
-----------------------
Given your station's real antenna position and a range of expected
reflector heights (which vary with tide), where does your GNSS-IR
reflection footprint actually fall on the ground? This produces a KML
you can open in Google Earth over real satellite imagery to check,
directly, whether your azimuth mask is actually pointed at water --
or at whatever else you're trying to measure.

USAGE
-----
Interactive (recommended for first-time use):
    python3 generate_fresnel_mask.py

Non-interactive (for scripting/automation), pass all values as flags:
    python3 generate_fresnel_mask.py \\
        --lat 41.8928243333 --lon -69.9633227139 \\
        --rh-min 0.51 --rh-typical 1.60 --rh-max 2.12 \\
        --center-azimuth 83 --half-width 90 \\
        --elevation 5.0 --freq 1 \\
        --output my_station_mask.kml

Requires the gnssrefl virtual environment active (for
gnssrefl.refl_zones) and the simplekml package installed.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import simplekml
from gnssrefl.refl_zones import makeEllipse_latlon


# ---------------------------------------------------------------------
# INTERACTIVE INPUT HELPERS
# ---------------------------------------------------------------------

def ask_float(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"  Not a number, using default ({default}).")
        return default


def ask_int(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  Not a whole number, using default ({default}).")
        return default


def ask_str(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw if raw else default


def collect_inputs_interactively():
    print()
    print("=" * 78)
    print("FRESNEL / OCEAN-MASK KML GENERATOR -- interactive setup")
    print("=" * 78)
    print()
    print("Press Enter at any prompt to accept the default shown in [brackets].")
    print("Defaults below are pre-filled with a real, previously validated")
    print("station's values as a working example -- replace them with your own.")
    print()

    print("--- Station position ---")
    lat = ask_float("Station latitude (decimal degrees)", 41.8928243333)
    lon = ask_float("Station longitude (decimal degrees)", -69.9633227139)

    print()
    print("--- Reflector height (RH) range ---")
    print("RH is the vertical distance from the antenna to the reflecting")
    print("surface -- NOT the antenna's orthometric/sea-level height. It")
    print("changes with the tide. If you don't know your real observed RH")
    print("range yet, run gnssrefl's quickLook tool first to find it.")
    rh_min = ask_float("Lowest observed RH, i.e. highest water (m)", 0.51)
    rh_typical = ask_float("Typical/median observed RH (m)", 1.60)
    rh_max = ask_float("Highest observed RH, i.e. lowest water (m)", 2.12)

    print()
    print("--- Azimuth mask ---")
    print("Center this on the TRUE bearing to water, measured directly")
    print("(e.g. in Google Earth), not assumed from a rough compass guess.")
    center_azimuth = ask_float("Center azimuth, true bearing to water (deg)", 83.0)
    half_width = ask_float("Half-width of the mask (deg each side of center)", 90.0)
    az_step = ask_float("Azimuth step size for the sweep (deg)", 5.0)

    print()
    print("--- Elevation and frequency ---")
    elevation = ask_float("Elevation angle to test (deg, lower = farther reach)", 5.0)
    freq = ask_int("GNSS frequency code (1=GPS L1, 20=GLONASS L1, etc.)", 1)

    print()
    print("--- Output ---")
    output = ask_str("Output KML filename", "fresnel_mask.kml")
    if not output.endswith(".kml"):
        output += ".kml"

    return {
        "lat": lat, "lon": lon,
        "rh_min": rh_min, "rh_typical": rh_typical, "rh_max": rh_max,
        "center_azimuth": center_azimuth, "half_width": half_width,
        "az_step": az_step, "elevation": elevation, "freq": freq,
        "output": output,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a Fresnel-zone/ocean-mask KML for a GNSS-IR station."
    )
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--rh-min", type=float)
    parser.add_argument("--rh-typical", type=float)
    parser.add_argument("--rh-max", type=float)
    parser.add_argument("--center-azimuth", type=float)
    parser.add_argument("--half-width", type=float, default=90.0)
    parser.add_argument("--az-step", type=float, default=5.0)
    parser.add_argument("--elevation", type=float, default=5.0)
    parser.add_argument("--freq", type=int, default=1)
    parser.add_argument("--output", type=str, default="fresnel_mask.kml")
    return parser.parse_args()


# ---------------------------------------------------------------------
# CORE LOGIC
# ---------------------------------------------------------------------

def build_azimuth_list(center_azimuth, half_width, step):
    """
    Builds an azimuth sweep centered on center_azimuth, correctly
    wrapping through 0/360 deg using modulo arithmetic rather than a
    naive min/max range (which silently breaks for any center near
    due north).
    """
    n_steps = int(round((2 * half_width) / step))
    return [
        (center_azimuth + offset) % 360
        for offset in np.linspace(-half_width, half_width, n_steps + 1)
    ]


def project_point(lat, lon, bearing_deg, distance_m):
    """Simple great-circle-ish projection for short reference lines."""
    R_EARTH = 6371000.0
    bearing_rad = np.radians(bearing_deg)
    dlat = (distance_m * np.cos(bearing_rad)) / R_EARTH
    dlon = (distance_m * np.sin(bearing_rad)) / (R_EARTH * np.cos(np.radians(lat)))
    return lat + np.degrees(dlat), lon + np.degrees(dlon)


def generate_kml(config):
    lat = config["lat"]
    lon = config["lon"]
    freq = config["freq"]
    elevation = config["elevation"]
    output = config["output"]

    rh_values = {
        "low_tide_max_reach": config["rh_max"],
        "typical": config["rh_typical"],
        "high_tide_min_reach": config["rh_min"],
    }

    rh_colors = {
        "low_tide_max_reach": simplekml.Color.red,
        "typical": simplekml.Color.yellow,
        "high_tide_min_reach": simplekml.Color.blue,
    }

    azimuths = build_azimuth_list(
        config["center_azimuth"], config["half_width"], config["az_step"]
    )

    kml = simplekml.Kml()

    # Confirmed necessary fix: explicit shared Style objects, one per
    # color, assigned directly -- not mutated per-feature. See module
    # docstring for why.
    shared_styles = {}
    for label, color in rh_colors.items():
        style = simplekml.Style()
        style.linestyle.color = color
        style.linestyle.width = 2
        style.polystyle.color = simplekml.Color.changealphaint(35, color)
        shared_styles[label] = style

    for label, rh_value in rh_values.items():
        folder = kml.newfolder(name=f"RH = {rh_value}m ({label})")
        for az in azimuths:
            lng, latp = makeEllipse_latlon(freq, elevation, rh_value, az, lat, lon)
            coords = [(float(x), float(y)) for x, y in zip(lng, latp)]

            p = folder.newpolygon(name=f"AZ {az:.1f} EL {elevation}")
            p.outerboundaryis = coords
            p.style = shared_styles[label]

    station = kml.newpoint(name="GNSS station")
    station.coords = [(lon, lat)]
    station.style.iconstyle.icon.href = (
        "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"
    )

    # Reference line at the center azimuth, at the typical RH's
    # approximate max reach, so the intended bearing is immediately
    # visible against the ellipses.
    ref_length_m = config["rh_typical"] * 11.5  # rough reach at 5 deg elevation
    end_lat, end_lon = project_point(
        lat, lon, config["center_azimuth"], ref_length_m
    )
    bearing_line = kml.newlinestring(
        name=f"Center azimuth ({config['center_azimuth']:.2f} deg)"
    )
    bearing_line.coords = [(lon, lat), (end_lon, end_lat)]
    bearing_line.style.linestyle.color = simplekml.Color.white
    bearing_line.style.linestyle.width = 5

    kml.save(output)

    print()
    print("=" * 78)
    print("FRESNEL / OCEAN-MASK KML -- SUMMARY")
    print("=" * 78)
    print(f"Station     : {lat}, {lon}")
    print(f"RH values   : {list(rh_values.values())} m")
    print(f"Frequency   : {freq}")
    print(f"Elevation   : {elevation} deg")
    print(f"Azimuths    : {azimuths[0]:.1f} to {azimuths[-1]:.1f} deg "
          f"(center {config['center_azimuth']:.1f}, +/-{config['half_width']:.0f})")
    print()
    print(f"KML written to: {output}")
    print(f"Zones generated: {len(rh_values) * len(azimuths)}")
    print()
    print("NEXT STEP: open this KML in Google Earth over real satellite")
    print("imagery and check directly whether the red (lowest-tide,")
    print("farthest-reaching) ellipses actually cross into open water at")
    print("your true center azimuth. If they fall short, no azimuth choice")
    print("will fix it -- the antenna height/reach itself needs increasing.")


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main():
    args = parse_args()

    # If the required core values weren't all passed as flags, fall
    # back to interactive prompts for a friendlier first-run experience.
    required = [args.lat, args.lon, args.rh_min, args.rh_typical,
                args.rh_max, args.center_azimuth]

    if any(v is None for v in required):
        config = collect_inputs_interactively()
    else:
        config = {
            "lat": args.lat, "lon": args.lon,
            "rh_min": args.rh_min, "rh_typical": args.rh_typical,
            "rh_max": args.rh_max,
            "center_azimuth": args.center_azimuth,
            "half_width": args.half_width, "az_step": args.az_step,
            "elevation": args.elevation, "freq": args.freq,
            "output": args.output if args.output.endswith(".kml")
                else args.output + ".kml",
        }

    generate_kml(config)


if __name__ == "__main__":
    main()
