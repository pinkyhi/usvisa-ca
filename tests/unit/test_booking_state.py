"""Explicit local booking-block management; no network or account access."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from usvisa.node.booking_state import manage
from usvisa.shared import audit
from usvisa.shared.locking import InstanceLock


class BookingStateCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.node = uuid4()
        directory = self.root / ".node-state" / str(self.node)
        directory.mkdir(parents=True)
        (directory.parent / "registration.json").write_text(json.dumps({"username": "fixture", "nodeId": str(self.node)}))
        self.database = directory / "state.sqlite3"
        c = sqlite3.connect(self.database)
        c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        c.executemany("INSERT INTO meta VALUES(?,?)", [("booking_application_id", json.dumps("123")),
            ("verified_booking", json.dumps({"date": "2027-09-17", "time": "10:45"})),
            ("booking_block", json.dumps({"reason": "unverified_booking"}))])
        c.commit()
        c.close()
        self.directory = directory
        for patcher in (patch("usvisa.node.booking_state.data_directory", return_value=self.root),
                        patch("usvisa.node.booking_state.cfg.USER_EMAIL", "fixture@example.invalid"),
                        patch("usvisa.node.booking_state.cfg.SESSION_CACHE_DIR", self.root / ".sessions"),
                        patch.object(audit, "GUARD_DIR", self.root / ".booking-state"),
                        patch.object(audit, "LOG_PATH", self.root / "audit.jsonl"),
                        patch.dict(os.environ, {"NODE_TOKEN": "", "PORTAL_USERNAME": "fixture"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        audit.claim("123", "2027-09-17", "10:45", "Calgary")
        audit.claim("456", "2027-10-01", "09:00", "Calgary")

    def test_status_is_read_only_and_reports_single_file_and_legacy_migration(self):
        status = manage()
        self.assertTrue(status["bookingDisabled"])
        self.assertEqual(status["applicationId"], "123")
        self.assertEqual(status["bookingState"]["state"], "pending")
        self.assertTrue(status["legacyStateNeedsMigration"])
        self.assertTrue(audit.claim_path("123").exists())

    def test_explicit_reset_clears_metadata_and_archives_only_its_own_guard(self):
        self.assertFalse(manage(reset=True)["bookingDisabled"])
        self.assertFalse(manage()["bookingDisabled"])
        self.assertFalse(audit.claim_path("123").exists())
        self.assertTrue(audit.claim_path("456").exists())
        archives = list((audit.GUARD_DIR / "cleared").glob("123-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text())["date"], "2027-09-17")

    def test_reset_refuses_a_running_node_and_read_only_status_still_works(self):
        lock = InstanceLock(self.directory / "instance.lock")
        try:
            with self.assertRaises(RuntimeError):
                manage(reset=True)
            self.assertTrue(manage()["bookingDisabled"])
        finally:
            lock.close()

    def test_reset_refuses_a_running_solo_node(self):
        lock = InstanceLock(audit.GUARD_DIR / "solo.lock")
        try:
            with self.assertRaises(RuntimeError):
                manage(reset=True)
        finally:
            lock.close()
        self.assertTrue(audit.claim_path("123").exists())

    def test_reset_refuses_wrong_application_and_wrong_portal_login(self):
        with self.assertRaises(ValueError):
            manage(reset=True, application_id="456")
        with patch.dict(os.environ, {"PORTAL_USERNAME": "different"}):
            with self.assertRaises(ValueError):
                manage(reset=True)
        self.assertTrue(audit.claim_path("123").exists())

    def test_corrupt_guard_still_blocks_and_path_traversal_is_rejected(self):
        audit.claim_path("123").write_text("not JSON")
        self.assertTrue(manage()["bookingDisabled"])
        self.assertEqual(manage()["bookingState"]["state"], "unreadable")
        with self.assertRaises(ValueError):
            audit.claim_path("../123")
