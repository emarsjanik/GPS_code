#!/usr/bin/env python3
"""
One-time full-archive GNSS-R vs tide diagnostic for the current Marconi NUC.

It searches existing processed CSV/TXT tables, identifies GNSS-R reflector-height
observations and a tide-model table, applies the established +0.242 m datum test,
and creates full-record overlay/track plots.

No existing data are modified.
"""

from pathlib import Path
import argparse
import math
import re
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

H_ORTHO_M = 18.665
DATUM_SHIFT_M = 0.242


def norm(x):
    return re.sub(r"[^a-z0-9]+", "", str(x).lower())


def col(df, names):
    d = {norm(c): c for c in df.columns}
    for n in names:
        if norm(n) in d:
            return d[norm(n)]
    return None


def datetime_series(df):
    for names in [
        ["datetime"], ["date_time"], ["timestamp"], ["time_utc"],
        ["utc_time"], ["observation_time"], ["obs_time"]
    ]:
        c = col(df, names)
        if c:
            x = pd.to_datetime(df[c], errors="coerce", utc=True)
            if x.notna().sum() >= max(3, int(len(df) * .25)):
                return x, c

    dc = col(df, ["date", "obs_date"])
    tc = col(df, ["time", "obs_time", "utc_time"])
    if dc and tc:
        x = pd.to_datetime(
            df[dc].astype(str) + " " + df[tc].astype(str),
            errors="coerce", utc=True
        )
        if x.notna().sum() >= max(3, int(len(df) * .25)):
            return x, dc + "+" + tc
    return None, None


def read_table(p):
    try:
        return pd.read_excel(p) if p.suffix.lower() in {".xlsx", ".xls"} else pd.read_csv(p, low_memory=False)
    except Exception:
        return None


def is_gnssr(df):
    if df is None or len(df) == 0:
        return False
    return (
        col(df, ["rh", "rh_m", "reflector_height", "reflector_height_m"]) is not None
        and col(df, ["sat", "prn", "satellite"]) is not None
        and datetime_series(df)[0] is not None
    )


def is_tide(df):
    if df is None or len(df) == 0:
        return False
    return (
        col(df, ["EOT20_heightm", "EOT20", "eot20", "tide_height_m",
                 "tide_height", "tide", "water_level_m", "water_level"]) is not None
        and datetime_series(df)[0] is not None
    )


def discover(root):
    skip = {"gnssrefl_venv", ".git", "__pycache__", ".cache", "site-packages", "node_modules"}
    gnssr, tide = [], []

    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in {".csv", ".txt", ".xlsx", ".xls"}:
            continue
        if any(s in p.parts for s in skip):
            continue
        if "marconi_all_data_tide_check" in p.parts:
            continue

        df = read_table(p)
        if df is None:
            continue

        if is_gnssr(df):
            score = 0
            n = p.name.lower()
            if "gnss" in n: score += 5
            if "refl" in n or "gnssir" in n: score += 5
            if "track" in n: score += 2
            if "datum" in n: score += 2
            gnssr.append((score, p, df))

        if is_tide(df):
            score = 0
            n = p.name.lower()
            tc = col(df, ["EOT20_heightm", "EOT20", "eot20"])
            if tc: score += 20
            if "eot20" in n: score += 20
            if "tide" in n: score += 10
            tide.append((score, p, df, tc or col(df, [
                "tide_height_m", "tide_height", "tide",
                "water_level_m", "water_level"
            ])))

    return sorted(gnssr, key=lambda x: (-x[0], str(x[1]))),            sorted(tide, key=lambda x: (-x[0], str(x[1])))


def normalize_gnssr(items):
    pieces = []
    for score, p, df in items:
        dt, dt_source = datetime_series(df)
        rc = col(df, ["rh_m", "rh", "reflector_height_m", "reflector_height"])
        sc = col(df, ["sat", "prn", "satellite"])
        if dt is None or rc is None:
            continue

        out = pd.DataFrame({
            "time": dt,
            "rh_m": pd.to_numeric(df[rc], errors="coerce"),
            "sat": df[sc].astype(str),
            "source_file": str(p),
        }).dropna(subset=["time", "rh_m"])

        out = out[np.isfinite(out["rh_m"])]
        if len(out):
            out["gnssr_water_level_navd88_m"] = H_ORTHO_M - out["rh_m"] + DATUM_SHIFT_M
            pieces.append(out)

    if not pieces:
        raise RuntimeError("No usable GNSS-R observations found.")

    x = pd.concat(pieces, ignore_index=True)
    return x.drop_duplicates(["time", "sat", "rh_m"]).sort_values("time")


def normalize_tide(item):
    score, p, df, tc = item
    dt, _ = datetime_series(df)
    x = pd.DataFrame({
        "time": dt,
        "tide_model_m": pd.to_numeric(df[tc], errors="coerce")
    }).dropna()
    x = x[np.isfinite(x["tide_model_m"])].sort_values("time").drop_duplicates("time")
    if not len(x):
        raise RuntimeError("Selected tide file contains no usable values.")
    return x, p, tc


def stats(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if len(a) < 2:
        return {}
    r = np.corrcoef(a, b)[0, 1]
    slope = np.polyfit(b, a, 1)[0]
    e = a - b
    return {
        "n": len(a),
        "correlation": r,
        "slope": slope,
        "mean_bias_m": e.mean(),
        "median_bias_m": np.median(e),
        "rms_m": math.sqrt(np.mean(e * e)),
        "mae_m": np.mean(np.abs(e)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path.home() / "GNSS" / "v4.1")
    ap.add_argument("--tide", type=Path, default=None)
    ap.add_argument("--out", type=Path,
                    default=Path.home() / "GNSS" / "v4.1" / "marconi_all_data_tide_check")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    print("=" * 96)
    print("MARCONI ALL-DATA GNSS-R / TIDE CHECK")
    print("=" * 96)
    print(f"Root: {args.root}")
    print(f"H_ortho: {H_ORTHO_M:.3f} m")
    print(f"Datum test: +{DATUM_SHIFT_M:.3f} m")
    print()

    gnss_items, tide_items = discover(args.root)

    print("GNSS-R TABLES FOUND")
    print("-" * 96)
    for score, p, df in gnss_items:
        print(f"score={score:2d}  {p}")

    gnss = normalize_gnssr(gnss_items)
    print(f"\nTOTAL UNIQUE GNSS-R OBSERVATIONS: {len(gnss)}")
    print(f"EARLIEST: {gnss.time.min()}")
    print(f"LATEST:   {gnss.time.max()}")
    print(f"PRNs:     {gnss.sat.nunique()}")

    if args.tide:
        df = read_table(args.tide)
        if df is None or not is_tide(df):
            raise RuntimeError(f"Could not identify a tide table in {args.tide}")
        tc = col(df, ["EOT20_heightm", "EOT20", "eot20", "tide_height_m",
                       "tide_height", "tide", "water_level_m", "water_level"])
        tide, tide_path, tide_col = normalize_tide((0, args.tide, df, tc))
    else:
        if not tide_items:
            raise RuntimeError("No tide model found. Re-run with --tide /full/path/to/tide.csv")
        print("\nTIDE CANDIDATES")
        print("-" * 96)
        for score, p, df, tc in tide_items[:10]:
            print(f"score={score:2d}  column={tc:25s}  {p}")
        tide, tide_path, tide_col = normalize_tide(tide_items[0])

    print(f"\nSELECTED TIDE: {tide_path}")
    print(f"TIDE COLUMN:   {tide_col}")

    ts = tide.time.astype("int64").to_numpy() / 1e9
    gs = gnss.time.astype("int64").to_numpy() / 1e9
    inside = (gs >= ts.min()) & (gs <= ts.max())

    c = gnss.loc[inside].copy()
    c["tide_model_m"] = np.interp(gs[inside], ts, tide.tide_model_m.to_numpy())
    c["residual_m"] = c.gnssr_water_level_navd88_m - c.tide_model_m
    s = stats(c.gnssr_water_level_navd88_m, c.tide_model_m)

    print("\n" + "=" * 96)
    print("FULL-ARCHIVE RESULT")
    print("=" * 96)
    print(f"GNSS-R observations in tide overlap: {len(c)}")
    if s:
        print(f"Correlation:  {s['correlation']:+.4f}")
        print(f"Free slope:   {s['slope']:+.4f}")
        print(f"Mean bias:    {s['mean_bias_m']:+.4f} m ({s['mean_bias_m']*100:+.2f} cm)")
        print(f"Median bias:  {s['median_bias_m']:+.4f} m ({s['median_bias_m']*100:+.2f} cm)")
        print(f"RMS:          {s['rms_m']:.4f} m ({s['rms_m']*100:.2f} cm)")
        print(f"MAE:          {s['mae_m']:.4f} m ({s['mae_m']*100:.2f} cm)")

    # Main overlay: GNSS-R points + continuous tide line.
    fig, ax = plt.subplots(figsize=(16, 7))
    ax.plot(tide.time, tide.tide_model_m, linewidth=1.7, label="Tide model")
    ax.scatter(c.time, c.gnssr_water_level_navd88_m,
               s=9, alpha=.45, label="All GNSS-R observations")
    ax.set_title("Marconi — ALL Available GNSS-R Data vs Tide Model")
    ax.set_xlabel("UTC")
    ax.set_ylabel("Water level (m NAVD88)")
    ax.grid(True, alpha=.25)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    overlay = args.out / "marconi_all_data_tide_overlay.png"
    fig.savefig(overlay, dpi=180)
    plt.close(fig)

    # Track-resolved plot.
    fig, ax = plt.subplots(figsize=(16, 8))
    ax.plot(tide.time, tide.tide_model_m, linewidth=2, label="Tide model")
    for sat, g in c.groupby("sat", sort=True):
        ax.plot(g.time, g.gnssr_water_level_navd88_m,
                marker=".", linewidth=.7, markersize=3, alpha=.6,
                label=f"PRN {sat}")
    ax.set_title("Marconi — All GNSS-R Tracks vs Tide Model")
    ax.set_xlabel("UTC")
    ax.set_ylabel("Water level (m NAVD88)")
    ax.grid(True, alpha=.25)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    tracks = args.out / "marconi_all_data_tide_tracks.png"
    fig.savefig(tracks, dpi=180, bbox_inches="tight")
    plt.close(fig)

    csv = args.out / "marconi_all_data_tide_comparison.csv"
    summary = args.out / "marconi_all_data_tide_summary.txt"
    c.to_csv(csv, index=False)

    with open(summary, "w") as f:
        f.write("MARCONI ALL-DATA GNSS-R / TIDE CHECK\n")
        f.write("=" * 96 + "\n")
        f.write(f"GNSS root: {args.root}\n")
        f.write(f"H_ortho: {H_ORTHO_M:.3f} m\n")
        f.write(f"Datum test: +{DATUM_SHIFT_M:.3f} m\n")
        f.write(f"Tide file: {tide_path}\n")
        f.write(f"Tide column: {tide_col}\n")
        f.write(f"GNSS-R observations discovered: {len(gnss)}\n")
        f.write(f"GNSS-R observations in tide overlap: {len(c)}\n")
        f.write(f"Unique PRNs: {gnss.sat.nunique()}\n")
        f.write(f"GNSS-R start: {gnss.time.min()}\n")
        f.write(f"GNSS-R end: {gnss.time.max()}\n")
        if s:
            for k, v in s.items():
                f.write(f"{k}: {v}\n")
        f.write("\nGNSS-R source files:\n")
        for p in sorted(gnss.source_file.unique()):
            f.write(f"  {p}\n")

    print("\n" + "=" * 96)
    print("OUTPUTS")
    print("=" * 96)
    print(f"Overlay:    {overlay}")
    print(f"Tracks:     {tracks}")
    print(f"CSV:        {csv}")
    print(f"Summary:    {summary}")
    print("DONE")


if __name__ == "__main__":
    main()
