"""Structured audit events, with opt-in complete reschedule bodies."""
import json
import math
import os
import uuid
import re
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
_guard_lock = threading.RLock()


def _with_guard_lock(function):
    @wraps(function)
    def run(*args, **kwargs):
        with _guard_lock:
            return function(*args, **kwargs)
    return run


def subscribe(listener):
    with _listener_lock:
        _listeners.append(listener)

    def remove():
        with _listener_lock:
            _listeners.remove(listener)
    return remove


def group_enabled(group):
    with _listener_lock:
        listeners = list(_listeners)
    return any(getattr(listener, "group_enabled", lambda group: False)(group) for listener in listeners)


def http_body(name, body, **details):
    """Capture lazily only when the coordinator enabled the diagnostic group."""
    try:
        if group_enabled("reschedule"):
            event(name, log_group="reschedule", body=body(), **details)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning("Could not record reschedule body (%s)", type(error).__name__)


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
    cause = error.__cause__ or error.__context__
    details = safe_details({"error_type": type(error).__name__, "message": str(error),
                           "reason": f"{type(cause).__name__}: {cause}" if cause else None, "stack": [
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
    # Explicit event fields may refine contextual fields (e.g. verification's
    # source='instructions' inside a source='poll' trace). dict(**a, **b) raises
    # on duplicate names; mapping unpacking intentionally lets details win.
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "run_id": RUN_ID,
              "pid": os.getpid(), "event": name, "event_id": str(uuid.uuid4()), **_context.get(), **details}
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
    return record

def claim_path(schedule_id):
    if not isinstance(schedule_id, (str, int)) or not re.fullmatch(r"\d+", str(schedule_id)):
        raise ValueError("Invalid visa application ID")
    return GUARD_DIR / f"{schedule_id}.json"


def read_claim(schedule_id):
    path = claim_path(schedule_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError("Invalid booking guard")
        deadline = data.get("verify_at", 0)
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Invalid verification deadline")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, OverflowError):
        # An unreadable guard must block booking, never silently permit a POST.
        return {"state": "unreadable"}


@_with_guard_lock
def claim(schedule_id, day, slot_time, consulate, **details):
    """Reserve one submission durably before sending it, in both node modes."""
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    path = claim_path(schedule_id)
    with path.open("x", encoding="utf-8") as output:
        json.dump(dict(run_id=RUN_ID, timestamp=datetime.now(timezone.utc).isoformat(),
                       date=str(day), time=slot_time, consulate=consulate,
                       state="pending", **details), output)
        output.flush()
        os.fsync(output.fileno())
    _sync_guard_directory()
    return path


def _sync_guard_directory(directory=None):
    if os.name != "nt":
        descriptor = os.open(directory or GUARD_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@_with_guard_lock
def update_claim(schedule_id, *, expected_claim=None, **details):
    """Atomically replace an existing state; an interrupted write keeps the old one."""
    path = claim_path(schedule_id)
    data = read_claim(schedule_id)
    if data is None or data.get("state") == "unreadable":
        raise OSError("Booking state is missing or unreadable")
    if expected_claim is not None and not same_claim(data, expected_claim):
        raise OSError("Booking state changed while the request was in flight")
    data.update(details)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_guard_directory()
    finally:
        temporary.unlink(missing_ok=True)
    return data


def same_claim(current, expected):
    return bool(current and expected and all(current.get(key) == expected.get(key)
                for key in ("attempt_id", "timestamp", "date", "time", "consulate")))
