#!/usr/bin/env python3
"""
independent_datum.py

Where local mean sea level -- and mean high and low water -- sit on
NAVD88 at Marconi, from sources that owe nothing to this station's
GNSS-IR record, and how far the two station.json offsets that were
measured against that record lie from them. Report only: nothing in
station.json or in any product is changed.

WHY (review finding B11)

Two offsets in station.json put other things on the water levels'
datum, and both were measured against the GNSS-IR record itself:

  tide_model_navd88_offset  (+0.09 m) the mean GNSS-IR-minus-model
                            difference over a 47-day overlap, moved
                            onto NAVD88 (station_datum.py).
  water_level_msl_offset    (~+0.10 m) the zero of the public 7-day
                            plot. Nothing records how it was set, but
                            its value and plot_7day.py's history point
                            to the same GNSS-IR-relative origin.

Each therefore absorbs whatever absolute bias the GNSS-IR level had
when it was set, and a comparison made through them cannot see that
bias (compare_to_tide_deviation.py says so). Neither enters the NAVD88
water level itself (Hortho - RH), the TWL export or the camera DEM.
What breaks the circle is a height for mean sea level on NAVD88 at the
site that does not come from the GNSS-IR, which is what this tool
puts together.

SOURCES. Each gives tidal datums on NAVD88 for the National Tidal
Datum Epoch (NTDE), the 19-year mean that NOAA's datums describe --
1983-2001 at the time of writing (Gill & Schultz 2001, NOAA Special
Publication NOS CO-OPS 1).

  CO-OPS   NOAA's published datums for tide stations with a levelled
           NAVD88 tie (metadata API, .../stations/<id>/datums.json).
           MSL - NAVD88 is the difference of the two published
           heights. NEARBY_STATIONS lists the stations; a live run
           prints each one's name and distance from its own metadata,
           so a wrong ID shows, and also lists every other NOAA datum
           station within 30 km (shown, not counted).
  VDatum   NOAA's datum-transformation model at Marconi itself: 0 m on
           LMSL, MHW and MLW converted to NAVD88 (REST API /convert),
           at the antenna, at the centre of the reflection zone and
           1 km offshore along the same azimuth. VDatum's tidal grids
           end at the shoreline, so the antenna on its bluff may get
           no value; the two seaward points are the ones that matter.
  Offline  OFFLINE_DATUMS: the published values this tool was written
           against, each with its source and the date it was read,
           used when NOAA cannot be reached. A --cache file saved by
           an earlier live run is preferred to them. There is no
           offline VDatum value: VDatum could not be reached when this
           was written, and a number not read from it is not put in
           its place.

EPOCH. A tidal datum is the epoch's mean, and sea level has risen
since: about 3-4 mm/yr on this coast, 10-14 cm since 1992. Present
mean sea level is the epoch value plus the mean of the reference
station's monthly mean sea levels over the latest whole year NOAA has
published (verified months arrive a month or two late), requested on
the MSL datum so that each is that month's departure from the epoch
mean (CO-OPS monthly_mean product). Twelve consecutive whole months,
so the seasonal cycle cancels. Offline, it is a linear sea-level
trend times the years since the middle of the epoch (OFFLINE_TRENDS),
with the spread of the trends of the long-term stations nearby as its
uncertainty. That trend is a lower bound rather than a centre: the
rise has been faster since the 1990s than over the long records, and a
single year can sit a few cm off any trend. MHW and MLW are moved by
the same amount: the range of the tide has not changed measurably over
that time. The epochs of the chosen source, the monthly means and the
spread are compared; a source on another epoch is left out of the
spread and named.

BEST ESTIMATE. VDatum at the reflection zone (or offshore) when it
answers: it is a model, but of the open coast at Marconi, and its MHW
and MLW are Marconi's. Its budget is VDatum's own skill here -- VDatum
asked at the local CO-OPS stations, against their published MSL --
and the spread over the VDatum points at the site. Otherwise Chatham,
the nearest NAVD88-tied gauge, for mean sea level only. Chatham sits
in a back-barrier harbour whose inlet damps the tide, low waters most
(cam compare_gnssr_to_gauge.py): its mean level is a few cm above the
open coast's and its MHW and MLW are not Marconi's at all, so they are
printed as the harbour's own, with no uncertainty. Its budget is the
published datum error and, as the transfer to Marconi, half the
spread of MSL - NAVD88 over the local stations; none of them is on
the open Atlantic coast (Chatham's harbour, Provincetown on Cape Cod
Bay, Saquatucket on Nantucket Sound), and much of that spread is in
their NAVD88 ties rather than in the tide, so it stands in for both.

WHAT IT PRINTS. The datums from each source and where they came from,
the epoch correction, the best present-day estimate with its
uncertainty budget, each configured offset minus that estimate (the
bias it implies), and how long a co-located gauge would have to run
to measure the GNSS-IR bias to a given uncertainty (calibration_plan;
protocol in docs/INDEPENDENT_DATUM.md, after Larson et al. 2013,
2017; Lofgren et al. 2014; Purnell et al. 2020). --daily-sigma sets
the daily scatter that table assumes.

Usage (on the station; the machine this was written on could not
reach NOAA):
    python3 analysis_tools/independent_datum.py
    python3 analysis_tools/independent_datum.py \\
        --cache products/refl_code/Files/usgs/independent_datum_cache.json \\
        --json products/refl_code/Files/usgs/independent_datum.json
    python3 analysis_tools/independent_datum.py --window START END
        (YYYY-MM-DD; the real overlap's dates are not recorded)
    python3 analysis_tools/independent_datum.py --offline
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from station_datum import (NAVD88_HORTHO_M, config_to_navd88, load_station,  # noqa: E402
                           navd88_hortho, tide_model_to_navd88)

# ---------------------------------------------------------------------
# SOURCES
# ---------------------------------------------------------------------

COOPS_DATUMS_API = ("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/"
                    "{station}/datums.json?units=metric")
COOPS_STATION_API = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{station}.json"
# Every station with published datums (id, name, lat, lng); used to show
# the ones near Marconi that NEARBY_STATIONS does not list.
COOPS_DATUM_STATIONS_API = ("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
                            "?type=datums")
COOPS_MONTHLY_API = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=monthly_mean"
                     "&station={station}&begin_date={begin}&end_date={end}&datum=MSL&units=metric"
                     "&time_zone=gmt&format=json&application=gnssir_station_datum")
# 0 m on a tidal datum -> its height on NAVD88. Request layout after
# NOAA's own client (noaa-ocs-hydrography/vyperdatum, vdatum_rest_utils.py),
# which spells the geoid parameters s_v_goid/t_v_goid; the geoid does not
# enter a tidal-datum-to-NAVD88 conversion. A point outside the tidal
# grids answers t_z = -999999.
VDATUM_API = ("https://vdatum.noaa.gov/vdatumweb/api/convert?s_x={lon:.7f}&s_y={lat:.7f}&s_z=0"
              "&region=contiguous&s_h_frame=NAD83_2011&s_coor=geo&s_v_frame={frame}&s_v_unit=m"
              "&s_v_elevation=height&s_v_geoid=geoid18&t_h_frame=NAD83_2011&t_coor=geo"
              "&t_v_frame=NAVD88&t_v_unit=m&t_v_elevation=height&t_v_geoid=geoid18")
FETCH_TIMEOUT_S = 30

NTDE = "1983-2001"
DATUM_NAMES = ("MHHW", "MHW", "MSL", "MTL", "MLW", "MLLW")
SHOWN = ("MSL", "MHW", "MLW")
VDATUM_FRAMES = {"MSL": "LMSL", "MHW": "MHW", "MLW": "MLW"}

# The documented Marconi antenna and azimuth regions
# (STATION_JSON_REFERENCE.md, real example); used only when station.json
# has no position, or no azimuth regions that point anywhere (unset, or
# the template's full circle, whose reflection zone is a ring round the
# antenna with no seaward centre).
MARCONI_LAT, MARCONI_LON = 41.8928243333, -69.9633227139
MARCONI_AZIMUTH_REGIONS = (353, 360, 0, 130)
OFFSHORE_M = 1000.0

# (id, name, local). IDs checked against NOAA's bench mark sheets and
# station pages (2026-10-20). "local": within ~30 km of Marconi, so its
# MSL - NAVD88 counts in the transfer spread; the others are long-term
# stations shown for the regional picture (and Nantucket is the control
# station Chatham's datums were reduced with). None of the local three
# is on the open Atlantic coast: Chatham is a back-barrier harbour,
# Provincetown is on Cape Cod Bay and Saquatucket (Harwich Port) on
# Nantucket Sound.
NEARBY_STATIONS = (
    ("8447435", "Chatham, Lydia Cove, MA", True),
    ("8446121", "Provincetown, MA", True),
    ("8447495", "Saquatucket Harbor, MA", True),
    ("8449130", "Nantucket Island, MA", False),
    ("8447930", "Woods Hole, MA", False),
    ("8443970", "Boston, MA", False),
)
PRIMARY_STATION = "8447435"
# A live run also lists every NOAA datum station within this distance
# that NEARBY_STATIONS leaves out (nearest first, at most MAX_DISCOVERED),
# with its datums. They are shown, not counted in the estimate or its
# spread: many are inside bays and harbours whose mean level is not the
# open coast's. One worth counting is added to NEARBY_STATIONS by hand.
LOCAL_RADIUS_KM = 30.0
MAX_DISCOVERED = 10

# Standard error of the published datums, NOAA "Tidal Datum Errors for
# East Coast of the United States" (tidesandcurrents.noaa.gov/pdf/
# Datums_error_east_coast.pdf), read 2026-10-20 through a web search.
DATUM_ERRORS_M = {"8447435": 0.012, "8446121": 0.016}

_READ_NOTE = ("read 2026-10-20 from the sheet's text as a web search returned it: "
              "the NOAA hosts could not be reached from the machine this was written on")

# Published heights above MLLW (m). Converted to NAVD88 by
# offline_record(). Superseded determinations are shown for comparison
# and never chosen as the estimate.
OFFLINE_DATUMS = {
    "8447435": {
        "name": "Chatham, Lydia Cove, MA",
        "epoch": NTDE,
        "series": "Sep 2015 - Aug 2017 (2 years), control station 8449130 Nantucket",
        "above_mllw": {"MHHW": 1.578, "MHW": 1.464, "MSL": 0.778, "MTL": 0.757,
                       "MLW": 0.050, "MLLW": 0.000, "NAVD88": 0.760},
        "source": "NOAA CO-OPS published bench mark sheet, "
                  "https://tidesandcurrents.noaa.gov/benchmarks/8447435.html (published 2020-05-20)",
        "read": _READ_NOTE + "; MSL and NAVD88 are also those of cam compare_gnssr_to_gauge.py",
    },
    "8447435/2000": {
        "name": "Chatham, Lydia Cove, MA, 2000",
        "superseded": "2003-04-21",
        "epoch": NTDE,
        "series": "3 months (sheet published 2000-06-30; the months not confirmed), "
                  "control station 8449130 Nantucket",
        "above_mllw": {"MSL": 0.771, "NAVD88": 0.816},
        "source": "NOAA CO-OPS superseded bench mark sheet, "
                  "https://tidesandcurrents.noaa.gov/benchmarks/benchmarks_old/8447435.html",
        "read": _READ_NOTE + "; MHW and MLW not captured",
    },
    "8447495": {
        "name": "Saquatucket Harbor, MA",
        "epoch": NTDE,
        "series": "May - Sep 1993 (5 months; historical station), control station 8449130 Nantucket",
        "above_mllw": {"MHHW": 1.325, "MHW": 1.220, "MSL": 0.682, "MTL": 0.652,
                       "MLW": 0.085, "MLLW": 0.000, "NAVD88": 0.788},
        "source": "NOAA CO-OPS published bench mark sheet, "
                  "https://tidesandcurrents.noaa.gov/benchmarks/8447495.html (published 2003-04-21)",
        "read": _READ_NOTE,
    },
}

# Relative sea-level trends (mm/yr, 95% interval), NOAA CO-OPS Extreme
# Water Levels station pages (tidesandcurrents.noaa.gov/est/
# est_station.shtml?stnid=<id>), read 2026-10-20 through a web search.
OFFLINE_TRENDS = {
    "8449130": (4.01, 0.31, "Nantucket Island"),
    "8447930": (3.07, 0.17, "Woods Hole"),
    "8443970": (2.94, 0.15, "Boston"),
}
DEFAULT_TREND_STATION = "8449130"     # control station of Chatham's datums
# CO-OPS publishes a monthly mean only once the month is verified, a
# month or two after it ends, so the 12 months just ended are rarely all
# there. The latest whole year within this many months is used instead.
MONTHLY_LOOKBACK = 24

# ---------------------------------------------------------------------
# CALIBRATION (co-located gauge; docs/INDEPENDENT_DATUM.md)
# ---------------------------------------------------------------------

# Daily-mean GNSS-IR minus gauge scatter in the literature: 2.3 cm
# (Larson et al. 2013, Kachemak Bay), ~2 cm (Larson et al. 2017, Friday
# Harbor, 10 years), 2.7-2.8 cm (Holden & Larson 2021); the spectral
# and inverse methods agree for daily means (Purnell et al. 2020).
# These are sheltered sites. At an exposed one the retrievals scatter
# far more (Sepulveda et al. 2023, Scripps Pier: 9-18 cm RMS), and
# Marconi's reflection zone (about 70-220 m out at 5-15 deg) lies
# around the surf zone, where wave setup moves the mean level with the
# wave height, storm after storm. --daily-sigma sets a larger value;
# the days needed grow as its square.
DAILY_SIGMA_M = 0.025
DAILY_SIGMA_SOURCE = "sheltered-site literature value"
# NOAA computes datums for a short-term station by simultaneous
# comparison with a control station from at least a month of data
# (Computational Techniques for Tidal Datums Handbook, NOAA Special
# Publication NOS CO-OPS 2, 2003); a month also spans two spring-neap
# cycles, over which tide-range-dependent GNSS-IR errors average out.
MIN_DEPLOYMENT_DAYS = 29
# The pressure logger's own level error over a deployment (1 sigma):
# the barometric correction (0.5-1 hPa, 5-10 mm), the seawater density
# that turns pressure into depth (0.1-0.3 % of a few metres, 3-10 mm)
# and sensor drift; 1-2 cm all told. 0.01 m is the better end: a
# barometer beside it, the density measured, drift checked before and
# after.
GAUGE_SIGMA_M = 0.01
# How the logger's zero is put on NAVD88 (1 sigma). A logger moored
# outside the surf zone is never dry, so it cannot be levelled or
# occupied: its zero comes from the water surface beside it, read
# against NAVD88 at slack water in calm weather (a staff levelled from
# an NGS mark, or an RTK pole) while it records, about 2.5 cm a session,
# averaged over the sessions. An intertidal logger in a pipe can be
# levelled from a mark at spring low water, but reads only when covered
# and is inside the surf at times: calm days only.
TIE_SESSION_M = 0.025
TIE_SESSIONS = 8
TIE_OPTIONS = (
    ("pipe mount levelled from an NGS mark", 0.005),
    (f"{TIE_SESSIONS} calm slack-water staff/RTK sessions",
     round(TIE_SESSION_M / math.sqrt(TIE_SESSIONS), 4)),
    ("1 calm slack-water staff/RTK session", TIE_SESSION_M),
)
TARGETS_M = (0.01, 0.02, 0.03)


# ---------------------------------------------------------------------
# NETWORK
# ---------------------------------------------------------------------

class Unavailable(Exception):
    """A source could not be read; the message says why."""


def fetch_json(url: str, timeout: float = FETCH_TIMEOUT_S):
    """The one place this tool touches the network (tests replace it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "gnssir-station-datum"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


class Net:
    """Network access for one run. A host that cannot be reached is
    tried once, not once per request: a station with no route to NOAA
    would otherwise wait out a timeout for every source. An HTTP error
    means the host answered, so it is not marked down."""

    def __init__(self, fetch=None, online: bool = True):
        self.fetch = fetch or fetch_json
        self.online = online
        self.down = {}

    def get(self, url: str):
        host = urllib.parse.urlsplit(url).netloc
        if not self.online:
            raise Unavailable("not tried (--offline)")
        if host in self.down:
            raise Unavailable(f"not tried: {host} was unreachable")
        try:
            return self.fetch(url)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"HTTP {exc.code} from {host}")
        except (urllib.error.URLError, OSError) as exc:
            self.down[host] = f"{host} unreachable ({getattr(exc, 'reason', exc)})"
            raise Unavailable(self.down[host])
        except http.client.HTTPException as exc:      # e.g. a body cut short by a proxy
            raise Unavailable(f"{host} answer broken off ({exc!r})")
        except ValueError as exc:
            raise Unavailable(f"{host} did not return JSON ({exc})")


# ---------------------------------------------------------------------
# PARSING (pure; the tests feed these recorded/mock responses)
# ---------------------------------------------------------------------

def _api_error(obj) -> str | None:
    """The message of a CO-OPS error answer ({"error": {"message": ...}})."""
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        return str(err.get("message", err)) if isinstance(err, dict) else str(err)
    return None


def parse_coops_datums(obj) -> dict:
    """Tidal datums on NAVD88 (m) from a CO-OPS datums answer.

    Reads the metadata API's {"datums": [{"name", "value"}]} and the
    data API's {"datums": [{"n", "v"}]} alike. The heights are above an
    arbitrary station zero; only their differences from the NAVD88
    entry are kept, so the zero does not matter. Raises ValueError for
    an error answer or a station without a NAVD88 tie."""
    msg = _api_error(obj)
    if msg:
        raise ValueError(msg)
    if not isinstance(obj, dict) or not obj.get("datums"):
        raise ValueError("no datums in the answer")
    vals = {}
    for row in obj.get("datums") or []:
        name = row.get("name", row.get("n"))
        value = row.get("value", row.get("v"))
        try:
            vals[str(name).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    scale = 0.3048 if str(obj.get("units", "meters")).lower().startswith(("feet", "english")) else 1.0
    navd = vals.get("NAVD88")
    if navd is None:
        raise ValueError("no NAVD88 height published for this station")
    datums = {k: round((vals[k] - navd) * scale, 4) for k in DATUM_NAMES if k in vals}
    if "MSL" not in datums:
        raise ValueError("no MSL published for this station")
    period = obj.get("DatumAnalysisPeriod") or []
    return {
        "datums": datums,
        "epoch": str(obj.get("epoch") or ""),
        "series": "; ".join(str(p) for p in period) if isinstance(period, list) else str(period),
        "control": str(obj.get("ctrlStation") or ""),
        "accepted": str(obj.get("accepted") or ""),
    }


def parse_station_meta(obj) -> dict:
    """{"name", "lat", "lon"} from a CO-OPS station metadata answer."""
    try:
        st = obj["stations"][0]
        return {"name": str(st.get("name", "")), "lat": float(st["lat"]), "lon": float(st["lng"])}
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def parse_station_list(obj) -> list:
    """[{"id", "name", "lat", "lon"}] from a CO-OPS stations.json
    answer; entries without an id or a position are skipped."""
    out = []
    stations = obj.get("stations") if isinstance(obj, dict) else None
    for st in stations or []:
        try:
            out.append({"id": str(st["id"]), "name": str(st.get("name", "")),
                        "lat": float(st["lat"]), "lon": float(st["lng"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_monthly_means(obj) -> list:
    """[{"year", "month", "MSL", "MHW", "MLW"}] from a monthly_mean
    answer; a blank value becomes None. Raises ValueError for an error
    answer."""
    msg = _api_error(obj)
    if msg:
        raise ValueError(msg)
    rows = []
    for r in (obj or {}).get("data") or []:
        try:
            row = {"year": int(r["year"]), "month": int(r["month"])}
        except (KeyError, TypeError, ValueError):
            continue
        for k in SHOWN:
            try:
                row[k] = float(r.get(k))
            except (TypeError, ValueError):
                row[k] = None
        rows.append(row)
    return rows


def parse_vdatum(obj):
    """(height on NAVD88, "") from a VDatum /convert answer, or (None,
    why). VDatum marks a point outside its tidal grids with t_z
    -999999, as a number or a string."""
    if not isinstance(obj, dict):
        return None, "not a VDatum answer"
    try:
        z = float(obj.get("t_z"))
    except (TypeError, ValueError):
        why = obj.get("message") or obj.get("errorCode") or "no t_z in the answer"
        return None, str(why)
    if not math.isfinite(z) or abs(z) > 1000:
        return None, "no tidal datum here (t_z -999999: outside VDatum's tidal grids)"
    return z, ""


# ---------------------------------------------------------------------
# GEOMETRY AND TIME
# ---------------------------------------------------------------------

def mean_azimuth(regions) -> float | None:
    """Mean direction (deg) of gnssrefl azimuth regions given as the
    flat list station.json uses ([353, 360, 0, 130] is 353-360 and
    0-130). A vector mean, so a window across north averages right.
    None when the regions point nowhere in particular -- unset, or
    (nearly) the full circle -- since a ring has no centre."""
    vals = [float(v) for v in (regions or [0, 360])]
    x = y = 0.0
    n = 0
    for lo, hi in zip(vals[0::2], vals[1::2]):
        if hi < lo:
            hi += 360.0
        a = lo
        while a <= hi:
            x += math.cos(math.radians(a))
            y += math.sin(math.radians(a))
            n += 1
            a += 1.0
    if n == 0 or math.hypot(x, y) / n < 0.1:
        return None
    return round(math.degrees(math.atan2(y, x)), 6) % 360.0


def offset_point(lat: float, lon: float, azimuth_deg: float, distance_m: float):
    """(lat, lon) distance_m from a point along an azimuth (flat Earth:
    the distances here are a kilometre or less)."""
    r = 6371000.0
    dlat = distance_m * math.cos(math.radians(azimuth_deg)) / r
    dlon = distance_m * math.sin(math.radians(azimuth_deg)) / (r * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def distance_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance (km)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def query_points(cfg: dict):
    """[(label, lat, lon)]: the antenna, the centre of the reflection
    zone, and OFFSHORE_M out along the same azimuth.

    The reflection point of an arc at elevation e is RH / tan(e) from
    the antenna (Roesler & Larson 2018); RH is taken as the antenna's
    NAVD88 height (water near 0 m), e as the middle of the elevation
    mask and the azimuth as the mean of the azimuth regions -- the
    documented Marconi regions when station.json's point nowhere in
    particular (MARCONI_AZIMUTH_REGIONS). The published water level
    uses only arcs in process_and_plot.sh's 35-125 deg subdaily window
    (mean about 80 deg); at ~110 m from the antenna the two centres
    are a few tens of metres apart, well inside one VDatum grid cell."""
    lat = cfg.get("latitude")
    lon = cfg.get("longitude")
    if lat is None or lon is None:
        lat, lon = MARCONI_LAT, MARCONI_LON
    lat, lon = float(lat), float(lon)
    e1 = float(cfg.get("gnssrefl_elevation_min") or 5.0)
    e2 = float(cfg.get("gnssrefl_elevation_max") or 15.0)
    rh = navd88_hortho(cfg) or NAVD88_HORTHO_M
    az = mean_azimuth(cfg.get("gnssrefl_azimuth_regions"))
    if az is None:
        az = mean_azimuth(MARCONI_AZIMUTH_REGIONS)
    zone = rh / math.tan(math.radians((e1 + e2) / 2))
    return [("antenna", lat, lon),
            (f"reflection zone ({zone:.0f} m at {az:.0f} deg)",)
            + offset_point(lat, lon, az, zone),
            (f"{OFFSHORE_M / 1000:.0f} km offshore at {az:.0f} deg",)
            + offset_point(lat, lon, az, OFFSHORE_M)]


def epoch_midpoint(epoch: str) -> float | None:
    """Middle of a tidal epoch as a decimal year: "1983-2001" -> 1992.5
    (1 Jan 1983 to 31 Dec 2001)."""
    m = re.match(r"\s*(\d{4})\s*-\s*(\d{4})", epoch or "")
    return (int(m.group(1)) + int(m.group(2)) + 1) / 2.0 if m else None


def decimal_year(t: datetime) -> float:
    start = datetime(t.year, 1, 1, tzinfo=t.tzinfo)
    end = datetime(t.year + 1, 1, 1, tzinfo=t.tzinfo)
    return t.year + (t - start).total_seconds() / (end - start).total_seconds()


def _months_back(year: int, month: int, n: int):
    """The n (year, month) pairs ending at year/month, oldest first."""
    out = []
    for _ in range(n):
        out.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out[::-1]


def last_whole_month(now: datetime):
    """(year, month) of the last month that ended before `now`."""
    return (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)


def annual_anomaly(rows, now: datetime, lookback: int = MONTHLY_LOOKBACK):
    """(mean, first, last): the mean monthly MSL (departure from the
    epoch mean, m) over the latest 12 consecutive whole months before
    `now` that all have a value, searched back `lookback` months; first
    and last are (year, month). None if there is no such year: a
    partial year would carry part of the seasonal cycle.

    Searched back rather than insisting on the 12 months just ended,
    because monthly means are published only once verified, a month or
    two late (MONTHLY_LOOKBACK). A year that ended up to a year ago
    misses about 4 mm of sea-level rise, well inside the budget."""
    end = last_whole_month(now)
    months = _months_back(end[0], end[1], max(lookback, 12))
    have = {(r["year"], r["month"]): r.get("MSL") for r in rows}
    for i in range(len(months) - 12, -1, -1):
        span = months[i:i + 12]
        vals = [have.get(m) for m in span]
        if all(v is not None for v in vals):
            return sum(vals) / len(vals), span[0], span[-1]
    return None


def window_anomaly(rows, start: date, end: date):
    """Mean monthly MSL departure over start..end (inclusive), each
    month weighted by its days inside the window; None if any of those
    months is missing. For tide_model_navd88_offset, which is the mean
    level over one comparison window, not over a year."""
    have = {(r["year"], r["month"]): r.get("MSL") for r in rows}
    total = weight = 0.0
    d = start
    while d <= end:
        v = have.get((d.year, d.month))
        if v is None:
            return None
        total += v
        weight += 1.0
        d += timedelta(days=1)
    return total / weight if weight else None


# ---------------------------------------------------------------------
# SOURCES -> RECORDS
# ---------------------------------------------------------------------

def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def offline_record(key: str) -> dict:
    """An OFFLINE_DATUMS entry as a source record on NAVD88."""
    o = OFFLINE_DATUMS[key]
    navd = o["above_mllw"]["NAVD88"]
    station = key.split("/")[0]
    return {
        "key": f"coops:{key}", "kind": "coops", "station": station,
        "label": f"CO-OPS {station} {o['name']}",
        "datums": {k: round(v - navd, 4) for k, v in o["above_mllw"].items() if k in DATUM_NAMES},
        "epoch": o["epoch"], "series": o.get("series", ""),
        "local": any(s == station and loc for s, _, loc in NEARBY_STATIONS),
        "superseded": o.get("superseded") or False,
        "datum_error": None if o.get("superseded") else DATUM_ERRORS_M.get(station),
        "provenance": f"offline: {o['source']}; {o['read']}",
        "distance_km": None,
    }


def coops_record(net: Net, station: str, name: str, local: bool, cache: dict,
                 now: datetime, home, meta: dict | None = None) -> tuple:
    """(record or None, what happened) for one CO-OPS station: live,
    else the cache, else OFFLINE_DATUMS. `meta` ({"name", "lat",
    "lon"}) saves the station metadata request when already known."""
    key = f"coops:{station}"
    try:
        d = parse_coops_datums(net.get(COOPS_DATUMS_API.format(station=station)))
        rec = {"key": key, "kind": "coops", "station": station, "label": f"CO-OPS {station} {name}",
               "datums": d["datums"], "epoch": d["epoch"] or NTDE,
               "series": d["series"] + (f", control station {d['control']}" if d["control"] else ""),
               "local": local, "superseded": False, "datum_error": DATUM_ERRORS_M.get(station),
               "distance_km": None, "retrieved": _stamp(now)}
        if meta is None:
            try:
                meta = parse_station_meta(net.get(COOPS_STATION_API.format(station=station)))
            except Unavailable:
                meta = {}
        if meta.get("name"):
            rec["label"] = f"CO-OPS {station} {meta['name']}"
        if "lat" in meta:
            rec["lat"], rec["lon"] = meta["lat"], meta["lon"]
            rec["distance_km"] = round(distance_km(home[0], home[1], meta["lat"], meta["lon"]), 1)
        cache[key] = dict(rec)
        rec["provenance"] = f"live, {rec['retrieved']}"
        return rec, "live"
    except (Unavailable, ValueError) as exc:
        why = str(exc)
    if key in cache:
        rec = dict(cache[key])
        rec["provenance"] = f"cache, retrieved {rec.get('retrieved', '?')}"
        return rec, f"failed ({why}); cache used"
    if station in OFFLINE_DATUMS:
        return offline_record(station), f"failed ({why}); offline values used"
    return None, f"failed ({why}); no offline values"


def nearby_unlisted(net: Net, home, listed, radius_km: float = LOCAL_RADIUS_KM,
                    limit: int = MAX_DISCOVERED) -> tuple:
    """([{"id", "name", "lat", "lon", "distance_km"}], what happened):
    NOAA stations with published datums within radius_km of the antenna
    that `listed` (station IDs) leaves out, nearest first.

    This is how a live run checks NEARBY_STATIONS against NOAA's own
    list: a closer open-coast station, or a renumbered one, shows here.
    The list itself is not cached: it only informs."""
    try:
        stations = parse_station_list(net.get(COOPS_DATUM_STATIONS_API))
    except Unavailable as exc:
        return [], f"failed ({exc})"
    found = []
    for st in stations:
        if st["id"] in listed:
            continue
        d = distance_km(home[0], home[1], st["lat"], st["lon"])
        if d <= radius_km:
            found.append(dict(st, distance_km=round(d, 1)))
    found.sort(key=lambda s: s["distance_km"])
    how = f"live: {len(found)} not in NEARBY_STATIONS within {radius_km:.0f} km"
    if len(found) > limit:
        how += f", nearest {limit} shown"
    return found[:limit], how


def vdatum_record(net: Net, label: str, lat: float, lon: float, cache: dict, now: datetime,
                  names=tuple(VDATUM_FRAMES)) -> tuple:
    """(record or None, what happened) for one VDatum point, asking for
    the datums in `names` (MSL first)."""
    key = f"vdatum:{lat:.5f},{lon:.5f}"
    datums, why = {}, ""
    for name in names:
        frame = VDATUM_FRAMES[name]
        try:
            z, bad = parse_vdatum(net.get(VDATUM_API.format(lat=lat, lon=lon, frame=frame)))
        except Unavailable as exc:
            why = str(exc)
            break
        if z is None:
            why = bad
            if name == "MSL":       # no tidal grid here: the other datums will not answer either
                break
            continue
        datums[name] = round(z, 4)
    if "MSL" in datums:
        # VDatum's tidal surfaces are taken to be on the same epoch as
        # NOAA's published datums; check this after NOAA's next epoch
        # update, when the two may not change together.
        rec = {"key": key, "kind": "vdatum", "station": "", "label": f"VDatum {label}",
               "lat": lat, "lon": lon, "datums": datums, "epoch": NTDE, "series": "",
               "local": True, "superseded": False, "datum_error": None, "distance_km": None,
               "retrieved": _stamp(now)}
        cache[key] = dict(rec)
        rec["provenance"] = f"live, {rec['retrieved']}"
        return rec, "live"
    if key in cache:
        rec = dict(cache[key])
        rec["provenance"] = f"cache, retrieved {rec.get('retrieved', '?')}"
        return rec, f"failed ({why}); cache used"
    return None, f"failed ({why})"


def vdatum_check(net: Net, sources, cache: dict, now: datetime) -> tuple:
    """([{"station", "label", "vdatum", "published", "residual"}],
    [(what, how)]): VDatum's MSL - NAVD88 at each local CO-OPS station
    whose position is known, minus the station's published value.

    VDatum's own skill on this coast, measured directly: its tidal and
    NAVD88 surfaces are built from these same stations, so a residual
    here is the model's error near the site (and the station's own
    datum error with it). Its RMS is the model term of the budget when
    VDatum is the estimate."""
    out, tried = [], []
    for s in sources:
        if (s["kind"] != "coops" or not s.get("local") or s.get("superseded")
                or s.get("discovered") or "lat" not in s):
            continue
        rec, how = vdatum_record(net, f"at CO-OPS {s['station']}", s["lat"], s["lon"], cache, now,
                                 names=("MSL",))
        if rec and rec["epoch"] != s["epoch"]:
            how += f"; not compared: the station is on the {s['epoch']} epoch, VDatum {rec['epoch']}"
            rec = None
        tried.append((f"VDatum at CO-OPS {s['station']}", how))
        if rec:
            out.append({"station": s["station"], "label": s["label"],
                        "vdatum": rec["datums"]["MSL"], "published": s["datums"]["MSL"],
                        "residual": round(rec["datums"]["MSL"] - s["datums"]["MSL"], 4)})
    return out, tried


def monthly_rows(net: Net, station: str, begin: date, end: date, cache: dict) -> tuple:
    """(monthly mean rows, what happened), merged into the cache."""
    key = f"monthly:{station}"
    cached = {(r["year"], r["month"]): r for r in cache.get(key, [])}
    try:
        rows = parse_monthly_means(net.get(COOPS_MONTHLY_API.format(
            station=station, begin=begin.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))))
        how = "live"
    except (Unavailable, ValueError) as exc:
        rows, how = [], f"failed ({exc})" + ("; cache used" if cached else "")
    for r in rows:
        cached[(r["year"], r["month"])] = r
    if cached:
        cache[key] = [cached[k] for k in sorted(cached)]
    return list(cached.values()), how


def sea_level_since_epoch(net: Net, station: str, epoch: str, now: datetime, cache: dict,
                          trend_station: str = DEFAULT_TREND_STATION) -> dict:
    """How far mean sea level has risen since the middle of the tidal
    epoch: {"value", "sigma", "method", "how"} (m).

    Observed when it can be: the mean of `station`'s monthly mean sea
    levels over the latest whole year published (annual_anomaly), each
    a departure from the epoch mean. This is the recent year's sea
    level, its year-to-year wobble included, which is what the
    present-day zero should be. Otherwise a linear trend:
    OFFLINE_TRENDS[trend_station] times the years since the epoch's
    middle, +/- half the spread the long-term stations' trends give."""
    end_month = date(now.year, now.month, 1) - timedelta(days=1)
    first = _months_back(end_month.year, end_month.month, MONTHLY_LOOKBACK)[0]
    begin = date(first[0], first[1], 1)
    rows, how = monthly_rows(net, station, begin, end_month, cache)
    found = annual_anomaly(rows, now)
    if found is not None:
        anomaly, (y1, m1), (y2, m2) = found
        return {"value": round(anomaly, 4), "sigma": None, "how": how, "epoch": epoch,
                "method": f"observed: mean of {station}'s monthly mean sea level, "
                          f"{y1}-{m1:02d} to {y2}-{m2:02d}, against the {epoch} mean"}
    mid = epoch_midpoint(epoch) or epoch_midpoint(NTDE)
    years = decimal_year(now) - mid
    rate = OFFLINE_TRENDS[trend_station][0]
    rates = [t[0] for t in OFFLINE_TRENDS.values()]
    return {"value": round(rate / 1000.0 * years, 4),
            "sigma": round((max(rates) - min(rates)) / 2000.0 * years, 4),
            "low": round(min(rates) / 1000.0 * years, 4),
            "high": round(max(rates) / 1000.0 * years, 4), "epoch": epoch,
            "how": how + "; linear trend used" if how != "live" else
                   f"live, but no 12 whole months in the last {MONTHLY_LOOKBACK}; linear trend used",
            "method": (f"trend: {rate:.2f} mm/yr ({OFFLINE_TRENDS[trend_station][2]}, NOAA) x "
                       f"{years:.1f} yr since {mid:.1f}; "
                       + ", ".join(f"{n} {r:.2f}" for r, _, n in OFFLINE_TRENDS.values())
                       + " mm/yr give the range; a linear trend is a lower bound, the rise "
                         "having been faster since the 1990s, and one year can sit a few cm off it")}


# ---------------------------------------------------------------------
# ESTIMATE, COMPARISON, CALIBRATION
# ---------------------------------------------------------------------

def _half_range(vals):
    return (max(vals) - min(vals)) / 2.0 if len(vals) >= 2 else None


def best_estimate(sources, adjustment: dict, check=None) -> dict | None:
    """The present-day datums at Marconi from the best source, with an
    uncertainty budget for MSL (m, 1 sigma; a component that is not
    known is left out and named).

    Choice: VDatum at the reflection zone, then offshore, then at the
    antenna (it models the open coast at the site), then Chatham, then
    any other local station, then any station. Superseded
    determinations, and stations found by nearby_unlisted, are never
    chosen.

    Budget for a VDatum pick: VDatum's own error, the RMS of `check`
    (vdatum_check: VDatum minus the published MSL at the local
    stations); the epoch correction's uncertainty; and half the spread
    of MSL - NAVD88 over the VDatum points at the site. Its MHW and MLW
    are Marconi's.

    Budget for a station pick: the published datum error; the epoch
    correction's uncertainty; and the transfer from the station to the
    site, half the spread of MSL - NAVD88 over the local stations. Only
    MSL is carried to Marconi ("present"): a harbour's MHW and MLW are
    its own tide, not the open coast's, and are given apart
    ("station_tide") with no uncertainty.

    Sources on another tidal epoch than the pick are left out of the
    spread, and a pick on another epoch than the sea-level correction
    is warned of ("warnings")."""
    usable = [s for s in sources if s and not s.get("superseded") and not s.get("discovered")
              and "MSL" in s["datums"]]
    if not usable:
        return None
    vd = [s for s in usable if s["kind"] == "vdatum"]
    vd_order = sorted(vd, key=lambda s: (0 if "reflection" in s["label"] else
                                         1 if "offshore" in s["label"] else 2))
    primary = [s for s in usable if s.get("station") == PRIMARY_STATION]
    local = [s for s in usable if s["kind"] == "coops" and s.get("local")]
    pick = (vd_order + primary + local + usable)[0]
    adj = adjustment["value"]
    warnings = []
    if adjustment.get("epoch") and pick.get("epoch") and pick["epoch"] != adjustment["epoch"]:
        warnings.append(f"{pick['label']} is on the {pick['epoch']} epoch, the sea-level correction "
                        f"on {adjustment['epoch']}: present MSL is off by the change between them")
    same = [s for s in usable if s.get("local") and s.get("epoch") == pick.get("epoch")]
    other = [s["label"] for s in usable if s.get("local") and s.get("epoch") != pick.get("epoch")]
    if other:
        warnings.append(f"left out of the spread, not on the {pick.get('epoch')} epoch: "
                        + ", ".join(other))
    station_tide = None
    if pick["kind"] == "vdatum":
        spread = [s["datums"]["MSL"] for s in same if s["kind"] == "vdatum"]
        res = [c["residual"] for c in check or []]
        present = {k: round(v + adj, 4) for k, v in pick["datums"].items()}
        parts = {"VDatum model": (math.sqrt(sum(r * r for r in res) / len(res)) if res else None),
                 "epoch correction": adjustment.get("sigma"),
                 "spread over the VDatum points": _half_range(spread)}
    else:
        spread = [s["datums"]["MSL"] for s in same]
        present = {"MSL": round(pick["datums"]["MSL"] + adj, 4)}
        station_tide = {k: round(pick["datums"][k] + adj, 4) for k in ("MHW", "MLW")
                        if k in pick["datums"]} or None
        parts = {"published datum": pick.get("datum_error"),
                 "epoch correction": adjustment.get("sigma"),
                 "transfer to Marconi": _half_range(spread)}
    known = [v for v in parts.values() if v is not None]
    return {"source": pick["label"], "kind": pick["kind"], "epoch_msl": pick["datums"]["MSL"],
            "present": present, "station_tide": station_tide,
            "uncertainty": round(math.sqrt(sum(v * v for v in known)), 4) if known else None,
            "parts": {k: (round(v, 4) if v is not None else None) for k, v in parts.items()},
            "spread_n": len(spread), "check_n": len(check or []) if pick["kind"] == "vdatum" else None,
            "warnings": warnings}


def compare_configured(cfg: dict, msl_now: float, msl_window: float | None = None,
                       window_given: bool = False) -> list:
    """[{"field", "value", "reference", "bias", "note"}] for the two
    station.json offsets, each on NAVD88 minus the independent height
    it stands for.

    water_level_msl_offset is a long-term mean sea level (the zero of
    the public plot), so it is set against present mean sea level.
    tide_model_navd88_offset is the mean water level over the overlap
    it was measured in (tide-model heights are zero-mean), so it is set
    against mean sea level over --window when that is known, which
    takes the season out of the comparison. That holds only for a model
    run without the seasonal SA/SSA constituents; with them, --window
    would count the season twice and present MSL is the reference."""
    out = []
    v = cfg.get("water_level_msl_offset")
    if v is None:
        out.append({"field": "water_level_msl_offset", "value": None, "reference": msl_now,
                    "bias": None, "note": "not set: the public plot is drawn on NAVD88"})
    else:
        nav = float(v) + config_to_navd88(cfg)
        out.append({"field": "water_level_msl_offset", "value": round(nav, 4), "reference": msl_now,
                    "bias": round(nav - msl_now, 4), "note": "against present mean sea level"})
    tide = tide_model_to_navd88(cfg)
    ref = msl_window if msl_window is not None else msl_now
    note = ("against mean sea level over --window" if msl_window is not None else
            "against present mean sea level: no monthly means for --window, so the season "
            "of its overlap (a few cm) is not taken out" if window_given else
            "against present mean sea level: the season of its overlap (a few cm) is not "
            "taken out; --window does that")
    if cfg.get("tide_model_navd88_offset") is None:
        note += "; station.json does not set it, default used"
    out.append({"field": "tide_model_navd88_offset", "value": round(tide, 4), "reference": ref,
                "bias": round(tide - ref, 4), "note": note})
    return out


def calibration_plan(target: float, sigma_tie: float, sigma_daily: float = DAILY_SIGMA_M,
                     min_days: int = MIN_DEPLOYMENT_DAYS,
                     sigma_gauge: float = GAUGE_SIGMA_M) -> dict:
    """Days of a co-located gauge needed to measure the GNSS-IR bias to
    `target` (m, 1 sigma).

    The bias is the mean of daily-mean differences, GNSS-IR minus
    gauge, so its error is sigma_daily / sqrt(n) on top of the gauge's
    tie to NAVD88 and its own level error (GAUGE_SIGMA_M), neither of
    which averages down: n = sigma_daily^2 / (target^2 - sigma_tie^2 -
    sigma_gauge^2). A gauge whose tie and own error alone reach the
    target can never get there. Never less
    than min_days (a month: NOAA's short-term datum practice, and two
    spring-neap cycles). Daily errors that persist with the weather
    make the real need larger, so the month is a floor, not a promise."""
    budget = target ** 2 - sigma_tie ** 2 - sigma_gauge ** 2
    if budget <= 0:
        return {"feasible": False, "days": None, "statistical_days": None}
    n = max(1, math.ceil(sigma_daily ** 2 / budget - 1e-9))
    return {"feasible": True, "statistical_days": n, "days": max(n, min_days)}


# ---------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------

def build_report(cfg: dict, net: Net, cache: dict, now: datetime,
                 stations=NEARBY_STATIONS, window=None, vdatum: bool = True,
                 discover: bool = True, daily_sigma: float | None = None) -> dict:
    """Everything the tool prints, as a dict (also what --json writes)."""
    points = query_points(cfg)
    home = (points[0][1], points[0][2])
    tried, sources = [], []
    for station, name, local in stations:
        rec, how = coops_record(net, station, name, local, cache, now, home)
        tried.append((f"CO-OPS datums {station}", how))
        if rec:
            sources.append(rec)
    if discover:
        found, how = nearby_unlisted(net, home, {s for s, _, _ in stations})
        tried.append((f"CO-OPS datum stations within {LOCAL_RADIUS_KM:.0f} km", how))
        for st in found:
            rec, how = coops_record(net, st["id"], st["name"], False, cache, now, home, meta=st)
            tried.append((f"CO-OPS datums {st['id']} (not listed)", how))
            if rec:
                rec["discovered"] = True
                sources.append(rec)
    check = []
    if vdatum:
        for label, lat, lon in points:
            rec, how = vdatum_record(net, label, lat, lon, cache, now)
            tried.append((f"VDatum {label}", how))
            if rec:
                sources.append(rec)
        if any(s["kind"] == "vdatum" for s in sources):
            check, how = vdatum_check(net, sources, cache, now)
            tried += how
    for key in OFFLINE_DATUMS:
        if OFFLINE_DATUMS[key].get("superseded"):
            sources.append(offline_record(key))
    epoch = next((s["epoch"] for s in sources if s.get("station") == PRIMARY_STATION
                  and not s.get("superseded")), NTDE)
    adjustment = sea_level_since_epoch(net, PRIMARY_STATION, epoch, now, cache)
    tried.append((f"CO-OPS monthly means {PRIMARY_STATION}", adjustment["how"]))
    best = best_estimate(sources, adjustment, check)
    msl_window = None
    if window and best:
        begin = date(window[0].year, window[0].month, 1)
        last = (date(window[1].year + (window[1].month == 12), window[1].month % 12 + 1, 1)
                - timedelta(days=1))                        # end of END's month
        rows, how = monthly_rows(net, PRIMARY_STATION, begin, last, cache)
        tried.append((f"CO-OPS monthly means {PRIMARY_STATION} for --window", how))
        anomaly = window_anomaly(rows, window[0], window[1])
        if anomaly is not None:
            msl_window = round(best["epoch_msl"] + anomaly, 4)
    comparisons = (compare_configured(cfg, best["present"]["MSL"], msl_window, bool(window))
                   if best else [])
    sigma = daily_sigma if daily_sigma is not None else DAILY_SIGMA_M
    plans = [{"tie": label, "sigma_tie": tie,
              "targets": {f"{t:.2f}": calibration_plan(t, tie, sigma) for t in TARGETS_M}}
             for label, tie in TIE_OPTIONS]
    return {"generated": _stamp(now), "points": points, "sources": sources, "tried": tried,
            "adjustment": adjustment, "best": best, "msl_window": msl_window,
            "window": [str(window[0]), str(window[1])] if window else None,
            "comparisons": comparisons, "vdatum_check": check, "calibration": plans,
            "daily_sigma": {"value": sigma, "source": DAILY_SIGMA_SOURCE if daily_sigma is None
                            else "--daily-sigma"},
            "hortho_navd88": navd88_hortho(cfg)}


def _f(v, width=7):
    return f"{v:+{width}.3f}" if isinstance(v, (int, float)) else " " * (width - 1) + "-"


def _wrap(text: str, indent: int = 6) -> list:
    return textwrap.wrap(text, width=96, initial_indent=" " * indent,
                         subsequent_indent=" " * (indent + 2))


def format_report(rep: dict) -> str:
    """The printed report."""
    L = []
    bar = "=" * 72
    L += [bar, "INDEPENDENT DATUM: mean sea level on NAVD88 at Marconi, not from GNSS-IR",
          "(report only; station.json and every product are left as they are)", bar]
    lat, lon = rep["points"][0][1], rep["points"][0][2]
    L.append(f"Antenna {lat:.5f}, {lon:.5f}; Hortho "
             + (f"{rep['hortho_navd88']:.3f} m NAVD88" if rep["hortho_navd88"] else "not set"))
    L += ["", "Sources tried:"]
    L += [f"  {what:46s} {how}" for what, how in rep["tried"]]
    L += ["", "Tidal datums on NAVD88 (m), as published for the tidal epoch:",
          f"  {'source':48s} {'MSL':>7s} {'MHW':>7s} {'MLW':>7s}"]
    for s in rep["sources"]:
        d = s["datums"]
        dist = f", {s['distance_km']:.0f} km" if s.get("distance_km") is not None else ""
        L.append(f"  {(s['label'] + dist)[:48]:48s} {_f(d.get('MSL'))} {_f(d.get('MHW'))} "
                 f"{_f(d.get('MLW'))}")
        L += _wrap(f"epoch {s.get('epoch') or '?'}{'; ' + s['series'] if s.get('series') else ''}"
                   + (f"; SUPERSEDED {s['superseded']}, shown for comparison only"
                      if s.get("superseded") else "")
                   + ("; not in NEARBY_STATIONS: shown, not counted" if s.get("discovered") else ""))
        L += _wrap(s["provenance"])
    a = rep["adjustment"]
    L += ["", f"Sea level since the epoch: {a['value']:+.3f} m"
          + (f" (range {a['low']:+.3f} to {a['high']:+.3f})" if "low" in a else "")]
    L += _wrap(a["method"], 2)
    b = rep["best"]
    if not b:
        L += ["", "No source gave a mean sea level: nothing to compare with."]
        return "\n".join(L)
    p = b["present"]
    L += ["", f"Present-day {'datums' if b['kind'] == 'vdatum' else 'mean sea level'} at Marconi, "
              f"from {b['source']}:",
          f"  MSL {p['MSL']:+.3f} m NAVD88"
          + (f"   MHW {p['MHW']:+.3f}" if "MHW" in p else "")
          + (f"   MLW {p['MLW']:+.3f}" if "MLW" in p else "")]
    t = b.get("station_tide")
    if t:
        L += _wrap("(the station's own " + "   ".join(f"{k} {v:+.3f}" for k, v in t.items())
                   + ", corrected the same way, are its harbour's tide: its range does not "
                   "transfer to the open coast at Marconi, and no uncertainty is given for them)", 2)
    parts = ", ".join(f"{k} {v:.3f}" if v is not None else f"{k} unknown" for k, v in b["parts"].items())
    L += _wrap("uncertainty of MSL " + (f"+/-{b['uncertainty']:.3f} m" if b["uncertainty"] is not None
                                        else "unknown") + f" (1 sigma: {parts})", 2)
    if b["kind"] == "vdatum":
        L += _wrap(f"(VDatum model: RMS of VDatum minus the published MSL at {b['check_n']} local "
                   "CO-OPS station(s); spread: over the VDatum points at the site)", 2)
    if b["parts"].get("transfer to Marconi", 0) is None:
        L.append("  (one local source only: the transfer from it to the open coast at Marconi "
                 "is not measured -- a few cm for Chatham's harbour)")
    for w in b.get("warnings") or []:
        L += _wrap(f"WARNING: {w}", 2)
    if rep["msl_window"] is not None:
        L.append(f"  MSL over --window {rep['window'][0]}..{rep['window'][1]}: "
                 f"{rep['msl_window']:+.3f} m NAVD88")
    L += ["", "Configured offsets (station.json, on NAVD88) minus the independent height:"]
    for c in rep["comparisons"]:
        L.append(f"  {c['field']:26s} {_f(c['value'])}  minus {_f(c['reference'])}  = "
                 f"{_f(c['bias'])} m")
        L += _wrap(f"({c['note']})")
    u = b["uncertainty"]
    biases = [c for c in rep["comparisons"] if c["bias"] is not None]
    for c in biases:
        d = c["bias"]
        verdict = ("within the independent height's own uncertainty" if u is not None and abs(d) <= u
                   else f"{'above' if d > 0 else 'below'} it by more than its uncertainty"
                   if u is not None else f"{'above' if d > 0 else 'below'} it")
        L.append(f"  {c['field']}: {verdict}.")
    missing = [k for k, v in b["parts"].items() if v is None]
    if u is not None and missing:
        L += _wrap(f"(that uncertainty leaves out what is not known: {', '.join(missing)})", 2)
    if u is not None and biases and all(abs(c["bias"]) <= u for c in biases):
        L += _wrap(f"So no GNSS-IR absolute bias shows at the +/-{u:.3f} m this estimate "
                   f"allows; a co-located gauge (below)"
                   + (" is what narrows it." if b["kind"] == "vdatum" else
                      " or VDatum at the site is what narrows it."), 2)
    L += ["  Both offsets were measured against the GNSS-IR record (B11), so each difference",
          "  also reads as the GNSS-IR absolute bias when that offset was set: levels read that",
          "  much high (+) or low (-) against NOAA's tidal datums. The NAVD88 water level",
          "  itself (Hortho - RH) does not use either offset.",
          "", "To measure the GNSS-IR bias directly (co-located gauge; docs/INDEPENDENT_DATUM.md):"]
    L += _wrap(f"days of daily means needed (daily scatter {rep['daily_sigma']['value']:.3f} m, "
               f"{rep['daily_sigma']['source']}; gauge's own level {GAUGE_SIGMA_M:.3f} m; never "
               f"under {MIN_DEPLOYMENT_DAYS}):", 2)
    L += [f"  {'gauge zero tied to NAVD88 by (1 sigma, m)':50s}"
          + "".join(f"{'+/-' + k + ' m':>11s}" for k in rep["calibration"][0]["targets"])]
    for plan in rep["calibration"]:
        cells = "".join(f"{(str(v['days']) + ' d') if v['feasible'] else 'never':>11s}"
                        for v in plan["targets"].values())
        tie = f"{plan['tie']} ({plan['sigma_tie']:.3f})"
        L.append(f"  {tie:50s}{cells}")
    L.append("  'never': the tie and the gauge's own error alone reach the target.")
    if rep["daily_sigma"]["source"] != "--daily-sigma":
        L += _wrap("The daily scatter is that of sheltered sites; on this exposed beach it is "
                   "likely larger (Sepulveda et al. 2023), and the days grow as its square: "
                   "--daily-sigma sets it.", 2)
    return "\n".join(L)


def load_cache(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_cache(path: Path | None, cache: dict) -> None:
    if not path or not cache:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True))
    tmp.replace(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--station-json", default=None,
                   help="station.json to read (default station/resources/station.json)")
    p.add_argument("--offline", action="store_true",
                   help="do not use the network: cache, then the built-in published values")
    p.add_argument("--cache", default=None,
                   help="JSON file: live answers are saved here and used when NOAA cannot be reached")
    p.add_argument("--json", default=None, help="also write the report as JSON to this file")
    p.add_argument("--window", nargs=2, metavar=("START", "END"), default=None,
                   help="YYYY-MM-DD YYYY-MM-DD: the overlap tide_model_navd88_offset was measured "
                        "in, so it is compared with mean sea level over the same days (for a "
                        "tide model run without the seasonal SA/SSA terms)")
    p.add_argument("--no-vdatum", action="store_true", help="skip the VDatum requests")
    p.add_argument("--daily-sigma", type=float, default=None, metavar="M",
                   help=f"daily-mean GNSS-IR minus gauge scatter (m) for the calibration table "
                        f"(default {DAILY_SIGMA_M}, a sheltered-site literature value)")
    args = p.parse_args(argv)
    if args.daily_sigma is not None and not args.daily_sigma > 0:
        p.error("--daily-sigma must be above 0")

    window = None
    if args.window:
        try:
            window = tuple(datetime.strptime(w, "%Y-%m-%d").date() for w in args.window)
        except ValueError:
            p.error("--window takes two dates, YYYY-MM-DD")
        if window[1] < window[0]:
            p.error("--window: END is before START")

    cfg = load_station(args.station_json)
    if not cfg:
        print("  (no station.json read: the documented Marconi position and heights are used)")
    cache = load_cache(args.cache)
    rep = build_report(cfg, Net(online=not args.offline), cache, datetime.now(timezone.utc),
                       window=window, vdatum=not args.no_vdatum, daily_sigma=args.daily_sigma)
    print(format_report(rep))
    if args.cache and not args.offline:
        save_cache(Path(args.cache), cache)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
        print(f"\nWrote {args.json}")
    return 0 if rep["best"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
