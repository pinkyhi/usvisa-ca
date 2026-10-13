import io
import unittest
from contextlib import redirect_stdout

import audit
import reschedule
from tests.unit.log_helpers import IsolatedLogsTest


class LogIsolationTests(unittest.TestCase):
    def test_files_are_isolated_and_removed_after_success_or_failure(self):
        original_audit = audit.LOG_PATH
        original_dates = reschedule.AVAILABLE_DATES_LOG_PATH
        for fail in (False, True):
            paths = []

            class Example(IsolatedLogsTest):
                def runTest(self):
                    from datetime import date
                    paths.append(self.test_log_directory)
                    self.assertNotEqual(audit.LOG_PATH, original_audit)
                    self.assertNotEqual(reschedule.AVAILABLE_DATES_LOG_PATH, original_dates)
                    audit.event("synthetic_test_event")
                    reschedule.log_available_dates([date(2026, 11, 12)])
                    self.assertTrue(audit.LOG_PATH.exists())
                    self.assertTrue(reschedule.AVAILABLE_DATES_LOG_PATH.exists())
                    if fail:
                        self.fail("intentional cleanup check")

            result = unittest.TestResult()
            with redirect_stdout(io.StringIO()):
                Example().run(result)
            self.assertEqual(len(result.errors), 0)
            self.assertEqual(len(result.failures), int(fail))
            self.assertFalse(paths[0].exists())
            self.assertEqual(audit.LOG_PATH, original_audit)
            self.assertEqual(reschedule.AVAILABLE_DATES_LOG_PATH, original_dates)
