#!/usr/bin/env python3
"""Compatibility wrapper for the canonical worker in src/.

Public documentation should use src/memory_worker.py. This legacy entrypoint
delegates to the canonical implementation for older local setups.
"""

from pathlib import Path
import runpy


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    runpy.run_path(str(root / "src" / "memory_worker.py"), run_name="__main__")
