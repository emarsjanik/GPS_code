"""
tests/test_send_health_mail.py

The health email carries Date, Message-ID and From itself rather than
relying on the msmtp version to add them; From is the address msmtp
sends as (~/.msmtprc), so the two agree.
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

    def test_first_account_then_defaults(self) -> None:
        self.assertEqual(self.parse("account a\nfrom a@example.org\naccount b\nfrom b@x.org\n"),
                         "a@example.org")
        self.assertEqual(self.parse("defaults\nfrom d@example.org\naccount a\nhost h\n"),
                         "d@example.org")

    def test_substitutions_and_missing_file_give_none(self) -> None:
        self.assertIsNone(self.parse("account a\nfrom %U@%H\n"))
        self.assertIsNone(shm.msmtp_from(Path("/nonexistent/msmtprc")))


class TestHeaders(unittest.TestCase):

    def send(self, *extra: str) -> email.message.EmailMessage:
        sent = {}

        def fake_run(cmd, input=None, capture_output=False):
            sent["cmd"], sent["bytes"] = cmd, input
            return unittest.mock.Mock(returncode=0, stderr=b"")

        argv = ["send_health_mail.py", "--subject", "GPS: all healthy",
                "--to", "a@example.gov", "b@example.gov", "--no-stats", *extra]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(sys, "stdin", io.StringIO("report\n")), \
                unittest.mock.patch.object(shm.subprocess, "run", fake_run), \
                unittest.mock.patch.object(shm, "msmtp_from", return_value=None), \
                unittest.mock.patch.dict(shm.os.environ, {}, clear=False):
            self.assertEqual(shm.main(), 0)
        self.assertEqual(sent["cmd"], ["msmtp", "a@example.gov", "b@example.gov"])
        return email.message_from_bytes(sent["bytes"], policy=policy.default)

    def test_date_message_id_and_from(self) -> None:
        msg = self.send("--from", "station@example.gov")
        self.assertTrue(msg["Date"])
        self.assertIsNotNone(email.utils.parsedate_to_datetime(msg["Date"]))
        self.assertRegex(msg["Message-ID"], r"^<[^@>]+@example\.gov>$")
        self.assertEqual(msg["From"].addresses[0].addr_spec, "station@example.gov")
        self.assertEqual(msg["To"], "a@example.gov, b@example.gov")

    def test_no_known_sender_leaves_from_to_msmtp(self) -> None:
        with unittest.mock.patch.dict(shm.os.environ):
            shm.os.environ.pop("HEALTH_MAIL_FROM", None)
            msg = self.send()
        self.assertIsNone(msg["From"])
        self.assertTrue(msg["Date"])
        self.assertTrue(msg["Message-ID"])


if __name__ == "__main__":
    unittest.main()
