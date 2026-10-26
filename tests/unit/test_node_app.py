"""Offline CLI checks for the inclusive booking limit and exclusive cutoff."""

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from usvisa.node import app, bootstrap, coordinated, settings


class NodeAppTests(unittest.TestCase):
    def setUp(self):
        configuration = patch.multiple(
            settings, MODE="coordinated", USER_EMAIL="test@example.com", USER_PASSWORD="test",
            USER_CONSULATE="Calgary", EARLIEST_ACCEPTABLE_DATE="2026-11-01",
        )
        configuration.start()
        self.addCleanup(configuration.stop)
        prepare = patch.object(bootstrap, "prepare")
        self.prepare = prepare.start()
        self.addCleanup(prepare.stop)
        worker = patch.object(coordinated, "main", return_value=0)
        self.worker = worker.start()
        self.addCleanup(worker.stop)

    def test_default_cutoff_includes_last_booking_day_across_calendar_boundaries(self):
        cases = (
            ("2028-02-28", "2028-02-29"),
            ("2028-02-29", "2028-03-01"),
            ("2027-02-28", "2027-03-01"),
            ("2027-04-30", "2027-05-01"),
            ("2027-12-31", "2028-01-01"),
        )
        for latest, expected in cases:
            for environment in ({}, {"NODE_MIN_DATE": ""}):
                with self.subTest(latest=latest, environment=environment), \
                     patch.object(settings, "LATEST_ACCEPTABLE_DATE", latest), \
                     patch.dict(os.environ, environment, clear=True):
                    # Check the value at the handoff to the actual worker entry point.
                    def run_worker(**kwargs):
                        self.assertEqual(os.environ["NODE_MIN_DATE"], expected)
                        return 0

                    self.worker.side_effect = run_worker
                    self.assertEqual(app.main(["--check-coordination"]), 0)
                    self.worker.assert_called_with(check_only=True)

    def test_explicit_cutoff_remains_available_as_an_override(self):
        with patch.object(settings, "LATEST_ACCEPTABLE_DATE", "2028-02-29"), \
             patch.dict(os.environ, {"NODE_MIN_DATE": "2029-01-01"}, clear=True):
            self.assertEqual(app.main(["--check-coordination"]), 0)
            self.assertEqual(os.environ["NODE_MIN_DATE"], "2029-01-01")

    def test_unrepresentable_next_day_stops_before_registration_or_worker_start(self):
        output = io.StringIO()
        with patch.object(settings, "LATEST_ACCEPTABLE_DATE", "9999-12-31"), \
             patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            self.assertEqual(app.main(["--check-coordination"]), 2)
        self.assertIn("Invalid coordinated configuration", output.getvalue())
        self.prepare.assert_not_called()
        self.worker.assert_not_called()
