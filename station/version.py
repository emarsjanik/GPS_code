"""
version.py

USGS GNSS Reference Station

The version of this station software itself (as opposed to the GNSS
receiver's firmware version, which comes from receiver.VersionInfo).
Bump this on meaningful releases; it is recorded into
station_info.software_version by station.py on every startup via
Database.save_receiver_version(..., software_version=__version__).
"""

__version__ = "1.0.0"
