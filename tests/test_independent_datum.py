"""
tests/test_independent_datum.py

analysis_tools/independent_datum.py puts mean sea level (and MHW/MLW)
on NAVD88 at Marconi from NOAA's tidal datums and VDatum, never from
the GNSS-IR record, and sets the two GNSS-IR-derived station.json
offsets against it. No test touches the network: every NOAA answer is
a mock shaped like the real one (CO-OPS metadata and data APIs, the
VDatum /convert endpoint), and the tool must fall back cleanly when a
host is unreachable.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
import independent_datum as idt  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

MARCONI = {
    "station_name": "Marconi", "latitude": 41.8928243333, "longitude": -69.9633227139,
    "gnssrefl_orthometric_height": 19.014, "vertical_datum": "NAVD88 (GEOID18)",
    "gnssrefl_elevation_min": 5.0, "gnssrefl_elevation_max": 15.0,
    "gnssrefl_azimuth_regions": [353, 360, 0, 130],
    "water_level_msl_offset": 0.10, "tide_model_navd88_offset": 0.09,
}

# Chatham as the metadata API gives it: heights above the station
# datum (MLLW put at 2.000 here, so a parser that forgot to subtract
# NAVD88 would be 2 m out), plus a range (GT) that is not a datum.
CHATHAM_DATUMS = {
    "accepted": "May 20 2020", "superseded": "", "epoch": "1983-2001", "units": "meters",
    "OrthometricDatum": "NAVD88",
    "datums": [
        {"name": "STND", "description": "Station Datum", "value": 0.0},
        {"name": "MHHW", "description": "Mean Higher-High Water", "value": 3.578},
        {"name": "MHW", "description": "Mean High Water", "value": 3.464},
        {"name": "MSL", "description": "Mean Sea Level", "value": 2.778},
        {"name": "MTL", "description": "Mean Tide Level", "value": 2.757},
        {"name": "MLW", "description": "Mean Low Water", "value": 2.050},
        {"name": "MLLW", "description": "Mean Lower-Low Water", "value": 2.000},
        {"name": "GT", "description": "Great Diurnal Range", "value": 1.578},
        {"name": "NAVD88", "description": "North American Vertical Datum of 1988", "value": 2.760},
    ],
    "DatumAnalysisPeriod": ["09/01/2015 - 08/31/2017"],
    "ctrlStation": "8449130 Nantucket Island, MA",
    "self": "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/8447435/datums.json",
}
CHATHAM_META = {"count": 1, "stations": [{"id": "8447435", "name": "Chatham, Lydia Cove",
                                          "lat": 41.688508, "lng": -69.951053}]}
# stations.json?type=datums, cut down: two listed stations, one unlisted
# station 10 km from Marconi (a made-up ID), one far away, one broken row.
DATUM_STATIONS = {"count": 5, "stations": [
    {"id": "8447435", "name": "Chatham, Lydia Cove", "lat": 41.688508, "lng": -69.951053},
    {"id": "8446121", "name": "Provincetown", "lat": 42.0483, "lng": -70.1867},
    {"id": "8440001", "name": "Back-barrier cove", "lat": 41.9300, "lng": -70.0700},
    {"id": "8452660", "name": "Newport", "lat": 41.505, "lng": -71.326},
    {"id": "8440002", "name": "no position"},
]}


def other_station(msl_minus_navd: float) -> dict:
    return {"epoch": "1983-2001", "units": "meters",
            "datums": [{"name": "MSL", "value": 1.0 + msl_minus_navd},
                       {"name": "MHW", "value": 1.6}, {"name": "MLW", "value": 0.4},
                       {"name": "NAVD88", "value": 1.0}]}


def monthly(year_months, msl_of=lambda y, m: 0.12):
    """A monthly_mean answer (datum=MSL): every value a string, as the
    API sends them."""
    return {"metadata": {"id": "8447435", "name": "Chatham, Lydia Cove"},
            "data": [{"year": str(y), "month": str(m), "highest": "1.9", "MHHW": "0.92",
                      "MHW": "0.80", "MSL": f"{msl_of(y, m):.3f}", "MTL": "0.11",
                      "MLW": "-0.58", "MLLW": "-0.62", "lowest": "-0.9", "inferred": "0"}
                     for y, m in year_months]}


LAST_12 = [(2025, m) for m in range(10, 13)] + [(2026, m) for m in range(1, 10)]


OFFSHORE_S_Y = f"s_y={idt.query_points(MARCONI)[2][1]:.7f}&"
CHATHAM_S_Y = "s_y=41.6885080&"


class Router:
    """A fake fetch_json: answers by URL and records every request.
    VDatum gives vdatum_zone at the site (vdatum_offshore 1 km out, when
    set) and vdatum_chatham at Chatham's position."""

    def __init__(self, vdatum_zone=0.061, down=(), http_404=(), monthly_answer=None,
                 vdatum_offshore=None, vdatum_chatham=0.025):
        self.calls = []
        self.vdatum_zone = vdatum_zone
        self.vdatum_offshore = vdatum_offshore
        self.vdatum_chatham = vdatum_chatham
        self.down = set(down)
        self.http_404 = set(http_404)
        self.monthly_answer = monthly_answer if monthly_answer is not None else monthly(LAST_12)

    def __call__(self, url):
        self.calls.append(url)
        host = url.split("/")[2]
        if host in self.down:
            raise urllib.error.URLError("no route to host")
        if "vdatum" in host:
            if "s_y=41.8928243&" in url:            # the antenna, on the bluff
                return {"t_z": "-999999", "message": "outside tidal grid"}
            frame = url.split("s_v_frame=")[1].split("&")[0]
            z = (self.vdatum_chatham if CHATHAM_S_Y in url else
                 self.vdatum_offshore if self.vdatum_offshore is not None and OFFSHORE_S_Y in url
                 else self.vdatum_zone)
            return {"t_z": str(z + {"LMSL": 0.0, "MHW": 0.70, "MLW": -0.75}[frame])}
        if "monthly_mean" in url:
            return self.monthly_answer
        if "stations.json?type=datums" in url:
            return DATUM_STATIONS
        station = url.split("/stations/")[1][:7]
        if station in self.http_404:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if url.endswith(f"{station}.json"):
            return CHATHAM_META if station == "8447435" else {"stations": []}
        if station == "8447435":
            return CHATHAM_DATUMS
        return other_station({"8446121": 0.030, "8447495": -0.106, "8440001": 0.250}.get(station, -0.08))


def report(router=None, online=True, cache=None, cfg=None, **kw):
    router = router or Router()
    rep = idt.build_report(dict(MARCONI if cfg is None else cfg), idt.Net(router, online=online),
                           {} if cache is None else cache, NOW, **kw)
    return rep, router


class TestParsing(unittest.TestCase):

    def test_coops_datums_are_put_on_navd88(self) -> None:
        d = idt.parse_coops_datums(CHATHAM_DATUMS)
        self.assertEqual(d["datums"], {"MHHW": 0.818, "MHW": 0.704, "MSL": 0.018,
                                       "MTL": -0.003, "MLW": -0.71, "MLLW": -0.76})
        self.assertEqual(d["epoch"], "1983-2001")
        self.assertIn("2015", d["series"])
        self.assertTrue(d["control"].startswith("8449130"))

    def test_data_api_shape_and_feet(self) -> None:
        d = idt.parse_coops_datums({"units": "feet", "datums": [
            {"n": "MSL", "v": "5.0"}, {"n": "NAVD88", "v": "4.0"}, {"n": "MHW", "v": ""}]})
        self.assertAlmostEqual(d["datums"]["MSL"], 0.3048)
        self.assertNotIn("MHW", d["datums"])          # blank value skipped

    def test_coops_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "No data"):
            idt.parse_coops_datums({"error": {"message": "No data was found."}})
        with self.assertRaisesRegex(ValueError, "NAVD88"):
            idt.parse_coops_datums({"datums": [{"name": "MSL", "value": 1.0}]})
        with self.assertRaisesRegex(ValueError, "no datums"):
            idt.parse_coops_datums({"stations": []})

    def test_vdatum_answers(self) -> None:
        self.assertEqual(idt.parse_vdatum({"t_z": "0.0612"}), (0.0612, ""))
        self.assertEqual(idt.parse_vdatum({"t_z": -0.25})[0], -0.25)
        self.assertIsNone(idt.parse_vdatum({"t_z": "-999999"})[0])
        self.assertIsNone(idt.parse_vdatum({"t_z": -999999.0})[0])
        z, why = idt.parse_vdatum({"message": "Invalid region"})
        self.assertIsNone(z)
        self.assertEqual(why, "Invalid region")

    def test_monthly_means(self) -> None:
        rows = idt.parse_monthly_means(monthly([(2026, 1)]))
        self.assertEqual(rows, [{"year": 2026, "month": 1, "MSL": 0.12, "MHW": 0.8, "MLW": -0.58}])
        blank = monthly([(2026, 2)])
        blank["data"][0]["MSL"] = ""
        self.assertIsNone(idt.parse_monthly_means(blank)[0]["MSL"])
        with self.assertRaises(ValueError):
            idt.parse_monthly_means({"error": {"message": "No data was found."}})

    def test_station_meta(self) -> None:
        self.assertEqual(idt.parse_station_meta(CHATHAM_META)["name"], "Chatham, Lydia Cove")
        self.assertEqual(idt.parse_station_meta({"stations": []}), {})

    def test_station_list(self) -> None:
        got = idt.parse_station_list(DATUM_STATIONS)
        self.assertEqual([s["id"] for s in got], ["8447435", "8446121", "8440001", "8452660"])
        self.assertEqual(got[0], {"id": "8447435", "name": "Chatham, Lydia Cove",
                                  "lat": 41.688508, "lon": -69.951053})
        self.assertEqual(idt.parse_station_list({"error": {"message": "bad type"}}), [])
        self.assertEqual(idt.parse_station_list(None), [])


class TestGeometryAndTime(unittest.TestCase):

    def test_mean_azimuth_across_north(self) -> None:
        self.assertAlmostEqual(idt.mean_azimuth([353, 360, 0, 130]), 61.5, delta=1.0)
        self.assertAlmostEqual(idt.mean_azimuth([350, 10]), 0.0, delta=0.5)
        self.assertAlmostEqual(idt.mean_azimuth([80, 100]), 90.0, delta=0.01)

    def test_full_circle_points_nowhere(self) -> None:
        # the template's [0, 360] (and unset) have no seaward centre
        self.assertIsNone(idt.mean_azimuth([0, 360]))
        self.assertIsNone(idt.mean_azimuth(None))
        self.assertIsNone(idt.mean_azimuth([0, 180, 180, 360]))

    def test_reflection_zone_point(self) -> None:
        pts = idt.query_points(MARCONI)
        self.assertEqual(pts[0][1:], (MARCONI["latitude"], MARCONI["longitude"]))
        # RH / tan(e): 19.014 m / tan(10 deg) = 107.8 m from the antenna
        zone_km = idt.distance_km(pts[0][1], pts[0][2], pts[1][1], pts[1][2])
        self.assertAlmostEqual(zone_km, 19.014 / 0.17632698 / 1000, places=3)
        self.assertAlmostEqual(idt.distance_km(pts[0][1], pts[0][2], pts[2][1], pts[2][2]), 1.0,
                               places=3)
        self.assertGreater(pts[1][2], pts[0][2])           # seaward: east
        self.assertIn("108 m at 61 deg", pts[1][0])

    def test_no_position_uses_marconi(self) -> None:
        pts = idt.query_points({})
        self.assertEqual(pts[0][1:], (idt.MARCONI_LAT, idt.MARCONI_LON))
        # and the documented Marconi azimuths, not north along the bluff
        self.assertIn("at 61 deg", pts[1][0])
        self.assertGreater(pts[1][2], pts[0][2])
        full = idt.query_points(dict(MARCONI, gnssrefl_azimuth_regions=[0, 360]))
        self.assertEqual(full[1][0], idt.query_points(MARCONI)[1][0])

    def test_epoch_and_trend(self) -> None:
        self.assertEqual(idt.epoch_midpoint("1983-2001"), 1992.5)
        self.assertEqual(idt.epoch_midpoint("2002-2020"), 2011.5)
        self.assertIsNone(idt.epoch_midpoint(""))
        mid_2026 = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
        self.assertAlmostEqual(idt.decimal_year(mid_2026), 2026.5, places=2)
        adj = idt.sea_level_since_epoch(idt.Net(online=False), "8447435", "1983-2001",
                                        mid_2026, {})
        years = idt.decimal_year(mid_2026) - 1992.5
        self.assertAlmostEqual(adj["value"], 4.01e-3 * years, places=4)
        self.assertAlmostEqual(adj["low"], 2.94e-3 * years, places=4)
        self.assertAlmostEqual(adj["sigma"], (4.01 - 2.94) / 2e3 * years, places=4)
        self.assertIn("linear trend used", adj["how"])

    def test_annual_anomaly_needs_twelve_whole_months(self) -> None:
        rows = idt.parse_monthly_means(monthly(LAST_12, lambda y, m: 0.10 + 0.05 * (m in (9, 10, 11))))
        # 3 autumn months at +0.15, 9 at +0.10, over Oct 2025 - Sep 2026
        value, first, last = idt.annual_anomaly(rows, NOW)
        self.assertAlmostEqual(value, (3 * 0.15 + 9 * 0.10) / 12, places=6)
        self.assertEqual((first, last), ((2025, 10), (2026, 9)))
        self.assertIsNone(idt.annual_anomaly(rows[1:], NOW))
        # a gap anywhere in the year spoils it
        self.assertIsNone(idt.annual_anomaly(rows[:5] + rows[6:], NOW))
        # older than the lookback: not used
        self.assertIsNone(idt.annual_anomaly(rows, datetime(2028, 11, 1, tzinfo=timezone.utc)))

    def test_annual_anomaly_takes_the_latest_published_year(self) -> None:
        # verified monthly means lag: Aug and Sep 2026 not out yet in Oct
        months = [(2024, m) for m in range(9, 13)] + [(2025, m) for m in range(1, 13)] \
            + [(2026, m) for m in range(1, 8)]
        rows = idt.parse_monthly_means(monthly(months, lambda y, m: 0.01 * (y - 2024) + 0.10))
        value, first, last = idt.annual_anomaly(rows, NOW)
        self.assertEqual((first, last), ((2025, 8), (2026, 7)))
        self.assertAlmostEqual(value, (5 * 0.11 + 7 * 0.12) / 12, places=6)
        # the 12 months searched for may not start before the lookback
        self.assertIsNone(idt.annual_anomaly(rows, NOW, lookback=13))

    def test_window_anomaly_weights_days(self) -> None:
        rows = idt.parse_monthly_means(monthly([(2026, 8), (2026, 9)],
                                               lambda y, m: 0.20 if m == 8 else 0.10))
        got = idt.window_anomaly(rows, date(2026, 8, 1), date(2026, 9, 16))
        self.assertAlmostEqual(got, (31 * 0.20 + 16 * 0.10) / 47, places=6)
        self.assertIsNone(idt.window_anomaly(rows, date(2026, 7, 20), date(2026, 8, 5)))


class TestLiveReport(unittest.TestCase):

    def test_vdatum_at_the_reflection_zone_is_the_estimate(self) -> None:
        rep, _ = report()
        best = rep["best"]
        self.assertIn("VDatum reflection zone", best["source"])
        self.assertAlmostEqual(best["epoch_msl"], 0.061)
        # observed: 12 months of Chatham monthly MSL at +0.12 m against the epoch
        self.assertEqual(rep["adjustment"]["value"], 0.12)
        self.assertIsNone(rep["adjustment"]["sigma"])
        self.assertAlmostEqual(best["present"]["MSL"], 0.181)
        self.assertAlmostEqual(best["present"]["MHW"], 0.881)
        self.assertAlmostEqual(best["present"]["MLW"], -0.569)

    def test_comparison_and_implied_bias(self) -> None:
        rep, _ = report()
        by = {c["field"]: c for c in rep["comparisons"]}
        self.assertAlmostEqual(by["water_level_msl_offset"]["bias"], 0.10 - 0.181)
        self.assertAlmostEqual(by["tide_model_navd88_offset"]["bias"], 0.09 - 0.181)
        self.assertIn("--window does that", by["tide_model_navd88_offset"]["note"])

    def test_vdatum_budget_is_its_own_skill_and_site_spread(self) -> None:
        rep, router = report(Router(vdatum_offshore=0.051))
        b = rep["best"]
        # VDatum at Chatham 0.025 against the published 0.018; zone 0.061
        # and offshore 0.051 (no value at the antenna); the harbours'
        # spread (Saquatucket -0.106) does not enter
        self.assertEqual(rep["vdatum_check"], [{"station": "8447435",
                                                "label": "CO-OPS 8447435 Chatham, Lydia Cove",
                                                "vdatum": 0.025, "published": 0.018,
                                                "residual": 0.007}])
        self.assertEqual(b["parts"], {"VDatum model": 0.007, "epoch correction": None,
                                      "spread over the VDatum points": 0.005})
        self.assertEqual((b["spread_n"], b["check_n"]), (2, 1))
        self.assertAlmostEqual(b["uncertainty"], (0.007 ** 2 + 0.005 ** 2) ** 0.5, places=4)
        # only Chatham's position is known (the others' metadata is empty);
        # it is asked for LMSL alone
        at_chatham = [u for u in router.calls if CHATHAM_S_Y in u]
        self.assertEqual(len(at_chatham), 1)
        self.assertIn("s_v_frame=LMSL&", at_chatham[0])
        self.assertIn("VDatum model: RMS of VDatum minus the published MSL at 1 local",
                      idt.format_report(rep).replace("\n    ", " "))

    def test_vdatum_far_from_the_harbours_does_not_widen_the_budget(self) -> None:
        near = report(Router(vdatum_zone=0.018, vdatum_chatham=0.020))[0]["best"]
        far = report(Router(vdatum_zone=0.100, vdatum_chatham=0.020))[0]["best"]
        self.assertEqual(near["uncertainty"], far["uncertainty"])
        self.assertAlmostEqual(far["uncertainty"], 0.002)

    def test_vdatum_without_a_check_names_what_is_left_out(self) -> None:
        rep, _ = report(Router(vdatum_chatham=-999999))       # no tidal grid at Chatham
        self.assertIsNone(rep["best"]["parts"]["VDatum model"])
        self.assertEqual(rep["vdatum_check"], [])
        text = " ".join(idt.format_report(rep).split())
        self.assertIn("VDatum model unknown", text)
        self.assertIn("leaves out what is not known: VDatum model", text)

    def test_requests_carry_the_right_parameters(self) -> None:
        _, router = report()
        vd = [u for u in router.calls if "vdatum" in u]
        self.assertTrue(vd)
        for u in vd:
            for part in ("region=contiguous", "s_z=0&", "t_v_frame=NAVD88", "s_v_unit=m",
                         "t_v_unit=m", "s_coor=geo"):
                self.assertIn(part, u)
        self.assertEqual({u.split("s_v_frame=")[1].split("&")[0] for u in vd}, {"LMSL", "MHW", "MLW"})
        # the antenna answers -999999 for LMSL, so its MHW/MLW are not asked
        self.assertEqual(sum("s_y=41.8928243&" in u for u in vd), 1)
        # VDatum is also asked at Chatham, for LMSL alone (vdatum_check)
        self.assertEqual(sum(CHATHAM_S_Y in u for u in vd), 1)
        mm = [u for u in router.calls if "monthly_mean" in u]
        self.assertEqual(len(mm), 1)
        for part in ("datum=MSL", "units=metric", "time_zone=gmt", "station=8447435",
                     "begin_date=20241001", "end_date=20260930"):
            self.assertIn(part, mm[0])
        self.assertIn("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/8447435/"
                      "datums.json?units=metric", router.calls)

    def test_chatham_gets_its_name_and_distance(self) -> None:
        rep, _ = report()
        ch = next(s for s in rep["sources"] if s.get("station") == "8447435" and not s["superseded"])
        self.assertEqual(ch["label"], "CO-OPS 8447435 Chatham, Lydia Cove")
        self.assertAlmostEqual(ch["distance_km"], 22.8, delta=0.3)
        self.assertEqual(ch["datum_error"], 0.012)
        self.assertTrue(ch["provenance"].startswith("live"))

    def test_window_compares_the_tide_offset_with_its_season(self) -> None:
        router = Router(monthly_answer=monthly(LAST_12, lambda y, m: 0.20 if m in (8, 9) else 0.10))
        rep, _ = report(router, window=(date(2026, 8, 1), date(2026, 9, 16)))
        self.assertAlmostEqual(rep["msl_window"], 0.061 + 0.20)
        by = {c["field"]: c for c in rep["comparisons"]}
        self.assertAlmostEqual(by["tide_model_navd88_offset"]["reference"], 0.261)
        self.assertAlmostEqual(by["tide_model_navd88_offset"]["bias"], 0.09 - 0.261)
        self.assertEqual(by["tide_model_navd88_offset"]["note"], "against mean sea level over --window")
        # the plot's zero is still compared with the whole year
        self.assertAlmostEqual(by["water_level_msl_offset"]["reference"],
                               0.061 + (10 * 0.10 + 2 * 0.20) / 12, places=4)

    def test_unlisted_nearby_stations_are_shown_not_counted(self) -> None:
        rep, router = report()
        tried = dict(rep["tried"])
        self.assertEqual(tried["CO-OPS datum stations within 30 km"],
                         "live: 1 not in NEARBY_STATIONS within 30 km")
        self.assertEqual(tried["CO-OPS datums 8440001 (not listed)"], "live")
        cove = next(s for s in rep["sources"] if s.get("station") == "8440001")
        self.assertTrue(cove["discovered"])
        self.assertFalse(cove["local"])
        self.assertAlmostEqual(cove["distance_km"], 9.8, delta=0.3)
        self.assertEqual(cove["datums"]["MSL"], 0.25)
        # its position came with the list: no metadata request for it
        self.assertFalse(any(u.endswith("/stations/8440001.json") for u in router.calls))
        self.assertFalse(any(s.get("station") == "8452660" for s in rep["sources"]))   # 150 km off
        # a station found this way is never the estimate, even alone
        self.assertIsNone(idt.best_estimate([cove], {"value": 0.1, "sigma": 0.01}))
        self.assertIn("not in NEARBY_STATIONS: shown, not counted", idt.format_report(rep))

    def test_unlisted_search_is_capped_nearest_first(self) -> None:
        home = (idt.MARCONI_LAT, idt.MARCONI_LON)
        net = idt.Net(Router())
        found, how = idt.nearby_unlisted(net, home, set(), limit=2)
        self.assertEqual([s["id"] for s in found], ["8440001", "8447435"])
        self.assertIn("3 not in NEARBY_STATIONS within 30 km, nearest 2 shown", how)
        found, how = idt.nearby_unlisted(idt.Net(Router(), online=False), home, set())
        self.assertEqual(found, [])
        self.assertIn("--offline", how)

    def test_superseded_values_never_chosen(self) -> None:
        old = idt.offline_record("8447435/2000")
        self.assertEqual(old["superseded"], "2003-04-21")
        self.assertIsNone(idt.best_estimate([old], {"value": 0.1, "sigma": 0.01}))


class TestFallbacks(unittest.TestCase):

    def test_unreachable_hosts_are_tried_once_and_offline_values_used(self) -> None:
        router = Router(down={"api.tidesandcurrents.noaa.gov", "vdatum.noaa.gov"})
        rep, _ = report(router)
        self.assertEqual(len(router.calls), 2)              # one per host
        best = rep["best"]
        self.assertEqual(best["source"], "CO-OPS 8447435 Chatham, Lydia Cove, MA")
        self.assertEqual(best["epoch_msl"], 0.018)
        self.assertIn("linear trend used", rep["adjustment"]["how"])
        self.assertAlmostEqual(best["present"]["MSL"], 0.018 + rep["adjustment"]["value"])
        # Chatham and Saquatucket from OFFLINE_DATUMS: spread (0.018 + 0.106) / 2
        self.assertAlmostEqual(best["parts"]["transfer to Marconi"], 0.062)
        ch = next(s for s in rep["sources"] if s["key"] == "coops:8447435")
        self.assertTrue(ch["provenance"].startswith("offline: NOAA CO-OPS published bench mark sheet"))
        tried = dict(rep["tried"])
        self.assertIn("unreachable", tried["CO-OPS datums 8447435"])
        self.assertIn("not tried", tried["CO-OPS datums 8443970"])
        self.assertIn("no offline values", tried["CO-OPS datums 8443970"])

    def test_offline_budget_and_harbour_tide(self) -> None:
        rep, _ = report(Router(down={"api.tidesandcurrents.noaa.gov", "vdatum.noaa.gov"}))
        b = rep["best"]
        self.assertEqual(b["parts"]["published datum"], 0.012)
        self.assertAlmostEqual(b["uncertainty"], 0.066, places=3)
        # Chatham's MHW and MLW are its harbour's, not Marconi's: kept apart
        self.assertEqual(set(b["present"]), {"MSL"})
        self.assertAlmostEqual(b["station_tide"]["MHW"], 0.704 + rep["adjustment"]["value"])
        self.assertAlmostEqual(b["station_tide"]["MLW"], -0.710 + rep["adjustment"]["value"])
        text = " ".join(idt.format_report(rep).split())
        self.assertIn("Present-day mean sea level at Marconi, from CO-OPS 8447435", text)
        self.assertIn("does not transfer to the open coast at Marconi", text)
        self.assertIn("uncertainty of MSL +/-0.066 m", text)
        self.assertIn("or VDatum at the site is what narrows it", text)

    def test_verdict_lines(self) -> None:
        rep, _ = report()
        rep["best"]["uncertainty"] = 0.05
        rep["comparisons"][0]["bias"] = 0.02
        rep["comparisons"][1]["bias"] = -0.08
        text = idt.format_report(rep)
        self.assertIn("water_level_msl_offset: within the independent height's own uncertainty.",
                      text)
        self.assertIn("tide_model_navd88_offset: below it by more than its uncertainty.", text)
        self.assertNotIn("So no GNSS-IR absolute bias shows", text)
        rep["comparisons"][1]["bias"] = -0.04
        self.assertIn("a co-located gauge (below) is what narrows it",
                      " ".join(idt.format_report(rep).split()))

    def test_epochs_are_compared(self) -> None:
        new = {"key": "coops:x", "kind": "coops", "station": "8447435", "label": "new epoch",
               "datums": {"MSL": 0.08}, "epoch": "2002-2020", "local": True}
        old = {"key": "coops:y", "kind": "coops", "station": "8446121", "label": "old epoch",
               "datums": {"MSL": -0.10}, "epoch": "1983-2001", "local": True}
        b = idt.best_estimate([new, old], {"value": 0.05, "sigma": 0.01, "epoch": "1983-2001"})
        self.assertEqual(b["spread_n"], 1)                # the old-epoch station left out
        self.assertIsNone(b["parts"]["transfer to Marconi"])
        self.assertEqual(len(b["warnings"]), 2)
        self.assertIn("sea-level correction on 1983-2001", b["warnings"][0])
        self.assertIn("old epoch", b["warnings"][1])

    def test_broken_off_answer_falls_back(self) -> None:
        def fetch(url):
            raise http.client.IncompleteRead(b"{\"dat")
        net = idt.Net(fetch)
        with self.assertRaisesRegex(idt.Unavailable, "broken off"):
            net.get("https://vdatum.noaa.gov/x")
        self.assertEqual(net.down, {})

    def test_http_error_does_not_mark_the_host_down(self) -> None:
        rep, router = report(Router(http_404={"8446121"}))
        tried = dict(rep["tried"])
        self.assertIn("HTTP 404", tried["CO-OPS datums 8446121"])
        self.assertEqual(tried["CO-OPS datums 8443970"], "live")
        self.assertFalse(any(s.get("station") == "8446121" for s in rep["sources"]))

    def test_offline_flag_makes_no_request(self) -> None:
        rep, router = report(online=False)
        self.assertEqual(router.calls, [])
        self.assertIsNotNone(rep["best"])
        self.assertTrue(all("--offline" in how or "offline values" in how
                            for _, how in rep["tried"]))

    def test_cache_round_trip(self) -> None:
        cache = {}
        report(cache=cache)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "cache.json"
            idt.save_cache(path, cache)
            again = idt.load_cache(path)
        self.assertIn("coops:8447435", again)
        self.assertIn("monthly:8447435", again)
        rep, router = report(online=False, cache=again)
        self.assertEqual(router.calls, [])
        self.assertIn("VDatum reflection zone", rep["best"]["source"])
        self.assertEqual(rep["adjustment"]["value"], 0.12)          # cached monthly means
        ch = next(s for s in rep["sources"] if s["key"] == "coops:8447435")
        self.assertTrue(ch["provenance"].startswith(f"cache, retrieved {idt._stamp(NOW)}"))

    def test_partial_year_falls_back_to_the_trend(self) -> None:
        rep, _ = report(Router(monthly_answer=monthly(LAST_12[1:])))
        self.assertEqual(rep["adjustment"]["how"],
                         "live, but no 12 whole months in the last 24; linear trend used")
        self.assertIn("low", rep["adjustment"])

    def test_missing_cache_file_is_empty(self) -> None:
        self.assertEqual(idt.load_cache(Path("/nonexistent/cache.json")), {})
        self.assertEqual(idt.load_cache(None), {})


class TestConfigured(unittest.TestCase):

    def test_unmigrated_config_is_moved_onto_navd88(self) -> None:
        cfg = {"gnssrefl_orthometric_height": 18.665, "water_level_msl_offset": -0.25}
        out = idt.compare_configured(cfg, 0.15)
        self.assertAlmostEqual(out[0]["value"], -0.25 + 0.349)
        self.assertAlmostEqual(out[0]["bias"], -0.25 + 0.349 - 0.15)
        self.assertIn("default used", out[1]["note"])
        self.assertAlmostEqual(out[1]["value"], idt.tide_model_to_navd88({}))

    def test_unset_msl_offset(self) -> None:
        out = idt.compare_configured({"tide_model_navd88_offset": 0.05}, 0.15)
        self.assertIsNone(out[0]["bias"])
        self.assertIn("not set", out[0]["note"])
        self.assertAlmostEqual(out[1]["bias"], -0.10)


class TestCalibrationPlan(unittest.TestCase):

    def test_days_needed(self) -> None:
        # (0.025^2) / (0.01^2 - 0.005^2) = 8.3 -> 9 days, floored at a month
        plan = idt.calibration_plan(0.01, 0.005, sigma_gauge=0.0)
        self.assertEqual(plan["statistical_days"], 9)
        self.assertEqual(plan["days"], idt.MIN_DEPLOYMENT_DAYS)
        self.assertEqual(idt.calibration_plan(0.01, 0.005, min_days=1, sigma_gauge=0.0)["days"], 9)
        self.assertEqual(idt.calibration_plan(0.005, 0.0, sigma_daily=0.05, min_days=1,
                                              sigma_gauge=0.0)["days"], 100)
        # the gauge's own 0.01 m: 0.025^2 / (0.02^2 - 0.005^2 - 0.01^2) = 2.3 -> 3
        self.assertEqual(idt.calibration_plan(0.02, 0.005)["statistical_days"], 3)

    def test_a_tie_as_uncertain_as_the_target_never_gets_there(self) -> None:
        self.assertFalse(idt.calibration_plan(0.02, 0.02)["feasible"])
        self.assertFalse(idt.calibration_plan(0.01, 0.03)["feasible"])
        self.assertTrue(idt.calibration_plan(0.03, 0.02)["feasible"])
        # nor a perfect tie when the gauge alone is as uncertain as the target
        self.assertFalse(idt.calibration_plan(0.01, 0.0)["feasible"])

    def test_ties_are_ones_a_moored_logger_can_have(self) -> None:
        sigmas = dict((label, tie) for label, tie in idt.TIE_OPTIONS)
        self.assertAlmostEqual(sigmas[f"{idt.TIE_SESSIONS} calm slack-water staff/RTK sessions"],
                               idt.TIE_SESSION_M / idt.TIE_SESSIONS ** 0.5, places=4)
        self.assertFalse(any("static GNSS" in label for label in sigmas))


class TestCommandLine(unittest.TestCase):

    def test_offline_run_prints_report_writes_json_and_leaves_station_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sj = Path(td) / "station.json"
            sj.write_text(json.dumps(MARCONI, indent=4))
            before = sj.read_bytes()
            out_json = Path(td) / "report.json"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = idt.main(["--offline", "--station-json", str(sj), "--json", str(out_json)])
            self.assertEqual(code, 0)
            self.assertEqual(sj.read_bytes(), before)
            rep = json.loads(out_json.read_text())
        text = buf.getvalue()
        self.assertIn("report only", text)
        self.assertIn("CO-OPS 8447435 Chatham, Lydia Cove, MA", text)
        self.assertIn("water_level_msl_offset", text)
        self.assertIn("tide_model_navd88_offset", text)
        self.assertIn("never", text)
        self.assertEqual(rep["best"]["epoch_msl"], 0.018)
        self.assertEqual(len(rep["calibration"]), len(idt.TIE_OPTIONS))
        self.assertEqual(rep["daily_sigma"], {"value": idt.DAILY_SIGMA_M,
                                              "source": idt.DAILY_SIGMA_SOURCE})
        self.assertIn("--daily-sigma sets it", " ".join(text.split()))

    def test_daily_sigma_option(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "report.json"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                idt.main(["--offline", "--no-vdatum", "--daily-sigma", "0.06",
                          "--station-json", str(Path(td) / "none.json"), "--json", str(out_json)])
            rep = json.loads(out_json.read_text())
        self.assertEqual(rep["daily_sigma"], {"value": 0.06, "source": "--daily-sigma"})
        # 0.06^2 / (0.02^2 - 0.005^2 - 0.01^2) = 13.1 -> 14 days (levelled pipe mount)
        self.assertEqual(rep["calibration"][0]["targets"]["0.02"]["statistical_days"], 14)
        self.assertIn("daily scatter 0.060 m, --daily-sigma", " ".join(buf.getvalue().split()))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                idt.main(["--offline", "--daily-sigma", "0"])

    def test_bad_window_is_refused(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                idt.main(["--offline", "--window", "2026-09-16", "2026-08-01"])
            with self.assertRaises(SystemExit):
                idt.main(["--offline", "--window", "2026-08", "2026-09"])


if __name__ == "__main__":
    unittest.main()
