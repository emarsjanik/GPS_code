"""
exceptions.py

USGS GNSS Reference Station
Prototype 1.0

Small, shared exceptions used at the orchestration level (station.py
and station_manager.py), as opposed to the module-specific
exceptions already defined in receiver.py (ReceiverError and
friends) and database.py (DatabaseError and friends).
"""


class ConfigurationError(Exception):
    """
    Raised when the loaded station configuration fails validation
    (e.g. a required field is empty, or an expected directory is
    missing). Distinct from json.JSONDecodeError or
    FileNotFoundError, which config.py itself already raises for a
    missing or malformed station.json; this is for values that
    parsed fine but aren't usable.
    """
