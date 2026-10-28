"""Local inspection/reset of booking locks. Never logs in or contacts AIS."""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from usvisa.shared import audit
from usvisa.shared.locking import InstanceLock
from usvisa.shared.paths import data_directory
from usvisa.transport.webstomp import parse_token
from . import settings as cfg


def migrate_legacy(state, application_id, consulate):
    """Move old SQLite flags into the single file before removing those flags."""
    verified, pending = state.get("verified_booking"), state.get("booking_block")
    if not verified and not pending:
        guard = audit.read_claim(application_id)
        if guard and guard.get("state") not in {"pending", "confirmed", "unreadable"}:
            guard = audit.update_claim(application_id, state="pending")
        return guard
    saved_id = state.get("booking_application_id")
    if saved_id and str(saved_id) != str(application_id):
        raise ValueError("Saved booking state belongs to another visa application")
    guard = audit.read_claim(application_id)
    legacy = verified or pending
    if guard is None:
        audit.claim(application_id, legacy.get("date"), legacy.get("time"), consulate,
                    migrated_from="sqlite", exclude_date=legacy.get("exclude_date", True))
        guard = audit.read_claim(application_id)
    if (verified and guard.get("state") != "unreadable"
            and guard.get("date") == verified.get("date") and guard.get("time") == verified.get("time")):
        guard = audit.update_claim(application_id, state="confirmed")
    with state.transaction() as db:
        db.execute("DELETE FROM meta WHERE key IN ('verified_booking','booking_block')")
    return guard


def manage(*, reset=False, application_id=None):
    root = data_directory() / ".node-state"
    node_id = None
    if os.getenv("NODE_TOKEN"):
        node_id = parse_token(os.environ["NODE_TOKEN"])[0]
    elif (root / "registration.json").is_file():
        registration = json.loads((root / "registration.json").read_text(encoding="utf-8"))
        username = os.getenv("PORTAL_USERNAME", "").strip().lower()
        if username and registration.get("username") != username:
            raise ValueError("Saved registration belongs to another portal login")
        node_id = UUID(registration["nodeId"])
    directory = root / str(node_id) if node_id else None
    locks = []
    connection = None
    try:
        if reset:
            locks.append(InstanceLock(audit.GUARD_DIR / "solo.lock"))
            if directory:
                locks.append(InstanceLock(directory / "instance.lock"))
        meta = {}
        if directory and (directory / "state.sqlite3").is_file():
            connection = sqlite3.connect(directory / "state.sqlite3", timeout=10)
            meta = {key: json.loads(value) for key, value in connection.execute(
                "SELECT key,value FROM meta WHERE key IN ('verified_booking','booking_block','booking_application_id')")}
        identities = set()
        if meta.get("booking_application_id"):
            identities.add(str(meta["booking_application_id"]))
        # The cached session is selected by this visa account, not by portal login.
        account_key = hashlib.sha256(cfg.USER_EMAIL.strip().casefold().encode("utf-8")).hexdigest()
        session_path = Path(cfg.SESSION_CACHE_DIR) / (account_key + ".json")
        if session_path.is_file():
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            match = re.fullmatch(r"/en-ca/niv/schedule/(\d+)/appointment", urlsplit(payload["appointment_url"]).path)
            if match:
                identities.add(match[1])
        if application_id:
            identities.add(str(application_id))
        if len(identities) > 1:
            raise ValueError("Visa application IDs differ between this account and saved node; no block was reset")
        identity = next(iter(identities), None)
        guard = audit.read_claim(identity) if identity else None
        if not reset:
            return {"nodeId": str(node_id) if node_id else None, "applicationId": identity,
                    "bookingDisabled": bool(meta.get("verified_booking") or meta.get("booking_block") or guard),
                    "bookingState": guard, "legacyStateNeedsMigration": bool(meta.get("verified_booking") or meta.get("booking_block")),
                    "stateFile": str(audit.claim_path(identity)) if identity else None}
        if identity is None:
            raise ValueError("Cannot identify the visa application; provide --application-id from its booking guard filename")
        source = audit.claim_path(identity).resolve()
        archive = source.parent / "cleared" / (identity + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8] + ".json")
        assert archive.resolve().is_relative_to(audit.GUARD_DIR.resolve())
        if source.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
        # Clear metadata first. If a crash/rename failure follows, the original
        # guard still blocks booking and is restored on the next start.
        if connection:
            connection.execute("DELETE FROM meta WHERE key IN ('verified_booking','booking_block')")
            connection.commit()
        if source.exists():
            source.rename(archive)
        audit.event("booking_block_reset", reason="explicit_local_reset")
        return {"bookingDisabled": False, "applicationId": identity,
                "message": "Booking block cleared. The next start may book again."}
    finally:
        if connection:
            connection.close()
        for lock in reversed(locks):
            lock.close()
