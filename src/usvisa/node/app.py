"""Node command line; help does not import browser or network clients."""

import argparse

from usvisa import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="US visa monitoring node")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--mode", choices=("solo", "coordinated"), help="Override MODE from configuration")
    parser.add_argument("--check-coordination", action="store_true", help="Register and receive a schedule without contacting the visa site")
    args = parser.parse_args(argv)

    from usvisa.node import settings
    import os
    if os.getenv("USVISA_CONTAINER") == "true":
        import signal

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)

    mode = args.mode or settings.MODE
    if mode not in {"solo", "coordinated"}:
        parser.error("MODE must be solo or coordinated")
    if mode == "coordinated":
        import logging
        import sys

        logging.basicConfig(stream=sys.stdout, level=logging.WARNING,
                            format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        logging.getLogger("usvisa.transport").setLevel(logging.INFO)
        settings.MODE = mode
        from usvisa.node.bootstrap import prepare
        try:
            settings.validate_settings()
            if not os.environ.get("NODE_MIN_DATE"):
                os.environ["NODE_MIN_DATE"] = settings.LATEST_ACCEPTABLE_DATE
            prepare()
        except ValueError as error:
            print(f"Invalid coordinated configuration: {error}")
            return 2
        except KeyboardInterrupt:
            return 0
        from usvisa.node.coordinated import main as run_coordinated
        return run_coordinated(**({"check_only": True} if args.check_coordination else {}))

    settings.MODE = mode
    if args.check_coordination:
        parser.error("--check-coordination requires coordinated mode")
    from usvisa.node.solo import main as run_solo

    return run_solo()
