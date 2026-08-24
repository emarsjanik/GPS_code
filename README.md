# USGS GNSS Reference Station

Autonomous GNSS data logging, RINEX conversion, and GNSS-IR (interferometric reflectometry) processing built around a Unicore UM980 receiver.

**New to this project? Start with [`GNSS-IR_Quick_Setup_Guide.docx`](GNSS-IR_Quick_Setup_Guide.docx)** — a complete, no-prior-experience-required walkthrough from downloading this repository through your first real plot, with a labeled screenshot of every command's real output. This is the master setup document for this project; everything below is the technical quick-reference for once you're up and running. For more depth than the guide covers (every configuration option, advanced troubleshooting), see [`QUICKSTART.md`](QUICKSTART.md) and [`STATION_JSON_REFERENCE.md`](STATION_JSON_REFERENCE.md).

## Data flow

```
UM980 receiver (USB serial)
        │
        ▼
receiver.py            Receiver.record_raw()
        │
        ▼
raw/*.um980             raw binary capture
        │
        ▼
rinex_processor.py      RinexProcessor.convert()  (wraps RTKLIB's convbin)
        │
        ▼
rinex/*.obs, *.nav      RINEX 3.05, with SNR observables
        │
        ▼
gnssrefl_processor.py   GnssIrProcessor.process()  (wraps gnssrefl: rinex2snr → gnssir)
        │
        ▼
products/refl_code/<year>/results/<station>/<doy>.txt   reflector heights
```

`pipeline.py` chains RINEX conversion and GNSS-IR processing for one file. `station_manager.py` chains recording, pipeline processing, and health checks for continuous, unattended operation.

## Requirements

- Ubuntu 20.04 (or compatible)
- Python 3.10+ (`gnssrefl` requires this; the system's default Python on Ubuntu 20.04 is 3.8 and cannot run it — see install notes below)
- [RTKLIB](https://github.com/rtklibexplorer/RTKLIB) (`convbin`), built from source
- [`gnssrefl`](https://github.com/kristinemlarson/gnssrefl) (installs via pip once on Python 3.10+)
- `pyserial`

## Quick start

The three commands below are this project's master install/verify/run
scripts — the same ones walked through step by step, with real
example output, in [`GNSS-IR_Quick_Setup_Guide.docx`](GNSS-IR_Quick_Setup_Guide.docx).
If you're setting this up for the first time, use that guide instead
of this section; it also covers identifying your receiver's USB port
and giving it a permanent name before you get here.

```bash
git clone -b master-scripts https://github.com/emarsjanik/GPS_code.git ~/GNSS/v4.1
cd ~/GNSS/v4.1

./install.sh              # checks/installs every dependency, sets up
                           # the venv, and walks you through station.json

./test_installation.sh    # confirms the whole setup actually works,
                           # with a clear pass/fail for each check

./process_and_plot.sh     # converts new raw data, runs the GNSS-IR
                           # analysis, and generates a plot
```

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | Loads/validates `station.json`; computes every project path relative to its own file location, not the CWD |
| `receiver.py` | `Receiver` class — all serial I/O with the UM980. Single low-level primitive (`read_message()`) handles both ASCII and binary (NovAtel/Unicore OEM7) framing. `record_raw()` supports chunked recording via `append=True` |
| `database.py` | `Database` class — SQLite persistence, 11 tables, WAL mode, self-upgrading schema |
| `rinex_processor.py` | `RinexProcessor` class — wraps `convbin`. **Requires `-os`** to include SNR observables; without it, output is silently useless for GNSS-IR (no error raised) |
| `gnssrefl_processor.py` | `GnssIrProcessor` class — wraps `gnssrefl` (`rinex2snr` → `gnssir`). See **gnssrefl integration notes** below — several non-obvious, confirmed quirks live here |
| `pipeline.py` | `Pipeline` class — chains RINEX conversion + GNSS-IR processing across whatever's new in `raw/`; archives raw files after successful processing |
| `station_manager.py` | `StationManager` class — top-level orchestrator for continuous operation. Records each UTC day in configurable chunks (default 3600s), runs the pipeline once a day completes, periodic health checks, graceful `SIGTERM`/`SIGINT` handling |
| `station.py` | One-shot dashboard/status check — connects, reports receiver + DB status, exits |
| `exceptions.py`, `version.py` | Shared exception types; version string |
| `start_station.sh`, `stop_station.sh` | Idempotent cron-based wrappers around `station_manager.py` — safe to call repeatedly (no-op if already running), used for autonomous start-on-boot + crash recovery in place of systemd |

All classes follow the same shape: `initialize()` (may raise on a real setup problem) → main working method (never raises for an ordinary operational failure — errors come back as a result object) → `status()` → `shutdown()`.

## gnssrefl integration notes

`gnssrefl_processor.py`'s complexity is almost entirely from confirmed, real quirks in `gnssrefl` 4.1.5 and `convbin`, not incidental design choices:

- **`rinex2snr()` decides RINEX 2.11 vs RINEX 3 purely by the length of the `station` argument** — 4 characters means "assume RINEX 2.11," 9 means "assume RINEX 3." Since `convbin` always outputs RINEX 3 here, `GnssIrProcessor` maintains two station codes: a 4-character one (used by `make_gnssir_input()` and `gnssir()`) and a 9-character one built from it plus a configurable monument number/country code (used only for `rinex2snr()` and the staged filename). Getting this wrong produces no error — just zero usable SNR data.
- **Staged filename convention**: `{STATION9}_R_{YYYY}{DOY}0000_01D_{RATE}S_MO.rnx`, staged under `$REFL_CODE/<year>/rinex/<4-char station>/`. The time segment is always `0000` regardless of the file's real start time.
- **Confirmed upstream bug**: `gnssrefl`'s file-discovery logic checks the current working directory for a same-named file *before* the properly staged directory, and reuses it forever if found — never refreshed. `_stage_rinex_file()` deletes any same-named file from the CWD before staging, specifically to prevent this.
- **Confirmed upstream bug**: `gps.checkEGM()` crashes with `TypeError` the first time `$REFL_CODE/Files` doesn't exist (malformed `subprocess.call()` argument). Worked around by creating that directory proactively in `initialize()`.
- **Reflector height / quality score parsing** is confirmed against a real results file's own header (`% year, doy, RH, sat, UTCtime, ...`): column 3 is RH (meters), column 14 is PkNoise. Reported as the mean across all parseable rows.
- **`soil_moisture`/`snow_depth` are always `None`** — these require separate, later `gnssrefl` modules operating on multi-day history, out of scope for a single day's `gnssir` run.
- Default orbit fetching (multi-GNSS SP3) failed in testing against CDDIS (likely requires an EarthData Login account, and/or same-day rapid products not yet published). Set `"gnssrefl_orbit_source": "nav"` in `station.json` to use broadcast GPS-only ephemeris instead — still requires a small internet download (SOPAC), not fully offline.

## Configuration (`station/resources/station.json`)

See [`STATION_JSON_REFERENCE.md`](STATION_JSON_REFERENCE.md) for the complete field-by-field reference. Summary of the most important fields:

| Field | Purpose |
|---|---|
| `station_id`, `latitude`, `longitude`, `height` | Station identity and coordinates |
| `receiver_port`, `receiver_baud`, `receiver_timeout` | Serial connection to the UM980 |
| `rinex_version` | Passed to `convbin`; this project always uses `3.05` |
| `gnssrefl_station_code` | 4-character station code (see integration notes above) |
| `gnssrefl_monument_number`, `gnssrefl_country_code` | Used to build the 9-character RINEX 3 station code |
| `gnssrefl_orbit_source` | `"nav"` for offline-friendly broadcast ephemeris; unset for default multi-GNSS SP3 |
| `gnssrefl_sample_rate` | Recording sample rate in seconds; must match the real `LOG ... ONTIME` interval |
| `record_raw_chunk_seconds` | `StationManager` recording chunk size; 3600 (1h) default, 600 (10min) recommended for cron/systemd-managed deployments so stop/restart stay responsive |
| `manager_retry_delay_seconds`, `health_check_interval_seconds` | `StationManager` tuning |
| `tide_model_file`, `tide_model_value_column`, `tide_model_time_column` | Optional — enables automatic tide model comparison in `process_and_plot.sh` |

## Running

```bash
# Autonomous (recommended) — cron-managed, survives crashes and reboots
~/GNSS/v4.1/station/start_station.sh    # idempotent; safe to call repeatedly
~/GNSS/v4.1/station/stop_station.sh     # graceful — waits for the current chunk to finish

# Direct (foreground, blocks)
python3 station_manager.py

# One-shot status check
python3 station.py
```

Crontab (used instead of systemd by choice — see commit history for the reasoning):

```
@reboot /home/argus_user/GNSS/v4.1/station/start_station.sh
*/5 * * * * /home/argus_user/GNSS/v4.1/station/start_station.sh
```

## Testing

```bash
python -m unittest discover -s tests -v
```

222 tests as of this writing, covering every module above with fake `Receiver`/`gnssrefl`/`Pipeline` stand-ins — no real hardware or `gnssrefl` install required to run the suite. All fakes are installed via `sys.modules` injection rather than mocking library internals, so the real integration code paths are exercised, not bypassed. `./test_installation.sh` runs this same suite as part of a broader, beginner-friendly pass/fail check of the whole setup.

```bash
python check_layout.py
```

Verifies every expected file is present and non-empty/non-stub — useful after a fresh clone or file transfer.

## Directory outputs

| Path | Contents |
|---|---|
| `raw/` | Raw UM980 binary captures |
| `rinex/` | RINEX `.obs`/`.nav` files |
| `products/refl_code/` | `gnssrefl`'s entire working directory (`$REFL_CODE`) — analysis strategy JSON, staged RINEX, SNR files, results, logs |
| `database/` | `station.db` (SQLite) |
| `logs/` | `database.log`, and (cron setup) `station_manager.pid`/`station_manager.out` |
| `archive/` | Raw files moved here after successful processing |

None of the above are tracked in git (see `.gitignore`) — they're all generated data, along with `gnssrefl_venv/`.

## Hardware

- NUC (Ubuntu 20.04.6 LTS), shared with an unrelated I2R I2RGUS coastal camera system on the same machine
- ArduSimple SimpleRTK3B Budget receiver board (Unicore UM980 chip) — BeiDou/Galileo/GLONASS/GPS/NavIC/QZSS/SBAS
- ArduSimple budget tripleband GNSS antenna (DigiKey 3619-AS-ANT3B-BUDSUR-L1L2L5-25SMA-ND) — 5 dBi, built-in LNA, IP66

## Known open items

- RINEX/product retention policy in `pipeline.py` is unimplemented — currently nothing prunes `rinex/`, `products/`, or `archive/` over time
- `convbin` build source lives at `~/software/RTKLIB/` on the deployed station (not in this repo) — see `install.sh`, which builds it fresh from the current `rtklibexplorer/RTKLIB` main branch
- BeiDou/Galileo ephemeris logging works but hasn't been observed within short test windows; needs a longer recording to confirm reliably
