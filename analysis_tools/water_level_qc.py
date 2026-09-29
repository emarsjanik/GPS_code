#!/usr/bin/env python3
"""
water_level_qc.py

Automatic quality control for the GNSS-IR water level, so readings
that cannot be trusted are left out of the public plot rather than
shown as real water levels.

WHY

Reflections degrade in big waves: they scatter off crests and foam.
In the 25-26 Sep 2026 storm the spline read up to ~3 m above what the
NOAA tide gauge at Chatham implied, and plot_7day.py would have
published those values labelled "+2.xx metres from the prediction".

TESTS (flags follow IOOS QARTOD: 1 good, 3 suspect, 4 fail). A
reading's flag is the worst of its tests. Ported from the waterline
project's gnssr_qc.py, where they were tuned against the Sep 2026
storm (16 of 16 bad readings caught, 0 of 1471 good ones rejected).

  gross range   outside GROSS_MIN..GROSS_MAX m -- beyond anything tide
                plus surge produces here.                        -> FAIL
  reference     more than max(REF_LIMIT, REF_SIGMAS * sigma) from the
                level predicted from the Chatham gauge (8447435):
                    water level ~ a * gauge(t - lag) + b
                a, lag and b are fitted robustly over the last
                REF_FIT_DAYS, so the test does not depend on the
                datum or on the gauge sitting in a harbour with a
                different tidal range. Catches SUSTAINED failures.
                Skipped if the gauge cannot be reached and nothing is
                cached.                                          -> FAIL
  spike         more than SPIKE_LIMIT from a quadratic through the
                readings within +/-2 h (at least 2 each side; failed
                readings left out). A curve, not a median, because the
                tide itself moves ~0.9 m in two hours.           -> FAIL
  rate          faster than MAX_RATE m/h between readings.    -> SUSPECT

WAVE SETUP. Chatham (Lydia Cove) is a harbour gauge; Marconi is an open
beach, and the GNSS-IR footprint (~70-220 m from the antenna) is the
surf zone. In a storm, breaking waves raise the mean water level there
(setup) by an amount the harbour gauge never records. A reading ABOVE
the gauge-predicted level is therefore only failed if it is above it by
more than the reference limit PLUS the largest setup the waves at the
time could produce, from Stockdon et al. (2006):

    setup = 0.35 * beta * sqrt(Hs * L0),   L0 = g * Tp^2 / (2 * pi)

with Hs, Tp from NDBC buoy WAVE_STATION (downloaded and cached like the
gauge) and beta the foreshore slope (SETUP_BETA, ~0.1 from the winter
2025 DEM). This is the setup at the SHORELINE, the upper bound for the
footprint. Readings inside that allowance are kept as SUSPECT with
reason "possible_setup" instead of failed; readings below the gauge
are unaffected (setup only raises the water). Without wave data the
test falls back to the plain reference limit.
Hs 3 m, Tp 10 s -> 0.76 m allowance; Hs 1 m, Tp 7 s -> 0.31 m.

Only FAIL readings are removed; SUSPECT ones are kept.

FOR PUBLIC DISPLAY (public_mask) two more rules, because the spline
is one smooth curve fitted through every reading: bad readings bend it
for about an hour either side, so points just outside a failed stretch
pass the tests but are still pulled towards the bad values.

  buffer        readings within PUBLIC_BUFFER_S of a failed one are
                also left out.
  fragments     a piece shorter than PUBLIC_MIN_SEGMENT_S that is left
                between failures is dropped: an hour of data squeezed
                between two failed stretches cannot be trusted, and on
                a public plot it reads as a spike.

On the real Sep 2026 storm the plain tests left short, steep pieces at
+1.3 to +1.8 m between the gaps; these rules remove them.

GAUGE DATA are downloaded from the NOAA CO-OPS API (datum NAVD88,
metric, UTC) and merged into a cache CSV, so a network outage falls
back to what is already on disk.

Standalone use (report only):
    python3 water_level_qc.py products/refl_code/Files/usgs/usgs_spline_out.txt
"""

from __future__ import annotations

import csv
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

GOOD, SUSPECT, FAIL = 1, 3, 4

GROSS_MIN, GROSS_MAX = -2.5, 3.5      # m
SPIKE_LIMIT = 0.5                     # m
SPIKE_WINDOW_S = 2 * 3600
REF_LIMIT = 0.5                       # m
REF_SIGMAS = 5.0
REF_FIT_DAYS = 30
MAX_RATE = 1.0                        # m per hour
MAX_LAG_MIN = 180
GAUGE_GAP_S = 720                     # gauge is 6-minute; wider gaps are gaps
PUBLIC_BUFFER_S = 3600                # drop readings this close to a failure
PUBLIC_MIN_SEGMENT_S = 3 * 3600       # drop shorter pieces left between failures
SEGMENT_GAP_S = 90 * 60               # matches plot_7day.split_on_gaps

GAUGE_STATION = "8447435"             # Chatham, Lydia Cove, MA
WAVE_STATION = "44008"                # NDBC Nantucket Shoals, as fetch_buoy_waves.py
WAVE_API = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"   # last 45 days
SETUP_COEF = 0.35                     # Stockdon et al. (2006) setup coefficient
SETUP_BETA = 0.10                     # foreshore slope, winter 2025 Marconi DEM
WAVE_MAX_GAP_S = 3 * 3600             # use a wave record up to 3 h away
API = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=water_level"
       "&station={station}&begin_date={begin}&end_date={end}&datum=NAVD&units=metric"
       "&time_zone=gmt&format=csv&application=gnssir_station_qc")


# ---------------------------------------------------------------------
# GAUGE
# ---------------------------------------------------------------------

def _read_cache(path: Path) -> dict:
    rows = {}
    if path.exists():
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows[int(r["epoch"])] = float(r["level_navd88"])
                except (KeyError, ValueError):
                    continue
    return rows


def _download(station: str, start: datetime, end: datetime) -> dict:
    """{epoch: level} from the CO-OPS API in <=30-day requests."""
    rows = {}
    t = start
    while t <= end:
        stop = min(t + timedelta(days=30), end)
        url = API.format(station=station, begin=t.strftime("%Y%m%d"), end=stop.strftime("%Y%m%d"))
        text = urllib.request.urlopen(url, timeout=60).read().decode()
        reader = csv.reader(text.splitlines())
        header = [h.strip() for h in next(reader, [])]
        if "Date Time" not in header or "Water Level" not in header:
            raise RuntimeError(f"NOAA API returned no data: {text[:200]}")
        it, iv = header.index("Date Time"), header.index("Water Level")
        for row in reader:
            try:
                dt = datetime.strptime(row[it].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                rows[int(dt.timestamp())] = float(row[iv])
            except (ValueError, IndexError):
                continue
        t = stop + timedelta(days=1)
    return rows


def load_gauge(cache: Path, start: datetime, end: datetime, station: str = GAUGE_STATION):
    """
    Gauge record covering start..end as (epochs, levels), merging a
    fresh download into `cache`. Returns None if there is no data at
    all. Prints one line saying where the data came from.
    """
    rows = _read_cache(cache)
    try:
        new = _download(station, start, end)
        rows.update(new)
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(cache.suffix + ".tmp")
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "epoch", "level_navd88"])
            for e in sorted(rows):
                w.writerow([datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            e, f"{rows[e]:.3f}"])
        tmp.replace(cache)
        print(f"  QC: Chatham gauge {len(new)} reading(s) downloaded")
    except Exception as exc:                 # offline: fall back to the cache
        print(f"  QC: Chatham gauge download failed ({exc}); using {len(rows)} cached reading(s)")
    if not rows:
        return None
    ep = np.array(sorted(rows), dtype=float)
    return ep, np.array([rows[int(e)] for e in ep])


def load_waves(cache: Path, station: str = WAVE_STATION):
    """
    Buoy Hs/Tp as (epochs, hs, tp), merging the NDBC realtime file (last
    45 days) into `cache` (columns epoch, wvht_m, dpd_s -- the same as
    the waterline project's fetch_buoy_waves.py). None if no data at all.
    """
    rows = {}
    if cache.exists():
        with open(cache, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows[int(float(r["epoch"]))] = (float(r["wvht_m"]), float(r["dpd_s"]))
                except (KeyError, ValueError):
                    continue
    try:
        text = urllib.request.urlopen(WAVE_API.format(station=station), timeout=60).read().decode()
        lines = [l.split() for l in text.splitlines() if l.strip()]
        head = [h.lstrip("#") for h in lines[0]]
        iw, idp = head.index("WVHT"), head.index("DPD")
        new = 0
        for v in lines[1:]:
            if v[0].startswith("#") or v[iw] == "MM" or v[idp] == "MM":
                continue
            t = datetime(int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4]), tzinfo=timezone.utc)
            rows[int(t.timestamp())] = (float(v[iw]), float(v[idp]))
            new += 1
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(cache.suffix + ".tmp")
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "epoch", "wvht_m", "dpd_s"])
            for e in sorted(rows):
                w.writerow([datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            e, rows[e][0], rows[e][1]])
        tmp.replace(cache)
        print(f"  QC: buoy {station} {new} wave record(s) downloaded")
    except Exception as exc:
        print(f"  QC: buoy {station} download failed ({exc}); using {len(rows)} cached record(s)")
    if not rows:
        return None
    ep = np.array(sorted(rows), dtype=float)
    return ep, np.array([rows[int(e)][0] for e in ep]), np.array([rows[int(e)][1] for e in ep])


def setup_allowance(epochs, waves):
    """Largest plausible shoreline wave setup (m) at each epoch; 0 where no wave record."""
    epochs = np.asarray(epochs, dtype=float)
    if waves is None or len(waves[0]) == 0:
        return np.zeros(len(epochs))
    w_ep, hs, tp = waves
    j = np.clip(np.searchsorted(w_ep, epochs), 0, len(w_ep) - 1)
    jm = np.clip(j - 1, 0, len(w_ep) - 1)
    k = np.where(np.abs(w_ep[jm] - epochs) < np.abs(w_ep[j] - epochs), jm, j)
    ok = np.abs(w_ep[k] - epochs) <= WAVE_MAX_GAP_S
    l0 = 9.81 * tp[k] ** 2 / (2 * np.pi)
    return np.where(ok, SETUP_COEF * SETUP_BETA * np.sqrt(np.maximum(hs[k] * l0, 0.0)), 0.0)


def reference_at(r_ep, r_lv, epochs):
    """Gauge level at `epochs`; NaN outside the record or across gaps."""
    out = np.interp(epochs, r_ep, r_lv, left=np.nan, right=np.nan)
    if len(r_ep) > 1:
        i = np.clip(np.searchsorted(r_ep, epochs), 1, len(r_ep) - 1)
        out[(r_ep[i] - r_ep[i - 1]) > GAUGE_GAP_S] = np.nan
    return out


# ---------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------

def fit_reference(ep, lv, r_ep, r_lv, keep):
    """
    Robust fit of level = a * gauge(t - lag) + b over the last
    REF_FIT_DAYS of readings not already failed. Returns
    (a, lag_s, b, sigma) or None if there is too little overlap.
    """
    recent = keep & (ep >= ep.max() - REF_FIT_DAYS * 86400)
    best = None
    for lag_min in range(-MAX_LAG_MIN, MAX_LAG_MIN + 1, 6):
        x = reference_at(r_ep, r_lv, ep - lag_min * 60)
        m = recent & np.isfinite(x)
        if m.sum() < 48:
            continue
        use = m.copy()
        for _ in range(4):                          # iteratively drop outliers
            A = np.c_[x[use], np.ones(use.sum())]
            coef, *_ = np.linalg.lstsq(A, lv[use], rcond=None)
            res = lv - (coef[0] * x + coef[1])
            sigma = max(1.4826 * np.nanmedian(np.abs(res[use])), 0.03)
            use = m & (np.abs(res) < max(3 * sigma, 0.15))
        rms = float(np.sqrt(np.mean(res[use] ** 2)))
        if best is None or rms < best[4]:
            best = (float(coef[0]), lag_min * 60.0, float(coef[1]), float(sigma), rms)
    return None if best is None else best[:4]


def run_qc(ep, lv, reference=None, waves=None):
    """
    ep: epochs (s, UTC), lv: levels (m). reference: (r_ep, r_lv) or None.
    waves: (w_ep, hs, tp) or None (see WAVE SETUP in the module docstring).
    Returns (flags, reasons, fit).
    """
    ep = np.asarray(ep, dtype=float)
    lv = np.asarray(lv, dtype=float)
    n = len(lv)
    flags = np.full(n, GOOD, dtype=np.int8)
    reasons = [[] for _ in range(n)]

    def mark(i, flag, why):
        flags[i] = max(flags[i], flag)
        reasons[i].append(why)

    for i in np.flatnonzero((lv < GROSS_MIN) | (lv > GROSS_MAX)):
        mark(i, FAIL, "gross_range")

    # Reference test before the spike test, so good readings are not
    # judged against neighbours that are themselves failures.
    fit = None
    if reference is not None and len(reference[0]) > 1 and n:
        fit = fit_reference(ep, lv, reference[0], reference[1], flags < FAIL)
        if fit is not None:
            a, lag_s, b, sigma = fit
            predicted = a * reference_at(reference[0], reference[1], ep - lag_s) + b
            limit = max(REF_LIMIT, REF_SIGMAS * sigma)
            allowance = setup_allowance(ep, waves)
            for i in np.flatnonzero(np.isfinite(predicted) & (np.abs(lv - predicted) > limit)):
                excess = lv[i] - predicted[i]
                if 0 < excess <= limit + allowance[i]:
                    mark(i, SUSPECT, "possible_setup")    # above the gauge, within wave setup
                else:
                    mark(i, FAIL, "reference")

    ok = flags < FAIL
    for i in range(n):
        if not ok[i]:
            continue
        near = ok & (np.abs(ep - ep[i]) <= SPIKE_WINDOW_S)
        near[i] = False
        if (near & (ep < ep[i])).sum() < 2 or (near & (ep > ep[i])).sum() < 2:
            continue                                # not evaluated at edges and gaps
        coef = np.polyfit((ep[near] - ep[i]) / 3600.0, lv[near], 2)
        if abs(lv[i] - coef[2]) > SPIKE_LIMIT:      # coef[2] = curve value at t_i
            mark(i, FAIL, "spike")

    for i in range(1, n):
        dt_h = (ep[i] - ep[i - 1]) / 3600.0
        if 0 < dt_h <= 2 and abs(lv[i] - lv[i - 1]) / dt_h > MAX_RATE:
            mark(i, SUSPECT, "rate_of_change")
    return flags, [";".join(r) for r in reasons], fit


def public_mask(ep, flags):
    """
    True where a reading may be shown publicly: not failed, not within
    PUBLIC_BUFFER_S of a failed reading, and not part of a fragment
    shorter than PUBLIC_MIN_SEGMENT_S that borders a removed reading.
    """
    ep = np.asarray(ep, dtype=float)
    failed = np.asarray(flags) >= FAIL
    show = ~failed
    fail_ep = ep[failed]
    if len(fail_ep):
        # distance from each reading to the nearest failed one
        j = np.searchsorted(fail_ep, ep)
        after = fail_ep[np.minimum(j, len(fail_ep) - 1)]
        before = fail_ep[np.maximum(j - 1, 0)]
        near = np.minimum(np.abs(ep - after), np.abs(ep - before))
        show &= near > PUBLIC_BUFFER_S

    removed = ~show
    idx = np.flatnonzero(show)
    if len(idx):
        # contiguous shown pieces: consecutive shown readings with no
        # removed reading between them and no data gap
        breaks = np.flatnonzero((np.diff(idx) > 1) | (np.diff(ep[idx]) > SEGMENT_GAP_S)) + 1
        for seg in np.split(idx, breaks):
            first, last = seg[0], seg[-1]
            borders = (first > 0 and removed[first - 1]) or (last < len(ep) - 1 and removed[last + 1])
            if borders and ep[last] - ep[first] < PUBLIC_MIN_SEGMENT_S:
                show[seg] = False
    return show


def summary(flags, reasons, fit, ep=None, since=None) -> str:
    """One line for the log. `since` limits the counts to ep >= since."""
    sel = np.ones(len(flags), bool) if since is None else (np.asarray(ep) >= since)
    counts = {}
    for r, f, s in zip(reasons, flags, sel):
        if s and f >= FAIL:
            for why in r.split(";"):
                counts[why] = counts.get(why, 0) + 1
    text = (f"QC: {int(((flags >= FAIL) & sel).sum())} of {int(sel.sum())} reading(s) failed"
            + (f" ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})" if counts else "")
            + f", {int(((flags == SUSPECT) & sel).sum())} suspect kept"
            + (f" ({sum(1 for r, s in zip(reasons, sel) if s and 'possible_setup' in r)} "
               f"above the gauge by no more than wave setup)"
               if any("possible_setup" in r for r, s in zip(reasons, sel) if s) else ""))
    if fit:
        a, lag_s, b, sigma = fit
        text += f"; gauge fit a={a:.2f} lag={lag_s / 60:+.0f} min b={b:+.2f} sigma={sigma:.3f} m"
    else:
        text += "; gauge test NOT applied (no gauge data)"
    return text


def qc_series(times, values, gauge_cache: Path, wave_cache: Path = None):
    """
    For plot_7day.py. times: naive UTC datetimes, values: levels.
    Returns (flags, epochs, summary_fn, reasons) where summary_fn(since_epoch)
    gives the log line for a window. The wave cache defaults to
    waves_<WAVE_STATION>.csv next to the gauge cache.
    """
    ep = np.array([t.replace(tzinfo=timezone.utc).timestamp() for t in times])
    start = datetime.fromtimestamp(max(ep.min(), ep.max() - (REF_FIT_DAYS + 1) * 86400),
                                   tz=timezone.utc)
    end = datetime.fromtimestamp(ep.max(), tz=timezone.utc)
    reference = load_gauge(gauge_cache, start, end)
    waves = load_waves(wave_cache or gauge_cache.parent / f"waves_{WAVE_STATION}.csv")
    flags, reasons, fit = run_qc(ep, values, reference, waves)
    return flags, ep, (lambda since=None: summary(flags, reasons, fit, ep, since)), reasons


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Quality-check the GNSS-IR spline water level.")
    p.add_argument("spline_file")
    p.add_argument("--gauge-cache", default=None,
                   help="CSV cache of the Chatham gauge (default: next to the spline file)")
    p.add_argument("--plot-setup", default=None,
                   help="PNG: GNSS-IR minus the gauge against the wave-setup allowance")
    p.add_argument("--days", type=int, default=7, help="days shown by --plot-setup (default 7)")
    args = p.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from plot_7day import load_spline
    times, values = load_spline(Path(args.spline_file))
    if not times:
        print("No usable spline data found.")
        return 1
    cache = Path(args.gauge_cache) if args.gauge_cache else \
        Path(args.spline_file).parent / f"gauge_{GAUGE_STATION}.csv"
    flags, ep, summarize, reasons = qc_series(times, values, cache)
    print(summarize())
    for label, pick in (("failed", lambda f, r: f >= FAIL),
                        ("possible wave setup (kept)", lambda f, r: "possible_setup" in r)):
        days = {}
        for t, f, r in zip(times, flags, reasons):
            if pick(f, r):
                days[t.strftime("%Y-%m-%d")] = days.get(t.strftime("%Y-%m-%d"), 0) + 1
        if days:
            print(f"  {label} readings by day: " + ", ".join(f"{d} {n}" for d, n in sorted(days.items())))
    if args.plot_setup:
        plot_setup(times, values, cache, args.plot_setup, args.days)
    return 0


def plot_setup(times, values, gauge_cache, out_png, days=7):
    """
    For judging the setup test: GNSS-IR level minus the gauge-predicted
    level against the wave-setup allowance, one point per reading over
    the last `days`, coloured by outcome.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ep = np.array([t.replace(tzinfo=timezone.utc).timestamp() for t in times])
    values = np.asarray(values, dtype=float)
    start = datetime.fromtimestamp(max(ep.min(), ep.max() - (REF_FIT_DAYS + 1) * 86400), tz=timezone.utc)
    reference = load_gauge(gauge_cache, start, datetime.fromtimestamp(ep.max(), tz=timezone.utc))
    waves = load_waves(gauge_cache.parent / f"waves_{WAVE_STATION}.csv")
    flags, reasons, fit = run_qc(ep, values, reference, waves)
    if not fit:
        print("No gauge fit -- cannot plot the setup test.")
        return
    a, lag_s, b, sigma = fit
    excess = values - (a * reference_at(reference[0], reference[1], ep - lag_s) + b)
    allow = setup_allowance(ep, waves)
    limit = max(REF_LIMIT, REF_SIGMAS * sigma)
    sel = ep >= ep.max() - days * 86400
    fig, ax = plt.subplots(2, 1, figsize=(12, 8))
    t = [datetime.fromtimestamp(e, tz=timezone.utc) for e in ep[sel]]
    ax[0].fill_between(t, limit + allow[sel], -limit, color="green", alpha=0.12,
                       label="kept: within the limit, or above by no more than wave setup")
    ax[0].plot(t, np.full(len(t), limit), "k:", lw=0.8, label=f"plain limit +/-{limit:.2f} m")
    for name, m, c in (("passed", flags[sel] < SUSPECT, "tab:blue"),
                       ("possible wave setup (kept)", np.array(["possible_setup" in r for r in np.array(reasons)[sel]]), "tab:orange"),
                       ("failed", flags[sel] >= FAIL, "tab:red")):
        ax[0].plot(np.array(t)[m], excess[sel][m], ".", color=c, label=name)
    ax[0].set_ylabel("GNSS-IR minus gauge-predicted (m)")
    ax[0].legend(fontsize=8)
    ax[0].set_title(f"Wave-setup test, last {days} days (UTC)")
    ax[1].plot(allow[sel], excess[sel], ".", color="grey")
    ax[1].plot([0, allow.max() + 0.1], [limit, limit + allow.max() + 0.1], "g-", lw=1,
               label="fail line: limit + setup allowance")
    ax[1].set_xlabel(f"wave setup allowance from buoy {WAVE_STATION} (m)")
    ax[1].set_ylabel("GNSS-IR minus gauge-predicted (m)")
    ax[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    print(f"Wrote {out_png}")


if __name__ == "__main__":
    raise SystemExit(main())
