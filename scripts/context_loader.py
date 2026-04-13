#!/usr/bin/env python3
"""
Session Memory Bootstrap - Layer 2

Runs at session start via SessionStart hook. Scans recent memory files
and injects context automatically so Claude picks up where you left off.

Usage:
    python3 context_loader.py [--hours N]

Configure:
    Set CORTEX_WORKSPACE to your project root (default: ~/cortex)
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from working_memory import WorkingMemory

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
MEMORY_DIR = WORKSPACE / "memory"
PLANNING_DIR = WORKSPACE / ".planning"
HANDOFF_DIR = PLANNING_DIR / "handoffs"
WORKING_MEMORY_DIR = PLANNING_DIR / "working-memory"

# Patterns that indicate ongoing technical work
TECHNICAL_PATTERNS = [
    r"config.*fix|fix.*config",
    r"debug|troubleshoot|investigate",
    r"implement|deploy|launch",
    r"error|fail|broken|issue",
    r"test.*run|run.*test",
    r"git.*commit|commit.*push",
]

# Patterns indicating incomplete work
INCOMPLETE_PATTERNS = [
    r"\btodo\b|action item|follow.?up",
    r"\bincomplete\b|\bunfinished\b|\bpending\b",
    r"next step|next:",
]


class MemoryBootstrap:
    def __init__(self, hours: int = 48):
        self.hours = hours
        self.cutoff = datetime.now() - timedelta(hours=hours)
        self.recent_files: List[Path] = []
        self.technical_work: List[Dict] = []
        self.incomplete_items: List[Dict] = []
        self.working_memory: Optional[WorkingMemory] = None
        self._load_working_memory()

    def _load_working_memory(self):
        """Load the most recent working memory session."""
        if not WORKING_MEMORY_DIR.exists():
            return
        session_dirs = [
            d
            for d in WORKING_MEMORY_DIR.iterdir()
            if d.is_dir() and d.name.startswith("session-")
        ]
        if not session_dirs:
            return
        most_recent = max(session_dirs, key=lambda d: d.stat().st_mtime)
        session_key = most_recent.name.replace("session-", "")
        try:
            self.working_memory = WorkingMemory(session_key)
        except Exception:
            self.working_memory = None

    def find_recent_memory_files(self) -> List[Path]:
        """Find memory files modified within the time window."""
        files = []
        if MEMORY_DIR.exists():
            for f in MEMORY_DIR.rglob("*.md"):
                try:
                    if "archive" in f.parts:
                        continue
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime > self.cutoff:
                        files.append(f)
                except Exception:
                    continue
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def find_recent_handoffs(self) -> List[Dict]:
        """Find session handoff documents."""
        if not HANDOFF_DIR.exists():
            return []
        handoffs = []
        for f in HANDOFF_DIR.glob("*.md"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime > self.cutoff:
                    content = f.read_text(encoding="utf-8")
                    handoffs.append(
                        {
                            "name": f.name,
                            "mtime": mtime.isoformat(),
                            "preview": content[:1000],
                        }
                    )
            except Exception:
                continue
        return sorted(handoffs, key=lambda h: h["mtime"], reverse=True)

    def _load_profile(self) -> str:
        """Load user profile entries from the Cortex SQLite DB and return a formatted string.

        Uses CORTEX_DATA_DIR env var, defaulting to ~/.cortex/data/.
        Returns empty string if the DB does not exist or has no profile entries.
        """
        data_dir_env = os.environ.get("CORTEX_DATA_DIR", "").strip()
        if data_dir_env:
            db_path = Path(data_dir_env).expanduser() / "cortex-observations.db"
        else:
            db_path = Path.home() / ".cortex" / "data" / "cortex-observations.db"

        if not db_path.exists():
            return ""

        try:
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT category, key, value FROM profile "
                "ORDER BY category ASC, confidence DESC"
            ).fetchall()
            conn.close()
        except Exception:
            return ""

        if not rows:
            return ""

        # Group by category
        grouped: Dict[str, List[str]] = {}
        for r in rows:
            cat = r["category"]
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(f"{r['key']}: {r['value']}")

        label_map = {
            "expertise": "Expertise",
            "preference": "Preferences",
            "style": "Style",
            "context": "Context",
        }

        lines = ["## User Profile"]
        for cat in ("expertise", "preference", "style", "context"):
            if cat not in grouped:
                continue
            label = label_map.get(cat, cat.capitalize())
            lines.append(f"**{label}:** {', '.join(grouped[cat])}")

        # Any categories not in the canonical set
        for cat, entries in grouped.items():
            if cat not in label_map:
                lines.append(f"**{cat.capitalize()}:** {', '.join(entries)}")

        return "\n".join(lines)

    def generate_summary(self) -> str:
        """Generate context summary injected at session start."""
        self.recent_files = self.find_recent_memory_files()
        handoffs = self.find_recent_handoffs()

        now = datetime.now()
        lines = []
        lines.append("=" * 60)
        lines.append(
            f"TIME VERIFIED: {now.strftime('%I:%M %p')} "
            f"on {now.strftime('%A, %Y-%m-%d')}"
        )
        lines.append("=" * 60)
        lines.append("")

        # === USER PROFILE (from Cortex DB) ===
        profile_section = self._load_profile()
        if profile_section:
            lines.append(profile_section)
            lines.append("")

        # === WORKING MEMORY ===
        if self.working_memory:
            active_goals = self.working_memory.get_active_goals()
            if active_goals:
                lines.append("Working Memory (Active Session):")
                lines.append(f"  Active Goals ({len(active_goals)}):")
                for goal in active_goals[:5]:
                    marker = (
                        "[!]" if goal["priority"] in ["high", "urgent"] else "[ ]"
                    )
                    lines.append(
                        f"    {marker} {goal['goal']} [{goal['priority']}]"
                    )
                lines.append("")

            scratchpad = self.working_memory.read_scratchpad()
            if scratchpad:
                notes = scratchpad.strip().split("\n")
                lines.append(f"  Scratchpad ({len(notes)} notes):")
                for note in notes[-3:]:
                    lines.append(f"    {note[:80]}")
                lines.append("")

        # === RECENT ACTIVITY ===
        lines.append(f"Recent Activity (last {self.hours}h):")

        if handoffs:
            lines.append(f"\n  Session handoffs ({len(handoffs)}):")
            for h in handoffs[:3]:
                lines.append(f"    - {h['name']}")
            lines.append("    -> Read these first for full context")

        # Detect technical work in progress
        for filepath in self.recent_files:
            try:
                content = filepath.read_text()
                content_lower = content.lower()
                for pattern in TECHNICAL_PATTERNS:
                    if re.search(pattern, content_lower):
                        self.technical_work.append(
                            {"file": filepath.name, "pattern": pattern}
                        )
                        break
            except Exception:
                continue

        if self.technical_work:
            lines.append("\n  Technical work in progress:")
            for i, work in enumerate(self.technical_work[:5], 1):
                lines.append(f"    {i}. {work['file']}: {work['pattern']}")

        # Find incomplete items
        for filepath in self.recent_files:
            try:
                content = filepath.read_text()
                for line_text in content.split("\n"):
                    stripped = line_text.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "[x]" in stripped.lower():
                        continue
                    for pattern in INCOMPLETE_PATTERNS:
                        if re.search(pattern, line_text.lower()):
                            self.incomplete_items.append(
                                {"line": stripped[:80], "file": filepath.name}
                            )
                            break
            except Exception:
                continue

        if self.incomplete_items:
            lines.append(f"\n  Incomplete items ({len(self.incomplete_items)}):")
            for i, item in enumerate(self.incomplete_items[:5], 1):
                lines.append(f"    {i}. [{item['file']}] {item['line']}...")

        if not self.recent_files and not handoffs:
            lines.append("  No recent activity found. Fresh start.")

        return "\n".join(lines)


def cortex_recall(cwd: Optional[str] = None) -> str:
    """Query the cortex memory worker for observations relevant to the current project.

    Detects the project name from CWD (last path component), searches cortex
    for the 5 most relevant observations, and returns a formatted summary block.

    Returns an empty string if the worker is unreachable or no results are found.
    """
    worker_url = os.environ.get("CORTEX_WORKER_URL", "http://localhost:37778")
    api_key = os.environ.get("CORTEX_WORKER_API_KEY", "cortex-local-2026")

    # Determine project name from CWD
    project_dir = cwd or os.environ.get("CORTEX_PROJECT_DIR") or os.getcwd()
    project_name = Path(project_dir).name
    if not project_name or project_name in (".", "/"):
        return ""

    # Build the search request
    endpoint = f"{worker_url}/api/memory/search"
    payload = json.dumps({"query": project_name, "limit": 5}).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return ""

    results = body.get("results", [])
    if not results:
        return ""

    lines = [f"Cortex Recall ({len(results)} relevant observations):"]
    for obs in results:
        timestamp = obs.get("timestamp", "")
        # Extract just the date portion (YYYY-MM-DD)
        date_str = "unknown"
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                # Try simple prefix extraction
                if len(timestamp) >= 10:
                    date_str = timestamp[:10]
        summary = obs.get("summary", "").strip()
        if summary:
            # Truncate long summaries to keep context concise
            if len(summary) > 120:
                summary = summary[:117] + "..."
            lines.append(f"  - [{date_str}] {summary}")

    return "\n".join(lines) if len(lines) > 1 else ""


OBSIDIAN_VAULT = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Cameron"

# Maps project directory name (or path fragment) to vault folder name.
# Checked in order — first match wins.
_PROJECT_VAULT_MAP: list[tuple[str, str]] = [
    ("broker-bridge-retail", "BrokerBridge"),
    ("MCP-Servers/brokerbridge", "BrokerBridgeMCP"),
    ("brokerbridge", "BrokerBridgeMCP"),   # fallback for bare name
    ("openclaw", "OpenClaw"),
    ("matrix-lstm", "MatrixLSTM"),
    ("moltytrades", "MoltyTrades"),
    ("CLIProxyAPI", "CLIProxyAPI"),
    ("ymi-website", "YMIWebsite"),
    ("llm-cortex", "LLMCortex"),
]


def _resolve_vault_folder(project_dir: str) -> Optional[Path]:
    """Return the Obsidian vault subfolder for the given project directory, or None."""
    for pattern, vault_name in _PROJECT_VAULT_MAP:
        if pattern.lower() in project_dir.lower():
            folder = OBSIDIAN_VAULT / vault_name
            if folder.exists():
                return folder
    return None


def obsidian_context(cwd: Optional[str] = None) -> str:
    """Read project-specific Obsidian vault files and return a formatted context block.

    Reads architecture.md, decisions.md, open-questions.md, and the most recent
    session note from the vault folder matching the current project.
    """
    project_dir = cwd or os.environ.get("CORTEX_PROJECT_DIR") or os.getcwd()
    vault_folder = _resolve_vault_folder(project_dir)
    if vault_folder is None:
        return ""

    label = vault_folder.name
    lines = [f"## Obsidian Vault — {label}"]

    standing_files = ["architecture.md", "decisions.md", "open-questions.md"]
    for fname in standing_files:
        fpath = vault_folder / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8").strip()
                if content:
                    lines.append(f"\n### {fname}")
                    if len(content) > 1500:
                        content = content[:1497] + "..."
                    lines.append(content)
            except Exception:
                continue

    # Most recent session note
    sessions_dir = vault_folder / "sessions"
    if sessions_dir.exists():
        session_files = sorted(sessions_dir.glob("*.md"), reverse=True)
        if session_files:
            try:
                content = session_files[0].read_text(encoding="utf-8").strip()
                if content:
                    lines.append(f"\n### Last session ({session_files[0].stem})")
                    if len(content) > 800:
                        content = content[:797] + "..."
                    lines.append(content)
            except Exception:
                pass

    return "\n".join(lines) if len(lines) > 1 else ""


def main():
    parser = argparse.ArgumentParser(description="Session memory bootstrap")
    parser.add_argument("--hours", type=int, default=48)
    args = parser.parse_args()

    bootstrap = MemoryBootstrap(hours=args.hours)
    summary = bootstrap.generate_summary()
    print(summary)

    # Append cortex recall after the main summary
    recall = cortex_recall()
    if recall:
        print("")
        print(recall)

    # Append Obsidian vault context
    vault = obsidian_context()
    if vault:
        print("")
        print(vault)


if __name__ == "__main__":
    main()
