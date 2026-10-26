import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from usvisa.node.coordinated import Worker, next_check_at
from usvisa.node.state import InstanceLock, NodeState
from usvisa.protocol.messages import GroupConfig, Message, ProtocolError, ScheduleUpdated, SlotsAvailable
from usvisa.transport.stomp import Decoder, encode
from usvisa.transport.webstomp import parse_token
from usvisa.shared.network import server_origin, websocket_url
from usvisa.visa.appointment_client import BookingNotVerified, BookingResult


NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
GROUP = GroupConfig(95, "Vancouver")


class NodeStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.node_id = uuid4()
        self.state = NodeState(Path(self.directory.name) / "state.sqlite3", self.node_id, GROUP)
        self.addCleanup(lambda: self.state.close())

    def schedule(self, revision=1, version=None, **overrides):
        data = dict(node_id=self.node_id, schedule_version=version or uuid4(), revision=revision,
                    start_at=NOW, interval_seconds=180, report_before=date(2027, 12, 1), config=GROUP)
        data.update(overrides)
        return Message.create(ScheduleUpdated(**data), sent_at=NOW)

    def test_snapshots_are_monotonic_and_same_revision_does_not_replace_saved_message(self):
        newer = self.schedule(2)
        self.state.accept(newer, now=NOW)
        self.state.accept(self.schedule(1), now=NOW)
        self.assertEqual(self.state.snapshot(), newer)
        repeat = self.schedule(2, newer.data.schedule_version)
        self.state.accept(repeat, now=NOW)
        self.assertEqual(self.state.snapshot(), newer)
        with self.assertRaises(ProtocolError):
            self.state.accept(self.schedule(2), now=NOW)

    def test_snapshot_cannot_change_identity_or_consulate(self):
        for changes in [{"node_id": uuid4()}, {"config": GroupConfig(94, "Toronto")}]:
            with self.assertRaises(ProtocolError):
                self.state.accept(self.schedule(**changes), now=NOW)
        self.assertIsNone(self.state.snapshot())

    def test_findings_are_deduplicated_by_finding_id_and_checked_for_expiry(self):
        finding_id = uuid4()
        data = SlotsAvailable(finding_id, uuid4(), NOW, NOW + timedelta(seconds=180), (date(2027, 11, 1),))
        for _ in range(2):
            self.state.accept(Message.create(data, sent_at=NOW), now=NOW)
        task = self.state.next_task(NOW)
        self.assertEqual(task.data.finding_id, finding_id)
        self.state.finish_task(finding_id)
        self.assertIsNone(self.state.next_task(NOW))
        self.state.accept(Message.create(SlotsAvailable(uuid4(), uuid4(), NOW, NOW + timedelta(seconds=180),
                                                        (date(2027, 11, 1),)), sent_at=NOW),
                          now=NOW + timedelta(seconds=180))
        self.assertIsNone(self.state.next_task(NOW + timedelta(seconds=180)))

    def test_tasks_and_outbox_survive_restart_and_receipt_removes_only_confirmed_finding(self):
        from usvisa.protocol.messages import SlotsFound
        outbound = Message.create(SlotsFound(self.node_id, NOW, (date(2027, 11, 1),)), sent_at=NOW)
        self.state.add_finding(outbound, NOW + timedelta(seconds=180))
        self.state.close()
        self.state = NodeState(Path(self.directory.name) / "state.sqlite3", self.node_id, GROUP)
        self.assertEqual(self.state.pending(NOW)[0][0], outbound)
        self.state.confirmed(outbound.message_id)
        self.assertEqual(self.state.pending(NOW), [])

    def test_instance_lock_blocks_another_process_instance_without_stale_pid_files(self):
        path = Path(self.directory.name) / "instance.lock"
        first = InstanceLock(path)
        try:
            with self.assertRaises(RuntimeError):
                InstanceLock(path)
        finally:
            first.close()
        InstanceLock(path).close()

    def test_grid_preserves_minimum_spacing_with_timer_jitter_and_skips_pauses(self):
        data = self.schedule().data
        anchor = NOW.timestamp()
        self.assertEqual(next_check_at(data, anchor + 2, anchor), anchor + 180)
        self.assertEqual(next_check_at(data, anchor + 179, anchor + 180.1), anchor + 180.1)
        self.assertEqual(next_check_at(data, anchor + 179, anchor + 210), anchor + 360)
        self.assertEqual(next_check_at(data, anchor + 800, anchor + 900), anchor + 900)

    def test_worker_checks_every_180_seconds_despite_timer_jitter(self):
        self.state.accept(self.schedule(), now=NOW)
        elapsed = [0.0]
        stopped = [False]
        stop = Mock()
        stop.is_set.side_effect = lambda: stopped[0]
        stop.wait.side_effect = lambda duration: elapsed.__setitem__(0, elapsed[0] + duration + .004)
        transport = Mock(stopping=stop, fatal=None)
        worker = Worker(self.state, transport)
        client = Mock()
        starts = []

        def get_dates():
            starts.append(elapsed[0])
            if len(starts) == 3:
                stopped[0] = True
            return []

        client.get_available_dates.side_effect = get_dates
        clock = Mock()
        clock.time.side_effect = lambda: NOW.timestamp() + elapsed[0]
        clock.monotonic.side_effect = lambda: 1000 + elapsed[0]
        with patch("usvisa.node.coordinated.time", clock), \
             patch("usvisa.node.coordinated.cfg.DATE_REQUEST_CYCLE_LENGTH", 0), \
             patch("usvisa.node.coordinated.solo.log_message"):
            worker.loop(client)
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(180 <= after - before < 181 for before, after in zip(starts, starts[1:])))

    def test_raw_dates_are_reported_outside_own_filters_and_dry_run_candidate_is_included(self):
        current = datetime.now(timezone.utc)
        self.state.accept(self.schedule(), now=current)
        transport = Mock(stopping=threading.Event())
        worker = Worker(self.state, transport)
        client = Mock()
        candidate, shared = date(2027, 10, 1), date(2027, 11, 15)
        client.book.return_value = BookingResult(candidate, "09:00", True)
        with patch("usvisa.node.coordinated.cfg.EARLIEST_ACCEPTABLE_DATE", "2027-10-01"), \
             patch("usvisa.node.coordinated.cfg.LATEST_ACCEPTABLE_DATE", "2027-10-31"), \
             patch("usvisa.node.coordinated.cfg.EXCLUSION_DATE_RANGES", []), \
             patch.object(worker, "notify"), patch("usvisa.node.coordinated.solo.log_message"):
            worker.try_dates(client, [candidate, shared], observed=current)
        self.assertEqual(self.state.pending(current)[0][0].data.dates, (candidate, shared))
        client.book.assert_called_once()
        self.assertIsNone(self.state.get("verified_booking"))

    def test_confirmed_and_uncertain_own_candidate_is_excluded_before_reporting(self):
        current = datetime.now(timezone.utc)
        self.state.accept(self.schedule(), now=current)
        candidate, shared = date(2027, 10, 1), date(2027, 11, 15)
        for outcome in [BookingResult(candidate, "09:00", False), BookingNotVerified("uncertain")]:
            self.state.set("verified_booking", None)
            worker = Worker(self.state, Mock(stopping=threading.Event()))
            client = Mock()
            if isinstance(outcome, Exception):
                client.book.side_effect = outcome
            else:
                client.book.return_value = outcome
            with patch("usvisa.node.coordinated.cfg.EARLIEST_ACCEPTABLE_DATE", "2027-10-01"), \
                 patch("usvisa.node.coordinated.cfg.LATEST_ACCEPTABLE_DATE", "2027-10-31"), \
                 patch("usvisa.node.coordinated.cfg.EXCLUSION_DATE_RANGES", []), \
                 patch.object(worker, "notify"), patch("usvisa.node.coordinated.solo.log_message"):
                if isinstance(outcome, Exception):
                    with self.assertRaises(BookingNotVerified):
                        worker.try_dates(client, [candidate, shared], observed=current)
                    self.assertIsNone(self.state.get("verified_booking"))
                else:
                    worker.try_dates(client, [candidate, shared], observed=current)
        for message, _ in self.state.pending(current):
            self.assertEqual(message.data.dates, (shared,))

    def run_monitoring(self, *, shared=False, dry_run=False):
        self.state.accept(self.schedule(), now=NOW)
        candidate, other = date(2027, 10, 1), date(2027, 11, 15)
        if shared:
            task = Message.create(SlotsAvailable(uuid4(), uuid4(), NOW, NOW + timedelta(seconds=180),
                                                 (candidate,)), sent_at=NOW)
            self.state.accept(task, now=NOW)
        elapsed, stopped, starts = [0.0], [False], []
        stop = Mock()
        stop.is_set.side_effect = lambda: stopped[0]
        stop.wait.side_effect = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds + .004)
        worker = Worker(self.state, Mock(stopping=stop, fatal=None))
        client = Mock()
        client.book.return_value = BookingResult(candidate, "09:00", dry_run)

        def get_dates():
            starts.append(elapsed[0])
            if len(starts) == 3:
                stopped[0] = True
            return [candidate, other]

        client.get_available_dates.side_effect = get_dates
        clock = Mock()
        clock.time.side_effect = lambda: NOW.timestamp() + elapsed[0]
        clock.monotonic.side_effect = lambda: 1000 + elapsed[0]
        with patch("usvisa.node.coordinated.time", clock), \
             patch("usvisa.node.coordinated.now", side_effect=lambda: NOW + timedelta(seconds=elapsed[0])), \
             patch.multiple("usvisa.node.coordinated.cfg", DATE_REQUEST_CYCLE_LENGTH=0,
                            EARLIEST_ACCEPTABLE_DATE="2027-10-01", LATEST_ACCEPTABLE_DATE="2027-10-31",
                            EXCLUSION_DATE_RANGES=[], TEST_MODE=dry_run), \
             patch.object(worker, "notify") as notify, patch("usvisa.node.coordinated.solo.log_message"):
            worker.loop(client)
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(180 <= after - before < 181 for before, after in zip(starts, starts[1:])))
        if dry_run:
            self.assertEqual(client.book.call_count, 2)
            self.assertIsNone(self.state.get("verified_booking"))
        else:
            client.book.assert_called_once()
            notify.assert_called_once()
            self.assertEqual(self.state.get("verified_booking"), {"date": candidate.isoformat(), "time": "09:00"})
            findings = self.state.pending(NOW + timedelta(seconds=elapsed[0]))
            self.assertTrue(findings)
            self.assertTrue(all(message.data.dates == (other,) for message, _ in findings))
        if shared:
            with self.state.transaction() as db:
                self.assertEqual(db.execute("SELECT status FROM tasks WHERE id=?",
                                           (str(task.data.finding_id),)).fetchone()[0], "done")

    def test_verified_own_booking_keeps_polling_and_sharing_without_booking_again(self):
        self.run_monitoring()

    def test_verified_shared_booking_keeps_polling_without_booking_again(self):
        self.run_monitoring(shared=True)

    def test_dry_run_keeps_monitoring_and_does_not_set_booking_stop(self):
        self.run_monitoring(dry_run=True)

    def test_saved_booking_stop_survives_restart_and_suppresses_shared_and_ordinary_booking(self):
        current = datetime.now(timezone.utc)
        self.state.accept(self.schedule(), now=current)
        candidate, other = date(2027, 10, 1), date(2027, 11, 15)
        self.state.set("verified_booking", {"date": candidate.isoformat(), "time": "09:00"})
        self.state.close()
        self.state = NodeState(Path(self.directory.name) / "state.sqlite3", self.node_id, GROUP)
        worker = Worker(self.state, Mock(stopping=threading.Event()))
        client = Mock()
        with patch("usvisa.node.coordinated.solo.log_message"), patch.object(worker, "notify") as notify:
            worker.try_dates(client, [candidate, other], expires=current + timedelta(seconds=180))
            worker.try_dates(client, [candidate, other], observed=current)
        client.book.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(self.state.pending(current)[0][0].data.dates, (other,))


class TransportTests(unittest.TestCase):
    def test_codec_handles_escaped_headers_multibyte_length_fragmentation_and_heartbeats(self):
        wire = encode("SEND", {"destination": "/exchange/test", "custom": "a:b\n\\"}, "привет\x00мир")
        decoder = Decoder()
        self.assertEqual(decoder.feed("\n" + wire[:20]), [])
        frame = decoder.feed(wire[20:] + "\n")[0]
        self.assertEqual(frame.headers["custom"], "a:b\n\\")
        self.assertEqual(frame.body.decode("utf-8"), "привет\x00мир")
        self.assertEqual(decoder.feed("\n\r\n"), [])

    def test_origin_validation_has_no_remote_http_or_certificate_bypass(self):
        for value in ["http://example.com", "http://100.64.0.1", "https://example.com/path", "https://a:b@example.com"]:
            with self.assertRaises(ValueError):
                server_origin(value, allow_local=True)
        with self.assertRaises(ValueError):
            server_origin("http://localhost:8187")
        self.assertEqual(websocket_url(server_origin("http://localhost:8187", allow_local=True)), "ws://localhost:8187/ws")
        self.assertEqual(websocket_url(server_origin("https://visa.example.com/")), "wss://visa.example.com/ws")
        self.assertEqual(websocket_url(server_origin(" HTTPS://VISA.Example.com:443/ ")),
                         "wss://visa.example.com/ws")

    def test_token_validation_and_stomp_buffer_limits(self):
        node = uuid4()
        self.assertEqual(parse_token(f"{node}." + "a" * 43)[0], node)
        for token in ["", "random", f"{node}.short"]:
            with self.assertRaises(ValueError):
                parse_token(token)
        with self.assertRaises(ValueError):
            Decoder().feed("a" * 262145)
