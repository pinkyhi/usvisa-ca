"""Live Gmail integration test: sends one real email using the project .env."""

import os
import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.gmail_check import check_gmail


@unittest.skipUnless(
    __name__ == "__main__" or os.getenv("RUN_GMAIL_INTEGRATION", "").lower() == "true",
    "Live Gmail check: run python tests/integration/test_check_gmail.py",
)
class GmailLiveTests(unittest.TestCase):
    def test_gmail_accepts_real_notification(self):
        self.assertEqual(
            check_gmail(), 0,
            "Gmail check failed; see the configuration/SMTP diagnostic above",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
