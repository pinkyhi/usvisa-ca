"""Complete opt-in POST diagnostics across durable, bounded transport messages."""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl
from uuid import uuid4

import requests

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import DAY, FORM, URL, response
from usvisa.coordinator.logs import prune
from usvisa.coordinator.repository import Repository
from usvisa.coordinator.service import Coordinator
from usvisa.node.state import NodeState
from usvisa.node.telemetry import Telemetry
from usvisa.protocol.messages import GroupConfig, Message, NodeConfig, parse_message, MAX_MESSAGE_BYTES
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient, BookingPending


class BodyLogTests(IsolatedLogsTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.node = uuid4()
        self.group = GroupConfig(95, "Vancouver")
        self.now = datetime.now(timezone.utc)
        self.state = NodeState(self.root / "node.sqlite3", self.node, self.group)
        self.addCleanup(lambda: self.state.close())
        self.repo = Repository(self.root / "coordinator.sqlite3")
        self.addCleanup(lambda: self.repo.close())
        self.repo.register_node(self.node)
        self.repo.mark_ready(self.node)
        self.service = Coordinator(self.repo, self.group, now=self.now)
        self.addCleanup(audit.subscribe(Telemetry(self.state)))
        self.policy(True)

    def policy(self, enabled):
        self.state.accept(Message.create(NodeConfig(self.node, uuid4(), False,
                          ("reschedule",) if enabled else ()), sent_at=self.now), now=self.now)

    def pending(self):
        with self.state.transaction() as db:
            raw = [row[0] for row in db.execute("SELECT body FROM telemetry_outbox ORDER BY rowid")]
        self.assertTrue(all(len(body.encode("utf-8")) <= MAX_MESSAGE_BYTES for body in raw))
        return [parse_message(body) for body in raw]

    def deliver(self, messages):
        for message in messages:
            result = self.service.handle(message, publisher=f"node_{self.node}", now=datetime.now(timezone.utc))
            self.assertIn(result.status, ("processed", "duplicate"))

    def records(self):
        with self.repo.transaction() as work:
            return [dict(row) for row in work.connection.execute("SELECT * FROM node_logs ORDER BY id")]

    def test_large_unicode_body_survives_restart_reordering_and_duplicate_delivery_as_one_event(self):
        body = "<html>authenticity_token=keep-exact&applicant=101\n" + ("\u0434\u0430\u0442\u0430\U0001f600" * 10000) + "</html>"
        with audit.context(trace_id=str(uuid4()), log_group="reschedule"):
            audit.http_body("booking_http_response", lambda: body, status=422)
        messages = self.pending()
        self.assertGreater(len(messages), 3)
        self.state.close()
        self.state = NodeState(self.root / "node.sqlite3", self.node, self.group)
        self.assertEqual(self.pending(), messages)
        self.deliver(messages[-1:])
        self.assertEqual(self.records(), [])
        self.repo.close()
        self.repo = Repository(self.root / "coordinator.sqlite3")
        self.service = Coordinator(self.repo, self.group, now=self.now)
        self.deliver(list(reversed(messages[:-1])))
        # A resend with a new message ID still cannot duplicate the complete log.
        self.deliver([Message.create(message.data, sent_at=self.now) for message in messages])
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0]["details"]), {"status": 422, "body": body})
        with self.repo.transaction() as work:
            self.assertEqual(work.connection.execute("SELECT COUNT(*) FROM log_body_parts").fetchone()[0], 0)

    def test_disabled_group_never_evaluates_or_records_body_even_on_errors(self):
        self.policy(False)
        factory = Mock(side_effect=AssertionError("must not read body"))
        audit.http_body("booking_http_request", factory)
        factory.assert_not_called()
        audit.event("booking_http_response", log_group="reschedule", level="error", body="private")
        self.assertEqual(self.pending(), [])
        local = [json.loads(line) for line in audit.LOG_PATH.read_text().splitlines()]
        self.assertFalse(any(row["event"] == "booking_http_request" for row in local))

    def test_real_booking_logs_all_form_fields_and_redirect_body_without_following(self):
        session = Mock()
        session.get.return_value = response(FORM)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        session.get.return_value = response(payload={"available_times": ["08:30"]})
        reply = response("<html>redirect body with fresh-token</html>", status=302)
        session.post.return_value = reply
        self.assertIsInstance(client.book(DAY, dry_run=False), BookingPending)
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        sent = session.post.call_args.kwargs["data"]
        self.deliver(self.pending())
        records = {row["event"]: json.loads(row["details"]) for row in self.records()}
        request_body = records["booking_http_request"]["body"]
        expected = requests.Request("POST", URL, data=sent).prepare().body
        self.assertEqual(request_body, expected)
        self.assertEqual([v for k, v in parse_qsl(request_body) if k == "appointments[applicant_ids][]"], ["101", "102"])
        self.assertIn(("authenticity_token", "fresh-token"), parse_qsl(request_body))
        self.assertEqual(records["booking_http_response"]["body"], reply.text)
        self.assertEqual(records["booking_http_response"]["status"], 302)
        self.assertEqual(session.get.call_count, 2)  # preparation and one time lookup

    def test_request_logged_before_timeout_and_body_capture_failure_does_not_abort(self):
        session = Mock()
        session.get.return_value = response(FORM)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        session.get.return_value = response(payload={"available_times": ["08:30"]})

        def timeout(*args, **kwargs):
            self.assertTrue(any(message.data.entries[0].event == "booking_http_request" for message in self.pending()))
            raise requests.Timeout()

        session.post.side_effect = timeout
        self.assertIsInstance(client.book(DAY, dry_run=False), BookingPending)
        names = [message.data.entries[0].event for message in self.pending()]
        self.assertIn("booking_http_request", names)
        self.assertNotIn("booking_http_response", names)
        self.assertEqual(audit.read_claim("123")["state"], "pending")
        with patch.object(audit, "event", side_effect=OSError("disk full")), self.assertLogs("usvisa.shared.audit", "WARNING"):
            audit.http_body("booking_http_response", lambda: "body")

    def test_unfinished_parts_and_complete_events_expire_after_seven_days(self):
        audit.http_body("booking_http_response", lambda: "x" * 100000)
        messages = self.pending()
        self.deliver(messages[:1])
        with self.repo.transaction() as work:
            self.assertEqual(work.connection.execute("SELECT COUNT(*) FROM log_body_parts").fetchone()[0], 1)
            prune(work, self.now + timedelta(days=8))
            self.assertEqual(work.connection.execute("SELECT COUNT(*) FROM log_body_parts").fetchone()[0], 0)
        self.deliver(messages)
        self.assertEqual(len(self.records()), 1)
        with self.repo.transaction() as work:
            prune(work, self.now + timedelta(days=8))
        self.assertEqual(self.records(), [])
