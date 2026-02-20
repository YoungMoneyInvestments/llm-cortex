#!/usr/bin/env python3
"""
Working Memory - Active Session State Tracking (Layer 3)

The "mental scratchpad" for AI sessions. Persists to disk, survives
context window compression and restarts.

Usage:
    from working_memory import WorkingMemory
    wm = WorkingMemory("session-main-20260211")
    wm.add_goal("Fix auth bug", priority="high")
    wm.add_scratchpad_note("Found issue in token validation")

Configure:
    Set CORTEX_WORKSPACE to your project root (default: ~/cortex)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
PLANNING_DIR = WORKSPACE / ".planning"
WORKING_MEMORY_DIR = PLANNING_DIR / "working-memory"


class WorkingMemory:
    """Active session state management."""

    def __init__(self, session_key: str):
        self.session_key = session_key
        self.session_dir = WORKING_MEMORY_DIR / f"session-{session_key}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.goals_file = self.session_dir / "active-goals.json"
        self.scratchpad_file = self.session_dir / "scratchpad.md"
        self.state_file = self.session_dir / "state.json"
        self.references_file = self.session_dir / "references.json"

        self.goals = self._load_json(self.goals_file, default=[])
        self.state = self._load_json(self.state_file, default={})
        self.references = self._load_json(self.references_file, default={})

        if "session_created_at" not in self.state:
            self.update_state("session_created_at", datetime.now().isoformat())
        self.verify_time()

    def _load_json(self, filepath: Path, default: Any) -> Any:
        if filepath.exists():
            try:
                with open(filepath) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return default
        return default

    def _save_json(self, filepath: Path, data: Any):
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

    # === GOALS ===

    def add_goal(
        self, goal: str, priority: str = "normal", context: Optional[str] = None
    ):
        """Add active goal. Priority: low, normal, high, urgent."""
        goal_obj = {
            "id": len(self.goals) + 1,
            "goal": goal,
            "priority": priority,
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "context": context,
            "subgoals": [],
        }
        self.goals.append(goal_obj)
        self._save_json(self.goals_file, self.goals)
        return goal_obj["id"]

    def add_subgoal(self, parent_goal_id: int, subgoal: str):
        for goal in self.goals:
            if goal["id"] == parent_goal_id:
                goal["subgoals"].append(
                    {
                        "subgoal": subgoal,
                        "completed": False,
                        "added_at": datetime.now().isoformat(),
                    }
                )
                self._save_json(self.goals_file, self.goals)
                return True
        return False

    def complete_goal(self, goal_id: int):
        for goal in self.goals:
            if goal["id"] == goal_id:
                goal["status"] = "completed"
                goal["completed_at"] = datetime.now().isoformat()
                self._save_json(self.goals_file, self.goals)
                return True
        return False

    def get_active_goals(self) -> List[Dict]:
        return [g for g in self.goals if g["status"] == "active"]

    # === SCRATCHPAD (Mental Notes) ===

    def add_scratchpad_note(self, note: str, category: str = "general"):
        """Timestamped note to the scratchpad."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(self.scratchpad_file, "a") as f:
            f.write(f"[{timestamp}] [{category}] {note}\n")

    def read_scratchpad(self) -> str:
        if self.scratchpad_file.exists():
            return self.scratchpad_file.read_text()
        return ""

    def clear_scratchpad(self):
        if self.scratchpad_file.exists():
            self.scratchpad_file.unlink()

    # === STATE (Key-Value Store) ===

    def update_state(self, key: str, value: Any):
        self.state[key] = {
            "value": value,
            "updated_at": datetime.now().isoformat(),
        }
        self._save_json(self.state_file, self.state)

    def get_state(self, key: str, default: Any = None) -> Any:
        if key in self.state:
            return self.state[key]["value"]
        return default

    def increment_counter(self, key: str, amount: int = 1) -> int:
        current = self.get_state(key, 0)
        new_value = current + amount
        self.update_state(key, new_value)
        return new_value

    # === TIME VERIFICATION ===

    def verify_time(self) -> Dict:
        """Record verified current time. Prevents LLM date guessing."""
        now = datetime.now()
        time_info = {
            "timestamp": now.isoformat(),
            "time_str": now.strftime("%I:%M %p"),
            "date_str": now.strftime("%Y-%m-%d"),
            "day_of_week": now.strftime("%A"),
        }
        self.update_state("last_time_verified", time_info)
        return time_info

    # === REFERENCES (Context Pointers) ===

    def add_reference(self, name: str, value: str, ref_type: str = "path"):
        """Add reference. ref_type: path, url, entity, node_id"""
        self.references[name] = {
            "value": value,
            "type": ref_type,
            "added_at": datetime.now().isoformat(),
        }
        self._save_json(self.references_file, self.references)

    def get_reference(self, name: str) -> Optional[str]:
        if name in self.references:
            return self.references[name]["value"]
        return None

    def list_references(self) -> Dict:
        return self.references

    # === LIFECYCLE ===

    def summarize(self) -> Dict:
        return {
            "session_key": self.session_key,
            "active_goals": len(self.get_active_goals()),
            "total_goals": len(self.goals),
            "state_variables": len(self.state),
            "references": len(self.references),
        }

    def archive(self):
        archive_dir = WORKING_MEMORY_DIR / "archive" / "completed"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = archive_dir / f"{self.session_key}-{timestamp}"
        import shutil

        if self.session_dir.exists():
            shutil.move(str(self.session_dir), str(archive_path))
        return archive_path

    def cleanup(self, archive: bool = True):
        if archive:
            return self.archive()
        import shutil

        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)
