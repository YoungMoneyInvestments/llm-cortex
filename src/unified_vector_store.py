#!/usr/bin/env python3
"""
Unified Vector Store — SQLite FTS5 + sqlite_vec for Cortex memory.

Provides fast full-text search (FTS5, zero-cost) and optional vector
similarity search (sqlite_vec + embeddings) across all memory.

Supports two embedding providers:
  - "local" (default): sentence-transformers/all-MiniLM-L6-v2 (384 dims, free)
  - "openai": text-embedding-3-small (1536 dims, costs API tokens)

Set CORTEX_EMBEDDING_PROVIDER=openai to use OpenAI embeddings.

Replaces the fragmented discord-embeddings.db / imessage-embeddings.db
approach with a single unified store.

Collections:
  - observations: Tool use observations from hooks
  - conversations: Session transcripts and summaries
  - knowledge: Knowledge graph entities and relationships

Usage:
    from unified_vector_store import get_vector_store

    store = get_vector_store()
    store.add_observation("obs-123", "User queried gamma exposure for ES", {...})
    results = store.search("gamma exposure", limit=10)
    results = store.search_hybrid("gamma exposure", limit=10)
"""

import hashlib
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

# -- Config ------------------------------------------------------------------

def _optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def _path_from_env(name: str, default: Path) -> Path:
    value = _optional_env(name)
    return Path(value).expanduser() if value else default


DATA_DIR = _path_from_env("CORTEX_DATA_DIR", Path.home() / ".cortex" / "data")
DB_PATH = DATA_DIR / "cortex-vectors.db"

EMBEDDING_PROVIDER = os.environ.get("CORTEX_EMBEDDING_PROVIDER", "local")

# Dimensions per provider
_PROVIDER_DIMS = {
    "openai": 1536,
    "local": 384,
}
EMBEDDING_DIM = _PROVIDER_DIMS.get(EMBEDDING_PROVIDER, 384)
EMBEDDING_MODEL = "text-embedding-3-small"  # OpenAI model name
LOCAL_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Chunking defaults
DEFAULT_CHUNK_MAX_CHARS = 1000
DEFAULT_CHUNK_OVERLAP = 100

# Hybrid search weights
DEFAULT_FTS_WEIGHT = 0.4
DEFAULT_VEC_WEIGHT = 0.6

# -- Singleton ---------------------------------------------------------------

_store_instance: Optional["UnifiedVectorStore"] = None


def get_vector_store(db_path: Optional[Path] = None) -> "UnifiedVectorStore":
    """Get or create the singleton vector store instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = UnifiedVectorStore(db_path or DB_PATH)
    return _store_instance


# -- Helpers -----------------------------------------------------------------


def _load_openai_key() -> Optional[str]:
    """Load OpenAI API key from env or an optional env file."""
    key = _optional_env("OPENAI_API_KEY")
    if key:
        return key

    env_file_value = _optional_env("CORTEX_ENV_FILE")
    if not env_file_value:
        return None

    env_file = Path(env_file_value).expanduser()
    if not env_file.exists():
        return None

    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        if stripped.startswith("OPENAI_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _float_list_to_blob(floats: list[float]) -> bytes:
    """Pack list of floats to binary blob for sqlite_vec."""
    return struct.pack(f"{len(floats)}f", *floats)


def _blob_to_float_list(blob: bytes) -> list[float]:
    """Unpack binary blob to list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _compute_text_hash(text: str) -> str:
    """Compute MD5 hash of text for deduplication (not security)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# -- Main class --------------------------------------------------------------


class UnifiedVectorStore:
    """Unified search store using SQLite FTS5 + optional sqlite_vec."""

    VALID_COLLECTIONS = ("observations", "conversations", "knowledge")

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = None
        self._connect(db_path)

        # Try loading sqlite_vec
        self.vec_available = False
        self._vec_dim_mismatch = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self.vec_available = True
        except (ImportError, Exception) as e:
            logger.warning(f"sqlite_vec not available, vector search disabled: {e}")

        # Embedding provider setup
        self._openai_client = None
        self._local_model = None  # Lazy-loaded
        self._local_model_load_failed = False

        self._init_schema()
        logger.info(
            f"Vector store initialized at {db_path} "
            f"(vec_search={'enabled' if self.vec_available else 'disabled'}, "
            f"provider={EMBEDDING_PROVIDER}, dim={EMBEDDING_DIM})"
        )

    def _connect(self, db_path: Optional[Path] = None):
        """Create or recreate the database connection."""
        path = db_path or self.db_path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def _ensure_connection(self):
        """Verify the connection is alive; reconnect if needed."""
        try:
            self.conn.execute("SELECT 1")
        except (sqlite3.OperationalError, sqlite3.ProgrammingError):
            logger.warning("Database connection lost, reconnecting...")
            try:
                self._connect()
                # Reload sqlite_vec if it was available before
                if self.vec_available:
                    try:
                        import sqlite_vec
                        self.conn.enable_load_extension(True)
                        sqlite_vec.load(self.conn)
                        self.conn.enable_load_extension(False)
                    except Exception:
                        self.vec_available = False
            except Exception as e:
                raise sqlite3.OperationalError(f"Failed to reconnect to database: {e}")

    def _safe_execute(self, sql: str, params=None, many=False):
        """Execute SQL with retry on transient errors (locked, I/O)."""
        try:
            self._ensure_connection()
            if many:
                return self.conn.executemany(sql, params or [])
            return self.conn.execute(sql, params or ())
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            err_msg = str(e).lower()
            if "locked" in err_msg or "disk i/o" in err_msg or "not connected" in err_msg:
                logger.warning(f"Transient SQLite error, retrying: {e}")
                try:
                    self._connect()
                    if self.vec_available and not self._vec_dim_mismatch:
                        try:
                            import sqlite_vec
                            self.conn.enable_load_extension(True)
                            sqlite_vec.load(self.conn)
                            self.conn.enable_load_extension(False)
                        except Exception:
                            self.vec_available = False
                    if many:
                        return self.conn.executemany(sql, params or [])
                    return self.conn.execute(sql, params or ())
                except Exception as retry_err:
                    raise sqlite3.OperationalError(
                        f"Failed after retry: {retry_err} (original: {e})"
                    )
            raise

    def _init_schema(self):
        """Create tables for full-text search and vector embeddings."""
        try:
            # Core table (without text_hash — added via migration for existing DBs)
            self.conn.executescript("""
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
        except Exception as e:
            logger.error(f"Failed to initialize schema: {e}")
            raise

        # Migrate: add text_hash column if missing (existing DBs)
        try:
            self.conn.execute("SELECT text_hash FROM documents LIMIT 1")
        except sqlite3.OperationalError:
            try:
                self.conn.execute("ALTER TABLE documents ADD COLUMN text_hash TEXT")
                logger.info("Migrated documents table: added text_hash column")
            except Exception as e:
                logger.warning(f"Failed to add text_hash column: {e}")

        # Create index on text_hash (safe now that column exists)
        try:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_text_hash ON documents(text_hash)")
        except Exception:
            pass  # Column may not exist if migration failed

        # Create vector table if sqlite_vec is available
        if self.vec_available:
            try:
                # Check if table already exists with different dimensions
                existing_dim = self._get_vec_table_dim()
                if existing_dim is not None and existing_dim != EMBEDDING_DIM:
                    logger.warning(
                        f"Existing vector table has {existing_dim} dimensions but "
                        f"configured provider '{EMBEDDING_PROVIDER}' uses {EMBEDDING_DIM}. "
                        f"Vector search disabled to avoid dimension mismatch. "
                        f"To fix: delete cortex-vectors.db and re-embed, or change provider."
                    )
                    self.vec_available = False
                    self._vec_dim_mismatch = True
                elif existing_dim is None:
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

        # Backfill metadata table
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS backfill_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
        except Exception as e:
            logger.warning(f"Failed to create backfill_meta table: {e}")

        self.conn.commit()

    def _get_vec_table_dim(self) -> Optional[int]:
        """Check if document_embeddings table exists and return its dimension, or None."""
        try:
            # Check if table exists
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_embeddings'"
            ).fetchone()
            if row is None:
                return None
            # Parse dimension from CREATE statement like: float[384]
            sql = row[0] if isinstance(row, tuple) else row["sql"]
            if sql and "float[" in sql:
                start = sql.index("float[") + 6
                end = sql.index("]", start)
                return int(sql[start:end])
            return None
        except Exception:
            return None

    # -- Embedding providers -------------------------------------------------

    def _get_openai(self):
        """Lazy-init OpenAI client."""
        if self._openai_client is None:
            key = _load_openai_key()
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY not set. OpenAI embeddings require an API key. "
                    "Set OPENAI_API_KEY directly or point CORTEX_ENV_FILE at a file containing it."
                )
            from openai import OpenAI
            self._openai_client = OpenAI(api_key=key)
        return self._openai_client

    def _get_local_model(self):
        """Lazy-load the local sentence-transformers model."""
        if self._local_model is not None:
            return self._local_model
        if self._local_model_load_failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading local embedding model: {LOCAL_EMBEDDING_MODEL}")
            self._local_model = SentenceTransformer(LOCAL_EMBEDDING_MODEL)
            logger.info("Local embedding model loaded successfully")
            return self._local_model
        except ImportError:
            logger.warning(
                "sentence-transformers not installed. Install with: "
                "pip install sentence-transformers. "
                "Falling back to FTS-only search."
            )
            self._local_model_load_failed = True
            return None
        except Exception as e:
            logger.warning(f"Failed to load local embedding model: {e}")
            self._local_model_load_failed = True
            return None

    def _get_embedding(self, text: str) -> Optional[list[float]]:
        """Generate embedding for text using the configured provider."""
        if EMBEDDING_PROVIDER == "openai":
            return self._get_embedding_openai(text)
        else:
            return self._get_embedding_local(text)

    def _get_embedding_openai(self, text: str) -> list[float]:
        """Generate embedding using OpenAI API."""
        client = self._get_openai()
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000],  # Truncate to avoid token limits
        )
        return response.data[0].embedding

    def _get_embedding_local(self, text: str) -> Optional[list[float]]:
        """Generate embedding using local sentence-transformers model."""
        model = self._get_local_model()
        if model is None:
            return None
        # Truncate to reasonable length for local model
        truncated = text[:2000]
        embedding = model.encode(truncated, show_progress_bar=False)
        return embedding.tolist()

    # -- Batch embedding -----------------------------------------------------

    def _get_embeddings_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Generate embeddings for a batch of texts.

        Args:
            texts: List of text strings to embed.
        Returns:
            List of embeddings (or None for failures), same length as texts.
        """
        if EMBEDDING_PROVIDER == "openai":
            return self._get_embeddings_batch_openai(texts)
        else:
            return self._get_embeddings_batch_local(texts)

    def _get_embeddings_batch_openai(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Batch embedding via OpenAI API."""
        try:
            client = self._get_openai()
            truncated = [t[:8000] for t in texts]
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=truncated,
            )
            # Response data is sorted by index
            embeddings: list[Optional[list[float]]] = [None] * len(texts)
            for item in response.data:
                embeddings[item.index] = item.embedding
            return embeddings
        except Exception as e:
            logger.error(f"OpenAI batch embedding failed: {e}")
            return [None] * len(texts)

    def _get_embeddings_batch_local(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Batch embedding via local sentence-transformers model."""
        model = self._get_local_model()
        if model is None:
            return [None] * len(texts)
        try:
            truncated = [t[:2000] for t in texts]
            embeddings = model.encode(
                truncated, batch_size=len(truncated), show_progress_bar=False
            )
            return [e.tolist() for e in embeddings]
        except Exception as e:
            logger.error(f"Local batch embedding failed: {e}")
            return [None] * len(texts)

    # -- Text chunking -------------------------------------------------------

    @staticmethod
    def _chunk_text(
        text: str,
        max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> list[str]:
        """
        Split text into overlapping chunks for better embedding coverage.
        Returns list of chunks. If text <= max_chars, returns [text].
        """
        if len(text) <= max_chars:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + max_chars
            chunk = text[start:end]

            # Try to break at sentence or word boundary
            if end < len(text):
                # Look for last sentence-ending punctuation
                for sep in [". ", ".\n", "! ", "!\n", "? ", "?\n", "\n\n", "\n"]:
                    last_sep = chunk.rfind(sep)
                    if last_sep > max_chars * 0.5:  # Only break if past halfway
                        chunk = chunk[: last_sep + len(sep)]
                        end = start + len(chunk)
                        break
                else:
                    # Fall back to word boundary
                    last_space = chunk.rfind(" ")
                    if last_space > max_chars * 0.5:
                        chunk = chunk[: last_space + 1]
                        end = start + len(chunk)

            chunks.append(chunk.strip())
            start = end - overlap
            if start <= 0 and end >= len(text):
                break

        return [c for c in chunks if c]  # Filter empty chunks

    # -- Add documents -------------------------------------------------------

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
        """Insert or update a document, with chunking for large texts and deduplication."""
        text_hash = _compute_text_hash(text)

        # Dedup check: same hash in same collection = skip (unless same doc_id = update)
        try:
            existing = self._safe_execute(
                "SELECT id FROM documents WHERE text_hash = ? AND collection = ? AND id != ?",
                (text_hash, collection, doc_id),
            ).fetchone()
            if existing:
                logger.debug(
                    f"Skipping duplicate: {doc_id} has same hash as {existing['id']} "
                    f"in collection {collection}"
                )
                return
        except Exception as e:
            logger.warning(f"Dedup check failed, proceeding with insert: {e}")

        chunks = self._chunk_text(text)

        if len(chunks) == 1:
            # Single document, no chunking needed
            meta_json = json.dumps(metadata or {})
            try:
                self._safe_execute(
                    "INSERT INTO documents (id, collection, text, metadata, text_hash) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "text=excluded.text, metadata=excluded.metadata, text_hash=excluded.text_hash",
                    (doc_id, collection, text, meta_json, text_hash),
                )
                self.conn.commit()
            except Exception as e:
                logger.error(f"Failed to upsert document {doc_id}: {e}")
                raise

            # Generate embedding immediately after successful insert
            try:
                self.embed_document(doc_id)
            except Exception as e:
                logger.warning(f"Embedding generation failed for {doc_id}, document stored without embedding: {e}")
        else:
            # Multiple chunks — store each with chunk suffix
            meta = metadata or {}
            meta["_parent_doc_id"] = doc_id
            meta["_total_chunks"] = len(chunks)

            chunk_ids = []
            for i, chunk in enumerate(chunks):
                chunk_id = f"{doc_id}-chunk-{i}"
                chunk_ids.append(chunk_id)
                chunk_meta = {**meta, "_chunk_index": i}
                meta_json = json.dumps(chunk_meta)
                chunk_hash = _compute_text_hash(chunk)
                try:
                    self._safe_execute(
                        "INSERT INTO documents (id, collection, text, metadata, text_hash) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "text=excluded.text, metadata=excluded.metadata, text_hash=excluded.text_hash",
                        (chunk_id, collection, chunk, meta_json, chunk_hash),
                    )
                except Exception as e:
                    logger.error(f"Failed to upsert chunk {chunk_id}: {e}")
                    raise
            self.conn.commit()

            # Generate embeddings for all chunks after successful commit
            for chunk_id in chunk_ids:
                try:
                    self.embed_document(chunk_id)
                except Exception as e:
                    logger.warning(f"Embedding generation failed for chunk {chunk_id}, chunk stored without embedding: {e}")

    def add_batch(
        self,
        collection: str,
        ids: list[str],
        texts: list[str],
        metadatas: Optional[list[dict]] = None,
    ):
        """Batch insert documents."""
        prefix = {"observations": "obs-", "conversations": "conv-", "knowledge": "kg-"}.get(
            collection, ""
        )
        for i, (doc_id, text) in enumerate(zip(ids, texts)):
            meta = metadatas[i] if metadatas and i < len(metadatas) else {}
            full_id = f"{prefix}{doc_id}"
            self._upsert(full_id, collection, text, meta)

    # -- Embedding management ------------------------------------------------

    def embed_document(self, doc_id: str):
        """Generate and store embedding for a document."""
        if not self.vec_available:
            return

        try:
            row = self._safe_execute(
                "SELECT text FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        except Exception as e:
            logger.error(f"Failed to fetch document {doc_id} for embedding: {e}")
            return

        if not row:
            return

        embedding = self._get_embedding(row["text"])
        if embedding is None:
            logger.debug(f"No embedding generated for {doc_id} (provider unavailable)")
            return

        blob = _float_list_to_blob(embedding)

        try:
            # vec0 virtual tables don't support UPSERT; delete-then-insert
            self._safe_execute(
                "DELETE FROM document_embeddings WHERE doc_id = ?", (doc_id,)
            )
            self._safe_execute(
                "INSERT INTO document_embeddings (doc_id, embedding) VALUES (?, ?)",
                (doc_id, blob),
            )
            self._safe_execute(
                "UPDATE documents SET has_embedding = 1 WHERE id = ?", (doc_id,)
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to store embedding for {doc_id}: {e}")

    def embed_pending(self, limit: int = 50):
        """Embed documents that don't have embeddings yet."""
        if not self.vec_available:
            return 0

        try:
            rows = self._safe_execute(
                "SELECT id, text FROM documents WHERE has_embedding = 0 LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception as e:
            logger.error(f"Failed to query pending embeddings: {e}")
            return 0

        count = 0
        for row in rows:
            try:
                self.embed_document(row["id"])
                count += 1
            except Exception as e:
                logger.warning(f"Failed to embed {row['id']}: {e}")

        return count

    # -- Observation sync ----------------------------------------------------

    def sync_from_observations(
        self,
        obs_db_path: Path,
        batch_size: int = 500,
        max_rows: Optional[int] = None,
    ) -> dict:
        """Sync processed observations from cortex-observations.db into the vector store.

        Only imports observations whose doc IDs are not already present. This is
        the prerequisite step before backfill_embeddings() — it populates the
        documents table so embeddings can be generated.

        Args:
            obs_db_path: Path to cortex-observations.db.
            batch_size: Rows to fetch and insert per batch.
            max_rows: Maximum rows to sync (None=all).
        Returns:
            dict with stats: synced, skipped, failed, total_eligible, elapsed_seconds.
        """
        import time as _time

        t0 = _time.monotonic()

        if not obs_db_path.exists():
            raise FileNotFoundError(f"Observations DB not found: {obs_db_path}")

        obs_conn = sqlite3.connect(str(obs_db_path), check_same_thread=False)
        obs_conn.row_factory = sqlite3.Row

        # Count eligible rows (processed with a summary)
        total_eligible = obs_conn.execute(
            "SELECT COUNT(*) as c FROM observations "
            "WHERE status = 'processed' AND summary IS NOT NULL AND summary != ''"
        ).fetchone()["c"]

        # Get IDs already in the vector store to skip them
        existing_ids = set()
        try:
            rows = self._safe_execute(
                "SELECT id FROM documents WHERE collection = 'observations'"
            ).fetchall()
            for r in rows:
                existing_ids.add(r["id"])
        except Exception as e:
            logger.warning(f"Failed to query existing docs, will attempt all: {e}")

        stats = {
            "synced": 0,
            "skipped": 0,
            "failed": 0,
            "total_eligible": total_eligible,
        }

        offset = 0
        while True:
            limit = batch_size
            if max_rows is not None:
                remaining = max_rows - stats["synced"]
                if remaining <= 0:
                    break
                limit = min(batch_size, remaining)

            rows = obs_conn.execute(
                "SELECT id, summary, source, tool_name, agent "
                "FROM observations "
                "WHERE status = 'processed' AND summary IS NOT NULL AND summary != '' "
                "ORDER BY id "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

            if not rows:
                break

            for row in rows:
                doc_id = f"obs-{row['id']}"
                if doc_id in existing_ids:
                    stats["skipped"] += 1
                    continue

                try:
                    meta = {
                        "source": row["source"] or "",
                        "tool_name": row["tool_name"] or "",
                        "agent": row["agent"] or "main",
                    }
                    meta_json = json.dumps(meta)
                    text = row["summary"]
                    text_hash = _compute_text_hash(text)

                    self._safe_execute(
                        "INSERT INTO documents (id, collection, text, metadata, text_hash) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO NOTHING",
                        (doc_id, "observations", text, meta_json, text_hash),
                    )
                    stats["synced"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    if stats["failed"] <= 5:
                        logger.warning(f"Failed to sync obs {row['id']}: {e}")

            # Commit after each batch
            self.conn.commit()

            offset += len(rows)
            total_so_far = stats["synced"] + stats["skipped"] + stats["failed"]

            if total_so_far % 5000 == 0 or total_so_far == total_eligible:
                logger.info(
                    f"Sync progress: {total_so_far}/{total_eligible} "
                    f"(synced={stats['synced']}, skipped={stats['skipped']})"
                )

            if len(rows) < limit:
                break

        obs_conn.close()
        stats["elapsed_seconds"] = round(_time.monotonic() - t0, 2)
        return stats

    # -- Backfill embeddings -------------------------------------------------

    def backfill_embeddings(
        self,
        batch_size: int = 100,
        max_docs: Optional[int] = None,
        callback=None,
    ) -> dict:
        """Batch-embed all documents that don't have embeddings yet.

        Uses batch encoding for efficiency (sentence-transformers processes
        multiple texts in a single forward pass). Commits after each batch
        so progress is saved and the backfill is resumable on interruption.

        Args:
            batch_size: Documents per embedding batch.
            max_docs: Maximum documents to process (None=all).
            callback: Optional callable(processed, total) for progress reporting.
        Returns:
            dict with stats: processed, failed, skipped, elapsed_seconds.
        """
        import time as _time

        if not self.vec_available:
            return {
                "processed": 0,
                "failed": 0,
                "skipped": 0,
                "elapsed_seconds": 0,
                "error": "Vector search not available (sqlite_vec not loaded)",
            }

        t0 = _time.monotonic()

        # Count total unembedded documents
        total_unembedded = self._safe_execute(
            "SELECT COUNT(*) as c FROM documents WHERE has_embedding = 0"
        ).fetchone()["c"]

        if max_docs is not None:
            total_to_process = min(total_unembedded, max_docs)
        else:
            total_to_process = total_unembedded

        stats = {
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "total_unembedded": total_unembedded,
            "total_to_process": total_to_process,
        }

        if total_to_process == 0:
            stats["elapsed_seconds"] = 0
            return stats

        logger.info(
            f"Starting embedding backfill: {total_to_process} documents "
            f"(batch_size={batch_size})"
        )

        last_log_count = 0

        while stats["processed"] + stats["failed"] < total_to_process:
            remaining = total_to_process - stats["processed"] - stats["failed"]
            fetch_size = min(batch_size, remaining)

            try:
                rows = self._safe_execute(
                    "SELECT id, text FROM documents WHERE has_embedding = 0 LIMIT ?",
                    (fetch_size,),
                ).fetchall()
            except Exception as e:
                logger.error(f"Failed to query unembedded docs: {e}")
                stats["failed"] += fetch_size
                break

            if not rows:
                break

            doc_ids = [r["id"] for r in rows]
            texts = [r["text"] for r in rows]

            # Batch-embed
            try:
                embeddings = self._get_embeddings_batch(texts)
            except Exception as e:
                logger.error(f"Batch embedding failed: {e}")
                stats["failed"] += len(rows)
                continue

            # Store each embedding
            batch_ok = 0
            batch_fail = 0
            for doc_id, embedding in zip(doc_ids, embeddings):
                if embedding is None:
                    batch_fail += 1
                    continue
                try:
                    blob = _float_list_to_blob(embedding)
                    # vec0 virtual tables don't support UPSERT; delete-then-insert
                    self._safe_execute(
                        "DELETE FROM document_embeddings WHERE doc_id = ?",
                        (doc_id,),
                    )
                    self._safe_execute(
                        "INSERT INTO document_embeddings (doc_id, embedding) VALUES (?, ?)",
                        (doc_id, blob),
                    )
                    self._safe_execute(
                        "UPDATE documents SET has_embedding = 1 WHERE id = ?",
                        (doc_id,),
                    )
                    batch_ok += 1
                except Exception as e:
                    batch_fail += 1
                    if batch_fail <= 5:
                        logger.warning(f"Failed to store embedding for {doc_id}: {e}")

            # Commit after each batch (resumability)
            self.conn.commit()

            stats["processed"] += batch_ok
            stats["failed"] += batch_fail

            # Progress callback
            if callback:
                try:
                    callback(stats["processed"], total_to_process)
                except Exception:
                    pass

            # Log progress every 1000 docs
            if stats["processed"] - last_log_count >= 1000 or stats["processed"] == total_to_process:
                elapsed = _time.monotonic() - t0
                rate = stats["processed"] / elapsed if elapsed > 0 else 0
                eta = (total_to_process - stats["processed"]) / rate if rate > 0 else 0
                logger.info(
                    f"Backfill progress: {stats['processed']}/{total_to_process} "
                    f"({stats['processed']*100//total_to_process}%) "
                    f"rate={rate:.0f} docs/s, ETA={eta:.0f}s"
                )
                last_log_count = stats["processed"]

        elapsed = _time.monotonic() - t0
        stats["elapsed_seconds"] = round(elapsed, 2)
        stats["rate_docs_per_sec"] = round(stats["processed"] / elapsed, 1) if elapsed > 0 else 0

        # Save backfill metadata
        try:
            now = datetime.now(timezone.utc).isoformat()
            meta_json = json.dumps(stats)
            self._safe_execute(
                "INSERT INTO backfill_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("last_backfill", meta_json, now),
            )
            self.conn.commit()
        except Exception as e:
            logger.warning(f"Failed to save backfill metadata: {e}")

        logger.info(
            f"Backfill complete: processed={stats['processed']}, "
            f"failed={stats['failed']}, elapsed={stats['elapsed_seconds']}s, "
            f"rate={stats.get('rate_docs_per_sec', 0)} docs/s"
        )
        return stats

    def get_backfill_status(self) -> dict:
        """Get current backfill status: unembedded count, total, last run stats.

        Returns:
            dict with keys: total_docs, unembedded_docs, embedded_docs,
                            embed_pct, last_backfill (dict or None).
        """
        try:
            total = self._safe_execute(
                "SELECT COUNT(*) as c FROM documents"
            ).fetchone()["c"]
            unembedded = self._safe_execute(
                "SELECT COUNT(*) as c FROM documents WHERE has_embedding = 0"
            ).fetchone()["c"]
        except Exception as e:
            logger.error(f"Failed to query backfill status: {e}")
            return {"error": str(e)}

        embedded = total - unembedded
        embed_pct = round(embedded / total * 100, 1) if total > 0 else 0.0

        # Load last backfill stats
        last_backfill = None
        try:
            row = self._safe_execute(
                "SELECT value, updated_at FROM backfill_meta WHERE key = 'last_backfill'"
            ).fetchone()
            if row:
                last_backfill = json.loads(row["value"])
                last_backfill["run_at"] = row["updated_at"]
        except Exception:
            pass

        return {
            "total_docs": total,
            "unembedded_docs": unembedded,
            "embedded_docs": embedded,
            "embed_pct": embed_pct,
            "last_backfill": last_backfill,
        }

    # -- Search --------------------------------------------------------------

    def search(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        where: Optional[dict] = None,
    ) -> list[dict]:
        """
        Full-text search using FTS5. Fast, zero-cost, no API needed.
        Returns results with id, text, metadata, and BM25 rank score.
        Deduplicates chunks from the same parent document.
        """
        fts_query = self._build_fts_query(query)
        # Fetch extra to account for chunk dedup
        fetch_limit = limit * 3

        try:
            if collection:
                rows = self._safe_execute(
                    "SELECT d.id, d.text, d.metadata, d.collection, d.created_at, "
                    "rank AS score "
                    "FROM documents_fts fts "
                    "JOIN documents d ON d.rowid = fts.rowid "
                    "WHERE documents_fts MATCH ? AND d.collection = ? "
                    "ORDER BY rank "
                    "LIMIT ?",
                    (fts_query, collection, fetch_limit),
                ).fetchall()
            else:
                rows = self._safe_execute(
                    "SELECT d.id, d.text, d.metadata, d.collection, d.created_at, "
                    "rank AS score "
                    "FROM documents_fts fts "
                    "JOIN documents d ON d.rowid = fts.rowid "
                    "WHERE documents_fts MATCH ? "
                    "ORDER BY rank "
                    "LIMIT ?",
                    (fts_query, fetch_limit),
                ).fetchall()
        except Exception as e:
            logger.error(f"FTS search failed for query '{query}': {e}")
            return []

        results = self._rows_to_results(rows, score_key="score")
        return self._deduplicate_chunks(results)[:limit]

    def vector_search(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Vector similarity search using sqlite_vec + embeddings.
        Falls back to FTS if vector search is unavailable.
        """
        if not self.vec_available:
            logger.info("Vector search unavailable, falling back to FTS")
            return self.search(query, collection=collection, limit=limit)

        try:
            query_embedding = self._get_embedding(query)
            if query_embedding is None:
                logger.info("Embedding generation failed, falling back to FTS")
                return self.search(query, collection=collection, limit=limit)

            query_blob = _float_list_to_blob(query_embedding)

            if collection:
                # vec0 MATCH can't be combined with JOINs in a single query.
                # Strategy: get more vec results, then filter by collection in Python.
                vec_limit = limit * 3
                rows = self._safe_execute(
                    "SELECT doc_id, distance "
                    "FROM document_embeddings "
                    "WHERE embedding MATCH ? "
                    "ORDER BY distance "
                    "LIMIT ?",
                    (query_blob, vec_limit),
                ).fetchall()

                # Filter by collection using document metadata
                filtered = []
                for r in rows:
                    doc = self._safe_execute(
                        "SELECT id, text, metadata, collection, created_at "
                        "FROM documents WHERE id = ? AND collection = ?",
                        (r["doc_id"], collection),
                    ).fetchone()
                    if doc:
                        filtered.append({
                            "id": doc["id"],
                            "text": doc["text"],
                            "metadata": json.loads(doc["metadata"]) if doc["metadata"] else {},
                            "collection": doc["collection"],
                            "created_at": doc["created_at"],
                            "distance": r["distance"],
                        })
                    if len(filtered) >= limit:
                        break

                results = filtered
            else:
                rows = self._safe_execute(
                    "SELECT doc_id, distance "
                    "FROM document_embeddings "
                    "WHERE embedding MATCH ? "
                    "ORDER BY distance "
                    "LIMIT ?",
                    (query_blob, limit * 3),
                ).fetchall()

                results = []
                for r in rows:
                    doc = self._safe_execute(
                        "SELECT id, text, metadata, collection, created_at "
                        "FROM documents WHERE id = ?",
                        (r["doc_id"],),
                    ).fetchone()
                    if doc:
                        results.append({
                            "id": doc["id"],
                            "text": doc["text"],
                            "metadata": json.loads(doc["metadata"]) if doc["metadata"] else {},
                            "collection": doc["collection"],
                            "created_at": doc["created_at"],
                            "distance": r["distance"],
                        })

            return self._deduplicate_chunks(results)[:limit]

        except Exception as e:
            logger.warning(f"Vector search failed, falling back to FTS: {e}")
            return self.search(query, collection=collection, limit=limit)

    def search_hybrid(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        fts_weight: float = DEFAULT_FTS_WEIGHT,
        vec_weight: float = DEFAULT_VEC_WEIGHT,
    ) -> list[dict]:
        """
        Hybrid search combining FTS5 BM25 scores with vector similarity.
        Normalizes both to [0, 1] and combines with configurable weights.
        Falls back to FTS-only if vector search is unavailable.
        """
        # Get FTS results (BM25 rank — lower/more negative = better match)
        fts_results = self.search(query, collection=collection, limit=limit * 2)

        # Get vector results if available
        vec_results = []
        if self.vec_available:
            try:
                vec_results = self.vector_search(query, collection=collection, limit=limit * 2)
            except Exception as e:
                logger.warning(f"Vector component of hybrid search failed: {e}")

        if not vec_results:
            # No vector results — return FTS only
            return fts_results[:limit]

        if not fts_results:
            return vec_results[:limit]

        # Normalize FTS scores (rank is negative, more negative = better)
        fts_scores = {}
        fts_by_id = {}
        raw_fts = [r.get("score", 0) for r in fts_results]
        if raw_fts:
            min_fts = min(raw_fts)
            max_fts = max(raw_fts)
            fts_range = max_fts - min_fts if max_fts != min_fts else 1.0
            for r in fts_results:
                raw = r.get("score", 0)
                # Invert: most negative rank -> highest score (1.0)
                normalized = 1.0 - ((raw - min_fts) / fts_range)
                fts_scores[r["id"]] = normalized
                fts_by_id[r["id"]] = r

        # Normalize vector distances (lower distance = better)
        vec_scores = {}
        vec_by_id = {}
        raw_vec = [r.get("distance", 0) for r in vec_results]
        if raw_vec:
            min_vec = min(raw_vec)
            max_vec = max(raw_vec)
            vec_range = max_vec - min_vec if max_vec != min_vec else 1.0
            for r in vec_results:
                raw = r.get("distance", 0)
                # Invert: lowest distance -> highest score (1.0)
                normalized = 1.0 - ((raw - min_vec) / vec_range)
                vec_scores[r["id"]] = normalized
                vec_by_id[r["id"]] = r

        # Combine scores
        all_ids = set(fts_scores.keys()) | set(vec_scores.keys())
        combined = []
        for doc_id in all_ids:
            fs = fts_scores.get(doc_id, 0.0)
            vs = vec_scores.get(doc_id, 0.0)
            hybrid_score = (fts_weight * fs) + (vec_weight * vs)

            # Get the document data from whichever source has it
            doc = fts_by_id.get(doc_id) or vec_by_id.get(doc_id)
            if doc:
                result = {**doc, "hybrid_score": hybrid_score}
                # Include component scores for transparency
                result["fts_score_normalized"] = fs
                result["vec_score_normalized"] = vs
                combined.append(result)

        # Sort by hybrid score descending (higher = better)
        combined.sort(key=lambda x: x["hybrid_score"], reverse=True)
        return combined[:limit]

    def search_all(
        self,
        query: str,
        limit_per_collection: int = 5,
    ) -> dict[str, list[dict]]:
        """Search across all collections. Returns results grouped by collection."""
        results = {}
        for name in self.VALID_COLLECTIONS:
            results[name] = self.search(
                query, collection=name, limit=limit_per_collection
            )
        return results

    def _build_fts_query(self, query: str) -> str:
        """
        Build FTS5 query from natural language.

        Supports:
        - Phrase queries: '"exact phrase"' -> searches as phrase
        - Multi-word: 'foo bar' -> 'foo* OR bar*' (prefix matching)
        - Single word: 'foo' -> 'foo*' (prefix matching)
        """
        query = query.strip()
        if not query:
            return '""'

        # Check for explicit phrase query (entire query wrapped in quotes)
        if query.startswith('"') and query.endswith('"') and len(query) > 2:
            inner = query[1:-1]
            # Validate it's clean
            clean = "".join(c for c in inner if c.isalnum() or c in " _-")
            if clean.strip():
                return f'"{clean}"'
            return '""'

        # Strip special FTS5 characters that could cause syntax errors
        clean = "".join(c for c in query if c.isalnum() or c in " _-")
        terms = clean.split()
        if not terms:
            return '""'

        # Use OR with prefix matching for better recall.
        # FTS5 treats "col:term" as column filter; hyphens in "foo-bar*" break into
        # invalid column names (e.g. health-probe -> column "health", "probe").
        parts = []
        for t in terms:
            if not t:
                continue
            quoted = f'"{t}"'
            if "-" in t:
                parts.append(quoted)
            else:
                parts.append(f'{quoted} OR {t}*')
        return " OR ".join(parts)

    def _rows_to_results(self, rows, score_key: str = "score") -> list[dict]:
        """Convert SQLite rows to result dicts."""
        results = []
        for r in rows:
            result = {
                "id": r["id"],
                "text": r["text"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "collection": r["collection"],
                "created_at": r["created_at"],
            }
            if score_key in r.keys():
                result[score_key] = r[score_key]
            results.append(result)
        return results

    def _deduplicate_chunks(self, results: list[dict]) -> list[dict]:
        """
        Deduplicate results from chunked documents.
        When multiple chunks from the same parent doc appear,
        keep only the best-scoring chunk.
        """
        seen_parents = set()
        deduped = []
        for r in results:
            meta = r.get("metadata", {})
            parent_id = meta.get("_parent_doc_id")
            if parent_id:
                if parent_id in seen_parents:
                    continue
                seen_parents.add(parent_id)
            deduped.append(r)
        return deduped

    # -- Deduplication -------------------------------------------------------

    def find_duplicates(self) -> list[list[dict]]:
        """
        Find groups of documents with identical text hashes.
        Returns list of groups, where each group is a list of duplicate docs.
        """
        try:
            rows = self._safe_execute(
                "SELECT text_hash, COUNT(*) as cnt FROM documents "
                "WHERE text_hash IS NOT NULL "
                "GROUP BY text_hash HAVING cnt > 1"
            ).fetchall()

            groups = []
            for r in rows:
                docs = self._safe_execute(
                    "SELECT id, collection, text, metadata, created_at, text_hash "
                    "FROM documents WHERE text_hash = ?",
                    (r["text_hash"],),
                ).fetchall()
                groups.append([
                    {
                        "id": d["id"],
                        "collection": d["collection"],
                        "text": d["text"][:200],  # Truncate for readability
                        "created_at": d["created_at"],
                        "text_hash": d["text_hash"],
                    }
                    for d in docs
                ])
            return groups
        except Exception as e:
            logger.error(f"Failed to find duplicates: {e}")
            return []

    # -- Stats ---------------------------------------------------------------

    def stats(self) -> dict:
        """Get detailed store statistics."""
        result = {
            "total": 0,
            "collections": {},
            "with_embeddings": 0,
            "embedding_provider": EMBEDDING_PROVIDER,
            "embedding_dim": EMBEDDING_DIM,
            "vec_available": self.vec_available,
        }

        try:
            for coll in self.VALID_COLLECTIONS:
                row = self._safe_execute(
                    "SELECT COUNT(*) as c, "
                    "MIN(created_at) as oldest, "
                    "MAX(created_at) as newest "
                    "FROM documents WHERE collection = ?",
                    (coll,),
                ).fetchone()
                count = row["c"]
                result["collections"][coll] = {
                    "count": count,
                    "oldest": row["oldest"],
                    "newest": row["newest"],
                }
                result["total"] += count

            result["with_embeddings"] = self._safe_execute(
                "SELECT COUNT(*) as c FROM documents WHERE has_embedding = 1"
            ).fetchone()["c"]

            # Total size estimate
            try:
                db_size = self.db_path.stat().st_size
                result["db_size_bytes"] = db_size
                result["db_size_mb"] = round(db_size / (1024 * 1024), 2)
            except OSError:
                pass

            # Duplicate count
            try:
                dup_row = self._safe_execute(
                    "SELECT COUNT(*) as c FROM ("
                    "  SELECT text_hash FROM documents "
                    "  WHERE text_hash IS NOT NULL "
                    "  GROUP BY text_hash HAVING COUNT(*) > 1"
                    ")"
                ).fetchone()
                result["duplicate_hash_groups"] = dup_row["c"]
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Failed to compute stats: {e}")

        return result

    # -- Management ----------------------------------------------------------

    def get_by_id(self, doc_id: str) -> Optional[dict]:
        """Get a single document by ID."""
        try:
            row = self._safe_execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        except Exception as e:
            logger.error(f"Failed to get document {doc_id}: {e}")
            return None
        if not row:
            return None
        return {
            "id": row["id"],
            "text": row["text"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "collection": row["collection"],
            "created_at": row["created_at"],
        }

    def get_by_ids(self, doc_ids: list[str]) -> list[dict]:
        """Get multiple documents by IDs."""
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        try:
            rows = self._safe_execute(
                f"SELECT * FROM documents WHERE id IN ({placeholders})", doc_ids
            ).fetchall()
        except Exception as e:
            logger.error(f"Failed to get documents by IDs: {e}")
            return []
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

    def delete(self, doc_ids: list[str]):
        """Delete documents by ID."""
        if not doc_ids:
            return
        placeholders = ",".join("?" for _ in doc_ids)
        try:
            self._safe_execute(
                f"DELETE FROM documents WHERE id IN ({placeholders})", doc_ids
            )
            if self.vec_available:
                self._safe_execute(
                    f"DELETE FROM document_embeddings WHERE doc_id IN ({placeholders})",
                    doc_ids,
                )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to delete documents: {e}")
            raise

    def recent(self, collection: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Get most recent documents."""
        try:
            if collection:
                rows = self._safe_execute(
                    "SELECT * FROM documents WHERE collection = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (collection, limit),
                ).fetchall()
            else:
                rows = self._safe_execute(
                    "SELECT * FROM documents ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception as e:
            logger.error(f"Failed to get recent documents: {e}")
            return []

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

    def vacuum(self):
        """Run VACUUM and FTS optimize to reclaim space and optimize indices."""
        try:
            logger.info("Running FTS optimize...")
            self._safe_execute(
                "INSERT INTO documents_fts(documents_fts) VALUES('optimize')"
            )
            self.conn.commit()
            logger.info("Running VACUUM...")
            self.conn.execute("VACUUM")
            logger.info("Vacuum complete")
        except Exception as e:
            logger.error(f"Vacuum failed: {e}")
            raise

    def reindex_fts(self):
        """
        Rebuild the FTS5 index from scratch.
        Useful for recovery if the FTS index gets corrupted.
        """
        try:
            logger.info("Rebuilding FTS5 index...")
            self._safe_execute(
                "INSERT INTO documents_fts(documents_fts) VALUES('rebuild')"
            )
            self.conn.commit()
            logger.info("FTS5 index rebuilt successfully")
        except Exception as e:
            logger.error(f"FTS reindex failed: {e}")
            raise

    def close(self):
        """Close database connection."""
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass


# -- CLI for testing ---------------------------------------------------------


def _cli_backfill(args):
    """CLI handler for the backfill command."""
    import argparse
    import time as _time

    parser = argparse.ArgumentParser(
        prog="unified_vector_store.py backfill",
        description="Sync observations and backfill embeddings.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100,
        help="Documents per embedding batch (default: 100)",
    )
    parser.add_argument(
        "--max-docs", type=int, default=None,
        help="Max documents to embed (default: all)",
    )
    parser.add_argument(
        "--obs-db", type=str, default=str(DATA_DIR / "cortex-observations.db"),
        help="Path to cortex-observations.db",
    )
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="Skip observation sync (only run embedding backfill)",
    )
    parser.add_argument(
        "--sync-only", action="store_true",
        help="Only sync observations, skip embedding backfill",
    )
    opts = parser.parse_args(args)

    # Configure logging to stdout for CLI
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    store = get_vector_store()
    print(f"Vector store: {store.db_path}")
    print(f"Vec search: {'enabled' if store.vec_available else 'disabled'}")
    print(f"Provider: {EMBEDDING_PROVIDER} ({EMBEDDING_DIM} dims)")
    print()

    # --- Step 1: Sync observations ---
    if not opts.skip_sync:
        obs_path = Path(opts.obs_db)
        if not obs_path.exists():
            print(f"ERROR: Observations DB not found: {obs_path}")
            sys.exit(1)

        print(f"=== Step 1: Syncing observations from {obs_path.name} ===")
        sync_stats = store.sync_from_observations(obs_path, batch_size=500)
        print(f"  Eligible:  {sync_stats['total_eligible']}")
        print(f"  Synced:    {sync_stats['synced']}")
        print(f"  Skipped:   {sync_stats['skipped']} (already present)")
        print(f"  Failed:    {sync_stats['failed']}")
        print(f"  Time:      {sync_stats['elapsed_seconds']}s")
        print()

    if opts.sync_only:
        print("Sync-only mode. Skipping embedding backfill.")
        _print_backfill_status(store)
        return

    # --- Step 2: Backfill embeddings ---
    if not store.vec_available:
        print("ERROR: sqlite_vec not available. Cannot generate embeddings.")
        sys.exit(1)

    status = store.get_backfill_status()
    total_to_embed = status["unembedded_docs"]
    if opts.max_docs:
        total_to_embed = min(total_to_embed, opts.max_docs)

    if total_to_embed == 0:
        print("All documents already have embeddings. Nothing to do.")
        _print_backfill_status(store)
        return

    print(f"=== Step 2: Embedding {total_to_embed} documents (batch_size={opts.batch_size}) ===")

    # Estimate time
    est_rate = 100  # conservative docs/sec for MiniLM on CPU
    est_seconds = total_to_embed / est_rate
    est_min = est_seconds / 60
    print(f"  Estimated time: ~{est_min:.1f} minutes ({est_rate} docs/s estimate)")
    print()

    # Progress display
    last_print = [0]
    t_start = [_time.monotonic()]

    def progress_callback(processed, total):
        now = _time.monotonic()
        # Print every 500 docs or on completion
        if processed - last_print[0] >= 500 or processed == total:
            elapsed = now - t_start[0]
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (total - processed) / rate if rate > 0 else 0
            pct = processed * 100 // total if total > 0 else 100
            bar_len = 40
            filled = bar_len * processed // total if total > 0 else bar_len
            bar = "#" * filled + "-" * (bar_len - filled)
            print(
                f"\r  [{bar}] {pct}% ({processed}/{total}) "
                f"{rate:.0f} docs/s, ETA {eta:.0f}s   ",
                end="", flush=True,
            )
            last_print[0] = processed

    result = store.backfill_embeddings(
        batch_size=opts.batch_size,
        max_docs=opts.max_docs,
        callback=progress_callback,
    )
    print()  # Newline after progress bar
    print()

    print("=== Backfill Results ===")
    print(f"  Processed: {result['processed']}")
    print(f"  Failed:    {result['failed']}")
    print(f"  Time:      {result['elapsed_seconds']}s")
    print(f"  Rate:      {result.get('rate_docs_per_sec', 0)} docs/s")
    print()

    _print_backfill_status(store)


def _print_backfill_status(store):
    """Print current backfill status summary."""
    status = store.get_backfill_status()
    print("=== Current Status ===")
    print(f"  Total docs:     {status['total_docs']}")
    print(f"  With embedding: {status['embedded_docs']}")
    print(f"  Without:        {status['unembedded_docs']}")
    print(f"  Coverage:       {status['embed_pct']}%")
    if status.get("last_backfill"):
        lb = status["last_backfill"]
        print(f"  Last backfill:  {lb.get('run_at', 'unknown')} "
              f"({lb.get('processed', '?')} docs in {lb.get('elapsed_seconds', '?')}s)")


def _cli_status(args):
    """CLI handler for the status command."""
    logging.basicConfig(level=logging.WARNING)
    store = get_vector_store()
    print(f"Vector store: {store.db_path}")
    print(f"Provider: {EMBEDDING_PROVIDER} ({EMBEDDING_DIM} dims)")
    print(f"Vec search: {'enabled' if store.vec_available else 'disabled'}")
    print()

    s = store.stats()
    print(f"Total documents: {s['total']}")
    print(f"With embeddings: {s['with_embeddings']}")
    if s.get("db_size_mb"):
        print(f"DB size: {s['db_size_mb']} MB")
    print()

    for coll, info in s.get("collections", {}).items():
        if info["count"] > 0:
            print(f"  {coll}: {info['count']} docs ({info['oldest']} to {info['newest']})")

    print()
    _print_backfill_status(store)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        _cli_backfill(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        _cli_status(sys.argv[2:])
    else:
        # Original search/test CLI
        store = get_vector_store()
        print(f"Vector store at: {store.db_path}")
        print(f"Vec search: {'enabled' if store.vec_available else 'disabled'}")
        print(f"Provider: {EMBEDDING_PROVIDER} ({EMBEDDING_DIM} dims)")
        print(f"Stats: {json.dumps(store.stats(), indent=2, default=str)}")

        if len(sys.argv) > 1:
            query = " ".join(sys.argv[1:])
            print(f"\nSearching for: '{query}'")

            print("\n=== FTS Search ===")
            results = store.search_all(query, limit_per_collection=5)
            for coll_name, items in results.items():
                if items:
                    print(f"\n--- {coll_name} ({len(items)} results) ---")
                    for item in items:
                        score = item.get("score", item.get("distance", "?"))
                        print(f"  [{item['id']}] score={score}: {item['text'][:100]}")

            if store.vec_available:
                print("\n=== Hybrid Search ===")
                hybrid = store.search_hybrid(query, limit=10)
                for item in hybrid:
                    hs = item.get("hybrid_score", "?")
                    print(f"  [{item['id']}] hybrid={hs:.3f}: {item['text'][:100]}")

            # Show duplicates if any
            dupes = store.find_duplicates()
            if dupes:
                print(f"\n=== {len(dupes)} Duplicate Groups ===")
                for group in dupes[:5]:
                    ids = [d["id"] for d in group]
                    print(f"  Hash group: {ids}")
