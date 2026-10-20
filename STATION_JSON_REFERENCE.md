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
| `gnssrefl_azimuth_regions` | list of numbers (degrees) | Which compass directions have a genuinely clear view, as a flat list of region boundaries. Default (unset) is the full circle, `[0, 360]`. **Worth measuring rather than guessing** -- see the note below. |

If part of your view is blocked (a building, trees, rising terrain), list
only the clear region(s). For example, `[0, 150, 180, 360]` analyzes
0°–150° and 180°–360°, excluding the 150°–180° range. This can wrap
through north: `[353, 360, 0, 173]` analyzes 353°–360° and 0°–173°
(i.e., roughly northwest through south, wrapping across due north).

### Tuning the azimuth mask against a reference

An azimuth mask set from a map, or from what looks like open water in
satellite imagery, is a reasonable starting point but rarely the best
available. Arcs whose reflection point lands on wet sand, a bar, or a
shoreline slope still return a plausible reflector height -- they do
not fail, they quietly disagree with the arcs that are genuinely
seeing water.

If you have an independent water-level reference (a tide gauge, or a
tide model), that disagreement is measurable, and the mask can be
optimized rather than assumed.

`analysis_tools/elevation_quality.py` compares each arc against the
consensus of all arcs and groups the disagreement by azimuth, which
shows directly which bearings are contributing error. On this
project's own station the result was stark:

| Azimuth band | Mean disagreement |
|---|---|
| 30-60 deg | 0.160 m |
| 60-120 deg | ~0.170 m |
| 120-150 deg | 0.719 m |
| 150-175 deg | 1.253 m |

Trimming the mask from `[353, 360, 0, 173]` to `[353, 360, 0, 130]`
discarded 9% of arcs and improved agreement against an independent
tide model by 18%:

| | Before (to 173 deg) | After (to 130 deg) |
|---|---|---|
| Mean absolute deviation | 9.2 cm | 7.5 cm |
| Correlation | 0.9884 | 0.9918 |
| Internal spline RMS | 0.206 m | 0.105 m |

Cuts at 110, 120 and 130 degrees all performed identically, so 130
was chosen as the one retaining the most data -- more arcs is useful
insurance on days with poor satellite geometry even when it does not
change the average.

Two cautions. First, tuning against a single reference risks fitting
that reference's own quirks; here the result was corroborated
independently by an azimuth-independence test and by the site
geometry, which is what made it trustworthy. Second, **the mean
offset moved 3.6 cm** when the mask changed, because the discarded
arcs were biased. Any absolute datum calibration must be redone
after changing the mask.

To test a mask without touching production, build an isolated config
with `make_gnssir_input(..., extension="myTest")` and reprocess with
`gnssir <station> <year> <doy> -extension myTest`. Nothing in the
production results directory is affected.

### Quality control thresholds

| Field | Type | Description |
|---|---|---|
| `gnssrefl_peak2noise` | number | Minimum peak-to-noise ratio for a retrieval to be accepted. gnssrefl's own default is roughly 2.8. Raising this is stricter (fewer, higher-confidence retrievals); lowering it is more permissive. |
| `gnssrefl_amplitude_min` | number | Minimum signal amplitude for a retrieval to be accepted. gnssrefl's own default is roughly 5.0. |
| `gnssrefl_amplitude_min_by_frequency` | object, frequency code → number | Optional, unset by default. One amplitude threshold per GNSS frequency, e.g. `{"1": 6.5, "20": 4.5}`; frequencies not listed keep `gnssrefl_amplitude_min`. See "Arc-level QC (opt-in)" below. |

### Arc-level QC (opt-in)

All four keys here are unset by default. Unset, the json gnssrefl writes
is byte for byte what it was before they existed, and the water level is
unchanged. **Try them with `analysis_tools/evaluate_qc_variants.py`
before switching any on** (below). They change the water levels in
storms, but never the format of any plot.

**Why they exist** (known finding G-B9). This project's own QC
(`water_level_qc.py`, and the waterline pipeline's `gnssr_qc.py`) sees
only gnssrefl's 30-minute spline, never the individual arcs. Inside
gnssrefl, every arc test is relative to the data themselves:

- gnssir's amplitude and peak-to-noise limits;
- subdaily's 2.5-sigma clip per UTC day (the day's spread is mostly tide,
  so it rarely fires);
- a 3-sigma clip against a first spline, where sigma is one plain
  standard deviation over the whole ~10-month fit.

A rough sea lowers the coherent reflected power, roughly by
exp(-4k²s²sin²e) for a surface height spread s (Strandberg et al. 2019).
Purnell et al. (2020) find that the dynamic sea surface dominates the
error budget, and Sepúlveda et al. (2023) see errors rise abruptly at
high waves. So in a storm, arcs drop below the amplitude limit, and the
ones that survive can be the biased ones. In the 25–27 Sep 2026 storm
the spline read up to ~3 m above what the Chatham gauge implied.

A synthetic reproduction of that storm with gnssrefl's real subdaily
found that a *sustained* bias is not removed by absolute spline limits
(0.30/0.25 m) plus `alt_sigma`. The storm high-water error stayed at
+1.65 m against +1.68 m, and 12% of calm arcs were lost: the limits are
relative to a spline that follows the bias. Whether these keys help on
this station's *real* arcs is the question the evaluation script answers.

| Field | Type | Description |
|---|---|---|
| `gnssrefl_subdaily_alt_sigma` | boolean | subdaily's part-1 clip (2.5 sigma per UTC day) uses the median and 1.4826 × the median absolute deviation, a robust sigma that gnssrefl credits to Felipe Geremia-Nievinski, instead of the mean and standard deviation (gnssrefl `-alt_sigma`). |
| `gnssrefl_subdaily_spline_outlier1_m` | number (metres) | Fixed limit for the clip against the FIRST spline (before the RHdot and inter-frequency corrections), instead of 3 × the whole fit's residual RMS (gnssrefl `-spline_outlier1`). This clip sets which arcs the published spline is fitted on. **Metres, not sigmas**: `3` would mean 3 m, i.e. no QC at all. |
| `gnssrefl_subdaily_spline_outlier2_m` | number (metres) | The same for the second clip, after the RHdot and inter-frequency corrections (`-spline_outlier2`). It decides the edit and outlier files and where gaps fall; the published spline is fitted on the arcs that survived the first clip. |
| `gnssrefl_amplitude_min_by_frequency` | object | One LSP amplitude threshold per frequency in the gnssir json's `reqAmp`. gnssir keeps an arc only if its amplitude is *above* its frequency's value. `make_gnssir_input` can only write one value for every frequency. The amplitudes of different signals are not comparable (transmit power, antenna gain at low elevation and receiver tracking differ by constellation and band), and gnssrefl itself calls the threshold "receiver and elevation angle region dependent". Keys are gnssrefl frequency codes: 1 GPS L1, 20 L2C, 5 L5, 101/102 GLONASS, 201–208 Galileo, 301–308 BeiDou. A code the station does not analyse is ignored with a warning. |

How they reach gnssrefl:

- The three `gnssrefl_subdaily_*` keys are written into the gnssir json's
  `subdaily_*` fields (`station/gnssrefl_processor.py`, at every
  initialize). `analysis_tools/gnss_record.py` reads them back and puts
  them on subdaily's command line each night.
  - That is needed: gnssrefl 4.1.5's subdaily never reads `alt_sigma`
    from its json. `subdaily()` defaults it to `False`, and the json is
    consulted only when it is `None` (confirmed against the source and a
    run). The spline-outlier fields are read, `alt_sigma` silently is not.
  - On the command line they are also part of the settings a frozen year
    is checked against. Switching one on therefore refits the whole
    record once, frozen years included.
  - `ultra_rapid_check.py` does the same.
- `gnssrefl_amplitude_min_by_frequency` rewrites `reqAmp` after
  `make_gnssir_input` has written the file. It acts in gnssir, so only on
  days processed from then on. Days already processed keep the threshold
  they were made with, until gnssir is re-run on them:
  `gnssir usgs <year> <doy> -doy_end <doy>`, with gnssrefl_venv active,
  while their SNR files are still there.
  - A raised threshold drops arcs at once, so `station_health.py`'s arc
    check may warn of a drop in arcs for up to a week afterwards.
- Every change takes effect at the next nightly run that processes new
  data, because Step 2 of `process_and_plot.sh` rewrites the gnssir json.
  - Re-running `./setup_station.sh` rebuilds station.json from its
    questions, which drops these keys; add them back by hand afterwards.

**Choosing per-frequency amplitudes.**
`analysis_tools/amplitude_thresholds.py` reads each frequency's
amplitude (column 7 of `results/<doy>.txt`) over a calm window, inside
the azimuth window the water level uses. It proposes

    threshold_f = fraction × median(Amp_f)

That keeps an arc if its amplitude is at least that fraction of what its
own signal gives in calm weather, which is the same relative damping for
every signal.

- The default fraction is the station's present overall strictness,
  (current threshold) / (median amplitude of every frequency together).
  The thresholds are redistributed between signals, not tightened as a
  whole. `--fraction` sets a different value.
- A frequency with fewer than 30 arcs in the window keeps its current
  value.
- The results file holds only arcs above today's threshold, so its
  histogram is cut off there. gnssrefl 4.1.5 also writes every rejected
  arc to `results/<sta>/failQC/<doy>.txt`, and the arcs there that failed
  on amplitude alone complete the histogram. They are the ones with a
  computed amplitude and peak-to-noise ratio, a peak-to-noise above the
  json's limit, a peak not within 0.10 m of the reflector-height limits,
  and an amplitude at or below the threshold. `--no-failqc` leaves them
  out.

```bash
python3 analysis_tools/amplitude_thresholds.py --station usgs \
    --from 2026-09-08 --to 2026-09-14 --json thresholds.json
```

**Trying them first: `analysis_tools/evaluate_qc_variants.py`.** This
runs gnssrefl's real subdaily once per variant, each in its own gnssrefl
`-extension` and in a private REFL_CODE (`products/refl_code_qc_eval`).
The production REFL_CODE is only read.

- **What each run sees.** Each run uses the production results, the
  production gnssir json plus the variant's keys, and last night's
  subdaily settings (from `record_status.json`). By default it covers the
  span the nightly fit covers, so the 3-sigma limits are the ones
  production has. A per-frequency amplitude variant gets exactly the arcs
  gnssir would have kept with those thresholds: stricter limits drop
  arcs, and looser ones bring back failQC arcs that failed on amplitude
  alone.
- **The windows.** The storm defaults to 25–27 Sep 2026. The calm week
  is either given with `--calm`, or the 7 results days with the lowest
  mean buoy Hs within 60 days of the storm and at least 3 days away from
  it.
- **The reference.** Every variant is scored against one reference:
  level = a × Chatham gauge(t − lag) + b, fitted with
  `water_level_qc.fit_reference` on the baseline in the calm week. If
  there is no gauge data, the tide model in `tide_model_file` is used
  the same way.
- **What it reports, per window:**
  - readings, RMSE and bias, and `water_level_qc.run_qc`'s flags;
  - the arcs at each step: in the azimuth window, after part 1, after
    the first spline clip, after the second;
  - in the storm, also the RMS of what Stockdon et al. (2006) wave setup
    cannot explain, the bias at high water and the longest gap.
- **The variants:**
  - `baseline` (production);
  - `alt_sigma`;
  - `abs_outliers`: 3 × the robust scatter of the baseline's arcs about
    its spline in the calm week, i.e. what 3 sigma is when no storm is
    in it;
  - `review_outliers` (0.30/0.25 m);
  - `amp_by_freq` (the rule above);
  - `amp_half` (half each signal's calm median);
  - `combined`.
- **The recommendation.** A variant is recommended only if all of these
  hold:
  - its calm RMSE is within 5 mm of the baseline's;
  - it keeps ≥ 90% of the calm arcs;
  - it keeps ≥ 90% of the storm readings;
  - it opens no new gap of 6 h or more (a break in the plot);
  - it cuts the storm RMS beyond wave setup by ≥ 10%.

  Of the variants that pass, it picks the best and prints the
  station.json lines that switch it on. Otherwise it says to keep the
  current settings. The summary is also written to
  `products/refl_code_qc_eval/qc_eval_summary.json`.

```bash
cd /home/argus_user/GNSS/v4.1 && source gnssrefl_venv/bin/activate
python3 analysis_tools/evaluate_qc_variants.py --station usgs
# a given calm week; a faster run over the windows only (+-3 days)
python3 analysis_tools/evaluate_qc_variants.py --station usgs \
    --calm 2026-09-08 2026-09-14 --span windows
rm -rf products/refl_code_qc_eval      # when done
```

Two cautions:

- **One storm is a small sample.** Run the script again after the next
  storm before relying on a setting.
- **The gauge-scaled reference comes from the GNSS-IR itself.** The fit
  of a, lag and b uses the station's own calm-week readings, so it
  checks consistency rather than absolute accuracy. The question it
  answers is "does the storm depart less from the calm-weather
  relation?"

References: Larson et al. (2017), *J. Atmos. Oceanic Technol.* 34, the
subdaily spline and RHdot method; Larson (2024), *GPS Solutions* 28:165,
gnssrefl; Strandberg et al. (2019), *GPS Solutions* 23; Purnell et al.
(2020), *IEEE JSTARS* 13; Sepúlveda et al. (2023), *Earth Space Sci.*
10; Stockdon et al. (2006), *Coastal Eng.* 53. The robust sigma is
1.4826 × MAD, the standard normal-consistent scale.

### Orthometric height reference

| Field | Type | Description |
|---|---|---|
| `gnssrefl_orthometric_height` | number (meters) | Your antenna's height above a specific vertical datum. If set, gnssrefl reports real, absolute water level (Hortho minus reflector height) on this datum instead of just a relative reflector height. **This system's standard is NAVD88**: use an NGS OPUS solution (NAVD88 with GEOID18). Do NOT use the orthometric height from a CSRS-PPP report -- that is CGVD2013, the Canadian height system, and at Marconi it is 0.349 m below NAVD88 (18.665 vs 19.014 m). |
| `vertical_datum` | string | The datum `gnssrefl_orthometric_height` (and so every water level) is on, e.g. `"NAVD88 (GEOID18)"`. Read by `analysis_tools/station_datum.py`; set by `maintenance/migrate_to_navd88.py`. |
| `water_level_msl_offset` | number (meters) | Height of local mean sea level in the same datum as the water levels (about +0.1 m NAVD88 here). The public 7-day plot subtracts it to draw levels above mean sea level; every other product stays NAVD88. Moves with `gnssrefl_orthometric_height` if that ever changes datum. |
| `tide_model_navd88_offset` | number (meters) | Added to tide-model heights to put them on NAVD88 (default +0.09: the model ensemble sits ~0.09 m below NAVD88 here). |
| `gnss_record_first_day` | string `"YYYY-MM-DD"` | Optional. Results before this day are left out of the whole-record water level (`analysis_tools/gnss_record.py` reads it itself), e.g. installation tests or an earlier antenna setup in an older `$REFL_CODE/<year>` folder, and `process_and_plot.sh` Step 1 does not recover days before it. Unset: every year's results count. Setting it later than the record's start shortens the record at the next run, on purpose (no `--allow-shrink` needed); the waterline cron then refuses the later start until its `archive/gnssr_record_start.txt` is deleted. |

All water-level products are NAVD88. `analysis_tools/station_datum.py`
is the single place that is defined; a spline file written with an
older antenna height is moved onto NAVD88 when it is read or archived,
using the `Hortho (m) is` line gnssrefl writes into its header.

### Refraction model

| Field | Type | Description |
|---|---|---|
| `gnssrefl_refraction_model` | integer | Which tropospheric refraction correction to apply, 0-6. It is passed to gnssrefl as `refr_model` (a string); `0` turns the correction off (`refraction: false`). `1` (gnssrefl's own default) is the standard Bennett correction; `6` is MPF (Williams & Nievinski 2017). gnssrefl exits on a number above 6. Leave unset unless you have a specific, understood reason to change it. |

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

### Choosing the reflector height window

`gnssrefl_reflector_height_min` / `_max` are not merely a search
hint. They are a hard limit on the water level that can be retrieved
at all: anything outside is discarded during retrieval, with no error
and no gap marker. A window that is too narrow does not degrade
gracefully -- it silently deletes the extremes.

Convert the window to water levels before trusting it:

    water level = gnssrefl_orthometric_height - reflector height

At this station (Hortho then 18.665 m, the old CGVD2013 height) a
17-23 m window meant a ceiling of +1.665 m, against an observed maximum
of +1.565 m. Ten centimetres of
headroom. A modest storm surge would have truncated the record
exactly when it mattered, and the plot would simply have shown a gap.

Widening is not free, though, and the failure mode is subtle. A
window extended too far admits spurious spectral peaks near its edge:
plausible-looking reflector heights that are not water. Testing three
windows against an independent tide model across the full record:

| Window | Arcs | Internal RMS | Mean abs dev | Correlation | Artifacts |
|---|---|---|---|---|---|
| 17-23 m | 6454 | 0.105 m | 6.8 cm | 0.9933 | ~0 |
| **16-24 m** | 6962 | 0.113 m | **6.8 cm** | 0.9931 | 63 (0.9%) |
| 15-24 m | 6893 | 0.131 m | -- | -- | 403 (6.2%) |

16-24 was adopted: no measurable cost, and the ceiling rises to
+2.665 m, about 1.1 m above the observed maximum.

15-24 was rejected. Its 403 extra retrievals showed water levels of
+2.7 to +3.4 m, scattered across the tidal cycle on days with no
storm, with simultaneous arcs disagreeing by centimetres. Real water
level is consistent across arcs and occurs at high tide; these were
neither.

Two things worth carrying forward. First, **the averaged agreement
figure will not reveal this problem** -- 15-24 m looked acceptable
until the individual retrievals were examined, because a small
fraction of bad points barely moves a mean. Look at the extremes
directly. Second, during an actual surge, genuine high water and edge
artifacts occupy the same band and are hard to separate; a wide
window is still the right choice, but extreme values during a storm
deserve scrutiny rather than assumption.


## Tide model comparison (optional)

If configured, `process_and_plot.sh` automatically generates a comparison
plot and agreement statistics (correlation, mean deviation) against an
independent tide model every time it runs. Set up during `./install.sh`
(which reads the real column headers from your file and lets you pick
one, rather than asking you to type a column name blind), or by hand.

| Field | Type | Required? | Description |
|---|---|---|---|
| `tide_model_file` | string (path) | Optional | Full path to an `.xlsx` file with a timestamp column and one or more tide-height columns. Leave unset (or omit entirely) to disable this feature completely -- most sites won't have one, and that's fine. |
| `tide_model_value_column` | string | Required if `tide_model_file` is set | The column name containing the tide height values to compare against, e.g. `"EOT20_heightm"`. A single file may have several candidate columns (one per tide model); pick whichever is most appropriate for your site. |
| `tide_model_time_column` | string | Optional | The column name containing timestamps. Default `"time"`. |

## External storage (optional)

| Field | Type | Description |
|---|---|---|
| `external_storage_path` | string (path) | If set, each day's raw data is moved here after processing, during continuous unattended operation, and whatever changed in `products/` since the last export is copied here (products/ itself is never moved or deleted: it holds the whole water-level record). Leave unset to disable this entirely — everything stays local. |
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
    "gnssrefl_orthometric_height": 19.014,
    "vertical_datum": "NAVD88 (GEOID18)",
    "gnssrefl_max_arc_minutes": 40.0,
    "gnssrefl_elevation_span_tolerance": 1.0,
    "gnssrefl_direct_signal_poly_order": 2,
    "gnssrefl_azimuth_regions": [353, 360, 0, 130],
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
