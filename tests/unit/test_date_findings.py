"""Date-only sharing performs no time discovery; recipients resolve their times."""
import json
import threading
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from tests.unit.log_helpers import IsolatedLogsTest
from tests.unit.test_booking import DAY, FORM, URL, response
from usvisa.coordinator.repository import Repository
from usvisa.coordinator.service import Coordinator
from usvisa.node.coordinated import Worker
from usvisa.node.state import NodeState
from usvisa.protocol.messages import (
    AppointmentSlot, GroupConfig, Heartbeat, Message, ProtocolError,
    ScheduleUpdated, SlotsAvailable, SlotsFound, parse_message,
)
from usvisa.shared import audit
from usvisa.visa.appointment_client import AppointmentClient


GROUP = GroupConfig(95, "Vancouver")


class DateFindingsTests(IsolatedLogsTest):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc)
        self.node = uuid4()
        self.state = NodeState(self.test_log_directory / "node.sqlite3", self.node, GROUP)
        self.addCleanup(self.state.close)
        self.state.accept(Message.create(ScheduleUpdated(self.node, uuid4(), 1, self.now,
            180, date(2028, 1, 1), GROUP), sent_at=self.now), now=self.now)
        self.worker = Worker(self.state, Mock(stopping=threading.Event()))
        self.addCleanup(self.worker.close)
        for patcher in (patch.multiple("usvisa.node.coordinated.cfg", TEST_MODE=False,
                            EARLIEST_ACCEPTABLE_DATE="2026-11-01", LATEST_ACCEPTABLE_DATE="2026-12-01",
                            EXCLUSION_DATE_RANGES=[], DATE_REQUEST_CYCLE_LENGTH=0),
                        patch("usvisa.node.coordinated.solo.log_message"),
                        patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live HTTP"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def findings(self):
        return [message.data for message, _ in self.state.pending(self.now)]

    def prepared_client(self):
        session = Mock()
        session.get.return_value = response(FORM)
        client = AppointmentClient(session, URL, 95, "Vancouver")
        client.check_session()
        session.get.reset_mock(side_effect=True, return_value=True)
        return client, session

    def test_many_other_dates_require_one_dates_get_and_no_times_requests(self):
        client, session = self.prepared_client()
        days = tuple(date(2027, 1, 1) + timedelta(days=index) for index in range(100))
        session.get.return_value = response(payload=[{"date": day.isoformat()} for day in days])
        with patch.object(client, "verification_client") as clone:
            self.worker.poll(client)
        session.get.assert_called_once()
        self.assertIn("/days/", session.get.call_args.args[0])
        session.post.assert_not_called()
        clone.assert_not_called()
        self.assertEqual(len(self.findings()), 1)
        self.assertEqual(self.findings()[0].dates, days)
        self.assertEqual(self.findings()[0].slots, ())
        self.assertEqual(self.findings()[0].excluded_slots, ())
        self.assertTrue(self.state.outbox_ready.is_set())

    def test_all_dates_including_own_are_shared_before_own_times_get(self):
        client, session = self.prepared_client()
        other = date(2027, 1, 1)

        def get(*args, **kwargs):
            self.assertEqual(self.findings()[0].dates, (DAY, other))
            return response(payload={"available_times": ["08:30", "10:00", "11:00"]})

        def post(*args, **kwargs):
            own = next(data for data in self.findings() if DAY in data.dates)
            self.assertEqual(own.excluded_slots, ())
            self.assertEqual(own.slots, ())
            self.assertEqual(audit.read_claim("123")["state"], "pending")
            self.assertEqual(dict(kwargs["data"])["appointments[consulate_appointment][time]"], "10:00")
            return response(status=302)

        session.get.side_effect = get
        session.post.side_effect = post
        with patch("usvisa.visa.appointment_client.random.choice", return_value="10:00"):
            self.worker.try_dates(client, [DAY, other], observed=self.now)
            self.worker.try_dates(client, [DAY])
        session.post.assert_called_once()
        session.get.assert_called_once()

    def test_receiver_selects_from_all_current_times_and_posts_once(self):
        client, session = self.prepared_client()
        session.get.return_value = response(payload={"available_times": ["08:30", "10:00", "11:00"]})
        session.post.return_value = response(status=302)
        task = Message.create(SlotsAvailable(uuid4(), uuid4(), self.now, self.now + timedelta(seconds=180),
            (DAY,), excluded_slots=(AppointmentSlot(DAY, "10:00"),)), sent_at=self.now)
        with patch("usvisa.visa.appointment_client.random.choice", side_effect=lambda values: values[-1]) as choose:
            self.worker.shared_task(client, task)
            self.worker.shared_task(client, task)
        choose.assert_called_once_with(["08:30", "10:00", "11:00"])
        session.get.assert_called_once()
        self.assertIn("/times/", session.get.call_args.args[0])
        session.post.assert_called_once()
        self.assertEqual(dict(session.post.call_args.kwargs["data"])["appointments[consulate_appointment][time]"], "11:00")
        self.assertEqual(self.findings(), [])  # Incoming findings never bounce back.

    def test_legacy_exclusion_does_not_hide_a_currently_available_time(self):
        client, session = self.prepared_client()
        session.get.return_value = response(payload={"available_times": ["10:00"]})
        task = Message.create(SlotsAvailable(uuid4(), uuid4(), self.now, self.now + timedelta(seconds=180),
            (DAY,), excluded_slots=(AppointmentSlot(DAY, "10:00"),)), sent_at=self.now)
        session.post.return_value = response(status=302)
        self.worker.shared_task(client, task)
        session.get.assert_called_once()
        session.post.assert_called_once()
        self.assertEqual(dict(session.post.call_args.kwargs["data"])["appointments[consulate_appointment][time]"], "10:00")
        self.assertEqual(audit.read_claim("123")["state"], "pending")

    def test_pending_and_confirmed_keep_reporting_same_date_without_time_gets(self):
        client, session = self.prepared_client()
        audit.claim("123", DAY, "10:00", "Vancouver")
        for status in ("pending", "confirmed"):
            audit.update_claim("123", state=status)
            self.worker.guard_checked = None
            self.worker.try_dates(client, [DAY], observed=self.now)
        self.assertEqual(len(self.findings()), 2)
        for data in self.findings():
            self.assertEqual(data.dates, (DAY,))
            self.assertEqual(data.excluded_slots, ())
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_expired_out_of_range_and_excluded_dates_do_not_trigger_http(self):
        client, session = self.prepared_client()
        self.worker.try_dates(client, [DAY], expires=self.now - timedelta(seconds=1))
        self.worker.try_dates(client, [date(2027, 2, 1)])
        with patch("usvisa.node.coordinated.cfg.EXCLUSION_DATE_RANGES", [(DAY.isoformat(), DAY.isoformat())]):
            self.worker.try_dates(client, [DAY])
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_protocol_validates_exclusions_and_round_trips_old_date_messages(self):
        excluded = AppointmentSlot(DAY, "10:00")
        message = Message.create(SlotsFound(self.node, self.now, (DAY,), excluded_slots=(excluded,)), sent_at=self.now)
        self.assertEqual(parse_message(message.to_json()), message)
        self.assertNotIn("slots", message.to_dict()["data"])
        with self.assertRaises(ProtocolError):
            Message.create(SlotsFound(self.node, self.now, slots=(excluded,),
                                     excluded_slots=(excluded,)), sent_at=self.now)
        for changes in ({"time": "24:00"}, {"date": "2026-11-13"}):
            body = message.to_dict()
            body["data"]["excludedSlots"][0].update(changes)
            with self.assertRaises(ProtocolError):
                parse_message(json.dumps(body))
        legacy = Message.create(SlotsFound(self.node, self.now, (DAY,)), sent_at=self.now)
        self.assertNotIn("excludedSlots", legacy.to_dict()["data"])
        self.assertEqual(parse_message(legacy.to_json()), legacy)
        heartbeat = Message.create(Heartbeat(self.node, DAY, None, date_exclusions=True), sent_at=self.now)
        self.assertEqual(parse_message(heartbeat.to_json()), heartbeat)
        body = heartbeat.to_dict()
        body["data"]["dateExclusions"] = 1
        with self.assertRaises(ProtocolError):
            parse_message(json.dumps(body))

    def test_server_sends_all_matching_dates_to_every_peer_without_time_metadata(self):
        path = self.test_log_directory / "server.sqlite3"
        peers = [self.node, uuid4(), uuid4(), uuid4()]
        other, reserved = date(2027, 6, 1), AppointmentSlot(DAY, "10:00")
        with Repository(path) as repository:
            service = Coordinator(repository, GROUP, now=self.now)
            for index, peer in enumerate(peers):
                repository.register_node(peer)
                repository.mark_ready(peer)
                heartbeat = Message.create(Heartbeat(peer, date(2027, 1 if index == 2 else 12, 1), None,
                    date_exclusions=index != 3), sent_at=self.now)
                service.handle(heartbeat, publisher=f"node_{peer}", now=self.now)
            for entry in repository.pending(now=self.now):
                repository.mark_published(entry.message.message_id)
            command = Message.create(SlotsFound(self.node, self.now, (DAY, other),
                excluded_slots=(reserved,)), sent_at=self.now)
            service.handle(command, publisher=f"node_{self.node}", now=self.now)
            pending = repository.pending(now=self.now)
            by_node = {entry.node_id: entry.message.data for entry in pending}
            self.assertEqual(by_node[peers[1]].dates, (DAY, other))
            self.assertEqual(by_node[peers[1]].excluded_slots, ())
            self.assertEqual(by_node[peers[2]].dates, (DAY,))
            self.assertEqual(by_node[peers[2]].excluded_slots, ())
            self.assertEqual(by_node[peers[3]].dates, (DAY, other))
            self.assertEqual(by_node[peers[3]].excluded_slots, ())
            with repository.transaction() as work:
                saved = work.connection.execute("SELECT dates, excluded_slots FROM findings").fetchone()
            self.assertEqual(json.loads(saved["dates"]), [DAY.isoformat(), other.isoformat()])
            self.assertEqual(json.loads(saved["excluded_slots"]), [reserved.to_dict()])
        with Repository(path) as repository:
            self.assertEqual(repository.pending(now=self.now), pending)
            self.assertTrue(next(node for node in repository.nodes() if node.node_id == peers[1]).date_exclusions)
            service = Coordinator(repository, GROUP, now=self.now)
            self.assertEqual(service.handle(command, publisher=f"node_{self.node}", now=self.now).status, "duplicate")
            self.assertEqual(repository.pending(now=self.now + timedelta(seconds=180)), [])

    def test_schema_six_upgrade_preserves_prior_findings_and_capabilities(self):
        path = self.test_log_directory / "migration.sqlite3"
        with Repository(path) as repository:
            repository.register_node(self.node)
            legacy = Message.create(SlotsFound(self.node, self.now, (DAY,)), sent_at=self.now)
            with repository.transaction() as work:
                work.record(self.node, legacy, "processed", None, self.now)
                work.finding(self.node, legacy, self.now + timedelta(seconds=180))
                work.connection.execute("ALTER TABLE nodes DROP COLUMN date_exclusions")
                work.connection.execute("ALTER TABLE findings DROP COLUMN excluded_slots")
                work.connection.execute("PRAGMA user_version=6")
        with Repository(path) as repository:
            self.assertFalse(repository.nodes()[0].date_exclusions)
            with repository.transaction() as work:
                self.assertEqual(work.connection.execute("PRAGMA user_version").fetchone()[0], 9)
                record = work.connection.execute("SELECT dates, excluded_slots FROM findings").fetchone()
                self.assertEqual(json.loads(record["dates"]), [DAY.isoformat()])
                self.assertEqual(json.loads(record["excluded_slots"]), [])

    def test_saved_ready_pair_from_previous_version_also_uses_fresh_times(self):
        client, session = self.prepared_client()
        session.post.return_value = response(status=302)
        task = Message.create(SlotsAvailable(uuid4(), uuid4(), self.now, self.now + timedelta(seconds=180),
            slots=(AppointmentSlot(DAY, "08:30"),)), sent_at=self.now)
        session.get.return_value = response(payload={"available_times": ["10:00"]})
        self.worker.shared_task(client, task)
        session.get.assert_called_once()
        session.post.assert_called_once()
        self.assertEqual(dict(session.post.call_args.kwargs["data"])["appointments[consulate_appointment][time]"], "10:00")
