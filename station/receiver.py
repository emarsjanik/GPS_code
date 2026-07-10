"""
receiver.py

USGS GNSS Reference Station
Prototype 1.0

Direct serial communications with the Unicore UM980 GNSS receiver.

This module is intentionally narrow in scope. It is responsible only
for:

    * opening / closing the serial connection to the receiver, and
      automatically reconnecting after a serial-level failure
    * reading and classifying every message the receiver sends,
      whether solicited (a command's response) or unsolicited (a
      streaming log like RANGEA, NMEA chatter, or anything not yet
      recognized) -- see read_message() below
    * sending ASCII command queries and waiting for the matching
      response out of that stream, ignoring/dispatching everything
      else in between
    * verifying the 32-bit CRC that terminates each ASCII log/response
    * parsing VERSIONA, BESTPOSA, and REFSTATIONA responses
    * recording every message's original bytes verbatim, byte-perfect
      (record_raw())
    * tracking basic per-command timing statistics

Satellite-by-satellite tracking data, DOP values, the UM980 binary
log formats, RINEX generation, database logging, multi-threaded
logging, and GNSS-IR processing are all out of scope here and belong
in later modules / versions.

Message routing
----------------
The receiver isn't only a request/response device: once logs like
RANGEA are streaming continuously, unsolicited messages can arrive at
any moment, including in between sending a command and reading its
response. Every read in this class funnels through one primitive,
read_message(), which pulls the next available line off the serial
port and classifies it into a ReceiverMessage (see that dataclass)
without assuming anything about what it is. query() then loops on
read_message() until a message of the type it's waiting for shows up,
letting everything else pass through (logged, and optionally handed
to `on_message`, a caller-supplied callback). record_raw() uses the
exact same primitive to capture every message verbatim, so there is
one serial reader and one parser, not two divergent code paths for
"answering a query" versus "recording the stream."

Classification is conservative by design: only two formats are
currently recognized -- NMEA ("$..." sentences) and the standard
Unicore/NovAtel ASCII log format ("#LOGNAME,...;...*checksum",
classified by LOGNAME). Anything else -- including a streaming log
configured to omit its "#LOGNAME" header -- comes back as message_
type "UNKNOWN" rather than being guessed at or, worse, mistaken for
whatever query() happens to be waiting for. Extending recognition to
a new format only requires changing _parse_message_type(); nothing
else in this class needs to change, since everything reads through
read_message().

Because commands are no longer the only thing arriving on the wire,
send_command() does NOT flush the input buffer before writing (it
used to; see git history). Flushing blindly discarded whatever was
already buffered, which may have included a legitimate message --
even, in principle, the very response being waited for. There is no
separate "purge" step now: query()'s read_message() loop naturally
drains and classifies whatever is already buffered, keeping anything
that matches and dispatching/discarding anything that doesn't,
instead of throwing it away sight unseen.

A note on field layouts: the UM980's ASCII command and log set is
partly modeled on NovAtel's OEM7 receivers, but is not identical.
The VERSIONA and BESTPOSA layouts below have been confirmed directly
against recorded UM980 output, including matching computed CRCs
against checksums the receiver actually sent. The REFSTATIONA layout
is taken from the published NovAtel OEM7 REFSTATION log definition,
since UM980-specific documentation and a recorded example for that
particular log were not available while writing this module; it
should be spot-checked against real REFSTATIONA output the first
time it is used against hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import logging
import time

import serial


# The version of this module's public interface (Receiver's methods
# and the VersionInfo/PositionInfo/ReferenceStationInfo/CommandStats
# dataclasses), as opposed to the station application's own version
# (station/version.py) or the physical receiver's firmware version
# (VersionInfo.firmware). Bump this if the public API changes in a
# backward-incompatible way. Displayed on station.py's dashboard.
API_VERSION = "1.0"

# How long record_raw()'s pre-recording "flush stale data" step is
# allowed to spend draining already-buffered messages before giving
# up and starting the timed recording loop regardless. Deliberately
# short: on a continuously streaming receiver, there may be no gap
# in the stream to wait for, so this is a fixed budget rather than
# "keep going until nothing is left."
_FLUSH_STALE_DATA_BUDGET = 0.25  # seconds

# Message ID -> name, for binary (NovAtel/Unicore OEM7-style)
# messages. Only IDs confirmed against official documentation, or a
# real and independently verifiable test vector, are listed here.
# Framing, byte counts, and checksum verification are all correct
# for *any* binary message regardless of whether its ID appears
# below (see _read_binary_message()); this table only controls
# whether a confirmed human-readable name is reported, or the
# honest, unclaimed "BINARY_<id>" fallback.
_BINARY_MESSAGE_NAMES: dict[int, str] = {
    # Confirmed via Unicore's own command/log reference manual
    # ("GPSEPHEM GPS Ephemeris ... Message ID: 7").
    7: "GPSEPHEMB",
    # Confirmed via a real NovAtel BESTPOSB binary test vector
    # (0xAA,0x44,0x12,0x1C,0x2A,0x00,...): bytes 4-5 = 0x2A,0x00 ->
    # message_id 42, and this module's _novatel_crc32() reproduces
    # that same test vector's published CRC exactly.
    42: "BESTPOSB",
    # Confirmed via Unicore's own command/log reference manual
    # ("BDSEPHEM BDS Ephemeris ... Message ID: 1047").
    1047: "BDSEPHEMB",
}


# The observation + ephemeris log set confirmed, against real UM980
# hardware (firmware R4.10Build11833), to produce a raw file convbin
# can actually build a complete RINEX obs+nav pair from. The ASCII
# ("...A") forms of these same logs are human-readable and useful
# for debugging, but convbin's Unicore/NovAtel decoder was unable to
# construct valid observation epochs from them in testing -- only
# the binary ("...B") forms worked. ONCHANGED (rather than ONCE) so
# a long-running recording keeps picking up real ephemeris updates
# as they occur, not just once at the start.
_DEFAULT_LOGGING_COMMANDS: tuple[str, ...] = (
    "LOG COM1 RANGEB ONTIME 1",
    "LOG COM1 GPSEPHEMB ONCHANGED",
    "LOG COM1 GLOEPHEMERISB ONCHANGED",
    "LOG COM1 BDSEPHEMB ONCHANGED",
    "LOG COM1 GALEPHEMB ONCHANGED",
    "LOG COM1 QZSSEPHEMERISB ONCHANGED",
)


# ----------------------------------------------------------------
# Data Classes
# ----------------------------------------------------------------

@dataclass
class VersionInfo:
    """
    Parsed response to a VERSIONA query.

    Confirmed UM980 VERSIONA responses are a flat, quoted-string
    record in the data portion:

        #VERSIONA,<port>,<sig>,<time_status>,<week>,<ms>,<status>,
        <reserved>,<sw_ver>;
        "<model>","<firmware_version>","<hardware_version>",
        "<psn>","<efuse_id>","<compile_date>"*<checksum>

    for example:

        #VERSIONA,97,GPS,FINE,2426,224878000,0,0,18,639;
        "UM980","R4.10Build11833","HRPT00-S10C-P",
        "2310415000001-MD22A4244202829","ff3be4677d4e25dc",
        "2023/11/24"*8f3f0201

    `raw` always preserves the full, unparsed response.
    """

    model: str = ""
    firmware: str = ""
    hardware: str = ""
    psn: str = ""
    efuse_id: str = ""
    compile_date: str = ""
    raw: str = ""


@dataclass
class PositionInfo:
    """
    Parsed response to a BESTPOSA query.

    UM980 BESTPOSA data fields, in order, are:

        sol_status, pos_type, lat, lon, hgt, undulation, datum,
        lat_stdev, lon_stdev, hgt_stdev, stn_id, diff_age, sol_age,
        num_svs_tracked, num_svs_in_solution, num_svs_l1,
        num_svs_multi, reserved, ext_sol_stat, galileo_beidou_mask,
        gps_glonass_mask

    Fields are extracted by name rather than by fixed position, so
    that a shorter-than-expected response degrades gracefully instead
    of raising an IndexError or silently reading the wrong field.

    Latitude and longitude are range-checked (-90..90, -180..180);
    a response with out-of-range coordinates raises ReceiverError
    rather than producing a silently bogus position.
    """

    solution: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    height: float = 0.0
    undulation: float = 0.0
    datum: str = ""
    latitude_stdev: float = 0.0
    longitude_stdev: float = 0.0
    height_stdev: float = 0.0
    differential_age: float = 0.0
    solution_age: float = 0.0
    num_svs_tracked: int = 0
    num_svs_in_solution: int = 0
    raw: str = ""


@dataclass
class ReferenceStationInfo:
    """
    Parsed response to a REFSTATIONA query.

    Field layout (per the NovAtel OEM7 REFSTATION log definition):

        status, ecef_x, ecef_y, ecef_z, health, station_type,
        station_id
    """

    status: str = ""
    ecef_x: float = 0.0
    ecef_y: float = 0.0
    ecef_z: float = 0.0
    health: int = 0
    station_type: str = ""
    station_id: str = ""
    raw: str = ""


@dataclass
class CommandStats:
    """Running timing statistics for a single command name."""

    count: int = 0
    failures: int = 0
    total_time: float = 0.0
    min_time: float = float("inf")
    max_time: float = 0.0

    @property
    def average_time(self) -> float:
        """Average successful query duration, in seconds."""

        if self.count == 0:
            return 0.0

        return self.total_time / self.count


@dataclass
class RecordingResult:
    """
    The outcome of one record_raw() call.

    `start_time`/`end_time` are wall-clock (UTC) markers, useful for
    archival record-keeping (e.g. naming/indexing a recording by when
    it happened). `duration_actual` and the average_rate_* fields are
    always computed from time.monotonic() measurements taken during
    recording, never from these wall-clock fields, since a wall-clock
    adjustment (NTP, DST, ...) mid-recording could otherwise corrupt
    an elapsed-time calculation.

    A callback-delivered mid-recording snapshot has `successful=False`
    and an `end_time` reflecting the moment of that snapshot, not
    final completion.
    """

    filename: Path
    start_time: datetime
    end_time: datetime
    duration_requested: float
    duration_actual: float
    bytes_written: int
    messages_written: int
    average_rate_bytes: float
    average_rate_messages: float
    logging_enabled: bool
    successful: bool
    receiver_model: str
    receiver_firmware: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReceiverMessage:
    """
    One classified message read from the receiver, whatever it turned
    out to be: the response to a query, an unsolicited streamed
    observation (RANGEA and friends), NMEA chatter, or something not
    yet recognized at all.

    This is the single unit every reading routine in this class
    passes around -- query()/read_response() filter a stream of
    these for one matching message_type; record_raw() writes every
    one of them, unfiltered, exactly as received.

    `raw` is decoded text, stripped of its line terminator, used for
    classification/parsing/logging. `raw_bytes` is the *original,
    unmodified* bytes exactly as read off the wire, terminator
    included -- record_raw() writes this, not `raw`, so archived
    recordings are byte-for-byte faithful rather than a re-encoding
    of a stripped/decoded string.
    """

    raw: str
    message_type: str
    timestamp: float
    is_ascii: bool
    is_binary: bool
    checksum_ok: bool | None = None
    raw_bytes: bytes = b""


# ----------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------

class ReceiverError(Exception):
    """Base exception for receiver communication failures."""


class ReceiverTimeoutError(ReceiverError):
    """Raised when the receiver does not respond within the timeout."""


class ReceiverNotConnectedError(ReceiverError):
    """Raised when a command is issued before connect() succeeds."""


# ----------------------------------------------------------------
# Field layouts used by the private parsers
# ----------------------------------------------------------------

# Order of the comma-separated, quoted fields in a UM980 VERSIONA
# response's data portion. Confirmed against real UM980 output:
#
#   #VERSIONA,97,GPS,FINE,2426,224878000,0,0,18,639;
#   "UM980","R4.10Build11833","HRPT00-S10C-P",
#   "2310415000001-MD22A4244202829","ff3be4677d4e25dc",
#   "2023/11/24"*8f3f0201
#
# This is a flat, single record, not the NovAtel OEM7-style
# "component count + repeated component groups" layout used by some
# other NovAtel/Unicore products.
_VERSIONA_FIELDS = (
    "model",
    "firmware_version",
    "hardware_version",
    "psn",
    "efuse_id",
    "compile_date",
)

# Order of the comma-separated fields in a BESTPOSA response.
_BESTPOSA_FIELDS = (
    "sol_status",
    "pos_type",
    "lat",
    "lon",
    "hgt",
    "undulation",
    "datum",
    "lat_stdev",
    "lon_stdev",
    "hgt_stdev",
    "stn_id",
    "diff_age",
    "sol_age",
    "num_svs_tracked",
    "num_svs_in_solution",
    "num_svs_l1",
    "num_svs_multi",
    "reserved",
    "ext_sol_stat",
    "galileo_beidou_mask",
    "gps_glonass_mask",
)

# Order of the comma-separated fields in a REFSTATIONA response.
_REFSTATIONA_FIELDS = (
    "status",
    "ecef_x",
    "ecef_y",
    "ecef_z",
    "health",
    "station_type",
    "station_id",
)


# ----------------------------------------------------------------
# Receiver
# ----------------------------------------------------------------

@dataclass
class Receiver:
    """
    Serial interface to a Unicore UM980 GNSS receiver.

    Parameters
    ----------
    device:
        Serial device path, e.g. "/dev/USB_GPS".
    baudrate:
        Serial baud rate.
    timeout:
        Overall query timeout, in seconds. This is the total time a
        single query() attempt is allowed to spend waiting for a
        matching response, independent of the low-level serial read
        granularity. Kept as `timeout` (rather than `query_timeout`)
        so existing callers, e.g.:

            cfg = Config()
            rx = Receiver(
                device=cfg.receiver_port,
                baudrate=cfg.receiver_baud,
                timeout=cfg.receiver_timeout,
            )

        continue to work unchanged.
    serial_timeout:
        Low-level timeout, in seconds, passed to the underlying
        pyserial `Serial` object and used for each individual
        `readline()` call. This should be short relative to
        `timeout` so that the overall query deadline can be checked
        responsively rather than blocking for the full duration on a
        single read.
    retries:
        Number of times a query() will be retried if a timeout or
        serial error occurs before giving up.
    retry_delay:
        Seconds to wait between retries.
    verify_checksums:
        If True (the default), the 32-bit CRC on each "#LOG...;...
        *checksum" response is verified. Mismatches are logged as
        warnings unless `strict_checksums` is also set.
    strict_checksums:
        If True, a checksum mismatch raises ReceiverError instead of
        only logging a warning. Off by default, since a single
        corrupted line should not necessarily abort a query that
        will be retried or superseded anyway.
    on_message:
        Optional callback invoked with every ReceiverMessage that
        query()/read_response()/record_raw() read while looking for
        something else -- and, for record_raw(), every message,
        period. Useful for a caller that wants live visibility into
        the full stream (e.g. logging RANGEA observations as they
        arrive) without polling read_message() itself. Exceptions
        raised by this callback are caught and logged, never allowed
        to break the read loop that called it.
    """

    device: str = "/dev/USB_GPS"
    baudrate: int = 115200
    timeout: float = 2.0
    serial_timeout: float = 0.5
    retries: int = 2
    retry_delay: float = 1.0
    verify_checksums: bool = True
    strict_checksums: bool = False
    on_message: Callable[["ReceiverMessage"], None] | None = None

    _serial: serial.Serial | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("station.receiver"),
        init=False,
        repr=False,
        compare=False,
    )
    _stats: dict[str, CommandStats] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    # --------------------------------------------------------
    # Connection management
    # --------------------------------------------------------

    def connect(self) -> None:
        """
        Open the serial connection to the receiver.

        Safe to call more than once; subsequent calls are a no-op
        while already connected.
        """

        if self._serial is not None and self._serial.is_open:
            self._logger.debug("Already connected to %s", self.device)
            return

        self._logger.info(
            "Connecting to %s at %d baud (serial_timeout=%.2fs, "
            "query_timeout=%.2fs)",
            self.device,
            self.baudrate,
            self.serial_timeout,
            self.timeout,
        )

        try:
            self._serial = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                timeout=self.serial_timeout,
            )
        except serial.SerialException as exc:
            raise ReceiverError(
                f"Could not open serial device {self.device}: {exc}"
            ) from exc

        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

        self._logger.info("Connected to %s", self.device)

    def disconnect(self) -> None:
        """Close the serial connection, if open."""

        if self._serial is None:
            return

        self._logger.info("Disconnecting from %s", self.device)

        try:
            self._serial.close()
        finally:
            self._serial = None

    def is_connected(self) -> bool:
        """Return True if the serial connection is open."""

        return self._serial is not None and self._serial.is_open

    def _reconnect(self) -> None:
        """
        Close and reopen the serial connection after a serial-level
        failure (e.g. the USB/serial adapter dropped out).

        Raises
        ------
        ReceiverError
            If reopening the connection also fails. Callers should
            treat this as "still down" and keep retrying / back off,
            rather than treating it as fatal.
        """

        self._logger.warning(
            "Attempting to reconnect to %s after a serial error",
            self.device,
        )

        self.disconnect()
        self.connect()

    def __enter__(self) -> "Receiver":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.disconnect()

    # --------------------------------------------------------
    # Low-level I/O
    # --------------------------------------------------------

    def send_command(self, command: str) -> None:
        """
        Send a single ASCII command to the receiver.

        A trailing carriage return / line feed is appended
        automatically; callers should not include one.

        Deliberately does NOT flush the input buffer first. With a
        log streaming continuously (e.g. RANGEA at 1 Hz), anything
        already buffered when this is called may be a legitimate
        message -- possibly even, depending on timing, the response
        to a command sent moments ago that hasn't been read yet.
        Discarding it sight unseen would be data loss, not cleanup.
        query()'s read_message() loop drains and classifies whatever
        is already buffered instead, keeping what matches and
        dispatching/discarding what doesn't -- see the module
        docstring.

        The output buffer is flushed after writing to ensure the
        command has actually been pushed out to the OS / device
        before this call returns.
        """

        if not self.is_connected():
            raise ReceiverNotConnectedError("Receiver is not connected")

        assert self._serial is not None

        message = command.strip() + "\r\n"

        self._logger.debug("TX: %s", command.strip())

        self._serial.write(message.encode("ascii"))
        self._serial.flush()

    @staticmethod
    def _novatel_crc32(data: bytes) -> int:
        """
        Compute the 32-bit CRC used to terminate NovAtel/Unicore
        ASCII and binary log messages (polynomial 0xEDB88320, no
        initial or final XOR).
        """

        polynomial = 0xEDB88320
        crc = 0

        for byte in data:
            tmp = (crc ^ byte) & 0xFF
            for _ in range(8):
                if tmp & 1:
                    tmp = (tmp >> 1) ^ polynomial
                else:
                    tmp >>= 1
            crc = ((crc >> 8) & 0x00FFFFFF) ^ tmp

        return crc

    def _verify_checksum(self, response: str) -> bool | None:
        """
        Verify the "*XXXXXXXX" checksum on a "#LOG,...;..." response.

        Returns
        -------
        True if the checksum is present and matches, False if present
        and does not match, or None if the response has no header/
        checksum to check (e.g. a plain "OK" reply to a config
        command, or a message type this class doesn't know a
        checksum scheme for -- NMEA and UNKNOWN both fall here; see
        read_message()).
        """

        if not response.startswith("#") or "*" not in response:
            return None

        body, _, checksum_text = response.rpartition("*")
        body = body[1:]  # drop the leading '#'

        try:
            expected = int(checksum_text.strip(), 16)
        except ValueError:
            self._logger.debug(
                "Could not parse checksum in response: %s", response
            )
            return None

        actual = self._novatel_crc32(
            body.encode("ascii", errors="ignore")
        )

        return actual == expected

    @staticmethod
    def _parse_message_type(text: str) -> str:
        """
        Classify one decoded, non-empty line of text.

        Confirmed formats:
            "$..."                          -> "NMEA"
            "#LOGNAME,...;...*checksum"      -> "LOGNAME" (e.g. "VERSIONA")

        Anything else comes back as "UNKNOWN" -- including a
        streaming log whose header omits the leading "#", which is
        NOT treated as an automatic match for whatever query() is
        waiting for. (The previous implementation's equivalent check
        returned "not unexpected" for any non-'#'-prefixed line,
        which is exactly what let an unprefixed RANGEA line get
        mistaken for a VERSIONA response.) Extend this method once an
        unrecognized format is confirmed/documented; nothing else in
        this class needs to change, since every read goes through
        read_message().
        """

        if text.startswith("$"):
            return "NMEA"

        if text.startswith("#"):
            header = text[1:].split(",", 1)[0].split(";", 1)[0]
            if header:
                return header.upper()

        return "UNKNOWN"

    def _dispatch(self, message: "ReceiverMessage") -> None:
        """Invoke on_message(), if set, never letting it raise out of a read loop."""

        if self.on_message is None:
            return

        try:
            self.on_message(message)
        except Exception:
            self._logger.exception("on_message callback raised an exception")

    def read_message(self) -> "ReceiverMessage | None":
        """
        Make one bounded attempt (governed by `serial_timeout`) to
        read and classify the next message from the receiver.

        Returns None if nothing arrived within that attempt -- the
        normal outcome of a single call during a quiet moment in the
        stream, not an error. Callers that need to keep trying until
        a deadline (query(), read_response()) or indefinitely
        (record_raw()) loop around this themselves; this method never
        blocks longer than one `serial_timeout`-bounded read.

        This is the single low-level read primitive: every other
        reading routine in this class -- query(), read_response(),
        record_raw() -- is built on top of it, so there is exactly
        one place that turns raw bytes into a classified message.

        Binary (NovAtel/Unicore OEM7-style) messages are properly
        framed, not guessed at: the leading 0xAA 0x44 0x12 sync
        sequence, the header's own `header_length` and
        `message_length` fields, and the trailing 4-byte CRC are all
        read exactly, so a binary message's boundary is exact even
        though it isn't newline-terminated -- confirmed against real
        UM980 output (RANGEB/xxxEPHEMB). `message_type` is a
        confirmed name (e.g. "GPSEPHEMB") only for message IDs this
        module has directly verified against official documentation
        or a real, independently-checked test vector (see
        `_BINARY_MESSAGE_NAMES`); anything else is reported as
        "BINARY_<id>" rather than guessed at, so a name is never
        claimed without real confirmation. Either way, `checksum_ok`
        and `raw_bytes` are always accurate, since those depend only
        on the framing above, not on knowing the message's name.

        Any other undecodable bytes (not starting with the binary
        sync sequence, but still not valid ASCII -- e.g. mid-stream
        corruption) are still returned, as an is_binary=True message
        whose `raw` is a hex dump, rather than silently dropped.

        Raises
        ------
        ReceiverNotConnectedError
            If the serial connection is not open.
        """

        if not self.is_connected():
            raise ReceiverNotConnectedError("Receiver is not connected")

        assert self._serial is not None

        first_byte = self._serial.read(1)

        if not first_byte:
            return None

        timestamp = time.monotonic()

        if first_byte == b"\xaa":
            return self._read_binary_message(timestamp)

        # ASCII/NMEA/unknown text path: reconstruct the full line,
        # since `first_byte` was already consumed above in order to
        # check for the binary sync sequence.
        line = first_byte + self._serial.readline()

        try:
            text = line.decode("ascii").strip()
        except UnicodeDecodeError:
            self._logger.debug("RX: <undecodable bytes, %d byte(s)>", len(line))
            return ReceiverMessage(
                raw="0x" + line.hex(),
                message_type="UNKNOWN",
                timestamp=timestamp,
                is_ascii=False,
                is_binary=True,
                checksum_ok=None,
                raw_bytes=line,
            )

        if not text:
            return None

        self._logger.debug("RX (raw): %s", text)

        message_type = self._parse_message_type(text)

        checksum_ok = (
            self._verify_checksum(text)
            if message_type not in ("NMEA", "UNKNOWN")
            else None
        )

        return ReceiverMessage(
            raw=text,
            message_type=message_type,
            timestamp=timestamp,
            is_ascii=True,
            is_binary=False,
            checksum_ok=checksum_ok,
            raw_bytes=line,
        )

    def _read_binary_message(self, timestamp: float) -> "ReceiverMessage":
        """
        Parse one NovAtel/Unicore OEM7-style binary message, having
        already consumed its first sync byte (0xAA).

        Binary message layout (confirmed against NovAtel's published
        OEM7 binary header structure, and cross-checked against a
        real, independently-published test vector whose CRC this
        module's `_novatel_crc32()` reproduces exactly):

            byte 0-2   sync bytes: 0xAA 0x44 0x12
            byte 3     header_length (total header size, in bytes,
                       counted from byte 0 -- typically 28)
            byte 4-5   message_id (uint16, little-endian)
            byte 6     message_type flags (format/response bits)
            byte 7     port_address
            byte 8-9   message_length (uint16, little-endian --
                       length of the payload *only*, excluding the
                       header and the trailing CRC)
            byte 10.. remaining header fields (sequence, idle time,
                       time status, week, ms, receiver status,
                       reserved, receiver SW version)

        followed by `message_length` bytes of payload, then a 4-byte
        CRC (the same 32-bit CRC used by ASCII logs, computed here
        over the header + payload).

        Reads exactly `header_length` + `message_length` + 4 bytes
        total (after the initial sync byte), so the message boundary
        is always exact -- never guessed at via a stray byte that
        happens to look like a line terminator inside binary payload
        data, which is what naive newline-based reading would do.
        """

        assert self._serial is not None

        sync_rest = self._serial.read(2)

        if sync_rest != b"\x44\x12":
            # Not actually a binary sync after all (corruption, or
            # an extremely unlikely coincidental 0xAA at the start
            # of a read -- ASCII/NMEA logs always start with a
            # printable '#' or '$', never 0xAA, so this should be
            # rare). Return what was consumed rather than losing it.
            raw_bytes = b"\xaa" + sync_rest
            return ReceiverMessage(
                raw="0x" + raw_bytes.hex(),
                message_type="UNKNOWN",
                timestamp=timestamp,
                is_ascii=False,
                is_binary=True,
                checksum_ok=None,
                raw_bytes=raw_bytes,
            )

        header_length_byte = self._serial.read(1)

        if not header_length_byte:
            raw_bytes = b"\xaa\x44\x12"
            return ReceiverMessage(
                raw="0x" + raw_bytes.hex(),
                message_type="UNKNOWN",
                timestamp=timestamp,
                is_ascii=False,
                is_binary=True,
                checksum_ok=None,
                raw_bytes=raw_bytes,
            )

        header_length = header_length_byte[0]

        # The 3 sync bytes + this length byte are already 4 of
        # header_length; read however many more complete it.
        remaining_header_bytes = max(header_length - 4, 0)
        rest_of_header = self._serial.read(remaining_header_bytes)

        header = b"\xaa\x44\x12" + header_length_byte + rest_of_header

        if len(rest_of_header) < remaining_header_bytes:
            self._logger.debug(
                "RX (binary): header read timed out (%d/%d bytes)",
                len(rest_of_header),
                remaining_header_bytes,
            )
            return ReceiverMessage(
                raw="0x" + header.hex(),
                message_type="UNKNOWN",
                timestamp=timestamp,
                is_ascii=False,
                is_binary=True,
                checksum_ok=None,
                raw_bytes=header,
            )

        message_id = int.from_bytes(header[4:6], "little")
        message_length = int.from_bytes(header[8:10], "little")

        payload = self._serial.read(message_length)
        crc_bytes = self._serial.read(4)

        raw_bytes = header + payload + crc_bytes

        checksum_ok: bool | None = None

        if len(payload) == message_length and len(crc_bytes) == 4:
            expected_crc = int.from_bytes(crc_bytes, "little")
            actual_crc = self._novatel_crc32(header + payload)
            checksum_ok = actual_crc == expected_crc
        else:
            self._logger.debug(
                "RX (binary): payload/CRC read timed out "
                "(payload %d/%d bytes, crc %d/4 bytes)",
                len(payload),
                message_length,
                len(crc_bytes),
            )

        message_type = _BINARY_MESSAGE_NAMES.get(
            message_id, f"BINARY_{message_id}"
        )

        self._logger.debug(
            "RX (binary): id=%d type=%s length=%d checksum_ok=%s",
            message_id,
            message_type,
            len(raw_bytes),
            checksum_ok,
        )

        return ReceiverMessage(
            raw=f"<binary {message_type} ({len(raw_bytes)} bytes)>",
            message_type=message_type,
            timestamp=timestamp,
            is_ascii=False,
            is_binary=True,
            checksum_ok=checksum_ok,
            raw_bytes=raw_bytes,
        )

    def _read_until(self, expected: str, deadline: float) -> "ReceiverMessage":
        """
        Loop on read_message() until a message of type `expected`
        arrives, or `deadline` (a time.monotonic() value) passes.

        Every non-matching message is logged and dispatched via
        on_message(), exactly like a matching one, before the loop
        continues -- nothing read is ever silently discarded.

        Raises
        ------
        ReceiverNotConnectedError
            If the serial connection is not open.
        ReceiverTimeoutError
            If no matching message arrives before `deadline`.
        ReceiverError
            If `strict_checksums` is set and the matching message's
            checksum does not match its content.
        """

        while True:

            if time.monotonic() > deadline:
                raise ReceiverTimeoutError(
                    f"No response to {expected!r} within {self.timeout}s"
                )

            message = self.read_message()

            if message is None:
                continue

            if message.message_type.upper() != expected.upper():
                self._logger.info(
                    "Ignoring unsolicited/unmatched message while "
                    "waiting for %s: %s",
                    expected,
                    message.raw,
                )
                self._dispatch(message)
                continue

            if self.verify_checksums and message.checksum_ok is False:

                note = f"Checksum mismatch in response: {message.raw}"

                if self.strict_checksums:
                    raise ReceiverError(note)

                self._logger.warning(note)

            elif message.checksum_ok is True:
                self._logger.debug("Checksum OK: %s", message.raw)

            self._dispatch(message)

            return message

    def read_response(self, expected: str | None = None) -> str:
        """
        Read messages from the receiver until one matching `expected`
        is found, and return its raw text.

        Kept for compatibility with existing callers; read_message()
        is the primitive this (and query()) are now built on. If
        `expected` is None, waits for the first message of any type
        other than NMEA/UNKNOWN (i.e. the first recognized "#LOGNAME"
        response, whatever its name).

        Raises
        ------
        ReceiverNotConnectedError
            If the serial connection is not open.
        ReceiverTimeoutError
            If no matching response is read within self.timeout.
        ReceiverError
            If `strict_checksums` is set and the response's checksum
            does not match its content.
        """

        if not self.is_connected():
            raise ReceiverNotConnectedError("Receiver is not connected")

        deadline = time.monotonic() + self.timeout

        if expected is not None:
            return self._read_until(expected, deadline).raw

        while True:
            if time.monotonic() > deadline:
                raise ReceiverTimeoutError(
                    f"No response within {self.timeout}s"
                )

            message = self.read_message()

            if message is None:
                continue

            if message.message_type in ("NMEA", "UNKNOWN"):
                self._dispatch(message)
                continue

            self._dispatch(message)

            return message.raw

    def query(self, command: str, expected: str | None = None) -> str:
        """
        Send `command` and return the matching response line,
        retrying on timeout or serial error according to
        self.retries / self.retry_delay.

        If a `serial.SerialException` is raised while sending the
        command or reading the response, the connection is
        automatically closed and reopened (see `_reconnect()`) before
        the next attempt.

        Parameters
        ----------
        command:
            The command to send, e.g. "VERSIONA".
        expected:
            The log name to wait for in the response. Defaults to
            `command` itself, which is correct for the simple
            query-style commands used by this module.
        """

        if expected is None:
            expected = command.strip()

        last_error: Exception | None = None

        for attempt in range(1, self.retries + 2):

            attempt_start = time.monotonic()

            try:
                self.send_command(command)
                message = self._read_until(
                    expected, deadline=attempt_start + self.timeout
                )

            except (ReceiverTimeoutError, serial.SerialException) as exc:

                last_error = exc

                self._record_stat(
                    command,
                    time.monotonic() - attempt_start,
                    success=False,
                )

                self._logger.warning(
                    "Query %r failed on attempt %d/%d: %s",
                    command,
                    attempt,
                    self.retries + 1,
                    exc,
                )

                if isinstance(exc, serial.SerialException):
                    try:
                        self._reconnect()
                    except ReceiverError as reconnect_exc:
                        self._logger.error(
                            "Reconnect to %s failed: %s",
                            self.device,
                            reconnect_exc,
                        )

                if attempt <= self.retries:
                    time.sleep(self.retry_delay)

                continue

            self._record_stat(
                command, time.monotonic() - attempt_start, success=True
            )

            return message.raw

        assert last_error is not None
        raise ReceiverError(
            f"Query {command!r} failed after {self.retries + 1} "
            f"attempts: {last_error}"
        ) from last_error

    def record_raw(
        self,
        filename: str | Path,
        duration: float,
        *,
        binary: bool = True,
        append: bool = False,
        create_directory: bool = True,
        enable_logging: bool = False,
        logging_command: str | list[str] | tuple[str, ...] = _DEFAULT_LOGGING_COMMANDS,
        stop_logging: bool = False,
        progress_callback: "Callable[[RecordingResult], None] | None" = None,
    ) -> "RecordingResult":
        """
        Records the exact byte stream received from the receiver.

        No parsing. No modification. No filtering. Produces an
        archival-quality receiver recording suitable for:

            * RINEX conversion
            * Replay
            * Debugging
            * Long-term archive
            * Product regeneration

        Every message is read through read_message() -- the same
        single parser query() uses -- and its *original, unmodified*
        bytes (ReceiverMessage.raw_bytes, not the decoded/stripped
        `.raw` text used for classification) are appended to
        `filename` exactly as received. The write path never
        branches on message type, so this works unchanged whether
        the stream is ASCII, binary, or a mix of both.

        Connection ownership
        ---------------------
        record_raw() owns this Receiver's serial connection for the
        entire requested duration. Do not call query()/version()/
        best_position()/etc. on the same Receiver from another
        thread while a recording is in progress; there is no locking
        here to prevent it, and doing so will corrupt both
        operations' reads.

        For the same reason, record_raw() never disconnects,
        reconnects, or reopens the serial connection during
        recording. A serial.SerialException raised mid-recording
        propagates to the caller (after the output file is safely
        closed -- see "Exception safety" below) rather than being
        retried here; reconnecting, if wanted, is left to the
        caller, after this call returns or raises.

        Parameters
        ----------
        filename:
            Output path (str or Path). Expanded (~) and resolved to
            an absolute path before use.
        duration:
            Seconds to record for. Measured with time.monotonic(),
            never datetime.now(), so a wall-clock adjustment mid-
            recording can't corrupt the elapsed-time measurement.
        binary:
            Reserved for future use. The output file is always
            opened in binary mode ("wb"/"ab") regardless of this
            flag's value -- record_raw() never writes decoded/text-
            mode content, even while ASCII logs are flowing, so a
            future binary or mixed-format stream needs no code
            changes here.
        append:
            If False (the default), overwrite `filename` ("wb"). If
            True, append to it ("ab").
        create_directory:
            If True (the default), create `filename`'s parent
            directory (and any missing parents) if it doesn't
            already exist.
        enable_logging:
            If True, send `logging_command` (all of them, if it's a
            list) before recording starts, asking the receiver to
            begin streaming whatever logs this recording is meant to
            capture.
        logging_command:
            A single command string, or a list/tuple of commands, to
            send when `enable_logging` is True. Defaults to the
            *binary* observation + ephemeris log set confirmed
            against real UM980 hardware to actually produce a
            convertible RINEX obs+nav pair (the ASCII "...A" forms
            of these same logs are readable for debugging, but
            convbin's Unicore/NovAtel decoder could not build valid
            observation epochs from them in testing):

                LOG COM1 RANGEB ONTIME 1
                LOG COM1 GPSEPHEMB ONCHANGED
                LOG COM1 GLOEPHEMERISB ONCHANGED
                LOG COM1 BDSEPHEMB ONCHANGED
                LOG COM1 GALEPHEMB ONCHANGED
                LOG COM1 QZSSEPHEMERISB ONCHANGED

            All commands are sent back-to-back with no pause and no
            reads in between, then recording begins immediately.
            Pausing here (even briefly) was confirmed, against real
            hardware, to silently lose data: with RANGEB/NMEA already
            streaming, a multi-second gap with nothing draining the
            serial port let the OS read buffer fill and drop data
            before the recording loop ever started reading it.
        stop_logging:
            If True, send "UNLOGALL COM1" once recording finishes
            normally (not sent if an exception aborts recording
            early; see "Exception safety").
        progress_callback:
            Optional callable invoked roughly once per second while
            recording, with a RecordingResult snapshot of progress so
            far (`successful=False`; `end_time` reflects the moment
            of that snapshot, not final completion). Exceptions it
            raises are caught and logged, never allowed to interrupt
            recording.

        Exception safety
        -----------------
        The output file is opened as a context manager, so it is
        always closed -- even if a KeyboardInterrupt, a
        serial.SerialException, or any other exception propagates
        out of the read loop.

        Raises
        ------
        ReceiverNotConnectedError
            If the serial connection is not open.
        FileNotFoundError / OSError
            If `filename`'s directory doesn't exist and
            create_directory is False, or the file otherwise can't
            be opened.
        ReceiverError
            If the recording completes its full duration but this
            session captured zero bytes, or the resulting file is
            missing or empty.
        serial.SerialException
            If a serial-level error occurs during recording (not
            retried here -- see "Connection ownership").
        """

        if not self.is_connected():
            raise ReceiverNotConnectedError("Receiver is not connected")

        path = Path(filename).expanduser().resolve()

        if create_directory:
            path.parent.mkdir(parents=True, exist_ok=True)

        self._logger.info(
            "Recording started: %s (duration=%.1fs)", path, duration
        )
        self._logger.info("Output file: %s", path)

        # Best-effort receiver identification for the archival
        # record. A failure here is a warning, not fatal: the
        # recording itself doesn't depend on knowing the model/
        # firmware, and must not be blocked by it.
        receiver_model = ""
        receiver_firmware = ""
        warnings: list[str] = []

        try:
            version_info = self.version()
            receiver_model = version_info.model
            receiver_firmware = version_info.firmware
        except ReceiverError as exc:
            note = f"Could not identify receiver before recording: {exc}"
            self._logger.warning(note)
            warnings.append(note)

        # Flush stale data: drain whatever is already buffered from
        # *before* this call (e.g. leftover output from a previous
        # session's logging configuration), through the same
        # read_message() primitive as everything else -- never a
        # blind reset_input_buffer() call.
        #
        # Bounded by a short, fixed time budget rather than "until
        # nothing is available": on a continuously streaming
        # receiver (RANGEB and friends, with no gaps in the stream),
        # there may never be a moment where read_message() returns
        # None, so waiting for one would hang here forever. This
        # only needs to clear pre-existing backlog, not the live
        # stream itself.
        #
        # Deliberately done BEFORE enabling logging below, not
        # after: if it ran after, it could just as easily consume
        # the first real messages the newly-enabled logs produce --
        # confirmed against real hardware, where a "flush stale
        # data" step placed after a fresh LOG command discarded the
        # very ephemeris records the recording was trying to
        # capture.
        flush_deadline = time.monotonic() + _FLUSH_STALE_DATA_BUDGET

        while time.monotonic() < flush_deadline:
            if self.read_message() is None:
                break

        if enable_logging:
            commands = (
                [logging_command]
                if isinstance(logging_command, str)
                else list(logging_command)
            )

            # Sent back-to-back with no pause and no reads in
            # between: confirmed against real hardware that pausing
            # here (even briefly, to "let the receiver catch up")
            # lets NMEA/observation chatter already flowing fill the
            # OS read buffer unread, silently dropping data by the
            # time the recording loop below finally starts reading.
            # Firing all commands in a few milliseconds and then
            # immediately proceeding to read avoids that entirely.
            for command in commands:
                self.send_command(command)

            self._logger.info("Logging enabled: %s", commands)

        mode = "ab" if append else "wb"

        start_time = datetime.now(timezone.utc)
        start_monotonic = time.monotonic()

        bytes_written = 0
        messages_written = 0
        checksum_failures = 0
        last_progress_at = start_monotonic
        errors: list[str] = []
        synced = False

        with open(path, mode) as handle:

            while True:

                elapsed = time.monotonic() - start_monotonic

                if elapsed >= duration:
                    break

                message = self.read_message()

                if message is not None:

                    # Never start writing mid-message. A fresh
                    # connection (or one that just had logging
                    # (re)configured) can have its very first
                    # readline() land partway through a message
                    # already in flight, rather than at a clean
                    # boundary -- confirmed against real hardware,
                    # where a recording's first bytes were a bare
                    # data fragment with no "#"/"$" header at all.
                    # ASCII text messages are only trustworthy once
                    # one genuinely starts with a recognized header;
                    # binary messages are self-synchronizing (framed
                    # by their own sync-byte sequence) and don't need
                    # this check.
                    if not synced and message.is_ascii and not (
                        message.raw.startswith("#") or message.raw.startswith("$")
                    ):
                        continue

                    synced = True

                    handle.write(message.raw_bytes)

                    bytes_written += len(message.raw_bytes)
                    messages_written += 1

                    if message.checksum_ok is False:
                        checksum_failures += 1

                    self._dispatch(message)

                # Checked every iteration -- idle or not -- so a
                # quiet stretch in the stream doesn't also silence
                # progress reporting: real time is still passing
                # even when no message has arrived recently.
                now = time.monotonic()

                if (
                    progress_callback is not None
                    and now - last_progress_at >= 1.0
                ):
                    elapsed_now = now - start_monotonic

                    self._logger.info(
                        "Progress: %d byte(s), %d message(s), "
                        "%.1f/%.1f sec",
                        bytes_written,
                        messages_written,
                        elapsed_now,
                        duration,
                    )

                    snapshot = RecordingResult(
                        filename=path,
                        start_time=start_time,
                        end_time=datetime.now(timezone.utc),
                        duration_requested=duration,
                        duration_actual=elapsed_now,
                        bytes_written=bytes_written,
                        messages_written=messages_written,
                        average_rate_bytes=(
                            bytes_written / elapsed_now
                            if elapsed_now > 0
                            else 0.0
                        ),
                        average_rate_messages=(
                            messages_written / elapsed_now
                            if elapsed_now > 0
                            else 0.0
                        ),
                        logging_enabled=enable_logging,
                        successful=False,
                        receiver_model=receiver_model,
                        receiver_firmware=receiver_firmware,
                        errors=list(errors),
                        warnings=list(warnings),
                    )

                    try:
                        progress_callback(snapshot)
                    except Exception:
                        self._logger.exception(
                            "progress_callback raised an exception"
                        )

                    last_progress_at = now

            handle.flush()

        if stop_logging:
            self.send_command("UNLOGALL COM1")

        duration_actual = time.monotonic() - start_monotonic
        end_time = datetime.now(timezone.utc)

        if checksum_failures:
            warnings.append(
                f"{checksum_failures} message(s) had checksum mismatches"
            )

        # Verification, immediately after closing: this session must
        # have captured something, and the file must actually exist
        # and be non-empty (covers both overwrite and append modes).
        if bytes_written == 0 or not path.exists() or path.stat().st_size == 0:
            note = (
                f"Recording captured zero bytes (file={path}, "
                f"bytes_written_this_session={bytes_written})"
            )
            self._logger.error(note)
            raise ReceiverError(note)

        average_rate_bytes = (
            bytes_written / duration_actual if duration_actual > 0 else 0.0
        )
        average_rate_messages = (
            messages_written / duration_actual if duration_actual > 0 else 0.0
        )

        self._logger.info("Finished: %s", path)
        self._logger.info(
            "Statistics: %.1f bytes/sec, %.1f messages/sec, %d byte(s), "
            "%d message(s)",
            average_rate_bytes,
            average_rate_messages,
            bytes_written,
            messages_written,
        )

        return RecordingResult(
            filename=path,
            start_time=start_time,
            end_time=end_time,
            duration_requested=duration,
            duration_actual=duration_actual,
            bytes_written=bytes_written,
            messages_written=messages_written,
            average_rate_bytes=average_rate_bytes,
            average_rate_messages=average_rate_messages,
            logging_enabled=enable_logging,
            successful=True,
            receiver_model=receiver_model,
            receiver_firmware=receiver_firmware,
            errors=errors,
            warnings=warnings,
        )

    # --------------------------------------------------------
    # Command timing statistics
    # --------------------------------------------------------

    def _record_stat(
        self, command: str, elapsed: float, success: bool
    ) -> None:
        """Update the running CommandStats entry for `command`."""

        stats = self._stats.setdefault(command, CommandStats())

        if success:
            stats.count += 1
            stats.total_time += elapsed
            stats.min_time = min(stats.min_time, elapsed)
            stats.max_time = max(stats.max_time, elapsed)
        else:
            stats.failures += 1

    def stats(
        self, command: str | None = None
    ) -> dict[str, CommandStats] | CommandStats:
        """
        Return command timing statistics.

        With no argument, returns a dict of {command: CommandStats}
        for every command that has been queried. With `command`
        given, returns just that command's CommandStats (a fresh,
        all-zero instance if it has never been queried).
        """

        if command is not None:
            return self._stats.get(command, CommandStats())

        return dict(self._stats)

    def log_stats(self) -> None:
        """Log a one-line timing summary for every queried command."""

        for name, stats in self._stats.items():

            min_time = 0.0 if stats.count == 0 else stats.min_time

            self._logger.info(
                "%s: %d ok, %d failed, avg %.3fs, min %.3fs, "
                "max %.3fs",
                name,
                stats.count,
                stats.failures,
                stats.average_time,
                min_time,
                stats.max_time,
            )

    # --------------------------------------------------------
    # Private parsers
    # --------------------------------------------------------

    @staticmethod
    def _split_header_and_data(response: str) -> tuple[str, str]:
        """
        Split a "#LOGNAME,...;field,field,...*checksum" response into
        its header and data portions. Any trailing "*checksum" is
        stripped from the data portion.

        Raises
        ------
        ReceiverError
            If the response does not contain the "field;data"
            separator.
        """

        if ";" not in response:
            raise ReceiverError(
                f"Response is missing ';' separator: {response!r}"
            )

        header, data = response.split(";", 1)

        if "*" in data:
            data = data.rsplit("*", 1)[0]

        return header, data

    @classmethod
    def _parse_named_fields(
        cls, data: str, names: tuple[str, ...]
    ) -> dict[str, str]:
        """
        Split a comma-separated data string and zip it against a
        tuple of field names, returning a dict.

        Only as many fields as are actually present are included, so
        a short response yields a partial dict rather than raising
        or misaligning later fields. Extra fields beyond `names` are
        ignored.
        """

        values = data.split(",")

        return dict(zip(names, values))

    def _parse_versiona(self, response: str) -> VersionInfo:
        """
        Parse a VERSIONA response into a VersionInfo.

        The data portion is a flat set of quoted fields (model,
        firmware version, hardware version, PSN, efuse ID, compile
        date) matched by position -- see VersionInfo's docstring for
        a real example. If the response is missing its header/data
        separator or has no data at all, a VersionInfo with the raw
        response preserved (and a logged warning) is returned rather
        than raising, since the receiver model and firmware string
        are informational and should not block startup.
        """

        try:
            _, data = self._split_header_and_data(response)
        except ReceiverError as exc:
            self._logger.warning("Could not parse VERSIONA: %s", exc)
            return VersionInfo(model="Unicore UM980", raw=response)

        values = [value.strip().strip('"') for value in data.split(",")]

        if not values or not values[0]:
            self._logger.warning(
                "VERSIONA response had no data fields: %s", response
            )
            return VersionInfo(model="Unicore UM980", raw=response)

        if len(values) < len(_VERSIONA_FIELDS):
            self._logger.debug(
                "VERSIONA response had only %d of %d expected "
                "fields: %s",
                len(values),
                len(_VERSIONA_FIELDS),
                response,
            )

        parsed = dict(zip(_VERSIONA_FIELDS, values))

        info = VersionInfo(
            model=parsed.get("model") or "Unicore UM980",
            firmware=parsed.get("firmware_version", ""),
            hardware=parsed.get("hardware_version", ""),
            psn=parsed.get("psn", ""),
            efuse_id=parsed.get("efuse_id", ""),
            compile_date=parsed.get("compile_date", ""),
            raw=response,
        )

        self._logger.debug("Parsed VERSIONA: %s", info)

        return info

    def _parse_bestposa(self, response: str) -> PositionInfo:
        """
        Parse a BESTPOSA response into a PositionInfo.

        Raises
        ------
        ReceiverError
            If the response cannot be split into header/data, if the
            required fields (lat, lon, hgt) are missing or not
            parseable as floats, or if latitude/longitude fall
            outside their valid ranges (-90..90 / -180..180).
        """

        _, data = self._split_header_and_data(response)

        parsed = self._parse_named_fields(data, _BESTPOSA_FIELDS)

        required = ("lat", "lon", "hgt")
        missing = [name for name in required if name not in parsed]

        if missing:
            raise ReceiverError(
                f"BESTPOSA response is missing required field(s) "
                f"{missing}: {response!r}"
            )

        def _to_float(name: str) -> float:
            try:
                return float(parsed[name])
            except ValueError as exc:
                raise ReceiverError(
                    f"BESTPOSA field {name!r}={parsed[name]!r} is "
                    f"not a valid number: {response!r}"
                ) from exc

        def _to_float_optional(name: str) -> float:
            value = parsed.get(name, "")
            if not value:
                return 0.0
            try:
                return float(value)
            except ValueError:
                self._logger.debug(
                    "Ignoring unparseable optional field %s=%r",
                    name,
                    value,
                )
                return 0.0

        def _to_int_optional(name: str) -> int:
            value = parsed.get(name, "")
            if not value:
                return 0
            try:
                return int(value)
            except ValueError:
                self._logger.debug(
                    "Ignoring unparseable optional field %s=%r",
                    name,
                    value,
                )
                return 0

        latitude = _to_float("lat")
        longitude = _to_float("lon")

        if not (-90.0 <= latitude <= 90.0):
            raise ReceiverError(
                f"BESTPOSA latitude {latitude} is out of range "
                f"(-90..90): {response!r}"
            )

        if not (-180.0 <= longitude <= 180.0):
            raise ReceiverError(
                f"BESTPOSA longitude {longitude} is out of range "
                f"(-180..180): {response!r}"
            )

        pos = PositionInfo(
            solution=parsed.get("sol_status", ""),
            latitude=latitude,
            longitude=longitude,
            height=_to_float("hgt"),
            undulation=_to_float_optional("undulation"),
            datum=parsed.get("datum", ""),
            latitude_stdev=_to_float_optional("lat_stdev"),
            longitude_stdev=_to_float_optional("lon_stdev"),
            height_stdev=_to_float_optional("hgt_stdev"),
            differential_age=_to_float_optional("diff_age"),
            solution_age=_to_float_optional("sol_age"),
            num_svs_tracked=_to_int_optional("num_svs_tracked"),
            num_svs_in_solution=_to_int_optional(
                "num_svs_in_solution"
            ),
            raw=response,
        )

        self._logger.debug("Parsed BESTPOSA: %s", pos)

        return pos

    def _parse_refstationa(self, response: str) -> ReferenceStationInfo:
        """
        Parse a REFSTATIONA response into a ReferenceStationInfo.

        See the module docstring for a caveat on this log's field
        layout.

        Raises
        ------
        ReceiverError
            If the response cannot be split into header/data.
        """

        _, data = self._split_header_and_data(response)

        parsed = self._parse_named_fields(data, _REFSTATIONA_FIELDS)

        def _to_float(name: str) -> float:
            try:
                return float(parsed.get(name, "0"))
            except ValueError:
                self._logger.debug(
                    "Ignoring unparseable REFSTATIONA field %s=%r",
                    name,
                    parsed.get(name),
                )
                return 0.0

        def _to_int(name: str) -> int:
            try:
                return int(parsed.get(name, "0"))
            except ValueError:
                self._logger.debug(
                    "Ignoring unparseable REFSTATIONA field %s=%r",
                    name,
                    parsed.get(name),
                )
                return 0

        info = ReferenceStationInfo(
            status=parsed.get("status", ""),
            ecef_x=_to_float("ecef_x"),
            ecef_y=_to_float("ecef_y"),
            ecef_z=_to_float("ecef_z"),
            health=_to_int("health"),
            station_type=parsed.get("station_type", ""),
            station_id=parsed.get("station_id", "").strip('"'),
            raw=response,
        )

        self._logger.debug("Parsed REFSTATIONA: %s", info)

        return info

    # --------------------------------------------------------
    # Public parsed queries
    # --------------------------------------------------------

    def version(self) -> VersionInfo:
        """Query the receiver's VERSIONA log and return VersionInfo."""

        response = self.query("VERSIONA")

        return self._parse_versiona(response)

    def best_position(self) -> PositionInfo:
        """Query the receiver's BESTPOSA log and return PositionInfo."""

        response = self.query("BESTPOSA")

        return self._parse_bestposa(response)

    def reference_station(self) -> ReferenceStationInfo:
        """
        Query the receiver's REFSTATIONA log and return
        ReferenceStationInfo.

        Only meaningful when the receiver is operating as an RTK
        rover receiving corrections from a base station.
        """

        response = self.query("REFSTATIONA")

        return self._parse_refstationa(response)
