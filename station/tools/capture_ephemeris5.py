import sys
import time

sys.path.insert(0, ".")
from receiver import Receiver

commands = [
    "UNLOGALL COM1",
    "LOG COM1 RANGEA ONTIME 1",
    "LOG COM1 GPSEPHEMA ONCE",
    "LOG COM1 GLOEPHEMERISA ONCE",
    "LOG COM1 BDSEPHEMA ONCE",
    "LOG COM1 GALEPHEMA ONCE",
    "LOG COM1 QZSSEPHEMERISA ONCE",
]

with Receiver() as rx:
    count = 0
    skipped = 0
    seen_types = {}
    command_index = 0
    next_command_at = time.monotonic()
    deadline = time.monotonic() + 60

    with open("../raw/test_recording9.um980", "wb") as f:
        while time.monotonic() < deadline:

            if command_index < len(commands) and time.monotonic() >= next_command_at:
                cmd = commands[command_index]
                print("Sending:", cmd)
                rx.send_command(cmd)
                command_index += 1
                next_command_at = time.monotonic() + 0.75

            message = rx.read_message()
            if message is None:
                continue

            # Only start writing once we're at a real message boundary
            # (an ASCII log header or NMEA sentence), never mid-message.
            if count == 0 and not (message.raw.startswith("#") or message.raw.startswith("$")):
                skipped += 1
                continue

            f.write(message.raw_bytes)
            count += 1
            seen_types[message.message_type] = seen_types.get(message.message_type, 0) + 1

            if message.message_type not in ("RANGEA", "NMEA", "UNKNOWN"):
                print("  ->", message.message_type)

print()
print("Skipped before first clean message:", skipped)
print("Total messages:", count)
print("By type:", seen_types)
