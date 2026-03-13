import tempfile
import unittest
from pathlib import Path

from src.unified_vector_store import UnifiedVectorStore


class UnifiedVectorStoreTests(unittest.TestCase):
    def test_search_returns_documents_from_requested_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "vectors.db"

            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            store.add_observation("1", "alpha beta gamma", {"source": "tests"})
            store.add_conversation("2", "alpha release checklist", {"source": "tests"})

            observation_results = store.search(
                "alpha",
                collection="observations",
                limit=5,
            )
            grouped_results = store.search_all("alpha", limit_per_collection=5)

            self.assertEqual([result["id"] for result in observation_results], ["obs-1"])
            self.assertEqual(grouped_results["observations"][0]["id"], "obs-1")
            self.assertEqual(grouped_results["conversations"][0]["id"], "conv-2")
