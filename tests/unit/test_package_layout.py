"""Regression checks for relocated state and dependency-free CLI help."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usvisa.shared import paths


ROOT = Path(__file__).resolve().parents[2]


class PackageLayoutTests(unittest.TestCase):
    def environment(self, **overrides):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["USVISA_ENV_FILE"] = str(ROOT / "tests" / "missing.env")
        environment.update(overrides)
        return environment

    def test_default_paths_stay_at_checkout_root_from_another_directory(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(Path, "cwd", return_value=ROOT / "tests"):
            self.assertEqual(paths.project_root(), ROOT)
            self.assertEqual(paths.data_directory(), ROOT)
            self.assertEqual(paths.environment_file(), ROOT / ".env")

    def test_explicit_state_and_env_paths_are_resolved_from_checkout(self):
        with patch.dict(os.environ, {
            "USVISA_DATA_DIR": "state",
            "USVISA_ENV_FILE": "configuration/node.env",
        }, clear=True):
            self.assertEqual(paths.data_directory(), ROOT / "state")
            self.assertEqual(paths.environment_file(), ROOT / "configuration" / "node.env")

    def test_cli_help_does_not_import_browser_or_transport_dependencies(self):
        for application in ("node", "coordinator"):
            code = (
                f"from usvisa.{application}.app import main\n"
                "import sys\n"
                "try:\n"
                "    main(['--help'])\n"
                "except SystemExit as result:\n"
                "    assert result.code == 0\n"
                "assert not {'selenium', 'requests', 'aio_pika', 'stomp'} & sys.modules.keys()\n"
            )
            with self.subTest(application=application):
                result = subprocess.run(
                    [sys.executable, "-c", code], cwd=ROOT,
                    env=self.environment(), capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_original_launcher_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(ROOT / "reschedule.py"), "--help"],
                cwd=directory, env=self.environment(),
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("US visa monitoring node", result.stdout)

    def test_existing_guard_is_respected_at_configured_state_location(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            guard = state / ".booking-state" / "123.json"
            guard.parent.mkdir()
            original = json.dumps({"state": "submission_reserved"})
            guard.write_text(original, encoding="utf-8")
            code = (
                "from datetime import date\n"
                "from usvisa.shared import audit\n"
                "try:\n"
                "    audit.claim('123', date(2027, 1, 1), '08:30', 'Vancouver')\n"
                "except FileExistsError:\n"
                "    print('blocked')\n"
                "else:\n"
                "    raise AssertionError('Existing submission guard was bypassed')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=ROOT,
                env=self.environment(USVISA_DATA_DIR=str(state)),
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "blocked")
            self.assertEqual(guard.read_text(encoding="utf-8"), original)
