#!/usr/bin/env python3
"""
run_status.py

How the last process_and_plot.sh run went, step by step:

    logs/process_and_plot_status.json

diagnostics/station_health.py reads it for the morning mail.

WHY

process_and_plot.sh runs every step whatever happened to the one before,
and a failed step only prints a note: recovering missed days (Step 1),
processing new raw data (2), the water-level fit (4), the 7-day plot and
the wave-setup check (5) and the tide comparison (6) can all fail on a
night that still ends with exit 0. daily_gnss.sh then uploads, writes
"finished successfully" in logs/daily_gnss.log, and the health check
reads that marker and says the cron ran fine (review finding G-B6,
reproduced through the real daily_gnss.sh: a night without subdaily and
with every plot crashing uploaded a 4-day-old 7_day_plot.png, and every
check said OK).

The exit status stays as it is, on purpose. daily_gnss.sh uploads only
after an exit 0, and the upload copies every product at once: a non-zero
exit for a failed tide comparison would also hold back the 7-day plot
that was redrawn fine, and the server would keep showing an older one.
So what the exit status does not say is written here instead, and the
health check reads it: a step that failed in the last run is a WARN, and
a FAIL once no run has gone through without a failed required step for
longer than a nightly run allows (station_health.RUN_OK_MAX_HOURS).

THE FILE (one JSON object, rewritten whole after every step)

    script          "process_and_plot.sh"
    run             "<pid>@<start>" of the run that wrote it
    pid, start      that run's process id and start (UTC, ...Z)
    end, rc         when it ended and its exit status; both null while it
                    runs -- and for good if it was killed (or the machine
                    went down) before it could finish
    ok              rc 0 and no required step failed (null while running)
    failed_steps    "Step 5 (7-day plot)", ... -- required or not
    steps           one entry per step, in the order they ran:
                      step      the step number of the script's sections
                      name      what it does ("7-day plot")
                      status    "ok", "failed" or "skipped"
                      rc        the exit status of its command (null if
                                skipped, or if the run stopped in it)
                      start, end  UTC
                      required  false for the steps that make diagnostics
                                only, not the record or the public plot:
                                recovering missed days, the wave-setup
                                check, the tide plot and comparison
                      message   why it failed or was skipped, in a line
    last_ok_start, last_ok_end
                    the newest run that was ok: this one, else carried
                    over from the file the run before left
    first_run_start the first run this file has been kept for, so "no
                    run ok since" has a starting point on a new station

USAGE (process_and_plot.sh)

    run_status.py FILE begin --run R --pid P --time T
    run_status.py FILE step  --run R --step 4 --name "water-level fit" \\
                             --start T0 --end T1 --rc 1 --message "..." [--optional]
    run_status.py FILE step  --run R --step 6 --name "tide plot" \\
                             --start T0 --end T1 --status skipped --message "..."
    run_status.py FILE end   --run R --time T --rc 0

Times come from process_and_plot.sh (date -u), so the file has one clock.
A step or end whose --run is not the file's run is not written: another
run has started since (a hand run during the cron run, say) and the file
is that one's. Writing the status never stops the run: any error is
printed on stderr and the exit status is still 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT = "process_and_plot.sh"
MESSAGE_MAX = 300          # characters of a step's message kept


def load(path: Path | str) -> dict | None:
    """The status as a dict; None if there is none, or it cannot be read."""
    try:
        st = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) else None


def save(path: Path, st: dict) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(st, indent=1) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def step_label(step: dict) -> str:
    """'Step 5 (7-day plot)'"""
    return f"Step {step.get('step')} ({step.get('name')})"


def begin(path: Path, run: str, pid: int, time: str) -> dict:
    """A new run: a fresh file that keeps only what the run before knew
    about the last ok run and when the file was first kept."""
    before = load(path) or {}
    st = {"script": SCRIPT, "run": run, "pid": pid, "start": time, "end": None,
          "rc": None, "ok": None, "failed_steps": [], "steps": [],
          "last_ok_start": before.get("last_ok_start"),
          "last_ok_end": before.get("last_ok_end"),
          "first_run_start": before.get("first_run_start") or before.get("start") or time}
    save(path, st)
    return st


def _own(path: Path, run: str) -> dict | None:
    st = load(path)
    if st is None or st.get("run") != run:
        print(f"run_status: {path} is not this run's ({run}) -- another run has started "
              f"since, or the file is gone; not written", file=sys.stderr)
        return None
    return st


def add_step(path: Path, run: str, step: str, name: str, start: str, end: str,
             rc: int | None = None, status: str | None = None, message: str = "",
             required: bool = True) -> dict | None:
    """Append one step. status defaults to ok for rc 0, else failed."""
    st = _own(path, run)
    if st is None:
        return None
    if status is None:
        status = "ok" if rc == 0 else "failed"
    entry = {"step": int(step) if str(step).isdigit() else step, "name": name,
             "status": status, "rc": rc, "start": start, "end": end,
             "required": required, "message": (message or "")[:MESSAGE_MAX]}
    st["steps"].append(entry)
    if status == "failed":
        st["failed_steps"].append(step_label(entry))
    save(path, st)
    return st


def end(path: Path, run: str, time: str, rc: int | None) -> dict | None:
    """The run is over: its exit status, whether it was ok, and if it was,
    it is the new last ok run."""
    st = _own(path, run)
    if st is None:
        return None
    st["end"], st["rc"] = time, rc
    st["ok"] = rc == 0 and not any(s.get("status") == "failed" and s.get("required", True)
                                   for s in st["steps"])
    if st["ok"]:
        st["last_ok_start"], st["last_ok_end"] = st["start"], time
    save(path, st)
    return st


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", type=Path, help="the status file (logs/process_and_plot_status.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("begin", help="start a new run")
    b.add_argument("--run", required=True)
    b.add_argument("--pid", type=int, required=True)
    b.add_argument("--time", required=True)
    s = sub.add_parser("step", help="record one step")
    s.add_argument("--run", required=True)
    s.add_argument("--step", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--rc", default="", help="the step's exit status (empty: none)")
    s.add_argument("--status", choices=("ok", "failed", "skipped"),
                   help="default: ok for --rc 0, else failed")
    s.add_argument("--message", default="")
    s.add_argument("--optional", action="store_true",
                   help="a diagnostic step: its failure does not make the run not ok")
    e = sub.add_parser("end", help="the run is over")
    e.add_argument("--run", required=True)
    e.add_argument("--time", required=True)
    e.add_argument("--rc", default="")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 0                       # never stop process_and_plot.sh
    try:
        if a.cmd == "begin":
            begin(a.file, a.run, a.pid, a.time)
        elif a.cmd == "step":
            rc = int(a.rc) if a.rc.strip() else None
            add_step(a.file, a.run, a.step, a.name, a.start, a.end, rc=rc, status=a.status,
                     message=a.message, required=not a.optional)
        else:
            end(a.file, a.run, a.time, int(a.rc) if a.rc.strip() else None)
    except Exception as exc:           # the status must never stop the run
        print(f"run_status: could not write {a.file}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
