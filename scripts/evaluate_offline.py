#!/usr/bin/env python3
"""Run the fixed offline kernel regression gate."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capslock.evaluation.offline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
