"""Durable Coordinator state and transactional inbox/outbox in SQLite.

The broker adapter must ACK only after a successful transaction. It publishes
pending outbox entries with confirms, then calls mark_published; a crash in
between intentionally repeats the same messageId.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from usvisa.protocol.messages import GroupConfig, Message, parse_message, timestamp, utc
from .observability import event


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ticks(value: datetime) -> int:
    delta = utc(value) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _datetime(value: int | None) -> datetime | None:
    return _EPOCH + timedelta(microseconds=value) if value is not None else None


@dataclass(frozen=True)
class NodeRecord:
    node_id: UUID
    username: str
    lifecycle: str
    active: bool
    min_date: date | None
    last_seen_at: datetime | None
    last_heartbeat_at: datetime | None
    start_at: datetime | None


@dataclass(frozen=True)
class ScheduleState:
    schedule_version: UUID | None
    revision: int
    config: GroupConfig
    report_before: date | None


@dataclass(frozen=True)
class OutboxItem:
    message: Message
    node_id: UUID
    exchange: str
    expires_at: datetime | None

    @property
    def routing_key(self) -> str:
        return str(self.node_id)

    def expiration_ms(self, now: datetime) -> int | None:
        if self.expires_at is None:
            return None
        remaining = _ticks(self.expires_at) - _ticks(now)
        return max(0, (remaining + 999) // 1000)


def _node(row) -> NodeRecord | None:
    if row is None:
        return None
    return NodeRecord(UUID(row["node_id"]), row["username"], row["lifecycle"], bool(row["active"]),
                      date.fromisoformat(row["min_date"]) if row["min_date"] else None,
                      _datetime(row["last_seen_at"]), _datetime(row["last_heartbeat_at"]),
                      _datetime(row["start_at"]))


class UnitOfWork:
    """Only usable inside Repository.transaction()."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.events = []

    def log_event(self, name, **fields):
        self.events.append((name, fields))

    def node(self, node_id: UUID) -> NodeRecord | None:
        return _node(self.connection.execute("SELECT * FROM nodes WHERE node_id = ?",
                                            (str(node_id),)).fetchone())

    def publisher(self, username: str) -> NodeRecord | None:
        return _node(self.connection.execute("SELECT * FROM nodes WHERE username = ?",
                                            (username,)).fetchone())

    def nodes(self) -> list[NodeRecord]:
        return [_node(row) for row in self.connection.execute("SELECT * FROM nodes ORDER BY node_id")]

    def schedule(self) -> ScheduleState | None:
        row = self.connection.execute("SELECT * FROM schedule WHERE singleton = 1").fetchone()
        if row is None:
            return None
        return ScheduleState(UUID(row["schedule_version"]) if row["schedule_version"] else None,
                             row["revision"], GroupConfig(row["consulate_id"], row["city"]),
                             date.fromisoformat(row["report_before"]) if row["report_before"] else None)

    def initialize(self, config: GroupConfig) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO schedule(singleton, revision, consulate_id, city) VALUES(1, 0, ?, ?)",
            (config.consulate_id, config.city))

    def heartbeat(self, node_id: UUID, min_date: date, sent_at: datetime, seen_at: datetime) -> None:
        self.connection.execute(
            "UPDATE nodes SET active = 1, min_date = ?, last_heartbeat_at = ?, last_seen_at = ? "
            "WHERE node_id = ?", (min_date.isoformat(), _ticks(sent_at), _ticks(seen_at), str(node_id)))

    def expire_members(self, cutoff: datetime) -> None:
        self.connection.execute("UPDATE nodes SET active = 0 WHERE active = 1 AND last_seen_at <= ?",
                                (_ticks(cutoff),))

    def revoke(self, node_id: UUID) -> None:
        self.connection.execute("UPDATE nodes SET lifecycle = 'revoked', active = 0 WHERE node_id = ?",
                                (str(node_id),))

    def save_schedule(self, state: ScheduleState, assignments: dict[UUID, datetime]) -> None:
        self.connection.execute(
            "UPDATE schedule SET schedule_version = ?, revision = ?, consulate_id = ?, city = ?, "
            "report_before = ? WHERE singleton = 1",
            (str(state.schedule_version), state.revision, state.config.consulate_id, state.config.city,
             state.report_before.isoformat() if state.report_before else None))
        self.connection.execute("UPDATE nodes SET start_at = NULL")
        self.connection.executemany("UPDATE nodes SET start_at = ? WHERE node_id = ?",
                                    [(_ticks(start), str(node)) for node, start in assignments.items()])

    def seen(self, node_id: UUID, message_id: UUID) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM inbox WHERE node_id = ? AND message_id = ?",
            (str(node_id), str(message_id))).fetchone() is not None

    def record(self, node_id: UUID, message: Message, status: str, reason: str | None,
               now: datetime) -> None:
        self.connection.execute(
            "INSERT INTO inbox(node_id, message_id, type, processed_at, status, reason) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (str(node_id), str(message.message_id), message.type, _ticks(now), status, reason))

    def enqueue(self, node_id: UUID, exchange: str, message: Message,
                expires_at: datetime | None = None) -> None:
        self.connection.execute(
            "INSERT INTO outbox(message_id, node_id, exchange, body, created_at, expires_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (str(message.message_id), str(node_id), exchange, message.to_json(), _ticks(message.sent_at),
             _ticks(expires_at) if expires_at else None))

    def finding(self, node_id: UUID, message: Message, expires_at: datetime) -> None:
        self.connection.execute(
            "INSERT INTO findings(node_id, message_id, observed_at, expires_at, dates) VALUES(?, ?, ?, ?, ?)",
            (str(node_id), str(message.message_id), _ticks(message.data.observed_at), _ticks(expires_at),
             json.dumps([day.isoformat() for day in message.data.dates])))

    def cancel_outbox(self, now: datetime) -> None:
        self.connection.execute(
            "UPDATE outbox SET status = 'cancelled' WHERE status = 'pending' AND "
            "((expires_at IS NOT NULL AND expires_at <= ?) OR node_id IN "
            "(SELECT node_id FROM nodes WHERE lifecycle = 'revoked'))", (_ticks(now),))


_SCHEMA = (
    """CREATE TABLE nodes(
        node_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('provisioning', 'ready', 'revoked')),
        active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
        min_date TEXT, last_seen_at INTEGER, last_heartbeat_at INTEGER, start_at INTEGER,
        CHECK(active = 0 OR (lifecycle = 'ready' AND min_date IS NOT NULL AND last_seen_at IS NOT NULL))
    )""",
    """CREATE TABLE schedule(
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1), schedule_version TEXT,
        revision INTEGER NOT NULL CHECK(revision >= 0), consulate_id INTEGER NOT NULL,
        city TEXT NOT NULL, report_before TEXT
    )""",
    """CREATE TABLE inbox(
        node_id TEXT NOT NULL REFERENCES nodes(node_id), message_id TEXT NOT NULL,
        type TEXT NOT NULL, processed_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('processed', 'ignored', 'rejected')), reason TEXT,
        PRIMARY KEY(node_id, message_id)
    )""",
    """CREATE TABLE findings(
        node_id TEXT NOT NULL, message_id TEXT NOT NULL, observed_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, dates TEXT NOT NULL,
        PRIMARY KEY(node_id, message_id),
        FOREIGN KEY(node_id, message_id) REFERENCES inbox(node_id, message_id) DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE outbox(
        message_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(node_id),
        exchange TEXT NOT NULL CHECK(exchange IN ('visa.control', 'visa.slots')),
        body TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'published', 'cancelled'))
    )""",
    "CREATE INDEX pending_outbox ON outbox(status, created_at)",
)

_PORTAL_SCHEMA = (
    """CREATE TABLE users(user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE portal_nodes(node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
        owner_id TEXT NOT NULL REFERENCES users(user_id), name TEXT NOT NULL, token_hash TEXT)""",
    """CREATE TABLE sessions(session_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
        csrf TEXT NOT NULL, expires_at REAL NOT NULL)""",
    """CREATE TABLE operations(owner_id TEXT NOT NULL REFERENCES users(user_id), key TEXT NOT NULL,
        action TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES nodes(node_id), completed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(owner_id, key))""",
    """CREATE TABLE login_limits(key TEXT PRIMARY KEY, started_at REAL NOT NULL, attempts INTEGER NOT NULL)""",
)


class Repository:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), isolation_level=None,
                                            check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            with self.transaction() as work:
                version = work.connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3, 4):
                    raise ValueError("Unsupported Coordinator database version")
                if version == 0:
                    for statement in _SCHEMA:
                        work.connection.execute(statement)
                if version < 2:
                    for statement in _PORTAL_SCHEMA:
                        work.connection.execute(statement)
                if version == 2:
                    work.connection.execute("ALTER TABLE users RENAME COLUMN email TO username")
                if version < 4:
                    from .logs import migrate
                    migrate(work.connection)
                    work.connection.execute("PRAGMA user_version = 4")
        except BaseException:
            self._connection.close()
            raise

    @contextmanager
    def transaction(self):
        work = UnitOfWork(self._connection)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield work
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        # Only report state changes after commit; rolled-back work stays silent.
        for name, fields in work.events:
            event(name, **fields)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def register_node(self, node_id: UUID) -> None:
        """Reserve identity; broker provisioning must finish before mark_ready()."""
        with self.transaction() as work:
            work.connection.execute(
                "INSERT INTO nodes(node_id, username, lifecycle) VALUES(?, ?, 'provisioning')",
                (str(node_id), f"node_{node_id}"))

    def mark_ready(self, node_id: UUID) -> None:
        with self.transaction() as work:
            node = work.node(node_id)
            if node is None or node.lifecycle == "revoked":
                raise ValueError("Node cannot complete provisioning")
            work.connection.execute("UPDATE nodes SET lifecycle = 'ready' WHERE node_id = ?",
                                    (str(node_id),))

    def schedule(self) -> ScheduleState | None:
        with self.transaction() as work:
            return work.schedule()

    def nodes(self) -> list[NodeRecord]:
        with self.transaction() as work:
            return work.nodes()

    def pending(self, *, now: datetime, limit: int = 100) -> list[OutboxItem]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        with self.transaction() as work:
            work.cancel_outbox(now)
            rows = work.connection.execute(
                "SELECT * FROM outbox WHERE status = 'pending' ORDER BY created_at, rowid LIMIT ?",
                (limit,)).fetchall()
            return [OutboxItem(parse_message(row["body"]), UUID(row["node_id"]), row["exchange"],
                               _datetime(row["expires_at"])) for row in rows]

    def mark_published(self, message_id: UUID) -> None:
        """Call only after the broker's publisher confirm."""
        with self.transaction() as work:
            work.connection.execute(
                "UPDATE outbox SET status = 'published' WHERE message_id = ? AND status = 'pending'",
                (str(message_id),))

    def view(self) -> dict:
        """One consistent, secret-free view for local inspection."""
        with self.transaction() as work:
            state = work.schedule()
            return {
                "scheduleVersion": str(state.schedule_version) if state and state.schedule_version else None,
                "revision": state.revision if state else 0,
                "config": state.config.to_dict() if state else None,
                "reportBefore": state.report_before.isoformat() if state and state.report_before else None,
                "nodes": [{"nodeId": str(node.node_id), "lifecycle": node.lifecycle, "active": node.active,
                           "minDate": node.min_date.isoformat() if node.min_date else None,
                           "lastSeenAt": timestamp(node.last_seen_at) if node.last_seen_at else None,
                           "startAt": timestamp(node.start_at) if node.start_at else None}
                          for node in work.nodes()],
            }
