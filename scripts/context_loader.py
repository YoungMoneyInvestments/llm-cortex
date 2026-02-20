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
import os
import re
import sys
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


def main():
    parser = argparse.ArgumentParser(description="Session memory bootstrap")
    parser.add_argument("--hours", type=int, default=48)
    args = parser.parse_args()
    print(MemoryBootstrap(hours=args.hours).generate_summary())


if __name__ == "__main__":
    main()
