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
    for command in commands:
        print("Sending:", command)
        rx.send_command(command)
        time.sleep(0.75)

    print("Listening for 25 seconds...")
    count = 0
    seen_types = {}
    deadline = time.monotonic() + 25

    with open("../raw/test_recording6.um980", "wb") as f:
        while time.monotonic() < deadline:
            message = rx.read_message()
            if message is None:
                continue
            f.write(message.raw_bytes)
            count += 1
            seen_types[message.message_type] = seen_types.get(message.message_type, 0) + 1
            if message.message_type not in ("RANGEA", "NMEA", "UNKNOWN"):
                print("  ->", message.message_type)

print()
print("Total messages:", count)
print("By type:", seen_types)
