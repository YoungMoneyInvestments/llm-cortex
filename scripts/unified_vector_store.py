#!/usr/bin/env python3
"""
Unified Vector Store — SQLite FTS5 + optional sqlite_vec (Layer 0)

Provides fast full-text search (FTS5, zero-cost) and optional vector
similarity search (sqlite_vec + OpenAI embeddings) across all memory.

Collections:
  - observations: Tool use observations from hooks
  - conversations: Session transcripts and summaries
  - knowledge: Knowledge graph entities and relationships

Configure:
    CORTEX_WORKSPACE  — Project root (default: ~/cortex)
    CORTEX_ENV_FILE   — Optional env file containing OPENAI_API_KEY
    OPENAI_API_KEY    — Required only for vector similarity search

Dependencies:
    Required: (none — uses stdlib sqlite3)
    Optional: pip install sqlite-vec openai
"""

import json
import logging
import os
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cortex-vectors")

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
DB_PATH = WORKSPACE / "data" / "cortex-vectors.db"

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# ── Singleton ───────────────────────────────────────────────────────────────

_store_instance: Optional["UnifiedVectorStore"] = None


def get_vector_store(db_path: Optional[Path] = None) -> "UnifiedVectorStore":
    """Get or create the singleton vector store instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = UnifiedVectorStore(db_path or DB_PATH)
    return _store_instance


# ── Helpers ─────────────────────────────────────────────────────────────────


def _load_openai_key() -> Optional[str]:
    """Load OpenAI API key from env or an optional env file."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_file_value = os.environ.get("CORTEX_ENV_FILE", "").strip()
    if not env_file_value:
        return None
    env_file = Path(env_file_value).expanduser()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip('"').strip("'")
    return None


def _float_list_to_blob(floats: list) -> bytes:
    """Pack list of floats to binary blob for sqlite_vec."""
    return struct.pack(f"{len(floats)}f", *floats)


def _blob_to_float_list(blob: bytes) -> list:
    """Unpack binary blob to list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ── Main class ──────────────────────────────────────────────────────────────


class UnifiedVectorStore:
    """Unified search store using SQLite FTS5 + optional sqlite_vec."""

    VALID_COLLECTIONS = ("observations", "conversations", "knowledge")

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

        # Try loading sqlite_vec for vector similarity search
        self.vec_available = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self.vec_available = True
        except (ImportError, Exception) as e:
            logger.info(f"sqlite_vec not available, vector search disabled: {e}")

        # OpenAI client (lazy init)
        self._openai_client = None

        self._init_schema()
        logger.info(
            f"Vector store initialized at {db_path} "
            f"(vec_search={'enabled' if self.vec_available else 'disabled'})"
        )

    def _init_schema(self):
        """Create tables for full-text search and vector embeddings."""
        self.conn.executescript("""
            -- Main document store
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                has_embedding INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_docs_collection ON documents(collection);
            CREATE INDEX IF NOT EXISTS idx_docs_created ON documents(created_at);

            -- FTS5 full-text search index
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                id UNINDEXED,
                collection UNINDEXED,
                text,
                content=documents,
                content_rowid=rowid
            );

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, id, collection, text)
                VALUES (new.rowid, new.id, new.collection, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, id, collection, text)
                VALUES ('delete', old.rowid, old.id, old.collection, old.text);
            END;

            CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, id, collection, text)
                VALUES ('delete', old.rowid, old.id, old.collection, old.text);
                INSERT INTO documents_fts(rowid, id, collection, text)
                VALUES (new.rowid, new.id, new.collection, new.text);
            END;
        """)

        # Create vector table if sqlite_vec is available
        if self.vec_available:
            try:
                self.conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS document_embeddings
                    USING vec0(
                        doc_id TEXT PRIMARY KEY,
                        embedding float[{EMBEDDING_DIM}]
                    )
                """)
            except Exception as e:
                logger.warning(f"Failed to create vector table: {e}")
                self.vec_available = False

        self.conn.commit()

    def _get_openai(self):
        """Lazy-init OpenAI client."""
        if self._openai_client is None:
            key = _load_openai_key()
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY not set. Vector search requires an API key. "
                    "Set it in env or in $CORTEX_WORKSPACE/.env"
                )
            from openai import OpenAI
            self._openai_client = OpenAI(api_key=key)
        return self._openai_client

    def _get_embedding(self, text: str) -> list:
        """Generate embedding for text using OpenAI."""
        client = self._get_openai()
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000],
        )
        return response.data[0].embedding

    # ── Add documents ───────────────────────────────────────────────────────

    def add_observation(
        self,
        obs_id: str,
        text: str,
        metadata: Optional[dict] = None,
    ):
        """Add a processed observation."""
        self._upsert(f"obs-{obs_id}", "observations", text, metadata)

    def add_conversation(
        self,
        conv_id: str,
        text: str,
        metadata: Optional[dict] = None,
    ):
        """Add a conversation excerpt or summary."""
        self._upsert(f"conv-{conv_id}", "conversations", text, metadata)

    def add_knowledge(
        self,
        knowledge_id: str,
        text: str,
        metadata: Optional[dict] = None,
    ):
        """Add a knowledge graph entity or relationship."""
        self._upsert(f"kg-{knowledge_id}", "knowledge", text, metadata)

    def _upsert(self, doc_id: str, collection: str, text: str, metadata: Optional[dict]):
        """Insert or update a document."""
        meta_json = json.dumps(metadata or {})
        self.conn.execute(
            "INSERT INTO documents (id, collection, text, metadata) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, metadata=excluded.metadata",
            (doc_id, collection, text, meta_json),
        )
        self.conn.commit()

    def add_batch(
        self,
        collection: str,
        ids: list,
        texts: list,
        metadatas: Optional[list] = None,
    ):
        """Batch insert documents."""
        prefix = {"observations": "obs-", "conversations": "conv-", "knowledge": "kg-"}.get(
            collection, ""
        )
        rows = []
        for i, (doc_id, text) in enumerate(zip(ids, texts)):
            meta = json.dumps(metadatas[i] if metadatas and i < len(metadatas) else {})
            rows.append((f"{prefix}{doc_id}", collection, text, meta))

        self.conn.executemany(
            "INSERT INTO documents (id, collection, text, metadata) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, metadata=excluded.metadata",
            rows,
        )
        self.conn.commit()

    # ── Embedding management ────────────────────────────────────────────────

    def embed_document(self, doc_id: str):
        """Generate and store embedding for a document. Requires OpenAI API key."""
        if not self.vec_available:
            return

        row = self.conn.execute(
            "SELECT text FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            return

        embedding = self._get_embedding(row["text"])
        blob = _float_list_to_blob(embedding)

        self.conn.execute(
            "INSERT INTO document_embeddings (doc_id, embedding) VALUES (?, ?) "
            "ON CONFLICT(doc_id) DO UPDATE SET embedding=excluded.embedding",
            (doc_id, blob),
        )
        self.conn.execute(
            "UPDATE documents SET has_embedding = 1 WHERE id = ?", (doc_id,)
        )
        self.conn.commit()

    def embed_pending(self, limit: int = 50):
        """Embed documents that don't have embeddings yet."""
        if not self.vec_available:
            return 0

        rows = self.conn.execute(
            "SELECT id, text FROM documents WHERE has_embedding = 0 LIMIT ?",
            (limit,),
        ).fetchall()

        count = 0
        for row in rows:
            try:
                self.embed_document(row["id"])
                count += 1
            except Exception as e:
                logger.warning(f"Failed to embed {row['id']}: {e}")

        return count

    # ── Search ──────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        where: Optional[dict] = None,
    ) -> list:
        """Full-text search using FTS5. Fast, zero-cost, no API needed."""
        fts_query = self._build_fts_query(query)

        if collection:
            rows = self.conn.execute(
                "SELECT d.id, d.text, d.metadata, d.collection, d.created_at, "
                "rank AS score "
                "FROM documents_fts fts "
                "JOIN documents d ON d.rowid = fts.rowid "
                "WHERE documents_fts MATCH ? AND d.collection = ? "
                "ORDER BY rank "
                "LIMIT ?",
                (fts_query, collection, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT d.id, d.text, d.metadata, d.collection, d.created_at, "
                "rank AS score "
                "FROM documents_fts fts "
                "JOIN documents d ON d.rowid = fts.rowid "
                "WHERE documents_fts MATCH ? "
                "ORDER BY rank "
                "LIMIT ?",
                (fts_query, limit),
            ).fetchall()

        return [
            {
                "id": r["id"],
                "text": r["text"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "collection": r["collection"],
                "created_at": r["created_at"],
                "score": r["score"],
            }
            for r in rows
        ]

    def vector_search(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
    ) -> list:
        """Vector similarity search using sqlite_vec + OpenAI embeddings.
        Falls back to FTS if unavailable."""
        if not self.vec_available:
            logger.info("Vector search unavailable, falling back to FTS")
            return self.search(query, collection=collection, limit=limit)

        try:
            query_embedding = self._get_embedding(query)
            query_blob = _float_list_to_blob(query_embedding)

            if collection:
                rows = self.conn.execute(
                    "SELECT e.doc_id, e.distance, d.text, d.metadata, d.collection, d.created_at "
                    "FROM document_embeddings e "
                    "JOIN documents d ON d.id = e.doc_id "
                    "WHERE d.collection = ? "
                    "AND e.embedding MATCH ? "
                    "AND k = ? "
                    "ORDER BY e.distance",
                    (collection, query_blob, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT e.doc_id, e.distance, d.text, d.metadata, d.collection, d.created_at "
                    "FROM document_embeddings e "
                    "JOIN documents d ON d.id = e.doc_id "
                    "WHERE e.embedding MATCH ? "
                    "AND k = ? "
                    "ORDER BY e.distance",
                    (query_blob, limit),
                ).fetchall()

            return [
                {
                    "id": r["doc_id"],
                    "text": r["text"],
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                    "collection": r["collection"],
                    "created_at": r["created_at"],
                    "distance": r["distance"],
                }
                for r in rows
            ]

        except Exception as e:
            logger.warning(f"Vector search failed, falling back to FTS: {e}")
            return self.search(query, collection=collection, limit=limit)

    def search_all(
        self,
        query: str,
        limit_per_collection: int = 5,
    ) -> dict:
        """Search across all collections. Returns results grouped by collection."""
        results = {}
        for name in self.VALID_COLLECTIONS:
            results[name] = self.search(
                query, collection=name, limit=limit_per_collection
            )
        return results

    def _build_fts_query(self, query: str) -> str:
        """Build FTS5 query from natural language."""
        clean = "".join(c for c in query if c.isalnum() or c in " _-")
        terms = clean.split()
        if not terms:
            return '""'
        return " OR ".join(f'"{t}"' for t in terms if t)

    # ── Stats ───────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Get store statistics."""
        result = {"total": 0, "collections": {}, "with_embeddings": 0}

        for coll in self.VALID_COLLECTIONS:
            count = self.conn.execute(
                "SELECT COUNT(*) as c FROM documents WHERE collection = ?",
                (coll,),
            ).fetchone()["c"]
            result["collections"][coll] = count
            result["total"] += count

        result["with_embeddings"] = self.conn.execute(
            "SELECT COUNT(*) as c FROM documents WHERE has_embedding = 1"
        ).fetchone()["c"]

        return result

    # ── Management ──────────────────────────────────────────────────────────

    def get_by_id(self, doc_id: str) -> Optional[dict]:
        """Get a single document by ID."""
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "text": row["text"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "collection": row["collection"],
            "created_at": row["created_at"],
        }

    def delete(self, doc_ids: list):
        """Delete documents by ID."""
        placeholders = ",".join("?" for _ in doc_ids)
        self.conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", doc_ids)
        if self.vec_available:
            self.conn.execute(
                f"DELETE FROM document_embeddings WHERE doc_id IN ({placeholders})",
                doc_ids,
            )
        self.conn.commit()

    def recent(self, collection: Optional[str] = None, limit: int = 20) -> list:
        """Get most recent documents."""
        if collection:
            rows = self.conn.execute(
                "SELECT * FROM documents WHERE collection = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (collection, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM documents ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [
            {
                "id": r["id"],
                "text": r["text"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "collection": r["collection"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def close(self):
        """Close database connection."""
        self.conn.close()


# ── CLI for testing ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    store = get_vector_store()
    print(f"Vector store at: {store.db_path}")
    print(f"Vec search: {'enabled' if store.vec_available else 'disabled'}")
    print(f"Stats: {json.dumps(store.stats(), indent=2)}")

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print(f"\nSearching for: '{query}'")
        results = store.search_all(query, limit_per_collection=5)
        for coll_name, items in results.items():
            if items:
                print(f"\n--- {coll_name} ({len(items)} results) ---")
                for item in items:
                    score = item.get("score", item.get("distance", "?"))
                    print(f"  [{item['id']}] score={score}: {item['text'][:100]}")
