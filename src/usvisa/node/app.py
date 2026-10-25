"""Node command line; help does not import browser or network clients."""

import argparse
import sys

from usvisa import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="US visa monitoring node")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--mode", choices=("solo", "coordinated"), help="Override MODE from configuration")
    args = parser.parse_args(argv)

    from usvisa.node import settings

    mode = args.mode or settings.MODE
    if mode not in {"solo", "coordinated"}:
        parser.error("MODE must be solo or coordinated")
    if mode == "coordinated":
        print("Coordinated mode is not implemented yet; use MODE=solo for the current runner.", file=sys.stderr)
        return 2

    settings.MODE = mode
    from usvisa.node.solo import main as run_solo

    return run_solo()
