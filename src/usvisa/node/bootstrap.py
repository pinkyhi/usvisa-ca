"""Owner-authenticated, crash-safe first registration of a persistent Node."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import requests

from usvisa.shared.discovery import quick_origin, save_private_json
from usvisa.shared.network import server_origin
from usvisa.shared.paths import data_directory
from usvisa.transport.webstomp import parse_token


class BootstrapError(ValueError):
    pass


def resolve_origin():
    metrics = os.getenv("QUICK_TUNNEL_METRICS", "")
    if metrics and os.getenv("EXPOSE_MODE", "") == "cloudflare_quick":
        return quick_origin(metrics)
    return server_origin(os.getenv("SERVER_URL", ""),
                         allow_local=os.getenv("ALLOW_INSECURE_LOCALHOST", "false").lower() == "true")


class Registration:
    def __init__(self, path=None):
        self.path = Path(path or data_directory() / ".node-state" / "registration.json")
        try:
            self.value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.value, dict) or not self.value.get("key"):
                raise ValueError("Invalid saved Node registration")
        except FileNotFoundError:
            self.value = {"key": str(uuid4())}
            self.save()

    def save(self):
        save_private_json(self.path, self.value)

    def request(self, session, origin, path, method="GET", body=None, key=None):
        headers = {"Origin": origin}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if key:
            headers["Idempotency-Key"] = key
        response = session.request(method, origin + "/api/v1" + path, json=body,
                                   headers=headers, timeout=15, allow_redirects=False)
        if response.status_code in {401, 403}:
            raise BootstrapError("Portal rejected the credentials or Node access")
        if response.status_code >= 500 or response.status_code in {400, 429}:
            raise requests.ConnectionError("Portal provisioning is temporarily unavailable")
        if response.status_code not in {200, 201}:
            raise BootstrapError(f"Portal registration failed (HTTP {response.status_code})")
        return response.json()

    def provision(self, origin, username, password, name, session=None):
        origin = server_origin(origin, allow_local=os.getenv("ALLOW_INSECURE_LOCALHOST", "false").lower() == "true")
        username = username.strip().lower()
        if self.value.get("username", username) != username:
            raise BootstrapError("Saved Node belongs to another portal login; use a separate data directory")
        self.value["username"] = username
        self.save()
        self.csrf = ""
        owned_session = session is None
        session = session or requests.Session()
        try:
            self.csrf = self.request(session, origin, "/csrf")["csrfToken"]
            result = self.request(session, origin, "/session", "POST", {"username": username, "password": password})
            self.csrf = result["csrfToken"]
            # Server-side idempotency keeps the same identity even after a crash
            # between broker provisioning and persisting the returned token.
            result = self.request(session, origin, "/nodes", "POST", {"name": name}, self.value["key"])
            node_id = result["nodeId"]
            nodes = self.request(session, origin, "/nodes")["nodes"]
            node = next((node for node in nodes if node["nodeId"] == node_id), None)
            if node is None or node["lifecycle"] == "revoked":
                raise BootstrapError("This Node was revoked; automatic registration will not restore its access")
            if not result.get("token") and not self.value.get("token"):
                for _ in range(3):
                    key = self.value.setdefault("rotationKey", str(uuid4()))
                    self.save()
                    result = self.request(session, origin, f"/nodes/{node_id}/token", "POST", key=key)
                    if result.get("token"):
                        break
                    self.value.pop("rotationKey", None)
                else:
                    raise BootstrapError("Could not recover the Node token")
            token = result.get("token") or self.value["token"]
            if str(parse_token(token)[0]) != node_id:
                raise BootstrapError("Portal returned a token for another Node")
            self.value.update(token=token, nodeId=node_id, origin=origin)
            self.value.pop("rotationKey", None)
            self.save()
            return token
        finally:
            if owned_session:
                try:
                    session.request("DELETE", origin + "/api/v1/session",
                                    headers={"Origin": origin, "X-CSRF-Token": self.csrf},
                                    timeout=3, allow_redirects=False)
                except requests.RequestException:
                    pass
                session.close()


def prepare():
    if os.getenv("NODE_TOKEN"):
        parse_token(os.environ["NODE_TOKEN"])
        os.environ["SERVER_URL"] = resolve_origin()
        return
    username, password = os.getenv("PORTAL_USERNAME", ""), os.getenv("PORTAL_PASSWORD", "")
    if not username or not password:
        raise BootstrapError("Provide NODE_TOKEN or PORTAL_USERNAME and PORTAL_PASSWORD")
    registration = Registration()
    delay = 1
    while True:
        try:
            origin = resolve_origin()
            ready = requests.get(origin + "/health/ready", timeout=10, allow_redirects=False)
            if ready.status_code != 200 or ready.json().get("ready") is not True:
                raise requests.ConnectionError("Coordinator is not ready")
            token = registration.provision(origin, username, password, os.getenv("NODE_NAME", "Docker Node"))
            os.environ.update(NODE_TOKEN=token, SERVER_URL=origin)
            print(f"Node registered: {registration.value['nodeId']}", flush=True)
            return
        except BootstrapError:
            raise
        except (OSError, requests.RequestException, KeyError, ValueError):
            # Cached credentials and snapshots remain usable through a portal or
            # tunnel outage. The communication thread retries independently.
            if registration.value.get("token") and registration.value.get("origin"):
                parse_token(registration.value["token"])
                os.environ.update(NODE_TOKEN=registration.value["token"], SERVER_URL=registration.value["origin"])
                print("Portal registration temporarily unavailable; using saved Node credentials. "
                      "Coordinator connection will retry automatically.", flush=True)
                return
            print(f"Waiting for Coordinator registration; retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
