"""Offline configuration checks without loading the user's .env."""

import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch


class BackoffSettingsTests(unittest.TestCase):
    def load_settings(self, overrides):
        environment = {
            "USER_EMAIL": "test@example.com", "USER_PASSWORD": "test",
            "USER_CONSULATE": "Vancouver", "EARLIEST_ACCEPTABLE_DATE": "2026-11-01",
            "LATEST_ACCEPTABLE_DATE": "2026-12-01",
        }
        environment.update(overrides)
        with patch.dict(os.environ, environment, clear=True), patch("dotenv.load_dotenv"):
            return runpy.run_path(str(Path(__file__).resolve().parents[2] / "settings.py"))

    def test_missing_parameters_default_to_zero(self):
        settings = self.load_settings({})
        settings["validate_settings"]()
        for prefix in ("RATE_LIMIT_BACKOFF", "EMPTY_DATES_BACKOFF"):
            for suffix in ("INITIAL_DELAY", "MAX_DELAY", "MULTIPLIER"):
                self.assertEqual(settings[f"{prefix}_{suffix}"], 0)

    def test_zero_blank_or_missing_parameter_disables_validation_of_enabled_limits(self):
        for prefix in ("RATE_LIMIT_BACKOFF", "EMPTY_DATES_BACKOFF"):
            for suffix in ("INITIAL_DELAY", "MAX_DELAY", "MULTIPLIER"):
                for value in (None, "", "0"):
                    with self.subTest(prefix=prefix, suffix=suffix, value=value):
                        overrides = {f"{prefix}_INITIAL_DELAY": "240",
                                     f"{prefix}_MAX_DELAY": "10",
                                     f"{prefix}_MULTIPLIER": "1"}
                        name = f"{prefix}_{suffix}"
                        if value is None:
                            del overrides[name]
                        else:
                            overrides[name] = value
                        settings = self.load_settings(overrides)
                        self.assertEqual(settings[name], 0)
                        settings["validate_settings"]()

    def test_invalid_enabled_backoff_or_negative_parameter_is_rejected(self):
        for prefix in ("RATE_LIMIT_BACKOFF", "EMPTY_DATES_BACKOFF"):
            for initial, maximum, multiplier in ((240, 10, 2), (4, 4096, 1), (-1, 0, 0)):
                with self.subTest(prefix=prefix, values=(initial, maximum, multiplier)):
                    settings = self.load_settings({f"{prefix}_INITIAL_DELAY": str(initial),
                                                   f"{prefix}_MAX_DELAY": str(maximum),
                                                   f"{prefix}_MULTIPLIER": str(multiplier)})
                    with self.assertRaises(ValueError):
                        settings["validate_settings"]()
