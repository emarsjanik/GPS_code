#!/usr/bin/env python3
"""
Report the exact raster extension needed to physically validate the
statistically strong but incompletely covered Marconi tracks.

Reads:
  marconi_final_physical_track_summary.csv
  2021022FA_Marconi_topobathy_1m.tif

Reports, for C_INSUFFICIENT_RASTER_COVERAGE tracks:
  predicted 9° footprint bounding box
  current raster bounding box
  required extension west/east/south/north
  whether the missing area is primarily eastward (ocean-side candidate)

Also reports the current final A/B tracks.
"""

from pathlib import Path
import math
import pandas as pd
import rasterio

SUMMARY = Path("marconi_final_physical_track_summary.csv")
RASTER = Path("2021022FA_Marconi_topobathy_1m.tif")

def main():
    df = pd.read_csv(SUMMARY)

    with rasterio.open(RASTER) as src:
        b = src.bounds

        print()
        print("=" * 96)
        print("MARCONI PHYSICAL TRACK / TOPObATHY EXTENSION REPORT")
        print("=" * 96)
        print(f"Raster bounds:")
        print(f"  West : {b.left:.3f}")
        print(f"  East : {b.right:.3f}")
        print(f"  South: {b.bottom:.3f}")
        print(f"  North: {b.top:.3f}")
        print()
        print("TRACKS")
        print("-" * 96)

        for _, r in df.iterrows():
            cls = str(r["geometry_class"])

            print(
                f"PRN {int(r['sat']):2d} "
                f"{'RISING' if int(r['rise']) == 1 else 'SETTING':7s} "
                f"Az={r['az_mean_deg']:7.2f} "
                f"class={cls}"
            )

            if cls != "C_INSUFFICIENT_RASTER_COVERAGE":
                print(
                    f"  9° wet={r['wet_9']:.3f} "
                    f"9° coverage={r['coverage_9']:.3f}"
                )
                continue

            vals = {}
            for key in [
                "pred9_min_e",
                "pred9_max_e",
                "pred9_min_n",
                "pred9_max_n",
            ]:
                vals[key] = float(r[key])

            west = max(0.0, b.left - vals["pred9_min_e"])
            east = max(0.0, vals["pred9_max_e"] - b.right)
            south = max(0.0, b.bottom - vals["pred9_min_n"])
            north = max(0.0, vals["pred9_max_n"] - b.top)

            print(
                f"  9° coverage = {r['coverage_9']:.3f}"
            )
            print(
                f"  9° wet fraction of covered cells = "
                f"{r['wet_9'] if math.isfinite(r['wet_9']) else float('nan'):.3f}"
            )
            print(
                f"  9° footprint bbox:"
                f" E {vals['pred9_min_e']:.3f} to {vals['pred9_max_e']:.3f},"
                f" N {vals['pred9_min_n']:.3f} to {vals['pred9_max_n']:.3f}"
            )
            print(
                f"  additional raster required:"
                f" west={west:.1f} m"
                f" east={east:.1f} m"
                f" south={south:.1f} m"
                f" north={north:.1f} m"
            )

        accepted = df[
            df["geometry_class"].astype(str).str.startswith(("A_", "B_"))
        ]

        print()
        print("=" * 96)
        print("CURRENT PHYSICAL ACCEPTANCE")
        print("=" * 96)
        print(
            accepted[
                [
                    "sat",
                    "rise",
                    "az_mean_deg",
                    "n",
                    "tide_r",
                    "corrected_rms_m",
                    "wet_9",
                    "wet_13",
                    "geometry_class",
                ]
            ].to_string(index=False)
        )

        print()
        print("Interpretation:")
        print(
            "Tracks with strong statistics but insufficient 9° raster coverage "
            "are not rejected as non-ocean; they are classified indeterminate."
        )
        print(
            "An eastward extension is especially important for reflection "
            "azimuths in the 90–135° sector if the missing bbox is east of the raster."
        )
        print()

if __name__ == "__main__":
    main()
