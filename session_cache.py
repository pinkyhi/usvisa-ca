"""Persist Selenium's authentication handoff and subsequent HTTP cookie updates."""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from appointment_client import AppointmentClient


logger = logging.getLogger(__name__)


class SessionCache:
    def __init__(self, directory, account_email):
        self.directory = Path(directory)
        account_key = hashlib.sha256(account_email.strip().casefold().encode("utf-8")).hexdigest()
        self.path = self.directory / f"{account_key}.json"

    def load(self, facility_id, consulate, timeout=30):
        session = None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            url = payload["appointment_url"]
            parsed = urlsplit(url)
            if (payload["version"] != 1 or parsed.scheme != "https"
                    or parsed.netloc != "ais.usvisa-info.com"
                    or not re.fullmatch(r"/en-ca/niv/schedule/\d+/appointment", parsed.path)
                    or parsed.query or parsed.fragment
                    or not isinstance(payload["user_agent"], str) or not payload["user_agent"]
                    or not isinstance(payload["cookies"], list)):
                raise ValueError("Invalid saved session")
            session = requests.Session()
            session.headers.update({"User-Agent": payload["user_agent"], "Referer": url})
            for item in payload["cookies"]:
                if (not isinstance(item["name"], str) or not item["name"]
                        or not isinstance(item["value"], str)
                        or not isinstance(item["domain"], str)
                        or item["domain"].lstrip(".") not in {"ais.usvisa-info.com", "usvisa-info.com"}
                        or not isinstance(item["path"], str) or not item["path"].startswith("/")
                        or not isinstance(item["secure"], bool)):
                    raise ValueError("Invalid saved cookie")
                expires = item.get("expires")
                if expires is not None:
                    if not isinstance(expires, (int, float)):
                        raise ValueError("Invalid cookie expiry")
                    if expires <= time.time():
                        continue
                session.cookies.set(
                    item["name"], item["value"], domain=item["domain"], path=item["path"],
                    secure=item["secure"], expires=expires,
                )
            if not session.cookies:
                session.close()
                return None
            return AppointmentClient(session, url, facility_id, consulate, timeout)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            if session is not None:
                session.close()
            # No payloads or cookie values in logs.
            logger.warning("Saved login session could not be loaded; Selenium login will be used")
            return None

    def save(self, client):
        temporary_path = None
        try:
            payload = {
                "version": 1,
                "appointment_url": client.appointment_url,
                "user_agent": client.session.headers["User-Agent"],
                "cookies": [
                    {"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                     "path": cookie.path, "secure": cookie.secure, "expires": cookie.expires}
                    for cookie in client.session.cookies if not cookie.is_expired()
                ],
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            # Replace atomically so an interruption cannot leave half a JSON file.
            # tempfile creates the file with owner-only permissions on POSIX.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory,
                prefix=self.path.stem + "-", suffix=".tmp", delete=False,
            ) as output:
                temporary_path = Path(output.name)
                json.dump(payload, output)
            os.replace(temporary_path, self.path)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Login session could not be saved; the booking result is unchanged")
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Temporary session file could not be removed")

    def invalidate(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Expired session file could not be removed")
