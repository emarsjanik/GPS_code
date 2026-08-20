#!/usr/bin/env python3
"""
======================================================================

USGS GNSS Reference Station

UM980 Command Utility

Sends a command to the receiver and prints the response.

Examples

    python3 tools/um980_cmd.py VERSIONA

    python3 tools/um980_cmd.py BESTPOSA

    python3 tools/um980_cmd.py UNILOGLIST

    python3 tools/um980_cmd.py "LOG COM1 RANGEA ONTIME 1"

======================================================================
"""

import argparse
import sys
from pathlib import Path

#
# Allow importing receiver.py from the parent directory
#

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from receiver import Receiver


def banner():

    print("=" * 70)
    print("USGS GNSS Reference Station")
    print("UM980 Command Utility")
    print("=" * 70)


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "command",
        nargs="+",
        help="Receiver command"
    )

    args = parser.parse_args()

    command = " ".join(args.command)

    banner()

    print()
    print("Sending Command")
    print("------------------------------")
    print(command)

    print()

    with Receiver() as rx:

        response = rx.query(command)

    print("Receiver Response")
    print("------------------------------")
    print(response)


if __name__ == "__main__":
    main()
