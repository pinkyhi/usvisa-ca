"""Bounded diagnostic data: no request bodies, credentials or browser cookies."""
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit


DETAIL_FIELDS = frozenset({
    "dates", "times", "slots", "excluded_slots", "date", "time", "count", "earliest", "latest", "available", "candidates",
    "exclusions", "status", "method", "endpoint", "elapsed_ms", "duration_ms", "source",
    "finding_id", "booking_post_sent", "verified", "outcome", "reason", "prepared_form_cached",
    "applicant_count", "test_mode", "dry_run", "consulate", "matches", "booking_disabled",
    "error_type", "message", "stack", "truncated",
    "response_messages", "response_location", "response_type", "response_bytes",
})


def safe_text(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"\b[0-9a-f-]{36}\.[A-Za-z0-9_-]{43,128}\b", "[NODE_TOKEN]", text)
    text = re.sub(r"(?i)\b(password|passcode|token|secret|csrf|cookie|authorization)\s*[=:]\s*[^\s,;]+",
                  r"\1=[REDACTED]", text)

    def url(match):
        try:
            parsed = urlsplit(match.group(0))
            return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
        except ValueError:
            return "[URL]"

    text = re.sub(r"https?://[^\s\"'<>]+", url, text)
    text = re.sub(r"/schedule/\d+", "/schedule/<application>", text)
    return text[:2000]


def safe_details(details, secrets=()):
    def value(item, depth=0):
        if depth > 4:
            return "[TRUNCATED]"
        if item is None or type(item) in (bool, int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, (list, tuple)):
            return [value(part, depth + 1) for part in item[:366]]
        if isinstance(item, dict):
            # Nested objects contain stack frames or prepared date/time pairs.
            return {key: value(part, depth + 1) for key, part in item.items()
                    if key in {"file", "function", "line", "date", "time"}}
        return safe_text(item, secrets)

    arrays = {"dates", "times", "slots", "excluded_slots", "available", "candidates", "exclusions", "stack", "response_messages"}
    result = {key: value(item) for key, item in details.items() if key in DETAIL_FIELDS
              and (key not in arrays or isinstance(item, (list, tuple)))}
    if "count" in result and (type(result["count"]) is not int or result["count"] < 0):
        del result["count"]
    if len(json.dumps(result, ensure_ascii=True).encode()) > 16_000:
        result = {key: part for key, part in result.items() if key not in {"dates", "times", "slots", "stack", "available"}}
        result["truncated"] = True
    return result
