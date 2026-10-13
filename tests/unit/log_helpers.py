"""Per-test files; cleanup runs even when a test fails."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit
import reschedule


class IsolatedLogsTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="rescheduler-test-", dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.test_log_directory = Path(directory.name)
        for patcher in (
            patch.multiple(audit, GUARD_DIR=self.test_log_directory / "guards",
                           LOG_PATH=self.test_log_directory / "audit.jsonl"),
            patch.object(reschedule, "AVAILABLE_DATES_LOG_PATH",
                         self.test_log_directory / "available_dates.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
