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


def run_qc(ep, lv, reference=None):
    """
    ep: epochs (s, UTC), lv: levels (m). reference: (r_ep, r_lv) or None.
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
            for i in np.flatnonzero(np.isfinite(predicted) & (np.abs(lv - predicted) > limit)):
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
            + f", {int(((flags == SUSPECT) & sel).sum())} suspect kept")
    if fit:
        a, lag_s, b, sigma = fit
        text += f"; gauge fit a={a:.2f} lag={lag_s / 60:+.0f} min b={b:+.2f} sigma={sigma:.3f} m"
    else:
        text += "; gauge test NOT applied (no gauge data)"
    return text


def qc_series(times, values, gauge_cache: Path):
    """
    For plot_7day.py. times: naive UTC datetimes, values: levels.
    Returns (flags, summary_fn) where summary_fn(since_epoch) gives the
    log line for a window.
    """
    ep = np.array([t.replace(tzinfo=timezone.utc).timestamp() for t in times])
    start = datetime.fromtimestamp(max(ep.min(), ep.max() - (REF_FIT_DAYS + 1) * 86400),
                                   tz=timezone.utc)
    end = datetime.fromtimestamp(ep.max(), tz=timezone.utc)
    reference = load_gauge(gauge_cache, start, end)
    flags, reasons, fit = run_qc(ep, values, reference)
    return flags, ep, (lambda since=None: summary(flags, reasons, fit, ep, since))


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Quality-check the GNSS-IR spline water level.")
    p.add_argument("spline_file")
    p.add_argument("--gauge-cache", default=None,
                   help="CSV cache of the Chatham gauge (default: next to the spline file)")
    args = p.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from plot_7day import load_spline
    times, values = load_spline(Path(args.spline_file))
    if not times:
        print("No usable spline data found.")
        return 1
    cache = Path(args.gauge_cache) if args.gauge_cache else \
        Path(args.spline_file).parent / f"gauge_{GAUGE_STATION}.csv"
    flags, _, summarize = qc_series(times, values, cache)
    print(summarize())
    days = {}
    for t, f in zip(times, flags):
        if f >= FAIL:
            days[t.strftime("%Y-%m-%d")] = days.get(t.strftime("%Y-%m-%d"), 0) + 1
    if days:
        print("  failed readings by day: " + ", ".join(f"{d} {n}" for d, n in sorted(days.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
