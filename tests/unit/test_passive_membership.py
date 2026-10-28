"""Passive membership participates in timing, never in demand or deliveries."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from usvisa.coordinator.repository import Repository
from usvisa.coordinator.service import Coordinator
from usvisa.node.state import NodeState
from usvisa.protocol.messages import GroupConfig, Heartbeat, Message, ProtocolError, SlotsFound, parse_message
from usvisa.transport.webstomp import WebStomp


class PassiveMembershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.repo = Repository(self.path / "server.sqlite3")
        self.addCleanup(lambda: self.repo.close())
        self.group = GroupConfig(95, "Vancouver")
        self.now = datetime.now(timezone.utc)
        self.service = Coordinator(self.repo, self.group, now=self.now)
        self.a, self.b = uuid4(), uuid4()
        for node in (self.a, self.b):
            self.repo.register_node(node)
            self.repo.mark_ready(node)

    def heartbeat(self, node, minimum=None, offset=0, passive=True):
        sent = self.now + timedelta(seconds=offset)
        message = Message.create(Heartbeat(node, minimum, None, passive=passive), sent_at=sent)
        self.assertEqual(self.service.handle(message, publisher=f"node_{node}", now=sent).status, "processed")
        return message

    def finding(self, node, offset=1):
        sent = self.now + timedelta(seconds=offset)
        message = Message.create(SlotsFound(node, sent, (date(2027, 11, 15),)), sent_at=sent)
        self.service.handle(message, publisher=f"node_{node}", now=sent)
        return message

    def test_passive_only_group_has_schedule_without_demand_and_can_later_become_active(self):
        message = self.heartbeat(self.a)
        self.assertNotIn("minDate", message.to_dict()["data"])
        self.assertEqual(self.repo.schedule().report_before, None)
        snapshots = [item.message for item in self.repo.pending(now=self.now)]
        self.assertEqual(len(snapshots), 1)
        self.assertIsNone(parse_message(snapshots[0].to_json()).data.report_before)
        self.assertIsNotNone(next(node for node in self.repo.nodes() if node.node_id == self.a).start_at)
        self.heartbeat(self.b, date(2027, 12, 1), passive=False)
        self.assertEqual(self.repo.schedule().report_before, date(2027, 12, 1))
        self.assertEqual(len({node.start_at for node in self.repo.nodes()}), 2)
        self.finding(self.a)
        recipients = {item.node_id for item in self.repo.pending(now=self.now) if item.exchange == "visa.slots"}
        self.assertEqual(recipients, {self.b})

    def test_transition_removes_old_demand_cancels_pending_slots_and_survives_restart(self):
        self.heartbeat(self.a, date(2027, 10, 1), passive=False)
        self.heartbeat(self.b, date(2028, 1, 1), passive=False)
        finding = self.finding(self.a)
        self.assertTrue(any(item.message.data.finding_id == finding.message_id for item in
                            self.repo.pending(now=self.now) if item.exchange == "visa.slots"))
        self.heartbeat(self.b, offset=2)
        self.assertEqual(self.repo.schedule().report_before, date(2027, 10, 1))
        self.assertFalse(any(item.exchange == "visa.slots" for item in self.repo.pending(now=self.now)))
        before = self.repo.schedule()
        self.repo.close()
        self.repo = Repository(self.path / "server.sqlite3")
        self.service = Coordinator(self.repo, self.group, now=self.now + timedelta(seconds=3))
        self.assertEqual(self.repo.schedule(), before)
        self.assertTrue(next(node for node in self.repo.nodes() if node.node_id == self.b).passive)
        self.heartbeat(self.b, date(2027, 12, 1), offset=4, passive=False)
        self.assertEqual(self.repo.schedule().report_before, date(2027, 12, 1))

    def test_legacy_active_heartbeat_still_requires_minimum_and_passive_cannot_include_one(self):
        packet = Message.create(Heartbeat(self.a, date(2027, 12, 1), None), sent_at=self.now).to_dict()
        del packet["data"]["minDate"]
        with self.assertRaises(ProtocolError):
            parse_message(json.dumps(packet))
        packet["data"].update(passive=True, minDate="2027-12-01")
        with self.assertRaises(ProtocolError):
            parse_message(json.dumps(packet))
        packet["data"].pop("minDate")
        packet["data"]["passive"] = "true"
        with self.assertRaises(ProtocolError):
            parse_message(json.dumps(packet))

    def test_transport_omits_demand_dynamically_and_restores_it_after_manual_reset(self):
        state = NodeState(self.path / "node.sqlite3", self.a, self.group)
        self.addCleanup(state.close)
        passive = [True]
        transport = WebStomp("http://localhost:8187", f"{self.a}." + "a" * 43,
                             date(2027, 12, 1), state, allow_local=True, passive_provider=lambda: passive[0])
        self.assertNotIn("minDate", transport.heartbeat().to_dict()["data"])
        passive[0] = False
        self.assertEqual(transport.heartbeat().data.min_date, date(2027, 12, 1))
        self.assertNotIn("passive", transport.heartbeat().to_dict()["data"])

    def test_schema_eight_upgrade_preserves_history_and_enables_null_passive_minimum(self):
        self.heartbeat(self.a, date(2027, 12, 1), passive=False)
        self.finding(self.a)
        with self.repo.transaction() as work:
            work.connection.execute("PRAGMA user_version=8")
        self.repo.close()
        self.repo = Repository(self.path / "server.sqlite3")
        self.service = Coordinator(self.repo, self.group, now=self.now + timedelta(seconds=2))
        self.heartbeat(self.a, offset=3)
        with self.repo.transaction() as work:
            self.assertIsNone(work.connection.execute("PRAGMA foreign_key_check").fetchone())
            self.assertEqual(work.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(work.connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0], 1)
            self.assertEqual(work.node(self.a).min_date, None)
