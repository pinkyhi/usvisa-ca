"""Log privacy and formatting without application-wide debug logging."""
import logging
import unittest

from usvisa.coordinator.observability import UTCFormatter, event


class CoordinatorObservabilityTests(unittest.TestCase):
    def test_log_fields_drop_credentials_and_escape_user_controlled_newlines(self):
        with self.assertLogs("usvisa.coordinator.events", level="INFO") as recorded:
            event("Node created", nodeId="example-node", nodeName="Home\nforged log",
                  password="private-password", token="private-token", headers={"Authorization": "secret"},
                  body={"password": "private-password"})
        line = recorded.records[0].getMessage()
        self.assertIn('nodeId="example-node"', line)
        self.assertIn('nodeName="Home\\nforged log"', line)
        self.assertNotIn("\n", line)
        for secret in ("private-password", "private-token", "Authorization", "secret"):
            self.assertNotIn(secret, line)

    def test_timestamp_is_explicitly_utc(self):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "ready", (), None)
        record.created = 0
        formatter = UTCFormatter("[%(asctime)s UTC] %(message)s", "%Y-%m-%d %H:%M:%S")
        self.assertEqual(formatter.format(record), "[1970-01-01 00:00:00 UTC] ready")
