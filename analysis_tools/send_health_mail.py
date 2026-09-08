#!/usr/bin/env python3
"""
send_health_mail.py

Builds the daily health email as a MIME message and hands it to
msmtp. Called by station_health_mail.sh, which keeps responsibility
for the retry loop and for deciding whether to send at all.

Reads the report body on stdin so the shell script stays the single
source of truth for what the report says.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def deviation_stats() -> str:
    """Runs the tide comparison and returns its output, or an
    explanation of why it could not."""
    station_json = PROJECT / "station" / "resources" / "station.json"
    try:
        cfg = json.loads(station_json.read_text())
    except Exception as exc:
        return f"(could not read station.json: {exc})"

    tide_file = cfg.get("tide_model_file")
    tide_col = cfg.get("tide_model_value_column")
    if not tide_file or not tide_col:
        return "(no tide model configured)"
    if not Path(tide_file).exists():
        return f"(tide model file not found: {tide_file})"

    code = (cfg.get("gnssrefl_station_code")
            or (cfg.get("station_id") or "")[:4]).lower() or "usgs"
    spline = PROJECT / "products" / "refl_code" / "Files" / code / f"{code}_spline_out.txt"
    if not spline.exists():
        return f"(no spline output at {spline})"

    script = PROJECT / "analysis_tools" / "compare_to_tide_deviation.py"
    if not script.exists():
        return "(compare_to_tide_deviation.py not found)"

    try:
        r = subprocess.run(
            [sys.executable, str(script),
             "--spline-file", str(spline),
             "--tide-file", str(tide_file),
             "--tide-value-col", str(tide_col)],
            capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "(tide comparison timed out)"
    except Exception as exc:
        return f"(tide comparison failed: {exc})"

    if r.returncode != 0:
        return f"(tide comparison exited {r.returncode})\n{r.stderr.strip()[:400]}"

    # Drop the two "Loaded N points" preamble lines; keep the block.
    lines = [ln for ln in r.stdout.splitlines()
             if not ln.startswith("Loaded ")]
    return "\n".join(lines).strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--subject", required=True)
    p.add_argument("--to", required=True, nargs="+")
    p.add_argument("--attach", action="append", default=[],
                   help="path to a file to attach; missing files are noted, not fatal")
    p.add_argument("--no-stats", action="store_true",
                   help="skip the tide-model deviation section")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    body = sys.stdin.read()

    if not args.no_stats:
        body += "\n\n"
        body += "=" * 68 + "\n"
        body += "  AGREEMENT WITH THE TIDE MODEL\n"
        body += "=" * 68 + "\n"
        body += deviation_stats() + "\n"

    attached, missing = [], []
    for path in args.attach:
        if Path(path).is_file():
            attached.append(Path(path))
        else:
            missing.append(path)

    if missing:
        body += "\n(not attached, file missing: " + ", ".join(missing) + ")\n"

    msg = EmailMessage()
    msg["Subject"] = args.subject
    msg["To"] = ", ".join(args.to)
    msg.set_content(body)

    for path in attached:
        msg.add_attachment(path.read_bytes(),
                           maintype="image", subtype="png",
                           filename=path.name)

    if args.dry_run:
        print(f"--- would send to: {' '.join(args.to)} ---")
        print(f"Subject: {args.subject}")
        print(f"Attachments: {[p.name for p in attached] or 'none'}")
        print()
        print(body)
        return 0

    r = subprocess.run(["msmtp"] + args.to,
                       input=msg.as_bytes(), capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr.decode(errors="replace"))
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
