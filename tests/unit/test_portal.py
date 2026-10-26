import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from usvisa.coordinator.auth import authenticate, create_user, normalize_username, session_user
from usvisa.coordinator.config import ServerConfig
from usvisa.coordinator.portal import create_app
from usvisa.coordinator.repository import Repository
from usvisa.protocol.messages import GroupConfig


class PortalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "state.sqlite3"
        self.path = path
        self.repository = Repository(path)
        self.addCleanup(self.repository.close)
        create_user(self.repository, "alice", "correct-password-123")
        create_user(self.repository, "bob", "different-password-123")
        self.admin = AsyncMock()
        config = ServerConfig(path, "https://visa.example.com", GroupConfig(95, "Vancouver"),
                              "amqp://unused", "http://unused", "admin", "unused", "coordinator", "unused")
        self.app = create_app(config, repository=self.repository, admin=self.admin, run_broker=False)
        self.client = TestClient(self.app, base_url=config.origin)
        self.addCleanup(self.client.close)

    def login(self, client=None, username="alice", password="correct-password-123"):
        client = client or self.client
        token = client.get("/api/v1/csrf").json()["csrfToken"]
        response = client.post("/api/v1/session", json={"username": username, "password": password},
                               headers={"Origin": "https://visa.example.com", "X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        client.headers.update({"Origin": "https://visa.example.com", "X-CSRF-Token": response.json()["csrfToken"]})
        return response

    def create(self, key=None):
        return self.client.post("/api/v1/nodes", json={"name": "Home computer"},
                                headers={"Idempotency-Key": key or str(uuid4())})

    def test_sessions_secure_cookies_host_origin_csrf_and_logout(self):
        self.assertEqual(self.client.get("/api/v1/nodes").status_code, 401)
        self.assertEqual(self.client.get("/", headers={"Host": "attacker.example"}).status_code, 400)
        response = self.login()
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertEqual(self.client.post("/api/v1/nodes", json={"name": "x"}, headers={"Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/nodes", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/v1/session").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_plain_username_eight_character_password_and_normalization(self):
        create_user(self.repository, " Admin ", "eight123")
        response = self.login(username=" ADMIN ", password="eight123")
        self.assertEqual(response.json()["username"], "admin")
        self.assertNotIn("email", response.json())
        self.assertEqual(self.client.get("/api/v1/me").json()["username"], "admin")
        with self.assertRaises(sqlite3.IntegrityError):
            create_user(self.repository, "ADMIN", "different-password")
        with self.assertRaises(ValueError):
            create_user(self.repository, "short-password", "seven12")
        for value in ("", "  ", "two words", "two\nlines", "bad\x00name", "a" * 255):
            with self.subTest(username=value), self.assertRaises(ValueError):
                normalize_username(value)
        self.assertEqual(normalize_username("Some_User-1"), "some_user-1")
        form = self.client.get("/login").text
        self.assertIn('name="username" type="text"', form)
        self.assertIn('minlength="8"', form)
        self.assertNotIn('type="email"', form)

    def test_legacy_database_migration_preserves_accounts_sessions_and_node_ownership(self):
        legacy_path = self.path.with_name("legacy.sqlite3")
        with Repository(legacy_path) as legacy:
            owner = create_user(legacy, "owner@example.com", "legacy-password")
            status, user = authenticate(legacy, "owner@example.com", "legacy-password", "migration-test")
            self.assertEqual(status, "ok")
            node_id = uuid4()
            legacy.register_node(node_id)
            with legacy.transaction() as work:
                work.connection.execute("INSERT INTO portal_nodes(node_id, owner_id, name) VALUES(?, ?, ?)",
                                        (str(node_id), owner, "Existing computer"))
                work.connection.execute("ALTER TABLE users RENAME COLUMN username TO email")
                work.connection.execute("PRAGMA user_version = 2")
        with Repository(legacy_path) as migrated:
            account = session_user(migrated, user["session"])
            self.assertEqual(account["username"], "owner@example.com")
            self.assertEqual(account["user_id"], owner)
            self.assertEqual(authenticate(migrated, "owner@example.com", "legacy-password", "migration-test")[0], "ok")
            with migrated.transaction() as work:
                self.assertEqual(work.connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(work.connection.execute("SELECT owner_id FROM portal_nodes WHERE node_id=?",
                                                        (str(node_id),)).fetchone()[0], owner)
                self.assertEqual(work.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        with Repository(legacy_path) as reopened:
            self.assertEqual(session_user(reopened, user["session"])["user_id"], owner)

    def test_cli_creates_plain_username_from_password_stdin(self):
        from usvisa.coordinator.app import main
        with patch("sys.stdin", io.StringIO("eight123\n")), patch("builtins.print"):
            result = main(["--database", str(self.path), "create-user", "--username", "command-user",
                           "--password-stdin"])
        self.assertEqual(result, 0)
        self.assertEqual(self.login(username="command-user", password="eight123").json()["username"], "command-user")

    def test_validation_does_not_echo_password_and_login_limits_are_persistent(self):
        token = self.client.get("/api/v1/csrf").json()["csrfToken"]
        headers = {"Origin": "https://visa.example.com", "X-CSRF-Token": token}
        response = self.client.post("/api/v1/session", json={"username": "alice", "password": "secret" * 180}, headers=headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret", response.text)
        for attempt in range(10):
            username = "alice" if attempt % 2 else " ALICE "
            response = self.client.post("/api/v1/session", json={"username": username, "password": "wrong-password"}, headers=headers)
            self.assertEqual(response.status_code, 401)
        response = self.client.post("/api/v1/session", json={"username": "alice", "password": "correct-password-123"}, headers=headers)
        self.assertEqual(response.status_code, 429)

    def test_creation_retry_reuses_identity_and_does_not_replay_secret(self):
        self.login()
        key = str(uuid4())
        first = self.create(key)
        self.assertEqual(first.status_code, 201)
        replay = self.create(key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.json()["nodeId"], replay.json()["nodeId"])
        self.assertNotIn("token", replay.json())
        self.assertEqual(self.admin.provision.await_count, 1)
        self.assertIn("no-store", first.headers["cache-control"])
        self.assertEqual(len(self.client.get("/api/v1/nodes").json()["nodes"]), 1)

    def test_partial_provisioning_is_resumed_and_secrets_are_not_stored(self):
        self.login()
        self.admin.provision.side_effect = [OSError("offline"), None]
        key = str(uuid4())
        self.assertEqual(self.create(key).status_code, 503)
        identity = self.client.get("/api/v1/nodes").json()["nodes"][0]["nodeId"]
        resumed = self.create(key)
        self.assertEqual(resumed.status_code, 201)
        self.assertEqual(resumed.json()["nodeId"], identity)
        secret = resumed.json()["token"].split(".")[1]
        with self.repository.transaction() as work:
            stored = work.connection.execute("SELECT token_hash FROM portal_nodes").fetchone()[0]
        self.assertNotIn(secret, stored)

    def test_ownership_rotation_and_revocation(self):
        self.login()
        created = self.create().json()
        identity = created["nodeId"]
        with TestClient(self.app, base_url="https://visa.example.com") as bob:
            self.login(bob, "bob", "different-password-123")
            self.assertEqual(bob.get("/api/v1/nodes").json()["nodes"], [])
            self.assertEqual(bob.post(f"/api/v1/nodes/{identity}/token", headers={"Idempotency-Key": str(uuid4())}).status_code, 404)
            self.assertEqual(bob.post(f"/api/v1/nodes/{identity}/revoke").status_code, 404)
        rotated = self.client.post(f"/api/v1/nodes/{identity}/token", headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(rotated.json()["token"], created["token"])
        self.assertEqual(rotated.json()["nodeId"], identity)
        self.assertEqual(self.client.post(f"/api/v1/nodes/{identity}/revoke").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/nodes").json()["nodes"][0]["lifecycle"], "revoked")
        self.assertEqual(self.client.post(f"/api/v1/nodes/{identity}/token", headers={"Idempotency-Key": str(uuid4())}).status_code, 403)
