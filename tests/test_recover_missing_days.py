"""
test_recover_missing_days.py

Tests for maintenance/recover_missing_days.py and its shell wrapper
(review B12). No convbin or gnssrefl is needed: the converter and the
GNSS-IR processor are replaced by fakes that write what the real ones
write (RINEX into the scratch folder; an SNR file and a results file
under REFL_CODE), so the day finding, the source order, the outcome
rules and the clean-up are tested as they run on the station.

Run with:

    python3 -m unittest discover -s tests -p "test_recover_missing_days.py" -v
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "maintenance"))
sys.path.insert(0, str(ROOT / "station"))

import recover_missing_days as rmd  # noqa: E402

NOW = datetime(2026, 10, 21, 2, 30, tzinfo=timezone.utc)   # 22:30 EDT, 20 Oct


def doy_of(day: date) -> int:
    return day.timetuple().tm_yday


class FakeRinex:
    """Writes .obs/.nav/.sbs into the scratch folder, as convbin does."""

    def __init__(self, scratch: Path, fail=()):
        self.scratch = scratch
        self.fail = set(fail)
        self.converted: list[Path] = []

    def convert(self, raw_path: Path):
        self.converted.append(raw_path)
        obs, nav, sbs = (self.scratch / f"{raw_path.stem}.{e}" for e in ("obs", "nav", "sbs"))
        if raw_path.name in self.fail:
            return SimpleNamespace(success=False, message="convbin exited 1: bad data",
                                   observation_file=obs, navigation_file=nav, sbas_file=sbs)
        for path in (obs, nav, sbs):
            path.write_text("     3.05           OBSERVATION DATA\n")
        return SimpleNamespace(success=True, message="verified",
                               observation_file=obs, navigation_file=nav, sbas_file=sbs)


class FakeGnssir:
    """
    Writes what rinex2snr + gnssir write, per day: "ok" an SNR file and
    a results file with arcs, "nodata" only the SNR file (gnssir kept
    no arc), "noorbit" nothing (rinex2snr had no orbit), "crash" the
    SNR file and then gnssir raised (gnssrefl_processor's message).
    """

    def __init__(self, station, behaviour=None, default="ok"):
        self.station = station
        self.behaviour = behaviour or {}
        self.default = default
        self.calls: list[tuple[date, Path, bool, str]] = []

    def process(self, observation_file, day):
        observation_file = Path(observation_file)
        self.calls.append((day, observation_file, observation_file.exists(), os.getcwd()))
        what = self.behaviour.get(day, self.default)
        if what in ("ok", "nodata", "crash"):
            snr = (self.station.refl_code / str(day.year) / "snr" / self.station.code4
                   / f"{self.station.code4}{doy_of(day):03d}0.{day:%y}.snr66")
            snr.parent.mkdir(parents=True, exist_ok=True)
            snr.write_text("snr\n")
            results = self.station.results_file(day)
            if what == "ok":
                results.parent.mkdir(parents=True, exist_ok=True)
                results.write_text(f"% year doy RH\n{day.year} {doy_of(day)} 19.1\n")
            elif what == "nodata" and results.exists():
                results.unlink()    # gnssrefl 4.1.5 removes it when no arc is kept
        if what == "crash":
            return SimpleNamespace(success=False, message=(
                "gnssir failed: [Errno 2] No such file: gpt_1wA.pickle"))
        return SimpleNamespace(success=what == "ok", message=f"fake {what}")


class RecoverTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.external = self.root / "external"
        for sub in ("raw", "rinex", "archive", "products"):
            (self.project / sub).mkdir(parents=True)
        self.station_section = {
            "station_id": "USGS00USA",
            "gnssrefl_station_code": "abcd",
            "gnssrefl_monument_number": "00",
            "gnssrefl_country_code": "usa",
            "gnssrefl_sample_rate": 1,
            "external_storage_path": str(self.external),
        }
        self.cfg = SimpleNamespace(
            project_root=self.project,
            raw_dir=self.project / "raw",
            rinex_dir=self.project / "rinex",
            archive_dir=self.project / "archive",
            products_dir=self.project / "products",
            station_id="USGS00USA",
            station=self.station_section,
        )
        self.station = rmd.station_from_config(self.cfg)
        self.rinex = None
        self.gnssir = FakeGnssir(self.station)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def raw(self, folder: Path, day: date, content: bytes = b"\xaa" * 100) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"station_{day:%Y%m%d}.um980"
        path.write_bytes(content)
        return path

    def legacy(self, day: date, export: str = "20260720", code9: str = "ABCD00USA") -> Path:
        folder = self.external / "Products" / export / "rinex"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{code9}_R_{day.year}{doy_of(day):03d}0000_01D_01S_MO.rnx"
        path.write_text("rinex\n")
        return path

    def results(self, day: date) -> Path:
        path = self.station.results_file(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("% header\n2026 1 19.0\n")
        return path

    def run_main(self, *argv):
        def rinex_factory(scratch):
            self.rinex = FakeRinex(scratch, fail=getattr(self, "convbin_fails", ()))
            return self.rinex

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rmd.main(list(argv), cfg=self.cfg, now=NOW,
                            rinex_factory=rinex_factory,
                            gnssir_factory=lambda: self.gnssir)
        return code, out.getvalue()

    def processed_days(self):
        return [call[0] for call in self.gnssir.calls]

    def scratch_dirs(self):
        return list(self.project.glob(".recover_missing_days_*"))


class TestStationFromStationJson(RecoverTestCase):

    def test_codes_and_folders_come_from_station_json(self) -> None:
        # Not the hard-coded usgs / usgs00usa / 01S of the shell version.
        self.assertEqual(self.station.code4, "abcd")
        self.assertEqual(self.station.code9, "abcd00usa")
        self.assertEqual(self.station.refl_code, self.project / "products" / "refl_code")
        self.assertEqual(self.station.results_file(date(2026, 1, 8)).name, "008.txt")
        self.assertEqual(self.station.no_data_marker(date(2026, 1, 8)).name, "8.no_data")

    def test_archive_conf_station_code_mismatch_is_reported(self) -> None:
        (self.project / "archive.conf").write_text(
            '# comment\nS3_BASE="s3://b/p"\nSTATION_CODE="usgs"   # 4 chars\n')
        _code, out = self.run_main()
        self.assertIn("archive.conf STATION_CODE='usgs' differs", out)
        (self.project / "archive.conf").write_text('STATION_CODE="abcd"\n')
        _code, out = self.run_main()
        self.assertNotIn("differs", out)


class TestSources(RecoverTestCase):

    def test_archive_then_external_raw_then_legacy_rinex(self) -> None:
        a, b, c, d = (date(2026, 7, n) for n in (1, 2, 3, 4))
        self.raw(self.project / "archive", a)
        self.raw(self.project / "archive", b)
        self.raw(self.external / "Raw", b)          # also archived: archive/ wins
        self.raw(self.external / "Raw", c)
        self.legacy(c)                              # raw wins over RINEX
        self.legacy(d)
        self.legacy(date(2026, 7, 5), code9="WXYZ00USA")      # another station
        self.raw(self.external / "Raw", date(2025, 7, 1))     # another year
        (self.external / "Raw" / "overnight_20260706_1352.um980").write_bytes(b"x")

        sources = rmd.find_sources(self.cfg, self.station, self.external, 2026)

        self.assertEqual(sorted(sources), [a, b, c, d])
        self.assertEqual(sources[a].kind, rmd.ARCHIVE)
        self.assertEqual(sources[b].kind, rmd.ARCHIVE)
        self.assertEqual(sources[c].kind, rmd.EXTERNAL_RAW)
        self.assertEqual(sources[d].kind, rmd.EXTERNAL_RINEX)
        self.assertFalse(sources[d].is_raw)

    def test_unmounted_external_storage_still_searches_archive(self) -> None:
        self.raw(self.project / "archive", date(2026, 7, 1))
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("External storage not available", out)
        self.assertEqual(self.processed_days(), [date(2026, 7, 1)])


class TestAutomaticMode(RecoverTestCase):

    def test_recovers_days_with_data_but_no_results(self) -> None:
        missing = date(2026, 7, 1)
        done = date(2026, 7, 2)
        in_raw = date(2026, 7, 3)
        today = NOW.date()
        sources = [
            self.raw(self.external / "Raw", missing),
            self.raw(self.project / "archive", done),
            self.raw(self.external / "Raw", in_raw),
            self.raw(self.external / "Raw", today),
        ]
        self.raw(self.project / "raw", in_raw)       # still the pipeline's
        self.results(done)
        sizes = [p.stat().st_size for p in sources]

        code, out = self.run_main()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.processed_days(), [missing])
        self.assertTrue(rmd.has_results(self.station.results_file(missing)))
        self.assertIn("recovered: fake ok", out)
        # the raw files are only read; nothing is left in rinex/ or scratch
        self.assertEqual([p.stat().st_size for p in sources], sizes)
        self.assertEqual(list((self.project / "rinex").iterdir()), [])
        self.assertEqual(self.scratch_dirs(), [])
        # converted in, and processed from, the scratch folder
        _day, obs, existed, cwd = self.gnssir.calls[0]
        self.assertTrue(existed)
        self.assertEqual(obs.parent.name, Path(cwd).name)
        self.assertTrue(Path(cwd).name.startswith(".recover_missing_days_"))

    def test_no_data_gets_a_marker_not_yet_does_not(self) -> None:
        # A fresh SNR file with no arcs is "no data" (marker, not tried
        # again); no SNR file at all is "not yet" (the orbit), tried
        # again next run. The shell version marked both.
        nodata, noorbit = date(2026, 7, 1), date(2026, 7, 2)
        self.raw(self.external / "Raw", nodata)
        self.raw(self.external / "Raw", noorbit)
        self.gnssir.behaviour = {nodata: "nodata", noorbit: "noorbit"}

        code, out = self.run_main()

        self.assertEqual(code, 0)
        self.assertTrue(self.station.no_data_marker(nodata).exists())
        self.assertFalse(self.station.no_data_marker(noorbit).exists())
        self.assertIn("No SNR file yet (orbit not published?), tried again next run: "
                      f"{doy_of(noorbit)}", out)

        self.gnssir.calls.clear()
        self.gnssir.behaviour = {}
        self.run_main()
        self.assertEqual(self.processed_days(), [noorbit])

    def test_gnssir_crash_after_the_snr_file_is_failed_not_no_data(self) -> None:
        # The SNR file is fresh, but gnssir raised: no marker, so the
        # day is tried again (the shell version checked gnssir's exit
        # code for this).
        day = date(2026, 7, 1)
        self.raw(self.external / "Raw", day)
        self.gnssir.behaviour = {day: "crash"}

        code, out = self.run_main()

        self.assertEqual(code, 0)
        self.assertFalse(self.station.no_data_marker(day).exists())
        self.assertIn("failed: gnssir failed: [Errno 2]", out)
        self.assertIn(f"Failed, tried again next run: {doy_of(day)}", out)

        self.gnssir.calls.clear()
        self.gnssir.behaviour = {}
        self.run_main()
        self.assertEqual(self.processed_days(), [day])

    def test_scratch_folder_is_emptied_after_every_day(self) -> None:
        # rinex2snr copies the day's RINEX into the working directory
        # and leaves it there (350-400 MB a day).
        days = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
        for day in days:
            self.raw(self.external / "Raw", day)
        seen = []

        class LeavesCopies(FakeGnssir):
            def process(self, observation_file, day):
                cwd = Path(os.getcwd())
                seen.append(sorted(p.name for p in cwd.iterdir()
                                   if p.suffix == ".rnx" or p.is_dir()))
                (cwd / f"ABCD00USA_R_{day:%Y%j}0000_01D_01S_MO.rnx").write_bytes(b"x" * 1000)
                (cwd / "logs").mkdir(exist_ok=True)
                return super().process(observation_file, day)

        self.gnssir = LeavesCopies(self.station)
        code, _out = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(self.processed_days(), days)
        self.assertEqual(seen, [[], [], []])
        self.assertEqual(self.scratch_dirs(), [])

    def test_stale_scratch_folders_are_removed(self) -> None:
        old = self.project / ".recover_missing_days_old"
        new = self.project / ".recover_missing_days_running"
        for folder in (old, new):
            folder.mkdir()
            (folder / "copy.rnx").write_text("x")
        two_days_ago = time.time() - 2 * 86400
        os.utime(old, (two_days_ago, two_days_ago))
        self.raw(self.external / "Raw", date(2026, 7, 1))

        self.run_main()

        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_convbin_failure_is_failed_and_tried_again(self) -> None:
        day = date(2026, 7, 1)
        raw = self.raw(self.external / "Raw", day)
        self.convbin_fails = {raw.name}

        code, out = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(self.processed_days(), [])
        self.assertIn("failed: convbin: convbin exited 1", out)
        self.assertFalse(self.station.no_data_marker(day).exists())
        self.assertEqual(self.scratch_dirs(), [])

    def test_legacy_rinex_is_processed_as_is(self) -> None:
        day = date(2026, 7, 4)
        rinex = self.legacy(day)

        self.run_main()

        self.assertEqual(self.gnssir.calls[0][1], rinex)
        self.assertIsNone(self.rinex)                  # no convbin for RINEX
        self.assertTrue(rinex.exists())

    def test_dry_run_processes_nothing(self) -> None:
        self.raw(self.external / "Raw", date(2026, 7, 1))
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn(f"from {rmd.EXTERNAL_RAW}", out)
        self.assertEqual(self.gnssir.calls, [])
        self.assertEqual(self.scratch_dirs(), [])

    def test_since_skips_days_before_the_first_day(self) -> None:
        # The shell version's RECOVER_SINCE rule, kept.
        for doy in range(185, 196):                     # 4-14 July
            self.legacy(date(2026, 1, 1) + timedelta(days=doy - 1))

        _code, out = self.run_main("--year", "2026", "--since", "2026-07-10")

        self.assertEqual([doy_of(d) for d in self.processed_days()], [191, 192, 193, 194, 195])
        self.assertIn("Skipped 6 day(s) of 2026 before 2026-07-10", out)

        self.gnssir.calls.clear()
        for day in list(self.station.refl_code.glob("2026/results/abcd/*.txt")):
            day.unlink()
        self.run_main("--year", "2026", "--since", "2027-01-03")
        self.assertEqual(self.gnssir.calls, [])


class TestRealConverterWritesToScratch(RecoverTestCase):

    def test_real_rinex_processor_converts_into_scratch_not_rinex(self) -> None:
        # The real station/rinex_processor.py with the pipeline's flags,
        # pointed at a stand-in convbin (station.json's convbin_path):
        # the RINEX goes to the scratch folder and is removed after.
        convbin = self.root / "convbin"
        convbin.write_text(
            "#!/bin/sh\n"
            "[ $# -eq 0 ] && { echo 'convbin ver.fake'; exit 1; }\n"
            "while [ $# -gt 1 ]; do\n"
            "  case \"$1\" in -o|-n|-s) echo '     3.05           OBSERVATION DATA    "
            "M                   RINEX VERSION / TYPE' > \"$2\"; shift 2;;\n"
            "  *) shift;; esac\n"
            "done\n")
        convbin.chmod(0o755)
        self.station_section["convbin_path"] = str(convbin)
        raw = self.raw(self.external / "Raw", date(2026, 7, 1))

        seen = {}

        class Watching(FakeGnssir):
            def process(self, observation_file, day):
                seen["obs"] = Path(observation_file)
                seen["exists"] = Path(observation_file).exists()
                return super().process(observation_file, day)

        self.gnssir = Watching(self.station)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rmd.main([], cfg=self.cfg, now=NOW, gnssir_factory=lambda: self.gnssir)

        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(seen["exists"])
        self.assertEqual(seen["obs"].name, "station_20260701.obs")
        self.assertTrue(seen["obs"].parent.name.startswith(".recover_missing_days_"))
        self.assertEqual(list((self.project / "rinex").iterdir()), [])
        self.assertEqual(self.scratch_dirs(), [])
        self.assertTrue(raw.exists())
        self.assertTrue(rmd.has_results(self.station.results_file(date(2026, 7, 1))))


class TestGivenDays(RecoverTestCase):

    def test_given_days_are_reprocessed_even_with_results(self) -> None:
        day = date(2026, 7, 1)
        self.raw(self.project / "archive", day)
        old = self.results(day)
        os.utime(old, (old.stat().st_mtime - 3600,) * 2)
        self.station.no_data_marker(day).write_text("old marker\n")

        code, out = self.run_main(str(doy_of(day)), "--since", "2026-12-01")

        self.assertEqual(code, 0)
        self.assertEqual(self.processed_days(), [day])
        self.assertIn("recovered", out)
        self.assertFalse(self.station.no_data_marker(day).exists())

    def test_today_raw_dir_and_missing_days_are_refused(self) -> None:
        self.raw(self.project / "raw", date(2026, 7, 3))
        code, out = self.run_main(str(doy_of(NOW.date())), str(doy_of(date(2026, 7, 3))),
                                  "200", "400")

        self.assertEqual(code, 0)
        self.assertEqual(self.gnssir.calls, [])
        self.assertIn("still being recorded", out)
        self.assertIn("still in raw/", out)
        self.assertIn("200 (2026-07-19): no raw file", out)
        self.assertIn("400: not a day of 2026", out)

    def test_a_kept_results_file_is_not_called_recovered(self) -> None:
        # gnssir made SNR but did not rewrite the old results: not
        # "recovered", and no marker over real results.
        day = date(2026, 7, 1)
        self.raw(self.project / "archive", day)
        old = self.results(day)
        os.utime(old, (old.stat().st_mtime - 3600,) * 2)

        class KeepsOld(FakeGnssir):
            def process(self, observation_file, day):
                self.calls.append((day, Path(observation_file), True, os.getcwd()))
                snr = (self.station.refl_code / str(day.year) / "snr" / self.station.code4
                       / f"{self.station.code4}{doy_of(day):03d}0.{day:%y}.snr66")
                snr.parent.mkdir(parents=True, exist_ok=True)
                snr.write_text("snr\n")
                return SimpleNamespace(success=True, message="kept")

        self.gnssir = KeepsOld(self.station)
        _code, out = self.run_main(str(doy_of(day)))

        self.assertIn("failed: gnssir did not replace the existing results file", out)
        self.assertFalse(self.station.no_data_marker(day).exists())


class TestFailuresAndInterrupts(RecoverTestCase):

    def test_processing_setup_failure_exits_1_and_cleans_up(self) -> None:
        self.raw(self.external / "Raw", date(2026, 7, 1))

        def no_gnssrefl():
            raise RuntimeError("gnssrefl is not installed")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rmd.main([], cfg=self.cfg, now=NOW,
                            rinex_factory=lambda s: FakeRinex(s),
                            gnssir_factory=no_gnssrefl)

        self.assertEqual(code, 1)
        self.assertIn("Stopped: RuntimeError: gnssrefl is not installed", out.getvalue())
        self.assertEqual(self.scratch_dirs(), [])

    def test_interrupt_removes_scratch_and_exits_130(self) -> None:
        self.raw(self.external / "Raw", date(2026, 7, 1))
        cwd = os.getcwd()

        class Interrupted(FakeGnssir):
            def process(self, observation_file, day):
                raise KeyboardInterrupt("signal 15")

        self.gnssir = Interrupted(self.station)
        code, out = self.run_main()

        self.assertEqual(code, 130)
        self.assertIn("Interrupted (signal 15)", out)
        self.assertEqual(self.scratch_dirs(), [])
        self.assertEqual(os.getcwd(), cwd)

    def test_sigterm_is_not_a_system_exit(self) -> None:
        # gnssrefl_processor.py catches SystemExit around rinex2snr and
        # gnssir, so a SIGTERM raised as SystemExit would only fail
        # that day and go on to the next.
        with self.assertRaises(KeyboardInterrupt):
            rmd._interrupt(15, None)


class TestShellWrapper(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "anywhere"
        (self.proj / "maintenance").mkdir(parents=True)
        script = (ROOT / "maintenance" / "recover_missing_days.sh").read_text()
        self.script = self.proj / "maintenance" / "recover_missing_days.sh"
        self.script.write_text(script)
        venv = self.proj / "gnssrefl_venv" / "bin"
        venv.mkdir(parents=True)
        self.log = Path(self._tmp.name) / "argv.log"
        (venv / "python3").write_text(
            f'#!/bin/sh\necho "$@" > {self.log}\nexit 3\n')
        (venv / "python3").chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_wrapper(self, *args, **env):
        base = {k: v for k, v in os.environ.items()
                if k not in ("RECOVER_YEAR", "RECOVER_SINCE")}
        return subprocess.run(["bash", str(self.script), *args],
                              env=dict(base, **env), capture_output=True, text=True,
                              timeout=60)

    def test_runs_the_helper_of_its_own_project_with_venv_python(self) -> None:
        r = self.run_wrapper("204", "205")
        self.assertEqual(r.returncode, 3, r.stderr)        # the helper's exit code
        self.assertEqual(self.log.read_text().split(),
                         [str(self.proj / "maintenance" / "recover_missing_days.py"),
                          "204", "205"])

    def test_passes_recover_year_and_since(self) -> None:
        self.run_wrapper("--dry-run", RECOVER_YEAR="2025", RECOVER_SINCE="2025-07-10")
        self.assertEqual(self.log.read_text().split()[1:],
                         ["--year", "2025", "--since", "2025-07-10", "--dry-run"])

    def test_no_hard_coded_paths_or_codes(self) -> None:
        text = (ROOT / "maintenance" / "recover_missing_days.sh").read_text()
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#"))
        for hard_coded in ("GNSS/v4.1", "/mnt/", "usgs", "01S", "process_gps_data"):
            self.assertNotIn(hard_coded, code)


if __name__ == "__main__":
    unittest.main()
