"""
receiver.py

USGS GNSS Reference Station
Prototype 1.0

Direct serial communications with the Unicore UM980 GNSS receiver.

This module is intentionally narrow in scope. It is responsible only
for:

    * opening / closing the serial connection to the receiver, and
      automatically reconnecting after a serial-level failure
    * sending ASCII command queries and reading the matching response
    * filtering out asynchronous / unsolicited messages that are not
      the response to the query currently in progress
    * verifying the 32-bit CRC that terminates each ASCII log/response
    * parsing VERSIONA, BESTPOSA, and REFSTATIONA responses
    * tracking basic per-command timing statistics

Satellite-by-satellite tracking data, DOP values, the UM980 binary
log formats, RINEX generation, database logging, multi-threaded
logging, and GNSS-IR processing are all out of scope here and belong
in later modules / versions.

A note on field layouts: the UM980's ASCII command and log set is
modeled closely on NovAtel's OEM7 receivers. The VERSIONA and
BESTPOSA layouts below have been exercised against recorded UM980
output. The REFSTATIONA layout is taken from the published NovAtel
OEM7 REFSTATION log definition, since UM980-specific documentation
for this particular log was not available while writing this module;
it should be spot-checked against real REFSTATIONA output the first
time it is used against hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time

import serial


# ----------------------------------------------------------------
# Data Classes
# ----------------------------------------------------------------

@dataclass
class ComponentVersion:
    """One component group parsed out of a VERSIONA response."""

    component_type: str = ""
    model: str = ""
    psn: str = ""
    hardware_version: str = ""
    software_version: str = ""
    boot_version: str = ""
    compile_date: str = ""
    compile_time: str = ""


@dataclass
class VersionInfo:
    """
    Parsed response to a VERSIONA query.

    UM980 VERSIONA responses follow the Unicore/NovAtel-style log
    format:

        #VERSIONA,...;<num_components>,
        <component_type>,<model>,<psn>,<hardware_version>,
        <software_version>,<boot_version>,<compile_date>,
        <compile_time>,
        <component_type>,<model>,...        (repeated per component)
        *<checksum>

    `components` holds every component group found in the response.
    The top-level `model` / `firmware` / `hardware` / etc. fields
    mirror `components[0]` for convenience, since the first component
    is normally the GNSS card itself. `raw` always preserves the
    full, unparsed response.
    """

    model: str = ""
    firmware: str = ""
    hardware: str = ""
    psn: str = ""
    boot_version: str = ""
    compile_date: str = ""
    compile_time: str = ""
    components: list[ComponentVersion] = field(default_factory=list)
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

# Order of the comma-separated fields within a single VERSIONA
# component group.
_VERSIONA_FIELDS = (
    "component_type",
    "model",
    "psn",
    "hardware_version",
    "software_version",
    "boot_version",
    "compile_date",
    "compile_time",
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
    """

    device: str = "/dev/USB_GPS"
    baudrate: int = 115200
    timeout: float = 2.0
    serial_timeout: float = 0.5
    retries: int = 2
    retry_delay: float = 1.0
    verify_checksums: bool = True
    strict_checksums: bool = False

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

        The input buffer is flushed immediately before writing, so
        that any unsolicited log lines the receiver produced while
        idle do not get mistaken for the response to this command.
        The output buffer is flushed after writing to ensure the
        command has actually been pushed out to the OS / device
        before this call returns.
        """

        if not self.is_connected():
            raise ReceiverNotConnectedError("Receiver is not connected")

        assert self._serial is not None

        self._serial.reset_input_buffer()

        message = command.strip() + "\r\n"

        self._logger.debug("TX: %s", command.strip())

        self._serial.write(message.encode("ascii"))
        self._serial.flush()

    @staticmethod
    def _is_nmea_chatter(text: str) -> bool:
        """Return True if a line is an unsolicited NMEA sentence."""

        return text.startswith("$")

    def _is_unexpected_log(self, text: str, expected: str) -> bool:
        """
        Return True if `text` looks like a receiver log/response
        header, but not the one we asked for.

        Responses to ASCII queries begin with "#<LOGNAME>". If the
        receiver is also streaming other configured logs
        asynchronously, those will show up interleaved with the
        response we are waiting for; this lets read_response() skip
        past them instead of misinterpreting them as our answer.
        """

        if not text.startswith("#"):
            return False

        header = text[1:].split(",", 1)[0].split(";", 1)[0]

        return header.upper() != expected.upper()

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
        command).
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

    def read_response(self, expected: str | None = None) -> str:
        """
        Read lines from the receiver until a response line matching
        `expected` is found, and return it.

        Parameters
        ----------
        expected:
            The log/command name the caller is waiting for, e.g.
            "VERSIONA". Lines that are NMEA chatter, or that are a
            recognizable receiver log header for a *different* log
            name, are skipped. If None, the first non-NMEA line is
            returned as-is (used for commands with no fixed response
            name).

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

        assert self._serial is not None

        deadline = time.monotonic() + self.timeout

        while True:

            if time.monotonic() > deadline:
                raise ReceiverTimeoutError(
                    f"No response to {expected!r} within "
                    f"{self.timeout}s"
                )

            line = self._serial.readline()

            if not line:
                # The low-level serial_timeout expired with no data;
                # loop back around and check our own deadline.
                continue

            try:
                text = line.decode("ascii", errors="ignore").strip()
            except UnicodeDecodeError:
                self._logger.debug("RX: <undecodable bytes>")
                continue

            if not text:
                continue

            self._logger.debug("RX (raw): %s", text)

            if self._is_nmea_chatter(text):
                self._logger.debug("Ignoring NMEA chatter: %s", text)
                continue

            if expected is not None and self._is_unexpected_log(
                text, expected
            ):
                self._logger.info(
                    "Ignoring unsolicited log while waiting for %s: "
                    "%s",
                    expected,
                    text,
                )
                continue

            if self.verify_checksums:

                checksum_ok = self._verify_checksum(text)

                if checksum_ok is False:

                    message = f"Checksum mismatch in response: {text}"

                    if self.strict_checksums:
                        raise ReceiverError(message)

                    self._logger.warning(message)

                elif checksum_ok is True:
                    self._logger.debug("Checksum OK: %s", text)

            return text

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
                response = self.read_response(expected=expected)

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

            return response

        assert last_error is not None
        raise ReceiverError(
            f"Query {command!r} failed after {self.retries + 1} "
            f"attempts: {last_error}"
        ) from last_error

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
        Parse a VERSIONA response into a VersionInfo, including every
        component group present (not just the first).

        If the response is missing its header/data separator, a
        VersionInfo with the raw response preserved (and a logged
        warning) is returned rather than raising, since the receiver
        model and firmware string are informational and should not
        block startup.
        """

        try:
            _, data = self._split_header_and_data(response)
        except ReceiverError as exc:
            self._logger.warning("Could not parse VERSIONA: %s", exc)
            return VersionInfo(model="Unicore UM980", raw=response)

        fields = data.split(",")

        if not fields or not fields[0]:
            self._logger.warning(
                "VERSIONA response had no data fields: %s", response
            )
            return VersionInfo(model="Unicore UM980", raw=response)

        try:
            num_components = int(fields[0])
        except ValueError:
            num_components = 1
            self._logger.debug(
                "VERSIONA component count not numeric (%r); "
                "assuming 1",
                fields[0],
            )

        remaining = fields[1:]
        group_size = len(_VERSIONA_FIELDS)

        components: list[ComponentVersion] = []

        for index in range(max(num_components, 1)):

            start = index * group_size
            group = remaining[start:start + group_size]

            if not group:
                break

            parsed = dict(zip(_VERSIONA_FIELDS, group))

            components.append(
                ComponentVersion(
                    component_type=parsed.get(
                        "component_type", ""
                    ).strip('"'),
                    model=parsed.get("model", "").strip('"'),
                    psn=parsed.get("psn", "").strip('"'),
                    hardware_version=parsed.get(
                        "hardware_version", ""
                    ).strip('"'),
                    software_version=parsed.get(
                        "software_version", ""
                    ).strip('"'),
                    boot_version=parsed.get(
                        "boot_version", ""
                    ).strip('"'),
                    compile_date=parsed.get(
                        "compile_date", ""
                    ).strip('"'),
                    compile_time=parsed.get(
                        "compile_time", ""
                    ).strip('"'),
                )
            )

        if len(components) != max(num_components, 1):
            self._logger.debug(
                "VERSIONA declared %d component(s) but only %d "
                "parsed: %s",
                num_components,
                len(components),
                response,
            )

        primary = components[0] if components else ComponentVersion(
            model="Unicore UM980"
        )

        info = VersionInfo(
            model=primary.model or "Unicore UM980",
            firmware=primary.software_version,
            hardware=primary.hardware_version,
            psn=primary.psn,
            boot_version=primary.boot_version,
            compile_date=primary.compile_date,
            compile_time=primary.compile_time,
            components=components,
            raw=response,
        )

        self._logger.debug(
            "Parsed VERSIONA: %d component(s), primary=%s",
            len(components),
            primary,
        )

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
