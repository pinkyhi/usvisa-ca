"""Portal users and opaque sessions. No public registration or recoverable tokens."""
import hashlib
import re
import secrets
import time
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError


hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_username(value):
    if not isinstance(value, str):
        raise ValueError("Enter a username without spaces (1 to 254 characters)")
    username = value.strip().lower()
    if not username or len(username) > 254 or re.search(r"[\s\x00-\x1f\x7f]", username):
        raise ValueError("Enter a username without spaces (1 to 254 characters)")
    return username


def create_user(repository, username, password):
    username = normalize_username(username)
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError("Password must contain 8 to 1024 characters")
    password_hash = hasher.hash(password)
    user_id = str(uuid4())
    with repository.transaction() as work:
        work.connection.execute("INSERT INTO users(user_id, username, password_hash) VALUES(?, ?, ?)",
                                (user_id, username, password_hash))
    return user_id


def authenticate(repository, username, password, client):
    """Persistent limits apply per normalized username and connecting address."""
    username = normalize_username(username)
    now = time.time()
    keys = [(digest("username:" + username), 10), (digest("client:" + client), 50)]
    with repository.transaction() as work:
        db = work.connection
        db.execute("DELETE FROM login_limits WHERE started_at < ?", (now - 900,))
        for key, maximum in keys:
            row = db.execute("SELECT attempts FROM login_limits WHERE key = ?", (key,)).fetchone()
            if row and row[0] >= maximum:
                return "limited", None
        for key, _ in keys:
            db.execute("INSERT INTO login_limits VALUES(?, ?, 1) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1",
                       (key, now))
        row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    try:
        valid = hasher.verify(row["password_hash"] if row else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid or row is None or row["disabled"]:
        return "denied", None
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with repository.transaction() as work:
        work.connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        work.connection.execute("INSERT INTO sessions VALUES(?, ?, ?, ?)",
                                (digest(session), row["user_id"], csrf, now + 43_200))
        work.connection.execute("DELETE FROM login_limits WHERE key = ?", (keys[0][0],))
    return "ok", {"session": session, "csrfToken": csrf, "userId": row["user_id"], "username": row["username"]}


def session_user(repository, token):
    if not token or len(token) > 256:
        return None
    with repository.transaction() as work:
        row = work.connection.execute(
            "SELECT users.user_id, users.username, users.disabled, sessions.csrf FROM sessions "
            "JOIN users ON users.user_id=sessions.owner_id WHERE session_hash=? AND expires_at>?",
            (digest(token), time.time())).fetchone()
        return dict(row) if row else None
