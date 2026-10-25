"""Strict, transport-independent version 1 of the Node/Coordinator protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import ClassVar
from uuid import UUID, uuid4


INTERVAL_SECONDS = 180
HEARTBEAT_SECONDS = 30
PRESENCE_SECONDS = 540
HEARTBEAT_TTL_SECONDS = 60
FINDING_TTL_SECONDS = 180
CLOCK_SKEW_SECONDS = 5
MAX_MESSAGE_BYTES = 65_536
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
    min_date: date
    schedule_version: UUID | None


@dataclass(frozen=True)
class ScheduleUpdated:
    TYPE: ClassVar[str] = "schedule.updated"
    node_id: UUID
    schedule_version: UUID
    revision: int
    start_at: datetime
    interval_seconds: int
    report_before: date
    config: GroupConfig


@dataclass(frozen=True)
class SlotsFound:
    TYPE: ClassVar[str] = "slots.found"
    node_id: UUID
    observed_at: datetime
    dates: tuple[date, ...]


@dataclass(frozen=True)
class SlotsAvailable:
    TYPE: ClassVar[str] = "slots.available"
    finding_id: UUID
    source_node_id: UUID
    observed_at: datetime
    expires_at: datetime
    dates: tuple[date, ...]


@dataclass(frozen=True)
class Error:
    TYPE: ClassVar[str] = "error"
    code: str
    message: str


Payload = Heartbeat | ScheduleUpdated | SlotsFound | SlotsAvailable | Error


def _payload_dict(data: Payload) -> dict:
    if isinstance(data, Heartbeat):
        return {"nodeId": str(data.node_id), "minDate": data.min_date.isoformat(),
                "scheduleVersion": str(data.schedule_version) if data.schedule_version else None}
    if isinstance(data, ScheduleUpdated):
        return {"nodeId": str(data.node_id), "scheduleVersion": str(data.schedule_version),
                "revision": data.revision, "startAt": timestamp(data.start_at),
                "intervalSeconds": data.interval_seconds, "reportBefore": data.report_before.isoformat(),
                "config": data.config.to_dict()}
    if isinstance(data, SlotsFound):
        return {"nodeId": str(data.node_id), "observedAt": timestamp(data.observed_at),
                "dates": [day.isoformat() for day in data.dates]}
    if isinstance(data, SlotsAvailable):
        return {"findingId": str(data.finding_id), "sourceNodeId": str(data.source_node_id),
                "observedAt": timestamp(data.observed_at), "expiresAt": timestamp(data.expires_at),
                "dates": [day.isoformat() for day in data.dates]}
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
        data = _fields(data, {"nodeId", "minDate", "scheduleVersion"})
        payload = Heartbeat(_uuid(data["nodeId"]), _day(data["minDate"], "INVALID_MIN_DATE"),
                            _uuid(data["scheduleVersion"], "INVALID_SCHEDULE_VERSION")
                            if data["scheduleVersion"] is not None else None)
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
                                  data["intervalSeconds"], _day(data["reportBefore"]), group)
    elif kind == SlotsFound.TYPE:
        data = _fields(data, {"nodeId", "observedAt", "dates"})
        payload = SlotsFound(_uuid(data["nodeId"]),
                             _moment(data["observedAt"], "INVALID_OBSERVED_AT"), _days(data["dates"]))
    elif kind == SlotsAvailable.TYPE:
        data = _fields(data, {"findingId", "sourceNodeId", "observedAt", "expiresAt", "dates"})
        observed = _moment(data["observedAt"], "INVALID_OBSERVED_AT")
        expires = _moment(data["expiresAt"], "INVALID_OBSERVED_AT")
        try:
            expected_expiry = observed + timedelta(seconds=FINDING_TTL_SECONDS)
        except OverflowError:
            _invalid("Invalid finding expiry", "INVALID_OBSERVED_AT")
        if expires != expected_expiry:
            _invalid("Finding expiry must be observedAt plus 180 seconds", "INVALID_OBSERVED_AT")
        payload = SlotsAvailable(_uuid(data["findingId"]), _uuid(data["sourceNodeId"]),
                                 observed, expires, _days(data["dates"]))
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
