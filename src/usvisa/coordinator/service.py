"""Single-group Coordinator core. No network, account cookies or booking code."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from usvisa.protocol.messages import (
    CLOCK_SKEW_SECONDS, FINDING_TTL_SECONDS, HEARTBEAT_TTL_SECONDS, INTERVAL_SECONDS,
    PRESENCE_SECONDS, Error, GroupConfig, Heartbeat, Message, ScheduleUpdated,
    SlotsAvailable, SlotsFound, NodeConfig, NodeLogs, LogEntry, utc,
)

from .repository import NodeRecord, Repository, ScheduleState, UnitOfWork
from .scheduling import assign_phases


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    reason: str | None = None


class Coordinator:
    def __init__(self, repository: Repository, config: GroupConfig, *, now: datetime, verbose=False, log_groups=()):
        self.repository = repository
        self.config = config
        self.verbose, self.log_groups = verbose, tuple(log_groups)
        self.logging_version = uuid5(NAMESPACE_URL, "usvisa.logging:" + str(verbose) + ",".join(self.log_groups))
        # Startup expires persisted presence rather than treating every restart as
        # a fresh group. Unchanged live state keeps the same UUID and revision.
        with repository.transaction() as work:
            work.initialize(config)
            self._reconcile(work, utc(now))

    def _snapshot(self, work: UnitOfWork, node: NodeRecord, now: datetime,
                  in_reply_to: UUID | None = None) -> None:
        state = work.schedule()
        message = Message.create(ScheduleUpdated(
            node.node_id, state.schedule_version, state.revision, node.start_at,
            INTERVAL_SECONDS, state.report_before, state.config), sent_at=now, in_reply_to=in_reply_to)
        work.enqueue(node.node_id, "visa.control", message)

    def _reconcile(self, work: UnitOfWork, now: datetime) -> bool:
        was_active = {node.node_id for node in work.nodes() if node.active}
        work.expire_members(now - timedelta(seconds=PRESENCE_SECONDS))
        nodes = work.nodes()
        active = {node.node_id: node for node in nodes if node.active}
        previous = {node.node_id: node.start_at for node in nodes if node.start_at is not None}
        state = work.schedule()
        report_before = max((node.min_date for node in active.values()), default=None)
        membership_changed = active.keys() != previous.keys()
        changed = membership_changed or state.report_before != report_before or state.config != self.config
        if changed:
            assignments = assign_phases(set(active), previous, now=now) if membership_changed else previous
            state = ScheduleState(uuid4(), state.revision + 1, self.config, report_before)
            work.save_schedule(state, assignments)
            for node_id in sorted(was_active - active.keys(), key=str):
                work.log_event("Node inactive", nodeId=node_id, reason="heartbeat_timeout")
            work.log_event("Schedule updated", scheduleVersion=state.schedule_version, revision=state.revision,
                           activeNodes=len(active), joined=sorted(map(str, active.keys() - previous.keys())),
                           removed=sorted(map(str, previous.keys() - active.keys())), reportBefore=report_before)
            for node in work.nodes():
                if node.active:
                    self._snapshot(work, node, now)
        work.cancel_outbox(now)
        return changed

    def expire(self, *, now: datetime) -> bool:
        with self.repository.transaction() as work:
            return self._reconcile(work, utc(now))

    def reject_invalid(self, *, publisher: str, message_id: UUID, error, now: datetime):
        """Persist a correlated error only for an identified, ready broker user."""
        now = utc(now)
        with self.repository.transaction() as work:
            source = work.publisher(publisher)
            if source is None or source.lifecycle != "ready" or work.seen(source.node_id, message_id):
                return
            rejected = Message.create(Error(error.code, str(error)), sent_at=now, message_id=message_id)
            result = self._reject(work, source, rejected, now, error.code, str(error))
            work.record(source.node_id, rejected, result.status, result.reason, now)

    def revoke(self, node_id: UUID, *, now: datetime) -> bool:
        """Local state change; provisioning adapter also blocks the broker user."""
        with self.repository.transaction() as work:
            if work.node(node_id) is None:
                raise ValueError("Unknown node")
            work.revoke(node_id)
            work.log_event("Node access revoked", nodeId=node_id)
            return self._reconcile(work, utc(now))

    def handle(self, message: Message, *, publisher: str, now: datetime) -> ProcessingResult:
        """Process a validated message from a broker-validated user_id property.

        JSON parsing and AMQP metadata checks happen in the transport adapter.
        Never pass a username supplied inside JSON as publisher.
        """
        now = utc(now)
        with self.repository.transaction() as work:
            source = work.publisher(publisher)
            if source is None or source.lifecycle != "ready":
                # No reply can be routed to an unprovisioned/revoked account.
                return ProcessingResult("rejected", "NODE_REVOKED")
            if work.seen(source.node_id, message.message_id):
                return ProcessingResult("duplicate")
            if not isinstance(message.data, (Heartbeat, SlotsFound, NodeLogs)):
                result = self._reject(work, source, message, now, "INVALID_MESSAGE", "Expected a node command")
            elif message.data.node_id != source.node_id:
                result = self._reject(work, source, message, now, "NODE_ID_MISMATCH",
                                      "Command identity does not match the authenticated publisher")
            elif isinstance(message.data, Heartbeat):
                result = self._heartbeat(work, source, message, now)
            elif isinstance(message.data, NodeLogs):
                from .logs import store
                if any(entry.occurred_at > now + timedelta(seconds=CLOCK_SKEW_SECONDS) for entry in message.data.entries):
                    result = self._reject(work, source, message, now, "INVALID_OBSERVED_AT", "Log timestamp is in the future")
                else:
                    for entry in message.data.entries:
                        store(work, source.node_id, entry, now)
                    result = ProcessingResult("processed")
            else:
                result = self._finding(work, source, message, now)
            work.record(source.node_id, message, result.status, result.reason, now)
            if result.status == "ignored":
                work.log_event("Command ignored", nodeId=source.node_id, messageId=message.message_id,
                               eventType=message.type, reason=result.reason)
            return result

    def _reject(self, work: UnitOfWork, source: NodeRecord, message: Message, now: datetime,
                code: str, description: str) -> ProcessingResult:
        response = Message.create(Error(code, description), sent_at=now, in_reply_to=message.message_id)
        work.enqueue(source.node_id, "visa.control", response)
        return ProcessingResult("rejected", code)

    def _heartbeat(self, work: UnitOfWork, source: NodeRecord, message: Message,
                   now: datetime) -> ProcessingResult:
        if message.sent_at > now + timedelta(seconds=CLOCK_SKEW_SECONDS):
            return self._reject(work, source, message, now, "INVALID_MESSAGE", "Heartbeat timestamp is in the future")
        if message.sent_at <= now - timedelta(seconds=HEARTBEAT_TTL_SECONDS):
            self._reconcile(work, now)
            return ProcessingResult("ignored", "STALE_HEARTBEAT")
        if source.last_heartbeat_at is not None and message.sent_at <= source.last_heartbeat_at:
            self._reconcile(work, now)
            return ProcessingResult("ignored", "OLDER_HEARTBEAT")
        data = message.data
        work.heartbeat(source.node_id, data.min_date, message.sent_at, min(message.sent_at, now))
        work.log_event("Heartbeat received", nodeId=source.node_id, messageId=message.message_id,
                       minDate=data.min_date, receivedScheduleVersion=data.schedule_version)
        changed = self._reconcile(work, now)
        if data.supports_logging and data.logging_version != self.logging_version:
            work.enqueue(source.node_id, "visa.control", Message.create(
                NodeConfig(source.node_id, self.logging_version, self.verbose, self.log_groups), sent_at=now))
        if not changed and data.schedule_version != work.schedule().schedule_version:
            self._snapshot(work, work.node(source.node_id), now, message.message_id)
            work.log_event("Schedule resync queued", nodeId=source.node_id,
                           scheduleVersion=work.schedule().schedule_version)
        return ProcessingResult("processed")

    def _finding(self, work: UnitOfWork, source: NodeRecord, message: Message,
                 now: datetime) -> ProcessingResult:
        data = message.data
        if (data.observed_at > now + timedelta(seconds=CLOCK_SKEW_SECONDS)
                or data.observed_at > message.sent_at + timedelta(seconds=CLOCK_SKEW_SECONDS)):
            return self._reject(work, source, message, now, "INVALID_OBSERVED_AT", "Observation timestamp is in the future")
        expires_at = data.observed_at + timedelta(seconds=FINDING_TTL_SECONDS)
        self._reconcile(work, now)
        if expires_at <= now:
            return ProcessingResult("ignored", "EXPIRED_FINDING")
        work.finding(source.node_id, message, expires_at)
        from .logs import store
        store(work, source.node_id, LogEntry(message.message_id, data.observed_at, "finding", "info", "slots_found",
                                            None, None, {"dates": [day.isoformat() for day in data.dates], "count": len(data.dates)}), now)
        recipients = []
        for recipient in work.nodes():
            if not recipient.active or recipient.node_id == source.node_id:
                continue
            dates = tuple(sorted({day for day in data.dates if day < recipient.min_date}))
            if dates:
                event = Message.create(SlotsAvailable(message.message_id, source.node_id,
                                                       data.observed_at, expires_at, dates), sent_at=now)
                work.enqueue(recipient.node_id, "visa.slots", event, expires_at)
                recipients.append(str(recipient.node_id))
        work.log_event("Slots found", nodeId=source.node_id, messageId=message.message_id,
                       dates=data.dates, observedAt=data.observed_at, expiresAt=expires_at,
                       recipients=recipients)
        return ProcessingResult("processed")
