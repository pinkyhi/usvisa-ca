"""Coordinator command line, separated from local visa account dependencies."""

import argparse
import sys

from usvisa import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="US visa scheduling coordinator")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--database", help="SQLite path (default: USVISA_DATA_DIR/.coordinator/state.sqlite3)")
    commands = parser.add_subparsers(dest="command")
    initialize = commands.add_parser("init-db", help="Initialize local state and group configuration")
    initialize.add_argument("--consulate-id", type=int, required=True)
    initialize.add_argument("--city", required=True)
    commands.add_parser("state", help="Print persisted state as JSON, without starting network services")
    user = commands.add_parser("create-user", help="Create a portal account locally")
    user.add_argument("--username", required=True)
    user.add_argument("--password-stdin", action="store_true", help="Read password from stdin instead of prompting")
    serve = commands.add_parser("serve", help="Run portal and AMQP coordinator (one instance)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    commands.add_parser("health", help="Check local coordinator readiness")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    # Keep --help/--version independent of configuration and runtime dependencies.
    import json
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path

    from usvisa.protocol.messages import GroupConfig
    from usvisa.shared.paths import data_directory, load_environment
    from .repository import Repository
    from .service import Coordinator

    load_environment()
    if args.command == "health":
        import os
        import urllib.request
        from urllib.parse import urlsplit
        from usvisa.shared.discovery import saved_origin
        try:
            origin_file = data_directory() / ".coordinator" / "public-origin.json"
            origin = saved_origin(origin_file, os.environ["PUBLIC_BASE_URL"],
                                  allow_local=os.getenv("ALLOW_INSECURE_LOCALHOST", "false").lower() == "true")
            request = urllib.request.Request("http://127.0.0.1:8000/health/ready",
                                             headers={"Host": urlsplit(origin).netloc})
            with urllib.request.urlopen(request, timeout=3) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    if args.command == "serve":
        import uvicorn
        from .config import ServerConfig
        from .portal import create_app
        try:
            config = ServerConfig.from_env(args.database)
        except (ValueError, KeyError) as error:
            parser.error(f"Invalid server configuration: {error}")
        uvicorn.run(create_app(config), host=args.host, port=args.port, access_log=False,
                    proxy_headers=False, workers=1)
        return 0
    database = Path(args.database).expanduser() if args.database else data_directory() / ".coordinator" / "state.sqlite3"
    if args.command == "state" and not database.is_file():
        parser.error("Coordinator database does not exist; run init-db first")
    try:
        config = GroupConfig(args.consulate_id, args.city) if args.command == "init-db" else None
        with Repository(database) as repository:
            if args.command == "create-user":
                import getpass
                from .auth import create_user
                password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
                if not args.password_stdin and password != getpass.getpass("Confirm password: "):
                    parser.error("Passwords do not match")
                create_user(repository, args.username, password)
                print("Portal account created")
                return 0
            if config is not None:
                Coordinator(repository, config, now=datetime.now(timezone.utc))
            print(json.dumps(repository.view(), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    return 0
