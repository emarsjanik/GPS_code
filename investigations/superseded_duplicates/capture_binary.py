import sys
import time

sys.path.insert(0, ".")
from receiver import Receiver

commands = [
    "UNLOGALL COM1",
    "LOG COM1 RANGEB ONTIME 1",
    "LOG COM1 GPSEPHEMB ONCE",
    "LOG COM1 GLOEPHEMERISB ONCE",
    "LOG COM1 BDSEPHEMB ONCE",
    "LOG COM1 GALEPHEMB ONCE",
    "LOG COM1 QZSSEPHEMERISB ONCE",
]

with Receiver() as rx:
    count = 0
    skipped = 0
    command_index = 0
    next_command_at = time.monotonic()
    deadline = time.monotonic() + 60

    with open("../raw/test_binary1.um980", "wb") as f:
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

            if count == 0 and not (message.raw.startswith("#") or message.raw.startswith("$") or message.is_binary):
                skipped += 1
                continue

            f.write(message.raw_bytes)
            count += 1

print()
print("Skipped before first clean message:", skipped)
print("Total messages/chunks written:", count)
