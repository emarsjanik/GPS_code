#!/usr/bin/env python3

from receiver import Receiver
from database import Database

print("=" * 60)
print("Receiver / Database Integration Test")
print("=" * 60)

db = Database()
db.connect()

print("Database Connected")

with Receiver() as rx:
    print("Receiver Connected")

    version = rx.version()
    position = rx.best_position()

print("\nReceiver Version")
print("----------------")
print(version)

print("\nReceiver Position")
print("-----------------")
print(position)

print("\nSaving to database...")

db.save_receiver_version(version)
db.save_position(position)
db.save_receiver_status(position)

print("SUCCESS")

db.close()

print("\nIntegration test PASSED")
