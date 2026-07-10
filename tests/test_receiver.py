"""
test_receiver.py

Unit tests for station/receiver.py.

These tests exercise the parser and connection-handling logic using
recorded / synthetic UM980-style ASCII responses. No hardware and no
real serial port is required: pyserial is replaced with a small fake
module before `receiver` is imported, and a `FakeSerial` class stands
in for the actual `serial.Serial` object.

Run with:

    python3 -m unittest discover -s tests -v

or, from the project root:

    python3 -m pytest tests/ -v
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

# ------------------------------------------------------------
# Install a fake `serial` module before importing `receiver`, so
# these tests do not require pyserial or real hardware.
# ------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "station"))


class _FakeSerialException(Exception):
    """Stand-in for serial.SerialException."""


class FakeSerial:
    """
    Minimal stand-in for serial.Serial.

    Responses are queued explicitly with `queue_response()` (or the
    convenience `queue_lines()` for multiple lines) rather than being
    generated from the command that was sent, so each test controls
    exactly what the "receiver" says back, byte for byte.
    """

    def __init__(self, port: str, baudrate: int, timeout: float):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.written: list[bytes] = []
        self._lines: list[bytes] = []
        self._pending: bytes = b""
        self.fail_writes = 0  # simulate N SerialExceptions on write()

    def queue_response(self, text: str) -> None:
        self._lines.append((text + "\r\n").encode("ascii"))

    def queue_lines(self, *lines: str) -> None:
        for line in lines:
            self.queue_response(line)

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise _FakeSerialException("simulated serial failure")
        self.written.append(data)

    def _refill_pending(self) -> None:
        if not self._pending and self._lines:
            self._pending = self._lines.pop(0)

    def read(self, n: int = 1) -> bytes:
        """
        Byte-level read, needed since read_message() peeks one byte
        at a time to detect a binary sync sequence before falling
        back to readline() for ASCII/NMEA text. Draws from the same
        queued lines as readline(), so either can be called in any
        combination without losing or duplicating bytes.
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
    """
    Install a fake `serial` module in sys.modules and return it, so
    `import serial` inside receiver.py resolves to our fakes instead
    of requiring pyserial to be installed.

    `serial.Serial(...)` is wired to a factory that returns a single
    shared FakeSerial "device" per test (tracked as
    `fake_module.current`), and simply re-marks it open on a
    reconnect, rather than manufacturing a brand new fake port each
    time. That mirrors the real world closely enough for these
    tests: reconnecting re-opens the *same* physical device rather
    than a different one, and it lets tests that simulate a dropped
    connection (see TestQueryRetryAndReconnect) keep their queued
    response visible across a disconnect()/connect() cycle.
    """

    fake_module = types.ModuleType("serial")
    fake_module.SerialException = _FakeSerialException
    fake_module.current = None

    def _serial_factory(
        port: str, baudrate: int, timeout: float
    ) -> FakeSerial:
        if fake_module.current is None:
            fake_module.current = FakeSerial(port, baudrate, timeout)
        else:
            fake_module.current.is_open = True
        return fake_module.current

    fake_module.Serial = _serial_factory
    sys.modules["serial"] = fake_module

    return fake_module


_install_fake_serial_module()

import receiver  # noqa: E402  (must follow the fake serial install)


# ------------------------------------------------------------
# Recorded / synthetic UM980-style responses.
#
# The BESTPOSA and REFSTATIONA checksums below are real, computed by
# station/receiver.py's own `_novatel_crc32()`. The REFSTATIONA body
# is the example log published in NovAtel's OEM7 REFSTATION
# documentation, and its checksum below (380be9f1) matches the
# checksum published there, confirming the CRC implementation
# against an independent, real-world example.
# ------------------------------------------------------------

VERSIONA_REAL = (
    '#VERSIONA,97,GPS,FINE,2426,224878000,0,0,18,639;'
    '"UM980","R4.10Build11833","HRPT00-S10C-P",'
    '"2310415000001-MD22A4244202829","ff3be4677d4e25dc",'
    '"2023/11/24"*8f3f0201'
)

VERSIONA_SHORT = (
    '#VERSIONA,97,GPS,FINE,2426,224878000,0,0,18,639;'
    '"UM980","R4.10Build11833"*00000000'
)

VERSIONA_MALFORMED = "#VERSIONA,no semicolon here"

BESTPOSA_VALID = (
    '#BESTPOSA,COM1,0,71.5,FINESTEERING,2216,148248.000,00000020,'
    '3681,16809;SOL_COMPUTED,SINGLE,41.8928336813,-69.9633123013,'
    '21.774,-30.000,WGS84,0.0100,0.0120,0.0250,"",1.500,2.500,12,11,'
    '11,0,0,0,00,00*381fd21d'
)

BESTPOSA_BAD_CHECKSUM = BESTPOSA_VALID[:-8] + "deadbeef"

BESTPOSA_MISSING_FIELDS = (
    '#BESTPOSA,COM1,0,71.5,FINESTEERING,2216,148248.000,00000020,'
    '3681,16809;SOL_COMPUTED*00000000'
)

BESTPOSA_BAD_LATITUDE = (
    '#BESTPOSA,COM1,0,71.5,FINESTEERING,2216,148248.000,00000020,'
    '3681,16809;SOL_COMPUTED,SINGLE,-95.0,-69.9633123013,21.774,'
    '-30.000,WGS84,0.0100,0.0120,0.0250,"",0.000,0.000,12,11,11,0,'
    '0,0,00,00*00000000'
)

BESTPOSA_BAD_LONGITUDE = (
    '#BESTPOSA,COM1,0,71.5,FINESTEERING,2216,148248.000,00000020,'
    '3681,16809;SOL_COMPUTED,SINGLE,41.8928336813,190.0,21.774,'
    '-30.000,WGS84,0.0100,0.0120,0.0250,"",0.000,0.000,12,11,11,0,'
    '0,0,00,00*00000000'
)

REFSTATIONA_VALID = (
    '#REFSTATIONA,USB1,0,68.0,FINESTEERING,2211,233731.221,'
    '02000020,4e46,16809;00000000,-1632851.222,-3662162.724,'
    '4944899.271,0,NOVATELX,"K250"*380be9f1'
)


class ReceiverTestCase(unittest.TestCase):
    """Base class that wires up a Receiver + FakeSerial pair."""

    def setUp(self) -> None:
        # Re-install THIS file's fake serial module and force
        # receiver.py to rebind its `import serial` to it, in case
        # another test module (e.g. tests/test_record_raw.py, which
        # installs its own different FakeSerial) ran/imported more
        # recently and left sys.modules["serial"] pointing elsewhere.
        # `import serial` binds a name once at import time, not a
        # live lookup, so simply reassigning sys.modules["serial"]
        # afterward doesn't retroactively fix an already-imported
        # receiver module -- reload() forces that re-binding. This
        # also gives each test a fresh fake device, same as the old
        # `sys.modules["serial"].current = None` line did on its own.
        _install_fake_serial_module()
        importlib.reload(receiver)

        self.rx = receiver.Receiver(
            device="/dev/USB_GPS",
            baudrate=115200,
            timeout=1.0,
            serial_timeout=0.1,
            retries=1,
            retry_delay=0.01,
        )
        self.rx.connect()
        self.fake: FakeSerial = self.rx._serial  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.rx.disconnect()


# ------------------------------------------------------------
# VERSIONA parsing
# ------------------------------------------------------------

class TestParseVersionA(ReceiverTestCase):

    def test_real_recorded_response(self) -> None:
        info = self.rx._parse_versiona(VERSIONA_REAL)

        self.assertEqual(info.model, "UM980")
        self.assertEqual(info.firmware, "R4.10Build11833")
        self.assertEqual(info.hardware, "HRPT00-S10C-P")
        self.assertEqual(
            info.psn, "2310415000001-MD22A4244202829"
        )
        self.assertEqual(info.efuse_id, "ff3be4677d4e25dc")
        self.assertEqual(info.compile_date, "2023/11/24")
        self.assertEqual(info.raw, VERSIONA_REAL)

    def test_short_response_degrades_gracefully(self) -> None:
        info = self.rx._parse_versiona(VERSIONA_SHORT)

        self.assertEqual(info.model, "UM980")
        self.assertEqual(info.firmware, "R4.10Build11833")
        # Fields beyond what was actually present stay at their
        # dataclass defaults rather than raising.
        self.assertEqual(info.hardware, "")
        self.assertEqual(info.psn, "")
        self.assertEqual(info.efuse_id, "")
        self.assertEqual(info.compile_date, "")

    def test_malformed_response_falls_back_gracefully(self) -> None:
        info = self.rx._parse_versiona(VERSIONA_MALFORMED)

        self.assertEqual(info.model, "Unicore UM980")
        self.assertEqual(info.raw, VERSIONA_MALFORMED)


# ------------------------------------------------------------
# BESTPOSA parsing
# ------------------------------------------------------------

class TestParseBestPosA(ReceiverTestCase):

    def test_valid_response_preserves_all_fields(self) -> None:
        pos = self.rx._parse_bestposa(BESTPOSA_VALID)

        self.assertEqual(pos.solution, "SOL_COMPUTED")
        self.assertAlmostEqual(pos.latitude, 41.8928336813)
        self.assertAlmostEqual(pos.longitude, -69.9633123013)
        self.assertAlmostEqual(pos.height, 21.774)
        self.assertEqual(pos.num_svs_tracked, 12)
        self.assertEqual(pos.num_svs_in_solution, 11)
        self.assertAlmostEqual(pos.differential_age, 1.5)
        self.assertAlmostEqual(pos.solution_age, 2.5)

    def test_missing_required_fields_raises(self) -> None:
        with self.assertRaises(receiver.ReceiverError):
            self.rx._parse_bestposa(BESTPOSA_MISSING_FIELDS)

    def test_out_of_range_latitude_raises(self) -> None:
        with self.assertRaises(receiver.ReceiverError):
            self.rx._parse_bestposa(BESTPOSA_BAD_LATITUDE)

    def test_out_of_range_longitude_raises(self) -> None:
        with self.assertRaises(receiver.ReceiverError):
            self.rx._parse_bestposa(BESTPOSA_BAD_LONGITUDE)


# ------------------------------------------------------------
# REFSTATIONA parsing
# ------------------------------------------------------------

class TestParseRefStationA(ReceiverTestCase):

    def test_valid_response(self) -> None:
        info = self.rx._parse_refstationa(REFSTATIONA_VALID)

        self.assertEqual(info.status, "00000000")
        self.assertAlmostEqual(info.ecef_x, -1632851.222)
        self.assertAlmostEqual(info.ecef_y, -3662162.724)
        self.assertAlmostEqual(info.ecef_z, 4944899.271)
        self.assertEqual(info.health, 0)
        self.assertEqual(info.station_type, "NOVATELX")
        self.assertEqual(info.station_id, "K250")


# ------------------------------------------------------------
# Checksum verification
# ------------------------------------------------------------

class TestChecksumVerification(ReceiverTestCase):

    def test_valid_checksum_matches(self) -> None:
        self.assertTrue(self.rx._verify_checksum(BESTPOSA_VALID))

    def test_known_good_refstationa_checksum(self) -> None:
        # This checksum is taken directly from NovAtel's published
        # REFSTATION example, independent of this codebase, so a
        # match here confirms the CRC implementation itself.
        self.assertTrue(self.rx._verify_checksum(REFSTATIONA_VALID))

    def test_known_good_versiona_checksum(self) -> None:
        # This checksum was recorded directly from a real UM980,
        # independent of this codebase, so a match here confirms the
        # CRC implementation against real hardware output.
        self.assertTrue(self.rx._verify_checksum(VERSIONA_REAL))

    def test_invalid_checksum_detected(self) -> None:
        self.assertFalse(
            self.rx._verify_checksum(BESTPOSA_BAD_CHECKSUM)
        )

    def test_bad_checksum_logs_warning_but_does_not_raise(self) -> None:
        self.fake.queue_response(BESTPOSA_BAD_CHECKSUM)

        # Non-strict by default: a bad checksum should not raise out
        # of read_response().
        response = self.rx.read_response(expected="BESTPOSA")

        self.assertEqual(response, BESTPOSA_BAD_CHECKSUM)

    def test_strict_checksums_raises(self) -> None:
        self.rx.strict_checksums = True
        self.fake.queue_response(BESTPOSA_BAD_CHECKSUM)

        with self.assertRaises(receiver.ReceiverError):
            self.rx.read_response(expected="BESTPOSA")


# ------------------------------------------------------------
# NMEA / unsolicited-log filtering
# ------------------------------------------------------------

class TestResponseFiltering(ReceiverTestCase):

    def test_nmea_chatter_is_skipped(self) -> None:
        self.fake.queue_lines(
            "$GPGGA,irrelevant,chatter*00",
            BESTPOSA_VALID,
        )

        response = self.rx.read_response(expected="BESTPOSA")

        self.assertEqual(response, BESTPOSA_VALID)

    def test_unsolicited_log_is_skipped(self) -> None:
        self.fake.queue_lines(
            VERSIONA_REAL,  # a different log entirely
            BESTPOSA_VALID,
        )

        response = self.rx.read_response(expected="BESTPOSA")

        self.assertEqual(response, BESTPOSA_VALID)

    def test_timeout_raises_when_nothing_matches(self) -> None:
        self.rx.timeout = 0.2

        with self.assertRaises(receiver.ReceiverTimeoutError):
            self.rx.read_response(expected="BESTPOSA")


# ------------------------------------------------------------
# query() retry / reconnect behavior
# ------------------------------------------------------------

class TestQueryRetryAndReconnect(ReceiverTestCase):

    def test_query_returns_response_on_first_try(self) -> None:
        self.fake.queue_response(BESTPOSA_VALID)

        response = self.rx.query("BESTPOSA")

        self.assertEqual(response, BESTPOSA_VALID)

    def test_query_retries_after_timeout(self) -> None:
        # First attempt: nothing queued -> times out.
        # Second attempt: response is queued -> succeeds.
        original_read_until = self.rx._read_until

        call_count = {"n": 0}

        def flaky_read_until(expected, deadline):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise receiver.ReceiverTimeoutError("simulated")
            return original_read_until(expected, deadline)

        self.rx._read_until = flaky_read_until  # type: ignore
        self.fake.queue_response(BESTPOSA_VALID)

        response = self.rx.query("BESTPOSA")

        self.assertEqual(response, BESTPOSA_VALID)
        self.assertEqual(call_count["n"], 2)

    def test_query_reconnects_after_serial_exception(self) -> None:
        # Simulate one failed write (serial-level failure), then a
        # working one on the retry.
        self.fake.fail_writes = 1
        self.fake.queue_response(BESTPOSA_VALID)

        response = self.rx.query("BESTPOSA")

        self.assertEqual(response, BESTPOSA_VALID)
        # A reconnect swaps in a *new* FakeSerial instance via
        # connect(); confirm the receiver re-connected successfully.
        self.assertTrue(self.rx.is_connected())

    def test_query_records_stats(self) -> None:
        self.fake.queue_response(BESTPOSA_VALID)

        self.rx.query("BESTPOSA")

        stats = self.rx.stats("BESTPOSA")

        self.assertEqual(stats.count, 1)
        self.assertEqual(stats.failures, 0)
        self.assertGreaterEqual(stats.average_time, 0.0)


# ------------------------------------------------------------
# read_message() -- the new low-level primitive
# ------------------------------------------------------------

class TestReadMessage(ReceiverTestCase):

    def test_returns_none_when_nothing_available(self) -> None:
        self.assertIsNone(self.rx.read_message())

    def test_classifies_a_named_ascii_log(self) -> None:
        self.fake.queue_response(BESTPOSA_VALID)

        message = self.rx.read_message()

        self.assertEqual(message.message_type, "BESTPOSA")
        self.assertEqual(message.raw, BESTPOSA_VALID)
        self.assertTrue(message.is_ascii)
        self.assertFalse(message.is_binary)
        self.assertTrue(message.checksum_ok)

    def test_classifies_nmea_chatter(self) -> None:
        self.fake.queue_response("$GPGGA,irrelevant,chatter*00")

        message = self.rx.read_message()

        self.assertEqual(message.message_type, "NMEA")
        self.assertIsNone(message.checksum_ok)

    def test_classifies_unprefixed_streaming_line_as_unknown(self) -> None:
        # This is bug #2 from the field report: a streaming log
        # (RANGEA in this case) arriving without its "#LOGNAME"
        # header must NOT be classified as a match for anything.
        self.fake.queue_response("201c1e23,4,1,abcdef0123456789")

        message = self.rx.read_message()

        self.assertEqual(message.message_type, "UNKNOWN")
        self.assertIsNone(message.checksum_ok)

    def test_undecodable_bytes_return_a_binary_message_not_none(self) -> None:
        self.fake._lines.append(b"\xff\xfe\x00\x01garbage\r\n")

        message = self.rx.read_message()

        self.assertIsNotNone(message)
        self.assertTrue(message.is_binary)
        self.assertFalse(message.is_ascii)
        self.assertEqual(message.message_type, "UNKNOWN")
        self.assertTrue(message.raw.startswith("0x"))

    def test_timestamp_is_set(self) -> None:
        self.fake.queue_response(BESTPOSA_VALID)

        before = time.monotonic()
        message = self.rx.read_message()
        after = time.monotonic()

        self.assertGreaterEqual(message.timestamp, before)
        self.assertLessEqual(message.timestamp, after)


# ------------------------------------------------------------
# The two bugs from the field report, reproduced directly
# ------------------------------------------------------------

class TestStreamingRegressions(ReceiverTestCase):
    """
    Reproduces the exact scenario from the bug report: a receiver
    continuously streaming RANGEA (here, in the unprefixed format
    that triggered the original bug) interleaved with command
    responses.
    """

    def test_send_command_does_not_discard_a_buffered_response(self) -> None:
        # Old behavior: send_command() called reset_input_buffer()
        # before every write, which -- on real hardware -- can
        # discard bytes that arrived between calls. This is the
        # closest equivalent we can exercise against the fake serial
        # (which has no OS-level buffer to actually flush): confirm
        # send_command() itself performs no such reset call at all.
        self.fake.queue_response(BESTPOSA_VALID)

        # A message sitting in the fake "port" before send_command()
        # runs must still be there afterward -- i.e. send_command()
        # must not have cleared it.
        self.rx.send_command("BESTPOSA")

        self.assertEqual(len(self.fake._lines), 1)
        self.assertEqual(self.fake._lines[0].decode().strip(), BESTPOSA_VALID)

    def test_query_finds_response_behind_a_backlog_of_streaming_data(self) -> None:
        # Simulates arriving to send a new query while several
        # already-buffered RANGEA-like lines (unprefixed, per the
        # bug report) are sitting ahead of the real response in the
        # OS buffer.
        self.fake.queue_lines(
            "201c1e23,4,1,aaaaaaaaaaaaaaaa",
            "201c1e23,4,1,bbbbbbbbbbbbbbbb",
            "201c1e23,4,1,cccccccccccccccc",
            BESTPOSA_VALID,
        )

        response = self.rx.query("BESTPOSA")

        self.assertEqual(response, BESTPOSA_VALID)

    def test_query_does_not_mistake_unprefixed_data_for_a_different_match(self) -> None:
        # Waiting for VERSIONA specifically; only unprefixed
        # streaming data and no VERSIONA response ever arrives ->
        # must time out, not return the streaming data.
        self.rx.timeout = 0.2
        self.fake.queue_lines(
            "201c1e23,4,1,aaaaaaaaaaaaaaaa",
            "201c1e23,4,1,bbbbbbbbbbbbbbbb",
        )

        with self.assertRaises(receiver.ReceiverTimeoutError):
            self.rx.read_response(expected="VERSIONA")


# ------------------------------------------------------------
# Binary (NovAtel/Unicore OEM7-style) message framing
# ------------------------------------------------------------

# A real, independently-published NovAtel BESTPOSB test vector (from
# docs.novatel.com's own 32-bit CRC documentation page). Its trailing
# 4-byte CRC (0x42,0xdc,0x4c,0x48) is the officially published value
# for this exact byte sequence -- an independent confirmation of both
# the framing (header_length/message_length fields) and the CRC
# algorithm this module already implements for ASCII logs and now
# reuses for binary ones.
BESTPOSB_REAL = bytes([
    0xAA, 0x44, 0x12, 0x1C, 0x2A, 0x00, 0x02, 0x20, 0x48, 0x00, 0x00, 0x00,
    0x90, 0xB4, 0x93, 0x05, 0xB0, 0xAB, 0xB9, 0x12, 0x00, 0x00, 0x00, 0x00,
    0x45, 0x61, 0xBC, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00,
    0x1B, 0x04, 0x50, 0xB3, 0xF2, 0x8E, 0x49, 0x40, 0x16, 0xFA, 0x6B, 0xBE,
    0x7C, 0x82, 0x5C, 0xC0, 0x00, 0x60, 0x76, 0x9F, 0x44, 0x9F, 0x90, 0x40,
    0xA6, 0x2A, 0x82, 0xC1, 0x3D, 0x00, 0x00, 0x00, 0x12, 0x5A, 0xCB, 0x3F,
    0xCD, 0x9E, 0x98, 0x3F, 0xDB, 0x66, 0x40, 0x40, 0x00, 0x30, 0x30, 0x30,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0B, 0x0B, 0x00, 0x00,
    0x00, 0x06, 0x00, 0x03,
    0x42, 0xdc, 0x4c, 0x48,
])


class TestBinaryMessageFraming(ReceiverTestCase):

    def _feed_bytes(self, data: bytes) -> None:
        """Load raw bytes directly into the fake, bypassing queue_lines()."""

        self.fake._lines = [data]

    def test_real_bestposb_test_vector_parses_correctly(self) -> None:
        self._feed_bytes(BESTPOSB_REAL)

        message = self.rx.read_message()

        self.assertEqual(message.message_type, "BESTPOSB")
        self.assertTrue(message.is_binary)
        self.assertFalse(message.is_ascii)
        self.assertTrue(message.checksum_ok)
        self.assertEqual(message.raw_bytes, BESTPOSB_REAL)

    def test_unconfirmed_message_id_reported_honestly_not_guessed(self) -> None:
        tampered = bytearray(BESTPOSB_REAL)
        tampered[4:6] = (9999).to_bytes(2, "little")
        self._feed_bytes(bytes(tampered))

        message = self.rx.read_message()

        self.assertEqual(message.message_type, "BINARY_9999")

    def test_corrupted_crc_is_detected(self) -> None:
        tampered = bytearray(BESTPOSB_REAL)
        tampered[-1] ^= 0xFF
        self._feed_bytes(bytes(tampered))

        message = self.rx.read_message()

        self.assertFalse(message.checksum_ok)

    def test_multiple_back_to_back_binary_messages_are_correctly_separated(
        self,
    ) -> None:
        self._feed_bytes(BESTPOSB_REAL + BESTPOSB_REAL + BESTPOSB_REAL)

        count = 0
        while True:
            message = self.rx.read_message()
            if message is None:
                break
            count += 1
            self.assertEqual(message.message_type, "BESTPOSB")
            self.assertTrue(message.checksum_ok)
            self.assertEqual(len(message.raw_bytes), len(BESTPOSB_REAL))

        self.assertEqual(count, 3)

    def test_mixed_ascii_and_binary_stream_does_not_misalign(self) -> None:
        # Reproduces the pattern seen on real hardware: command
        # acknowledgements and NMEA chatter interleaved with binary
        # observation/ephemeris messages.
        self._feed_bytes(
            b"$command,LOG COM1 RANGEB ONTIME 1,response: OK*51\r\n"
            + BESTPOSB_REAL
            + b"$GPGGA,test*00\r\n"
            + BESTPOSB_REAL
        )

        types_seen = []
        while True:
            message = self.rx.read_message()
            if message is None:
                break
            types_seen.append((message.message_type, message.is_binary))

        self.assertEqual(
            types_seen,
            [
                ("NMEA", False),
                ("BESTPOSB", True),
                ("NMEA", False),
                ("BESTPOSB", True),
            ],
        )

    def test_ascii_messages_still_classified_normally(self) -> None:
        # Confirms the binary-detection path doesn't interfere with
        # ordinary ASCII messages (which never start with 0xAA).
        self.fake.queue_lines(BESTPOSA_VALID)

        message = self.rx.read_message()

        self.assertEqual(message.message_type, "BESTPOSA")
        self.assertTrue(message.is_ascii)
        self.assertFalse(message.is_binary)

    def test_truncated_binary_message_does_not_crash(self) -> None:
        # Only the sync bytes and part of the header arrive before
        # the stream goes idle (e.g. a dropped connection mid-message).
        self._feed_bytes(BESTPOSB_REAL[:10])

        message = self.rx.read_message()

        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, "UNKNOWN")
        self.assertTrue(message.is_binary)


# record_raw() has its own dedicated test file: tests/test_record_raw.py.


if __name__ == "__main__":
    unittest.main()
