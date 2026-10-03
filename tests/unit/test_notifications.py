"""Offline Gmail checks using a mocked SMTP connection."""

import io
import smtplib
import socket
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import notifications
import settings
from tests.gmail_check import check_gmail


class GmailCheckTests(unittest.TestCase):
    def setUp(self):
        self.real_smtp = smtplib.SMTP
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

    def test_smtp_initialization_does_not_resolve_local_hostname(self):
        self.smtp.side_effect = self.real_smtp
        with patch.object(socket, "getfqdn", side_effect=AssertionError("Local DNS lookup")) as lookup, \
             patch.object(self.real_smtp, "connect", return_value=(220, b"ready")), \
             patch.object(self.real_smtp, "ehlo"), \
             patch.object(self.real_smtp, "starttls"), \
             patch.object(self.real_smtp, "login"), \
             patch.object(self.real_smtp, "send_message") as send, \
             patch.object(self.real_smtp, "quit", return_value=(221, b"bye")):
            self.assertEqual(check_gmail(), 0)
        lookup.assert_not_called()
        send.assert_called_once()

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

    def test_disconnect_reports_the_failing_step_without_credentials(self):
        for method, stage in (("starttls", "starting TLS"),
                              ("login", "authenticating"),
                              ("send_message", "submitting the message")):
            with self.subTest(method=method):
                self.output.seek(0)
                self.output.truncate(0)
                with patch.object(self.connection, method,
                                  side_effect=smtplib.SMTPServerDisconnected("closed")):
                    self.assertEqual(check_gmail(), 1)
                self.assertIn(stage, self.output.getvalue())
                self.assertIn("SMTPServerDisconnected", self.output.getvalue())
                self.assertNotIn("abcdefghijklmnop", self.output.getvalue())
                self.assertNotIn("sender@gmail.com", self.output.getvalue())

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


