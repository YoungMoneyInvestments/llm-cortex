#!/usr/bin/env python3
"""Quick health check for Cortex + Obsidian memory wiring."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path


HOME = Path.home()
DATA_DIR = Path(os.environ.get("CORTEX_DATA_DIR", HOME / ".cortex" / "data")).expanduser()
WORKER_URL = os.environ.get("CORTEX_WORKER_URL", "http://127.0.0.1:37778")
OBS_DB = DATA_DIR / "cortex-observations.db"
VECTOR_DB = DATA_DIR / "cortex-vectors.db"
KEY_FILE = DATA_DIR / ".worker_api_key"


def status(ok: bool, label: str, detail: str = "") -> None:
    mark = "OK" if ok else "WARN"
    suffix = f" - {detail}" if detail else ""
    print(f"[{mark}] {label}{suffix}")


def read_key() -> str:
    env_key = os.environ.get("CORTEX_WORKER_API_KEY", "").strip()
    try:
        file_key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
    except OSError:
        file_key = ""
    return file_key or env_key


def request_json(path: str, api_key: str = "", method: str = "GET") -> tuple[bool, dict]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{WORKER_URL}{path}", headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return False, {"error": str(exc)}


def check_worker() -> None:
    ok, body = request_json("/api/health")
    detail = body.get("status", body.get("error", "no status"))
    if "total_observations" in body:
        detail += f", observations={body['total_observations']}"
    status(ok and body.get("status") == "healthy", "worker health", detail)

    api_key = read_key()
    status(bool(api_key), "worker API key available", "env/key-file resolved" if api_key else "")
    if api_key:
        ok, body = request_json("/api/stats", api_key=api_key)
        total = body.get("total_observations")
        detail = f"observations={total}" if total is not None else body.get("error", "no stats")
        status(ok, "authenticated stats", detail)


def check_databases() -> None:
    status(OBS_DB.exists(), "observations DB", str(OBS_DB.resolve()) if OBS_DB.exists() else str(OBS_DB))
    status(VECTOR_DB.exists(), "vector DB canonical path", str(VECTOR_DB.resolve()) if VECTOR_DB.exists() else str(VECTOR_DB))

    if OBS_DB.exists():
        try:
            conn = sqlite3.connect(str(OBS_DB))
            rows = conn.execute(
                "SELECT agent, COUNT(*) FROM observations GROUP BY agent ORDER BY COUNT(*) DESC LIMIT 8"
            ).fetchall()
            detail = ", ".join(f"{agent or 'unknown'}={count}" for agent, count in rows)
            status(True, "observation agent counts", detail)
            rows = conn.execute(
                "SELECT vector_synced, COUNT(*) FROM observations GROUP BY vector_synced ORDER BY vector_synced"
            ).fetchall()
            detail = ", ".join(f"{value}={count}" for value, count in rows)
            status(True, "vector sync states", detail)
            conn.close()
        except sqlite3.Error as exc:
            status(False, "observation agent counts", str(exc))

    candidates = [
        DATA_DIR / "cortex-vectors.db",
        HOME / "clawd" / "data" / "cortex-vectors.db",
        HOME / "Projects" / "llm-cortex" / "data" / "cortex-vectors.db",
    ]
    existing = []
    for path in candidates:
        if path.exists():
            size_mb = path.stat().st_size / 1024 / 1024
            wal = path.with_name(path.name + "-wal")
            wal_mb = wal.stat().st_size / 1024 / 1024 if wal.exists() else 0
            existing.append(f"{path} ({size_mb:.1f}MB, wal={wal_mb:.1f}MB)")
    status(len(existing) <= 1, "extra vector DB copies", "; ".join(existing) if existing else "none")


def check_configs() -> None:
    files = {
        "Claude settings": HOME / ".claude" / "settings.json",
        "Codex hooks": HOME / ".codex" / "hooks.json",
        "Codex config": HOME / ".codex" / "config.toml",
        "Cursor MCP": HOME / ".cursor" / "mcp.json",
        "Gemini settings": HOME / ".gemini" / "settings.json",
    }
    for label, path in files.items():
        if not path.exists():
            status(False, label, "missing")
            continue
        text = path.read_text(errors="ignore")
        status("CORTEX_AGENT_NAME" in text, label, "agent attribution configured" if "CORTEX_AGENT_NAME" in text else "missing CORTEX_AGENT_NAME")

    hook_dir = HOME / "Projects" / "llm-cortex" / "hooks"
    for hook in ("post_tool_use.sh", "user_prompt_submit.sh", "session_end.sh"):
        path = hook_dir / hook
        text = path.read_text(errors="ignore") if path.exists() else ""
        has_key_fallback = ".worker_api_key" in text
        status(path.exists() and has_key_fallback, hook, "key fallback present" if has_key_fallback else "missing key fallback")


def check_obsidian() -> None:
    memory_dir = HOME / "Knowledge" / "claude-memory"
    index = memory_dir / "MEMORY.md"
    projects_dir = HOME / "Knowledge" / "projects"
    status(index.exists(), "Obsidian memory index", str(index))
    if index.exists():
        line_count = len(index.read_text(errors="ignore").splitlines())
        status(line_count <= 200, "Obsidian MEMORY.md size", f"{line_count} lines")
    status(projects_dir.exists(), "Obsidian project notes", str(projects_dir))


def main() -> int:
    print("Cortex Memory Health")
    check_worker()
    check_databases()
    check_configs()
    check_obsidian()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
