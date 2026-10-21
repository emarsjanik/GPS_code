"""
tests/test_run_status.py

A failed step of process_and_plot.sh used to leave no trace the health
check could see: the run went on, exited 0, daily_gnss.sh uploaded and
logged "finished successfully", and every check said OK (review finding
G-B6). Each step now says how it went in logs/process_and_plot_status.json
(analysis_tools/run_status.py), and station_health.check_run_status
reports it.

Three parts:
  * RunStatusFileTests -- the file itself: what a run carries over from
    the one before, which failures make a run not ok, a second run.
  * ProcessAndPlotTests -- the real process_and_plot.sh (and daily_gnss.sh)
    through subprocess, with every command it runs stubbed: a stand-in
    venv, convbin, gnss_record.py, the plot scripts, the pipeline and
    recover_missing_days.sh, each exiting as the test asks. The exit
    status of every case is the one the script had before the status
    file (checked side by side against 1f3a8c7), so the upload gate is
    unchanged.
  * HealthRunStatusTests -- the health check on hand-made status files:
    WARN after one bad night, FAIL once none has gone through for a day
    and a half, a killed run, a run still going.

Run with:
    python3 -m unittest discover -s tests -p "test_run_status.py" -v
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "diagnostics"))
import station_health as sh  # noqa: E402
import run_status as rs  # noqa: E402

STA = "usgs"
STATUS = Path("logs") / "process_and_plot_status.json"

# A stand-in for each python step: exits with $STUB_<NAME>_RC (default 0).
STUB = """import os, sys, time
name = {name!r}
if name == "gnss_record":
    name += "_update" if "update" in sys.argv else "_summary"
marker = os.environ.get("STUB_SLEEP_" + name.upper())
if marker:
    open(marker, "w").close()
    time.sleep(60)
rc = int(os.environ.get("STUB_" + name.upper() + "_RC", "0"))
print(name, "stub ->", rc)
sys.exit(rc)
"""

PIPELINE = """import os
class PipelineBusyError(RuntimeError):
    pass
class _Summary:
    files_found = files_processed = products_created = 1
    files_failed = int(os.environ.get("STUB_PIPELINE_FILES_FAILED", "0"))
    errors = ["station_20261020.um980: simulated failure"] if files_failed else []
class Pipeline:
    def initialize(self): pass
    def run(self):
        if os.environ.get("STUB_PIPELINE_FAIL"):
            raise RuntimeError("simulated pipeline crash")
        if os.environ.get("STUB_PIPELINE_BUSY"):
            raise PipelineBusyError("another pipeline run has held the lock")
        return _Summary()
    def shutdown(self): pass
"""


def iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_project(root: Path) -> Path:
    """A project with the real process_and_plot.sh, daily_gnss.sh and
    run_status.py, and stubs for everything they call."""
    proj = root / "proj"
    for d in ("analysis_tools", "maintenance", "station/resources", "raw",
              "gnssrefl_venv/bin", f"products/refl_code/2026/results/{STA}",
              f"products/refl_code/Files/{STA}"):
        (proj / d).mkdir(parents=True)
    for f in ("process_and_plot.sh", "daily_gnss.sh"):
        shutil.copy(ROOT / f, proj / f)
        (proj / f).chmod(0o755)
    shutil.copy(ROOT / "analysis_tools" / "run_status.py", proj / "analysis_tools")
    for name in ("gnss_record", "plot_7day", "water_level_qc", "plot_gnssir_vs_tide",
                 "compare_to_tide_deviation"):
        (proj / "analysis_tools" / f"{name}.py").write_text(STUB.format(name=name))
    (proj / "maintenance" / "recover_missing_days.sh").write_text("exit ${STUB_RECOVER_RC:-0}\n")
    (proj / "station" / "pipeline.py").write_text(PIPELINE)
    (proj / "station" / "resources" / "station.json").write_text(json.dumps(
        {"station_id": "USGS00USA", "tide_model_file": str(proj / "tides.xlsx")}))
    (proj / "tides.xlsx").write_text("stub")
    (proj / "raw" / "station_20261020.um980").write_text("x")
    (proj / "products" / "refl_code" / "2026" / "results" / STA / "293.txt").write_text(
        "2026 293 19.0\n")
    (proj / "products" / "refl_code" / "Files" / STA / f"{STA}_spline_out.txt").write_text(
        "% stub spline\n")
    bindir = root / "bin"
    bindir.mkdir()
    (bindir / "convbin").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    upload_log = root / "uploads.log"
    (bindir / "upload.sh").write_text(f'#!/bin/sh\necho uploaded >> "{upload_log}"\n')
    for f in ("convbin", "python3", "upload.sh"):
        (bindir / f).chmod(0o755)
    (proj / "gnssrefl_venv" / "bin" / "activate").write_text(f'export PATH="{bindir}:$PATH"\n')
    return proj


class RunStatusFileTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "logs" / "status.json"

    def night(self, run: str, t: str, steps, rc: int = 0) -> dict:
        rs.begin(self.path, run, 100, t)
        for step, name, step_rc, optional in steps:
            rs.add_step(self.path, run, step, name, t, t, rc=step_rc, message=f"{name} said why",
                        required=not optional)
        return rs.end(self.path, run, t, rc)

    def test_ok_run_becomes_the_last_ok_one(self) -> None:
        st = self.night("1@a", "2026-10-19T02:30:00Z", [("4", "water-level fit", 0, False)])
        self.assertTrue(st["ok"])
        self.assertEqual((st["last_ok_start"], st["last_ok_end"]),
                         ("2026-10-19T02:30:00Z", "2026-10-19T02:30:00Z"))
        self.assertEqual(st["steps"][0]["step"], 4)

    def test_a_failed_required_step_keeps_the_last_ok_run(self) -> None:
        self.night("1@a", "2026-10-19T02:30:00Z", [("4", "water-level fit", 0, False)])
        st = self.night("2@b", "2026-10-20T02:30:00Z",
                        [("4", "water-level fit", 1, False), ("5", "7-day plot", 0, False)])
        self.assertFalse(st["ok"])
        self.assertEqual(st["rc"], 0)
        self.assertEqual(st["failed_steps"], ["Step 4 (water-level fit)"])
        self.assertEqual(st["last_ok_end"], "2026-10-19T02:30:00Z")
        self.assertEqual(st["first_run_start"], "2026-10-19T02:30:00Z")
        self.assertEqual(st["steps"][0]["message"], "water-level fit said why")

    def test_a_failed_optional_step_still_leaves_the_run_ok(self) -> None:
        st = self.night("1@a", "2026-10-20T02:30:00Z", [("6", "tide comparison", 1, True)])
        self.assertTrue(st["ok"])
        self.assertEqual(st["failed_steps"], ["Step 6 (tide comparison)"])
        self.assertEqual(st["last_ok_end"], "2026-10-20T02:30:00Z")

    def test_a_non_zero_exit_is_not_ok(self) -> None:
        st = self.night("1@a", "2026-10-20T02:30:00Z", [("0", "sanity checks", 1, False)], rc=1)
        self.assertFalse(st["ok"])
        self.assertIsNone(st["last_ok_end"])

    def test_a_second_run_owns_the_file(self) -> None:
        # a hand run started while the cron run was going: the cron run's
        # later steps do not end up in the hand run's file
        rs.begin(self.path, "1@a", 1, "2026-10-20T02:30:00Z")
        rs.begin(self.path, "2@b", 2, "2026-10-20T02:31:00Z")
        with unittest.mock.patch("sys.stderr"):
            self.assertIsNone(rs.add_step(self.path, "1@a", "5", "7-day plot", "x", "y", rc=1))
            self.assertIsNone(rs.end(self.path, "1@a", "z", 0))
        st = rs.load(self.path)
        self.assertEqual((st["run"], st["steps"], st["end"]), ("2@b", [], None))

    def test_the_command_line_never_fails(self) -> None:
        cli = [sys.executable, str(ROOT / "analysis_tools" / "run_status.py")]
        blocked = Path(self._tmp.name) / "a_file"
        blocked.write_text("not a directory")
        for args in (["--nonsense"], [str(blocked / "status.json"), "begin", "--run", "r",
                                      "--pid", "1", "--time", "t"],
                     [str(self.path), "step", "--run", "r"]):
            r = subprocess.run(cli + args, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, args)
        self.assertFalse(self.path.exists())

    def test_the_command_line_records_a_skip_and_a_long_message(self) -> None:
        cli = [sys.executable, str(ROOT / "analysis_tools" / "run_status.py"), str(self.path)]
        subprocess.run(cli + ["begin", "--run", "r", "--pid", "1", "--time", "t0"], check=True)
        subprocess.run(cli + ["step", "--run", "r", "--step", "6", "--name", "tide plot",
                              "--start", "t0", "--end", "t1", "--rc", "", "--status", "skipped",
                              "--message", "x" * 1000, "--optional"], check=True)
        subprocess.run(cli + ["end", "--run", "r", "--time", "t2", "--rc", "0"], check=True)
        st = rs.load(self.path)
        (step,) = st["steps"]
        self.assertEqual((step["status"], step["rc"], step["required"]), ("skipped", None, False))
        self.assertEqual(len(step["message"]), rs.MESSAGE_MAX)
        self.assertTrue(st["ok"])


class ProcessAndPlotTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.proj = make_project(self.root)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("STUB_") and k != "REFL_CODE"}
        self.env["PATH"] = f"{self.root / 'bin'}:{self.env.get('PATH', '/usr/bin:/bin')}"

    def run_script(self, cwd: Path | None = None, **stubs) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.proj / "process_and_plot.sh")],
                              cwd=cwd or self.proj, env=dict(self.env, **stubs),
                              capture_output=True, text=True, timeout=300)

    def status(self) -> dict:
        return json.loads((self.proj / STATUS).read_text())

    def steps(self) -> list:
        return [(s["step"], s["name"], s["status"]) for s in self.status()["steps"]]

    def test_a_good_night_records_every_step(self) -> None:
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.steps(), [
            (0, "sanity checks", "ok"), (1, "recover missed days", "ok"),
            (2, "process new raw data", "ok"), (3, "list results", "ok"),
            (4, "water-level fit", "ok"), (5, "7-day plot", "ok"),
            (5, "wave-setup check", "ok"), (6, "tide plot", "ok"),
            (6, "tide comparison", "ok")])
        st = self.status()
        self.assertEqual((st["rc"], st["ok"], st["failed_steps"]), (0, True, []))
        self.assertEqual(st["last_ok_end"], st["end"])
        self.assertTrue(st["run"].startswith(f"{st['pid']}@"))
        for s in st["steps"]:
            self.assertTrue(s["start"].endswith("Z") and s["start"] <= s["end"], s)
        self.assertEqual([s["required"] for s in st["steps"]],
                         [True, False, True, True, True, True, False, False, False])
        self.assertNotIn("Failed in this run", r.stdout)

    def test_failed_steps_are_recorded_and_the_exit_status_stays_zero(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        first_ok = self.status()["last_ok_end"]
        r = self.run_script(STUB_GNSS_RECORD_UPDATE_RC="1", STUB_PLOT_7DAY_RC="1",
                            STUB_COMPARE_TO_TIDE_DEVIATION_RC="1")
        self.assertEqual(r.returncode, 0)
        st = self.status()
        self.assertEqual(st["failed_steps"], ["Step 4 (water-level fit)", "Step 5 (7-day plot)",
                                              "Step 6 (tide comparison)"])
        self.assertFalse(st["ok"])
        self.assertEqual(st["last_ok_end"], first_ok)
        fit = next(s for s in st["steps"] if s["step"] == 4)
        self.assertEqual(fit["rc"], 1)
        self.assertIn("keeps the previous night's", fit["message"])
        plot = next(s for s in st["steps"] if s["name"] == "7-day plot")
        self.assertIn("7_day_plot.png was not redrawn", plot["message"])
        # the steps after a failed one still ran
        self.assertIn((6, "tide plot", "ok"), self.steps())
        self.assertIn("Failed in this run: Step 4 (water-level fit), Step 5 (7-day plot), "
                      "Step 6 (tide comparison)", r.stdout)

    def test_the_exit_status_is_the_one_it_was(self) -> None:
        # daily_gnss.sh uploads only after an exit 0: these are the exit
        # statuses of process_and_plot.sh before the status file (1f3a8c7)
        cases = [({"STUB_RECOVER_RC": "3"}, 0, "Step 1 (recover missed days)"),
                 ({"STUB_PIPELINE_FAIL": "1"}, 0, "Step 2 (process new raw data)"),
                 ({"STUB_GNSS_RECORD_SUMMARY_RC": "2"}, 1, "Step 3 (list results)"),
                 ({"STUB_GNSS_RECORD_UPDATE_RC": "1"}, 0, "Step 4 (water-level fit)"),
                 ({"STUB_WATER_LEVEL_QC_RC": "1"}, 0, "Step 5 (wave-setup check)"),
                 ({"STUB_PLOT_GNSSIR_VS_TIDE_RC": "2"}, 0, "Step 6 (tide plot)")]
        for stubs, rc, failed in cases:
            with self.subTest(failed=failed):
                r = self.run_script(**stubs)
                self.assertEqual(r.returncode, rc, r.stdout[-2000:])
                st = self.status()
                self.assertEqual((st["rc"], st["failed_steps"]), (rc, [failed]))
        # no results yet: exit 0, nothing after Step 3
        r = self.run_script(STUB_GNSS_RECORD_SUMMARY_RC="3")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.steps()[-1], (3, "list results", "ok"))
        self.assertTrue(self.status()["ok"])

    def test_a_setup_failure_is_recorded(self) -> None:
        shutil.rmtree(self.proj / "gnssrefl_venv")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        st = self.status()
        self.assertEqual(self.steps(), [(0, "sanity checks", "failed")])
        self.assertIn("virtual environment not found", st["steps"][0]["message"])
        self.assertEqual((st["rc"], st["ok"]), (1, False))

    def test_the_pipeline_imports_from_the_project_outside_it(self) -> None:
        # cron starts the script from $HOME. Step 2 imported the pipeline
        # from the relative path 'station' and died there (finding H1); it
        # now imports from the project's own station/ folder, wherever it
        # was started, and never from a 'station' folder of the cwd
        decoy = self.root / "station"
        decoy.mkdir()
        (decoy / "pipeline.py").write_text("raise SystemExit('the cwd copy was imported')\n")
        r = self.run_script(cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        step2 = self.status()["steps"][2]
        self.assertEqual((step2["name"], step2["status"], step2["rc"]),
                         ("process new raw data", "ok", 0))
        self.assertIn("[OK]   Running pipeline", r.stdout)

    def test_a_failed_file_fails_the_step_not_the_run(self) -> None:
        # the -c snippet exits 1 when any file failed: the step is failed
        # in the status, the script's exit status (and so the upload) not
        r = self.run_script(cwd=self.root, STUB_PIPELINE_FILES_FAILED="2")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        st = self.status()
        step2 = st["steps"][2]
        self.assertEqual((step2["status"], step2["rc"]), ("failed", 1))
        self.assertIn("2 file(s) failed", step2["message"])
        self.assertEqual(st["failed_steps"], ["Step 2 (process new raw data)"])
        # the steps after it still ran
        self.assertIn((4, "water-level fit", "ok"), self.steps())

    def test_a_pipeline_kept_waiting_by_another_run_fails_the_step(self) -> None:
        r = self.run_script(STUB_PIPELINE_BUSY="1")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        step2 = self.status()["steps"][2]
        self.assertEqual((step2["status"], step2["rc"]), ("failed", 1))
        self.assertIn("another pipeline run", step2["message"])

    def test_no_raw_data_is_a_skip(self) -> None:
        (self.proj / "raw" / "station_20261020.um980").unlink()
        self.run_script()
        self.assertIn((2, "process new raw data", "skipped"), self.steps())
        self.assertTrue(self.status()["ok"])

    def start_until_plot(self) -> subprocess.Popen:
        marker = self.root / "plotting"
        p = subprocess.Popen(["bash", str(self.proj / "process_and_plot.sh")], cwd=self.proj,
                             env=dict(self.env, STUB_SLEEP_PLOT_7DAY=str(marker)),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and os.killpg(p.pid, signal.SIGKILL))
        deadline = time.time() + 120
        while not marker.exists():
            self.assertLess(time.time(), deadline, "the 7-day plot never started")
            time.sleep(0.1)
        return p

    def test_a_terminated_run_records_the_step_it_stopped_in(self) -> None:
        p = self.start_until_plot()
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=60)
        st = self.status()
        self.assertIsNotNone(st["end"])
        self.assertNotEqual(st["rc"], 0)     # bash's $? after a SIGTERM: 0 is not kept
        self.assertFalse(st["ok"])
        last = st["steps"][-1]
        self.assertEqual((last["name"], last["status"]), ("7-day plot", "failed"))
        self.assertIn("ended during this step", last["message"])
        self.assertEqual(st["failed_steps"], ["Step 5 (7-day plot)"])

    def test_a_killed_run_is_a_fail(self) -> None:
        p = self.start_until_plot()
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=60)
        st = self.status()
        self.assertIsNone(st["end"])
        self.assertEqual(self.steps()[-1], (4, "water-level fit", "ok"))
        with unittest.mock.patch.object(sh, "PROJECT_DIR", self.proj):
            sh.findings.clear()
            sh.check_run_status()
            (status, check, message), = sh.findings
        self.assertEqual((status, check), ("FAIL", "run"))
        self.assertIn("stopped before it finished", message)
        self.assertIn("Step 4 (water-level fit)", message)

    @unittest.skipIf(shutil.which("flock") is None, "flock not installed")
    def test_daily_gnss_still_uploads_after_a_failed_step(self) -> None:
        # what is uploaded, and when, is as before: a failed 7-day plot does
        # not hold back the upload; a run that cannot start still does
        uploads = self.root / "uploads.log"
        env = dict(self.env, UPLOAD_SCRIPT=str(self.root / "bin" / "upload.sh"),
                   STUB_PLOT_7DAY_RC="1")
        r = subprocess.run(["bash", str(self.proj / "daily_gnss.sh")], cwd=self.proj, env=env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(uploads.read_text().split(), ["uploaded"])
        log = (self.proj / "logs" / "daily_gnss.log").read_text()
        self.assertIn("finished successfully", log)
        self.assertIn("Failed in this run: Step 5 (7-day plot)", log)
        self.assertNotIn("finished with errors", log)
        with unittest.mock.patch.object(sh, "PROJECT_DIR", self.proj):
            sh.findings.clear()
            sh.check_run_status()
            run = [f for f in sh.findings if f[1] == "run"]
        self.assertEqual([f[0] for f in run], ["WARN"])
        self.assertIn("Step 5 (7-day plot) failed", run[0][2])

        shutil.rmtree(self.proj / "gnssrefl_venv")
        r = subprocess.run(["bash", str(self.proj / "daily_gnss.sh")], cwd=self.proj, env=env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(uploads.read_text().split(), ["uploaded"], "no second upload")
        self.assertEqual(self.steps(), [(0, "sanity checks", "failed")])


class HealthRunStatusTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name)
        patch = unittest.mock.patch.object(sh, "PROJECT_DIR", self.proj)
        patch.start()
        self.addCleanup(patch.stop)
        sh.findings.clear()
        self.path = self.proj / STATUS
        self.now = sh.utcnow()

    def findings(self):
        sh.check_run_status()
        return [(f[0], f[2]) for f in sh.findings if f[1] == "run"]

    def write(self, hours_ago: float, steps=(), last_ok_hours=None, first_hours=None,
              rc=0, finished=True, pid=12345) -> None:
        start = self.now - timedelta(hours=hours_ago)
        run = f"{pid}@{iso(start)}"
        st = {"script": "process_and_plot.sh", "run": run, "pid": pid, "start": iso(start),
              "end": None, "rc": None, "ok": None, "failed_steps": [], "steps": [],
              "last_ok_start": None,
              "last_ok_end": (iso(self.now - timedelta(hours=last_ok_hours))
                              if last_ok_hours is not None else None),
              "first_run_start": iso(self.now - timedelta(hours=first_hours or hours_ago))}
        rs.save(self.path, st)
        for step, name, status, required in steps:
            rs.add_step(self.path, run, step, name, iso(start), iso(start),
                        rc=0 if status == "ok" else (1 if status == "failed" else None),
                        status=status, message=f"why {name} {status}", required=required)
        if finished:
            rs.end(self.path, run, iso(start + timedelta(minutes=1)), rc)

    GOOD = [("4", "water-level fit", "ok", True), ("5", "7-day plot", "ok", True),
            ("6", "tide plot", "skipped", False)]

    def test_a_good_last_night_is_ok(self) -> None:
        self.write(9, self.GOOD)
        (status, message), = self.findings()
        self.assertEqual(status, "OK")
        self.assertIn("2 step(s) ok, 1 skipped", message)

    def test_one_failed_night_warns(self) -> None:
        self.write(9, [("4", "water-level fit", "failed", True)], last_ok_hours=33)
        (status, message), = self.findings()
        self.assertEqual(status, "WARN")
        self.assertIn("Step 4 (water-level fit) failed in the run of", message)
        self.assertIn("(exit 1): why water-level fit failed", message)
        self.assertIn("the last run without a failed step ended", message)

    def test_a_second_failed_night_fails(self) -> None:
        self.write(9, [("5", "7-day plot", "failed", True)], last_ok_hours=57)
        (status, message), = self.findings()
        self.assertEqual(status, "FAIL")
        self.assertIn("not a single bad night", message)

    def test_failing_since_the_status_was_first_kept(self) -> None:
        self.write(9, [("2", "process new raw data", "failed", True)], first_hours=10)
        self.assertEqual([s for s, _ in self.findings()], ["WARN"])
        sh.findings.clear()
        self.write(9, [("2", "process new raw data", "failed", True)], first_hours=58)
        (status, message), = self.findings()
        self.assertEqual(status, "FAIL")
        self.assertIn("no run without a failed step since", message)

    TIDE_FAILS = [("4", "water-level fit", "ok", True), ("6", "tide comparison", "failed", False)]

    def test_an_optional_step_failing_every_night_only_warns(self) -> None:
        # the run is still ok, so it is its own last ok run: the morning
        # after each night it is a warning, however long it has gone on
        for first_hours in (9, 33, 57, 240):
            sh.findings.clear()
            self.write(9, self.TIDE_FAILS, first_hours=first_hours)
            self.assertTrue(rs.load(self.path)["ok"])
            self.assertEqual([s for s, _ in self.findings()], ["WARN"])

    def test_an_old_run_fails_even_if_only_a_diagnostic_failed_in_it(self) -> None:
        # the tide comparison failing every night must not hide that the
        # run itself has stopped coming
        self.write(57, self.TIDE_FAILS)
        found = self.findings()
        self.assertEqual([s for s, _ in found], ["WARN", "FAIL"])
        self.assertIn("Step 6 (tide comparison) failed", found[0][1])
        self.assertIn("has not run since", found[1][1])

    def test_a_run_stopped_in_a_diagnostic_step(self) -> None:
        # SIGTERM during the tide plot: the step says so; the run is not
        # ok, so the age of the last ok run counts
        steps = [("4", "water-level fit", "ok", True), ("6", "tide plot", "failed", False)]
        self.write(9, steps, rc=None, last_ok_hours=33)
        self.assertEqual([s for s, _ in self.findings()], ["WARN"])
        sh.findings.clear()
        self.write(9, steps, rc=None, last_ok_hours=57)
        found = self.findings()
        self.assertEqual([s for s, _ in found], ["WARN", "FAIL"])
        self.assertIn("ended without an exit status", found[1][1])
        self.assertIn("the last run without a failed step ended", found[1][1])

    def test_a_good_run_long_ago_fails(self) -> None:
        self.write(72, self.GOOD)
        (status, message), = self.findings()
        self.assertEqual(status, "FAIL")
        self.assertIn("has not run since", message)

    def test_missing_or_unreadable_status_warns(self) -> None:
        (status, message), = self.findings()
        self.assertEqual(status, "WARN")
        self.assertIn("has not run since it began writing one", message)
        sh.findings.clear()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json")
        (status, message), = self.findings()
        self.assertEqual((status, message), ("WARN", f"could not read {self.path.name}"))

    def test_a_run_in_progress(self) -> None:
        # a live process whose command line names process_and_plot.sh
        script = self.proj / "process_and_plot.sh"
        script.write_text("sleep 60\n")
        p = subprocess.Popen(["bash", str(script)])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.write(0.2, [("0", "sanity checks", "ok", True)], last_ok_hours=24,
                   finished=False, pid=p.pid)
        self.assertEqual([s for s, _ in self.findings()], ["OK"])
        sh.findings.clear()
        self.write(8, [("0", "sanity checks", "ok", True)], last_ok_hours=30,
                   finished=False, pid=p.pid)
        (status, message), = self.findings()
        self.assertEqual(status, "WARN")
        self.assertIn("still running after 8 h", message)
        # the same pid, now something else: the run is gone
        sh.findings.clear()
        self.write(0.2, [("0", "sanity checks", "ok", True)], last_ok_hours=24,
                   finished=False, pid=os.getpid())
        (status, message), = self.findings()
        self.assertEqual(status, "FAIL")
        self.assertIn("stopped before it finished", message)

    def test_main_reports_the_run(self) -> None:
        db = self.proj / "database" / "station.db"
        db.parent.mkdir()
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE error_log (id INTEGER PRIMARY KEY, timestamp TEXT, module "
                    "TEXT, severity TEXT, description TEXT, recovered INTEGER)")
        con.execute("CREATE TABLE system_health (id INTEGER PRIMARY KEY, timestamp TEXT, "
                    "disk_free REAL, receiver_connected INTEGER, internet_connected INTEGER)")
        con.commit()
        con.close()
        self.write(9, [("5", "7-day plot", "failed", True)], last_ok_hours=57)
        with unittest.mock.patch.object(sys, "argv", ["station_health.py"]), \
                unittest.mock.patch("sys.stdout") as out:
            self.assertEqual(sh.main(), 1)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("[FAIL] run: Step 5 (7-day plot) failed", printed)


if __name__ == "__main__":
    unittest.main()
