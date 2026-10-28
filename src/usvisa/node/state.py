"""Node persistence, separate from existing cookies and booking guards."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from usvisa.protocol.messages import Error, NodeConfig, ProtocolError, ScheduleUpdated, SlotsAvailable, parse_message
from usvisa.shared.locking import InstanceLock as InstanceLock


class NodeState:
    def __init__(self, path, node_id, config):
        self.node_id, self.config = node_id, config
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.outbox_ready = threading.Event()
        self.work_available = threading.Event()
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL, "
                       "expires REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")
            db.execute("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS telemetry_outbox(id TEXT PRIMARY KEY, body TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("UPDATE tasks SET status='pending' WHERE status='running'")
        saved = self.snapshot()
        if saved and (saved.data.node_id != node_id or saved.data.config != config):
            self.close()
            raise ValueError("Saved schedule does not match the nodeId or local consulate")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def close(self):
        with self.lock:
            self.connection.close()

    def get(self, key, default=None):
        with self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, json.dumps(value)))

    def snapshot(self):
        body = self.get("schedule")
        return parse_message(body) if body else None

    def logging_config(self):
        body = self.get("logging_config")
        return parse_message(body).data if body else None

    def accept(self, message, *, now):
        data = message.data
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM inbox WHERE id=?", (str(message.message_id),)).fetchone():
                return "duplicate"
            if isinstance(data, NodeConfig):
                if data.node_id != self.node_id:
                    raise ProtocolError("INVALID_MESSAGE", "Logging configuration identity differs")
                db.execute("INSERT OR REPLACE INTO meta VALUES('logging_config', ?)", (json.dumps(message.to_json()),))
            elif isinstance(data, ScheduleUpdated):
                if data.node_id != self.node_id or data.config != self.config:
                    raise ProtocolError("INVALID_MESSAGE", "Schedule identity or consulate differs from local configuration")
                row = db.execute("SELECT value FROM meta WHERE key='schedule'").fetchone()
                old = parse_message(json.loads(row[0])).data if row else None
                if old and data.revision == old.revision and data.schedule_version != old.schedule_version:
                    raise ProtocolError("INVALID_SCHEDULE_VERSION", "Equal revisions have different schedule UUIDs")
                if old and data.revision == old.revision and data != old:
                    raise ProtocolError("INVALID_SCHEDULE_VERSION", "Same schedule version contains different state")
                if old is None or data.revision > old.revision:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('schedule', ?)", (json.dumps(message.to_json()),))
            elif isinstance(data, SlotsAvailable):
                if data.expires_at > now and data.source_node_id != self.node_id:
                    # findingId also guards against a repeated relay with a new delivery ID.
                    db.execute("INSERT OR IGNORE INTO tasks(id, body, expires) VALUES(?, ?, ?)",
                               (str(data.finding_id), message.to_json(), data.expires_at.timestamp()))
            elif not isinstance(data, Error):
                raise ProtocolError("INVALID_MESSAGE", "Unexpected server command")
            db.execute("INSERT INTO inbox VALUES(?)", (str(message.message_id),))
        self.work_available.set()
        return "accepted"

    def add_finding(self, message, expires_at):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO outbox VALUES(?, ?, ?)",
                       (str(message.message_id), message.to_json(), expires_at.timestamp()))
        self.outbox_ready.set()

    def pending(self, now):
        with self.transaction() as db:
            db.execute("DELETE FROM outbox WHERE expires <= ?", (now.timestamp(),))
            return [(parse_message(row["body"]), row["expires"]) for row in
                    db.execute("SELECT * FROM outbox ORDER BY rowid LIMIT 20")]

    def confirmed(self, message_id):
        with self.transaction() as db:
            db.execute("DELETE FROM outbox WHERE id=?", (str(message_id),))
            db.execute("DELETE FROM telemetry_outbox WHERE id=?", (str(message_id),))

    def add_telemetry(self, message, expires_at):
        self.add_telemetry_batch((message,), expires_at)

    def add_telemetry_batch(self, messages, expires_at):
        # Persist all parts together: a crash cannot leave a partial body queued.
        with self.transaction() as db:
            db.executemany("INSERT OR IGNORE INTO telemetry_outbox VALUES(?,?,?)",
                           [(str(message.message_id), message.to_json(), expires_at.timestamp()) for message in messages])
        self.outbox_ready.set()

    def pending_telemetry(self, now):
        with self.transaction() as db:
            db.execute("DELETE FROM telemetry_outbox WHERE expires<=?", (now.timestamp(),))
            return [(parse_message(row["body"]), row["expires"]) for row in
                    db.execute("SELECT * FROM telemetry_outbox ORDER BY rowid LIMIT 10")]

    def next_task(self, now):
        with self.transaction() as db:
            db.execute("UPDATE tasks SET status='expired' WHERE expires <= ? AND status IN ('pending','running')",
                       (now.timestamp(),))
            row = db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE tasks SET status='running' WHERE id=?", (row["id"],))
                return parse_message(row["body"])

    def finish_task(self, finding_id):
        with self.transaction() as db:
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (str(finding_id),))

    def retry_task(self, finding_id):
        with self.transaction() as db:
            db.execute("UPDATE tasks SET status='pending' WHERE id=? AND status='running'", (str(finding_id),))
