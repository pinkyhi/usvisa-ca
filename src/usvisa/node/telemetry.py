"""Forward selected audit records through a separate durable low-priority outbox."""
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID
from dataclasses import replace

from usvisa.protocol.messages import LogEntry, Message, NodeLogs
from usvisa.shared.logdata import safe_details
from usvisa.shared.bodylogs import BODY_EVENTS, chunks


class Telemetry:
    def __init__(self, state):
        self.state = state
        self.secrets = [value for key, value in os.environ.items()
                        if any(part in key.upper() for part in ("PASSWORD", "TOKEN", "SECRET", "APPLICATION_PWD"))]

    def group_enabled(self, group):
        policy = self.state.logging_config()
        return bool(policy and group in policy.log_groups)

    def __call__(self, record):
        policy = self.state.logging_config()
        group = record.get("log_group")
        is_body = record["event"] in BODY_EVENTS
        if is_body and (group != "reschedule" or not self.group_enabled(group)):
            return
        level = record.get("level", "info")
        if level == "error":
            category = "error"
        elif record["event"] == "poll_completed" and policy and policy.verbose:
            category = "poll"
        elif policy and group in policy.log_groups:
            category = "reschedule"
        else:
            return
        observed = datetime.fromisoformat(record["timestamp"])
        details = safe_details(record, self.secrets)
        entry = LogEntry(UUID(record["event_id"]), observed, category, level, record["event"],
                         UUID(record["trace_id"]) if record.get("trace_id") else None, group, details)
        if is_body:
            details["body"] = record["body"]
            messages = [Message.create(NodeLogs(self.state.node_id, (replace(entry, details=part),)),
                                       sent_at=datetime.now(timezone.utc)) for part in chunks(details)]
            self.state.add_telemetry_batch(messages, observed + timedelta(days=7))
            return
        message = Message.create(NodeLogs(self.state.node_id, (entry,)), sent_at=datetime.now(timezone.utc))
        self.state.add_telemetry(message, observed + timedelta(days=7))
