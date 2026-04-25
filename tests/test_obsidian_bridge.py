import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from obsidian_bridge import build_project_markers, resolve_vault_folder, resolve_vault_match


class ObsidianBridgeTests(unittest.TestCase):
    def test_resolve_vault_match_prefers_specific_pattern(self) -> None:
        pattern, vault_name = resolve_vault_match("/Users/me/Projects/llm-cortex")
        self.assertEqual(pattern, "llm-cortex")
        self.assertEqual(vault_name, "LLMCortex")

    def test_resolve_vault_folder_uses_supplied_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "LLMCortex").mkdir()
            folder = resolve_vault_folder("/Users/me/Projects/llm-cortex", vault_root=root)
            self.assertEqual(folder, root / "LLMCortex")

    def test_build_project_markers_returns_conservative_strong_markers(self) -> None:
        markers = build_project_markers("/Users/me/Projects/llm-cortex")
        self.assertIn("llm-cortex", markers)
        self.assertIn("llmcortex", markers)
        self.assertIn("/projects/llm-cortex", markers)
        self.assertIn("/users/me/projects/llm-cortex", markers)
        self.assertNotIn("memory", markers)

    def test_resolve_vault_match_uses_repo_root_not_parent_path_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "openclaw" / "vendor" / "llm-cortex"
            (repo / "src").mkdir(parents=True)
            import subprocess

            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            pattern, vault_name = resolve_vault_match(repo / "src")
            self.assertEqual(pattern, "llm-cortex")
            self.assertEqual(vault_name, "LLMCortex")


if __name__ == "__main__":
    unittest.main()
