"""One independent communication thread: WSS, heartbeat, receipts and manual ACK."""
import logging
from collections import deque
import random
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from uuid import UUID

import websocket

from usvisa.protocol.messages import Error, Heartbeat, Message, NodeConfig, ProtocolError, ScheduleUpdated, SlotsAvailable, parse_message
from usvisa.shared.network import server_origin, websocket_url
from .stomp import Decoder, encode


log = logging.getLogger(__name__)


def parse_token(token):
    identity, separator, secret = token.partition(".")
    try:
        node = UUID(identity)
    except ValueError:
        raise ValueError("NODE_TOKEN must be nodeId.secret from the portal") from None
    if str(node) != identity or not separator or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", secret):
        raise ValueError("NODE_TOKEN must be nodeId.secret from the portal")
    return node, secret


class WebStomp:
    def __init__(self, origin, token, min_date, state, *, allow_local=False, origin_provider=None, passive_provider=None):
        self.origin = server_origin(origin, allow_local=allow_local)
        self.origin_provider, self.allow_local = origin_provider, allow_local
        self.node_id, self.secret = parse_token(token)
        self.min_date, self.state = min_date, state
        self.passive_provider = passive_provider or (lambda: False)
        self.heartbeat_ready = threading.Event()
        self.stopping = threading.Event()
        self.connected = threading.Event()
        self.fatal = None
        self.thread = threading.Thread(target=self.run, name="coordinator-communication", daemon=True)

    def start(self):
        self.thread.start()

    def refresh_heartbeat(self):
        self.heartbeat_ready.set()

    def heartbeat(self):
        saved = self.state.snapshot()
        policy = self.state.logging_config()
        passive = bool(self.passive_provider())
        return Message.create(Heartbeat(self.node_id, None if passive else self.min_date,
                                       saved.data.schedule_version if saved else None,
                                       policy.config_version if policy else None, True, passive=passive),
                              sent_at=datetime.now(timezone.utc))

    def stop(self):
        self.stopping.set()
        self.state.outbox_ready.set()
        self.state.work_available.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)

    def run(self):
        attempt = 0
        while not self.stopping.is_set():
            try:
                self.session()
                attempt = 0
            except ProtocolError as error:
                self.fatal = str(error)
                self.stopping.set()
                log.error("Coordinator protocol error: %s", error.code)
            except Exception as error:
                log.warning("Coordinator connection unavailable (%s)", type(error).__name__)
                attempt = min(attempt + 1, 5)
            finally:
                self.connected.clear()
            self.stopping.wait(random.uniform(.5, min(30, 2 ** attempt)))

    def session(self):
        if self.origin_provider:
            self.origin = server_origin(self.origin_provider(), allow_local=self.allow_local)
        # Explicit verification; there is no insecure TLS switch.
        socket = websocket.WebSocket(enable_multithread=True, sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True})
        try:
            socket.connect(websocket_url(self.origin), timeout=1, redirect_limit=0,
                           subprotocols=["v12.stomp"], origin=self.origin)
            self.exchange(socket)
        finally:
            socket.close(timeout=1)
            # recv(CLOSE) can clear `connected` before close(), which otherwise
            # skips releasing the underlying TLS socket on reconnect.
            socket.shutdown()

    def exchange(self, socket):
        decoder = Decoder()
        pending = {}
        pending_lock = threading.Lock()
        recent_receipts = deque(maxlen=512)
        session_done = threading.Event()
        publisher_errors = []
        publisher = None
        wire_activity = [time.monotonic()]

        def publish_outbox():
            while not session_done.is_set() and not self.stopping.is_set():
                self.state.outbox_ready.clear()
                try:
                    tick_now = datetime.now(timezone.utc)
                    for message, expiry in [*self.state.pending(tick_now), *self.state.pending_telemetry(tick_now)]:
                        if session_done.is_set() or self.stopping.is_set():
                            return
                        receipt = str(message.message_id)
                        remaining = max(0, int((expiry - time.time()) * 1000))
                        if not remaining:
                            continue
                        with pending_lock:
                            if receipt in recent_receipts:
                                # A stale DB snapshot can race a receipt. An
                                # explicitly re-enqueued immutable message ID
                                # is also already durably confirmed here.
                                self.state.confirmed(message.message_id)
                                continue
                            if receipt in pending:
                                continue
                            pending[receipt] = time.monotonic()
                        headers = self.headers(message, remaining)
                        headers.update({"receipt": receipt, "persistent": "true"})
                        socket.send(encode("SEND", headers, message.to_json()))
                        wire_activity[0] = time.monotonic()
                except Exception as error:
                    publisher_errors.append(error)
                    session_done.set()
                    socket.shutdown()
                    return
                self.state.outbox_ready.wait(.5)

        sent_hb = sent_wire = last_rx = connected_at = time.monotonic()
        handshake = False
        subscribed = False
        receive_timeout = 30
        socket.send(encode("CONNECT", {"accept-version": "1.2", "host": "visa",
                                     "login": f"node_{self.node_id}", "passcode": self.secret,
                                     "heart-beat": "10000,10000"}))
        try:
            while not self.stopping.is_set():
                tick = time.monotonic()
                if publisher_errors:
                    raise publisher_errors[0]
                if tick - last_rx > receive_timeout:
                    raise TimeoutError("STOMP peer stopped sending heartbeats")
                if not handshake and tick - connected_at > 10:
                    raise TimeoutError("STOMP handshake timed out")
                if subscribed:
                    if tick - sent_hb >= 30 or self.heartbeat_ready.is_set():
                        self.heartbeat_ready.clear()
                        hb = self.heartbeat()
                        socket.send(encode("SEND", self.headers(hb, 60_000), hb.to_json()))
                        sent_hb = sent_wire = tick
                    with pending_lock:
                        timed_out = any(tick - at > 20 for at in pending.values())
                    if timed_out:
                        raise TimeoutError("Broker receipt timed out")
                if handshake and tick - max(sent_wire, wire_activity[0]) >= 10:
                    socket.send("\n")
                    socket.ping()
                    sent_wire = tick
                try:
                    raw = socket.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if raw == "" or raw == b"":
                    raise ConnectionError("WebSocket closed")
                last_rx = time.monotonic()
                for frame in decoder.feed(raw):
                    if frame.command == "CONNECTED":
                        if frame.headers.get("version") != "1.2":
                            raise ProtocolError("UNSUPPORTED_VERSION", "Broker did not negotiate STOMP 1.2")
                        server_send, _ = map(int, frame.headers.get("heart-beat", "0,0").split(","))
                        receive_timeout = max(30, server_send / 1000 * 3) if server_send else 90
                        handshake = True
                        socket.send(encode("SUBSCRIBE", {"id": "inbox", "destination":
                            f"/amq/queue/visa.node.{self.node_id}.inbox", "ack": "client-individual",
                            "prefetch-count": "10", "receipt": "subscribed"}))
                        sent_wire = tick
                    elif frame.command == "RECEIPT":
                        receipt = frame.headers.get("receipt-id")
                        if receipt == "subscribed" and publisher is None:
                            subscribed = True
                            self.connected.set()
                            log.info("Coordinator connected; personal inbox subscribed, heartbeat every 30s")
                            sent_hb = -1e9
                            publisher = threading.Thread(target=publish_outbox, name="coordinator-publisher", daemon=True)
                            publisher.start()
                        else:
                            with pending_lock:
                                if receipt in pending:
                                    self.state.confirmed(UUID(receipt))
                                    recent_receipts.append(receipt)
                                    del pending[receipt]
                                    self.state.outbox_ready.set()
                    elif frame.command == "MESSAGE":
                        try:
                            message = parse_message(frame.body)
                            if (frame.headers.get("amqp-message-id") != str(message.message_id)
                                    or frame.headers.get("type") != message.type or "ack" not in frame.headers):
                                raise ProtocolError("INVALID_MESSAGE", "Broker delivery metadata mismatch")
                            previous = self.state.snapshot() if isinstance(message.data, ScheduleUpdated) else None
                            accepted = self.state.accept(message, now=datetime.now(timezone.utc))
                        except ProtocolError:
                            if "ack" in frame.headers:
                                socket.send(encode("NACK", {"id": frame.headers["ack"], "requeue": "false"}))
                            raise
                        socket.send(encode("ACK", {"id": frame.headers["ack"]}))
                        data = message.data
                        if isinstance(data, NodeConfig):
                            log.info("Logging configuration received: verbose=%s, logGroups=%s", data.verbose, ",".join(data.log_groups))
                        if isinstance(data, ScheduleUpdated) and (previous is None or data.revision > previous.data.revision):
                            log.info("Schedule received: revision=%s, startAt=%s, interval=%ss, reportBefore=%s",
                                     data.revision, data.start_at.isoformat(), data.interval_seconds, data.report_before)
                        elif isinstance(data, SlotsAvailable) and accepted != "duplicate":
                            log.info("Shared slots received: %s date(s), expiresAt=%s", len(data.dates), data.expires_at.isoformat())
                        if isinstance(message.data, Error):
                            log.error("Coordinator rejected a command: %s", message.data.code)
                            raise ProtocolError(message.data.code, "Coordinator rejected a command; check configuration and clocks")
                    elif frame.command == "ERROR":
                        # Rabbit errors can contain original CONNECT headers; never echo them.
                        raise ConnectionError("Broker rejected the STOMP operation")
        finally:
            session_done.set()
            self.state.outbox_ready.set()
            if publisher:
                publisher.join(timeout=2)
            self.connected.clear()

    def headers(self, message, expiration):
        return {"destination": "/exchange/visa.commands/coordinator", "content-type": "application/json",
                "amqp-message-id": str(message.message_id), "type": message.type,
                "user-id": f"node_{self.node_id}", "expiration": str(expiration)}
