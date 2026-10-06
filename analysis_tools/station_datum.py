"""
station_datum.py

The one place the vertical datum of this station's water levels is
defined. Every product -- the spline, the monthly archive files, the
TWL export, the plots -- is NAVD88, and gets there through here.

ANTENNA HEIGHT. gnssrefl reports water level as Hortho - RH, where
Hortho is "gnssrefl_orthometric_height" in station/resources/station.json
(passed to gnssrefl as a list; see station/gnssrefl_processor.py).

  18.665 m  (old) came from a CSRS-PPP report, whose orthometric height
            is CGVD2013 -- the Canadian height system, not NAVD88.
  19.014 m  (current) NGS OPUS, NAVD88 computed with GEOID18,
            2026-09-28, +/-0.061 m (mostly geoid model). The ellipsoid
            heights of the two solutions agree to 2 mm (-10.025 m PPP,
            -10.023 m OPUS ITRF2020): the antenna never moved, only the
            height system differs. Water levels made with 18.665 are
            0.349 m LOW against NAVD88.

station.json declares its datum with "vertical_datum". Until it says
NAVD88 (maintenance/migrate_to_navd88.py does that), a configured
height equal to the old CGVD2013 value is recognised and mapped to the
NAVD88 one, so products are NAVD88 either way. Any other undeclared
height is used as it is, with a warning.

Changing Hortho does not change RH, only the constant subtracted from
it, so a spline written with the old height converts exactly: the
level becomes (NAVD88 Hortho - RH), and RH is column 2 of the file.

TIDE MODEL. The marconi_tides_*.xlsx models are not referenced to
NAVD88. Over a 47-day overlap the model ensemble sat 0.257 m above the
CGVD2013-based GNSS-IR level, i.e. 0.257 - 0.349 = ~0.09 m BELOW
NAVD88. "tide_model_navd88_offset" in station.json (default +0.09 m)
is added to model heights to put them on NAVD88.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATION_JSON = PROJECT_DIR / "station" / "resources" / "station.json"

NAVD88_HORTHO_M = 19.014          # NGS OPUS 2026-09-28, NAVD88 (GEOID18)
CGVD2013_HORTHO_M = 18.665        # CSRS-PPP, CGVD2013 (CGG2013a) -- superseded
DEFAULT_TIDE_MODEL_TO_NAVD88_M = 0.09

_TOL = 0.0015


def load_station(path: Path | str | None = None) -> dict:
    """station.json as a dict; {} if it is missing or unreadable."""
    p = Path(path) if path else STATION_JSON
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def declared_navd88(cfg: dict) -> bool:
    """True when station.json says its heights are NAVD88."""
    return str(cfg.get("vertical_datum", "")).upper().startswith("NAVD88")


def configured_hortho(cfg: dict) -> float | None:
    v = cfg.get("gnssrefl_orthometric_height")
    return float(v) if v is not None else None


def navd88_hortho(cfg: dict | None = None) -> float | None:
    """The antenna height on NAVD88 (m), or None if it cannot be told.

    A declared-NAVD88 station.json is taken at its word. An undeclared
    one holding the old CGVD2013 height maps to the OPUS NAVD88 height.
    Anything else is returned as configured, with a warning, because
    nothing here can say which datum it is in."""
    cfg = load_station() if cfg is None else cfg
    h = configured_hortho(cfg)
    if h is None:
        return None
    if declared_navd88(cfg):
        return h
    if abs(h - CGVD2013_HORTHO_M) < _TOL:
        return NAVD88_HORTHO_M
    if abs(h - NAVD88_HORTHO_M) < _TOL:
        return h
    print(f"  WARNING: gnssrefl_orthometric_height {h:.3f} m has no vertical_datum "
          f"in station.json; treated as NAVD88")
    return h


def config_to_navd88(cfg: dict | None = None) -> float:
    """Metres to add to a height expressed in station.json's datum to
    put it on NAVD88 (0.349 for an unmigrated CGVD2013 config, else 0)."""
    cfg = load_station() if cfg is None else cfg
    h, n = configured_hortho(cfg), navd88_hortho(cfg)
    return 0.0 if h is None or n is None else n - h


def tide_model_to_navd88(cfg: dict | None = None) -> float:
    """Metres added to tide-model heights to put them on NAVD88."""
    cfg = load_station() if cfg is None else cfg
    v = cfg.get("tide_model_navd88_offset")
    return float(v) if v is not None else DEFAULT_TIDE_MODEL_TO_NAVD88_M


def spline_hortho(path: Path | str) -> float | None:
    """The Hortho gnssrefl wrote into a spline file's header, if any
    ("% orthometric height minus RH, where Hortho (m) is   18.665")."""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if not line.startswith("%"):
                    break
                if "Hortho (m) is" in line:
                    return float(line.rsplit("is", 1)[1].split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return None


def spline_shift(path: Path | str, cfg: dict | None = None, quiet: bool = False) -> float:
    """Metres to add to a spline file's water levels to put them on
    NAVD88: (NAVD88 antenna height) - (height in the file header).
    Zero for a file already on NAVD88, or when either height is
    unknown."""
    target = navd88_hortho(cfg)
    in_file = spline_hortho(path)
    if target is None or in_file is None:
        return 0.0
    shift = target - in_file
    if abs(shift) < _TOL:
        return 0.0
    if not quiet:
        print(f"  {Path(path).name}: Hortho {in_file:.3f} m in file -> "
              f"{target:.3f} m NAVD88, levels shifted {shift:+.3f} m")
    return shift


def hortho_note(hortho: float) -> str:
    """One line saying which datum a Hortho value puts water levels on."""
    if abs(hortho - NAVD88_HORTHO_M) < _TOL:
        return f"Hortho {hortho:.3f} m -> water levels in NAVD88"
    if abs(hortho - CGVD2013_HORTHO_M) < _TOL:
        return (f"Hortho {hortho:.3f} m is CGVD2013, NOT NAVD88: water levels are "
                f"{NAVD88_HORTHO_M - hortho:.3f} m low")
    return f"Hortho {hortho:.3f} m (datum not recognised)"
