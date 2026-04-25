import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
PROMOTE_SCRIPT = SCRIPTS_DIR / "promote_to_obsidian.py"

from promote_to_obsidian import (
    MANAGED_END,
    MANAGED_START,
    managed_note_content,
    project_focus_score,
    promote_reports,
    promote_sessions,
    read_existing_text,
    render_session_note,
    session_filename,
    tracked_markdown_reports,
    session_matches_project,
    write_text_with_timeout,
)


def create_obs_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                agent TEXT DEFAULT 'main',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                user_prompt TEXT,
                summary TEXT,
                observation_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                key_decisions TEXT,
                entities_mentioned TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute(
            """
            INSERT INTO sessions (id, started_at, user_prompt, observation_count, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "sess-1",
                "2026-04-18 00:00:00",
                "work on llm-cortex obsidian bridge",
                18,
                "summarized",
            ),
        )
        conn.execute(
            """
            INSERT INTO session_summaries (
                session_id, summary, key_decisions, entities_mentioned, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "sess-1",
                "[main] Session with 18 observations. | Files: /Users/me/Projects/llm-cortex/scripts/context_loader.py",
                '["promote curated sessions only"]',
                '["/Users/me/Projects/llm-cortex/scripts/context_loader.py", "LLMCortex"]',
                "2026-04-18 12:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def init_git_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Codex"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "codex@example.com"], cwd=repo_dir, check=True, capture_output=True)


def commit_path(repo_dir: Path, pathspec: str, message: str = "test commit") -> None:
    subprocess.run(["git", "add", pathspec], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo_dir, check=True, capture_output=True)


class PromoteToObsidianTests(unittest.TestCase):
    def test_session_matches_project_checks_summary_prompt_and_entities(self) -> None:
        row = {
            "summary": "worked in /Users/me/Projects/llm-cortex",
            "user_prompt": "continue the llm-cortex obsidian bridge",
            "key_decisions": '["keep Cortex as system of record"]',
            "entities_mentioned": '["LLMCortex"]',
        }
        self.assertTrue(session_matches_project(row, ["llm-cortex"]))
        self.assertFalse(session_matches_project(row, ["matrix-lstm"]))

    def test_session_matching_penalizes_mixed_sessions_with_unrelated_decisions(self) -> None:
        row = {
            "summary": "files: /Users/me/Projects/llm-cortex/src/memory_worker.py",
            "user_prompt": "",
            "key_decisions": '["edit /Users/me/Projects/brokerbridge/file.py", "update /Users/me/Projects/brokerbridge/other.py"]',
            "entities_mentioned": '["/Users/me/Projects/llm-cortex/src/memory_worker.py", "/Users/me/Projects/llm-cortex/README.md"]',
        }
        score = project_focus_score(row, ["llm-cortex"])
        self.assertLess(score, 6)
        self.assertFalse(session_matches_project(row, ["llm-cortex"]))

    def test_session_matching_rejects_sibling_repo_name_collision(self) -> None:
        row = {
            "summary": "worked in /Users/me/Projects/llm-cortex-old/scripts/cleanup.py",
            "user_prompt": "continue llm-cortex-old cleanup",
            "key_decisions": '["remove stale files from /Users/me/Projects/llm-cortex-old"]',
            "entities_mentioned": '["/Users/me/Projects/llm-cortex-old/scripts/cleanup.py"]',
        }
        self.assertFalse(session_matches_project(row, ["llm-cortex"]))

    def test_render_session_note_includes_key_sections(self) -> None:
        row = {
            "summary_id": 42,
            "session_id": "sess-1",
            "created_at": "2026-04-18 12:00:00",
            "agent": "main",
            "observation_count": 18,
            "summary": "summary body",
            "user_prompt": "initial prompt",
            "key_decisions": '["decision one"]',
            "entities_mentioned": '["LLMCortex"]',
        }
        content = render_session_note(row, "llm-cortex")
        self.assertIn("project: llm-cortex", content)
        self.assertIn("## Initial Prompt", content)
        self.assertIn("## Key Decisions", content)
        self.assertIn("## Entities Mentioned", content)

    def test_session_filename_includes_second_precision(self) -> None:
        row = {"summary_id": 42, "created_at": "2026-04-18T12:00:33.123456+00:00", "session_id": "sess-1"}
        self.assertEqual(session_filename(row), "2026-04-18-120033123456-00000042-sess-1.md")

    def test_promote_sessions_and_reports_write_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "cortex-observations.db"
            vault = root / "LLMCortex"
            vault.mkdir()
            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)
            reports_dir = project_dir / "reports"
            reports_dir.mkdir()
            (reports_dir / "sample.md").write_text("# Sample\n\nbody\n", encoding="utf-8")
            commit_path(project_dir, "reports/sample.md")
            create_obs_db(db_path)

            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=5,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            session_files = list((vault / "sessions").glob("*.md"))
            self.assertEqual(len(session_files), 1)

            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=5,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 0)

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            report_content = (vault / "research" / "sample.md").read_text(encoding="utf-8")
            self.assertIn("note_type: promoted-report", report_content)

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 0)

    def test_managed_note_content_preserves_manual_section(self) -> None:
        existing = (
            f"{MANAGED_START}\nold generated\n{MANAGED_END}\n\n"
            "## Manual Notes\n\nkeep this\n"
        )
        updated = managed_note_content("new generated", existing_text=existing)
        self.assertIn("new generated", updated)
        self.assertIn("keep this", updated)

    def test_managed_note_content_migrates_legacy_promoted_note_without_dropping_text(self) -> None:
        existing = (
            "---\n"
            "note_type: promoted-report\n"
            "source: llm-cortex\n"
            "---\n\n"
            "# Old Note\n\n"
            "legacy manual text\n"
        )
        updated = managed_note_content("new generated", existing_text=existing)
        self.assertIn("new generated", updated)
        self.assertIn("legacy manual text", updated)
        self.assertIn("## Legacy Note Snapshot", updated)
        self.assertIn("## Manual Notes", updated)

    def test_managed_note_content_refuses_unmanaged_existing_note(self) -> None:
        with self.assertRaises(ValueError):
            managed_note_content("generated", existing_text="# hand-written note\n")

    def test_read_existing_text_times_out_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note_path = Path(temp_dir) / "note.md"
            note_path.write_text("content\n", encoding="utf-8")
            with mock.patch(
                "promote_to_obsidian.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["/bin/cat"], timeout=5),
            ):
                with self.assertRaises(RuntimeError):
                    read_existing_text(note_path, timeout_seconds=5)

    def test_write_text_with_timeout_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note_path = Path(temp_dir) / "note.md"
            with mock.patch(
                "promote_to_obsidian.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=[sys.executable], timeout=5),
            ):
                with self.assertRaises(RuntimeError):
                    write_text_with_timeout(note_path, "content\n", timeout_seconds=5)

    def test_write_text_with_timeout_preserves_existing_file_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note_path = Path(temp_dir) / "note.md"
            note_path.write_text("old content\n", encoding="utf-8")
            with mock.patch(
                "promote_to_obsidian.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=[sys.executable], timeout=5),
            ):
                with self.assertRaises(RuntimeError):
                    write_text_with_timeout(note_path, "new content\n", timeout_seconds=5)
            self.assertEqual(note_path.read_text(encoding="utf-8"), "old content\n")

    def test_write_text_with_timeout_cleans_up_temp_file_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note_path = Path(temp_dir) / "note.md"
            with mock.patch(
                "promote_to_obsidian.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(args=[sys.executable], returncode=0, stdout="", stderr=""),
                    subprocess.TimeoutExpired(cmd=[sys.executable], timeout=5),
                ],
            ):
                with self.assertRaises(RuntimeError):
                    write_text_with_timeout(note_path, "new content\n", timeout_seconds=5)
            leftovers = [path for path in Path(temp_dir).iterdir() if path.name.startswith(f".{note_path.name}.")]
            self.assertEqual(leftovers, [])

    def test_timezone_aware_session_timestamp_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "cortex-observations.db"
            vault = root / "LLMCortex"
            vault.mkdir()
            create_obs_db(db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("UPDATE session_summaries SET created_at = ?", ("2026-04-18T12:00:00+00:00",))
                conn.commit()
            finally:
                conn.close()

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=5,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)

    def test_tracked_markdown_reports_excludes_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            init_git_repo(root)
            reports_dir = root / "reports"
            reports_dir.mkdir()
            tracked = reports_dir / "tracked.md"
            tracked.write_text("tracked\n", encoding="utf-8")
            untracked = reports_dir / "untracked.md"
            untracked.write_text("untracked\n", encoding="utf-8")
            commit_path(root, "reports/tracked.md")
            paths = tracked_markdown_reports(root, reports_dir)
            self.assertEqual(paths, [tracked])

    def test_tracked_markdown_reports_keeps_head_files_even_if_deleted_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            init_git_repo(root)
            reports_dir = root / "reports"
            reports_dir.mkdir()
            committed = reports_dir / "kept.md"
            committed.write_text("committed\n", encoding="utf-8")
            commit_path(root, "reports/kept.md")
            committed.unlink()

            paths = tracked_markdown_reports(root, reports_dir)
            self.assertEqual(paths, [committed])

    def test_promote_reports_uses_committed_content_and_nested_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            vault.mkdir()
            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)

            reports_dir = project_dir / "reports"
            nested_dir = reports_dir / "nested"
            nested_dir.mkdir(parents=True)
            committed = nested_dir / "sample.md"
            committed.write_text("# Committed\n\nbody v1\n", encoding="utf-8")
            commit_path(project_dir, "reports/nested/sample.md")

            committed.write_text("# Dirty\n\nbody v2\n", encoding="utf-8")
            staged_only = reports_dir / "staged-only.md"
            staged_only.write_text("# Staged\n\nnot committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "reports/staged-only.md"], cwd=project_dir, check=True, capture_output=True)

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )

            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            promoted = vault / "research" / "nested" / "sample.md"
            self.assertTrue(promoted.exists())
            promoted_text = promoted.read_text(encoding="utf-8")
            self.assertIn("body v1", promoted_text)
            self.assertNotIn("body v2", promoted_text)
            self.assertFalse((vault / "research" / "staged-only.md").exists())

    def test_promote_reports_handles_locally_deleted_committed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            vault.mkdir()
            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)

            reports_dir = project_dir / "reports"
            reports_dir.mkdir(parents=True)
            committed = reports_dir / "kept.md"
            committed.write_text("# Committed\n\nbody v1\n", encoding="utf-8")
            commit_path(project_dir, "reports/kept.md")
            committed.unlink()

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )

            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            promoted = vault / "research" / "kept.md"
            self.assertTrue(promoted.exists())
            self.assertIn("body v1", promoted.read_text(encoding="utf-8"))

    def test_promote_reports_prunes_stale_managed_notes_removed_from_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            vault.mkdir()
            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)

            reports_dir = project_dir / "reports"
            reports_dir.mkdir(parents=True)
            report_path = reports_dir / "dead.md"
            report_path.write_text("# Dead\n\nbody\n", encoding="utf-8")
            commit_path(project_dir, "reports/dead.md")

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            promoted = vault / "research" / "dead.md"
            self.assertTrue(promoted.exists())

            subprocess.run(["git", "rm", "reports/dead.md"], cwd=project_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "remove dead report"], cwd=project_dir, check=True, capture_output=True)

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )
            self.assertEqual(matched, 0)
            self.assertEqual(changed, 1)
            self.assertFalse(promoted.exists())

    def test_promote_reports_prunes_stale_legacy_note_removed_from_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            (vault / "research" / "_nested").mkdir(parents=True)
            legacy = vault / "research" / "_nested" / "dead.md"
            legacy.write_text(
                "---\nsource: llm-cortex\nnote_type: promoted-report\n---\n\n# Old note\n",
                encoding="utf-8",
            )

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)
            (project_dir / "README.md").write_text("root\n", encoding="utf-8")
            commit_path(project_dir, "README.md")
            reports_dir = project_dir / "reports"
            reports_dir.mkdir()

            matched, changed = promote_reports(
                vault_folder=vault,
                project_dir=project_dir,
                reports_dir=reports_dir,
                dry_run=False,
            )

            self.assertEqual(matched, 0)
            self.assertEqual(changed, 1)
            self.assertFalse(legacy.exists())

    def test_promote_reports_refuses_to_prune_when_report_enumeration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            (vault / "research").mkdir(parents=True)
            promoted = vault / "research" / "kept.md"
            promoted.write_text(
                f"{MANAGED_START}\n---\nsource: llm-cortex\nnote_type: promoted-report\n---\n{MANAGED_END}\n",
                encoding="utf-8",
            )

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            reports_dir = project_dir / "reports"
            reports_dir.mkdir()

            with self.assertRaises(RuntimeError):
                promote_reports(
                    vault_folder=vault,
                    project_dir=project_dir,
                    reports_dir=reports_dir,
                    dry_run=False,
                )
            self.assertTrue(promoted.exists())

    def test_promote_reports_refuses_out_of_tree_reports_dir_without_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            (vault / "research").mkdir(parents=True)
            promoted = vault / "research" / "kept.md"
            promoted.write_text(
                f"{MANAGED_START}\n---\nsource: llm-cortex\nnote_type: promoted-report\n---\n{MANAGED_END}\n",
                encoding="utf-8",
            )

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)
            (project_dir / "README.md").write_text("root\n", encoding="utf-8")
            commit_path(project_dir, "README.md")
            reports_dir = root / "elsewhere" / "reports"
            reports_dir.mkdir(parents=True)

            with self.assertRaises(RuntimeError):
                promote_reports(
                    vault_folder=vault,
                    project_dir=project_dir,
                    reports_dir=reports_dir,
                    dry_run=False,
                )
            self.assertTrue(promoted.exists())

    def test_promote_reports_refuses_missing_in_tree_reports_dir_without_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            (vault / "research").mkdir(parents=True)
            promoted = vault / "research" / "kept.md"
            promoted.write_text(
                f"{MANAGED_START}\n---\nsource: llm-cortex\nnote_type: promoted-report\n---\n{MANAGED_END}\n",
                encoding="utf-8",
            )

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)
            (project_dir / "README.md").write_text("root\n", encoding="utf-8")
            commit_path(project_dir, "README.md")
            reports_dir = project_dir / "reports-typo"

            with self.assertRaises(RuntimeError):
                promote_reports(
                    vault_folder=vault,
                    project_dir=project_dir,
                    reports_dir=reports_dir,
                    dry_run=False,
                )
            self.assertTrue(promoted.exists())

    def test_promote_reports_refuses_missing_nested_reports_override_without_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "LLMCortex"
            (vault / "research").mkdir(parents=True)
            promoted = vault / "research" / "kept.md"
            promoted.write_text(
                f"{MANAGED_START}\n---\nsource: llm-cortex\nnote_type: promoted-report\n---\n{MANAGED_END}\n",
                encoding="utf-8",
            )

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)
            init_git_repo(project_dir)
            (project_dir / "README.md").write_text("root\n", encoding="utf-8")
            commit_path(project_dir, "README.md")
            reports_dir = project_dir / "typo-parent" / "reports"

            with self.assertRaises(RuntimeError):
                promote_reports(
                    vault_folder=vault,
                    project_dir=project_dir,
                    reports_dir=reports_dir,
                    dry_run=False,
                )
            self.assertTrue(promoted.exists())

    def test_promote_sessions_prunes_notes_outside_new_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "cortex-observations.db"
            vault = root / "LLMCortex"
            vault.mkdir()
            create_obs_db(db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (id, started_at, user_prompt, observation_count, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("sess-2", "2026-04-18 00:30:00", "continue llm-cortex bridge", 20, "summarized"),
                )
                conn.execute(
                    """
                    INSERT INTO session_summaries (
                        session_id, summary, key_decisions, entities_mentioned, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "sess-2",
                        "[main] Session with 20 observations. | Files: /Users/me/Projects/llm-cortex/scripts/promote_to_obsidian.py",
                        '["keep vault sync selective"]',
                        '["/Users/me/Projects/llm-cortex/scripts/promote_to_obsidian.py", "LLMCortex"]',
                        "2026-04-18 13:00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)

            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=2,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )
            self.assertEqual(matched, 2)
            self.assertEqual(changed, 2)
            self.assertEqual(len(list((vault / "sessions").glob("*.md"))), 2)

            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=1,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )
            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            remaining = list((vault / "sessions").glob("*.md"))
            self.assertEqual(len(remaining), 1)
            self.assertIn("sess-2"[:8], remaining[0].name)

    def test_promote_sessions_sorts_by_parsed_created_at_not_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "cortex-observations.db"
            vault = root / "LLMCortex"
            vault.mkdir()
            create_obs_db(db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (id, started_at, user_prompt, observation_count, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("sess-2", "2026-04-18 00:10:00", "continue llm-cortex bridge", 20, "summarized"),
                )
                conn.execute(
                    """
                    INSERT INTO session_summaries (
                        session_id, summary, key_decisions, entities_mentioned, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "sess-2",
                        "[main] Session with 20 observations. | Files: /Users/me/Projects/llm-cortex/scripts/context_loader.py",
                        '["keep vault sync selective"]',
                        '["/Users/me/Projects/llm-cortex/scripts/context_loader.py", "LLMCortex"]',
                        "2026-04-18T01:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)

            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=1,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )

            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            remaining = list((vault / "sessions").glob("*.md"))
            self.assertEqual(len(remaining), 1)
            self.assertIn("sess-1"[:8], remaining[0].name)

    def test_promote_sessions_breaks_same_second_ties_by_summary_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "cortex-observations.db"
            vault = root / "LLMCortex"
            vault.mkdir()
            create_obs_db(db_path)

            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (id, started_at, user_prompt, observation_count, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("sess-b", "2026-04-18 00:10:00", "continue llm-cortex bridge", 20, "summarized"),
                )
                conn.execute(
                    """
                    INSERT INTO session_summaries (
                        session_id, summary, key_decisions, entities_mentioned, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "sess-b",
                        "[main] Session with 20 observations. | Files: /Users/me/Projects/llm-cortex/scripts/context_loader.py",
                        '["keep vault sync selective"]',
                        '["/Users/me/Projects/llm-cortex/scripts/context_loader.py", "LLMCortex"]',
                        "2026-04-18 12:00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            project_dir = root / "project" / "llm-cortex"
            project_dir.mkdir(parents=True)

            matched, changed = promote_sessions(
                vault_folder=vault,
                project_dir=project_dir,
                db_path=db_path,
                hours=168,
                limit=1,
                min_observations=5,
                extra_markers=[],
                dry_run=False,
            )

            self.assertEqual(matched, 1)
            self.assertEqual(changed, 1)
            remaining = list((vault / "sessions").glob("*.md"))
            self.assertEqual(len(remaining), 1)
            self.assertIn("sess-b", remaining[0].name)

    def test_tracked_markdown_reports_keeps_head_files_when_worktree_copy_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            init_git_repo(root)
            reports_dir = root / "reports"
            reports_dir.mkdir()
            committed = reports_dir / "tracked.md"
            committed.write_text("tracked\n", encoding="utf-8")
            commit_path(root, "reports/tracked.md")
            committed.unlink()

            paths = tracked_markdown_reports(root, reports_dir)
            self.assertEqual(paths, [reports_dir / "tracked.md"])

    def test_skip_sessions_does_not_require_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "llm-cortex"
            project_dir.mkdir()
            init_git_repo(project_dir)
            reports_dir = project_dir / "reports"
            reports_dir.mkdir()
            tracked = reports_dir / "tracked.md"
            tracked.write_text("tracked\n", encoding="utf-8")
            commit_path(project_dir, "reports/tracked.md")

            vault_root = root / "vault"
            (vault_root / "LLMCortex").mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROMOTE_SCRIPT),
                    "--project-dir",
                    str(project_dir),
                    "--vault-dir",
                    str(vault_root),
                    "--skip-sessions",
                    "--db-path",
                    str(root / "missing.db"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("report_notes: matched=1 changed=1", result.stdout)

    def test_nested_project_dir_resolves_to_repo_root_for_default_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "llm-cortex"
            project_dir.mkdir()
            init_git_repo(project_dir)
            (project_dir / "src").mkdir()
            reports_dir = project_dir / "reports"
            reports_dir.mkdir()
            (reports_dir / "tracked.md").write_text("tracked\n", encoding="utf-8")
            commit_path(project_dir, "reports/tracked.md")

            vault_root = root / "vault"
            (vault_root / "LLMCortex").mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROMOTE_SCRIPT),
                    "--project-dir",
                    str(project_dir / "src"),
                    "--vault-dir",
                    str(vault_root),
                    "--skip-sessions",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("report_notes: matched=1 changed=1", result.stdout)

    def test_relative_reports_dir_override_is_resolved_from_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "llm-cortex"
            project_dir.mkdir()
            init_git_repo(project_dir)
            nested_reports = project_dir / "reports" / "nested"
            nested_reports.mkdir(parents=True)
            (nested_reports / "tracked.md").write_text("tracked\n", encoding="utf-8")
            commit_path(project_dir, "reports/nested/tracked.md")

            vault_root = root / "vault"
            (vault_root / "LLMCortex").mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROMOTE_SCRIPT),
                    "--project-dir",
                    str(project_dir),
                    "--vault-dir",
                    str(vault_root),
                    "--reports-dir",
                    "reports/nested",
                    "--skip-sessions",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("report_notes: matched=1 changed=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
