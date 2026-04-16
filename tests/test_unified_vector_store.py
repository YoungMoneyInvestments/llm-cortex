import tempfile
import unittest
from pathlib import Path

from src.unified_vector_store import UnifiedVectorStore, get_vector_store, _store_instances


class UnifiedVectorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate the singleton registry so tests don't pollute each other.
        _store_instances.clear()

    def tearDown(self) -> None:
        # Close any stores opened via get_vector_store during the test.
        for store in list(_store_instances.values()):
            store.close()
        _store_instances.clear()

    def test_get_vector_store_keyed_by_path_returns_distinct_instances(self) -> None:
        """BUG-A5-05: get_vector_store(pathA) and get_vector_store(pathB) must return
        two distinct instances, each opening its own database."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path_a = (Path(temp_dir) / "a.db").resolve()
            path_b = (Path(temp_dir) / "b.db").resolve()

            store_a = get_vector_store(path_a)
            store_b = get_vector_store(path_b)

            # Must be different objects
            self.assertIsNot(store_a, store_b)

            # Each must track its own path
            self.assertEqual(store_a.db_path, path_a)
            self.assertEqual(store_b.db_path, path_b)

    def test_get_vector_store_same_path_returns_same_instance(self) -> None:
        """Calling get_vector_store twice with the same path returns the same object."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path_a = (Path(temp_dir) / "shared.db").resolve()

            store_first = get_vector_store(path_a)
            store_second = get_vector_store(path_a)

            self.assertIs(store_first, store_second)

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
