# An independent datum for the water levels

The water level itself is NAVD88 throughout: the antenna's OPUS height
minus the reflector height. Two numbers in `station.json` sit beside
it, though, and both were measured against the GNSS-IR record itself:

| Field | Value | What it does | How it was set |
|---|---|---|---|
| `tide_model_navd88_offset` | +0.09 m | puts the tide model on NAVD88 for every comparison | mean GNSS-IR minus model over a 47-day overlap (`station_datum.py`) |
| `water_level_msl_offset` | ~+0.10 m | the zero of the public 7-day plot | not recorded; its value and history point the same way |

An offset measured against the GNSS-IR absorbs whatever absolute error
the GNSS-IR had at the time. A later comparison made through it cannot
see that error (review finding B11). The way out is a height for mean
sea level on NAVD88 at Marconi that does not come from the GNSS-IR.
`analysis_tools/independent_datum.py` puts one together and sets both
offsets against it.

**It is a report.** It changes nothing in `station.json` and nothing
in any product or plot. What to do with the result is your decision
(see "Deciding" at the end).

---

## Running it on the station

The station already reaches NOAA every night (`water_level_qc.py`
downloads the Chatham gauge), so run it there:

```bash
cd /path/to/this/repository           # the folder holding process_and_plot.sh
source gnssrefl_venv/bin/activate
python3 analysis_tools/independent_datum.py \
    --cache products/refl_code/Files/usgs/independent_datum_cache.json \
    --json  products/refl_code/Files/usgs/independent_datum.json
```

- `--cache` keeps what NOAA returned, with the date. A later run that
  cannot reach NOAA uses it before the built-in values.
- `--json` writes the whole report, for the record.
- `--window START END` (dates, `YYYY-MM-DD`) gives the overlap that
  `tide_model_navd88_offset` was measured in. That offset is a mean
  over a few weeks, and sea level has a seasonal cycle of a few cm.
  With the window, the offset is compared with mean sea level over the
  same days, from Chatham's monthly means.
- `--offline` uses no network at all.

It takes a minute or two: about twenty small requests, each with a
30-second timeout. A host that does not answer is tried once, not once
per request.

Please send back the printed report, or the `--json` file.

---

## Where the numbers come from

**NOAA CO-OPS tidal datums.** These are the published datums of tide
stations levelled to NAVD88. MSL − NAVD88 is the difference of the two
published heights. The stations, all IDs checked against NOAA's bench
mark sheets:

| ID | Station | Used for |
|---|---|---|
| 8447435 | Chatham, Lydia Cove | the estimate when VDatum does not answer; the monthly means |
| 8446121 | Provincetown | spread around Marconi |
| 8447495 | Saquatucket Harbor (historical) | spread around Marconi |
| 8449130 | Nantucket Island | regional picture; Chatham's control station |
| 8447930 | Woods Hole | regional picture |
| 8443970 | Boston | regional picture |

On the station, the tool also prints each station's name and distance
from NOAA's own metadata, so a wrong ID shows.

**NOAA VDatum.** This is NOAA's datum-transformation model. The tool
asks it for 0 m on LMSL, MHW and MLW, converted to NAVD88, at three
points: the antenna, the centre of the reflection zone (about 108 m
out at 61°), and 1 km offshore. VDatum's tidal grids stop at the
shoreline, so the antenna on its bluff will probably get no answer.
The two seaward points are the ones that count. VDatum models the open
coast at Marconi itself, so its answer is used as the estimate
whenever it gives one.

**The epoch.** Tidal datums are means over the National Tidal Datum
Epoch, 1983–2001 (Gill & Schultz 2001). Sea level has risen since.
"Present" mean sea level is the epoch value plus Chatham's mean
monthly sea level over the last 12 whole months. Each of those monthly
values is already a departure from the epoch mean, and averaging a
whole year cancels the seasonal cycle.

When the monthly means cannot be read, a linear trend stands in. The
default is Nantucket's 4.01 mm/yr. The range shown runs to Boston's
2.94 mm/yr.

**Offline values.** These are used when NOAA cannot be reached. They
are stored in the script with their source and the date they were
read.

### What happened from the machine this was written on (20 Oct 2026)

None of NOAA's hosts could be reached from the build sandbox:

- `api.tidesandcurrents.noaa.gov` and `vdatum.noaa.gov` (and the
  `tidesandcurrents`, `ngdc` and `ngs` web hosts) were refused by its
  network proxy (HTTP 403 on CONNECT).
- A web fetch could not resolve the host names.

Because of that:

- **The offline values were read from NOAA's published bench mark
  sheets, through the text a web search returned.** Chatham's MSL and
  NAVD88 also agree with the values already in the camera repository
  (`compare_gnssr_to_gauge.py`).
- **The request formats were checked against working clients rather
  than against the live services.** For VDatum that was NOAA's own
  `vyperdatum` package (`vdatum_rest_utils.py`) and two other
  open-source clients. For CO-OPS it was recorded `datums.json`
  answers and the `monthly_mean` field names used by the `searvey`
  package.
- **There is no offline VDatum value.** No number could be read from
  VDatum, and the tool does not make one up.

The first run on the station is therefore the first live test. The
tests (`tests/test_independent_datum.py`) feed the tool answers shaped
like the real ones, with no network.

| Offline value (heights above MLLW, m) | MSL | NAVD88 | MSL − NAVD88 | MHW − NAVD88 | MLW − NAVD88 | Source |
|---|---|---|---|---|---|---|
| 8447435 Chatham, Sep 2015–Aug 2017 | 0.778 | 0.760 | **+0.018** | +0.704 | −0.710 | bench mark sheet, published 2020-05-20; datum error 0.012 m |
| 8447435 Chatham, 3 months of 2000 (superseded 2003) | 0.771 | 0.816 | −0.045 | – | – | superseded sheet; shown, never used |
| 8447495 Saquatucket Harbor, May–Sep 1993 | 0.682 | 0.788 | −0.106 | +0.432 | −0.703 | bench mark sheet, published 2003-04-21 |

The two Chatham determinations differ by 6 cm, and Saquatucket is 12 cm
from Chatham. MSL − NAVD88 is not one number around the outer Cape.
That is why the tool reports the spread of the local sources as a
"transfer" uncertainty, and why VDatum at the site is worth having.

### What the offline numbers say

```
Present MSL at Marconi (from Chatham): +0.156 m NAVD88   (+/-0.066 m, 1 sigma)
    epoch +0.018, plus +0.138 m of sea-level rise since 1992.5
    (+0.101 m at Boston's trend)
water_level_msl_offset     +0.100  ->  -0.056 m against it
tide_model_navd88_offset   +0.090  ->  -0.066 m against it
```

Both offsets come out **below** the independent estimate, but by less
than its own uncertainty. A GNSS-IR absolute bias of a few cm either
way is not ruled out, and nothing here shows one.

Two things change the earlier reading of B11:

- **The sea-level rise since the epoch is 10–14 cm, not "a few cm".**
  Against the epoch value alone (+0.018 m), the plot's zero looked
  8 cm high. Against present sea level it is 3–6 cm low.
- **Chatham is a back-barrier harbour.** Its inlet damps the low tides,
  which raises its mean level a few cm above the open coast. That
  pushes the Marconi value down by about the same amount.

The station's live run (VDatum at the reflection zone, plus Chatham's
real monthly means) replaces both approximations.

---

## Measuring the bias directly: a co-located gauge

A datum transferred from a harbour 23 km away can only be good to a
few cm. The published GNSS-IR validations reach about 2 cm because
they compare against a gauge at the site:

- Larson et al. 2013a (Onsala, Friday Harbor) and 2013b (Kachemak
  Bay): daily mean sea level within 2.3 cm.
- Larson et al. 2017: ten years against the co-located NOAA gauge at
  Friday Harbor, with daily means at about 2 cm and monthly at 1.3 cm.
- Löfgren et al. 2014: five sites with co-located gauges.
- Purnell et al. 2020: for daily and monthly means, the spectral
  method this station uses is as good as inverse modelling, so daily
  means are the right unit to calibrate.

The same comparison here measures the one number B11 is about: the
GNSS-IR absolute bias.

### 1. The sensor

**A pressure logger with a barometer** is the practical choice on an
exposed beach. Moor it seaward of the low-tide terrace and outside the
surf zone, because wave setup inside the surf raises the mean level a
pressure sensor sees. The Signature 1000 ADCP deployed off Marconi from
Dec 2024 to Mar 2025 is the right kind of instrument. Its level was
only relative to its own mean, though: that is the part a datum tie
supplies.

**A staff or stilling pipe read by RTK** works only in calm weather on
this beach. Treat it as a spot check.

### 2. The tie to NAVD88

The tie is the whole point. The tool's last table shows what each kind
of tie can reach.

| Tie | 1σ | Notes |
|---|---|---|
| Levelled to an NGS bench mark | ~5 mm | The only tie that also checks the GEOID18 part of the antenna height (OPUS ±0.061 m, mostly geoid). |
| Static GNSS on the sensor's mark (OPUS; NGS 58/59 guidelines) | ~2 cm | Shares GEOID18 with the antenna height. It measures the GNSS-IR's own bias (reflector height, phase centre, refraction, sea state) but not the geoid. |
| RTK, repeated occupations | ~3 cm | Good for a first look, not for a ±2 cm answer. |

### 3. How long

At least **29 days**. That is a lunar month and two spring–neap cycles,
and it is NOAA's practice for datums at a short-term station (NOAA
2003). The statistics alone need far less: with a daily scatter of
2.5 cm and a levelled tie, a ±1 cm answer takes 9 days. The month is there because
GNSS-IR errors depend on tide range (the height-rate correction) and on
sea state, and those need to average out.

| Gauge tied by | ±0.01 m | ±0.02 m | ±0.03 m |
|---|---|---|---|
| levelling (0.005) | 29 d | 29 d | 29 d |
| static GNSS (0.02) | never | never | 29 d |
| RTK (0.03) | never | never | never |

"Never" means the tie alone is as uncertain as the target.

### 4. The analysis

1. Take daily means of complete days, from both the gauge and the
   GNSS-IR spline. Leave out days with QC failures or runup readings
   (`water_level_qc.py`).
2. The bias is the median daily difference (GNSS-IR minus gauge). Its
   error is the scatter over the square root of the number of days,
   combined with the tie error.
3. Break the bias down by significant wave height (buoy 44008) and by
   spring versus neap tides. A bias that grows with Hs is a sea-state
   effect (Purnell et al. 2020; Sepúlveda et al. 2023), not a datum
   error.
4. Compare M2, S2, N2, K1 and O1 amplitudes and phases (Larson et al.
   2017). This checks the tide shape independently of the datum.
5. From the gauge alone, compute Marconi's own tidal datums (MSL, MHW,
   MLW) for the epoch, by simultaneous comparison with Chatham (NOAA
   2003). This gives the open-coast MSL − NAVD88 that replaces the
   transfer from Chatham. Then bring it to the present with the same
   monthly-mean correction this tool uses.

The camera repository's `compare_gnssr_to_gauge.py` already does the
time-matched comparison against Chatham, in NAVD88, without any of the
offsets. Re-run it after any change to processing. It is the
continuing absolute check, and this tool is the datum check.

---

## Deciding (owner; nothing here is applied)

When a live run, and ideally a gauge deployment, gives a present-day
MSL at Marconi with an uncertainty you accept:

- **`water_level_msl_offset`** would become that MSL. On the public
  7-day plot, the blue level curve and every departure value would move
  by (old − new). The format would not change.
- **`tide_model_navd88_offset`** would become MSL over the tide model's
  overlap, or simply present MSL. The model's heights are zero-mean.
  This moves:
  - the internal `usgs_vs_tide.png` tide curve;
  - the "Mean offset" and "RAW RMS" lines of
    `compare_to_tide_deviation.py`;
  - the tide column of the monthly CSV.
- **Nothing else moves:** not the NAVD88 spline, the monthly water
  levels, the TWL export or the camera DEM.
- Record the source and date in `station.json` (a `_comment`) and in
  `station_datum.py`.
- Do not re-derive either offset from the GNSS-IR again.

---

## References

- Gill, S.K., Schultz, J.R. (2001). *Tidal datums and their
  applications.* NOAA Special Publication NOS CO-OPS 1.
- NOAA (2003). *Computational techniques for tidal datums handbook.*
  NOAA Special Publication NOS CO-OPS 2.
- Larson, K.M., Löfgren, J.S., Haas, R. (2013a). Coastal sea level
  measurements using a single geodetic GPS receiver. *Adv. Space Res.*
  51(8), 1301–1310. doi:10.1016/j.asr.2012.04.017
- Larson, K.M., Ray, R.D., Nievinski, F.G., Freymueller, J.T. (2013b).
  The accidental tide gauge: a GPS reflection case study from Kachemak
  Bay, Alaska. *IEEE GRSL* 10(5), 1200–1204.
- Larson, K.M., Ray, R.D., Williams, S.D.P. (2017). A 10-year
  comparison of water levels measured with a geodetic GPS receiver
  versus a conventional tide gauge. *J. Atmos. Oceanic Technol.* 34(2),
  295–307. doi:10.1175/JTECH-D-16-0101.1
- Löfgren, J.S., Haas, R., Scherneck, H.-G. (2014). Sea level time
  series and ocean tide analysis from multipath signals at five GPS
  sites in different parts of the world. *J. Geodyn.* 80, 66–80.
  doi:10.1016/j.jog.2014.02.012
- Purnell, D., Gomez, N., Chan, N.H., Strandberg, J., Holland, D.M.,
  Hobiger, T. (2020). Quantifying the uncertainty in ground-based
  GNSS-reflectometry sea level measurements. *IEEE JSTARS* 13,
  4419–4428. doi:10.1109/JSTARS.2020.3010413
- Sepúlveda, I., Cao, B., Haase, J.S., Murphy, M.J. (2023). Optimizing
  simultaneous water level and wave measurements from multi-GNSS
  interferometric reflectometry over 1 year at an exposed coastal
  site. *Earth Space Sci.* 10(6), e2022EA002767.
- Holden, L.D., Larson, K.M. (2021). Ten years of Lake Taupō surface
  height estimates using the GNSS interferometric reflectometry.
  *J. Geodesy* 95.
- Roesler, C., Larson, K.M. (2018). Software tools for GNSS
  interferometric reflectometry (GNSS-IR). *GPS Solutions* 22, 80.
  doi:10.1007/s10291-018-0744-8
- NGS 58 / NGS 59: NOAA National Geodetic Survey guidelines for
  GPS-derived ellipsoid heights (1997) and orthometric heights (2008).
