"""Wire-contract checks shared by both applications; no broker required."""

import copy
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from usvisa.protocol.messages import (
    Error, GroupConfig, Heartbeat, Message, ProtocolError, ScheduleUpdated,
    SlotsAvailable, SlotsFound, parse_message,
)


NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
NODE = UUID("00000000-0000-4000-8000-000000000001")
VERSION = UUID("00000000-0000-4000-8000-000000000002")
DAY = date(2027, 12, 1)


class ProtocolTests(unittest.TestCase):
    def heartbeat(self):
        return Message.create(Heartbeat(NODE, DAY, None), sent_at=NOW)

    def reject(self, value, code="INVALID_MESSAGE"):
        with self.assertRaises(ProtocolError) as raised:
            parse_message(json.dumps(value))
        self.assertEqual(raised.exception.code, code)

    def test_five_message_types_round_trip_without_losing_identifiers_or_times(self):
        payloads = [
            Heartbeat(NODE, DAY, VERSION),
            ScheduleUpdated(NODE, VERSION, 9, NOW, 180, DAY, GroupConfig(95, "Vancouver")),
            SlotsFound(NODE, NOW, (DAY,)),
            SlotsAvailable(uuid4(), NODE, NOW, NOW + timedelta(seconds=180), (DAY,)),
            Error("NODE_ID_MISMATCH", "Command identity does not match publisher"),
        ]
        for payload in payloads:
            with self.subTest(type=payload.TYPE):
                original = Message.create(payload, sent_at=NOW, in_reply_to=uuid4())
                self.assertEqual(parse_message(original.to_json().encode("utf-8")), original)
                self.assertEqual(original.to_dict()["version"], 1)
                self.assertTrue(original.to_dict()["sentAt"].endswith("Z"))

    def test_unknown_or_missing_fields_are_rejected_at_each_level(self):
        base = self.heartbeat().to_dict()
        variants = []
        for field in base:
            changed = copy.deepcopy(base)
            del changed[field]
            variants.append(changed)
        for target in (None, "data"):
            changed = copy.deepcopy(base)
            (changed if target is None else changed[target])["secret"] = "do-not-echo"
            variants.append(changed)
        for value in variants:
            with self.subTest(value=value):
                self.reject(value)

    def test_version_and_integer_fields_do_not_accept_booleans(self):
        for value, code in [(True, "INVALID_MESSAGE"), ("1", "INVALID_MESSAGE"),
                            (2, "UNSUPPORTED_VERSION")]:
            body = self.heartbeat().to_dict()
            body["version"] = value
            self.reject(body, code)
        snapshot = Message.create(ScheduleUpdated(NODE, VERSION, 1, NOW, 180, DAY,
                                                  GroupConfig(95, "Vancouver")), sent_at=NOW).to_dict()
        for field, invalid in [("revision", True), ("revision", 0), ("intervalSeconds", 90)]:
            body = copy.deepcopy(snapshot)
            body["data"][field] = invalid
            self.reject(body)
        snapshot["data"]["config"]["consulateId"] = True
        self.reject(snapshot)

    def test_invalid_dates_and_versions_use_specific_error_codes(self):
        for field, value, code in [
            ("minDate", "2027-02-30", "INVALID_MIN_DATE"),
            ("minDate", "2027-1-01", "INVALID_MIN_DATE"),
            ("scheduleVersion", "not-a-uuid", "INVALID_SCHEDULE_VERSION"),
            ("nodeId", "not-a-uuid", "INVALID_MESSAGE"),
        ]:
            body = self.heartbeat().to_dict()
            body["data"][field] = value
            with self.subTest(field=field, value=value):
                self.reject(body, code)

    def test_timestamp_requires_full_utc_format_and_not_a_local_datetime(self):
        for value in ["2026-10-25", "2026-10-25T15:00:00", "2026-10-25T15:00:00+00:00",
                      "2026-10-25T25:00:00Z", "2026-10-25T15:00:00.1234567Z"]:
            body = self.heartbeat().to_dict()
            body["sentAt"] = value
            self.reject(body)
        with self.assertRaises(ValueError):
            Message.create(Heartbeat(NODE, DAY, None), sent_at=NOW.replace(tzinfo=None))

    def test_findings_normalize_duplicates_and_reject_empty_or_invalid_dates(self):
        message = Message.create(SlotsFound(NODE, NOW, (DAY, date(2027, 11, 1), DAY)), sent_at=NOW)
        self.assertEqual(message.data.dates, (date(2027, 11, 1), DAY))
        for dates in [[], [None], ["2027-02-30"], "2027-12-01"]:
            body = message.to_dict()
            body["data"]["dates"] = dates
            self.reject(body)
        body = message.to_dict()
        body["data"]["observedAt"] = "yesterday"
        self.reject(body, "INVALID_OBSERVED_AT")

    def test_forwarded_findings_cannot_extend_original_expiry(self):
        body = Message.create(SlotsAvailable(uuid4(), NODE, NOW, NOW + timedelta(seconds=180),
                                            (DAY,)), sent_at=NOW).to_dict()
        body["data"]["expiresAt"] = "2026-10-25T15:06:00Z"
        self.reject(body, "INVALID_OBSERVED_AT")

    def test_malformed_json_duplicate_keys_nonfinite_values_and_large_bodies_are_safe(self):
        for body in [b"\xff", "{", "[]", '{"version":1,"version":2}',
                     '{"version":NaN}', "[" * 2000, "9" * 5000, " " * 65_537]:
            with self.subTest(body=repr(body[:30])):
                with self.assertRaises(ProtocolError) as raised:
                    parse_message(body)
                self.assertNotIn("do-not-echo", str(raised.exception))

    def test_errors_do_not_echo_invalid_input(self):
        body = self.heartbeat().to_dict()
        body["data"]["minDate"] = "do-not-echo"
        with self.assertRaises(ProtocolError) as raised:
            parse_message(json.dumps(body))
        self.assertNotIn("do-not-echo", str(raised.exception))
