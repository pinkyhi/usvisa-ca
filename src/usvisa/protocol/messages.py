"""Strict, transport-independent version 1 of the Node/Coordinator protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import ClassVar
from uuid import UUID, uuid4

from usvisa.shared.logdata import safe_details
from usvisa.shared.bodylogs import BODY_EVENTS, validate_chunk


INTERVAL_SECONDS = 180
HEARTBEAT_SECONDS = 30
PRESENCE_SECONDS = 540
HEARTBEAT_TTL_SECONDS = 60
FINDING_TTL_SECONDS = 180
CLOCK_SKEW_SECONDS = 5
MAX_MESSAGE_BYTES = 65_536
MAX_SLOTS_PER_MESSAGE = 512
ERROR_CODES = frozenset({
    "INVALID_MESSAGE", "UNSUPPORTED_VERSION", "NODE_ID_MISMATCH", "NODE_REVOKED",
    "INVALID_MIN_DATE", "INVALID_SCHEDULE_VERSION", "INVALID_OBSERVED_AT",
})


class ProtocolError(ValueError):
    """A permanent validation error with a safe, payload-free description."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(timezone.utc)


def timestamp(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GroupConfig:
    consulate_id: int
    city: str

    def __post_init__(self):
        if type(self.consulate_id) is not int or self.consulate_id <= 0:
            raise ValueError("consulateId must be a positive integer")
        if (not isinstance(self.city, str) or not self.city.strip() or len(self.city) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in self.city)):
            raise ValueError("city must be a nonempty string of at most 200 characters")

    def to_dict(self) -> dict:
        return {"consulateId": self.consulate_id, "city": self.city}


@dataclass(frozen=True)
class Heartbeat:
    TYPE: ClassVar[str] = "node.heartbeat"
    node_id: UUID
    min_date: date | None
    schedule_version: UUID | None
    logging_version: UUID | None = None
    supports_logging: bool = False
    slots_version: int = 0
    date_exclusions: bool = False
    passive: bool = False


@dataclass(frozen=True)
class NodeConfig:
    TYPE: ClassVar[str] = "node.config"
    node_id: UUID
    config_version: UUID
    verbose: bool
    log_groups: tuple[str, ...]


@dataclass(frozen=True)
class LogEntry:
    event_id: UUID
    occurred_at: datetime
    category: str
    level: str
    event: str
    trace_id: UUID | None
    log_group: str | None
    details: dict


@dataclass(frozen=True)
class NodeLogs:
    TYPE: ClassVar[str] = "node.logs"
    node_id: UUID
    entries: tuple[LogEntry, ...]


@dataclass(frozen=True)
class ScheduleUpdated:
    TYPE: ClassVar[str] = "schedule.updated"
    node_id: UUID
    schedule_version: UUID
    revision: int
    start_at: datetime
    interval_seconds: int
    report_before: date | None
    config: GroupConfig


@dataclass(frozen=True, order=True)
class AppointmentSlot:
    date: date
    time: str

    def to_dict(self):
        return {"date": self.date.isoformat(), "time": self.time}


@dataclass(frozen=True)
class SlotsFound:
    TYPE: ClassVar[str] = "slots.found"
    node_id: UUID
    observed_at: datetime
    dates: tuple[date, ...] = ()
    slots: tuple[AppointmentSlot, ...] = ()
    excluded_slots: tuple[AppointmentSlot, ...] = ()

    def __post_init__(self):
        if self.slots:
            object.__setattr__(self, "dates", tuple(sorted({slot.date for slot in self.slots})))


@dataclass(frozen=True)
class SlotsAvailable:
    TYPE: ClassVar[str] = "slots.available"
    finding_id: UUID
    source_node_id: UUID
    observed_at: datetime
    expires_at: datetime
    dates: tuple[date, ...] = ()
    slots: tuple[AppointmentSlot, ...] = ()
    excluded_slots: tuple[AppointmentSlot, ...] = ()

    def __post_init__(self):
        if self.slots:
            object.__setattr__(self, "dates", tuple(sorted({slot.date for slot in self.slots})))


@dataclass(frozen=True)
class Error:
    TYPE: ClassVar[str] = "error"
    code: str
    message: str


Payload = Heartbeat | NodeConfig | NodeLogs | ScheduleUpdated | SlotsFound | SlotsAvailable | Error


def _payload_dict(data: Payload) -> dict:
    if isinstance(data, Heartbeat):
        result = {"nodeId": str(data.node_id),
                  "scheduleVersion": str(data.schedule_version) if data.schedule_version else None}
        if data.passive:
            result["passive"] = True
        else:
            result["minDate"] = data.min_date.isoformat() if data.min_date else None
        if data.supports_logging:
            result["loggingVersion"] = str(data.logging_version) if data.logging_version else None
        if data.slots_version:
            result["slotsVersion"] = data.slots_version
        if data.date_exclusions:
            result["dateExclusions"] = True
        return result
    if isinstance(data, NodeConfig):
        return {"nodeId": str(data.node_id), "configVersion": str(data.config_version),
                "verbose": data.verbose, "logGroups": list(data.log_groups)}
    if isinstance(data, NodeLogs):
        return {"nodeId": str(data.node_id), "entries": [
            {"eventId": str(entry.event_id), "occurredAt": timestamp(entry.occurred_at),
             "category": entry.category, "level": entry.level, "event": entry.event,
             "traceId": str(entry.trace_id) if entry.trace_id else None,
             "logGroup": entry.log_group, "details": entry.details} for entry in data.entries]}
    if isinstance(data, ScheduleUpdated):
        return {"nodeId": str(data.node_id), "scheduleVersion": str(data.schedule_version),
                "revision": data.revision, "startAt": timestamp(data.start_at),
                "intervalSeconds": data.interval_seconds, "reportBefore": data.report_before.isoformat() if data.report_before else None,
                "config": data.config.to_dict()}
    if isinstance(data, SlotsFound):
        return {"nodeId": str(data.node_id), "observedAt": timestamp(data.observed_at),
                **({"slots": [slot.to_dict() for slot in data.slots]} if data.slots else
                   {"dates": [day.isoformat() for day in data.dates]}),
                **({"excludedSlots": [slot.to_dict() for slot in data.excluded_slots]} if data.excluded_slots else {})}
    if isinstance(data, SlotsAvailable):
        return {"findingId": str(data.finding_id), "sourceNodeId": str(data.source_node_id),
                "observedAt": timestamp(data.observed_at), "expiresAt": timestamp(data.expires_at),
                **({"slots": [slot.to_dict() for slot in data.slots]} if data.slots else
                   {"dates": [day.isoformat() for day in data.dates]}),
                **({"excludedSlots": [slot.to_dict() for slot in data.excluded_slots]} if data.excluded_slots else {})}
    if isinstance(data, Error):
        return {"code": data.code, "message": data.message}
    raise TypeError("Unsupported payload type")


@dataclass(frozen=True)
class Message:
    message_id: UUID
    sent_at: datetime
    data: Payload
    in_reply_to: UUID | None = None

    @property
    def type(self) -> str:
        return self.data.TYPE

    def to_dict(self) -> dict:
        result = {"version": 1, "type": self.type, "messageId": str(self.message_id),
                  "sentAt": timestamp(self.sent_at), "data": _payload_dict(self.data)}
        if self.in_reply_to is not None:
            result["inReplyTo"] = str(self.in_reply_to)
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @classmethod
    def create(cls, data: Payload, *, sent_at: datetime, message_id: UUID | None = None,
               in_reply_to: UUID | None = None) -> Message:
        # Apply the same constraints to locally created and received messages.
        return parse_message(cls(message_id or uuid4(), sent_at, data, in_reply_to).to_json())


def _invalid(message: str, code: str = "INVALID_MESSAGE"):
    raise ProtocolError(code, message)


def _fields(value, required: set[str], optional: set[str] = frozenset()) -> dict:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        _invalid("Message fields do not match the contract")
    return value


def _uuid(value, code="INVALID_MESSAGE") -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (ValueError, AttributeError):
        _invalid("Expected a canonical UUID string", code)


def _day(value, code="INVALID_MESSAGE") -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        _invalid("Expected a calendar date in YYYY-MM-DD format", code)


def _moment(value, code="INVALID_MESSAGE") -> datetime:
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
        ):
            raise ValueError
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid("Expected a UTC timestamp ending in Z", code)


def _days(value) -> tuple[date, ...]:
    if not isinstance(value, list) or not value:
        _invalid("dates must be a nonempty array")
    return tuple(sorted({_day(day) for day in value}))


def _slots(value) -> tuple[AppointmentSlot, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SLOTS_PER_MESSAGE:
        _invalid("slots must contain 1 to 512 date/time pairs")
    result = []
    for item in value:
        item = _fields(item, {"date", "time"})
        slot_time = item["time"]
        if (not isinstance(slot_time, str) or not re.fullmatch(r"\d{2}:\d{2}", slot_time)
                or int(slot_time[:2]) > 23 or int(slot_time[3:]) > 59):
            _invalid("Expected local consulate time in HH:MM format")
        result.append(AppointmentSlot(_day(item["date"]), slot_time))
    return tuple(sorted(set(result)))


def _finding_fields(data, required):
    data = _fields(data, required, {"dates", "slots", "excludedSlots"})
    if ("dates" in data) == ("slots" in data):
        _invalid("Expected exactly one of dates or slots")
    slots = _slots(data["slots"]) if "slots" in data else ()
    dates = tuple(sorted({slot.date for slot in slots})) if slots else _days(data["dates"])
    excluded = _slots(data["excludedSlots"]) if "excludedSlots" in data else ()
    if excluded and (slots or any(slot.date not in dates for slot in excluded)):
        _invalid("Exclusions must refer to dates in a date finding")
    return data, dates, slots, excluded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid("Duplicate JSON fields are not allowed")
        result[key] = value
    return result


def _reject_constant(_value):
    _invalid("Non-finite JSON numbers are not allowed")


def parse_message(body: str | bytes) -> Message:
    """Decode a complete envelope; never include untrusted values in errors."""
    if not isinstance(body, (str, bytes)):
        _invalid("Expected a UTF-8 JSON message")
    try:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        if len(raw) > MAX_MESSAGE_BYTES:
            _invalid("Message exceeds 64 KiB")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _invalid("Expected a UTF-8 JSON message")
    value = _fields(value, {"version", "type", "messageId", "sentAt", "data"}, {"inReplyTo"})
    if type(value["version"]) is not int:
        _invalid("version must be an integer")
    if value["version"] != 1:
        _invalid("Unsupported protocol version", "UNSUPPORTED_VERSION")
    message_id = _uuid(value["messageId"])
    sent_at = _moment(value["sentAt"])
    reply = _uuid(value["inReplyTo"]) if "inReplyTo" in value else None
    data = value["data"]
    kind = value["type"]
    if kind == Heartbeat.TYPE:
        data = _fields(data, {"nodeId", "scheduleVersion"}, {"minDate", "passive", "loggingVersion", "slotsVersion", "dateExclusions"})
        passive = data.get("passive", False)
        if type(passive) is not bool or (passive and "minDate" in data):
            _invalid("Passive heartbeat must omit minDate")
        minimum = None if passive else _day(data.get("minDate"), "INVALID_MIN_DATE")
        if "slotsVersion" in data and (type(data["slotsVersion"]) is not int or data["slotsVersion"] != 1):
            _invalid("Unsupported prepared slots capability")
        if "dateExclusions" in data and type(data["dateExclusions"]) is not bool:
            _invalid("dateExclusions must be a boolean")
        payload = Heartbeat(_uuid(data["nodeId"]), minimum,
                            _uuid(data["scheduleVersion"], "INVALID_SCHEDULE_VERSION")
                            if data["scheduleVersion"] is not None else None,
                            _uuid(data["loggingVersion"]) if data.get("loggingVersion") is not None else None,
                            "loggingVersion" in data, data.get("slotsVersion", 0), data.get("dateExclusions", False), passive)
    elif kind == NodeConfig.TYPE:
        data = _fields(data, {"nodeId", "configVersion", "verbose", "logGroups"})
        if type(data["verbose"]) is not bool or type(data["logGroups"]) is not list:
            _invalid("Invalid logging configuration")
        if any(group != "reschedule" for group in data["logGroups"]) or len(data["logGroups"]) > 1:
            _invalid("Unsupported log group")
        payload = NodeConfig(_uuid(data["nodeId"]), _uuid(data["configVersion"]), data["verbose"], tuple(data["logGroups"]))
    elif kind == NodeLogs.TYPE:
        data = _fields(data, {"nodeId", "entries"})
        if type(data["entries"]) is not list or not 1 <= len(data["entries"]) <= 25:
            _invalid("Expected 1 to 25 log entries")
        entries = []
        for item in data["entries"]:
            item = _fields(item, {"eventId", "occurredAt", "category", "level", "event", "traceId", "logGroup", "details"})
            if (item["category"] not in ("poll", "reschedule", "error")
                    or item["level"] not in ("info", "warning", "error")
                    or not isinstance(item["event"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", item["event"])
                    or item["logGroup"] not in (None, "reschedule") or type(item["details"]) is not dict):
                _invalid("Invalid log entry")
            if item["event"] in BODY_EVENTS:
                if item["category"] != "reschedule" or item["logGroup"] != "reschedule":
                    _invalid("Invalid diagnostic body category")
                try:
                    details = validate_chunk(item["details"])
                except ValueError:
                    _invalid("Invalid diagnostic body chunk")
            else:
                details = safe_details(item["details"])
            entries.append(LogEntry(_uuid(item["eventId"]), _moment(item["occurredAt"]), item["category"],
                                    item["level"], item["event"], _uuid(item["traceId"]) if item["traceId"] is not None else None,
                                    item["logGroup"], details))
        payload = NodeLogs(_uuid(data["nodeId"]), tuple(entries))
    elif kind == ScheduleUpdated.TYPE:
        data = _fields(data, {"nodeId", "scheduleVersion", "revision", "startAt",
                              "intervalSeconds", "reportBefore", "config"})
        if type(data["revision"]) is not int or data["revision"] <= 0:
            _invalid("revision must be a positive integer")
        if type(data["intervalSeconds"]) is not int or data["intervalSeconds"] != INTERVAL_SECONDS:
            _invalid("intervalSeconds must be 180")
        config = _fields(data["config"], {"consulateId", "city"})
        try:
            group = GroupConfig(config["consulateId"], config["city"])
        except ValueError:
            _invalid("Invalid group configuration")
        payload = ScheduleUpdated(_uuid(data["nodeId"]),
                                  _uuid(data["scheduleVersion"], "INVALID_SCHEDULE_VERSION"),
                                  data["revision"], _moment(data["startAt"]),
                                  data["intervalSeconds"], _day(data["reportBefore"]) if data["reportBefore"] is not None else None, group)
    elif kind == SlotsFound.TYPE:
        data, dates, slots, excluded = _finding_fields(data, {"nodeId", "observedAt"})
        payload = SlotsFound(_uuid(data["nodeId"]),
                             _moment(data["observedAt"], "INVALID_OBSERVED_AT"), dates, slots, excluded)
    elif kind == SlotsAvailable.TYPE:
        data, dates, slots, excluded = _finding_fields(data, {"findingId", "sourceNodeId", "observedAt", "expiresAt"})
        observed = _moment(data["observedAt"], "INVALID_OBSERVED_AT")
        expires = _moment(data["expiresAt"], "INVALID_OBSERVED_AT")
        try:
            expected_expiry = observed + timedelta(seconds=FINDING_TTL_SECONDS)
        except OverflowError:
            _invalid("Invalid finding expiry", "INVALID_OBSERVED_AT")
        if expires != expected_expiry:
            _invalid("Finding expiry must be observedAt plus 180 seconds", "INVALID_OBSERVED_AT")
        payload = SlotsAvailable(_uuid(data["findingId"]), _uuid(data["sourceNodeId"]),
                                 observed, expires, dates, slots, excluded)
    elif kind == Error.TYPE:
        data = _fields(data, {"code", "message"})
        if not isinstance(data["code"], str) or data["code"] not in ERROR_CODES:
            _invalid("Unknown error code")
        if not isinstance(data["message"], str) or not data["message"] or len(data["message"]) > 500:
            _invalid("Invalid error description")
        payload = Error(data["code"], data["message"])
    else:
        _invalid("Unknown message type")
    return Message(message_id, sent_at, payload, reply)
