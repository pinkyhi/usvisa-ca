"""The only development exception to HTTPS is an explicitly enabled loopback URL."""
from urllib.parse import urlsplit


def server_origin(value: str, *, allow_local: bool = False) -> str:
    parsed = urlsplit(value.strip())
    local = allow_local and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
        raise ValueError("SERVER_URL/PUBLIC_BASE_URL must be HTTPS (HTTP is allowed only for explicit loopback tests)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Server URL must contain only scheme, hostname and optional port")
    try:
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except ValueError:
        raise ValueError("Invalid server port") from None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return f"{parsed.scheme}://{host}"


def websocket_url(origin: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("Invalid server scheme")
    return f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/ws"
