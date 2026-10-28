"""
tests/test_send_health_mail.py

The health email carries Date, Message-ID and From itself rather than
relying on the msmtp version to add them. From is the address msmtp
sends as -- the from of its account named "default", or an address
given with --from, which is then passed to msmtp as the envelope-from
too -- so the two agree.
"""

from __future__ import annotations

import email
import io
import sys
import tempfile
import unittest
import unittest.mock
from email import policy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis_tools"))
import send_health_mail as shm  # noqa: E402


class TestMsmtpFrom(unittest.TestCase):

    def parse(self, text: str):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "msmtprc"
            path.write_text(text)
            return shm.msmtp_from(path)

    def test_default_account_alias(self) -> None:
        self.assertEqual(self.parse(
            "defaults\nauth on\n\naccount other\nfrom other@example.org\n\n"
            "account usgs\nhost smtp.example.gov\nfrom station@example.gov\n\n"
            "account default : usgs\n"), "station@example.gov")

    def test_only_the_account_named_default_counts(self) -> None:
        # msmtp without --account or --from uses the account named
        # "default", wherever it is in the file, and nothing else: the
        # first account is not a fallback.
        self.assertEqual(self.parse(
            "account a\nfrom a@example.org\naccount default\nfrom d@example.gov\n"),
            "d@example.gov")
        self.assertIsNone(self.parse("account a\nfrom a@example.org\naccount b\nfrom b@x.org\n"))
        self.assertIsNone(self.parse("defaults\nfrom d@example.org\naccount a\nhost h\n"))

    def test_inheritance_defaults_and_overrides(self) -> None:
        # `account default : usgs` inherits usgs's settings; lines after
        # it override them. An account without its own from has the
        # defaults' one.
        self.assertEqual(self.parse(
            "account usgs\nfrom station@example.gov\n"
            "account default : usgs\nfrom health@example.gov\n"), "health@example.gov")
        self.assertEqual(self.parse(
            "defaults\nfrom d@example.gov\naccount default\nhost h\n"), "d@example.gov")
        self.assertEqual(self.parse(
            "account a\nhost h\naccount b\nfrom b@example.gov\naccount default : a, b\n"),
            "b@example.gov")

    def test_tabs_and_quotes(self) -> None:
        self.assertEqual(self.parse('account\tdefault\nfrom\t"station@example.gov"\n'),
                         "station@example.gov")

    def test_substitutions_and_missing_file_give_none(self) -> None:
        self.assertIsNone(self.parse("account default\nfrom %U@%H\n"))
        self.assertIsNone(shm.msmtp_from(Path("/nonexistent/msmtprc")))

    def test_what_only_msmtp_can_know_gives_none(self) -> None:
        for text in ("account default\nfrom *@example.gov\n",
                     "account default\nfrom a@example.gov\nauto_from on\n",
                     "account default\neval echo from a@example.gov\n"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_user_file_then_system_file(self) -> None:
        # ~/.msmtprc, else $XDG_CONFIG_HOME/msmtp/config; an account
        # "default" there overrides /etc/msmtprc's.
        with tempfile.TemporaryDirectory() as td:
            home, system = Path(td) / "home", Path(td) / "msmtprc"
            (home / ".config" / "msmtp").mkdir(parents=True)
            system.write_text("account default\nfrom system@example.gov\n")
            with unittest.mock.patch.object(shm.Path, "home", return_value=home), \
                    unittest.mock.patch.object(shm, "MSMTP_SYSTEM_CONFIG", system), \
                    unittest.mock.patch.dict(shm.os.environ, {}, clear=False):
                shm.os.environ.pop("XDG_CONFIG_HOME", None)
                self.assertEqual(shm.msmtp_from(), "system@example.gov")
                (home / ".config" / "msmtp" / "config").write_text(
                    "account default\nfrom xdg@example.gov\n")
                self.assertEqual(shm.msmtp_from(), "xdg@example.gov")
                (home / ".msmtprc").write_text("account other\nfrom o@example.gov\n")
                self.assertEqual(shm.msmtp_from(), "system@example.gov")
                (home / ".msmtprc").write_text("account default\nfrom user@example.gov\n")
                self.assertEqual(shm.msmtp_from(), "user@example.gov")


class TestHeaders(unittest.TestCase):

    def send(self, *extra: str, configured: str | None = None):
        sent = {}

        def fake_run(cmd, input=None, capture_output=False):
            sent["cmd"], sent["bytes"] = cmd, input
            return unittest.mock.Mock(returncode=0, stderr=b"")

        argv = ["send_health_mail.py", "--subject", "GPS: all healthy",
                "--to", "a@example.gov", "b@example.gov", "--no-stats", *extra]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(sys, "stdin", io.StringIO("report\n")), \
                unittest.mock.patch.object(shm.subprocess, "run", fake_run), \
                unittest.mock.patch.object(shm, "msmtp_from", return_value=configured), \
                unittest.mock.patch.dict(shm.os.environ, {}, clear=False):
            self.assertEqual(shm.main(), 0)
        return sent["cmd"], email.message_from_bytes(sent["bytes"], policy=policy.default)

    def test_date_message_id_and_from(self) -> None:
        cmd, msg = self.send("--from", "station@example.gov")
        self.assertTrue(msg["Date"])
        self.assertIsNotNone(email.utils.parsedate_to_datetime(msg["Date"]))
        # A long id is folded onto the next line, legally; it comes
        # back with the fold's leading blank, depending on its length.
        self.assertRegex(str(msg["Message-ID"]).strip(), r"^<[^@>]+@example\.gov>$")
        self.assertEqual(msg["From"].addresses[0].addr_spec, "station@example.gov")
        self.assertEqual(msg["To"], "a@example.gov, b@example.gov")

    def test_given_sender_is_the_envelope_from_too(self) -> None:
        # Given only to the header, msmtp sent as its default account's
        # address: From and envelope-from disagreed.
        cmd, msg = self.send("--from", "station@example.gov", configured="other@example.org")
        self.assertEqual(cmd, ["msmtp", "--from=station@example.gov",
                               "a@example.gov", "b@example.gov"])
        self.assertEqual(msg["From"].addresses[0].addr_spec, "station@example.gov")

    def test_configured_sender_leaves_msmtp_its_account(self) -> None:
        with unittest.mock.patch.dict(shm.os.environ):
            shm.os.environ.pop("HEALTH_MAIL_FROM", None)
            cmd, msg = self.send(configured="station@example.gov")
        self.assertEqual(cmd, ["msmtp", "a@example.gov", "b@example.gov"])
        self.assertEqual(msg["From"].addresses[0].addr_spec, "station@example.gov")

    def test_no_known_sender_leaves_from_to_msmtp(self) -> None:
        with unittest.mock.patch.dict(shm.os.environ):
            shm.os.environ.pop("HEALTH_MAIL_FROM", None)
            cmd, msg = self.send()
        self.assertEqual(cmd, ["msmtp", "a@example.gov", "b@example.gov"])
        self.assertIsNone(msg["From"])
        self.assertTrue(msg["Date"])
        self.assertTrue(msg["Message-ID"])


class TestAttachments(unittest.TestCase):
    """The pictures are downscaled for email; a data file -- the waterline's 7-day DEM as a GeoTIFF --
    goes byte for byte as image/tiff, or, over DATA_MAX_BYTES, not at all, the body saying where it is."""

    send = TestHeaders.send

    def parts(self, msg):
        return {p.get_filename(): p for p in msg.iter_attachments()}

    def body(self, msg) -> str:
        return msg.get_body(preferencelist=("plain",)).get_content()

    def test_geotiff_attached_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tif = Path(td) / "dem_intertidal_7day_dem.tif"
            data = b"II*\x00" + bytes(range(256)) * 6000          # 1.5 MB: above the pictures' limit
            tif.write_bytes(data)
            _, msg = self.send("--attach", str(tif))
        part = self.parts(msg).get("dem_intertidal_7day_dem.tif")
        self.assertIsNotNone(part)
        self.assertEqual(part.get_content_type(), "image/tiff")
        self.assertEqual(part.get_content(), data)
        self.assertIn("dem_intertidal_7day_dem.tif", self.body(msg).split("Attached:", 1)[1])

    def test_geotiff_over_the_cap_not_attached_body_says_where(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tif = Path(td) / "dem_intertidal_7day_dem.tif"
            tif.write_bytes(b"II*\x00" + bytes(64))
            with unittest.mock.patch.object(shm, "DATA_MAX_BYTES", 10):
                _, msg = self.send("--attach", str(tif))
        self.assertNotIn("dem_intertidal_7day_dem.tif", self.parts(msg))
        self.assertIn("not attached, too large to email: dem_intertidal_7day_dem.tif", self.body(msg))
        self.assertIn(str(tif), self.body(msg))

    def test_missing_geotiff_noted(self) -> None:
        _, msg = self.send("--attach", "/nonexistent/dem_intertidal_7day_dem.tif")
        self.assertFalse(self.parts(msg))
        self.assertIn("file missing: /nonexistent/dem_intertidal_7day_dem.tif", self.body(msg))

    def test_small_png_unchanged_as_png(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "7_day_plot.png"
            raw = b"\x89PNG\r\n\x1a\n" + bytes(100)
            png.write_bytes(raw)
            _, msg = self.send("--attach", str(png))
        part = self.parts(msg).get("7_day_plot.png")
        self.assertEqual(part.get_content_type(), "image/png")
        self.assertEqual(part.get_content(), raw)

    def test_mime_types(self) -> None:
        self.assertEqual(shm.mime_type("a.TIF"), ("image", "tiff"))
        self.assertEqual(shm.mime_type("a.tiff"), ("image", "tiff"))
        self.assertEqual(shm.mime_type("a.jpg"), ("image", "jpeg"))
        self.assertEqual(shm.mime_type("a.png"), ("image", "png"))
        self.assertEqual(shm.mime_type("a.asc"), ("application", "octet-stream"))

    def test_the_daily_email_attaches_the_7day_geotiff(self) -> None:
        text = (ROOT / "station_health_mail.sh").read_text()
        self.assertIn('--attach "/mnt/I2Rgus_Data/waterline/dem_intertidal_7day_dem.tif"', text)


if __name__ == "__main__":
    unittest.main()
