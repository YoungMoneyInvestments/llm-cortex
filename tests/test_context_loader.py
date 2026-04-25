import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import context_loader


class ContextLoaderTests(unittest.TestCase):
    def test_obsidian_context_skips_timeouting_vault_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "LLMCortex"
            vault.mkdir()
            (vault / "architecture.md").write_text("architecture\n", encoding="utf-8")

            with mock.patch("context_loader.resolve_vault_folder", return_value=vault):
                with mock.patch("context_loader.read_text_with_timeout", side_effect=RuntimeError("timeout")):
                    self.assertEqual(context_loader.obsidian_context("/Users/me/Projects/llm-cortex"), "")

    def test_obsidian_context_prefers_promoted_session_note_over_stray_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "LLMCortex"
            sessions = vault / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "zzzz.md").write_text("manual stray note\n", encoding="utf-8")
            (sessions / "2026-04-18-120033-sess-1.md").write_text(
                "---\nsource: llm-cortex\nnote_type: promoted-session-summary\n---\n\npromoted session\n",
                encoding="utf-8",
            )

            with mock.patch("context_loader.resolve_vault_folder", return_value=vault):
                content = context_loader.obsidian_context("/Users/me/Projects/llm-cortex")
            self.assertIn("promoted session", content)
            self.assertNotIn("manual stray note", content)


if __name__ == "__main__":
    unittest.main()
