"""
config.py

USGS GNSS Reference Station
Prototype 1.0

Loads and validates the station configuration from
station/resources/station.json.
"""

from pathlib import Path
import json


class Config:
    """Application configuration."""

    def __init__(self):

        #
        # Project Root
        #
        # Directory layout:
        #
        # GNSS/
        #     archive/
        #     database/
        #     logs/
        #     products/
        #     raw/
        #     reports/
        #     rinex/
        #     scripts/
        #     station/
        #

        self.project_root = Path(__file__).resolve().parent.parent

        #
        # Resources
        #

        self.station_dir = self.project_root / "station"
        self.resource_dir = self.station_dir / "resources"

        self.station_json = self.resource_dir / "station.json"

        #
        # Data directories
        #

        self.raw_dir = self.project_root / "raw"
        self.rinex_dir = self.project_root / "rinex"
        self.archive_dir = self.project_root / "archive"
        self.products_dir = self.project_root / "products"
        self.logs_dir = self.project_root / "logs"
        self.database_dir = self.project_root / "database"
        self.reports_dir = self.project_root / "reports"
        self.scripts_dir = self.project_root / "scripts"

        #
        # Database
        #

        self.database_file = self.database_dir / "station.db"

        #
        # Receiver defaults (overridden below by station.json, if present)
        #

        self.receiver_port = "/dev/USB_GPS"
        self.receiver_baud = 115200
        self.receiver_timeout = 2.0

        #
        # Load station.json
        #

        self.station = {}

        self.load_station()

        # station.json may specify receiver_port / receiver_baud /
        # receiver_timeout explicitly; if so, they take precedence
        # over the hardcoded defaults above. (Previously these three
        # were set only once, before load_station() ran, so a
        # station.json value of the same name was silently ignored.)

        self.receiver_port = self.station.get("receiver_port", self.receiver_port)
        self.receiver_baud = self.station.get("receiver_baud", self.receiver_baud)
        self.receiver_timeout = self.station.get(
            "receiver_timeout", self.receiver_timeout
        )

        #
        # Verify directory structure
        #

        self.create_directories()

    # ---------------------------------------------------------

    def load_station(self):

        if not self.station_json.exists():

            raise FileNotFoundError(
                f"Missing configuration file:\n{self.station_json}"
            )

        with open(self.station_json, "r") as f:

            self.station = json.load(f)

    # ---------------------------------------------------------

    def create_directories(self):

        directories = [

            self.raw_dir,
            self.rinex_dir,
            self.archive_dir,
            self.products_dir,
            self.logs_dir,
            self.database_dir,
            self.reports_dir,
            self.scripts_dir,

        ]

        for directory in directories:

            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

    # ---------------------------------------------------------

    @property
    def station_id(self):

        return self.station.get("station_id", "")

    @property
    def station_name(self):

        return self.station.get("station_name", "")

    @property
    def agency(self):

        return self.station.get("agency", "")

    @property
    def observer(self):

        return self.station.get("observer", "")

    @property
    def receiver_model(self):

        return self.station.get("receiver_model", "")

    @property
    def receiver_firmware(self):

        return self.station.get("receiver_firmware", "")

    @property
    def latitude(self):

        return self.station.get("latitude", 0.0)

    @property
    def longitude(self):

        return self.station.get("longitude", 0.0)

    @property
    def height(self):

        return self.station.get("height", 0.0)

    # ---------------------------------------------------------

    def __str__(self):

        lines = [

            "Configuration",
            "-------------",
            f"Station ID : {self.station_id}",
            f"Station    : {self.station_name}",
            f"Agency     : {self.agency}",
            f"Observer   : {self.observer}",
            "",
            f"Receiver   : {self.receiver_model}",
            f"Firmware   : {self.receiver_firmware}",
            "",
            f"Latitude   : {self.latitude}",
            f"Longitude  : {self.longitude}",
            f"Height     : {self.height}",
            "",
            f"Raw Dir    : {self.raw_dir}",
            f"RINEX Dir  : {self.rinex_dir}",
            f"Database   : {self.database_file}",

        ]

        return "\n".join(lines)
