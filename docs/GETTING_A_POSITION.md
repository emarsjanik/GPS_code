# Getting your antenna's position

Every GNSS-IR result depends on knowing where the antenna is and how
high it sits. Get this wrong and nothing fails: the software runs,
plots appear, and the numbers are meaningless. This is the single
most consequential thing to get right during setup, and it is worth
an hour of care.

The good news is that the receiver you have already installed can
survey its own position, for free, to within a couple of centimetres.
You do not need survey equipment or a surveyor.

---

## The short version

1. Record 24 hours of data with the receiver already in place.
2. Convert it to RINEX (the pipeline does this automatically).
3. Upload the RINEX file to a free processing service.
4. Copy three numbers from the report into `station.json`.

You can run the station before doing this. GNSS-IR reprocesses from
the raw recordings, so a position corrected later costs nothing but
a rerun -- see "Starting without a position" at the end.

---

## Step 1: record a full day

The processing services want at least a few hours; 24 gives the best
result. If the station is already running, you have this already --
look in `rinex/` for a file covering a complete day.

If you have just installed the receiver, start it and come back
tomorrow:

```bash
./station/start_station.sh
```

**The antenna must not move during the recording, or afterwards.**
The position you get describes exactly where the antenna was. If it
is later adjusted, even by a few centimetres, the survey no longer
applies and every water level shifts by the same amount.

---

## Step 2: find the RINEX file

The station writes one observation file per day:

```bash
ls -la rinex/*.obs
```

If the day has already been compressed into an archive, unpack it:

```bash
tar -xzf rinex/station_YYYYMMDD.tar.gz -C rinex/
```

You want the `.obs` file. It will be a few hundred megabytes.

---

## Step 3: upload it for processing

Several free services will do this. They differ mainly in which
region they cover and which vertical datum they report.

**CSRS-PPP** (Natural Resources Canada) --
<https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php>

Works anywhere in the world, needs a free account, and returns
results in minutes to hours. It reports both an ellipsoidal height
and a CGVD2013 orthometric height, which is convenient because
GNSS-IR needs both. This is the service this project's own station
used.

**OPUS** (US National Geodetic Survey) --
<https://geodesy.noaa.gov/OPUS/>

United States only. Reports NAVD88 orthometric heights, which is
the datum most US tide gauges and flood studies use. If your work
will be compared against NOAA or USGS water level data, OPUS may
save you a datum conversion later.

**AUSPOS** (Geoscience Australia) --
<https://gnss.ga.gov.au/auspos>

Global coverage, no account needed, reports AHD heights for
Australian sites.

Any of these is fine. What matters is knowing **which vertical datum
your height is in**, because that determines what your published
water levels are referenced to.

---

## Step 4: read the report

A PPP report contains many numbers. Three go into `station.json`.

### Latitude and longitude

Reported in degrees, minutes and seconds:

```
Latitude (+n)     41° 53' 34.16760"
Longitude (+e)   -69° 57' 47.96177"
```

`station.json` wants **decimal degrees**, so convert:

```
decimal = degrees + minutes/60 + seconds/3600
```

Keep the sign. Western longitudes and southern latitudes are
negative. The example above becomes:

```json
"latitude": 41.8928243333,
"longitude": -69.9633227139
```

Six decimal places is about 10 cm, which is more than enough.

### Ellipsoidal height -> `height`

```
Ell. Height   -10.025 m
```

This goes in the `height` field. It is the height above the WGS84
reference ellipsoid, a mathematical surface, and **it is often
negative** -- across the northeastern United States the ellipsoid
sits above the geoid by roughly 30 m, so a station 19 m above the
water can have an ellipsoidal height of about -10 m. A negative
number here is normal and does not indicate an error.

### Orthometric height -> `gnssrefl_orthometric_height`

```
Orthometric Height
CGVD2013 (CGG2013a)
18.665 m
```

This goes in `gnssrefl_orthometric_height`. It is the height above a
geoid-based vertical datum -- roughly, height above mean sea level --
and it is what converts a reflector height into an actual water
level.

**These two heights are not interchangeable.** Putting the
ellipsoidal height in the orthometric field, or the reverse, produces
water levels wrong by tens of metres. They usually differ enough
that the mistake is obvious, but check.

---

## What the report says about your antenna

Two lines are worth reading even though they do not go into the
config:

```
Antenna Model     Unknown
APC to ARP        REF PCO : NULL
ARP to Marker     H: 0.000m
```

`Unknown` and `NULL` mean no antenna calibration was applied, so the
height you were given is the height of the **antenna phase centre** --
the electrical point where the signal is received -- rather than of a
monument or mounting bracket.

For GNSS-IR that is usually what you want, since reflector height is
measured from the phase centre down to the water. But it carries a
few centimetres of uncertainty, and if you later enter a separate
antenna height offset in `station.json`, be careful not to
double-count it.

If your antenna model is in the NGS calibration database, supplying
it during upload gives a better-defined height.

---

## Step 5: check it

After entering the values:

```bash
./test_installation.sh
```

This fails rather than warns if the coordinates are missing or
still placeholders, and cross-checks the reflector height range
against the orthometric height for consistency.

Then confirm the position is physically sensible:

```bash
python3 validate_station.py --checks reflection-zone
```

That writes a KML file. Open it in Google Earth over satellite
imagery and check the footprints actually land on the water you mean
to measure. If they fall short, no amount of configuration will fix
it -- the antenna is not high enough, or is too far from the water.

---

## Starting without a position

You do not need to wait. Enter an approximate position -- a phone's
GPS, or a point read off Google Earth -- and start recording. The
station will run and produce plots.

Those early results will be wrong in absolute terms, but the raw
recordings are kept. Once you have a proper position, update
`station.json` and reprocess:

```bash
python3 -c "import sys; sys.path.insert(0,'station'); \
    from gnssrefl_processor import GnssIrProcessor; \
    print(GnssIrProcessor().initialize())"

for doy in $(ls products/refl_code/2026/results/<station>/*.txt \
             | xargs -n1 basename | sed 's/\.txt//'); do
    gnssir <station> 2026 $doy
done
```

Everything recorded in the meantime is recovered with the corrected
position. Nothing is lost by starting early.

---

## Common mistakes

**Degrees-minutes-seconds entered as decimal degrees.** `41° 53'`
entered as `41.53` puts the station about 4 km from where it is.
Convert properly.

**A dropped minus sign.** Western longitude must be negative. A
positive longitude at Cape Cod places the station in Uzbekistan, and
the software will not object.

**The two heights swapped.** See above.

**Moving the antenna after surveying.** The position describes where
the antenna was. Adjust the mount and it needs redoing.

**Assuming a negative ellipsoidal height is wrong.** It usually is
not.
