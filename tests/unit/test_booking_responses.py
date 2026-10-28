"""All POST responses are diagnostic only: one attempt, permanent manual block."""
import json
from unittest.mock import Mock, patch

import requests

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import DAY, FORM, URL, response
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient, BookingPending, BookingNotVerified


class BookingResponseTests(IsolatedLogsTest):
    def test_no_status_or_feedback_can_confirm_release_retry_or_trigger_a_get(self):
        for status, body in ((200, '<div class="alert-box success">Successfully Scheduled</div>'),
                             (422, '<div class="alert-box alert">Your appointment has not been rescheduled.</div>'),
                             (302, ''), (429, ''), (500, ''), (200, '{"success":false}')):
            with self.subTest(status=status, body=body), patch.object(audit, "GUARD_DIR", self.test_log_directory / str(status)):
                audit.claim_path("123").unlink(missing_ok=True)
                session = Mock()
                session.get.side_effect = [response(FORM), response(payload={"available_times": ["08:30", "10:00"]})]
                session.post.return_value = response(body, status=status)
                client = AppointmentClient(session, URL, 95, "Vancouver")
                self.assertIsInstance(client.book(DAY, dry_run=False), BookingPending)
                self.assertEqual(audit.read_claim("123")["state"], "pending")
                self.assertNotIn("verify_at", audit.read_claim("123"))
                self.assertEqual(session.get.call_count, 2)
                session.post.assert_called_once()
                with self.assertRaises(BookingNotVerified):
                    client.book(DAY, dry_run=False)
                self.assertEqual(session.get.call_count, 2)
                session.post.assert_called_once()
        events = [json.loads(line) for line in audit.LOG_PATH.read_text().splitlines()]
        self.assertFalse(any(event["event"] in {"booking_verification", "booking_rejected", "booking_response"} for event in events))

    def test_timeout_keeps_block_and_never_checks_or_retries(self):
        session = Mock()
        session.get.side_effect = [response(FORM), response(payload={"available_times": ["08:30", "10:00"]})]
        session.post.side_effect = requests.Timeout()
        client = AppointmentClient(session, URL, 95, "Vancouver")
        self.assertIsInstance(client.book(DAY, dry_run=False), BookingPending)
        self.assertEqual(audit.read_claim("123")["outcome"], "Timeout")
        self.assertEqual(session.get.call_count, 2)
        session.post.assert_called_once()
