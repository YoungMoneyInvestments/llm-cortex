#!/usr/bin/env python3
"""Compatibility wrapper for the canonical worker in src/.

Public documentation should use src/memory_worker.py. This legacy entrypoint
delegates to the canonical implementation for older local setups.
"""

from pathlib import Path
import runpy


root = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    runpy.run_path(str(root / "src" / "memory_worker.py"), run_name="__main__")
else:
    globals().update(runpy.run_path(str(root / "src" / "memory_worker.py")))
