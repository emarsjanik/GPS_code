#!/usr/bin/env python3
"""
export_twl.py

Observed total water level (TWL) at Marconi from GNSS-IR, for validating
a total water level model (flooding and dune-erosion forecasts).

WHAT GNSS-IR MEASURES IN A STORM. The footprint is the surf and swash
zone. In calm seas the reflecting surface is the sea, and the reading is
the still-water level (tide + surge). In heavy surf the footprint takes
in water running up the beach, and the reading rises towards the total
water level at the shore: tide + surge + wave setup + runup. In the
late-Sep 2026 storm the readings sat 2.0-3.3 m above the measured
Chatham level, against a Stockdon 2% runup of ~2.4-2.9 m.
water_level_qc.py keeps such readings as "possible_runup".

OUTPUT <output>/observed_twl.csv, one row per GNSS-IR reading (NAVD88, UTC):
    gnss_navd88          the GNSS-IR level
    qc_flag, qc_reason   1 good / 3 suspect / 4 fail, and why
                         (possible_setup, possible_runup, reference, ...)
    swl_navd88           still-water level at Marconi from the measured
                         Chatham gauge (tide + surge), via the robust
                         a * gauge(t - lag) + b fit of water_level_qc.py
    hs_m, tp_s           NDBC buoy waves (WAVE_STATION)
    setup_m, r2_m        Stockdon et al. (2006) setup and 2% runup
    twl_stockdon_navd88  swl + r2: a simple TWL model built from the same
                         inputs, as a baseline
    gnss_minus_swl_m     what the GNSS saw above still water
    model_twl_navd88     the TWL model under test, if --model is given

REPORT <output>/twl_validation.txt and twl_validation.png:
  * how much of the Stockdon runup the GNSS-IR sees in heavy surf
    (gnss_minus_swl against r2 for the runup-flagged readings);
  * against the model (--model): bias, RMSE and r for the storm
    (runup) readings, and for the DAILY MAXIMA of storm days, the usual
    way TWL forecasts are scored against flooding and dune-impact
    thresholds. Calm readings are still water, not TWL, so they are not
    compared: a TWL model sits above them by ~R2 by construction.

The model file needs a time column and a TWL column in metres NAVD88
(--model-time-col, --model-twl-col; --model-offset to shift a different
datum onto NAVD88; --model-tz for naive times, default UTC).

Usage:
    python3 analysis_tools/export_twl.py products/refl_code/Files/usgs/usgs_spline_out.txt \\
        --output products/refl_code/Files/usgs/twl \\
        [--model twl_forecast.csv --model-time-col time --model-twl-col twl]
"""

from __future__ import annotations

import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import water_level_qc as q                      # noqa: E402
from plot_7day import load_spline               # noqa: E402

MODEL_MAX_GAP_S = 90 * 60


def load_model(path, time_col, twl_col, tz, offset):
    import pandas as pd
    m = pd.read_excel(path) if str(path).endswith((".xlsx", ".xls")) else pd.read_csv(path)
    t = pd.to_datetime(m[time_col])
    if t.dt.tz is None:
        t = t.dt.tz_localize(tz)
    t = t.dt.tz_convert("UTC")
    ep = ((t - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy(float)
    lv = pd.to_numeric(m[twl_col], errors="coerce").to_numpy(float) + offset
    ok = np.isfinite(ep) & np.isfinite(lv)
    order = np.argsort(ep[ok])
    return ep[ok][order], lv[ok][order]


def at(ep_src, lv_src, epochs, max_gap_s):
    out = np.interp(epochs, ep_src, lv_src, left=np.nan, right=np.nan)
    if len(ep_src) > 1:
        i = np.clip(np.searchsorted(ep_src, epochs), 1, len(ep_src) - 1)
        out[(ep_src[i] - ep_src[i - 1]) > max_gap_s] = np.nan
    return out


def stats(obs, mod):
    ok = np.isfinite(obs) & np.isfinite(mod)
    if ok.sum() < 3:
        return f"n={int(ok.sum())} (too few)"
    d = mod[ok] - obs[ok]
    r = np.corrcoef(obs[ok], mod[ok])[0, 1] if ok.sum() > 2 else np.nan
    return f"n={int(ok.sum())}  bias (model - GNSS) {d.mean():+.2f} m  RMSE {np.sqrt(np.mean(d ** 2)):.2f} m  r {r:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Observed total water level from GNSS-IR, and TWL model validation.")
    ap.add_argument("spline_file")
    ap.add_argument("--output", default="twl")
    ap.add_argument("--gauge-cache", default=None, help="default: next to the spline file")
    ap.add_argument("--wave-cache", default=None, help="default: next to the gauge cache")
    ap.add_argument("--beta", type=float, default=q.SETUP_BETA, help="foreshore slope for Stockdon")
    ap.add_argument("--model", default=None, help="TWL model output (CSV/XLSX), m NAVD88")
    ap.add_argument("--model-time-col", default="time")
    ap.add_argument("--model-twl-col", default="twl")
    ap.add_argument("--model-tz", default="UTC")
    ap.add_argument("--model-offset", type=float, default=0.0, help="m added to the model (datum shift)")
    args = ap.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    times, values = load_spline(Path(args.spline_file))
    if not times:
        print("No usable spline data found.")
        return 1
    ep = np.array([t.replace(tzinfo=timezone.utc).timestamp() for t in times])
    lv = np.asarray(values, dtype=float)
    gauge_cache = Path(args.gauge_cache) if args.gauge_cache else \
        Path(args.spline_file).parent / f"gauge_{q.GAUGE_STATION}.csv"
    start = datetime.fromtimestamp(max(ep.min(), ep.max() - (q.REF_FIT_DAYS + 1) * 86400), tz=timezone.utc)
    reference = q.load_gauge(gauge_cache, start, datetime.fromtimestamp(ep.max(), tz=timezone.utc))
    waves = q.load_waves(Path(args.wave_cache) if args.wave_cache else
                         gauge_cache.parent / f"waves_{q.WAVE_STATION}.csv")
    flags, reasons, fit = q.run_qc(ep, lv, reference, waves)
    if not fit:
        print("No gauge fit (no Chatham data): the still-water level cannot be formed.")
        return 1
    a, lag_s, b, _ = fit
    swl = a * q.reference_at(reference[0], reference[1], ep - lag_s) + b
    hs, tp = q.waves_at(ep, waves)
    setup, r2 = q.stockdon(hs, tp, args.beta)
    twl_sd = swl + r2
    excess = lv - swl
    runup = q.is_runup(reasons)

    model = np.full(len(ep), np.nan)
    if args.model:
        m_ep, m_lv = load_model(args.model, args.model_time_col, args.model_twl_col,
                                args.model_tz, args.model_offset)
        model = at(m_ep, m_lv, ep, MODEL_MAX_GAP_S)

    import csv
    with open(out / "observed_twl.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "epoch", "gnss_navd88", "qc_flag", "qc_reason", "swl_navd88",
                    "hs_m", "tp_s", "setup_m", "r2_m", "twl_stockdon_navd88",
                    "gnss_minus_swl_m", "model_twl_navd88"])
        fmt = lambda x: "" if not np.isfinite(x) else f"{x:.3f}"
        for i in range(len(ep)):
            w.writerow([datetime.fromtimestamp(ep[i], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        int(ep[i]), fmt(lv[i]), int(flags[i]), reasons[i], fmt(swl[i]),
                        fmt(hs[i]), fmt(tp[i]), fmt(setup[i]), fmt(r2[i]), fmt(twl_sd[i]),
                        fmt(excess[i]), fmt(model[i])])

    good = flags < q.FAIL
    lines = [f"Observed total water level, {len(ep)} GNSS-IR readings "
             f"{times[0]:%Y-%m-%d} to {times[-1]:%Y-%m-%d} (UTC), beta {args.beta}",
             f"  still water from Chatham: a={a:.2f} lag={lag_s / 60:+.0f} min b={b:+.2f} m",
             f"  kept as total water level (runup): {int(runup.sum())};  "
             f"failed: {int((flags >= q.FAIL).sum())}", ""]
    ok = runup & np.isfinite(r2) & (r2 > 0)
    if ok.sum() >= 3:
        k = float(np.sum(excess[ok] * r2[ok]) / np.sum(r2[ok] ** 2))
        lines += ["How much of the Stockdon 2% runup the GNSS-IR sees in heavy surf:",
                  f"  GNSS - still water ~ {k:.2f} x R2 over {int(ok.sum())} runup readings "
                  f"(R2 {np.nanmin(r2[ok]):.2f}-{np.nanmax(r2[ok]):.2f} m, "
                  f"excess {np.nanmin(excess[ok]):.2f}-{np.nanmax(excess[ok]):.2f} m)",
                  "  ~1 means it reads the 2% runup level; ~0.5 a mean swash level. Use this factor",
                  "  before comparing with a model's R2-based TWL.", ""]
    lines.append("GNSS-IR vs the Stockdon baseline TWL (swl + R2):")
    lines.append(f"  runup readings      {stats(lv[runup], twl_sd[runup])}")
    if args.model:
        lines += ["", f"GNSS-IR vs the TWL model ({Path(args.model).name}):",
                  f"  runup readings      {stats(lv[runup], model[runup])}"]
        # Daily maxima on STORM days only (days with runup readings): on
        # calm days the GNSS-IR reads still water, so a TWL model sits
        # above it by about R2 by construction, not by error.
        days = {}
        for i in np.flatnonzero(good):
            d = datetime.fromtimestamp(ep[i], tz=timezone.utc).strftime("%Y-%m-%d")
            o, mm, st = days.get(d, (-np.inf, -np.inf, False))
            days[d] = (max(o, lv[i]), max(mm, model[i]) if np.isfinite(model[i]) else mm,
                       st or bool(runup[i]))
        dm = np.array([(o, mm) for o, mm, st in days.values() if st and mm > -np.inf])
        if len(dm):
            lines.append(f"  storm-day maxima    {stats(dm[:, 0], dm[:, 1])}")
        lines.append("  (calm readings are still water, not TWL, and are not compared)")
    report = "\n".join(lines)
    (out / "twl_validation.txt").write_text(report + "\n")
    print(report)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sel = ep >= ep.max() - 14 * 86400
    t = np.array([datetime.fromtimestamp(e, tz=timezone.utc) for e in ep[sel]])
    fig, ax = plt.subplots(2, 1, figsize=(13, 8))
    ax[0].plot(t, swl[sel], color="grey", lw=1, label="still water (Chatham, measured)")
    ax[0].plot(t, twl_sd[sel], color="tab:green", lw=1, label="Stockdon TWL (still water + R2)")
    if args.model:
        ax[0].plot(t, model[sel], color="tab:orange", lw=1.2, label="TWL model")
    for name, m, st in (("GNSS-IR, still water", good[sel] & ~runup[sel], dict(color="tab:blue", ms=3)),
                        ("GNSS-IR, total water level (runup)", runup[sel], dict(color="purple", ms=4)),
                        ("GNSS-IR, failed", ~good[sel], dict(color="tab:red", ms=3))):
        ax[0].plot(t[m], lv[sel][m], "o", label=name, **st)
    ax[0].set_ylabel("m NAVD88")
    ax[0].legend(fontsize=8, ncol=2)
    ax[0].set_title("Total water level at Marconi, last 14 days (UTC)")
    ax[1].plot(r2[runup], excess[runup], "o", color="purple", ms=4, label="runup readings")
    ax[1].plot(r2[good & ~runup], excess[good & ~runup], ".", color="tab:blue", ms=2, alpha=0.4,
               label="other kept readings")
    top = float(np.nanmax(r2)) if np.isfinite(r2).any() else 1.0
    ax[1].plot([0, top], [0, top], "k:", lw=0.8, label="GNSS - still water = R2")
    ax[1].set_xlabel("Stockdon 2% runup R2 (m)")
    ax[1].set_ylabel("GNSS-IR minus still water (m)")
    ax[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "twl_validation.png", dpi=110)
    print(f"\nWrote {out / 'observed_twl.csv'}, twl_validation.txt and twl_validation.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
