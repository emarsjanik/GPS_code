#!/usr/bin/env python3
"""
position_stability_check.py

USGS GNSS Reference Station

Quick, standalone diagnostic -- NOT part of the delivered station
software. Polls the receiver's position solution repeatedly for a
fixed duration, printing the running horizontal and vertical
deviation once per minute. Useful for a quick sanity check on
antenna/receiver stability before committing to a long, unattended
recording.

Horizontal/vertical deviation are computed from the standard
deviation of all position samples collected so far (since script
start, not a rolling window), converted from lat/lon degrees to
local East/North meters using a flat-earth approximation. This is
accurate enough for a stationary antenna over the tiny distances
involved here (a properly fixed GNSS antenna's position noise should
be centimeters to a few meters, not kilometers) -- the same kind of
approximation used throughout surveying for short baselines.

Run from the station/ directory (or anywhere; it locates receiver.py
relative to its own location, not the current working directory):

    python3 position_stability_check.py
    python3 position_stability_check.py --duration 600 --poll 2 --report 60
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from receiver import Receiver, ReceiverError  # noqa: E402

METERS_PER_DEGREE_LAT = 111_320.0


def local_offsets_meters(
    origin_lat: float, origin_lon: float, lat: float, lon: float
) -> tuple[float, float]:
    """
    East/North offset in meters from (origin_lat, origin_lon) to
    (lat, lon), via a flat-earth approximation -- fine for the
    centimeter-to-meter scale distances a stationary antenna's
    position noise actually spans.
    """

    north = (lat - origin_lat) * METERS_PER_DEGREE_LAT
    east = (
        (lon - origin_lon)
        * METERS_PER_DEGREE_LAT
        * math.cos(math.radians(origin_lat))
    )
    return east, north


def sample_stdev(values: list[float]) -> float:
    """Sample standard deviation; 0.0 for fewer than 2 values (nothing to compare yet)."""

    if len(values) < 2:
        return 0.0

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)

    return math.sqrt(variance)


def print_report(elapsed_seconds: float, lats, lons, heights, origin) -> None:
    origin_lat, origin_lon = origin

    easts = []
    norths = []

    for lat, lon in zip(lats, lons):
        east, north = local_offsets_meters(origin_lat, origin_lon, lat, lon)
        easts.append(east)
        norths.append(north)

    east_dev = sample_stdev(easts)
    north_dev = sample_stdev(norths)
    horizontal_dev = math.sqrt(east_dev**2 + north_dev**2)
    vertical_dev = sample_stdev(heights)

    elapsed_min = elapsed_seconds / 60

    print(
        f"[{elapsed_min:5.1f} min] samples={len(lats):3d}   "
        f"horizontal dev: {horizontal_dev:7.4f} m   "
        f"vertical dev: {vertical_dev:7.4f} m"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=float, default=600, help="total run time, seconds (default: 600 = 10 min)"
    )
    parser.add_argument(
        "--poll", type=float, default=2.0, help="seconds between position queries (default: 2.0)"
    )
    parser.add_argument(
        "--report", type=float, default=60.0, help="seconds between printed reports (default: 60.0)"
    )
    args = parser.parse_args()

    print("=" * 64)
    print("Position Stability Check")
    print(
        f"Duration: {args.duration:.0f}s   "
        f"Poll interval: {args.poll:.1f}s   "
        f"Report interval: {args.report:.1f}s"
    )
    print("=" * 64)

    lats: list[float] = []
    lons: list[float] = []
    heights: list[float] = []
    origin: tuple[float, float] | None = None

    start = time.monotonic()
    next_poll = start
    next_report = start + args.report
    deadline = start + args.duration

    with Receiver() as rx:
        while True:
            now = time.monotonic()

            if now >= deadline:
                break

            if now >= next_poll:
                try:
                    position = rx.best_position()
                except ReceiverError as exc:
                    print(f"  [warning] position query failed: {exc}")
                else:
                    if origin is None:
                        origin = (position.latitude, position.longitude)
                    lats.append(position.latitude)
                    lons.append(position.longitude)
                    heights.append(position.height)
                next_poll = now + args.poll

            if now >= next_report and origin is not None:
                print_report(now - start, lats, lons, heights, origin)
                next_report = now + args.report

            time.sleep(0.1)

    print("=" * 64)

    if origin is not None and lats:
        print("Final:")
        print_report(time.monotonic() - start, lats, lons, heights, origin)
    else:
        print("No successful position samples were collected.")

    print("Done.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
