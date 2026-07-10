#!/usr/bin/env python3
"""
Supervised, short-chunk live test of StationManager -- NOT part of
the delivered station software. Uses a short chunk duration (default
60s) instead of the real 1-hour default, so a human can watch several
chunks cycle by in a few minutes and manually Ctrl+C to confirm
graceful shutdown actually works, without waiting for real UTC
midnight or a real 1-hour chunk.

This exercises the real Receiver, real record_raw() append behavior,
and real signal handling -- everything about StationManager that
unit tests (which use a fake Receiver) couldn't verify.

Usage:
    python3 test_station_manager_live.py
    python3 test_station_manager_live.py --chunk-seconds 30
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging

from station_manager import StationManager

parser = argparse.ArgumentParser()
parser.add_argument("--chunk-seconds", type=float, default=60.0)
args = parser.parse_args()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

manager = StationManager()

print("=" * 64)
print("Supervised live StationManager test")
print(f"Chunk duration: {args.chunk_seconds:.0f}s")
print("Watch for 2-3 chunks to complete, then press Ctrl+C to test")
print("graceful shutdown.")
print("=" * 64)

manager.initialize()

# Override AFTER initialize() so this doesn't require touching the
# real station.json -- initialize() already read the configured
# (real, 3600s default) value; we just replace it here for this
# supervised test only.
manager._chunk_seconds = args.chunk_seconds

try:
    manager.run()
except KeyboardInterrupt:
    print()
    print("Ctrl+C caught in this script's own try/except -- but the")
    print("SIGINT handler registered by initialize() should already")
    print("have called manager.stop() before this. If you see this")
    print("message, run() returned control back to us as expected.")
finally:
    manager.shutdown()

status = manager.status()
print("=" * 64)
print("Final status:")
print("  initialized:          ", status.initialized)
print("  running:              ", status.running)
print("  stop_requested:       ", status.stop_requested)
print("  current_day:          ", status.current_day)
print("  chunks_recorded_today:", status.chunks_recorded_today)
print("=" * 64)
