#!/usr/bin/env python3
"""Compatibility wrapper — delegates to canonical implementation in src/."""
from pathlib import Path
import runpy
root = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    runpy.run_path(str(root / "src" / "unified_vector_store.py"), run_name="__main__")
else:
    globals().update(runpy.run_path(str(root / "src" / "unified_vector_store.py")))
