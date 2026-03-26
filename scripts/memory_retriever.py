#!/usr/bin/env python3
"""Compatibility wrapper — delegates to canonical implementation in src/."""
from pathlib import Path
import runpy
if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    runpy.run_path(str(root / "src" / "memory_retriever.py"), run_name="__main__")
