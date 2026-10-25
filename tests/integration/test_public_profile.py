"""Opt-in HTTPS/WSS smoke test for any of the three configured server profiles."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import ssl
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx

from usvisa.node.state import NodeState
from usvisa.protocol.messages import GroupConfig, Message, SlotsFound
from usvisa.transport.webstomp import WebStomp
from usvisa.transport.stomp import Decoder, encode
import websocket
from contextlib import contextmanager
from unittest.mock import Mock, patch
from tests.integration.test_coordinated_stack import until


@unittest.skipUnless(os.getenv("RUN_PUBLIC_PROFILE_INTEGRATION") == "true", "Explicit public profile smoke test only")
class PublicProfileTests(unittest.TestCase):
    def test_https_portal_and_wss_snapshot_finding_rotation(self):
        from scripts.server import read_env
        configuration = read_env(Path(os.getenv("COORDINATED_TEST_ENV", "artifacts/test-server.env")))
        origin = configuration["PUBLIC_BASE_URL"]
        self.assertTrue(origin.startswith("https://"))
        credentials = json.loads(Path(os.getenv("COORDINATED_TEST_ACCOUNT", "artifacts/browser-test.json")).read_text())
        client = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=20)
        self.addCleanup(client.close)
        until(lambda: client.get("/health/ready").status_code == 200)
        csrf = client.get("/api/v1/csrf").json()["csrfToken"]
        response = client.post("/api/v1/session", json=credentials, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrfToken"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        nodes, states, transports = [], [], []
        try:
            for index in range(2):
                response = client.post("/api/v1/nodes", json={"name": f"WSS profile test {index}"},
                                       headers={"Idempotency-Key": str(uuid4())})
                self.assertEqual(response.status_code, 201, response.text)
                node = response.json()
                nodes.append(node)
                state = NodeState(Path(temporary.name) / f"{index}.sqlite3", UUID(node["nodeId"]),
                                  GroupConfig(int(configuration["CONSULATE_ID"]), configuration["CITY"]))
                states.append(state)
                transport = WebStomp(origin, node["token"], date(2027, 11 if index == 0 else 12, 1), state)
                transports.append(transport)
                transport.start()
            until(lambda: all(state.snapshot() for state in states), timeout=40)
            until(lambda: states[0].snapshot().data.schedule_version == states[1].snapshot().data.schedule_version)
            self.assertEqual(abs((states[0].snapshot().data.start_at - states[1].snapshot().data.start_at).total_seconds()), 90)
            observed = datetime.now(timezone.utc)
            command = Message.create(SlotsFound(UUID(nodes[0]["nodeId"]), observed, (date(2027, 11, 15),)), sent_at=observed)
            states[0].add_finding(command, observed + timedelta(seconds=180))
            event = until(lambda: states[1].next_task(datetime.now(timezone.utc)))
            self.assertEqual(event.data.finding_id, command.message_id)
            self.assertEqual(event.data.expires_at, observed + timedelta(seconds=180))
            first_seen = {node["nodeId"]: node["lastSeenAt"] for node in client.get("/api/v1/nodes").json()["nodes"]}
            # Simulate a blocking foreground visa operation while the dedicated
            # communication threads must still send their next heartbeat.
            time.sleep(31)
            refreshed = {node["nodeId"]: node["lastSeenAt"] for node in client.get("/api/v1/nodes").json()["nodes"]}
            for node in nodes:
                self.assertGreater(refreshed[node["nodeId"]], first_seen[node["nodeId"]] + 25)
            # Each denied operation uses an isolated credential so connection
            # teardown timing cannot mask a permissions failure.
            for forbidden in ("read-peer", "publish-control", "spoof-user"):
                response = client.post("/api/v1/nodes", json={"name": f"Permission check {forbidden}"},
                                       headers={"Idempotency-Key": str(uuid4())})
                self.assertEqual(response.status_code, 201)
                restricted = response.json()
                nodes.append(restricted)
                identity, secret = restricted["token"].split(".")
                socket = websocket.create_connection(origin.replace("https:", "wss:") + "/ws", timeout=5,
                    subprotocols=["v12.stomp"], origin=origin,
                    sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True})
                try:
                    decoder = Decoder()
                    socket.send(encode("CONNECT", {"accept-version": "1.2", "host": "visa",
                                                   "login": f"node_{identity}", "passcode": secret}))
                    self.assertEqual(decoder.feed(socket.recv())[0].command, "CONNECTED")
                    if forbidden == "read-peer":
                        socket.send(encode("SUBSCRIBE", {"id": "foreign", "ack": "client-individual",
                            "destination": f"/amq/queue/visa.node.{nodes[1]['nodeId']}.inbox"}))
                    else:
                        headers = {"destination": f"/exchange/visa.control/{nodes[1]['nodeId']}" if forbidden == "publish-control"
                                   else "/exchange/visa.commands/coordinator", "receipt": "denied", "persistent": "true"}
                        if forbidden == "spoof-user":
                            headers["user-id"] = f"node_{nodes[1]['nodeId']}"
                        socket.send(encode("SEND", headers, "{}"))
                    frames = []
                    closed = False
                    deadline = time.monotonic() + 8
                    while not any(frame.command == "ERROR" for frame in frames) and time.monotonic() < deadline:
                        try:
                            frames.extend(decoder.feed(socket.recv()))
                        except websocket.WebSocketConnectionClosedException:
                            closed = True
                            break
                    self.assertTrue(closed or any(frame.command == "ERROR" for frame in frames), f"Broker allowed {forbidden}")
                    self.assertFalse(any(frame.command == "RECEIPT" for frame in frames), "Forbidden publication was confirmed")
                    self.assertIsNone(transports[1].fatal)
                finally:
                    socket.close(timeout=1)
                    socket.shutdown()
            response = client.post(f"/api/v1/nodes/{nodes[0]['nodeId']}/token",
                                   headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(response.status_code, 200)
            until(lambda: not transports[0].connected.is_set())
            transports[0].stop()
            replacement = WebStomp(origin, response.json()["token"], date(2027, 11, 1), states[0])
            transports.append(replacement)
            replacement.start()
            until(lambda: replacement.connected.is_set())
            # Exercise the actual Node entry point, worker, local SQLite,
            # reporting and bounded shutdown against WSS. Only the visa session
            # and first timer are substituted; no browser, POST or email runs.
            from usvisa.node import coordinated
            from usvisa.visa.appointment_client import BookingResult
            response = client.post("/api/v1/nodes", json={"name": "Complete Node dry run"},
                                   headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(response.status_code, 201)
            worker_node = response.json()
            nodes.append(worker_node)
            visa = Mock()
            visa.get_available_dates.return_value = [date(2027, 11, 15)]
            visa.book.return_value = BookingResult(date(2027, 11, 15), "09:00", True)

            @contextmanager
            def fake_session():
                yield visa

            with patch.dict(os.environ, {"NODE_TOKEN": worker_node["token"], "NODE_MIN_DATE": "2027-11-01",
                                         "SERVER_URL": origin}), \
                 patch.multiple(coordinated.cfg, MODE="coordinated", USER_EMAIL="offline@example.test",
                                USER_PASSWORD="offline-test-only", USER_CONSULATE="Vancouver", RUN_ONCE=True,
                                TEST_MODE=True, EXCLUSION_DATE_RANGES=[], EARLIEST_ACCEPTABLE_DATE="2027-11-01", LATEST_ACCEPTABLE_DATE="2027-11-30"), \
                 patch.object(coordinated, "authenticated_client", fake_session), \
                 patch.object(coordinated, "data_directory", return_value=Path(temporary.name)), \
                 patch.object(coordinated, "next_check_at", side_effect=lambda *args: time.time()), \
                 patch.object(coordinated.audit, "LOG_PATH", Path(temporary.name) / "audit.jsonl"), \
                 patch.object(coordinated.solo, "AVAILABLE_DATES_LOG_PATH", Path(temporary.name) / "dates.log"), \
                 patch.object(coordinated.solo, "send_notification", return_value=False):
                self.assertEqual(coordinated.main(), 0)
            visa.book.assert_called_once_with(date(2027, 11, 15), dry_run=True)
            relayed = until(lambda: states[1].next_task(datetime.now(timezone.utc)))
            self.assertEqual(relayed.data.source_node_id, UUID(worker_node["nodeId"]))
            self.assertTrue((Path(temporary.name) / ".node-state" / worker_node["nodeId"] / "state.sqlite3").is_file())
        finally:
            for transport in transports:
                transport.stop()
            for node in nodes:
                client.post(f"/api/v1/nodes/{node['nodeId']}/revoke")
            for state in states:
                state.close()
