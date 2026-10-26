"""Structured audit events without cookies, credentials, tokens or response bodies."""
import json
import os
import uuid
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone
from usvisa.shared.paths import data_directory, load_environment

load_environment()

RUN_ID = uuid.uuid4().hex
LOG_PATH = data_directory() / "logs" / "rescheduler.jsonl"
GUARD_DIR = data_directory() / ".booking-state"
_context = ContextVar("audit_context", default={})
_listeners = []
_listener_lock = threading.RLock()


def subscribe(listener):
    with _listener_lock:
        _listeners.append(listener)

    def remove():
        with _listener_lock:
            _listeners.remove(listener)
    return remove


@contextmanager
def context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def exception(name, error):
    if getattr(error, "_audit_logged", False):
        return
    from usvisa.shared.logdata import safe_details
    secrets = [value for key, value in os.environ.items()
               if any(part in key.upper() for part in ("PASSWORD", "TOKEN", "SECRET", "APPLICATION_PWD"))]
    details = safe_details({"error_type": type(error).__name__, "message": str(error), "stack": [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)[-20:]]}, secrets)
    event(name, level="error", **details)
    error._audit_logged = True


def traced(source, *, log_group=None, expected=()):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            previous = _context.get()
            fields = {"trace_id": previous.get("trace_id") or str(uuid.uuid4()),
                      "source": previous.get("source") or source}
            if log_group:
                fields["log_group"] = log_group
            with context(**fields):
                try:
                    return function(*args, **kwargs)
                except expected:
                    raise
                except Exception as error:
                    exception("operation_exception", error)
                    raise
        return run
    return decorate

def event(name, **details):
    record = dict(timestamp=datetime.now(timezone.utc).isoformat(), run_id=RUN_ID,
                  pid=os.getpid(), event=name, event_id=str(uuid.uuid4()), **_context.get(), **details)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, default=str) + "\n")
        output.flush()
        os.fsync(output.fileno())
    with _listener_lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(record)
        except Exception as error:
            # Remote diagnostics must never interrupt an availability check/POST.
            import logging
            logging.getLogger(__name__).warning("Diagnostic event remains in local audit (%s)", type(error).__name__)

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
