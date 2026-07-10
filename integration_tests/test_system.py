#!/usr/bin/env python3
"""
USGS GNSS Reference Station
System Integration Test
"""

import traceback

from database import Database
from receiver import Receiver
from pipeline import Pipeline


def banner(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def success(msg):
    print(f"[PASS] {msg}")


def failure(msg):
    print(f"[FAIL] {msg}")


def main():

    banner("USGS GNSS Reference Station")
    print("Full System Integration Test")

    # --------------------------------------------------
    # Database
    # --------------------------------------------------

    banner("Database")

    db = Database()

    try:
        db.connect()
        success("Database Connected")
    except Exception:
        traceback.print_exc()
        return

    # --------------------------------------------------
    # Receiver
    # --------------------------------------------------

    banner("Receiver")

    try:
        with Receiver() as rx:

            version = rx.version()
            position = rx.best_position()

            success("Receiver Connected")

            print(version)
            print(position)

    except Exception:
        traceback.print_exc()
        return

    # --------------------------------------------------
    # Save Data
    # --------------------------------------------------

    banner("Database Write")

    try:

        db.save_receiver_version(version)
        db.save_position(position)
        db.save_receiver_status(position)

        success("Receiver information saved")

    except Exception:
        traceback.print_exc()
        return

    # --------------------------------------------------
    # Pipeline
    # --------------------------------------------------

    banner("Pipeline")

    try:

        pipeline = Pipeline(db=db)

        pipeline.initialize()

        summary = pipeline.run()

        print(summary)

        pipeline.shutdown()

        success("Pipeline Completed")

    except Exception:
        traceback.print_exc()
        return

    db.close()

    banner("RESULT")

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
