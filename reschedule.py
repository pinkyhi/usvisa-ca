"""Compatible source-checkout launcher for the Node application."""

import sys
from pathlib import Path

# Preserve "python reschedule.py" before an editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from usvisa.node.app import main


if __name__ == "__main__":
    raise SystemExit(main())
