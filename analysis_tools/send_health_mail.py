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

    def run(spline_path):
        try:
            r = subprocess.run(
                [sys.executable, str(script),
                 "--spline-file", str(spline_path),
                 "--tide-file", str(tide_file),
                 "--tide-value-col", str(tide_col)],
                capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return None, "(tide comparison timed out)"
        except Exception as exc:
            return None, f"(tide comparison failed: {exc})"
        if r.returncode != 0:
            return None, f"(tide comparison exited {r.returncode})"
        return r.stdout, None

    def summarize(out):
        """Pulls the three numbers worth watching from the block."""
        pts = mad = corr = "?"
        for ln in out.splitlines():
            if ln.startswith("Points compared"):
                pts = ln.split(":", 1)[1].strip()
            elif "MEAN ABSOLUTE DEVIATION" in ln and "MEDIAN" not in ln:
                mad = ln.split(":", 1)[1].strip()
            elif ln.startswith("Correlation"):
                corr = ln.split(":", 1)[1].strip()
        return pts, mad, corr

    full_out, err = run(spline)
    if err:
        return err

    # A recent-window copy of the spline, so the same comparison can
    # be run over just the last few days. Written to a temporary file
    # rather than adding a date filter to the comparison script,
    # which is used elsewhere and better left alone.
    # Each window answers a different question; see this patch's own
    # notes. Ordered shortest first so the fastest-responding figure
    # is read before the slow baseline.
    WINDOWS = [(2, "Last 2 days"), (7, "Last 7 days")]

    recent_note = ""
    try:
        import tempfile
        from datetime import datetime, timedelta

        rows, header = [], []
        for ln in spline.read_text(errors="replace").splitlines():
            if ln.startswith("%"):
                header.append(ln)
                continue
            c = ln.split()
            if len(c) < 9:
                continue
            try:
                dt = datetime(int(float(c[2])), int(float(c[3])), int(float(c[4])),
                              int(float(c[5])), int(float(c[6])), int(float(c[7])))
            except (ValueError, IndexError):
                continue
            rows.append((dt, ln))

        summaries = []
        if rows:
            newest = max(dt for dt, _ in rows)
            for days, label in WINDOWS:
                cutoff = newest - timedelta(days=days)
                recent = [ln for dt, ln in rows if dt >= cutoff]
                # Too few points and the figure is meaningless rather
                # than merely noisy; say so instead of printing it.
                if len(recent) <= 20:
                    summaries.append((label, None, None, len(recent)))
                    continue
                with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                                 delete=False) as tf:
                    tf.write("\n".join(header + recent) + "\n")
                    tmp_path = tf.name
                out, err = run(Path(tmp_path))
                Path(tmp_path).unlink(missing_ok=True)
                if out:
                    p, m, c = summarize(out)
                    summaries.append((label, m, c, p))
                else:
                    summaries.append((label, None, None, len(recent)))

        p1, m1, c1 = summarize(full_out)
        summaries.append(("Full record", m1, c1, p1))

        width = max(len(lbl) for lbl, *_ in summaries)
        for label, mad, corr, pts in summaries:
            if mad is None:
                recent_note += (f"  {label:<{width}} : not enough data yet "
                                f"({pts} points)\n")
            else:
                recent_note += (f"  {label:<{width}} : {mad}"
                                f"   correlation {corr}"
                                f"   ({pts} points)\n")
        recent_note += (
            "\n"
            "  The short windows respond quickly but are noisy; the full\n"
            "  record is steady but slow. Watch for all three drifting the\n"
            "  same way -- one moving alone is usually just sampling.\n"
            "\n")
    except Exception as exc:
        recent_note = f"  (could not compute the recent windows: {exc})\n\n"

    lines = [ln for ln in full_out.splitlines()
             if not ln.startswith("Loaded ")
             and "points have both a GNSS-IR value" not in ln]
    return recent_note + "\n".join(lines).strip()


# Images larger than this are downscaled before attaching. The GNSS
# plots are around 200 KB and are left alone; the waterline
# elevation maps are several megabytes and are not.
DOWNSCALE_ABOVE_BYTES = 1_000_000
DOWNSCALE_LONG_EDGE = 1400
DOWNSCALE_QUALITY = 85


def _downscale_for_email(path: Path, tmpdir: Path):
    """Returns (bytes, filename) for attaching.

    Falls back to the original on any failure -- a large attachment
    is better than a missing one, and this runs unattended.
    """
    raw = path.read_bytes()
    if len(raw) <= DOWNSCALE_ABOVE_BYTES:
        return raw, path.name, False

    try:
        from PIL import Image
    except ImportError:
        return raw, path.name, False

    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = DOWNSCALE_LONG_EDGE / max(w, h)
            if scale >= 1.0:
                return raw, path.name, False
            im = im.resize((int(w * scale), int(h * scale)),
                           Image.LANCZOS)
            out = tmpdir / (path.stem + ".jpg")
            im.save(out, "JPEG", quality=DOWNSCALE_QUALITY, optimize=True)
        return out.read_bytes(), out.name, True
    except Exception:
        return raw, path.name, False


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

    import tempfile
    with tempfile.TemporaryDirectory(prefix="health_mail_") as td:
        tmpdir = Path(td)
        for path in attached:
            data, name, shrunk = _downscale_for_email(path, tmpdir)
            subtype = "jpeg" if name.lower().endswith(".jpg") else "png"
            msg.add_attachment(data, maintype="image", subtype=subtype,
                               filename=name)
            if shrunk:
                print(f"  {path.name}: {path.stat().st_size / 1e6:.1f} MB "
                      f"-> {len(data) / 1e6:.2f} MB for email", file=sys.stderr)

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
