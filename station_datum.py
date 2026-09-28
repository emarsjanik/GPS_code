"""
station_datum.py

The one place the vertical datum of this station's water levels is
defined, so every script reports the same thing.

ANTENNA HEIGHT. gnssrefl reports water level as Hortho - RH, where
Hortho is "gnssrefl_orthometric_height" in station/resources/station.json.
That value is read from there, never copied into a script.

  18.665 m  (old) came from a CSRS-PPP report, whose orthometric height
            is CGVD2013 -- the Canadian height system, not NAVD88.
  19.014 m  (current) NGS OPUS, NAVD88 computed with GEOID18,
            2026-09-28. The ellipsoid heights of the two solutions
            agree to 2 mm (-10.025 PPP vs -10.023 m OPUS ITRF2020), so
            the antenna position was never in question -- only the
            height system. Water levels made with 18.665 are 0.349 m
            LOW against NAVD88.

TIDE MODEL. The marconi_tides_*.xlsx models are not referenced to
NAVD88. Over a 47-day overlap the model ensemble sat 0.257 m above the
CGVD2013-based GNSS-IR level, i.e. 0.257 - 0.349 = ~0.09 m BELOW NAVD88.
TIDE_MODEL_TO_NAVD88_M is added to model heights to put them on NAVD88.

The old OFFSET_M = +0.242 in process_all_and_plot.py was this same
difference (0.349 - 0.09 ~ 0.26), measured empirically before its cause
was known. With Hortho on NAVD88 it must NOT be applied as well.
"""

from __future__ import annotations

import json
from pathlib import Path

STATION_JSON = Path(__file__).resolve().parent / "station" / "resources" / "station.json"

# Tide model ensemble -> NAVD88, metres (see module docstring).
TIDE_MODEL_TO_NAVD88_M = 0.09

# Heights that were NOT NAVD88, kept so old outputs can be recognised.
CGVD2013_HORTHO_M = 18.665
NAVD88_HORTHO_M = 19.014


def station_hortho(path: Path | str | None = None) -> float:
    """Antenna orthometric height (m) from station.json."""
    p = Path(path) if path else STATION_JSON
    with open(p) as f:
        value = json.load(f).get("gnssrefl_orthometric_height")
    if value is None:
        raise SystemExit(f"gnssrefl_orthometric_height is not set in {p}")
    return float(value)


def hortho_note(hortho: float) -> str:
    """One line saying which datum a Hortho value puts water levels on."""
    if abs(hortho - NAVD88_HORTHO_M) < 0.0015:
        return f"Hortho {hortho:.3f} m -> water levels in NAVD88"
    if abs(hortho - CGVD2013_HORTHO_M) < 0.0015:
        return (f"Hortho {hortho:.3f} m is CGVD2013, NOT NAVD88: water levels are "
                f"{NAVD88_HORTHO_M - hortho:.3f} m low")
    return f"Hortho {hortho:.3f} m (datum not recognised)"
