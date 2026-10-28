"""Node command line; help does not import browser or network clients."""

import argparse
from datetime import date, timedelta

from usvisa import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="US visa monitoring node")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--mode", choices=("solo", "coordinated", "coordinated-passive"), help="Override MODE from configuration")
    parser.add_argument("--check-coordination", action="store_true", help="Register and receive a schedule without contacting the visa site")
    booking = parser.add_mutually_exclusive_group()
    booking.add_argument("--booking-status", action="store_true", help="Show local booking blocks without contacting the visa site")
    booking.add_argument("--reset-booking-block", action="store_true", help="Clear local booking blocks while the node is stopped; next start may book again")
    parser.add_argument("--application-id", help="Visa application ID when saved booking/session metadata is unavailable")
    args = parser.parse_args(argv)

    from usvisa.node import settings
    import os
    if args.booking_status or args.reset_booking_block:
        import json
        from usvisa.node.booking_state import manage
        try:
            print(json.dumps(manage(reset=args.reset_booking_block, application_id=args.application_id), indent=2))
            return 0
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            print(f"Booking state command failed: {error}")
            return 2
    if args.application_id:
        parser.error("--application-id requires --booking-status or --reset-booking-block")
    if os.getenv("USVISA_CONTAINER") == "true":
        import signal

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)

    mode = args.mode or settings.MODE
    if mode not in {"solo", "coordinated", "coordinated-passive"}:
        parser.error("MODE must be solo, coordinated or coordinated-passive")
    if mode in {"coordinated", "coordinated-passive"}:
        import logging
        import sys

        logging.basicConfig(stream=sys.stdout, level=logging.WARNING,
                            format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        logging.getLogger("usvisa.transport").setLevel(logging.INFO)
        settings.MODE = mode
        from usvisa.node.bootstrap import prepare
        try:
            settings.validate_settings()
            if mode != "coordinated-passive" and not os.environ.get("NODE_MIN_DATE"):
                # The booking limit is inclusive; Coordinator uses day < minDate.
                os.environ["NODE_MIN_DATE"] = (
                    date.fromisoformat(settings.LATEST_ACCEPTABLE_DATE) + timedelta(days=1)
                ).isoformat()
            prepare()
        except (ValueError, OverflowError) as error:
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
    from usvisa.shared import audit
    from usvisa.shared.locking import InstanceLock
    lock = InstanceLock(audit.GUARD_DIR / "solo.lock")
    try:
        return run_solo()
    finally:
        lock.close()
