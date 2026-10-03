"""Run directly to send one real email; unittest discovery runs only offline checks."""

import io
import re
import smtplib
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# Direct execution puts tests/ on sys.path; locate settings.py beside the project.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notifications
import settings


def check_gmail():
    required = ("GMAIL_EMAIL", "GMAIL_APPLICATION_PWD", "RECEIVER_EMAIL")
    missing = [name for name in required if not getattr(settings, name)]
    if missing:
        print("Fill these settings in .env: " + ", ".join(missing))
        return 2
    for name in ("GMAIL_EMAIL", "RECEIVER_EMAIL"):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", getattr(settings, name)):
            print(f"{name} must contain an email address, not a display name")
            return 2
    try:
        notifications.send_notification(
            "[TEST] US Visa Rescheduler Gmail check",
            "Standalone Gmail notification test for US Visa Rescheduler.",
            raise_on_error=True,
        )
    except smtplib.SMTPAuthenticationError as error:
        print(f"Gmail rejected authentication (SMTP {error.smtp_code}). "
              "Check GMAIL_EMAIL and its Google App Password.")
        return 1
    except smtplib.SMTPRecipientsRefused:
        print("SMTP rejected RECEIVER_EMAIL. Check the recipient address.")
        return 1
    except smtplib.SMTPSenderRefused:
        print("SMTP rejected the sender. Check GMAIL_EMAIL.")
        return 1
    except (smtplib.SMTPException, OSError, ValueError) as error:
        print(f"Gmail test failed ({type(error).__name__}). Check SMTP connectivity and settings.")
        return 1
    print("Gmail accepted one test email. Check the recipient inbox/spam folder.")
    return 0


class GmailCheckTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(
            settings, GMAIL_EMAIL="sender@gmail.com", GMAIL_APPLICATION_PWD="abcd efgh ijkl mnop",
            RECEIVER_EMAIL="receiver@example.com",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        output = redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.smtp_patcher = patch.object(notifications.smtplib, "SMTP")
        self.smtp = self.smtp_patcher.start()
        self.addCleanup(self.smtp_patcher.stop)
        self.connection = self.smtp.return_value.__enter__.return_value

    def test_explicit_live_command_sends_one_message_through_production_sender(self):
        self.assertEqual(check_gmail(), 0)
        self.connection.login.assert_called_once_with("sender@gmail.com", "abcdefghijklmnop")
        self.connection.send_message.assert_called_once()
        message = self.connection.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "[TEST] US Visa Rescheduler Gmail check")
        self.assertIn("Gmail accepted", self.output.getvalue())

    def test_missing_settings_do_not_open_smtp(self):
        with patch.object(settings, "GMAIL_APPLICATION_PWD", ""):
            self.assertEqual(check_gmail(), 2)
        self.smtp.assert_not_called()

    def test_display_name_instead_of_address_does_not_open_smtp(self):
        with patch.object(settings, "GMAIL_EMAIL", "Reschedule"):
            self.assertEqual(check_gmail(), 2)
        self.smtp.assert_not_called()

    def test_authentication_failure_is_reported_without_password(self):
        self.connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"rejected")
        self.assertEqual(check_gmail(), 1)
        self.assertIn("authentication", self.output.getvalue())
        self.assertNotIn("abcdefghijklmnop", self.output.getvalue())
        self.connection.send_message.assert_not_called()

    def test_connection_error_fails_the_check(self):
        self.smtp.side_effect = OSError("unavailable")
        self.assertEqual(check_gmail(), 1)
        self.assertIn("OSError", self.output.getvalue())

    def test_recipient_rejection_fails_the_check(self):
        self.connection.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"receiver@example.com": (550, b"rejected")},
        )
        self.assertEqual(check_gmail(), 1)
        self.assertIn("RECEIVER_EMAIL", self.output.getvalue())

    def test_malformed_message_is_nonfatal_for_appointment_notifications(self):
        with patch.object(settings, "GMAIL_SENDER_NAME", "invalid\nname"), \
             self.assertLogs("notifications", level="WARNING"):
            self.assertFalse(notifications.send_notification("subject", "body"))
        self.smtp.assert_not_called()


@unittest.skipUnless(
    __name__ == "__main__",
    "Live Gmail check: run python tests/test_check_gmail.py",
)
class GmailLiveTests(unittest.TestCase):
    def test_gmail_accepts_real_notification(self):
        # Uses the real .env and production SMTP sender, without any mocks.
        self.assertEqual(
            check_gmail(), 0,
            "Gmail check failed; see the configuration/SMTP diagnostic above",
        )


if __name__ == "__main__":
    unittest.main(defaultTest="GmailLiveTests", verbosity=2)
