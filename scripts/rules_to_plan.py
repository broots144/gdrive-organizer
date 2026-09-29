#!/usr/bin/env python3
"""Kept for older instructions: same as `gdrive-organizer plan`."""
import sys

import _pkg  # noqa: E402,F401  (loads the package without the repo root on sys.path)
from gdrive_organizer.plan import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
