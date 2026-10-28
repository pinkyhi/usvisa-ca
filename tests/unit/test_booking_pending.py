"""The file blocks subsequent submissions, including crashes and restarts."""
from unittest.mock import Mock

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import DAY, FORM, URL, response
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient, BookingNotVerified, BookingPending


class PendingBookingTests(IsolatedLogsTest):
    def test_crash_during_post_leaves_durable_block_and_restart_sends_nothing(self):
        session = Mock()
        session.get.side_effect = [response(FORM), response(payload={"available_times": ["08:30"]})]

        def crash(*args, **kwargs):
            self.assertEqual(audit.read_claim("123")["state"], "pending")
            raise SystemExit("simulated crash")

        session.post.side_effect = crash
        with self.assertRaises(SystemExit):
            AppointmentClient(session, URL, 95, "Vancouver").book(DAY, dry_run=False)
        restarted = AppointmentClient(Mock(), URL, 95, "Vancouver")
        with self.assertRaises(BookingNotVerified):
            restarted.book(DAY, dry_run=False)
        restarted.session.get.assert_not_called()
        restarted.session.post.assert_not_called()

    def test_manual_removal_allows_exactly_one_new_submission(self):
        session = Mock()
        session.get.return_value = response(FORM)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        session.get.return_value = response(payload={"available_times": ["08:30"]})
        session.post.return_value = response(status=302)
        client.book(DAY, dry_run=False)
        with self.assertRaises(BookingNotVerified):
            client.book(DAY, dry_run=False)
        audit.claim_path("123").unlink()
        self.assertIsInstance(client.book(DAY, dry_run=False), BookingPending)
        self.assertEqual(session.post.call_count, 2)
