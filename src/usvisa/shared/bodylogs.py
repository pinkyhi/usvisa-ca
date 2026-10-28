"""Lossless multipart transport for explicitly enabled reschedule body logs."""
import base64
import hashlib
import json
import re

from .logdata import safe_details


BODY_EVENTS = frozenset({"booking_http_request", "booking_http_response"})
CHUNK_SIZE = 24_000
CHUNK_FIELDS = {"body_chunk", "chunk_index", "chunk_count", "chunk_sha256"}


def chunks(details):
    raw = json.dumps(details, ensure_ascii=True).encode("ascii")
    encoded = base64.b64encode(raw).decode("ascii")
    digest = hashlib.sha256(raw).hexdigest()
    count = (len(encoded) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for index in range(count):
        yield {"body_chunk": encoded[index * CHUNK_SIZE:(index + 1) * CHUNK_SIZE],
               "chunk_index": index, "chunk_count": count, "chunk_sha256": digest}


def validate_chunk(details):
    if (set(details) != CHUNK_FIELDS
            or type(details["chunk_index"]) is not int or type(details["chunk_count"]) is not int
            or not 0 <= details["chunk_index"] < details["chunk_count"]
            or not isinstance(details["body_chunk"], str)
            or not 1 <= len(details["body_chunk"]) <= CHUNK_SIZE
            or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", details["body_chunk"])
            or not isinstance(details["chunk_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", details["chunk_sha256"])):
        raise ValueError("Invalid diagnostic body chunk")
    return dict(details)


def assemble(parts, digest):
    raw = base64.b64decode("".join(parts), validate=True)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Diagnostic body checksum mismatch")
    details = json.loads(raw)
    if not isinstance(details, dict) or not isinstance(details.get("body"), str):
        raise ValueError("Invalid diagnostic body")
    # Only the explicitly requested body bypasses ordinary redaction/length limits.
    return {**safe_details(details), "body": details["body"]}
