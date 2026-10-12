#!/usr/bin/env python3
"""Thin wrapper for the end-to-end smoke test.

The implementation lives in ``crewhall.smoke`` so an installed package can run
it too (``crewhall watch smoke``). This script only makes the checkout
importable and forwards the arguments.

    python scripts/smoke.py            # skip cleanly if the CLIs are missing
    python scripts/smoke.py --require  # fail instead of skipping
"""
from __future__ import annotations

import os
import sys

# Make the repository importable when run as `python scripts/smoke.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import smoke  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(smoke.main())
