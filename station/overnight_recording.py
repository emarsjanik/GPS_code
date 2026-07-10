#!/usr/bin/env python3
"""
overnight_recording.py

USGS GNSS Reference Station

Standalone script -- NOT part of the delivered station software.
Computes the exact duration (in seconds) from right now until a
target local time, then runs record_raw() for that long, using the
confirmed-working binary logging defaults.

Duration is computed dynamically when the script actually starts,
not hardcoded -- so it's correct regardless of how much time passes
between deciding to run this and actually running it.

Meant to be run inside `screen` or `tmux`, since it's designed to
run for hours and must survive an SSH disconnect:

    screen -S gnss_overnight
    source ~/GNSS/v4.1/gnssrefl_venv/bin/activate
    cd ~/GNSS/v4.1/station
    python3 overnight_recording.py
    # Ctrl+A, D to detach; screen -r gnss_overnight to reattach later

By default, records until 08:00 *local system time* tomorrow. Override
with --until:

    python3 overnight_recording.py --until "2026-07-09 08:00:00"
    python3 overnight_recording.py --hours 8
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from receiver import Receiver  # noqa: E402


def compute_target_time(until: str | None, hours: float | None) -> datetime:
    """
    Returns the target stop time as a naive local datetime.

    Priority: --until (exact date/time) > --hours (relative) >
    default (08:00 tomorrow, local system clock).
    """

    now = datetime.now()

    if until:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(until, fmt)
            except ValueError:
                continue
        raise SystemExit(
            f"Could not parse --until {until!r}; expected "
            f"'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD HH:MM'"
        )

    if hours is not None:
        return now + timedelta(hours=hours)

    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time()).replace(hour=8)


def make_progress_callback(report_every_seconds: float):
    """
    record_raw()'s own progress_callback fires roughly once a
    second -- printing every call for an 18-hour run would flood
    the terminal/log with tens of thousands of lines. This wraps it
    to only actually print once every `report_every_seconds`.
    """

    state = {"last_reported": 0.0}

    def callback(snapshot) -> None:
        if snapshot.duration_actual - state["last_reported"] < report_every_seconds:
            return

        state["last_reported"] = snapshot.duration_actual

        elapsed_hours = snapshot.duration_actual / 3600
        remaining_hours = (
            snapshot.duration_requested - snapshot.duration_actual
        ) / 3600

        print(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
            f"elapsed {elapsed_hours:5.2f}h / remaining {remaining_hours:5.2f}h   "
            f"messages={snapshot.messages_written:6d}   "
            f"bytes={snapshot.bytes_written:10d}"
        )

    return callback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--until",
        default=None,
        help="exact stop time, 'YYYY-MM-DD HH:MM:SS' (local system time)",
    )
    parser.add_argument(
        "--hours", type=float, default=None, help="record for this many hours from now"
    )
    parser.add_argument(
        "--report-every",
        type=float,
        default=600,
        help="seconds between progress lines (default: 600 = 10 min)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="output filename (default: ../raw/overnight_<timestamp>.um980)",
    )
    args = parser.parse_args()

    start = datetime.now()
    target = compute_target_time(args.until, args.hours)
    duration_seconds = (target - start).total_seconds()

    if duration_seconds <= 0:
        print(
            f"Target time {target:%Y-%m-%d %H:%M:%S} is not in the future "
            f"(current time: {start:%Y-%m-%d %H:%M:%S}). Nothing to do."
        )
        return 1

    output_path = (
        Path(args.output)
        if args.output
        else Path(__file__).resolve().parent.parent
        / "raw"
        / f"overnight_{start:%Y%m%d_%H%M%S}.um980"
    )

    hours = duration_seconds / 3600

    print("=" * 64)
    print("Overnight Recording")
    print(f"Start:    {start:%Y-%m-%d %H:%M:%S}")
    print(f"Target:   {target:%Y-%m-%d %H:%M:%S}")
    print(f"Duration: {duration_seconds:.0f} sec  ({hours:.2f} hours)")
    print(f"Output:   {output_path}")
    print("=" * 64)

    try:
        with Receiver() as rx:
            result = rx.record_raw(
                output_path,
                duration=duration_seconds,
                enable_logging=True,
                progress_callback=make_progress_callback(args.report_every),
            )
    except Exception as exc:
        print("=" * 64)
        print("Recording FAILED")
        print(f"{type(exc).__name__}: {exc}")
        print("=" * 64)
        return 1

    print("=" * 64)
    print("Recording finished")
    print(f"success:            {result.successful}")
    print(f"start_time:         {result.start_time}")
    print(f"end_time:           {result.end_time}")
    print(f"duration_requested: {result.duration_requested:.0f} sec")
    print(f"duration_actual:    {result.duration_actual:.0f} sec")
    print(f"bytes_written:      {result.bytes_written}")
    print(f"messages_written:   {result.messages_written}")
    print(f"average_rate_bytes: {result.average_rate_bytes:.1f} bytes/sec")
    print(f"receiver_model:     {result.receiver_model}")
    print(f"receiver_firmware:  {result.receiver_firmware}")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for warning in result.warnings:
            print(f"  - {warning}")
    print("=" * 64)

    return 0 if result.successful else 1


if __name__ == "__main__":
    sys.exit(main())
