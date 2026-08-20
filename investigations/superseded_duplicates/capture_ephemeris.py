import sys
import time

sys.path.insert(0, ".")
from receiver import Receiver

with Receiver() as rx:
    rx.send_command("UNLOGALL COM1")
    time.sleep(0.5)
    rx.send_command("LOG COM1 RANGEA ONTIME 1")
    rx.send_command("LOG COM1 GPSEPHEMA ONCE")
    rx.send_command("LOG COM1 GLOEPHEMERISA ONCE")
    rx.send_command("LOG COM1 BDSEPHEMA ONCE")
    rx.send_command("LOG COM1 GALEPHEMA ONCE")
    rx.send_command("LOG COM1 QZSSEPHEMERISA ONCE")

    count = 0
    deadline = time.monotonic() + 20

    with open("../raw/test_recording5.um980", "wb") as f:
        while time.monotonic() < deadline:
            message = rx.read_message()
            if message is None:
                continue
            f.write(message.raw_bytes)
            count += 1

print("messages captured:", count)
