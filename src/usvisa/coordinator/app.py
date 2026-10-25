"""Coordinator command line, separated from local visa account dependencies."""

import argparse
import sys

from usvisa import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="US visa scheduling coordinator (under development)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    print("Coordinator services are not implemented yet; see docs/coordinated-mode-plan.md.", file=sys.stderr)
    return 2
