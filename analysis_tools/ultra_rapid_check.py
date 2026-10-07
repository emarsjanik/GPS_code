#!/usr/bin/env python3
"""
ultra_rapid_check.py

Same-day GNSS-IR water level from ultra-rapid orbits -- AS A CHECK,
kept entirely apart from the production results.

WHY

The production water level waits for final/rapid orbits, so it runs
one to two days behind. The camera products at Marconi need the water
level within the hour; until then they use the Chatham gauge (sigma
~0.13 m). gnssrefl's "ultra" orbits (GFZ ultra-rapid, else Wuhan,
multi-GNSS) are published within hours, so today's partial data can be
processed today. This measures how close that comes to the final
answer before anything depends on it.

WHAT IT DOES (each run)

  1. Copies today's raw file so far (raw/station_YYYYMMDD.um980; the
     receiver keeps appending to the original) and converts the copy to
     RINEX in products/refl_code_ultra/rinex.
  2. Runs rinex2snr + gnssir on it with orb=ultra -- or, when today's
     ultra-rapid file is not published yet, the broadcast orbits in the
     station's own data (orb=nav: GPS only) -- writing ONLY under
     products/refl_code_ultra (its own REFL_CODE: orbits, SNR, results).
     The production products/refl_code is never written.
  3. Copies the production results of the previous two days into that
     folder (for continuity), runs subdaily over the three days with the
     same settings as process_and_plot.sh (-rhdot True -knots 8, azimuth
     35-125), and keeps today's part of the spline as
     products/refl_code_ultra/history/ultra_<run time>.csv.
  4. Compares every earlier run's same-day levels with the production
     spline where production now covers them: n, bias, RMS. That table
     is the answer to "is ultra-rapid good enough to use?"

Usage (on the station):
    ./analysis_tools/ultra_rapid_check.sh            run once and compare
    ./analysis_tools/ultra_rapid_check.sh --compare  compare only

To stop the check: remove its crontab line. To remove its output:
    rm -rf products/refl_code_ultra
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "station"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from station_datum import spline_shift  # noqa: E402
from gnss_record import results_days, subdaily_range  # noqa: E402


def read_spline(path: Path):
    """gnssrefl spline file -> (epochs, levels), levels on NAVD88."""
    ep, lv = [], []
    if not path.exists():
        return np.array([]), np.array([])
    shift = spline_shift(path, quiet=True)
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("%") or not line.strip():
            continue
        c = line.split()
        if len(c) < 9:
            continue
        try:
            t = datetime(int(c[2]), int(c[3]), int(c[4]), int(c[5]), int(c[6]),
                         int(float(c[7])), tzinfo=timezone.utc)
            v = float(c[8])
        except ValueError:
            continue
        if abs(v) > 900:                      # gnssrefl gap marker
            continue
        ep.append(t.timestamp()); lv.append(v + shift)
    o = np.argsort(ep)
    return np.asarray(ep)[o], np.asarray(lv)[o]


def compare(ultra_dir: Path, prod_spline: Path) -> None:
    """Every saved same-day run against the production spline, where it now exists."""
    pe, pv = read_spline(prod_spline)
    hist = sorted((ultra_dir / "history").glob("ultra_*.csv"))
    print("=" * 68)
    print("  ULTRA-RAPID (same day) vs PRODUCTION (final) water level")
    print("=" * 68)
    if not hist:
        print("  no ultra-rapid runs saved yet")
        return
    if len(pe) < 2:
        print(f"  production spline not found: {prod_spline}")
        return
    print(f"  {'run':<16} {'orbit':<5} {'readings':>8} {'covered':>8} {'bias':>8} {'RMS':>7} {'max|d|':>7}  latency")
    all_d = []
    for h in hist:
        rows = [l.split(",") for l in h.read_text().splitlines()[1:] if l.strip()]
        if not rows:
            continue
        ue = np.array([float(r[0]) for r in rows]); uv = np.array([float(r[1]) for r in rows])
        parts = h.stem.split("_")                      # ultra_YYYYMMDD_HHMM[_orbit]
        run = datetime.strptime(parts[1] + "_" + parts[2], "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)
        orb = parts[3] if len(parts) > 3 else "ultra"
        lat = (run.timestamp() - ue.max()) / 3600
        m = (ue >= pe[0]) & (ue <= pe[-1])
        if not m.any():
            print(f"  {run:%Y-%m-%d %H:%M} {orb:<5} {len(ue):>8} {0:>8}  (final not available yet)  {lat:.1f} h")
            continue
        d = uv[m] - np.interp(ue[m], pe, pv)
        all_d.append(d)
        print(f"  {run:%Y-%m-%d %H:%M} {orb:<5} {len(ue):>8} {int(m.sum()):>8} {d.mean():>+8.3f} "
              f"{np.sqrt(np.mean(d ** 2)):>7.3f} {np.abs(d).max():>7.3f}  {lat:.1f} h")
    if all_d:
        d = np.concatenate(all_d)
        print(f"  {'ALL':<22} {'':>8} {len(d):>8} {d.mean():>+8.3f} {np.sqrt(np.mean(d ** 2)):>7.3f} "
              f"{np.abs(d).max():>7.3f}")
        print()
        print("  latency = run time minus the newest same-day level it produced.")
        print("  For comparison, the Chatham backup the camera products use now is")
        print("  good to ~0.13 m. Ultra-rapid is worth switching on if its RMS here")
        print("  is clearly below that.")


def main() -> int:
    p = argparse.ArgumentParser(description="Same-day GNSS-IR with ultra-rapid orbits (check only)")
    p.add_argument("--compare", action="store_true", help="only compare saved runs with production")
    p.add_argument("--days-back", type=int, default=2, help="production days copied in for continuity")
    p.add_argument("--orbits", default="ultra,nav",
                   help="orbit sources to try in order (default ultra, then broadcast nav)")
    args = p.parse_args()

    from config import Config
    cfg = Config()
    ultra = Path(cfg.products_dir) / "refl_code_ultra"
    station = (cfg.station.get("gnssrefl_station_code")
               or (cfg.station.get("station_id") or "")[:4]).lower() or "usgs"
    prod_refl = Path(cfg.station.get("gnssrefl_refl_code") or Path(cfg.products_dir) / "refl_code")
    prod_spline = prod_refl / "Files" / station / f"{station}_spline_out.txt"

    if args.compare:
        compare(ultra, prod_spline)
        return 0

    now = datetime.now(timezone.utc)
    day = now.date()
    raw = Path(cfg.raw_dir) / f"station_{day:%Y%m%d}.um980"
    if not raw.exists():
        cands = sorted(Path(cfg.raw_dir).glob("*.um980"), key=lambda q: q.stat().st_mtime)
        if not cands:
            print(f"no raw file for today in {cfg.raw_dir}")
            return 1
        raw = cands[-1]
    work = ultra / "work"
    work.mkdir(parents=True, exist_ok=True)
    snap = work / raw.name
    shutil.copy2(raw, snap)                       # the receiver keeps writing the original
    print(f"raw snapshot      : {raw.name} ({snap.stat().st_size / 1e6:.0f} MB)")

    # the same configuration, pointed at its own folders and ultra orbits -- in memory only
    cfg.rinex_dir = ultra / "rinex"
    base_station = dict(cfg.station, gnssrefl_refl_code=str(ultra / "refl_code"))

    # gnssrefl also copies the RINEX into the current directory: use our own
    os.chdir(work)
    from rinex_processor import RinexProcessor
    from gnssrefl_processor import GnssIrProcessor
    rp = RinexProcessor(cfg=cfg); rp.initialize()
    conv = rp.convert(snap)
    if not conv.success:
        print(f"RINEX conversion failed: {conv.message}")
        return 1
    # orbits in order: ultra-rapid multi-GNSS (often not yet published for the
    # current day), then the broadcast orbits in the station's own data (always
    # there; GPS only, so fewer tracks, but direction is all GNSS-IR needs)
    used = None
    for orb in args.orbits.split(","):
        cfg.station = dict(base_station, gnssrefl_orbit_source=orb)
        gp = GnssIrProcessor(cfg=cfg); gp.initialize()
        res = gp.process(Path(conv.observation_file), day)
        print(f"gnssir ({orb:<5})     : {'ok' if res.success else 'FAILED'}, {res.num_tracks} track(s)"
              + ("" if res.success else f" -- {getattr(res, 'message', '')}"))
        if res.success:
            used = orb
            break
    snap.unlink(missing_ok=True)
    for f in (conv.observation_file, conv.navigation_file, conv.sbas_file):
        try:
            Path(f).unlink()
        except (OSError, TypeError):
            pass
    if not used:
        print("no orbit source worked -- nothing to compare this run")
        return 1

    # previous days from production, for a continuous spline
    refl = ultra / "refl_code"
    for k in range(1, args.days_back + 1):
        d = day - timedelta(days=k)
        src = prod_refl / str(d.year) / "results" / station / f"{d.timetuple().tm_yday:03d}.txt"
        dst = refl / str(d.year) / "results" / station / src.name
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    env = dict(os.environ, REFL_CODE=str(refl), ORBITS=str(refl / "orbits"), EXE=str(refl / "exe"))
    # the days actually there, across the year boundary on 1-2 January
    # (a single-year call with doy1 > doy2 makes subdaily exit 0 and write nothing)
    days = [d for d in results_days(refl, station) if day - timedelta(days=args.days_back) <= d <= day]
    try:
        y1, d1, y2, d2 = subdaily_range(days)
    except ValueError as exc:
        print(f"nothing to fit: {exc}")
        return 1
    spline = refl / "Files" / station / f"{station}_spline_out.txt"
    spline.unlink(missing_ok=True)                # so last run's spline is never read as this one's
    cmd = ["subdaily", station, str(y1), "-doy1", str(d1), "-year_end", str(y2), "-doy2", str(d2),
           "-rhdot", "True", "-knots", "8", "-azim1", "35", "-azim2", "125"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0 or not spline.exists():
        print(f"subdaily wrote no spline (exit status {r.returncode}):\n" + (r.stderr or r.stdout)[-1500:])
        return 1
    ue, uv = read_spline(spline)
    t0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    m = ue >= t0
    hist = ultra / "history"
    hist.mkdir(exist_ok=True)
    out = hist / f"ultra_{now:%Y%m%d_%H%M}_{used}.csv"
    out.write_text("epoch,level\n" + "".join(f"{e:.0f},{v:.4f}\n" for e, v in zip(ue[m], uv[m])))
    if m.any():
        print(f"same-day levels   : {int(m.sum())} up to {datetime.fromtimestamp(ue[m].max(), tz=timezone.utc):%H:%M}Z "
              f"({(now.timestamp() - ue[m].max()) / 3600:.1f} h ago) -> {out}")
    else:
        print("same-day levels   : none yet (too little of today's data?)")
    print()
    compare(ultra, prod_spline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
