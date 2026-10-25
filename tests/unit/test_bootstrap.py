import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
import requests

from usvisa.coordinator.auth import create_user
from usvisa.coordinator.config import ServerConfig
from usvisa.coordinator.portal import create_app
from usvisa.coordinator.repository import Repository
from usvisa.node.bootstrap import BootstrapError, Registration
from usvisa.protocol.messages import GroupConfig
from usvisa.shared.discovery import quick_origin, saved_origin, save_private_json


class PortalSession:
    def __init__(self, client):
        self.client = client
        self.lose_creation_response = False

    def request(self, method, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs["follow_redirects"] = kwargs.pop("allow_redirects")
        response = self.client.request(method, url, **kwargs)
        if self.lose_creation_response and method == "POST" and url.endswith("/nodes"):
            self.lose_creation_response = False
            raise requests.ConnectionError("Response lost after commit")
        return response


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repository = Repository(self.directory / "server.sqlite3")
        self.addCleanup(self.repository.close)
        create_user(self.repository, "owner", "owner-password")
        self.admin = AsyncMock()
        self.origin = "https://visa.example.com"
        config = ServerConfig(self.directory / "server.sqlite3", self.origin, GroupConfig(89, "Calgary"),
                              "amqp://unused", "http://unused", "admin", "unused", "coordinator", "unused")
        self.app = create_app(config, repository=self.repository, admin=self.admin, run_broker=False)
        self.client = TestClient(self.app, base_url=self.origin)
        self.addCleanup(self.client.close)
        self.session = PortalSession(self.client)
        self.path = self.directory / "node" / "registration.json"

    def provision(self, registration):
        return registration.provision(self.origin, "OWNER", "owner-password", "Main Node", self.session)

    def test_restart_reuses_identity_and_token_without_creating_another_node(self):
        first = Registration(self.path)
        token = self.provision(first)
        second = Registration(self.path)
        self.assertEqual(self.provision(second), token)
        self.assertEqual(first.value["nodeId"], second.value["nodeId"])
        self.assertEqual(len(self.repository.nodes()), 1)
        self.assertEqual(self.admin.provision.await_count, 1)
        persisted = self.path.read_text()
        self.assertNotIn("owner-password", persisted)
        self.assertEqual(json.loads(persisted)["username"], "owner")

    def test_lost_creation_response_recovers_token_for_the_same_identity(self):
        registration = Registration(self.path)
        self.session.lose_creation_response = True
        with self.assertRaises(requests.ConnectionError):
            self.provision(registration)
        identity = self.repository.nodes()[0].node_id
        recovered = Registration(self.path)
        token = self.provision(recovered)
        self.assertTrue(token.startswith(str(identity) + "."))
        self.assertEqual(len(self.repository.nodes()), 1)
        self.assertEqual(self.admin.provision.await_count, 2)

    def test_revoked_node_and_another_owner_are_not_automatically_restored(self):
        registration = Registration(self.path)
        self.provision(registration)
        identity = registration.value["nodeId"]
        response = self.client.post(f"/api/v1/nodes/{identity}/revoke", headers={
            "Origin": self.origin, "X-CSRF-Token": registration.csrf})
        self.assertEqual(response.status_code, 204)
        with self.assertRaisesRegex(BootstrapError, "revoked"):
            self.provision(Registration(self.path))
        with self.assertRaisesRegex(BootstrapError, "another portal login"):
            Registration(self.path).provision(self.origin, "other", "owner-password", "Main Node", self.session)
        self.assertEqual(self.admin.provision.await_count, 1)

    def test_redirect_is_rejected_before_credentials_can_follow_it(self):
        session = Mock()
        session.request.return_value.status_code = 302
        registration = Registration(self.path)
        registration.csrf = ""
        with self.assertRaises(BootstrapError):
            registration.request(session, self.origin, "/session", "POST", {"password": "private"})
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_quick_hostname_validation_and_saved_origin(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            response.read.return_value = b'{"hostname":"fresh-tunnel.trycloudflare.com"}'
            self.assertEqual(quick_origin("http://quick:20241/quicktunnel"), "https://fresh-tunnel.trycloudflare.com")
            response.read.return_value = b'{"hostname":"evil.example"}'
            with self.assertRaises(ValueError):
                quick_origin("http://quick:20241/quicktunnel")
        path = self.directory / "origin.json"
        save_private_json(path, {"origin": self.origin})
        self.assertEqual(saved_origin(path, "https://fallback.example"), self.origin)
        path.write_text("broken")
        self.assertEqual(saved_origin(path, "https://fallback.example"), "https://fallback.example")

    def test_dynamic_portal_origin_updates_host_origin_and_token_configuration(self):
        new_origin = "https://fresh-tunnel.trycloudflare.com"
        self.app.state.public_origin["value"] = new_origin
        self.assertEqual(self.client.get("/login").status_code, 400)
        with TestClient(self.app, base_url=new_origin) as client:
            registration = Registration(self.path)
            token = registration.provision(new_origin, "owner", "owner-password", "Main Node", PortalSession(client))
            self.assertTrue(token)
            self.assertEqual(registration.value["origin"], new_origin)
            self.assertEqual(client.get("/api/v1/me").json()["username"], "owner")
