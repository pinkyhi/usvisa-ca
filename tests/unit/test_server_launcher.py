from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import server


class ServerLauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.patch_root = patch.object(server, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def run_command(self, *arguments):
        with patch("sys.argv", ["server.py", *arguments]), redirect_stdout(io.StringIO()):
            return server.main()

    def test_setup_preserves_visa_settings_and_infers_group_city(self):
        path = self.root / ".env"
        path.write_text("# Existing account\nUSER_PASSWORD=keep-this\nUSER_CONSULATE=Calgary\nTEST_MODE=false\n")
        self.assertEqual(self.run_command("setup", "--mode", "coordinated"), 0)
        body = path.read_text()
        self.assertIn("USER_PASSWORD=keep-this", body)
        self.assertIn("TEST_MODE=false", body)
        self.assertIn("MODE=coordinated", body)
        self.assertIn("COMPOSE_PROFILES=${MODE:-solo},${MODE:-solo}-${EXPOSE_MODE:-cloudflare_quick}", body)
        self.assertEqual(server.read_env(self.root / ".server.env")["CONSULATE_ID"], "89")

    def test_solo_without_server_secrets_stops_infrastructure_and_starts_only_node(self):
        (self.root / ".env").write_text("MODE=solo\n")
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.run_command("up"), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("stop", commands[0])
        self.assertEqual(commands[1][-4:], ["up", "-d", "--build", "node"])
        self.assertNotIn("--env-file", commands[1])
        self.assertEqual(run.call_args.kwargs["env"]["COMPOSE_PROFILES"], "solo,solo-cloudflare_quick")

    def test_solo_setup_does_not_require_domain_credentials_or_server_configuration(self):
        with patch("getpass.getpass", side_effect=AssertionError("Solo does not need a tunnel token")):
            self.assertEqual(self.run_command("setup", "--mode", "solo", "--profile", "cloudflare_domain"), 0)
        self.assertFalse((self.root / ".server.env").exists())
        self.assertEqual(server.read_env(self.root / ".env")["MODE"], "solo")

    def coordinated_up(self, server_only):
        (self.root / ".env").write_text("MODE=coordinated\n")
        server.save_env(self.root / ".server.env", {"EXPOSE_MODE": "cloudflare_quick", "PUBLIC_BASE_URL": "https://pending.trycloudflare.com"})
        result = SimpleNamespace(returncode=0, stdout="https://unit-tunnel.trycloudflare.com", stderr="")
        with patch("subprocess.run", return_value=result) as run, patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            self.assertEqual(self.run_command("up", *(["--server-only"] if server_only else [])), 0)
        return [call.args[0] for call in run.call_args_list]

    def test_server_only_does_not_start_visa_worker(self):
        commands = self.coordinated_up(True)
        self.assertFalse(any(command[-1] == "node" for command in commands))

    def test_combined_up_starts_node_after_the_server(self):
        commands = self.coordinated_up(False)
        self.assertEqual(commands[-1][-4:], ["up", "-d", "--build", "node"])
        self.assertIn("rabbitmq", commands[0])

    def test_passive_setup_keeps_passive_mode_in_node_configuration(self):
        (self.root / ".env").write_text("USER_CONSULATE=Calgary\nUSER_PASSWORD=keep-this\n")
        self.assertEqual(self.run_command("setup", "--mode", "coordinated-passive"), 0)
        body = server.read_env(self.root / ".env")
        self.assertEqual(body["MODE"], "coordinated-passive")
        self.assertEqual(body["USER_PASSWORD"], "keep-this")

    def test_passive_up_starts_infrastructure_and_node_with_passive_profiles(self):
        (self.root / ".env").write_text("MODE=coordinated-passive\n")
        server.save_env(self.root / ".server.env", {"EXPOSE_MODE": "cloudflare_quick",
            "PUBLIC_BASE_URL": "https://pending.trycloudflare.com"})
        result = SimpleNamespace(returncode=0, stdout="https://unit-tunnel.trycloudflare.com", stderr="")
        with patch("subprocess.run", return_value=result) as run, patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            self.assertEqual(self.run_command("up"), 0)
        self.assertEqual(run.call_args.kwargs["env"]["MODE"], "coordinated-passive")
        self.assertEqual(run.call_args.kwargs["env"]["COMPOSE_PROFILES"],
                         "coordinated-passive,coordinated-passive-cloudflare_quick")
        self.assertEqual(run.call_args.args[0][-4:], ["up", "-d", "--build", "node"])
