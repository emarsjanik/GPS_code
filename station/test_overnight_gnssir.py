#!/usr/bin/env python3
"""
Quick, one-off test script -- run the real GnssIrProcessor against
the overnight recording, for a single specified day.

Usage:
    python3 test_overnight_gnssir.py 2026-07-09
    python3 test_overnight_gnssir.py 2026-07-08
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gnssrefl_processor import GnssIrProcessor

if len(sys.argv) != 2:
    print("Usage: python3 test_overnight_gnssir.py YYYY-MM-DD")
    sys.exit(1)

target_day = date.fromisoformat(sys.argv[1])

processor = GnssIrProcessor()
processor.initialize()

observation_file = Path(__file__).resolve().parent.parent / "rinex" / "overnight_20260708_135223.obs"

print(f"Processing {observation_file} for day {target_day}")
print("=" * 64)

result = processor.process(observation_file, day=target_day)

print("=" * 64)
print("success:          ", result.success)
print("num_tracks:       ", result.num_tracks)
print("message:          ", result.message)
print("runtime_seconds:  ", result.runtime_seconds)
print("output_directory: ", result.output_directory)
