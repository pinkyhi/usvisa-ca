"""Durable diagnostics, legacy peers, trace correlation and portal isolation."""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import DAY, FORM, URL, response
from usvisa.coordinator.auth import create_user
from usvisa.coordinator.config import ServerConfig
from usvisa.coordinator.portal import create_app
from usvisa.coordinator.repository import Repository
from usvisa.coordinator.service import Coordinator
from usvisa.node.coordinated import Worker
from usvisa.node.state import NodeState
from usvisa.node.telemetry import Telemetry
from usvisa.protocol.messages import (
    GroupConfig, Heartbeat, LogEntry, Message, NodeConfig, NodeLogs, ScheduleUpdated, parse_message,
)
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient, BookingNotVerified, RateLimited


GROUP = GroupConfig(95, "Vancouver")


class TelemetryTests(IsolatedLogsTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.node = uuid4()
        self.state = NodeState(self.path / "node.sqlite3", self.node, GROUP)
        self.addCleanup(lambda: self.state.close())
        self.state.accept(Message.create(ScheduleUpdated(self.node, uuid4(), 1, self.now, 180,
                          date(2028, 1, 1), GROUP), sent_at=self.now), now=self.now)
        self.policy(True, ("reschedule",))

    def policy(self, verbose, groups):
        self.state.accept(Message.create(NodeConfig(self.node, uuid4(), verbose, groups), sent_at=self.now), now=self.now)

    def entries(self):
        with self.state.transaction() as db:
            messages = [parse_message(row[0]) for row in db.execute("SELECT body FROM telemetry_outbox ORDER BY rowid")]
        return [entry for message in messages for entry in message.data.entries]

    def test_protocol_roundtrip_preserves_policy_and_opt_in_without_changing_legacy_heartbeat(self):
        old = Message.create(Heartbeat(self.node, DAY, None), sent_at=self.now)
        self.assertNotIn("loggingVersion", old.to_dict()["data"])
        modern = Message.create(Heartbeat(self.node, DAY, None, uuid4(), True), sent_at=self.now)
        self.assertTrue(parse_message(modern.to_json()).data.supports_logging)
        for data in (NodeConfig(self.node, uuid4(), True, ("reschedule",)),
                     NodeLogs(self.node, (LogEntry(uuid4(), self.now, "poll", "info", "poll_completed", None, None, {"count": 0}),))):
            message = Message.create(data, sent_at=self.now)
            self.assertEqual(parse_message(message.to_json()), message)

    def test_configuration_is_only_sent_to_opted_in_nodes_and_resyncs_on_version_mismatch(self):
        with Repository(self.path / "server.sqlite3") as repository:
            repository.register_node(self.node)
            repository.mark_ready(self.node)
            service = Coordinator(repository, GROUP, now=self.now, verbose=True, log_groups=("reschedule",))
            def heartbeat(offset, modern=False, version=None):
                sent = self.now + timedelta(seconds=offset)
                message = Message.create(Heartbeat(self.node, DAY, None, version, modern), sent_at=sent)
                service.handle(message, publisher=f"node_{self.node}", now=sent)
                pending = repository.pending(now=sent)
                for event in pending:
                    repository.mark_published(event.message.message_id)
                return [event.message.data for event in pending if isinstance(event.message.data, NodeConfig)]
            self.assertEqual(heartbeat(0), [])
            policy = heartbeat(30, True)[0]
            self.assertTrue(policy.verbose)
            self.assertEqual(policy.log_groups, ("reschedule",))
            self.assertEqual(heartbeat(60, True, policy.config_version), [])
            self.assertEqual(len(heartbeat(90, True, uuid4())), 1)

    def test_errors_are_forwarded_with_verbose_off_and_expected_429_is_not_a_serious_error(self):
        self.policy(False, ())
        remove = audit.subscribe(Telemetry(self.state))
        self.addCleanup(remove)
        audit.event("poll_completed", dates=[], count=0)
        audit.event("poll_completed", dates=[], count=0, level="warning", status="rate_limited")
        try:
            raise RuntimeError("password=not-for-logs and user@example.com")
        except RuntimeError as error:
            audit.exception("worker_exception", error)
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].category, "error")
        text = json.dumps(entries[0].details)
        self.assertNotIn("not-for-logs", text)
        self.assertNotIn("user@example.com", text)
        self.assertIn("test_errors_are_forwarded", text)

    def test_diagnostics_survive_restart_and_receipt_only_removes_confirmed_message(self):
        remove = audit.subscribe(Telemetry(self.state))
        try:
            audit.event("poll_completed", count=0, dates=[])
        finally:
            remove()
        first = self.state.pending_telemetry(self.now)[0][0]
        self.state.close()
        self.state = NodeState(self.path / "node.sqlite3", self.node, GROUP)
        self.assertEqual(self.state.pending_telemetry(self.now)[0][0], first)
        self.state.confirmed(uuid4())
        self.assertEqual(len(self.entries()), 1)
        self.state.confirmed(first.message_id)
        self.assertEqual(self.entries(), [])

    def test_failed_remote_log_persistence_does_not_interrupt_the_worker(self):
        remove = audit.subscribe(Telemetry(self.state))
        try:
            with patch.object(self.state, "add_telemetry", side_effect=OSError("disk full")), \
                 self.assertLogs("usvisa.shared.audit", level="WARNING"):
                audit.event("poll_completed", dates=[], count=0)
        finally:
            remove()

    def run_attempt(self, times_response):
        session = Mock()
        session.get.side_effect = [response(FORM), response(payload=[{"date": DAY.isoformat()}]), times_response]
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        worker = Worker(self.state, Mock(stopping=threading.Event()))
        remove = audit.subscribe(Telemetry(self.state))
        try:
            with patch.multiple("usvisa.node.coordinated.cfg", EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[], TEST_MODE=False), \
                 patch("usvisa.node.coordinated.solo.log_message"), patch.object(worker, "notify"):
                worker.poll(client)
        finally:
            remove()
        session.post.assert_not_called()
        return self.entries()

    def test_no_times_trace_contains_original_dates_filter_http_and_empty_times_without_secrets(self):
        entries = self.run_attempt(response(payload={"available_times": []}))
        names = [entry.event for entry in entries]
        for name in ("availability_response", "availability_filtered", "times_requested", "http_started", "times_response", "booking_no_times", "poll_completed"):
            self.assertIn(name, names)
        self.assertEqual(len({entry.trace_id for entry in entries}), 1)
        empty_times = next(entry for entry in entries if entry.event == "times_response")
        self.assertEqual(empty_times.details["times"], [])
        self.assertFalse(any(entry.level == "error" for entry in entries))
        self.assertNotIn("authenticity_token", json.dumps([entry.details for entry in entries]))

    def test_rate_limit_during_booking_is_warning_with_trace_and_not_a_serious_error(self):
        with self.assertRaises(RateLimited):
            self.run_attempt(response(status=429))
        entries = self.entries()
        self.assertFalse(any(entry.level == "error" for entry in entries))
        self.assertTrue(any(entry.event == "poll_completed" and entry.level == "warning" for entry in entries))

    def test_unverified_post_trace_includes_request_outcome_verification_and_exception(self):
        session = Mock()
        session.get.side_effect = [response(FORM), response(payload=[{"date": DAY.isoformat()}]),
                                  response(payload={"available_times": ["08:30"]})]
        session.post.return_value = response(status=422)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        worker = Worker(self.state, Mock(stopping=threading.Event()))
        remove = audit.subscribe(Telemetry(self.state))
        try:
            with patch.multiple("usvisa.node.coordinated.cfg", EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[], TEST_MODE=False), \
                 patch("usvisa.node.coordinated.solo.log_message"), patch.object(client, "verify_booking", return_value=False):
                with self.assertRaises(BookingNotVerified):
                    worker.poll(client)
        finally:
            remove()
        session.post.assert_called_once()
        entries = self.entries()
        for event in ("times_requested", "booking_prepared", "booking_post_started", "booking_post_outcome", "booking_verification", "operation_exception"):
            self.assertTrue(any(entry.event == event for entry in entries), event)
        self.assertEqual(len({entry.trace_id for entry in entries}), 1)
        self.assertTrue(any(entry.details.get("outcome") == "HTTP 422" for entry in entries))
        self.assertNotIn("fresh-token", json.dumps([entry.details for entry in entries]))


class LogPortalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repository = Repository(Path(tmp.name) / "server.sqlite3")
        self.addCleanup(self.repository.close)
        self.now = datetime.now(timezone.utc)
        self.nodes = {}
        for username in ("alice", "bob", "admin"):
            owner = create_user(self.repository, username, "fixture-password")
            node = uuid4()
            self.nodes[username] = node
            self.repository.register_node(node)
            self.repository.mark_ready(node)
            with self.repository.transaction() as work:
                work.connection.execute("INSERT INTO portal_nodes(node_id,owner_id,name) VALUES(?,?,?)", (str(node), owner, username + " computer"))
        self.service = Coordinator(self.repository, GROUP, now=self.now)
        config = ServerConfig(Path(tmp.name) / "server.sqlite3", "https://visa.example.com", GROUP,
                              "amqp://unused", "http://unused", "unused", "unused", "coordinator", "unused")
        self.client = TestClient(create_app(config, repository=self.repository, admin=AsyncMock(), run_broker=False), base_url=config.origin)
        self.addCleanup(self.client.close)
        self.trace = uuid4()
        self.add_log("alice")
        self.add_log("bob")

    def add_log(self, username, *, entry_id=None, category="poll", level="info", group=None, observed=None):
        entry = LogEntry(entry_id or uuid4(), observed or self.now, category, level, "poll_completed", self.trace, group,
                         {"dates": ["2027-06-23"], "count": 1})
        message = Message.create(NodeLogs(self.nodes[username], (entry,)), sent_at=self.now)
        result = self.service.handle(message, publisher=f"node_{self.nodes[username]}", now=self.now)
        self.assertEqual(result.status, "processed")
        return message

    def login(self, username):
        csrf = self.client.get("/api/v1/csrf").json()["csrfToken"]
        result = self.client.post("/api/v1/session", json={"username": username, "password": "fixture-password"},
                                  headers={"Origin": "https://visa.example.com", "X-CSRF-Token": csrf})
        self.assertEqual(result.status_code, 200)

    def test_unauthenticated_logs_require_login(self):
        self.assertEqual(self.client.get("/api/v1/logs").status_code, 401)
        self.assertEqual(self.client.get("/logs", follow_redirects=False).status_code, 303)

    def test_owner_cannot_read_other_nodes_logs_or_same_trace_on_another_node(self):
        self.login("alice")
        data = self.client.get("/api/v1/logs").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual({node["owner"] for node in data["nodes"]}, {"alice"})
        detail = self.client.get(f"/api/v1/logs/{data['events'][0]['id']}").json()
        self.assertEqual({event["owner"] for event in detail["trace"]}, {"alice"})
        self.assertEqual(self.client.get(f"/api/v1/logs?nodeId={self.nodes['bob']}").json()["total"], 0)
        with self.repository.transaction() as work:
            bob_id = work.connection.execute("SELECT id FROM node_logs WHERE node_id=?", (str(self.nodes["bob"]),)).fetchone()[0]
        self.assertEqual(self.client.get(f"/api/v1/logs/{bob_id}").status_code, 404)

    def test_admin_can_view_all_nodes_and_errors_are_separate_from_expected_retries(self):
        self.add_log("alice", category="error", level="error", group="reschedule")
        self.add_log("bob", level="warning")
        self.login("admin")
        self.assertTrue(self.client.get("/api/v1/me").json()["canViewAllLogs"])
        self.assertEqual(self.client.get("/api/v1/logs").json()["total"], 3)
        errors = self.client.get("/api/v1/logs?view=errors").json()
        self.assertEqual(errors["total"], 1)
        self.assertEqual(errors["events"][0]["level"], "error")
        self.assertEqual(self.client.get("/api/v1/logs?view=reschedule").json()["total"], 1)

    def test_duplicate_entries_are_idempotent_and_ids_are_scoped_to_authenticated_node(self):
        entry_id = uuid4()
        self.add_log("alice", entry_id=entry_id)
        self.add_log("alice", entry_id=entry_id)
        self.add_log("bob", entry_id=entry_id)
        with self.repository.transaction() as work:
            self.assertEqual(work.connection.execute("SELECT COUNT(*) FROM node_logs WHERE event_id=?", (str(entry_id),)).fetchone()[0], 2)

    def test_spoofed_node_identity_cannot_store_logs(self):
        entry = LogEntry(uuid4(), self.now, "error", "error", "worker_exception", None, None, {})
        message = Message.create(NodeLogs(self.nodes["bob"], (entry,)), sent_at=self.now)
        result = self.service.handle(message, publisher=f"node_{self.nodes['alice']}", now=self.now)
        self.assertEqual(result.reason, "NODE_ID_MISMATCH")

    def test_filters_and_page_cursor_are_validated(self):
        self.login("admin")
        first = self.client.get("/api/v1/logs?limit=1").json()
        second = self.client.get(f"/api/v1/logs?limit=1&before={first['nextCursor']}").json()
        self.assertNotEqual(first["events"][0]["id"], second["events"][0]["id"])
        for query in ("hours=721", "hours=0", "limit=1000", "view=unknown", "nodeId=invalid", "before=-1"):
            self.assertEqual(self.client.get("/api/v1/logs?" + query).status_code, 422)

    def test_delayed_delivery_is_sorted_by_observation_with_stable_pagination(self):
        self.add_log("alice", observed=self.now - timedelta(hours=2))
        self.login("admin")
        events, cursor = [], None
        for _ in range(3):
            query = "/api/v1/logs?limit=1" + (f"&before={cursor}" if cursor else "")
            page = self.client.get(query).json()
            events.extend(page["events"])
            cursor = page["nextCursor"]
        self.assertIsNone(cursor)
        self.assertEqual(len({event["id"] for event in events}), 3)
        self.assertLess(events[-1]["occurredAt"], events[0]["occurredAt"])
