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
  same days, from Chatham's monthly means. The dates are not recorded
  anywhere; the older tide file named in `merge_tide_files.py`
  (`…_20260715-20260831.xlsx`) is a hint, not a record. Use the window
  only if the tide model was run without the seasonal SA and SSA
  constituents (EOT20 has them): a model that carries the season
  would have it counted twice.
- `--daily-sigma M` sets the daily GNSS-IR minus gauge scatter that
  the calibration table assumes (default 0.025 m, from sheltered
  sites; see "How long").
- `--offline` uses no network at all.

It takes a minute or two: twenty to thirty small requests, each with a
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
| 8446121 | Provincetown (Cape Cod Bay) | spread around Marconi |
| 8447495 | Saquatucket Harbor, Harwich Port (Nantucket Sound; historical) | spread around Marconi |
| 8449130 | Nantucket Island | regional picture; Chatham's control station |
| 8447930 | Woods Hole | regional picture |
| 8443970 | Boston | regional picture |

On the station, the tool also prints each station's name and distance
from NOAA's own metadata, so a wrong ID shows. It also asks NOAA for
every station with published datums and lists the ones within 30 km
that the table above leaves out (up to ten, nearest first), with their
datums. Those are shown but not counted, because many sit inside bays
and harbours whose mean level is not the open coast's. If one of them
is on the open coast, add it to `NEARBY_STATIONS` in the script.

**NOAA VDatum.** This is NOAA's datum-transformation model. The tool
asks it for 0 m on LMSL, MHW and MLW, converted to NAVD88, at three
points: the antenna, the centre of the reflection zone (about 108 m
out at 61°), and 1 km offshore. VDatum's tidal grids stop at the
shoreline, so the antenna on its bluff will probably get no answer.
The two seaward points are the ones that count. VDatum models the open
coast at Marconi itself, so its answer is used as the estimate
whenever it gives one, MHW and MLW included. Its uncertainty is then
measured, not borrowed from the harbours: the tool also asks VDatum
for MSL at each local CO-OPS station whose position NOAA returns, and
the RMS of VDatum minus the published value is the "VDatum model"
term. Half the spread over the VDatum points at the site is the other
term, with the epoch correction.

**The epoch.** Tidal datums are means over the National Tidal Datum
Epoch, 1983–2001 (Gill & Schultz 2001). Sea level has risen since.
"Present" mean sea level is the epoch value plus Chatham's mean
monthly sea level over the latest 12 consecutive months NOAA has
published. Each of those monthly values is already a departure from
the epoch mean, and averaging a whole year cancels the seasonal cycle.
NOAA publishes a monthly mean only once it is verified, a month or two
after the month ends, so the year used usually ends two or three
months back. The tool looks back up to 24 months for one, and prints
which months it used.

When the monthly means cannot be read, a linear trend stands in. The
default is Nantucket's 4.01 mm/yr (Chatham's control station). The
range shown runs to Boston's 2.94 mm/yr. A linear trend over a long
record is a lower bound for the rise since 1992: the rise has been
faster since the 1990s, and any one year can sit a few cm off the
trend. The ±σ (half the range) is a size, not an interval centred on
the truth.

The tool compares the epochs of the chosen source, of the monthly
means and of the spread. A source on another epoch (NOAA is moving to
2002–2020) is left out of the spread and named in a warning.

**Offline values.** These are used when NOAA cannot be reached. They
are stored in the script with their source and the date they were
read.

### What happened from the machine this was written on (20 Oct 2026)

None of NOAA's hosts could be reached from the build sandbox:

- `api.tidesandcurrents.noaa.gov` and `vdatum.noaa.gov` (and the
  `tidesandcurrents`, `ngdc` and `ngs` web hosts) were refused by its
  network proxy (HTTP 403 on CONNECT).
- A web fetch could not resolve the host names.
- A second try some hours later, after the tool was revised, failed
  the same way.

Because of that:

- **The offline values were read from NOAA's published bench mark
  sheets, through the text a web search returned.** Chatham's MSL and
  NAVD88 also agree with the values already in the camera repository
  (`compare_gnssr_to_gauge.py`). Saquatucket's agree, within the
  rounding, with NOAA's Inundation Analysis Tool page for the station,
  which gives them in feet above MHHW to 0.01 ft. The months of the
  superseded 3-month Chatham series could not be confirmed; that sheet
  was published 30 Jun 2000.
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

MHW and MLW below are each harbour's own tide. Chatham's inlet damps
the tide, low waters most, so they are not Marconi's (whose lows were
about −1.0 m against Chatham's −0.5 m in Sep 2026, camera
`compare_gnssr_to_gauge.py`). The tool prints them apart from the
Marconi estimate, with no uncertainty; only VDatum gives MHW and MLW
at Marconi.

| Offline value (heights above MLLW, m) | MSL | NAVD88 | MSL − NAVD88 | MHW − NAVD88 | MLW − NAVD88 | Source |
|---|---|---|---|---|---|---|
| 8447435 Chatham, Sep 2015–Aug 2017 | 0.778 | 0.760 | **+0.018** | +0.704 | −0.710 | bench mark sheet, published 2020-05-20; datum error 0.012 m |
| 8447435 Chatham, 3-month series, sheet of 2000 (superseded 2003) | 0.771 | 0.816 | −0.045 | – | – | superseded sheet; shown, never used |
| 8447495 Saquatucket Harbor, May–Sep 1993 | 0.682 | 0.788 | −0.106 | +0.432 | −0.703 | bench mark sheet, published 2003-04-21 |

The two Chatham determinations differ by 6 cm, and Saquatucket is 12 cm
from Chatham. None of the local stations is on the open Atlantic
coast: Chatham is a back-barrier harbour, Provincetown is on Cape Cod
Bay and Saquatucket (Harwich Port) on Nantucket Sound, a different
water body and tidal regime. Almost all of the 6 cm between the
Chatham sheets is in the NAVD88 tie (0.816 → 0.760 m above MLLW),
while MSL moved 0.771 → 0.778 m. So the spread is as much the
uncertainty of the stations' NAVD88 ties as of the tide. The tool
reports half of it as the "transfer" term when a station is the
estimate, and it stands in for both. It is not used when VDatum is the
estimate, which is why VDatum at the site is worth having.

### What the offline numbers say

```
Present MSL at Marconi (from Chatham): +0.156 m NAVD88   (+/-0.066 m, 1 sigma)
    epoch +0.018, plus +0.138 m of sea-level rise since 1992.5
    (+0.101 m at Boston's trend)
    budget: published datum 0.012, epoch correction 0.018, transfer 0.062
    (no MHW or MLW at Marconi: Chatham's are its harbour's)
water_level_msl_offset     +0.100  ->  -0.056 m against it
tide_model_navd88_offset   +0.090  ->  -0.066 m against it
```

Both offsets come out **below** the independent estimate, but by less
than its own uncertainty. A GNSS-IR absolute bias of a few cm either
way is not ruled out, and nothing here shows one.

Two things change the earlier reading of B11:

- **The sea-level rise since the epoch is 10–14 cm, not "a few cm".**
  Against the epoch value alone (+0.018 m), the plot's zero looked
  8 cm high. Against present sea level it is 2–6 cm low.
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

- Larson et al. 2013a (Onsala, Friday Harbor): single retrievals
  within 4.8 cm RMS at Onsala and about 10 cm at Friday Harbor.
- Larson et al. 2013b (Kachemak Bay): daily mean sea level within
  2.3 cm.
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

The logger's own level is good to about 1–2 cm over a deployment: the
barometric correction (0.5–1 hPa, 5–10 mm), the seawater density that
turns pressure into depth (0.1–0.3 % of a few metres, 3–10 mm) and
sensor drift. The tool takes 1 cm (`GAUGE_SIGMA_M`), the better end:
a barometer beside it, the density measured (CTD cast or the logger's
own temperature and a salinity), drift checked before and after. This
term does not average down with more days.

**A staff or stilling pipe read by RTK** works only in calm weather on
this beach. Treat it as a spot check, or as the tie below.

### 2. The tie to NAVD88

The tie is the whole point. A moored logger is never dry, so nothing
on it can be levelled or occupied with GNSS. Its zero has to come from
the water surface beside it, at times when that surface is flat:

| Tie (`TIE_OPTIONS`) | 1σ | How |
|---|---|---|
| Calm slack-water sessions, 8 of them | ~9 mm | At high or low slack water in calm weather (Hs under about 0.5 m, buoy 44008), read the still-water level at the water's edge against NAVD88 for 10–15 min: a staff levelled from an NGS bench mark, or an RTK pole. Each session gives the logger's zero to about 2.5 cm; 8 sessions average to about 9 mm. Log the times to the second; the logger's level is taken over the same minutes. |
| One such session | ~2.5 cm | A first look, not a ±2 cm answer. |
| Pipe mount levelled from an NGS mark | ~5 mm | A logger in a pipe on the low-tide terrace, its mount levelled from a bench mark at spring low water. It reads only when covered, and it is inside the surf at times: use calm days only. |

A GNSS buoy (PPK) moored beside the logger in calm spells is another
way to read the surface; its per-session error should be measured
before it is put in the table. Levelling from a bench mark is the only
tie that also checks the GEOID18 part of the antenna height (OPUS
±0.061 m, mostly geoid); an RTK tie shares GEOID18 with the antenna
and so measures the GNSS-IR's own bias (reflector height, phase
centre, refraction, sea state) but not the geoid.

### 3. How long

At least **29 days**. That is a lunar month and two spring–neap cycles,
and it is NOAA's practice for datums at a short-term station (NOAA
2003). The month is there because GNSS-IR errors depend on tide range
(the height-rate correction) and on sea state, and those need to
average out.

The statistics need n = σ_daily² / (target² − σ_tie² − σ_gauge²)
days. The default σ_daily, 2.5 cm, comes from sheltered sites (Kachemak
Bay, Friday Harbor, Lake Taupō). Marconi is exposed: its reflection
zone (about 70–220 m out at 5–15°) lies around the surf zone, where
wave setup moves the mean level with Hs, and errors persist through a
storm. At an exposed site the retrievals scatter far more (Sepúlveda
et al. 2023, Scripps Pier: 9–18 cm RMS). Run the tool with
`--daily-sigma` at a value from this station's own record, for example
the RMS of de-meaned daily GNSS-IR minus Chatham daily means; the days
grow as its square.

| Gauge zero tied by (σ_gauge 0.01 m) | ±0.01 m | ±0.02 m | ±0.03 m |
|---|---|---|---|
| pipe mount, levelled (0.005) | never | 29 d (3 at σ_daily 0.025; 10 at 0.05) | 29 d |
| 8 slack-water sessions (0.009) | never | 29 d (3; 12 at 0.05) | 29 d |
| 1 slack-water session (0.025) | never | never | 29 d (4; 15 at 0.05) |

"Never" means the tie and the gauge's own error alone reach the
target. A ±1 cm answer needs a gauge better than 1 cm, which a
pressure logger on this beach is not.

### 4. The analysis

1. Take daily means of complete days, from both the gauge and the
   GNSS-IR spline. Leave out days with QC failures or runup readings
   (`water_level_qc.py`).
2. The bias is the mean daily difference (GNSS-IR minus gauge). Its
   error is the scatter over the square root of the number of days,
   combined with the tie error and the gauge's own. (A median resists
   outliers better, but its error is about 1.25 times larger.)
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
- **`tide_model_navd88_offset`** would become present annual MSL. Not
  MSL over one window: for a model without the seasonal SA and SSA
  constituents, that would put one window's seasonal anomaly into
  every other month's comparison, the monthly CSV's tide column
  included; and a model run with them (EOT20 has them) already
  carries the season. Check which it was before using `--window`.
  This moves:
  - the internal `usgs_vs_tide.png` tide curve;
  - the "Mean offset" and "RAW RMS" lines of
    `compare_to_tide_deviation.py`;
  - the tide column of the monthly CSV.
- **Nothing else in this repository moves:** not the NAVD88 spline,
  the monthly water levels, the TWL export or the GNSS-IR-driven
  camera DEM.
- **The camera repository has its own copies of the same +0.09 m:**
  `process_historical.py` (`DEFAULT_DATUM_OFFSET`, passed to
  `extract_elevation_contours.py` as `--vertical-datum-offset`),
  `historical_forcing.py` (`MSL_TO_NAVD88`) and `adcp_to_navd88.py`
  (`MSL_TO_NAVD88`, the `--offset` fallback). The historical,
  tide-model-driven DEMs move by (new − 0.09) only if those copies are
  changed too.
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
