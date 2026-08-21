# Quickstart: GNSS-IR Reference Station

This guide walks you through setting up a working GNSS-IR (GNSS
Interferometric Reflectometry) reference station from a completely bare
machine to your first real plot, with no prior experience assumed.

GNSS-IR uses the same signals a regular GPS receiver already gets from
satellites, but analyzes how they *reflect* off a nearby surface (water,
soil, snow) before reaching the antenna. That reflection shows up as a
subtle interference pattern in the raw signal strength data, and from that
pattern, this software can work out how far below the antenna the
reflecting surface is — and therefore, over time, how that distance
(water level, soil moisture, snow depth) is changing.

---

## What you'll need

- A Linux machine (this guide assumes Ubuntu or a close relative — see
  the note in `install.sh` if you're on something else)
- A supported GNSS receiver (this project was built and tested against a
  Unicore UM980; other receivers may need changes to `station/receiver.py`)
- A GNSS antenna, mounted somewhere with a reasonably clear view of the
  sky and, ideally, of whatever you're trying to sense (open water,
  bare soil, snow-covered ground)
- Your antenna's precise location: latitude, longitude, and height. If
  you don't have a survey-grade position yet, even a consumer GPS
  reading is enough to get started and see real results — you can
  refine the location later without losing any collected data, since
  GNSS-IR reprocesses from the same raw recordings
- Internet access on the machine (used to download orbit data and
  install software; a single well-configured continuous connection is
  ideal, but the software tolerates intermittent connectivity)

---

## Step 1: Install everything

From the project directory:

```bash
./install.sh
```

This checks for, and installs if missing:
- Python and a private virtual environment for this project (so it
  can't conflict with anything else on your system)
- `git` and build tools
- RTKLIB's `convbin` (built from source — this project needs a specific
  version, not the one available via your system's package manager;
  `install.sh` explains why and handles this automatically)
- `gnssrefl`, the GNSS-IR analysis engine this project is built on
- This project's own directory structure (`raw/`, `rinex/`, `products/`,
  etc.)

It will ask before making any change that needs administrator (`sudo`)
access, and is safe to re-run if it's interrupted partway through —
every step checks whether it's already done before doing it again.

At the end, it will automatically start the **station configuration
wizard** (`setup_station.sh`) if you don't already have a
`station.json` file. Answer its questions about your station's
identity, receiver, and — most importantly — its exact location. See
**`STATION_JSON_REFERENCE.md`** for a complete explanation of every
setting if you want more detail than the wizard gives inline, or if you
need to change something later by hand.

---

## Step 2: Verify everything is working

```bash
./test_installation.sh
```

This checks the Python environment, every required tool, your
configuration file, the project's internal file layout, and (if a
receiver happens to be connected already) basic communication with it.
It gives a clear pass/fail for each check with a plain-language
explanation of what to do about anything that failed — fix those before
moving on.

It's normal, and not a failure, to see a warning here if your receiver
isn't connected yet. This step is meant to be useful even before you
have hardware set up.

---

## Step 3: Connect your receiver

Plug in your receiver via USB (or however your specific model connects).
Confirm your operating system sees it:

```bash
dmesg | tail -20
```

You should see a new serial device appear, e.g. `/dev/ttyUSB0` or
`/dev/ttyACM0`.

**Strongly recommended:** set up a stable, named symlink (like
`/dev/USB_GPS`, the default this project expects) rather than using the
raw device name directly, since that name can change across reboots or
if other USB devices are plugged in. This is normally done with a
`udev` rule based on your receiver's specific USB vendor/product ID —
consult your receiver's documentation, or your Linux distribution's
udev documentation, for the exact steps, since these details vary by
hardware. Once set up, update `receiver_port` in `station.json` (or
re-run `./setup_station.sh`) to match.

---

## Step 4: A quick connectivity check (optional but recommended)

Before committing to a long recording, confirm the receiver responds:

```bash
source gnssrefl_venv/bin/activate
python3 station/tools/um980_cmd.py VERSIONA
```

You should see the receiver's model and firmware printed back. If this
times out or errors, double check `receiver_port` and
`receiver_baud` in `station.json`, and that nothing else (another
program, a stale terminal session) already has the port open.

You can also check basic antenna position stability before a long
unattended run:

```bash
python3 station/position_stability_check.py --duration 300
```

---

## Step 5: Record some data

**For a first test**, a recording of an hour or more is enough to see
whether the whole pipeline works end-to-end (though a real, useful
result — especially for something slowly changing like soil moisture —
usually needs many hours to days of data). To record for a specific,
fixed duration:

```bash
cd station
python3 overnight_recording.py --hours 4
```

This can run for as long as you like; run it inside `screen` or `tmux`
if you're connecting over SSH, since it needs to keep running even if
your connection drops:

```bash
screen -S gnss_recording
source ../gnssrefl_venv/bin/activate
python3 overnight_recording.py --hours 4
# Ctrl+A, D to detach; "screen -r gnss_recording" to reattach later
```

**For continuous, unattended, indefinite operation** (the normal way to
actually run a reference station long-term), see **Step 8** below
instead of this manual recording step.

---

## Step 6: Process your data and see a plot

Once you have at least a little recorded data:

```bash
./process_and_plot.sh
```

This one command:
1. Recovers any previously-missed days, if you've configured external
   storage
2. Converts new raw recordings to RINEX and runs the GNSS-IR analysis
3. Finds the most recent stretch of results and generates a plot

You'll see live progress throughout — a count of how many files have
been processed so far, and gnssrefl's own detailed per-day output as it
runs. Processing genuinely takes real time (RINEX conversion plus
orbit-data-dependent analysis, per file) — this is expected, not a
sign that anything has stalled.

When it finishes, it tells you exactly where the plots were saved. The
main result is usually the file ending in `_last.png`.

---

## Step 7: Understand what you're looking at

The main plot shows your computed reflector height (or, if you set
`gnssrefl_orthometric_height` in `station.json`, absolute water level)
over time. A few things worth knowing before you trust what you see:

- **A handful of data points from one day is not a signal.** Real,
  useful GNSS-IR results generally need many satellite passes across
  many hours to start showing a clear, physically consistent pattern.
- **Not every day will produce results, and that's normal.** Depending
  on your elevation mask, azimuth mask, and site geometry, some days
  simply won't have enough usable satellite passes. This project's
  tooling (`recover_missing_days.sh`) distinguishes this genuine
  "no data" outcome from a transient failure (like a not-yet-published
  orbit product) automatically.
- **A pattern that correlates with something external does not by
  itself prove the antenna is sensing that thing.** Before trusting a
  result, especially one you plan to publish or rely on, use
  `validate_station.py` (next step) to check whether the pattern you're
  seeing is actually geometry-specific (as real reflection sensing
  should be), or a shared artifact that shows up regardless of
  direction (which would suggest something else — an atmospheric
  effect, an instrumental artifact, a processing issue — not genuine
  sensing).

---

## Step 8: Check whether your results are real

```bash
python3 validate_station.py --checks reflection-zone \
    --center-azimuth <bearing toward your target, in degrees>
```

This generates a KML file you can open in Google Earth, showing exactly
where your antenna's reflection footprint actually falls at different
water levels (or ground conditions) — useful for confirming your
azimuth mask and reflector-height range are physically sensible before
you trust anything downstream.

If you have an independent reference signal to compare against (a
nearby tide gauge, a soil moisture sensor, a weather station), you can
run the tool's other checks (`azimuth`, `elevation`, `refraction`) to
test whether an apparent signal is genuinely geometry-specific — see
the comments at the top of `validate_station.py` for the full
methodology and how to run each check.

---

## Step 9: Go continuous (optional, for long-term deployment)

Once you're confident the whole pipeline works, `station_manager.py`
runs the station continuously and unattended — recording in
configurable chunks, processing automatically, and (if configured)
exporting to external storage:

```bash
cd station
python3 station_manager.py
```

For a truly hands-off deployment, `start_station.sh` and
`stop_station.sh` provide an idempotent wrapper suitable for running
from `cron` as a watchdog (so an unexpected crash gets automatically
restarted, without ever risking two copies fighting over the same
serial port):

```bash
crontab -e
# Add a line like:
# @reboot /path/to/station/start_station.sh
# */5 * * * * /path/to/station/start_station.sh
```

To stop gracefully (waiting for the current recording chunk to finish,
rather than an abrupt kill):

```bash
station/stop_station.sh
```

---

## Troubleshooting

**"No results found" after processing, but no errors either.**
Most likely, none of your recorded days had enough usable satellite
passes to produce a retrieval — a genuine, normal outcome for some
site/configuration combinations, not necessarily a bug. Check your
elevation and azimuth mask in `station.json` against your site's real,
physical sky view.

**Very few retrievals per day, or a `RankWarning` in the processing
output.**
If you're using a narrow elevation mask (a small range between
`gnssrefl_elevation_min` and `gnssrefl_elevation_max`), see the
"Elevation angle mask" section of `STATION_JSON_REFERENCE.md` — this is
a documented, expected tradeoff with a known fix
(`gnssrefl_elevation_span_tolerance`, `gnssrefl_direct_signal_poly_order`).

**Results look confident but implausible (wrong magnitude, wrong
timing).**
Double-check `latitude`/`longitude`/`height` in `station.json` first —
these are the single most common source of confusing results, since an
error here doesn't cause a processing failure, just a wrong answer.

**"convbin not found" or receiver connection errors.**
Re-run `./test_installation.sh` for a clear diagnosis, then
`./install.sh` again if needed — both are safe to re-run at any time.

**Something else.**
Every script's own header comment explains what it does and why, in
detail — `station/gnssrefl_processor.py` and `station/receiver.py` in
particular document several confirmed, real quirks of both gnssrefl and
the receiver hardware that are worth reading if you're debugging
something unusual.
