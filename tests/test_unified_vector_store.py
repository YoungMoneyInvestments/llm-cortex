import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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

    def test_close_evicts_from_singleton_registry(self) -> None:
        """Pass N Fix 4: close() must remove the instance from _store_instances so
        get_vector_store() returns a fresh connection rather than a dead handle."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "close_test.db"

            store1 = get_vector_store(db_path)
            resolved = db_path.resolve()
            self.assertIn(resolved, _store_instances)

            store1.close()

            # Evicted from registry
            self.assertNotIn(resolved, _store_instances)

            # Subsequent call creates a fresh instance (not the closed one)
            store2 = get_vector_store(db_path)
            self.assertIsNot(store1, store2)
            self.assertIn(resolved, _store_instances)

    def test_add_batch_uses_single_embed_call(self) -> None:
        """Pass N Fix 1: add_batch must call _get_embeddings_batch once for all
        documents, not one embed_document() call per document."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "batch_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            # Patch vec_available so the embedding path is exercised
            store.vec_available = True
            call_count = {"n": 0}

            def fake_batch(texts):
                call_count["n"] += 1
                return [None] * len(texts)  # Return None embeddings (no vec table in test)

            with patch.object(store, "_get_embeddings_batch", side_effect=fake_batch):
                store.add_batch(
                    "observations",
                    ids=["a", "b", "c"],
                    texts=["alpha text", "beta text", "gamma text"],
                )

            # Exactly one batch call regardless of document count
            self.assertEqual(call_count["n"], 1)

    def test_add_batch_deduplicates_identical_texts(self) -> None:
        """add_batch must not insert duplicate documents with the same text content."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "dedup_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            # First batch inserts two distinct observations
            store.add_batch(
                "observations",
                ids=["1", "2"],
                texts=["unique text alpha", "unique text beta"],
            )

            # Second batch tries to insert the same text for a different id
            store.add_batch(
                "observations",
                ids=["3"],
                texts=["unique text alpha"],  # Same content as id=1
            )

            # Only 2 distinct documents should exist (id=3 was a dup of id=1)
            conn = store.conn
            rows = conn.execute(
                "SELECT id FROM documents WHERE collection='observations' ORDER BY id"
            ).fetchall()
            ids = [r["id"] for r in rows]
            self.assertEqual(len(ids), 2)
            self.assertIn("obs-1", ids)
            self.assertIn("obs-2", ids)
            self.assertNotIn("obs-3", ids)
