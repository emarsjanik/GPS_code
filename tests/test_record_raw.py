"""
test_record_raw.py

Unit tests for Receiver.record_raw() / RecordingResult in
station/receiver.py.

Self-contained: installs its own fake `serial` module (mirroring
tests/test_receiver.py's pattern), so no real hardware or pyserial
installation is required.

FakeSerial here is *scripted* rather than a simple pre-populated
queue: each readline() call consumes the next entry of an explicit,
ordered script (a string -> encoded as a line; raw bytes -> returned
verbatim, e.g. b"" for "nothing available right now"; an exception
instance -> raised). This determinism matters specifically for
record_raw(): its "flush stale data" step (see receiver.py) drains
anything already buffered *before* the timed recording loop starts,
so naively pre-queuing messages before calling record_raw() would
have them silently eaten by that flush rather than captured by the
loop under test. Scripting an explicit empty slot between the
VERSIONA identification response and the "real" data makes the
handoff from flush-phase to recording-phase exact and reproducible,
with no reliance on background threads or timing races.

Run with:

    python3 -m unittest discover -s tests -p "test_record_raw.py" -v
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))


class _FakeSerialException(Exception):
    """Stand-in for serial.SerialException."""


class FakeSerial:
    """
    Scripted stand-in for serial.Serial. See module docstring for why
    this is scripted rather than a simple pre-populated queue.
    """

    def __init__(self, port: str, baudrate: int, timeout: float):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.written: list[bytes] = []
        self._script: list = []
        self._index = 0
        self._continuous_line: bytes | None = None
        self._pending: bytes = b""

    def set_script(self, *steps) -> None:
        """
        Each step is a str (encoded as that line + CRLF), bytes
        (returned verbatim -- use b"" for "nothing available"), or a
        BaseException instance (raised). Consumed in order, one step
        per refill (see _refill_pending()); once exhausted, reads
        return b"" (idle) forever after, unless set_continuous_stream()
        was also called.
        """

        self._script = list(steps)
        self._index = 0
        self._pending = b""

    def append_step(self, step) -> None:
        """
        Append one more step to the (already-running) script, without
        resetting the read index -- used to deliver data from a
        background thread after a deliberate delay, for tests whose
        timing windows are naturally well-separated (e.g. after an
        earlier query has already timed out).
        """

        self._script.append(step)

    def set_continuous_stream(self, line: str) -> None:
        """
        Once the scripted steps are exhausted, keep returning `line`
        forever -- simulating a receiver whose stream genuinely never
        has a gap (e.g. RANGEA at 1 Hz+ with no pauses), rather than
        the default idle b"".
        """

        self._continuous_line = (line + "\r\n").encode("ascii")

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def _refill_pending(self) -> None:
        if self._pending:
            return

        if self._index < len(self._script):
            step = self._script[self._index]
            self._index += 1

            if isinstance(step, BaseException):
                raise step

            if isinstance(step, str):
                self._pending = (step + "\r\n").encode("ascii")
            else:
                self._pending = step

            return

        if self._continuous_line is not None:
            self._pending = self._continuous_line
            return

        # Idle: mirrors real pyserial blocking up to its read
        # timeout before returning empty, rather than a hot loop.
        time.sleep(min(0.01, self.timeout or 0.01))
        self._pending = b""

    def read(self, n: int = 1) -> bytes:
        """
        Byte-level read, needed since read_message() peeks one byte
        at a time to detect a binary sync sequence before falling
        back to readline() for ASCII/NMEA text. Draws from the same
        script/continuous-stream as readline(), so either can be
        called in any combination without losing, duplicating, or
        misaligning bytes.
        """

        self._refill_pending()
        chunk = self._pending[:n]
        self._pending = self._pending[n:]
        return chunk

    def readline(self) -> bytes:
        self._refill_pending()

        if not self._pending:
            return b""

        newline_index = self._pending.find(b"\n")

        if newline_index == -1:
            chunk = self._pending
            self._pending = b""
        else:
            chunk = self._pending[: newline_index + 1]
            self._pending = self._pending[newline_index + 1 :]

        return chunk

    def close(self) -> None:
        self.is_open = False


def _install_fake_serial_module() -> types.ModuleType:
    fake_module = types.ModuleType("serial")
    fake_module.SerialException = _FakeSerialException
    fake_module.current = None

    def _serial_factory(port: str, baudrate: int, timeout: float) -> FakeSerial:
        if fake_module.current is None:
            fake_module.current = FakeSerial(port, baudrate, timeout)
        else:
            fake_module.current.is_open = True
        return fake_module.current

    fake_module.Serial = _serial_factory
    sys.modules["serial"] = fake_module

    return fake_module


_install_fake_serial_module()

import receiver  # noqa: E402


# ------------------------------------------------------------
# Fixtures (same recorded/synthetic UM980-style responses used in
# tests/test_receiver.py; duplicated here for this file's
# self-containment).
# ------------------------------------------------------------

VERSIONA_REAL = (
    '#VERSIONA,97,GPS,FINE,2426,224878000,0,0,18,639;'
    '"UM980","R4.10Build11833","HRPT00-S10C-P",'
    '"2310415000001-MD22A4244202829","ff3be4677d4e25dc",'
    '"2023/11/24"*8f3f0201'
)

BESTPOSA_VALID = (
    '#BESTPOSA,COM1,0,71.5,FINESTEERING,2216,148248.000,00000020,'
    '3681,16809;SOL_COMPUTED,SINGLE,41.8928336813,-69.9633123013,'
    '21.774,-30.000,WGS84,0.0100,0.0120,0.0250,"",1.500,2.500,12,11,'
    '11,0,0,0,00,00*381fd21d'
)

BESTPOSA_BAD_CHECKSUM = BESTPOSA_VALID[:-8] + "deadbeef"


class RecordRawTestCase(unittest.TestCase):
    """
    Every test starts from a connected Receiver whose FakeSerial
    script begins with (VERSIONA_REAL, b""): the VERSIONA response
    record_raw() queries for receiver identification, followed by
    one empty slot ending the "flush stale data" step. Whatever the
    test appends via extend_script() after that lands in the timed
    recording loop itself.
    """

    def setUp(self) -> None:
        # Re-install THIS file's fake serial module and force
        # receiver.py to rebind its `import serial` to it, in case
        # another test module (e.g. tests/test_receiver.py, which
        # installs its own different FakeSerial) ran/imported more
        # recently and left sys.modules["serial"] pointing elsewhere.
        # `import serial` binds a name once at import time, not a
        # live lookup, so simply reassigning sys.modules["serial"]
        # afterward doesn't retroactively fix an already-imported
        # receiver module -- reload() forces that re-binding.
        _install_fake_serial_module()
        importlib.reload(receiver)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

        self.rx = receiver.Receiver(
            device="/dev/USB_GPS",
            baudrate=115200,
            timeout=1.0,
            serial_timeout=0.05,
            retries=0,
        )
        self.rx.connect()
        self.fake: FakeSerial = self.rx._serial

        self._script: list = [VERSIONA_REAL, b""]
        self.fake.set_script(*self._script)

    def extend_script(self, *steps) -> None:
        """Append steps for the timed recording loop to consume."""

        self._script.extend(steps)
        self.fake.set_script(*self._script)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()


# ------------------------------------------------------------
# File handling
# ------------------------------------------------------------

class TestFileHandling(RecordRawTestCase):

    def test_file_created(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertTrue(destination.exists())
        self.assertTrue(result.successful)

    def test_directory_auto_created(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "nested" / "deep" / "raw.bin"

        self.rx.record_raw(destination, duration=0.2)

        self.assertTrue(destination.exists())
        self.assertTrue(destination.parent.is_dir())

    def test_overwrite_replaces_existing_content(self) -> None:
        destination = self.root / "raw.bin"
        destination.write_bytes(b"OLD CONTENT THAT SHOULD BE GONE")

        self.extend_script(BESTPOSA_VALID)
        self.rx.record_raw(destination, duration=0.2, append=False)

        self.assertNotIn(b"OLD CONTENT", destination.read_bytes())

    def test_append_preserves_existing_content(self) -> None:
        destination = self.root / "raw.bin"
        destination.write_bytes(b"PREVIOUS SESSION DATA\n")

        self.extend_script(BESTPOSA_VALID)
        self.rx.record_raw(destination, duration=0.2, append=True)

        contents = destination.read_bytes()
        self.assertTrue(contents.startswith(b"PREVIOUS SESSION DATA\n"))
        self.assertIn(b"BESTPOSA", contents)

    def test_invalid_filename_missing_directory_without_create(self) -> None:
        destination = self.root / "does_not_exist" / "raw.bin"

        with self.assertRaises((FileNotFoundError, OSError)):
            self.rx.record_raw(destination, duration=0.1, create_directory=False)

    def test_filename_expanded_and_resolved_to_absolute(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertTrue(result.filename.is_absolute())


# ------------------------------------------------------------
# Bytes / statistics / timing
# ------------------------------------------------------------

class TestBytesAndStatistics(RecordRawTestCase):

    def test_bytes_and_messages_written_match_exactly(self) -> None:
        self.extend_script(BESTPOSA_VALID, BESTPOSA_VALID, BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.3)

        self.assertEqual(result.messages_written, 3)
        self.assertEqual(result.bytes_written, destination.stat().st_size)

    def test_raw_bytes_are_byte_perfect(self) -> None:
        # Confirms record_raw() writes the ORIGINAL bytes, including
        # line terminators, not a re-encoding of stripped/decoded
        # text -- central to the "archival-quality" requirement.
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        self.rx.record_raw(destination, duration=0.2)

        self.assertEqual(
            destination.read_bytes(), (BESTPOSA_VALID + "\r\n").encode("ascii")
        )

    def test_statistics_are_internally_consistent(self) -> None:
        self.extend_script(*([BESTPOSA_VALID] * 5))
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.3)

        self.assertAlmostEqual(
            result.average_rate_bytes,
            result.bytes_written / result.duration_actual,
            places=3,
        )
        self.assertAlmostEqual(
            result.average_rate_messages,
            result.messages_written / result.duration_actual,
            places=3,
        )

    def test_timer_accuracy(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"
        requested = 0.3

        started = time.monotonic()
        result = self.rx.record_raw(destination, duration=requested)
        wall_elapsed = time.monotonic() - started

        self.assertGreaterEqual(result.duration_actual, requested)
        self.assertLess(result.duration_actual, requested + 0.5)
        self.assertLess(wall_elapsed, requested + 1.0)


# ------------------------------------------------------------
# Zero-byte detection
# ------------------------------------------------------------

class TestZeroByteDetection(RecordRawTestCase):

    def test_raises_when_nothing_captured(self) -> None:
        # No data extended onto the script -- only the identification
        # VERSIONA response and the flush's empty slot exist, so the
        # timed loop captures nothing for its whole duration.
        destination = self.root / "raw.bin"

        with self.assertRaises(receiver.ReceiverError):
            self.rx.record_raw(destination, duration=0.2)

    def test_zero_byte_file_is_not_left_behind_as_a_false_success(self) -> None:
        destination = self.root / "raw.bin"

        try:
            self.rx.record_raw(destination, duration=0.2)
        except receiver.ReceiverError:
            pass

        # The file may exist (empty) or not; either way it must not
        # be mistaken for a successful, non-empty recording.
        if destination.exists():
            self.assertEqual(destination.stat().st_size, 0)


# ------------------------------------------------------------
# Progress callback
# ------------------------------------------------------------

class TestCallback(RecordRawTestCase):

    def test_callback_called_with_recording_result_snapshots(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        snapshots = []
        result = self.rx.record_raw(
            destination, duration=1.2, progress_callback=snapshots.append
        )

        self.assertGreaterEqual(len(snapshots), 1)
        for snapshot in snapshots:
            self.assertIsInstance(snapshot, receiver.RecordingResult)
            self.assertFalse(snapshot.successful)
        self.assertTrue(result.successful)

    def test_callback_exception_does_not_abort_recording(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        def bad_callback(snapshot):
            raise RuntimeError("boom")

        result = self.rx.record_raw(
            destination, duration=1.2, progress_callback=bad_callback
        )

        self.assertTrue(result.successful)


# ------------------------------------------------------------
# Interruption / errors / connection ownership
# ------------------------------------------------------------

class TestInterruptionAndErrors(RecordRawTestCase):

    def test_keyboard_interrupt_still_closes_file(self) -> None:
        self.extend_script(BESTPOSA_VALID, BESTPOSA_VALID, KeyboardInterrupt())
        destination = self.root / "raw.bin"

        with self.assertRaises(KeyboardInterrupt):
            self.rx.record_raw(destination, duration=5.0)

        self.assertTrue(destination.exists())
        contents = destination.read_bytes()
        self.assertEqual(contents.count(b"BESTPOSA"), 2)

    def test_serial_exception_propagates_without_reconnecting(self) -> None:
        self.extend_script(
            BESTPOSA_VALID, receiver.serial.SerialException("USB unplugged")
        )
        destination = self.root / "raw.bin"

        connection_before = self.rx._serial

        with self.assertRaises(receiver.serial.SerialException):
            self.rx.record_raw(destination, duration=5.0)

        # Connection ownership rule: record_raw() must never
        # reconnect/reopen during recording. Same object as before.
        self.assertIs(self.rx._serial, connection_before)
        self.assertTrue(destination.exists())
        self.assertIn(b"BESTPOSA", destination.read_bytes())

    def test_disconnected_receiver_raises_immediately(self) -> None:
        self.rx.disconnect()
        destination = self.root / "raw.bin"

        with self.assertRaises(receiver.ReceiverNotConnectedError):
            self.rx.record_raw(destination, duration=0.1)

    def test_idle_periods_do_not_abort_recording(self) -> None:
        # Simulates several quiet 1 Hz-style gaps (readline()
        # returning nothing) before one real message arrives -- must
        # not raise or exit early.
        self.extend_script(b"", b"", b"", BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.3)

        self.assertTrue(result.successful)
        self.assertEqual(result.messages_written, 1)


# ------------------------------------------------------------
# Receiver identification
# ------------------------------------------------------------

class TestReceiverIdentification(RecordRawTestCase):

    def test_receiver_model_and_firmware_populated(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertEqual(result.receiver_model, "UM980")
        self.assertEqual(result.receiver_firmware, "R4.10Build11833")

    def test_version_query_failure_is_a_warning_not_fatal(self) -> None:
        # No VERSIONA response scripted at all -- the identification
        # query will time out after ~0.1s. Recording must still
        # proceed: deliver the "real" message on a delayed background
        # thread, landing safely inside the recording window that
        # follows the failed identification attempt (rather than via
        # the script, since _read_until("VERSIONA", ...) would just
        # skip/dispatch a non-matching scripted message and keep
        # waiting, consuming it before recording ever starts).
        sys.modules["serial"].current = None
        self.rx = receiver.Receiver(
            device="/dev/USB_GPS", timeout=0.1, serial_timeout=0.02, retries=0
        )
        self.rx.connect()
        self.fake = self.rx._serial
        self.fake.set_script()  # nothing at all -- identification times out

        def _deliver_later() -> None:
            time.sleep(0.15)
            self.fake.append_step(BESTPOSA_VALID)

        threading.Thread(target=_deliver_later, daemon=True).start()

        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.3)

        self.assertTrue(result.successful)
        self.assertEqual(result.receiver_model, "")
        self.assertTrue(any("identify receiver" in w for w in result.warnings))


# ------------------------------------------------------------
# Receiver logging enable/disable
# ------------------------------------------------------------

class TestLoggingCommands(RecordRawTestCase):

    def test_enable_logging_sends_command(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        self.rx.record_raw(
            destination,
            duration=0.2,
            enable_logging=True,
            logging_command="LOG COM1 RANGEA ONTIME 1",
        )

        sent = [w.decode().strip() for w in self.fake.written]
        self.assertIn("LOG COM1 RANGEA ONTIME 1", sent)

    def test_stop_logging_sends_unlogall(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        self.rx.record_raw(destination, duration=0.2, stop_logging=True)

        sent = [w.decode().strip() for w in self.fake.written]
        self.assertIn("UNLOGALL COM1", sent)

    def test_logging_enabled_flag_recorded_in_result(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2, enable_logging=True)

        self.assertTrue(result.logging_enabled)

    def test_logging_not_enabled_by_default(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertFalse(result.logging_enabled)

    def test_logging_command_accepts_a_list_and_sends_all_of_them(self) -> None:
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        commands = [
            "LOG COM1 RANGEB ONTIME 1",
            "LOG COM1 GPSEPHEMB ONCHANGED",
            "LOG COM1 GLOEPHEMERISB ONCHANGED",
        ]

        self.rx.record_raw(
            destination,
            duration=0.2,
            enable_logging=True,
            logging_command=commands,
        )

        sent = [w.decode().strip() for w in self.fake.written]
        for command in commands:
            self.assertIn(command, sent)

    def test_default_logging_command_is_the_confirmed_binary_set(self) -> None:
        # Regression: these are the specific commands confirmed
        # against real UM980 hardware to produce output convbin can
        # actually build a RINEX obs+nav pair from -- the ASCII
        # ("...A") forms of the same logs did not work.
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        self.rx.record_raw(destination, duration=0.2, enable_logging=True)

        sent = [w.decode().strip() for w in self.fake.written]

        self.assertIn("LOG COM1 RANGEB ONTIME 1", sent)
        self.assertIn("LOG COM1 GPSEPHEMB ONCHANGED", sent)
        self.assertIn("LOG COM1 GLOEPHEMERISB ONCHANGED", sent)
        self.assertIn("LOG COM1 BDSEPHEMB ONCHANGED", sent)
        self.assertIn("LOG COM1 GALEPHEMB ONCHANGED", sent)
        self.assertIn("LOG COM1 QZSSEPHEMERISB ONCHANGED", sent)

        for command in sent:
            self.assertNotIn("ONCE", command)
            self.assertNotIn("RANGEA", command)

    def test_flush_happens_before_enabling_logging_not_after(self) -> None:
        # Regression: confirmed against real hardware that running
        # the flush step *after* sending fresh LOG commands could
        # discard the very first messages those new logs produce.
        # The identification VERSIONA response plus the flush's
        # empty slot are consumed first (per the base class's
        # script), so the very next thing read_message() sees should
        # be the logging command echoes themselves, and then real
        # data -- none of it silently eaten.
        self.extend_script(BESTPOSA_VALID)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(
            destination,
            duration=0.2,
            enable_logging=True,
            logging_command="LOG COM1 RANGEB ONTIME 1",
        )

        self.assertTrue(result.successful)
        self.assertEqual(result.messages_written, 1)
        sent = [w.decode().strip() for w in self.fake.written]
        self.assertNotIn("LOG COM1 RANGEA ONTIME 1", sent)


# ------------------------------------------------------------
# Checksum handling (reuses existing verification -- nothing new)
# ------------------------------------------------------------

class TestChecksumHandling(RecordRawTestCase):

    def test_checksum_failure_recorded_as_warning_not_fatal(self) -> None:
        self.extend_script(BESTPOSA_BAD_CHECKSUM)
        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertTrue(result.successful)
        self.assertTrue(
            any("checksum mismatch" in w.lower() for w in result.warnings)
        )


# ------------------------------------------------------------
# Regression: the exact reported hang -- a continuously streaming
# receiver with no gaps must not make record_raw() wait forever.
# ------------------------------------------------------------

class TestContinuousStreamRegression(RecordRawTestCase):

    def setUp(self) -> None:
        super().setUp()
        # The base class's default script ends with an explicit b""
        # (a deliberate gap, ending the normal flush step). This
        # scenario needs NO gap anywhere after the VERSIONA
        # identification response, to genuinely reproduce "the
        # stream never pauses" -- otherwise that trailing b"" alone
        # would satisfy even a naive "wait for one gap" loop.
        self.fake._script = [VERSIONA_REAL]
        self.fake._index = 0

    def test_flush_stale_data_does_not_hang_on_gapless_stream(self) -> None:
        # Reproduces the field report exactly: a receiver whose
        # stream never has a pause (e.g. RANGEA at 1 Hz+ with zero
        # gaps) previously made the pre-recording "flush stale data"
        # step (`while read_message() is not None: pass`) spin
        # forever, since read_message() never returned None. It is
        # now bounded by a short, fixed time budget instead.
        self.fake.set_continuous_stream(BESTPOSA_VALID)

        destination = self.root / "raw.bin"

        started = time.monotonic()
        result = self.rx.record_raw(destination, duration=0.5)
        elapsed = time.monotonic() - started

        self.assertTrue(result.successful)
        # Must complete in roughly duration + the flush budget, with
        # generous slack -- not hang indefinitely.
        self.assertLess(
            elapsed, 0.5 + receiver._FLUSH_STALE_DATA_BUDGET + 2.0
        )

    def test_gapless_stream_still_captures_data_during_recording(self) -> None:
        self.fake.set_continuous_stream(BESTPOSA_VALID)

        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.3)

        self.assertTrue(result.successful)
        self.assertGreater(result.messages_written, 0)
        self.assertGreater(result.bytes_written, 0)


# ------------------------------------------------------------
# Regression: a fresh connection's first read can land mid-message,
# not at a clean boundary -- confirmed against real hardware, where
# a recording's first bytes were a bare data fragment with no
# "#"/"$" header at all.
# ------------------------------------------------------------

class TestMessageBoundarySync(RecordRawTestCase):

    def test_leading_fragment_is_skipped_not_written(self) -> None:
        # A trailing fragment of some earlier, already-in-flight
        # message -- no "#"/"$" header -- followed by a real,
        # complete message.
        fragment = "302,0.011,2316.995,36.010,205b1ca3"
        self.extend_script(fragment, BESTPOSA_VALID)

        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertTrue(result.successful)
        self.assertEqual(result.messages_written, 1)

        contents = destination.read_bytes()
        self.assertNotIn(b"302,0.011", contents)
        self.assertEqual(contents, (BESTPOSA_VALID + "\r\n").encode("ascii"))

    def test_multiple_leading_fragments_are_all_skipped(self) -> None:
        self.extend_script("garbage1", "garbage2", "garbage3", BESTPOSA_VALID)

        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertEqual(result.messages_written, 1)
        contents = destination.read_bytes()
        self.assertNotIn(b"garbage", contents)

    def test_clean_start_is_unaffected(self) -> None:
        # No fragment at all -- the very first message already
        # starts cleanly. Must not be skipped.
        self.extend_script(BESTPOSA_VALID, BESTPOSA_VALID)

        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertEqual(result.messages_written, 2)

    def test_only_the_leading_fragment_is_skipped_not_later_unknowns(self) -> None:
        # Once synced, a later UNKNOWN-classified message (e.g. a
        # streaming log without its usual header) must still be
        # written -- the guard only applies before the first real
        # boundary is found, not for the rest of the recording.
        self.extend_script(
            "leading_fragment_no_header",
            BESTPOSA_VALID,
            "201c1e23,4,1,aaaaaaaaaaaaaaaa",
        )

        destination = self.root / "raw.bin"

        result = self.rx.record_raw(destination, duration=0.2)

        self.assertEqual(result.messages_written, 2)
        contents = destination.read_bytes()
        self.assertNotIn(b"leading_fragment", contents)
        self.assertIn(b"201c1e23", contents)


if __name__ == "__main__":
    unittest.main()
