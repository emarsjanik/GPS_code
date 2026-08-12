import numpy as np
import simplekml

from gnssrefl.refl_zones import makeEllipse_latlon


LAT = 41.8928243333
LON = -69.9633227139
H = 18.665
FREQ = 1

AZIMUTHS = list(range(90, 151, 5))
ELEVATIONS = [5, 10, 15]

OUT = "marconi_ocean_mask_test.kml"


kml = simplekml.Kml()

colors = {
    5: simplekml.Color.yellow,
    10: simplekml.Color.blue,
    15: simplekml.Color.red,
}

for az in AZIMUTHS:

    for el in ELEVATIONS:

        lng, lat = makeEllipse_latlon(
            FREQ,
            el,
            H,
            az,
            LAT,
            LON,
        )

        coords = [
            (float(x), float(y))
            for x, y in zip(lng, lat)
        ]

        p = kml.newpolygon(
            name=f"AZ {az} EL {el}"
        )

        p.outerboundaryis = coords

        p.style.linestyle.color = colors[el]
        p.style.linestyle.width = 3

        p.style.polystyle.color = (
            simplekml.Color.changealphaint(
                40,
                colors[el]
            )
        )


station = kml.newpoint(name="USGS Marconi GNSS station")
station.coords = [(LON, LAT)]

station.style.iconstyle.icon.href = (
    "http://maps.google.com/mapfiles/"
    "kml/shapes/placemark_circle.png"
)

kml.save(OUT)

print()
print("=" * 80)
print("MARCONI OCEAN MASK TEST")
print("=" * 80)
print(f"Station : {LAT}, {LON}")
print(f"Height  : {H} m")
print(f"Frequency: {FREQ}")
print()
print("Azimuths:")
print(AZIMUTHS)
print()
print("Elevations:")
print(ELEVATIONS)
print()
print(f"KML written to:")
print(f"  {OUT}")
print()
print("Zones generated:", len(AZIMUTHS) * len(ELEVATIONS))
