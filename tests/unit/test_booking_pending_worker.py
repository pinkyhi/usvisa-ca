"""Blocked workers keep ordinary polling and date reporting, without verification."""
import threading
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import FORM, URL, DAY, response
from usvisa.node.coordinated import Worker
from usvisa.node.state import NodeState
from usvisa.protocol.messages import GroupConfig, Message, ScheduleUpdated
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient


class PendingWorkerTests(IsolatedLogsTest):
    def test_submission_then_polls_keep_reporting_without_times_post_or_account_get(self):
        node, now = uuid4(), datetime.now(timezone.utc)
        group = GroupConfig(95, "Vancouver")
        state = NodeState(self.test_log_directory / "node.sqlite3", node, group)
        self.addCleanup(state.close)
        state.accept(Message.create(ScheduleUpdated(node, uuid4(), 1, now, 180,
                     date(2028, 1, 1), group), sent_at=now), now=now)
        transport = Mock(stopping=threading.Event())
        worker = Worker(state, transport)
        session = Mock()
        session.get.return_value = response(FORM)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        session.get.return_value = response(payload={"available_times": ["08:30"]})
        session.post.return_value = response('<div class="alert-box success">Successfully Scheduled</div>')
        with patch.multiple("usvisa.node.coordinated.cfg", MODE="coordinated", TEST_MODE=False,
                           DATE_REQUEST_CYCLE_LENGTH=0, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                           LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch("usvisa.node.coordinated.solo.log_message"), patch.object(worker, "notify") as notify:
            worker.try_dates(client, [DAY], observed=now)
            before = session.get.call_count
            session.get.return_value = response(payload=[{"date": DAY.isoformat()}])
            with self.assertLogs("usvisa.node.solo", "WARNING") as warnings:
                worker.poll(client)
                worker.poll(client)
            self.assertEqual(len(warnings.output), 2)
            self.assertEqual(session.get.call_count, before + 2)
            self.assertTrue(all('/days/' in call.args[0] for call in session.get.call_args_list[-2:]))
            session.post.assert_called_once()
            notify.assert_not_called()
        self.assertEqual(audit.read_claim("123")["state"], "pending")
        self.assertEqual(len(state.pending(now)), 3)
        transport.refresh_heartbeat.assert_called()
