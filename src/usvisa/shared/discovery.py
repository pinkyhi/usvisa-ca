"""Discover the local Quick Tunnel without Docker socket access or log scraping."""
import json
import os
from pathlib import Path
import re
import urllib.request

from .network import server_origin


def quick_origin(metrics_url):
    # This endpoint is reachable only on the private Compose network.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(metrics_url, timeout=3) as response:
        hostname = json.loads(response.read(4096))["hostname"]
    if not isinstance(hostname, str) or not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", hostname):
        raise ValueError("Quick Tunnel has not supplied a valid hostname")
    return server_origin("https://" + hostname)


def save_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def saved_origin(path, fallback, *, allow_local=False):
    try:
        return server_origin(json.loads(Path(path).read_text(encoding="utf-8"))["origin"], allow_local=allow_local)
    except (OSError, ValueError, KeyError, TypeError):
        return fallback
