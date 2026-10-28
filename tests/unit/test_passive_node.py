"""Passive nodes only monitor/share; saved blocks warn on every ordinary poll."""
import threading
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import DAY, FORM, URL, response
from usvisa.node import app, settings
from usvisa.node.coordinated import Worker
from usvisa.node.state import NodeState
from usvisa.protocol.messages import GroupConfig, Message, ScheduleUpdated, SlotsAvailable
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient


class PassiveNodeTests(IsolatedLogsTest):
    def setUp(self):
        super().setUp()
        self.now, self.node = datetime.now(timezone.utc), uuid4()
        self.state = NodeState(self.test_log_directory / "node.sqlite3", self.node, GroupConfig(95, "Vancouver"))
        self.addCleanup(self.state.close)
        self.state.accept(Message.create(ScheduleUpdated(self.node, uuid4(), 1, self.now, 180,
            date(2028, 1, 1), GroupConfig(95, "Vancouver")), sent_at=self.now), now=self.now)
        self.worker = Worker(self.state, Mock(stopping=threading.Event()))
        self.addCleanup(self.worker.close)
        patcher = patch.multiple(settings, MODE="coordinated-passive", TEST_MODE=False,
            DATE_REQUEST_CYCLE_LENGTH=0, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
            LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_passive_polls_and_shares_without_times_booking_or_verification(self):
        client = Mock(schedule_id="123")
        client.get_available_dates.return_value = [DAY]
        audit.claim("123", DAY, "08:30", "Vancouver", verify_at=0)
        with patch("usvisa.node.coordinated.solo.log_message"), \
                patch("usvisa.node.coordinated.solo.warn_booking_block"):
            self.worker.poll(client)
            task = Message.create(SlotsAvailable(uuid4(), uuid4(), self.now,
                self.now + timedelta(seconds=180), (DAY,)), sent_at=self.now)
            self.worker.shared_task(client, task)
        client.get_available_dates.assert_called_once()
        client.get_available_times.assert_not_called()
        client.book.assert_not_called()
        client.verification_client.assert_not_called()
        self.assertEqual(len(self.state.pending(self.now)), 1)
        self.assertEqual(self.state.pending(self.now)[0][0].data.dates, (DAY,))

    def test_passive_without_saved_block_still_never_books(self):
        client = Mock(schedule_id="123")
        with patch("usvisa.node.coordinated.solo.log_message"):
            self.worker.try_dates(client, [DAY], observed=self.now)
        client.book.assert_not_called()
        self.assertEqual(self.state.pending(self.now)[0][0].data.dates, (DAY,))

    def test_warning_is_emitted_on_every_empty_poll_while_block_exists(self):
        client = Mock(schedule_id="123")
        client.get_available_dates.return_value = []
        audit.claim("123", DAY, "08:30", "Vancouver")
        with self.assertLogs("usvisa.node.solo", level="WARNING") as recorded, \
                patch("usvisa.node.coordinated.solo.log_message"):
            self.worker.poll(client)
            self.worker.poll(client)
        self.assertEqual(len(recorded.output), 2)
        for line in recorded.output:
            self.assertIn("Rescheduling was attempted so is blocked now to prevent overbooking.", line)
            self.assertIn(str(audit.claim_path("123").resolve()), line)
            self.assertIn("Remove it to continue reschedule attemps.", line)

    def test_manual_file_removal_is_seen_before_next_active_booking_attempt(self):
        session = Mock()
        session.get.return_value = response(FORM)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        session.get.reset_mock()
        session.get.return_value = response(payload={"available_times": ["08:30"]})
        session.post.return_value = response(status=302)
        audit.claim("123", DAY, "08:30", "Vancouver")
        audit.update_claim("123", state="confirmed")
        with patch.object(settings, "MODE", "coordinated"), patch("usvisa.node.coordinated.solo.log_message"):
            self.worker.try_dates(client, [DAY])
            session.post.assert_not_called()
            audit.claim_path("123").unlink()
            self.worker.try_dates(client, [DAY])
        session.post.assert_called_once()
        self.assertEqual(audit.read_claim("123")["state"], "pending")

    def test_passive_cli_uses_same_registration_and_schedule_path(self):
        with patch.multiple(settings, USER_EMAIL="test@example.com", USER_PASSWORD="test", USER_CONSULATE="Vancouver"), \
                patch("usvisa.node.bootstrap.prepare") as prepare, \
                patch("usvisa.node.coordinated.main", return_value=0) as run:
            self.assertEqual(app.main(["--mode", "coordinated-passive", "--check-coordination"]), 0)
        prepare.assert_called_once()
        run.assert_called_once_with(check_only=True)

    def test_passive_settings_and_cli_do_not_require_acceptable_dates(self):
        with patch.multiple(settings, USER_EMAIL="test@example.com", USER_PASSWORD="test",
                            USER_CONSULATE="Vancouver", EARLIEST_ACCEPTABLE_DATE="", LATEST_ACCEPTABLE_DATE=""), \
                patch.dict("os.environ", {"NODE_MIN_DATE": ""}), \
                patch("usvisa.node.bootstrap.prepare"), patch("usvisa.node.coordinated.main", return_value=0):
            settings.validate_settings()
            self.assertEqual(app.main(["--mode", "coordinated-passive", "--check-coordination"]), 0)

    def test_no_consumers_still_poll_but_do_not_queue_useless_findings(self):
        self.state.accept(Message.create(ScheduleUpdated(self.node, uuid4(), 2, self.now, 180,
            None, GroupConfig(95, "Vancouver")), sent_at=self.now), now=self.now)
        client = Mock(schedule_id="123")
        client.get_available_dates.return_value = [DAY]
        with patch("usvisa.node.coordinated.solo.log_message"):
            self.worker.poll(client)
        client.get_available_dates.assert_called_once()
        client.book.assert_not_called()
        self.assertEqual(self.state.pending(self.now), [])
