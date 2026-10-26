"""Membership, scheduling, recipient filtering and crash recovery with real SQLite."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from usvisa.coordinator.repository import Repository, UnitOfWork
from usvisa.coordinator.scheduling import assign_phases
from usvisa.coordinator.service import Coordinator
from usvisa.protocol.messages import GroupConfig, Heartbeat, Message, SlotsFound


NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
CONFIG = GroupConfig(95, "Vancouver")
A, B, C, D = [UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(1, 5)]
EARLY = date(2027, 11, 1)
LATE = date(2027, 12, 1)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.repository = Repository(self.path)
        self.addCleanup(lambda: self.repository.close())
        self.service = Coordinator(self.repository, CONFIG, now=NOW)
        for node in (A, B, C, D):
            self.repository.register_node(node)
            self.repository.mark_ready(node)

    def heartbeat(self, node=A, *, day=EARLY, sent=NOW, received=None, version=None, message_id=None):
        message = Message.create(Heartbeat(node, day, version), sent_at=sent, message_id=message_id)
        result = self.service.handle(message, publisher=f"node_{node}", now=received or sent)
        return result, message

    def finding(self, *, node=A, observed=NOW, received=NOW, dates=(date(2027, 11, 15),), message_id=None):
        message = Message.create(SlotsFound(node, observed, dates), sent_at=received, message_id=message_id)
        return self.service.handle(message, publisher=f"node_{node}", now=received), message

    def active(self):
        return {node.node_id: node for node in self.repository.nodes() if node.active}

    def drain(self, now=NOW):
        pending = self.repository.pending(now=now)
        for item in pending:
            self.repository.mark_published(item.message.message_id)
        return pending

    def test_one_two_three_nodes_have_even_phases_and_personal_snapshots(self):
        for node, expected in [(A, [0]), (B, [0, 90]), (C, [0, 60, 120])]:
            self.heartbeat(node)
            records = self.active()
            starts = sorted((item.start_at - NOW).total_seconds() for item in records.values())
            self.assertEqual(starts, expected)
            state = self.repository.schedule()
            events = self.drain()
            self.assertEqual({event.node_id for event in events}, set(records))
            for item in events:
                data = item.message.data
                self.assertEqual((data.schedule_version, data.revision), (state.schedule_version, state.revision))
                self.assertEqual(data.node_id, item.node_id)
                self.assertEqual(data.interval_seconds, 180)
                self.assertNotIn("nodes", item.message.to_dict()["data"])
        self.assertEqual(self.repository.schedule().revision, 3)

    def test_threshold_change_preserves_all_anchors_and_uses_maximum(self):
        self.heartbeat(A)
        self.heartbeat(B)
        self.drain()
        before = self.active()
        old_state = self.repository.schedule()
        self.heartbeat(B, day=LATE, sent=NOW + timedelta(seconds=30))
        state = self.repository.schedule()
        self.assertEqual(state.report_before, LATE)
        self.assertEqual(state.revision, old_state.revision + 1)
        self.assertNotEqual(state.schedule_version, old_state.schedule_version)
        self.assertEqual({node: item.start_at for node, item in before.items()},
                         {node: item.start_at for node, item in self.active().items()})
        self.assertEqual({item.message.data.report_before for item in self.drain()}, {LATE})

    def test_personal_threshold_below_group_max_updates_routing_without_new_revision(self):
        self.heartbeat(A)
        self.heartbeat(B, day=LATE)
        state = self.repository.schedule()
        self.heartbeat(A, day=date(2027, 11, 20), sent=NOW + timedelta(seconds=30), version=state.schedule_version)
        self.assertEqual(self.repository.schedule(), state)
        self.finding(node=B, received=NOW + timedelta(seconds=31))
        findings = [item for item in self.drain() if item.exchange == "visa.slots"]
        self.assertEqual([item.node_id for item in findings], [A])

    def test_matching_heartbeat_has_no_reply_and_mismatched_version_gets_one_snapshot(self):
        self.heartbeat()
        state = self.repository.schedule()
        self.drain()
        self.heartbeat(sent=NOW + timedelta(seconds=30), version=state.schedule_version)
        self.assertEqual(self.drain(), [])
        _, command = self.heartbeat(sent=NOW + timedelta(seconds=60), version=uuid4())
        events = self.drain()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].message.in_reply_to, command.message_id)
        self.assertEqual(events[0].message.data.revision, state.revision)

    def test_presence_expires_at_540_seconds_and_survivor_keeps_anchor(self):
        self.heartbeat(A)
        self.heartbeat(B)
        self.heartbeat(C)
        self.drain()
        before = self.active()
        self.heartbeat(B, sent=NOW + timedelta(seconds=510))
        self.heartbeat(C, sent=NOW + timedelta(seconds=510))
        self.assertFalse(self.service.expire(now=NOW + timedelta(seconds=539)))
        self.assertTrue(self.service.expire(now=NOW + timedelta(seconds=540)))
        active = self.active()
        self.assertEqual(set(active), {B, C})
        self.assertEqual(active[B].start_at, before[B].start_at)
        self.assertEqual((active[C].start_at - active[B].start_at).total_seconds(), 90)
        a = next(node for node in self.repository.nodes() if node.node_id == A)
        self.assertEqual(a.lifecycle, "ready")
        self.assertIsNone(a.start_at)
        self.heartbeat(A, sent=NOW + timedelta(seconds=541))
        starts = sorted(node.start_at for node in self.active().values())
        self.assertEqual([(start - starts[0]).total_seconds() for start in starts], [0, 60, 120])

    def test_all_nodes_can_expire_and_a_fresh_heartbeat_starts_a_new_grid(self):
        self.heartbeat()
        self.service.expire(now=NOW + timedelta(seconds=540))
        self.assertEqual(self.active(), {})
        self.assertIsNone(self.repository.schedule().report_before)
        later = NOW + timedelta(seconds=600)
        self.heartbeat(sent=later)
        self.assertEqual(self.active()[A].start_at, later)
        self.assertEqual(self.repository.schedule().revision, 3)

    def test_stale_and_out_of_order_heartbeats_cannot_restore_presence_or_change_threshold(self):
        self.heartbeat(sent=NOW + timedelta(seconds=30))
        stale, _ = self.heartbeat(day=LATE, sent=NOW, received=NOW + timedelta(seconds=61))
        older, _ = self.heartbeat(day=LATE, sent=NOW + timedelta(seconds=20), received=NOW + timedelta(seconds=40))
        self.assertEqual((stale.reason, older.reason), ("STALE_HEARTBEAT", "OLDER_HEARTBEAT"))
        node = self.active()[A]
        self.assertEqual(node.min_date, EARLY)
        self.assertEqual(node.last_seen_at, NOW + timedelta(seconds=30))
        self.service.expire(now=NOW + timedelta(seconds=570))
        stale, _ = self.heartbeat(sent=NOW + timedelta(seconds=500), received=NOW + timedelta(seconds=571))
        self.assertEqual(stale.status, "ignored")
        self.assertEqual(self.active(), {})

    def test_last_seen_uses_sent_time_and_future_skew_does_not_allow_rollback(self):
        self.heartbeat(sent=NOW, received=NOW + timedelta(seconds=59))
        self.assertEqual(self.active()[A].last_seen_at, NOW)
        self.heartbeat(day=LATE, sent=NOW + timedelta(seconds=62), received=NOW + timedelta(seconds=60))
        self.assertEqual(self.active()[A].last_seen_at, NOW + timedelta(seconds=60))
        result, _ = self.heartbeat(sent=NOW + timedelta(seconds=61), received=NOW + timedelta(seconds=61))
        self.assertEqual(result.reason, "OLDER_HEARTBEAT")
        self.assertEqual(self.active()[A].min_date, LATE)
        result, _ = self.heartbeat(sent=NOW + timedelta(seconds=100), received=NOW + timedelta(seconds=90))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.active()[A].last_seen_at, NOW + timedelta(seconds=60))

    def test_duplicate_heartbeat_does_not_extend_presence_or_repeat_events(self):
        _, original = self.heartbeat()
        self.drain()
        result = self.service.handle(original, publisher=f"node_{A}", now=NOW + timedelta(seconds=30))
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(self.active()[A].last_seen_at, NOW)
        self.assertEqual(self.drain(), [])

    def test_heartbeat_age_boundary_and_maximum_clock_skew(self):
        result, _ = self.heartbeat(sent=NOW, received=NOW + timedelta(seconds=60))
        self.assertEqual(result.reason, "STALE_HEARTBEAT")
        self.assertEqual(self.active(), {})
        result, _ = self.heartbeat(sent=NOW + timedelta(seconds=65), received=NOW + timedelta(seconds=60))
        self.assertEqual(result.status, "processed")
        self.assertEqual(self.active()[A].last_seen_at, NOW + timedelta(seconds=60))

    def test_findings_are_direct_and_use_recipient_threshold_not_source_threshold(self):
        self.heartbeat(A, day=EARLY)
        self.heartbeat(B, day=LATE)
        self.heartbeat(C, day=date(2027, 11, 15))
        self.drain()
        result, command = self.finding(dates=(date(2027, 11, 15), LATE, date(2027, 10, 31)))
        self.assertEqual(result.status, "processed")
        events = self.drain()
        self.assertEqual({event.node_id for event in events}, {B, C})
        by_node = {event.node_id: event for event in events}
        self.assertEqual(by_node[B].message.data.dates, (date(2027, 10, 31), date(2027, 11, 15)))
        self.assertEqual(by_node[C].message.data.dates, (date(2027, 10, 31),))
        for event in events:
            self.assertEqual(event.exchange, "visa.slots")
            self.assertEqual(event.routing_key, str(event.node_id))
            self.assertEqual(event.message.data.finding_id, command.message_id)
            self.assertEqual(event.message.data.source_node_id, A)
            self.assertNotEqual(event.message.message_id, command.message_id)
            self.assertIsNone(event.message.in_reply_to)

    def test_empty_recipient_match_sends_no_slots_accepted_or_other_application_reply(self):
        self.heartbeat(A)
        self.heartbeat(B)
        self.drain()
        self.finding()
        self.assertEqual(self.drain(), [])

    def test_findings_exclude_expired_members_and_do_not_extend_presence(self):
        self.heartbeat(A)
        self.heartbeat(B, day=LATE)
        self.heartbeat(C, day=LATE)
        self.heartbeat(C, day=LATE, sent=NOW + timedelta(seconds=510))
        self.drain(now=NOW + timedelta(seconds=540))
        later = NOW + timedelta(seconds=540)
        self.finding(observed=later, received=later)
        events = self.drain(now=later)
        findings = [event for event in events if event.exchange == "visa.slots"]
        self.assertEqual([event.node_id for event in findings], [C])
        self.assertEqual(set(self.active()), {C})

    def test_findings_expire_at_original_observation_time_across_retries(self):
        self.heartbeat(A)
        self.heartbeat(B, day=LATE)
        self.drain()
        delayed = NOW + timedelta(seconds=120)
        _, command = self.finding(received=delayed)
        first = self.repository.pending(now=delayed)[0]
        retry = self.repository.pending(now=NOW + timedelta(seconds=150))[0]
        self.assertEqual(first.message, retry.message)
        self.assertEqual(first.expiration_ms(delayed), 60_000)
        self.assertEqual(retry.expiration_ms(NOW + timedelta(seconds=150)), 30_000)
        duplicate = self.service.handle(command, publisher=f"node_{A}", now=NOW + timedelta(seconds=150))
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(self.repository.pending(now=NOW + timedelta(seconds=180)), [])
        self.assertEqual(first.expiration_ms(NOW + timedelta(seconds=181)), 0)
        result, _ = self.finding(received=NOW + timedelta(seconds=180))
        self.assertEqual(result.reason, "EXPIRED_FINDING")

    def test_future_observation_returns_safe_correlated_error_to_authenticated_sender(self):
        self.heartbeat(A)
        self.drain()
        result, command = self.finding(observed=NOW + timedelta(seconds=6))
        self.assertEqual(result.reason, "INVALID_OBSERVED_AT")
        event = self.drain()[0]
        self.assertEqual(event.message.type, "error")
        self.assertEqual(event.message.in_reply_to, command.message_id)
        self.assertEqual(event.node_id, A)

    def test_unprovisioned_revoked_and_spoofed_publishers_cannot_join(self):
        unknown = uuid4()
        message = Message.create(Heartbeat(unknown, LATE, None), sent_at=NOW)
        result = self.service.handle(message, publisher=f"node_{unknown}", now=NOW)
        self.assertEqual(result.status, "rejected")
        self.repository.register_node(unknown)
        result = self.service.handle(message, publisher=f"node_{unknown}", now=NOW)
        self.assertEqual(result.status, "rejected")
        spoof = Message.create(Heartbeat(B, LATE, None), sent_at=NOW)
        result = self.service.handle(spoof, publisher=f"node_{A}", now=NOW)
        self.assertEqual(result.reason, "NODE_ID_MISMATCH")
        self.assertEqual(self.drain()[0].node_id, A)
        self.assertEqual(self.active(), {})
        self.service.revoke(A, now=NOW)
        result, _ = self.heartbeat(A)
        self.assertEqual(result.reason, "NODE_REVOKED")
        with self.assertRaises(ValueError):
            self.repository.mark_ready(A)

    def test_revoke_rebuilds_immediately_and_cancels_pending_recipient_events(self):
        self.heartbeat(A)
        self.heartbeat(B, day=LATE)
        self.finding()
        old_revision = self.repository.schedule().revision
        self.service.revoke(B, now=NOW + timedelta(seconds=1))
        self.assertEqual(set(self.active()), {A})
        self.assertEqual(self.repository.schedule().revision, old_revision + 1)
        self.assertEqual(self.repository.schedule().report_before, EARLY)
        self.assertEqual({event.node_id for event in self.drain()}, {A})

    def test_restart_preserves_versions_presence_and_pending_event_identifiers(self):
        self.heartbeat(A)
        state = self.repository.schedule()
        events = self.repository.pending(now=NOW)
        self.repository.close()
        self.repository = Repository(self.path)
        self.service = Coordinator(self.repository, CONFIG, now=NOW + timedelta(seconds=30))
        self.assertEqual(self.repository.schedule(), state)
        self.assertEqual(self.repository.pending(now=NOW + timedelta(seconds=30)), events)
        self.assertEqual(self.active()[A].last_seen_at, NOW)
        self.service = Coordinator(self.repository, CONFIG, now=NOW + timedelta(seconds=540))
        self.assertEqual(self.active(), {})
        self.assertEqual(self.repository.schedule().revision, state.revision + 1)

    def test_restart_preserves_findings_deduplication_and_absolute_expiry(self):
        self.heartbeat(A)
        self.heartbeat(B, day=LATE)
        self.drain()
        _, command = self.finding()
        event = self.repository.pending(now=NOW)[0]
        self.repository.close()
        self.repository = Repository(self.path)
        self.service = Coordinator(self.repository, CONFIG, now=NOW + timedelta(seconds=120))
        result = self.service.handle(command, publisher=f"node_{A}", now=NOW + timedelta(seconds=120))
        self.assertEqual(result.status, "duplicate")
        restored = self.repository.pending(now=NOW + timedelta(seconds=120))
        self.assertEqual(restored, [event])
        self.assertEqual(restored[0].expiration_ms(NOW + timedelta(seconds=120)), 60_000)
        self.assertEqual(self.repository.pending(now=NOW + timedelta(seconds=180)), [])

    def test_configuration_change_on_restart_creates_version_without_moving_phases(self):
        self.heartbeat(A)
        self.heartbeat(B)
        before = self.active()
        revision = self.repository.schedule().revision
        self.drain()
        changed = GroupConfig(94, "Toronto")
        self.service = Coordinator(self.repository, changed, now=NOW + timedelta(seconds=1))
        self.assertEqual(self.repository.schedule().revision, revision + 1)
        self.assertEqual({node: item.start_at for node, item in self.active().items()},
                         {node: item.start_at for node, item in before.items()})
        self.assertEqual({event.message.data.config for event in self.drain()}, {changed})

    def test_failed_outbox_write_rolls_back_membership_version_and_inbox_together(self):
        original = self.repository.view()
        message = Message.create(Heartbeat(A, EARLY, None), sent_at=NOW)
        with self.assertNoLogs("usvisa.coordinator", level="INFO"), \
                patch.object(UnitOfWork, "enqueue", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.service.handle(message, publisher=f"node_{A}", now=NOW)
        self.assertEqual(self.repository.view(), original)
        self.assertEqual(self.drain(), [])
        with self.assertLogs("usvisa.coordinator", level="INFO") as recorded:
            result = self.service.handle(message, publisher=f"node_{A}", now=NOW)
        self.assertTrue(any("Heartbeat received" in line for line in recorded.output))
        self.assertTrue(any("Schedule updated" in line for line in recorded.output))
        self.assertEqual(result.status, "processed")
        self.assertEqual(self.repository.schedule().revision, 1)

    def test_finding_and_inbox_are_rolled_back_if_recipient_event_cannot_be_saved(self):
        self.heartbeat(A)
        self.heartbeat(B, day=LATE)
        self.drain()
        message = Message.create(SlotsFound(A, NOW, (date(2027, 11, 15),)), sent_at=NOW)
        with patch.object(UnitOfWork, "enqueue", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.service.handle(message, publisher=f"node_{A}", now=NOW)
        with self.repository.transaction() as work:
            self.assertEqual(work.connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0], 0)
        result = self.service.handle(message, publisher=f"node_{A}", now=NOW)
        self.assertEqual(result.status, "processed")
        self.assertEqual(len(self.drain()), 1)

    def test_separate_repository_connections_observe_committed_deduplication(self):
        _, command = self.heartbeat()
        with Repository(self.path) as other:
            service = Coordinator(other, CONFIG, now=NOW)
            result = service.handle(command, publisher=f"node_{A}", now=NOW)
            self.assertEqual(result.status, "duplicate")
            self.assertEqual(other.schedule(), self.repository.schedule())


class PhaseAllocationTests(unittest.TestCase):
    def test_anchor_and_relative_cyclic_order_survive_membership_changes(self):
        previous = {B: NOW, C: NOW + timedelta(seconds=60), A: NOW + timedelta(seconds=120)}
        assigned = assign_phases({A, C, D}, previous, now=NOW + timedelta(days=1))
        self.assertEqual(list(assigned), [C, A, D])
        self.assertEqual(assigned[C], previous[C])
        self.assertEqual((assigned[A] - assigned[C]).total_seconds(), 60)

    def test_many_nodes_fill_one_period_without_cumulative_rounding_drift(self):
        nodes = {UUID(int=index) for index in range(1, 8)}
        assigned = assign_phases(nodes, {}, now=NOW)
        starts = sorted(assigned.values())
        gaps = [(right - left).total_seconds() for left, right in zip(starts, starts[1:])]
        gaps.append((starts[0] + timedelta(seconds=180) - starts[-1]).total_seconds())
        self.assertAlmostEqual(sum(gaps), 180)
        self.assertLessEqual(max(gaps) - min(gaps), .0000011)
        self.assertEqual(assign_phases(set(), assigned, now=NOW), {})


class CoordinatorCommandTests(unittest.TestCase):
    def test_init_and_state_work_without_browser_broker_or_visa_credentials(self):
        root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root / "src")
        environment["USVISA_ENV_FILE"] = str(root / "tests" / "missing.env")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            command = [sys.executable, "-m", "usvisa.coordinator", "--database", str(database)]
            initialized = subprocess.run(command + ["init-db", "--consulate-id", "95", "--city", "Vancouver"],
                                         env=environment, cwd=directory, capture_output=True, text=True, timeout=15)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            inspected = subprocess.run(command + ["state"], env=environment, cwd=directory,
                                       capture_output=True, text=True, timeout=15)
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(initialized.stdout), json.loads(inspected.stdout))
            self.assertEqual(json.loads(inspected.stdout)["config"], CONFIG.to_dict())
