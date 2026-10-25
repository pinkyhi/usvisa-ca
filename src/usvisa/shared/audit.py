"""Structured audit events without cookies, credentials, tokens or response bodies."""
import json
import os
import uuid
from datetime import datetime, timezone
from usvisa.shared.paths import data_directory, load_environment

load_environment()

RUN_ID = uuid.uuid4().hex
LOG_PATH = data_directory() / "logs" / "rescheduler.jsonl"
GUARD_DIR = data_directory() / ".booking-state"

def event(name, **details):
    record = dict(timestamp=datetime.now(timezone.utc).isoformat(), run_id=RUN_ID,
                  pid=os.getpid(), event=name, **details)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, default=str) + "\n")
        output.flush()
        os.fsync(output.fileno())

def claim(schedule_id, day, slot_time, consulate):
    """Exclusive durable claim, retained even when submission outcome is unknown."""
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    path = GUARD_DIR / f"{schedule_id}.json"
    with path.open("x", encoding="utf-8") as output:
        json.dump(dict(run_id=RUN_ID, timestamp=datetime.now(timezone.utc).isoformat(),
                       date=str(day), time=slot_time, consulate=consulate,
                       state="submission_reserved"), output)
        output.flush()
        os.fsync(output.fileno())
    return path
