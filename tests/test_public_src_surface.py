import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODULES = [
    "src.knowledge_graph",
    "src.unified_vector_store",
    "src.memory_retriever",
    "src.mcp_memory_server",
    "src.memory_worker",
]


class PublicSrcSurfaceImportTests(unittest.TestCase):
    def test_public_modules_import_in_clean_process(self) -> None:
        for module_name in PUBLIC_MODULES:
            with self.subTest(module=module_name), tempfile.TemporaryDirectory() as temp_home:
                env = os.environ.copy()
                env["HOME"] = temp_home
                env["PYTHONPATH"] = str(REPO_ROOT)

                completed = subprocess.run(
                    [sys.executable, "-c", f"import {module_name}"],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=(
                        f"{module_name} failed to import.\n"
                        f"stdout:\n{completed.stdout}\n"
                        f"stderr:\n{completed.stderr}"
                    ),
                )
