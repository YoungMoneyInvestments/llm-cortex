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

    def test_delete_document_and_chunks_removes_unchunked(self) -> None:
        """delete_document_and_chunks on an unchunked doc deletes it by exact id."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "del_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            store.add_knowledge("doc-simple", "short text no chunking", {})
            # Stored as kg-doc-simple
            row = store.conn.execute(
                "SELECT id FROM documents WHERE id = 'kg-doc-simple'"
            ).fetchone()
            self.assertIsNotNone(row)

            n_deleted = store.delete_document_and_chunks("kg-doc-simple")
            self.assertEqual(n_deleted, 1)

            row_after = store.conn.execute(
                "SELECT id FROM documents WHERE id = 'kg-doc-simple'"
            ).fetchone()
            self.assertIsNone(row_after)

    def test_delete_document_and_chunks_removes_all_chunk_suffixes(self) -> None:
        """delete_document_and_chunks removes base doc and all -chunk-N rows."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "del_chunks_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            # Manually insert a doc with multiple chunk rows to simulate what _upsert
            # produces for a large file
            base_id = "kg-strategy-kb--test--big-file-md"
            store.conn.execute(
                "INSERT INTO documents (id, collection, text, metadata) VALUES (?, 'knowledge', 'chunk0', '{}')",
                (base_id + "-chunk-0",),
            )
            store.conn.execute(
                "INSERT INTO documents (id, collection, text, metadata) VALUES (?, 'knowledge', 'chunk1', '{}')",
                (base_id + "-chunk-1",),
            )
            store.conn.execute(
                "INSERT INTO documents (id, collection, text, metadata) VALUES (?, 'knowledge', 'chunk2', '{}')",
                (base_id + "-chunk-2",),
            )
            store.conn.commit()

            rows_before = store.conn.execute(
                "SELECT COUNT(*) FROM documents WHERE id LIKE ?", (base_id + "%",)
            ).fetchone()[0]
            self.assertEqual(rows_before, 3)

            n_deleted = store.delete_document_and_chunks(base_id)
            self.assertEqual(n_deleted, 3)

            rows_after = store.conn.execute(
                "SELECT COUNT(*) FROM documents WHERE id LIKE ?", (base_id + "%",)
            ).fetchone()[0]
            self.assertEqual(rows_after, 0)

    def test_delete_document_and_chunks_does_not_touch_sibling_prefix(self) -> None:
        """delete_document_and_chunks('kg-kb-content_posts-1') must NOT delete
        'kg-kb-content_posts-10' -- sibling IDs that share a common prefix."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prefix_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            # Simulate two KB posts: pk=1 and pk=10
            store.conn.execute(
                "INSERT INTO documents (id, collection, text, metadata) "
                "VALUES ('kg-kb-content_posts-1', 'knowledge', 'post one', '{}')"
            )
            store.conn.execute(
                "INSERT INTO documents (id, collection, text, metadata) "
                "VALUES ('kg-kb-content_posts-10', 'knowledge', 'post ten', '{}')"
            )
            store.conn.commit()

            # Delete only the pk=1 doc
            n_deleted = store.delete_document_and_chunks("kg-kb-content_posts-1")
            self.assertEqual(n_deleted, 1)

            # pk=10 must still be present
            surviving = store.conn.execute(
                "SELECT id FROM documents WHERE id = 'kg-kb-content_posts-10'"
            ).fetchone()
            self.assertIsNotNone(surviving)

    def test_delete_document_and_chunks_returns_zero_when_not_found(self) -> None:
        """delete_document_and_chunks returns 0 when the doc does not exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "notfound_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            n_deleted = store.delete_document_and_chunks("kg-does-not-exist")
            self.assertEqual(n_deleted, 0)

    def test_idempotent_replace_via_delete_before_insert(self) -> None:
        """Simulates ingest_strategy_kb replace pattern: delete then re-add produces
        the same final chunk count as the first ingest, regardless of prior chunk count."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "idempotent_test.db"
            store = UnifiedVectorStore(db_path=db_path)
            self.addCleanup(store.close)

            # Insert an initial version with synthetic chunk rows (e.g., 3 chunks)
            base_id = "kg-strategy-kb--test--evolving-md"
            for i in range(3):
                store.conn.execute(
                    "INSERT INTO documents (id, collection, text, metadata, text_hash) "
                    "VALUES (?, 'knowledge', ?, '{}', ?)",
                    (f"{base_id}-chunk-{i}", f"old chunk {i}", f"hash_old_{i}"),
                )
            store.conn.commit()

            count_before = store.conn.execute(
                "SELECT COUNT(*) FROM documents WHERE id LIKE ?", (base_id + "%",)
            ).fetchone()[0]
            self.assertEqual(count_before, 3)

            # Simulate replace: delete old chunks, then add new (shorter) content
            store.delete_document_and_chunks(base_id)
            new_text = "new short content"  # Fits in 1 chunk
            store.add_knowledge("strategy-kb--test--evolving-md", new_text, {})

            # Should have exactly 1 row (the new single-chunk doc), not 4
            count_after = store.conn.execute(
                "SELECT COUNT(*) FROM documents WHERE id LIKE ?", (base_id + "%",)
            ).fetchone()[0]
            self.assertEqual(count_after, 1)

            # Re-running delete+add again should still yield 1 row (true idempotency)
            store.delete_document_and_chunks(base_id)
            store.add_knowledge("strategy-kb--test--evolving-md", new_text, {})

            count_idempotent = store.conn.execute(
                "SELECT COUNT(*) FROM documents WHERE id LIKE ?", (base_id + "%",)
            ).fetchone()[0]
            self.assertEqual(count_idempotent, 1)
