#!/usr/bin/env python3
"""
migrate_to_navd88.py

One-time move of this station's configuration onto NAVD88. Run once on
the station computer, then re-run processing so gnssrefl rewrites the
whole-record spline with the NAVD88 antenna height.

WHAT IT CHANGES (station/resources/station.json, backed up first)

  gnssrefl_orthometric_height   18.665 (CGVD2013) -> 19.014 m NAVD88
                                (NGS OPUS 2026-09-28, GEOID18)
  vertical_datum                "NAVD88 (GEOID18)" -- declares it, so no
                                script has to guess
  water_level_msl_offset        + the same 0.349 m, if set: it is the
                                height of local mean sea level in the
                                datum of the water levels, so it moves
                                with them and the public 7-day plot
                                does not jump
  tide_model_navd88_offset      +0.09 m, if not already set (the tide
                                model sits ~0.09 m below NAVD88; see
                                analysis_tools/station_datum.py)

and "Hortho" in gnssrefl's own analysis json
($REFL_CODE/input/<station>.json), which is what subdaily actually
reads. The pipeline rewrites that json from station.json at every
start, but a night with no new raw data does not start the pipeline,
so it is updated here too.

Reflector heights do not depend on Hortho (water level = Hortho - RH),
so nothing needs reprocessing from RINEX: the next subdaily run, or
process_and_plot.sh, writes the same record 0.349 m higher.

AFTERWARDS

    ./process_and_plot.sh                          # spline on NAVD88
    ./maintenance/s3UploadTimeseries.sh --backfill # every month re-uploaded

Already-uploaded monthly files are also converted by filter_month.py
whichever height wrote the spline, so the backfill alone corrects the
archive.

Safe to run twice: a configuration already on NAVD88 is left alone.

Usage:
    python3 maintenance/migrate_to_navd88.py            (show, then ask)
    python3 maintenance/migrate_to_navd88.py --yes      (no prompt)
    python3 maintenance/migrate_to_navd88.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis_tools"))
from station_datum import (CGVD2013_HORTHO_M, DEFAULT_TIDE_MODEL_TO_NAVD88_M,  # noqa: E402
                           NAVD88_HORTHO_M, PROJECT_DIR, STATION_JSON,
                           declared_navd88)

DATUM_LABEL = "NAVD88 (GEOID18)"


def gnssrefl_jsons(cfg: dict) -> list[Path]:
    """gnssrefl's analysis json(s) for this station, wherever this
    gnssrefl version keeps them."""
    refl = Path(cfg.get("gnssrefl_refl_code") or PROJECT_DIR / "products" / "refl_code")
    code = (cfg.get("gnssrefl_station_code") or str(cfg.get("station_id", ""))[:4]).lower()
    if not code:
        return []
    cands = [refl / "input" / f"{code}.json", refl / "input" / code / f"{code}.json"]
    return [p for p in cands if p.exists()]


def backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.pre_navd88_{stamp}")
    shutil.copy2(path, dest)
    return dest


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station-json", default=str(STATION_JSON))
    ap.add_argument("--navd88-height", type=float, default=NAVD88_HORTHO_M,
                    help=f"antenna height, m NAVD88 (default {NAVD88_HORTHO_M})")
    ap.add_argument("--yes", action="store_true", help="apply without asking")
    ap.add_argument("--dry-run", action="store_true", help="show the changes only")
    args = ap.parse_args()

    path = Path(args.station_json)
    if not path.exists():
        print(f"station.json not found: {path}")
        return 1
    cfg = json.loads(path.read_text())

    old_h = cfg.get("gnssrefl_orthometric_height")
    if old_h is None:
        print("gnssrefl_orthometric_height is not set -- nothing to migrate.")
        return 1
    old_h = float(old_h)
    new_h = args.navd88_height

    if declared_navd88(cfg) and abs(old_h - new_h) < 0.0005:
        print(f"Already on NAVD88: Hortho {old_h:.3f} m, vertical_datum "
              f"{cfg.get('vertical_datum')!r}. Nothing to do.")
        changes, delta = [], 0.0
    else:
        if declared_navd88(cfg):
            print(f"station.json already says NAVD88 but Hortho is {old_h:.3f} m, "
                  f"not {new_h:.3f} m. Check the survey before going on; "
                  f"pass --navd88-height to keep {old_h:.3f}.")
            return 1
        if abs(old_h - CGVD2013_HORTHO_M) > 0.0005:
            print(f"WARNING: Hortho {old_h:.3f} m is not the known CGVD2013 value "
                  f"{CGVD2013_HORTHO_M} m; the shift below is simply "
                  f"{new_h:.3f} - {old_h:.3f}.")
        delta = new_h - old_h
        changes = [("gnssrefl_orthometric_height", old_h, new_h),
                   ("vertical_datum", cfg.get("vertical_datum"), DATUM_LABEL)]
        msl = cfg.get("water_level_msl_offset")
        if msl is not None:
            changes.append(("water_level_msl_offset", float(msl),
                            round(float(msl) + delta, 4)))

    if cfg.get("tide_model_navd88_offset") is None:
        changes.append(("tide_model_navd88_offset", None, DEFAULT_TIDE_MODEL_TO_NAVD88_M))

    jsons = []
    for jp in gnssrefl_jsons(cfg):
        try:
            jd = json.loads(jp.read_text())
        except Exception as exc:
            print(f"  could not read {jp}: {exc}")
            continue
        h = jd.get("Hortho")
        if h != [new_h]:
            jsons.append((jp, jd, h))

    if not changes and not jsons:
        return 0

    print(f"station.json: {path}")
    for key, a, b in changes:
        print(f"  {key:30s} {a!s:>22} -> {b}")
    for jp, _, h in jsons:
        print(f"gnssrefl json: {jp}\n  {'Hortho':30s} {h!s:>22} -> {[new_h]}")
    if delta:
        print(f"\nEvery GNSS-IR water level moves {delta:+.3f} m (onto NAVD88).")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if not args.yes:
        if input("\nApply? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing written.")
            return 1

    if changes:
        print(f"  backup: {backup(path)}")
        for key, _, b in changes:
            cfg[key] = b
        write_json(path, cfg)
        print(f"  wrote {path}")
    for jp, jd, _ in jsons:
        print(f"  backup: {backup(jp)}")
        jd["Hortho"] = [new_h]
        write_json(jp, jd)
        print(f"  wrote {jp}")

    print("\nNext:\n"
          "    ./process_and_plot.sh\n"
          "    ./maintenance/s3UploadTimeseries.sh --backfill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
