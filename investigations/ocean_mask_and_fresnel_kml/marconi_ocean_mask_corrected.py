#!/usr/bin/env python3
"""
marconi_ocean_mask_corrected.py

Corrected version of test_marconi_ocean_mask.py. Two real, confirmed
problems fixed:

1. The original script passed H = 18.665 (our station's orthometric
   height above sea level, from station.json) directly as the
   reflector-height parameter to makeEllipse_latlon(). gnssrefl's own
   documentation confirms that parameter is explicitly "reflector
   height in meters" -- not orthometric height. Since ellipse size
   scales linearly with this value, that produced ellipses roughly
   18.665/1.6 = ~11.7x too large compared to reality, which could
   make it look like our reflection zones reach far out into open
   water when they don't.

2. The original azimuth sweep (90-150 deg) never actually included
   the true, confirmed bearing to water (~83 deg, measured directly
   in Google Earth from the real antenna position). This sweeps a
   wider range explicitly centered on that confirmed bearing instead.

This version sweeps across our real, observed RH range (0.51m to
2.12m, from tonight's actual 99-arc dataset) at 5 degrees elevation
(our lowest, farthest-reaching angle) -- giving an honest visual and
quantitative picture of how the reachable footprint actually changes
with tidal state, rather than assuming one fixed value.

Usage:
    python3 marconi_ocean_mask_corrected.py

Requires the gnssrefl virtual environment active (for
gnssrefl.refl_zones) and simplekml installed.
"""

import numpy as np
import simplekml
from gnssrefl.refl_zones import makeEllipse_latlon

LAT = 41.8928243333
LON = -69.9633227139

# Real RH values observed across tonight's actual 99-arc dataset --
# spanning our real tidal range, not a single assumed value.
RH_VALUES = {
    "low_tide_max_reach_RH2.12m": 2.12,   # highest observed RH = lowest water = farthest reach
    "typical_RH1.60m": 1.60,               # our typical/median observed RH
    "high_tide_min_reach_RH0.51m": 0.51,   # lowest observed RH = highest water = shortest reach
}

FREQ = 1

# PROPOSED NEW WATER-FACING MASK: centered on the confirmed true
# bearing to water (83.06 deg, verified directly). Widened from an
# initial 55-115 deg proposal to 35-135 deg, based on direct visual
# confirmation in Google Earth that petals 20 deg further out on each
# side still reach open water. This is a PROPOSAL to visually verify
# in Google Earth before touching production config -- not yet
# applied anywhere.
# PROPOSED NEW WATER-FACING MASK: a full 180 deg arc centered on the
# confirmed true bearing to water (83 deg). This range genuinely
# wraps around 0/360 deg (83-90=-7 -> 353, 83+90=173), so it's built
# with modulo arithmetic here to handle that correctly rather than
# risk a silent range-generation bug. Confirmed for production use as
# two positive-only sector pairs: [353, 360] and [0, 173].
_CENTER_AZ = 83
_HALF_WIDTH = 90
AZIMUTHS = [(_CENTER_AZ + offset) % 360 for offset in range(-_HALF_WIDTH, _HALF_WIDTH + 1, 5)]

ELEVATION = 5.0  # our lowest, farthest-reaching angle

OUT = "marconi_ocean_mask_proposed_180deg.kml"

RH_COLORS = {
    "low_tide_max_reach_RH2.12m": simplekml.Color.red,
    "typical_RH1.60m": simplekml.Color.yellow,
    "high_tide_min_reach_RH0.51m": simplekml.Color.blue,
}

# Our currently configured (confirmed wrong) production sectors, for
# direct visual comparison against the proposed footprints.
CURRENT_SECTORS = [(100, 130), (150, 215)]

kml = simplekml.Kml()

# Confirmed necessary: mutating .style.linestyle.color per-feature
# (the original pattern) produced a real, reproducible bug across
# every single polygon when combined with folders and this many
# features (~100) -- Google Earth reported "referencing a style that
# does not exist" for every polygon. Explicitly creating one shared
# Style object per color upfront and assigning it directly avoids
# simplekml's automatic per-feature style creation/deduplication,
# which is the documented, recommended pattern for exactly this
# situation (many features sharing a small number of styles).
shared_styles = {}
for rh_label, color in RH_COLORS.items():
    style = simplekml.Style()
    style.linestyle.color = color
    style.linestyle.width = 2
    style.polystyle.color = simplekml.Color.changealphaint(35, color)
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

        # Flag whether this azimuth falls within our currently
        # configured production sectors, for direct visual/labeling
        # comparison.
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

# Mark the confirmed true bearing to water directly, as a simple
# reference line, so it's immediately visible against the ellipses.
true_bearing_deg = 83.06
true_bearing_length_m = 71.78  # confirmed measured distance
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

kml.save(OUT)

print()
print("=" * 80)
print("MARCONI OCEAN MASK -- CORRECTED")
print("=" * 80)
print(f"Station    : {LAT}, {LON}")
print(f"RH values  : {list(RH_VALUES.values())} m (real observed range)")
print(f"Frequency  : {FREQ}")
print(f"Elevation  : {ELEVATION} deg (lowest/farthest-reaching)")
print()
print(f"Azimuths swept: {AZIMUTHS[0]}-{AZIMUTHS[-1]} deg (centered on confirmed true bearing 83 deg)")
print()
print("Also drawn: the confirmed true bearing/distance line to water")
print("(83.06 deg, 71.78m), and a label on every ellipse currently")
print("inside our production mask (100-130, 150-215) for direct comparison.")
print()
print(f"KML written to: {OUT}")
print()
print(f"Zones generated: {len(RH_VALUES) * len(AZIMUTHS)}")
