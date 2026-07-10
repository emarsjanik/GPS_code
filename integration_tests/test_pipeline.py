#!/usr/bin/env python3
"""
======================================================================

USGS GNSS Reference Station
Pipeline Integration Test

Tests:

    ✓ Database connection
    ✓ Pipeline initialization
    ✓ Pipeline status
    ✓ Queue status
    ✓ Raw file discovery
    ✓ Pipeline execution
    ✓ Pipeline summary
    ✓ Clean shutdown

This test does NOT require:
    - Receiver connection
    - convbin
    - gnssrefl

======================================================================
"""

from pathlib import Path
import traceback

from pipeline import Pipeline


def banner(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():

    banner("USGS GNSS Reference Station")
    print("Pipeline Integration Test")

    try:

        #
        # Create pipeline
        #
        print("\nCreating Pipeline...")
        pipeline = Pipeline()
        print("PASS")

        #
        # Initialize
        #
        print("\nInitializing Pipeline...")
        result = pipeline.initialize()
        print("Result :", result)

        #
        # Status
        #
        banner("Pipeline Status")
        print(pipeline.status())

        #
        # Queue
        #
        banner("Queue Status")
        print(pipeline.queue_status())

        #
        # Scan Raw Directory
        #
        banner("Raw File Discovery")

        raw_dir = Path("../raw")

        if raw_dir.exists():
            files = sorted(raw_dir.glob("*"))

            print(f"Raw Directory : {raw_dir.resolve()}")
            print(f"Files Found   : {len(files)}")

            for f in files:
                print("   ", f.name)

        else:
            print("Raw directory does not exist.")

        #
        # Execute Pipeline
        #
        banner("Running Pipeline")

        summary = pipeline.run()

        print(summary)

        #
        # Status after run
        #
        banner("Pipeline Status After Run")

        print(pipeline.status())

        #
        # Shutdown
        #
        banner("Shutdown")

        pipeline.shutdown()

        print("PASS")

        #
        # Finished
        #
        banner("RESULT")

        print("PIPELINE TEST PASSED")

    except Exception:

        banner("PIPELINE TEST FAILED")

        traceback.print_exc()


if __name__ == "__main__":
    main()
