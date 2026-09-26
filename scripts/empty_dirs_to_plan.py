#!/usr/bin/env python3
"""Kept for older instructions: same as `gdrive-organizer empty-dirs`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gdrive_organizer.empty_dirs import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
