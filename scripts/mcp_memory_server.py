#!/usr/bin/env python3
"""Compatibility wrapper for the canonical MCP server in src/.

Public configurations should point to src/mcp_memory_server.py. This legacy
entrypoint remains only to avoid breaking older local setups.
"""

from pathlib import Path
import runpy


root = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    runpy.run_path(str(root / "src" / "mcp_memory_server.py"), run_name="__main__")
else:
    globals().update(runpy.run_path(str(root / "src" / "mcp_memory_server.py")))
