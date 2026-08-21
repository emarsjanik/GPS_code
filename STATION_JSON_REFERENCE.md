# station.json Reference

This document explains every setting in `station/resources/station.json`.

You don't need to read this top to bottom to get started — `./setup_station.sh`
walks you through every field interactively with the same explanations shown
here, and sensible defaults for most of it. This document exists for when you
want to understand what a setting actually does, change something later by
hand, or double-check a value before a long unattended recording run.

Settings are grouped the same way the setup wizard presents them. Fields
marked **Required** have no safe default — the station will not run
correctly without a real value. Fields marked **Optional** are genuinely
optional: if you leave them out of the file entirely, gnssrefl's own
internal default is used, unchanged.

---

## Station identity

| Field | Type | Required? | Description |
|---|---|---|---|
| `station_id` | string | Required | A short identifier for your station, e.g. `"USGS001"`. Used in reports, RINEX file headers, and (if `gnssrefl_station_code` below isn't set) to derive the 4-character code gnssrefl itself uses internally. |
| `station_name` | string | Recommended | A human-readable name, e.g. `"Marconi Beach"`. Shown on dashboards and in reports; has no effect on processing. |
| `agency` | string | Recommended | Your organization, e.g. `"USGS"`. Written into RINEX file headers. |
| `observer` | string | Recommended | Who's responsible for the station, e.g. `"Jane Smith"`. Written into RINEX file headers. |

## Receiver

| Field | Type | Required? | Description |
|---|---|---|---|
| `receiver_model` | string | Recommended | Your receiver's model, e.g. `"Unicore UM980"`. Written into RINEX file headers and shown on the dashboard. |
| `receiver_firmware` | string | Optional | Firmware version. Usually left blank — the station reads this from the receiver itself and records it automatically the first time it connects. |
| `receiver_port` | string | Required | The serial device path the receiver is connected to, e.g. `"/dev/USB_GPS"` (a stable, named symlink is strongly recommended over `/dev/ttyUSB0`, which can change across reboots). |
| `receiver_baud` | integer | Required | Serial baud rate. `115200` is correct for the UM980 at its default configuration. |
| `receiver_timeout` | number | Required | Seconds to wait for a response to a query before giving up. `2.0` is a reasonable default. |

## Location — **the most important section**

Everything GNSS-IR computes depends on knowing exactly where your antenna
is. Getting this wrong doesn't cause an error — it silently produces
confident-looking, meaningless results.

| Field | Type | Required? | Description |
|---|---|---|---|
| `latitude` | number | **Required, must be accurate** | Decimal degrees, e.g. `41.8928243333`. **Not** degrees/minutes/seconds — if your survey data is in DMS format, convert it first. |
| `longitude` | number | **Required, must be accurate** | Decimal degrees, e.g. `-69.9633227139`. Western longitudes are negative. |
| `height` | number | **Required, must be accurate** | Antenna height in meters, **ellipsoidal** (WGS84), not orthometric/MSL height. If you only have an orthometric height, you need to apply your local geoid model's separation value to convert it, or use an ellipsoidal height directly from your GNSS survey. |

## Marker / antenna

These describe the physical monument and antenna, used for RINEX header
metadata (important for anyone else who later uses your RINEX files) and
have no effect on the GNSS-IR analysis itself.

| Field | Type | Required? | Description |
|---|---|---|---|
| `marker_name` | string | Recommended | Usually the same as `station_id`. |
| `marker_number` | string | Recommended | Usually the same as `station_id`. |
| `antenna.model` | string | Recommended | Antenna model, e.g. `"AS-ANT3B-BUDSUR-L1L2L5-25SMA"`. |
| `antenna.serial` | string | Optional | Antenna serial number. |
| `antenna.height` | number | Optional | Antenna offset height above the marker, meters. Default `0.0`. |
| `antenna.east_offset` | number | Optional | East offset from the marker, meters. Default `0.0`. |
| `antenna.north_offset` | number | Optional | North offset from the marker, meters. Default `0.0`. |

## RINEX

| Field | Type | Required? | Description |
|---|---|---|---|
| `rinex_version` | string | Recommended | RINEX version to produce, e.g. `"3.05"`. RINEX 3 is required for this project's RINEX-3-specific station code handling to work correctly (see `gnssrefl_monument_number`/`gnssrefl_country_code` below). |

## Recording

| Field | Type | Required? | Description |
|---|---|---|---|
| `record_raw_chunk_seconds` | integer | Recommended | How long each recording "chunk" is, in seconds, during continuous unattended operation (`station_manager.py`). Recording happens in chunks rather than one call per day so a shutdown request only has to wait for the current chunk to finish, not the whole day. `600` (10 minutes) is a reasonable default; `3600` (1 hour) trades slightly slower shutdown response for slightly less overhead. |

## gnssrefl station code

gnssrefl's own RINEX-processing tools require a station identifier of an
*exact* length depending on RINEX version — 4 characters for RINEX 2.11,
9 characters for RINEX 3. This project handles that automatically, but
these settings let you override the automatic derivation if needed.

| Field | Type | Required? | Description |
|---|---|---|---|
| `gnssrefl_station_code` | string, exactly 4 chars | Optional | The 4-character code gnssrefl uses internally. If not set, it's derived automatically from the first 4 characters of `station_id`, lowercased. Only set this by hand if that automatic derivation isn't right for you. |
| `gnssrefl_monument_number` | string, 2 chars | Optional | Used to build the 9-character RINEX 3 station code (4-char code + monument number + country code). Default `"00"`. |
| `gnssrefl_country_code` | string, 3 chars | Optional | Same purpose as above. Default `"usa"`. |

## Orbit source

| Field | Type | Required? | Description |
|---|---|---|---|
| `gnssrefl_orbit_source` | string | Optional | Leave unset for gnssrefl's own default: automatic multi-GNSS SP3 orbit downloads from CDDIS, covering GPS, GLONASS, Galileo, and BeiDou. Set to `"nav"` only if you need offline-friendlier GPS-only broadcast ephemeris instead (confirmed to roughly **halve** the number of usable satellite tracks compared to the multi-GNSS default — only use this if you have a specific reason to, such as no reliable internet access, or needing same-day results before that day's multi-GNSS orbit product has been published, which routinely takes 1-2 days). |

## Sample rate and constellations

| Field | Type | Required? | Description |
|---|---|---|---|
| `gnssrefl_sample_rate` | integer | Recommended | Must match your receiver's actual logging rate, in seconds. `1` (1 Hz) is standard. |
| `gnssrefl_all_frequencies` | boolean | Strongly recommended: `true` | Whether to analyze all constellations and frequencies (GPS + GLONASS + Galileo + BeiDou) rather than GPS-only. Confirmed to make a large, real difference in data density — leaving this `false` (gnssrefl's own internal default) silently discards most of what your receiver actually captured. |

---

## Advanced gnssrefl tuning (all optional)

Everything below has a sensible gnssrefl-internal default. Only change
these if you have a specific, understood reason to — an obstructed view,
an unusually tall or short site, a noisy environment, or a site with very
fast water-level changes. Leaving a field out of `station.json` entirely
means gnssrefl's own default applies, unchanged.

### Elevation angle mask

| Field | Type | Description |
|---|---|---|
| `gnssrefl_elevation_min` | number (degrees) | Minimum satellite elevation angle to analyze. gnssrefl's own default is roughly 5°. |
| `gnssrefl_elevation_max` | number (degrees) | Maximum satellite elevation angle to analyze. gnssrefl's own default is roughly 25°. |

A narrower window (e.g. 5–15°) generally means a *closer*, lower reflection
footprint — useful for a site with a limited clear view, but also means
fewer minutes of data per satellite pass and a smaller pool of usable
tracks (see `gnssrefl_direct_signal_poly_order` and
`gnssrefl_elevation_span_tolerance` below, which usually need adjusting
together with a narrow window like this).

### Reflector height search range

| Field | Type | Description |
|---|---|---|
| `gnssrefl_reflector_height_min` | number (meters) | Minimum reflector height to search for. gnssrefl's own default is roughly 0.5m. |
| `gnssrefl_reflector_height_max` | number (meters) | Maximum reflector height to search for. gnssrefl's own default is roughly 8m. |

This is the vertical distance gnssrefl searches for a reflecting surface
below the antenna — set this to comfortably bracket your antenna's real
height above whatever you're trying to sense (water, soil, snow),
including its full range of expected motion (e.g. full tidal range for a
coastal site).

**Important, confirmed technical detail:** if you set both of these,
this project automatically keeps gnssrefl's internal noise-floor
estimation region (`nr1`/`nr2`) in sync with them. Without this, widening
this range without knowing to *also* widen the noise region separately
causes every single retrieval to silently fail with no clear error — this
project handles that automatically as long as both `_min` and `_max` are
set together.

### Azimuth mask

| Field | Type | Description |
|---|---|---|
| `gnssrefl_azimuth_regions` | list of numbers (degrees) | Which compass directions have a genuinely clear view, as a flat list of region boundaries. Default (unset) is the full circle, `[0, 360]`. |

If part of your view is blocked (a building, trees, rising terrain), list
only the clear region(s). For example, `[0, 150, 180, 360]` analyzes
0°–150° and 180°–360°, excluding the 150°–180° range. This can wrap
through north: `[353, 360, 0, 173]` analyzes 353°–360° and 0°–173°
(i.e., roughly northwest through south, wrapping across due north).

### Quality control thresholds

| Field | Type | Description |
|---|---|---|
| `gnssrefl_peak2noise` | number | Minimum peak-to-noise ratio for a retrieval to be accepted. gnssrefl's own default is roughly 2.8. Raising this is stricter (fewer, higher-confidence retrievals); lowering it is more permissive. |
| `gnssrefl_amplitude_min` | number | Minimum signal amplitude for a retrieval to be accepted. gnssrefl's own default is roughly 5.0. |

### Orthometric height reference

| Field | Type | Description |
|---|---|---|
| `gnssrefl_orthometric_height` | number (meters) | Your antenna's height above a specific vertical datum (e.g. NAVD88, a local tide gauge datum). If set, gnssrefl reports real, absolute water level relative to this datum instead of just a relative reflector height. Meaningful for coastal sites; not typically applicable to interior lakes/rivers with no established local datum. |

### Refraction model

| Field | Type | Description |
|---|---|---|
| `gnssrefl_refraction_model` | integer | Which tropospheric refraction correction to apply. `1` (gnssrefl's own default) is the standard Bennett correction. Leave unset unless you have a specific, understood reason to change it. |

### Maximum arc length

| Field | Type | Description |
|---|---|---|
| `gnssrefl_max_arc_minutes` | number (minutes) | Maximum duration of a single satellite pass to include in one retrieval. gnssrefl's own default is 75 minutes, which is documented as too long for a site with a fast tidal (or other water-level) rate of change — a single retrieval's reflector-height estimate gets blurred across however much real change happens during that whole window. Consider a much shorter value (e.g. 20–40) for a site with strong or fast tides. |

### Arc elevation-span tolerance

| Field | Type | Description |
|---|---|---|
| `gnssrefl_elevation_span_tolerance` | number (degrees) | How close to your full elevation range (above) a satellite pass must actually reach to be accepted. gnssrefl's own default is 2 — e.g. with an elevation mask of 5–15°, a pass must span at least 7–13° to be accepted. This default is documented by gnssrefl itself as too strict for a narrow elevation mask like 5–15°; consider tightening it to `1` if you're using a narrow window like that and seeing very few retrievals. |

### Direct-signal removal polynomial order

| Field | Type | Description |
|---|---|---|
| `gnssrefl_direct_signal_poly_order` | integer | Order of the polynomial fit used to remove the antenna's own direct-signal gain trend from the raw data before the real reflection analysis begins. gnssrefl's own default is 4. A narrow elevation mask gives each pass less raw data to fit against, which can make this default numerically unstable — if you see a `RankWarning: The fit may be poorly conditioned` message in your processing output, lowering this (e.g. to `2`) is the standard, documented fix. |

---

## External storage (optional)

| Field | Type | Description |
|---|---|---|
| `external_storage_path` | string (path) | If set, each day's raw data and processed results are automatically moved here after processing, during continuous unattended operation. Leave unset to disable this entirely — everything stays local. |
| `manager_retry_delay_seconds` | number | How long to wait before retrying after a failed recording chunk, during continuous operation. Default `60.0`. |
| `health_check_interval_seconds` | number | How often to record a system health snapshot (disk space, CPU, memory) during continuous operation. Default `3600.0` (1 hour). |
| `export_retry_window_days` | integer | How many days to keep retrying a day's export to external storage before giving up (this exists because a day's orbit product routinely isn't published for 1-2 days after the fact, so an immediate export would predictably fail). Default `3`. |

---

## A complete, real example

This is a genuine, working configuration (with the identifying details
changed), showing which fields a real deployment actually used:

```json
{
    "station_id": "USGS001",
    "station_name": "Example Station",
    "agency": "USGS",
    "observer": "J. Researcher",
    "receiver_model": "Unicore UM980",
    "receiver_firmware": "",
    "receiver_port": "/dev/USB_GPS",
    "receiver_baud": 115200,
    "receiver_timeout": 2.0,
    "latitude": 41.8928243333,
    "longitude": -69.9633227139,
    "height": -10.025,
    "marker_name": "USGS001",
    "marker_number": "USGS001",
    "rinex_version": "3.05",
    "antenna": {
        "model": "AS-ANT3B-BUDSUR-L1L2L5-25SMA",
        "serial": "",
        "height": 0.0,
        "east_offset": 0.0,
        "north_offset": 0.0
    },
    "record_raw_chunk_seconds": 600,
    "gnssrefl_sample_rate": 1,
    "gnssrefl_all_frequencies": true,
    "gnssrefl_monument_number": "00",
    "gnssrefl_country_code": "usa",
    "gnssrefl_elevation_min": 5.0,
    "gnssrefl_elevation_max": 15.0,
    "gnssrefl_reflector_height_min": -0.5,
    "gnssrefl_reflector_height_max": 5.0,
    "gnssrefl_orthometric_height": 18.665,
    "gnssrefl_max_arc_minutes": 40.0,
    "gnssrefl_elevation_span_tolerance": 1.0,
    "gnssrefl_direct_signal_poly_order": 2,
    "gnssrefl_azimuth_regions": [353, 360, 0, 173],
    "external_storage_path": "/mnt/external_storage/GPS_Data"
}
```

Notice this real example uses a narrow, non-default elevation mask
(5–15°) and correspondingly tightens `gnssrefl_elevation_span_tolerance`
(to `1.0`) and lowers `gnssrefl_direct_signal_poly_order` (to `2`) —
exactly the paired adjustment described above for a narrow window. If you
copy this file as a starting point for your own station, **change the
location fields first** — everything else can reasonably stay at these
values until you have a specific reason to change them.
