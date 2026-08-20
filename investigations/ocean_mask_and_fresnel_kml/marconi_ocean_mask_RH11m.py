#!/usr/bin/env python3
"""
marconi_ocean_mask_RH11m.py

Tests a new hypothesis: the widened quickLook search (10-25m) found a
consistent, repeatable RH ~11.2m at azimuth ~112 deg (our established
PRN29 ocean-facing geometry) across all four days (204-207) -- not
the production-reported ~1.8m, and not the ~18.665m orthometric-height
guess either.

Since Fresnel zone reach scales linearly with RH, this checks directly
whether a ~11.2m reflector height produces a footprint that actually
reaches the real, confirmed shoreline (bearing 83.06 deg, 71.78m away,
measured directly in Google Earth earlier).

Sweeps RH across the observed range from the quickLook results
(11.2m to 12.9m, matching what was actually seen across azimuths
33 deg and 112 deg over all four days) at 5 degrees elevation (lowest,
farthest-reaching angle).

Usage:
    python3 marconi_ocean_mask_RH11m.py

Requires the gnssrefl virtual environment active (for
gnssrefl.refl_zones) and simplekml installed.
"""

import numpy as np
import simplekml
from gnssrefl.refl_zones import makeEllipse_latlon

LAT = 41.8928243333
LON = -69.9633227139

# The actual, repeatable RH values found via quickLook's widened
# 10-25m search, across all four days (204-207) at the two real,
# QC-passing azimuths (~33 deg and ~112 deg -- our PRN29 geometry).
RH_VALUES = {
    "RH11.2m_az112_typical": 11.2,   # the consistent ~112 deg peak seen all 4 days
    "RH12.9m_az33": 12.9,             # the other real, QC-passing azimuth's peak
}

FREQ = 1

# Centered on the confirmed true bearing to water (83 deg), with
# enough spread to also directly show the 112 deg and 33 deg
# geometries where these RH values were actually observed.
AZIMUTHS = list(range(20, 141, 5))

ELEVATION = 5.0  # lowest, farthest-reaching angle

OUT = "marconi_ocean_mask_RH11m.kml"

RH_COLORS = {
    "RH11.2m_az112_typical": simplekml.Color.red,
    "RH12.9m_az33": simplekml.Color.orange,
}

# Our currently configured (180 deg placeholder) production sectors,
# for reference.
CURRENT_SECTORS = [(353, 360), (0, 173)]

kml = simplekml.Kml()

# Confirmed necessary (same real bug found and fixed earlier tonight):
# explicit shared Style objects, not per-feature .style mutation,
# to avoid the "referencing a style that does not exist" error that
# occurs with this many features inside folders.
shared_styles = {}
for rh_label, color in RH_COLORS.items():
    style = simplekml.Style()
    style.linestyle.color = color
    style.linestyle.width = 2
    style.polystyle.color = simplekml.Color.changealphaint(40, color)
    shared_styles[rh_label] = style

for rh_label, rh_value in RH_VALUES.items():
    folder = kml.newfolder(name=f"RH = {rh_value}m ({rh_label})")
    for az in AZIMUTHS:
        lng, lat = makeEllipse_latlon(
            FREQ,
            ELEVATION,
            rh_value,
            az,
            LAT,
            LON,
        )
        coords = [(float(x), float(y)) for x, y in zip(lng, lat)]

        in_current_sector = any(lo <= az <= hi for lo, hi in CURRENT_SECTORS)
        sector_tag = " [IN CURRENT MASK]" if in_current_sector else ""

        p = folder.newpolygon(name=f"AZ {az} EL {ELEVATION}{sector_tag}")
        p.outerboundaryis = coords
        p.style = shared_styles[rh_label]

station = kml.newpoint(name="USGS Marconi GNSS station")
station.coords = [(LON, LAT)]
station.style.iconstyle.icon.href = (
    "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"
)

# The confirmed true bearing/distance line to water, same as before.
true_bearing_deg = 83.06
true_bearing_length_m = 71.78
R_EARTH = 6371000.0
true_bearing_rad = np.radians(true_bearing_deg)
dlat = (true_bearing_length_m * np.cos(true_bearing_rad)) / R_EARTH
dlon = (true_bearing_length_m * np.sin(true_bearing_rad)) / (R_EARTH * np.cos(np.radians(LAT)))
end_lat = LAT + np.degrees(dlat)
end_lon = LON + np.degrees(dlon)

bearing_line = kml.newlinestring(name="Confirmed true bearing to water (83.06 deg, 71.78m)")
bearing_line.coords = [(LON, LAT), (end_lon, end_lat)]
bearing_line.style.linestyle.color = simplekml.Color.white
bearing_line.style.linestyle.width = 5

# Also mark the specific 112 deg bearing directly, since that's the
# exact azimuth where the ~11.2m RH was actually, repeatedly observed.
prn29_bearing_deg = 112.98  # exact mean azimuth from tonight's real data
prn29_rad = np.radians(prn29_bearing_deg)
dlat2 = (150 * np.cos(prn29_rad)) / R_EARTH  # long reference line, 150m, just for visual context
dlon2 = (150 * np.sin(prn29_rad)) / (R_EARTH * np.cos(np.radians(LAT)))
end_lat2 = LAT + np.degrees(dlat2)
end_lon2 = LON + np.degrees(dlon2)

prn29_line = kml.newlinestring(name="PRN29 observed bearing (112.98 deg)")
prn29_line.coords = [(LON, LAT), (end_lon2, end_lat2)]
prn29_line.style.linestyle.color = simplekml.Color.yellow
prn29_line.style.linestyle.width = 4

kml.save(OUT)

print()
print("=" * 80)
print("MARCONI OCEAN MASK -- TESTING RH~11.2m HYPOTHESIS")
print("=" * 80)
print(f"Station    : {LAT}, {LON}")
print(f"RH values  : {list(RH_VALUES.values())} m (from real quickLook widened search)")
print(f"Frequency  : {FREQ}")
print(f"Elevation  : {ELEVATION} deg (lowest/farthest-reaching)")
print()
print(f"Azimuths swept: {AZIMUTHS[0]}-{AZIMUTHS[-1]} deg")
print()
print("Also drawn: the confirmed true bearing/distance line to water")
print("(83.06 deg, 71.78m, white), and the exact PRN29 observed bearing")
print("(112.98 deg, yellow) for direct visual comparison.")
print()
print(f"KML written to: {OUT}")
print()
print(f"Zones generated: {len(RH_VALUES) * len(AZIMUTHS)}")
