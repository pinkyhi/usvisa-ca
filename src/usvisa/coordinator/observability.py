"""Readable UTC business events on stdout, with explicit safe fields only."""
import json
import logging
import sys
import time


log = logging.getLogger("usvisa.coordinator.events")
_FIELDS = frozenset({
    "nodeId", "nodeName", "username", "messageId", "eventType", "status", "reason",
    "scheduleVersion", "receivedScheduleVersion", "revision", "reportBefore", "minDate",
    "activeNodes", "joined", "removed", "startAt", "intervalSeconds", "city",
    "dates", "slots", "excludedSlots", "observedAt", "expiresAt", "recipients", "findingId",
    "serverUrl",
})


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def configure_logging():
    logger = logging.getLogger("usvisa.coordinator")
    if not any(handler.name == "coordinator-console" for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.name = "coordinator-console"
        handler.setFormatter(UTCFormatter("[%(asctime)s UTC] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def event(name, **fields):
    # Never dump commands, HTTP bodies, headers, credentials or exceptions.
    details = " ".join(f"{key}={json.dumps(value, default=str, ensure_ascii=True)}"
                       for key, value in fields.items() if key in _FIELDS)
    log.info("%s%s", name, " " + details if details else "")
