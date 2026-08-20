import sys
import time

sys.path.insert(0, ".")
from receiver import Receiver

if len(sys.argv) < 2:
    print("Usage: python3 tools/send_cmd.py \"COMMAND TEXT\"")
    sys.exit(1)

command = sys.argv[1]

print("=" * 70)
print("Sending:", command)
print("=" * 70)

with Receiver() as rx:
    rx.send_command(command)
    time.sleep(1.0)

    responses = []
    for _ in range(20):
        message = rx.read_message()
        if message is None:
            break
        responses.append(message.raw)

if responses:
    print("Receiver response(s):")
    for line in responses:
        print(" ", line)
else:
    print("(no response received -- this can be normal for CONFIG/LOG commands)")
