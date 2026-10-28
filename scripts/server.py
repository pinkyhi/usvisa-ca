"""Cross-platform server launcher. Generated secrets never appear in command arguments."""
import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    if not path.is_file():
        return {}
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#") and "=" in line)


def save_env(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    # os.open avoids a momentarily world-readable file on Unix.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("# Generated server configuration. Keep private and back up with the data volumes.\n")
        output.write("".join(f"{key}={value}\n" for key, value in values.items()))


def update_node_env(values):
    """Preserve existing visa credentials, comments, date filters and flags."""
    path = ROOT / ".env"
    body = path.read_text(encoding="utf-8") if path.is_file() else ""
    for key, value in values.items():
        pattern = rf"(?m)^\s*(?:export\s+)?{re.escape(key)}=.*$"
        line = f"{key}={value}"
        if re.search(pattern, body):
            body = re.sub(pattern, lambda _: line, body)
        else:
            body = body.rstrip("\n") + "\n" + line + "\n"
    path.write_text(body, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".server.env")
    parser.add_argument("--project", default="usvisa")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup")
    setup.add_argument("--mode", choices=["solo", "coordinated", "coordinated-passive"], default="coordinated")
    setup.add_argument("--profile", choices=["tailscale", "cloudflare_quick", "cloudflare_domain"], default="cloudflare_quick")
    setup.add_argument("--url", help="HTTPS origin for Tailscale or your named tunnel")
    setup.add_argument("--port", type=int, default=8080)
    setup.add_argument("--consulate-id", type=int)
    setup.add_argument("--city")
    setup.add_argument("--local-test", action="store_true", help="Explicit loopback-only HTTP test, never a public profile")
    up = sub.add_parser("up")
    up.add_argument("--mode", choices=["solo", "coordinated", "coordinated-passive"])
    up.add_argument("--server-only", action="store_true", help="Start infrastructure without the visa worker")
    sub.add_parser("down")
    sub.add_parser("status")
    user = sub.add_parser("create-user")
    user.add_argument("--username", required=True)
    sub.add_parser("backup")
    args = parser.parse_args()
    args.env_file = args.env_file.resolve()
    values = read_env(args.env_file)
    if args.command == "setup":
        if args.mode == "solo":
            if args.env_file == ROOT / ".server.env":
                update_node_env({"MODE": "solo",
                                 "COMPOSE_PROFILES": "${MODE:-solo},${MODE:-solo}-${EXPOSE_MODE:-cloudflare_quick}"})
            else:
                values.update(MODE="solo", EXPOSE_MODE=args.profile)
                save_env(args.env_file, values)
            print("Solo Node configured. Next: python scripts/server.py up")
            return 0
        if args.local_test:
            origin = f"http://127.0.0.1:{args.port}"
        elif args.url:
            origin = args.url.strip().lower().rstrip("/")
            if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?", origin):
                parser.error("--url must be an HTTPS origin")
            if origin.endswith(":443"):
                origin = origin[:-4]
        elif args.profile == "cloudflare_quick":
            origin = "https://pending.trycloudflare.com"
        else:
            parser.error("Provide --url for Tailscale or your Cloudflare domain")
        node_values = read_env(ROOT / ".env")
        city = args.city or node_values.get("USER_CONSULATE", "Vancouver").strip("'\"")
        consulate_id = args.consulate_id or {"Calgary": 89, "Halifax": 90, "Montreal": 91, "Ottawa": 92,
                                            "Quebec": 93, "Toronto": 94, "Vancouver": 95}.get(city)
        if not consulate_id:
            parser.error("Provide a supported city or --consulate-id")
        values.update(EXPOSE_MODE=args.profile, PUBLIC_BASE_URL=origin, LOCAL_PORT=str(args.port),
                      CONSULATE_ID=str(consulate_id), CITY=city,
                      ALLOW_INSECURE_LOCALHOST=str(args.local_test).lower())
        values.setdefault("BROKER_ADMIN_USER", "provisioner")
        values.setdefault("BROKER_ADMIN_PASSWORD", secrets.token_urlsafe(48))
        values.setdefault("BROKER_PASSWORD", secrets.token_urlsafe(48))
        values.setdefault("VERBOSE", "false")
        values.setdefault("LOG_GROUPS", "reschedule")
        if args.profile == "cloudflare_domain" and not values.get("CF_TUNNEL_TOKEN"):
            values["CF_TUNNEL_TOKEN"] = getpass.getpass("Cloudflare tunnel token: ").strip()
            if not values["CF_TUNNEL_TOKEN"]:
                parser.error("Cloudflare tunnel token is required")
        if values.get("CF_TUNNEL_TOKEN"):
            values["TUNNEL_TOKEN"] = values["CF_TUNNEL_TOKEN"]
        if args.env_file != ROOT / ".server.env":
            values["MODE"] = args.mode
        save_env(args.env_file, values)
        if args.env_file == ROOT / ".server.env":
            update_node_env({"MODE": args.mode, "EXPOSE_MODE": args.profile,
                             "COMPOSE_PROFILES": "${MODE:-solo},${MODE:-solo}-${EXPOSE_MODE:-cloudflare_quick}"})
            if args.profile != "cloudflare_quick":
                update_node_env({"SERVER_URL": origin})
        print(f"Saved {args.env_file}. Next: python scripts/server.py up")
        return 0
    node_values = read_env(ROOT / ".env")
    custom_mode = values.get("MODE") if args.env_file != ROOT / ".server.env" else None
    mode = (getattr(args, "mode", None) or custom_mode or node_values.get("MODE", "solo")).strip("'\"")
    if not values:
        if mode == "solo":
            values = {"EXPOSE_MODE": "cloudflare_quick"}
        else:
            parser.error("Run setup first")
    profile = values["EXPOSE_MODE"]
    if mode not in {"solo", "coordinated", "coordinated-passive"}:
        parser.error("MODE must be solo, coordinated or coordinated-passive")
    if getattr(args, "mode", None) and args.env_file == ROOT / ".server.env":
        update_node_env({"MODE": mode})
    if values.get("CF_TUNNEL_TOKEN") and values.get("TUNNEL_TOKEN") != values["CF_TUNNEL_TOKEN"]:
        values["TUNNEL_TOKEN"] = values["CF_TUNNEL_TOKEN"]
        save_env(args.env_file, values)
    base = ["docker", "compose", "--project-name", args.project]
    if args.env_file.is_file():
        base.extend(["--env-file", str(args.env_file)])
    base.extend(["-f", str(ROOT / "compose.yaml")])

    def compose(*arguments, capture=False, input=None):
        environment = {**os.environ, **values, "MODE": mode, "EXPOSE_MODE": profile,
                       "SERVER_ENV_FILE": str(args.env_file),
                       "COMPOSE_PROFILES": f"{mode},{mode}-{profile}"}
        return subprocess.run(base + list(arguments), cwd=ROOT, check=True, text=True, env=environment,
                              capture_output=capture, input=input)

    if args.command == "up":
        if mode == "solo":
            compose("--profile", "*", "stop", "quick", "tunnel", "edge", "coordinator", "rabbitmq")
            compose("up", "-d", "--build", "node")
            print("Solo Node started")
            return 0
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180", "rabbitmq", "coordinator", "edge")
        compose("exec", "-T", "edge", "nginx", "-s", "reload")
        profile = values["EXPOSE_MODE"]
        # Stop the previous connector, retaining all state when switching profiles.
        compose("--profile", "*", "stop", "quick", "tunnel")
        if values.get("ALLOW_INSECURE_LOCALHOST") == "true":
            print("Loopback test ready: " + values["PUBLIC_BASE_URL"])
            return 0
        if profile == "cloudflare_quick":
            compose("--profile", "coordinated-cloudflare_quick", "up", "-d", "--force-recreate", "quick")
            for _ in range(60):
                logs = compose("--profile", "coordinated-cloudflare_quick", "logs", "--no-color", "quick", capture=True)
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", logs.stdout + logs.stderr)
                if match:
                    values["PUBLIC_BASE_URL"] = match[0]
                    save_env(args.env_file, values)
                    compose("up", "-d", "--no-deps", "--wait", "coordinator")
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Quick Tunnel URL was not received; inspect docker compose logs quick")
        elif profile == "cloudflare_domain":
            compose("--profile", "coordinated-cloudflare_domain", "up", "-d", "tunnel")
        elif profile == "tailscale":
            executable = shutil.which("tailscale")
            if not executable:
                raise RuntimeError("Install Tailscale on the host, sign in, then run up again")
            subprocess.run([executable, "serve", "--bg", f"http://127.0.0.1:{values['LOCAL_PORT']}"], check=True)
        import urllib.request
        for _ in range(45):
            try:
                with urllib.request.urlopen(values["PUBLIC_BASE_URL"] + "/health/ready", timeout=3) as response:
                    if response.status == 200:
                        break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Public HTTPS readiness failed; check the tunnel hostname/route or Tailscale Serve and tailnet access")
        print("Portal and Node SERVER_URL: " + values["PUBLIC_BASE_URL"])
        if not args.server_only:
            compose("up", "-d", "--build", "node")
    elif args.command == "create-user":
        password = getpass.getpass("Portal password (at least 8 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
        compose("exec", "-T", "coordinator", "usvisa-coordinator", "create-user", "--username", args.username,
                "--password-stdin", input=password + "\n")
    elif args.command == "down":
        compose("--profile", "*", "down")
    elif args.command == "status":
        compose("--profile", "*", "ps")
    elif args.command == "backup":
        # SQLite online backup is consistent while the service runs. Broker data
        # needs a cold volume backup; the runbook documents that separately.
        compose("exec", "-T", "coordinator", "python", "-c",
                "import sqlite3; from pathlib import Path; from datetime import datetime; "
                "p=Path('/data/backups'); p.mkdir(exist_ok=True); "
                "s=sqlite3.connect('/data/.coordinator/state.sqlite3'); "
                "d=sqlite3.connect(p/(datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')); "
                "s.backup(d); d.close(); s.close(); print('SQLite backup saved in /data/backups')")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
