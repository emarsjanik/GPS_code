#!/usr/bin/env python3
"""
filter_by_time.py

Removes data rows from a gnssrefl results file (e.g. 199.txt) whose
UTCtime (column 5) is before a given cutoff hour, while preserving
all header/comment lines (starting with %) exactly as-is.

Usage: python3 filter_by_time.py input.txt output.txt cutoff_hour
"""
import sys


def main():
    input_path, output_path, cutoff_hour = sys.argv[1], sys.argv[2], float(sys.argv[3])

    kept = 0
    removed = 0

    with open(input_path) as infile, open(output_path, "w") as outfile:
        for line in infile:
            stripped = line.strip()
            if not stripped or stripped.startswith("%"):
                outfile.write(line)
                continue

            columns = stripped.split()
            utc_time = float(columns[4])

            if utc_time >= cutoff_hour:
                outfile.write(line)
                kept += 1
            else:
                removed += 1

    print(f"Kept {kept} row(s), removed {removed} row(s) before {cutoff_hour}:00 UTC")


if __name__ == "__main__":
    main()
