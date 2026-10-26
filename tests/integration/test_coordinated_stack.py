"""Opt-in real RabbitMQ test; never contacts the visa site or SMTP.

Start a dedicated project first (see docs/coordinated-setup.md), then set
RUN_COORDINATED_INTEGRATION=true. Only that named project's broker is restarted.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest
import socket
import ssl
import threading
from unittest.mock import Mock, patch
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import websocket

from usvisa.node.state import NodeState
from usvisa.protocol.messages import GroupConfig, LogEntry, Message, NodeLogs, SlotsFound
from usvisa.transport.stomp import Decoder, encode
from usvisa.transport.webstomp import WebStomp, parse_token


ROOT = Path(__file__).resolve().parents[2]


def until(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError("Timed out waiting for coordinated stack")


@unittest.skipUnless(os.getenv("RUN_COORDINATED_INTEGRATION") == "true", "Explicit local RabbitMQ integration only")
class CoordinatedStackTests(unittest.TestCase):
    def test_self_signed_certificate_is_rejected_before_sending_node_credentials(self):
        project = os.getenv("COORDINATED_TEST_PROJECT", "usvisa-test")
        self.assertTrue(project.startswith("usvisa-test"))
        stem = f"/tmp/usvisa-tls-test-{uuid4()}"
        result = subprocess.run(["docker", "exec", f"{project}-rabbitmq-1", "openssl", "req", "-x509", "-newkey",
                                 "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=wrong-host",
                                 "-keyout", stem + ".key", "-out", stem + ".crt"],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        with tempfile.TemporaryDirectory() as directory:
            for extension in ("key", "crt"):
                response = subprocess.run(["docker", "exec", f"{project}-rabbitmq-1", "cat", stem + "." + extension],
                                           capture_output=True, check=True)
                Path(directory, extension).write_bytes(response.stdout)
            subprocess.run(["docker", "exec", f"{project}-rabbitmq-1", "rm", stem + ".key", stem + ".crt"], check=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(Path(directory, "crt"), Path(directory, "key"))
            for trust_test_certificate in (False, True):
                with self.subTest(trusted_chain=trust_test_certificate), socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(1)
                    listener.settimeout(5)

                    def handshake():
                        connection, _ = listener.accept()
                        try:
                            context.wrap_socket(connection, server_side=True).close()
                        except ssl.SSLError:
                            connection.close()

                    thread = threading.Thread(target=handshake, daemon=True)
                    thread.start()
                    transport = WebStomp(f"https://127.0.0.1:{listener.getsockname()[1]}", f"{uuid4()}." + "a" * 43,
                                         date(2027, 12, 1), Mock())
                    with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                            "WEBSOCKET_CLIENT_CA_BUNDLE": str(Path(directory, "crt")) if trust_test_certificate else ""}):
                        with self.assertRaises(ssl.SSLCertVerificationError) as raised:
                            transport.session()
                        if trust_test_certificate:
                            self.assertIn("mismatch", raised.exception.verify_message.lower())
                    thread.join(timeout=3)

    def test_real_provisioning_schedules_direct_delivery_permissions_and_broker_restart(self):
        project = os.getenv("COORDINATED_TEST_PROJECT", "usvisa-test")
        if not project.startswith("usvisa-test"):
            self.fail("Use an isolated project name starting with usvisa-test")
        origin = os.getenv("COORDINATED_TEST_URL", "http://127.0.0.1:8187")
        from urllib.parse import urlsplit
        self.assertIn(urlsplit(origin).hostname, {"localhost", "127.0.0.1"})
        username, password = f"test-{uuid4()}", secrets.token_urlsafe(24)
        command = ["docker", "exec", "-i", f"{project}-coordinator-1", "usvisa-coordinator", "create-user",
                   "--username", username, "--password-stdin"]
        result = subprocess.run(command, input=password + "\n", text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        client = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=20)
        self.addCleanup(client.close)
        csrf = client.get("/api/v1/csrf").json()["csrfToken"]
        response = client.post("/api/v1/session", json={"username": username, "password": password}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)
        client.headers["X-CSRF-Token"] = response.json()["csrfToken"]
        nodes = []
        for name in ("Integration A", "Integration B"):
            response = client.post("/api/v1/nodes", json={"name": name}, headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(response.status_code, 201, response.text)
            nodes.append(response.json())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        states, transports = [], []
        for index, node in enumerate(nodes):
            state = NodeState(Path(directory.name) / f"{index}.sqlite3", UUID(node["nodeId"]), GroupConfig(95, "Vancouver"))
            states.append(state)
            self.addCleanup(state.close)
            transport = WebStomp(origin, node["token"], date(2027, 11 if index == 0 else 12, 1), state, allow_local=True)
            transports.append(transport)
            self.addCleanup(transport.stop)
            transport.start()
        until(lambda: all(state.snapshot() and state.snapshot().data.revision >= 2 for state in states))
        until(lambda: states[0].snapshot().data.schedule_version == states[1].snapshot().data.schedule_version)
        self.assertEqual(abs((states[0].snapshot().data.start_at - states[1].snapshot().data.start_at).total_seconds()), 90)
        self.assertEqual(states[0].snapshot().data.report_before, date(2027, 12, 1))

        # Updated peers get logging policy through their existing personal inbox.
        # Telemetry uses durable SEND receipts and is browsable only by its owner.
        until(lambda: all(state.logging_config() is not None for state in states))
        observed, trace = datetime.now(timezone.utc), uuid4()
        entries = (
            LogEntry(uuid4(), observed, "poll", "info", "poll_completed", trace, None,
                     {"dates": ["2027-11-15"], "count": 1, "status": "success"}),
            LogEntry(uuid4(), observed, "reschedule", "warning", "rate_limited", trace, "reschedule",
                     {"status": 429, "endpoint": "/appointment/times/95.json"}),
            LogEntry(uuid4(), observed, "error", "error", "operation_exception", trace, "reschedule",
                     {"error_type": "PortalError", "message": "Isolated diagnostic fixture"}),
        )
        telemetry = Message.create(NodeLogs(UUID(nodes[0]["nodeId"]), entries), sent_at=observed)
        states[0].add_telemetry(telemetry, observed + timedelta(days=7))
        until(lambda: not states[0].pending_telemetry(datetime.now(timezone.utc)))
        activity = until(lambda: client.get("/api/v1/logs").json()["events"])
        self.assertEqual(activity[0]["nodeId"], nodes[0]["nodeId"])
        event = client.get(f"/api/v1/logs/{activity[0]['id']}").json()
        self.assertEqual(len(event["trace"]), 3)
        self.assertEqual(len(client.get("/api/v1/logs", params={"view": "errors"}).json()["events"]), 1)
        self.assertEqual(len(client.get("/api/v1/logs", params={"view": "reschedule"}).json()["events"]), 2)
        states[0].add_telemetry(telemetry, observed + timedelta(days=7))
        until(lambda: not states[0].pending_telemetry(datetime.now(timezone.utc)))
        self.assertEqual(client.get("/api/v1/logs").json()["total"], 1)

        # Broker must prevent a second connection with the same credentials,
        # including Web STOMP (not just raw AMQP).
        identity, secret = parse_token(nodes[0]["token"])
        extra = websocket.create_connection(origin.replace("http:", "ws:") + "/ws", timeout=3,
                                             origin=origin, subprotocols=["v12.stomp"])
        try:
            extra.send(encode("CONNECT", {"host": "visa", "login": f"node_{identity}", "passcode": secret,
                                         "accept-version": "1.2"}))
            frames = Decoder().feed(extra.recv())
            self.assertTrue(any(frame.command == "ERROR" for frame in frames), "Second connection was not refused")
        finally:
            extra.close()
            extra.shutdown()

        # Recipient disconnects. Its durable queue must retain the finding over
        # a broker restart. A is the source; the date only matches B's minDate.
        transports[1].stop()
        observation = datetime.now(timezone.utc)
        finding = Message.create(SlotsFound(UUID(nodes[0]["nodeId"]), observation, (date(2027, 11, 15),)), sent_at=observation)
        states[0].add_finding(finding, observation + timedelta(seconds=180))
        until(lambda: not states[0].pending(datetime.now(timezone.utc)))
        time.sleep(1)
        subprocess.run(["docker", "restart", f"{project}-rabbitmq-1"], check=True, capture_output=True, timeout=40)
        replacement = WebStomp(origin, nodes[1]["token"], date(2027, 12, 1), states[1], allow_local=True)
        self.addCleanup(replacement.stop)
        replacement.start()
        task = until(lambda: states[1].next_task(datetime.now(timezone.utc)), timeout=50)
        self.assertEqual(task.data.finding_id, finding.message_id)
        self.assertEqual(task.data.dates, (date(2027, 11, 15),))
        self.assertEqual(task.data.expires_at, observation + timedelta(seconds=180))
        self.assertIsNone(states[0].next_task(datetime.now(timezone.utc)))
        states[1].finish_task(task.data.finding_id)
        until(lambda: transports[0].connected.is_set())
        states[0].add_finding(finding, observation + timedelta(seconds=180))
        until(lambda: not states[0].pending(datetime.now(timezone.utc)))
        time.sleep(.5)
        self.assertIsNone(states[1].next_task(datetime.now(timezone.utc)))

        # Rotation preserves queue/identity and disconnects the previous secret.
        response = client.post(f"/api/v1/nodes/{nodes[0]['nodeId']}/token", headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["nodeId"], nodes[0]["nodeId"])
        self.assertNotEqual(response.json()["token"], nodes[0]["token"])
        until(lambda: not transports[0].connected.is_set())
        transports[0].stop()
        rotated = WebStomp(origin, response.json()["token"], date(2027, 11, 1), states[0], allow_local=True)
        self.addCleanup(rotated.stop)
        rotated.start()
        until(lambda: rotated.connected.is_set())
        self.assertEqual(client.post(f"/api/v1/nodes/{nodes[1]['nodeId']}/revoke").status_code, 204)
        until(lambda: not replacement.connected.is_set())
        self.assertEqual(client.post(f"/api/v1/nodes/{nodes[0]['nodeId']}/revoke").status_code, 204)
        # Save this isolated account for browser visual QA; it has no visa data.
        artifact = ROOT / "artifacts" / "browser-test.json"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
