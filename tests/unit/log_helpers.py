"""Per-test files; cleanup runs even when a test fails."""
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from usvisa.shared import audit
from usvisa.node import solo as reschedule


class IsolatedLogsTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="rescheduler-test-", dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.test_log_directory = Path(directory.name)
        # Booking verification uses real deadlines in production. Offline tests
        # advance a private client clock instead of sleeping for thirty seconds.
        from unittest.mock import Mock
        clock = Mock(wraps=time)
        self.client_clock = clock
        elapsed = [time.time()]
        clock.time.side_effect = lambda: elapsed[0]
        clock.sleep.side_effect = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
        for patcher in (
            patch("usvisa.visa.appointment_client.clock", clock),
            patch.multiple(audit, GUARD_DIR=self.test_log_directory / "guards",
                           LOG_PATH=self.test_log_directory / "audit.jsonl"),
            patch.object(reschedule, "AVAILABLE_DATES_LOG_PATH",
                         self.test_log_directory / "available_dates.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
