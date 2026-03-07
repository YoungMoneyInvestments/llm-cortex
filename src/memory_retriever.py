#!/usr/bin/env python3
"""
Memory Retriever — Token-efficient 3-layer retrieval for Cortex.

Implements the search → timeline → details pattern inspired by claude-mem,
minimizing token waste when querying Cortex memory.

Layers:
  L1: search()      → Compact index: IDs + 1-line summaries (~20 tokens each)
  L2: timeline()    → Chronological context around an observation (~100 tokens each)
  L3: get_details() → Full observation text (variable, only when needed)

Usage:
    from memory_retriever import MemoryRetriever

    retriever = MemoryRetriever()

    # L1: Find relevant memories (cheap)
    results = retriever.search("gamma exposure")

    # L2: Get context around a specific observation
    context = retriever.timeline("obs-42", window=5)

    # L3: Get full details (only what you need)
    details = retriever.get_details(["obs-42", "obs-43"])
"""

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cortex-retriever")

# ── Config ──────────────────────────────────────────────────────────────────

DATA_DIR = Path.home() / "clawd" / "data"
OBS_DB_PATH = DATA_DIR / "cortex-observations.db"
VEC_DB_PATH = DATA_DIR / "cortex-vectors.db"

# ── Main class ──────────────────────────────────────────────────────────────


class MemoryRetriever:
    """Token-efficient 3-layer memory retrieval."""

    def __init__(
        self,
        obs_db_path: Optional[Path] = None,
        vec_db_path: Optional[Path] = None,
    ):
        self.obs_db_path = obs_db_path or OBS_DB_PATH
        self.vec_db_path = vec_db_path or VEC_DB_PATH

        self._obs_conn: Optional[sqlite3.Connection] = None
        self._vec_conn: Optional[sqlite3.Connection] = None
        self._kg = None  # Lazy-loaded KnowledgeGraph instance
        self._kg_unavailable = False  # Set True if KG import/init fails (warn once)

    @property
    def obs_db(self) -> sqlite3.Connection:
        """Lazy connection to observations database."""
        if self._obs_conn is None:
            if not self.obs_db_path.exists():
                raise FileNotFoundError(
                    f"Observations DB not found: {self.obs_db_path}. "
                    "Start the memory worker first."
                )
            self._obs_conn = sqlite3.connect(str(self.obs_db_path))
            self._obs_conn.row_factory = sqlite3.Row
            self._ensure_fts_index(self._obs_conn)
        return self._obs_conn

    def _ensure_fts_index(self, conn: sqlite3.Connection):
        """Create FTS5 virtual table on observations.summary if it doesn't exist.

        Uses content-sync triggers to keep the FTS index up to date with
        the observations table, following the same pattern as unified_vector_store.py.
        """
        try:
            # Check if the FTS table already exists
            exists = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='observations_fts'"
            ).fetchone()

            if exists:
                return  # Already set up

            conn.executescript("""
                -- FTS5 full-text search index on observation summaries
                CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                    summary,
                    content=observations,
                    content_rowid=rowid
                );

                -- Triggers to keep FTS in sync with observations
                CREATE TRIGGER IF NOT EXISTS obs_fts_ai AFTER INSERT ON observations BEGIN
                    INSERT INTO observations_fts(rowid, summary)
                    VALUES (new.rowid, new.summary);
                END;

                CREATE TRIGGER IF NOT EXISTS obs_fts_ad AFTER DELETE ON observations BEGIN
                    INSERT INTO observations_fts(observations_fts, rowid, summary)
                    VALUES ('delete', old.rowid, old.summary);
                END;

                CREATE TRIGGER IF NOT EXISTS obs_fts_au AFTER UPDATE OF summary ON observations BEGIN
                    INSERT INTO observations_fts(observations_fts, rowid, summary)
                    VALUES ('delete', old.rowid, old.summary);
                    INSERT INTO observations_fts(rowid, summary)
                    VALUES (new.rowid, new.summary);
                END;
            """)

            # Populate FTS with existing rows that have summaries
            conn.execute(
                "INSERT INTO observations_fts(rowid, summary) "
                "SELECT rowid, summary FROM observations "
                "WHERE summary IS NOT NULL AND summary != ''"
            )
            count = conn.execute(
                "SELECT COUNT(*) as c FROM observations_fts"
            ).fetchone()["c"]
            conn.commit()
            logger.info(f"FTS5 index created on observations.summary, populated {count} rows")

        except Exception as e:
            logger.warning(f"FTS5 index setup failed (non-fatal, LIKE fallback active): {e}")

    @property
    def vec_db(self) -> sqlite3.Connection:
        """Lazy connection to vector store database."""
        if self._vec_conn is None:
            if not self.vec_db_path.exists():
                raise FileNotFoundError(
                    f"Vector DB not found: {self.vec_db_path}. "
                    "Initialize the vector store first."
                )
            self._vec_conn = sqlite3.connect(str(self.vec_db_path))
            self._vec_conn.row_factory = sqlite3.Row
        return self._vec_conn

    # ── L1: Search (compact index) ─────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 20,
        source: Optional[str] = None,
        agent: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """
        L1: Compact index search. Returns IDs + 1-line summaries.

        Token cost: ~20 tokens per result.
        Use this first to identify relevant memories, then drill into
        specific ones with timeline() or get_details().

        Combines results from observations (FTS5), vector store, and
        session summaries. Normalizes scores to [0, 1], applies time
        decay boosting, and deduplicates by text overlap.
        """
        results = []

        # Search observations database (FTS5 or LIKE fallback)
        results.extend(
            self._search_observations(query, limit, source, agent, session_id)
        )

        # Search vector store (FTS5)
        results.extend(self._search_vector_store(query, limit))

        # Search session summaries
        results.extend(self._search_session_summaries(query, limit))

        # Normalize scores to [0, 1] range
        self._normalize_scores(results)

        # Apply time decay boost
        self._apply_time_decay(results)

        # Deduplicate — first by ID, then by text overlap
        deduped = self._deduplicate_results(results)

        # Sort by relevance (higher normalized score = better) and limit
        deduped.sort(key=lambda x: x.get("score", 0), reverse=True)
        return deduped[:limit]

    def _normalize_scores(self, results: list[dict]):
        """Normalize all scores to [0, 1] range.

        FTS5 BM25 scores are negative (more negative = better match).
        Vector store scores may be distances (lower = better) or ranks.
        Session summary and LIKE results start at 0.
        """
        if not results:
            return

        # Group by origin to normalize within each source
        by_origin: dict[str, list[dict]] = {}
        for r in results:
            origin = r.get("origin", "unknown")
            by_origin.setdefault(origin, []).append(r)

        for origin, group in by_origin.items():
            raw_scores = [r.get("score", 0) for r in group]
            min_s = min(raw_scores)
            max_s = max(raw_scores)

            if min_s == max_s:
                # All same score — give them a neutral 0.5
                for r in group:
                    r["score"] = 0.5
                continue

            if origin in ("observations", "vector_store"):
                # BM25/rank: more negative = better. Invert so higher = better.
                for r in group:
                    raw = r.get("score", 0)
                    r["score"] = (max_s - raw) / (max_s - min_s)
            else:
                # Generic: assume higher = better
                for r in group:
                    raw = r.get("score", 0)
                    r["score"] = (raw - min_s) / (max_s - min_s)

    def _apply_time_decay(self, results: list[dict]):
        """Boost recent results: last 24h gets 1.5x, last 7d gets 1.2x."""
        now = datetime.now(timezone.utc)

        for r in results:
            ts_str = r.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_hours = (now - ts).total_seconds() / 3600

                if age_hours <= 24:
                    r["score"] = min(1.0, r.get("score", 0) * 1.5)
                elif age_hours <= 168:  # 7 days
                    r["score"] = min(1.0, r.get("score", 0) * 1.2)
            except (ValueError, TypeError):
                pass

    def _deduplicate_results(self, results: list[dict]) -> list[dict]:
        """Deduplicate by ID first, then by >80% text overlap in summaries."""
        # Phase 1: ID-based dedup
        seen_ids = set()
        id_deduped = []
        for r in results:
            key = r.get("obs_id") or r["id"]
            if key not in seen_ids:
                seen_ids.add(key)
                id_deduped.append(r)

        # Phase 2: Text overlap dedup — keep higher-scored result
        # Sort by score descending so we keep the best version
        id_deduped.sort(key=lambda x: x.get("score", 0), reverse=True)
        final = []
        for candidate in id_deduped:
            c_summary = candidate.get("summary", "")
            is_dup = False
            for kept in final:
                k_summary = kept.get("summary", "")
                if c_summary and k_summary:
                    ratio = SequenceMatcher(None, c_summary, k_summary).ratio()
                    if ratio > 0.8:
                        is_dup = True
                        break
            if not is_dup:
                final.append(candidate)

        return final

    def _search_observations(
        self,
        query: str,
        limit: int,
        source: Optional[str],
        agent: Optional[str],
        session_id: Optional[str],
    ) -> list[dict]:
        """Search observations database using FTS5 (with LIKE fallback)."""
        try:
            results = self._search_observations_fts(query, limit, source, agent, session_id)
            if results is not None:
                return results
        except Exception:
            pass

        # Fallback to LIKE matching
        return self._search_observations_like(query, limit, source, agent, session_id)

    def _search_observations_fts(
        self,
        query: str,
        limit: int,
        source: Optional[str],
        agent: Optional[str],
        session_id: Optional[str],
    ) -> Optional[list[dict]]:
        """Search using FTS5 MATCH with BM25 scoring. Returns None if FTS unavailable."""
        # Check if FTS table exists
        exists = self.obs_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='observations_fts'"
        ).fetchone()
        if not exists:
            return None

        # Build FTS5 query — OR-join for permissive matching
        terms = query.strip().split()
        if not terms:
            return None
        fts_query = " OR ".join(
            f'"{("".join(c for c in t if c.isalnum() or c in "_-"))}"'
            for t in terms if t
        )
        if not fts_query:
            return None

        # Build filter conditions on the joined observations table
        filters = ["o.status = 'processed'"]
        params = [fts_query]

        if source:
            filters.append("o.source = ?")
            params.append(source)
        if agent:
            filters.append("o.agent = ?")
            params.append(agent)
        if session_id:
            filters.append("o.session_id = ?")
            params.append(session_id)

        where = " AND ".join(filters)
        params.append(limit)

        rows = self.obs_db.execute(
            f"SELECT o.id, o.session_id, o.timestamp, o.source, o.tool_name, "
            f"o.agent, o.summary, rank AS bm25_score "
            f"FROM observations_fts fts "
            f"JOIN observations o ON o.rowid = fts.rowid "
            f"WHERE observations_fts MATCH ? AND {where} "
            f"ORDER BY rank "
            f"LIMIT ?",
            params,
        ).fetchall()

        return [
            {
                "id": f"obs-{r['id']}",
                "obs_id": r["id"],
                "summary": self._truncate_summary(r["summary"]),
                "source": r["source"],
                "tool": r["tool_name"],
                "agent": r["agent"],
                "timestamp": r["timestamp"],
                "session_id": r["session_id"][:12] + "..." if r["session_id"] else None,
                "score": r["bm25_score"],  # BM25 rank (negative, more negative = better)
                "origin": "observations",
            }
            for r in rows
        ]

    def _search_observations_like(
        self,
        query: str,
        limit: int,
        source: Optional[str],
        agent: Optional[str],
        session_id: Optional[str],
    ) -> list[dict]:
        """Fallback: search using LIKE matching on summaries."""
        try:
            terms = query.lower().split()
            if not terms:
                return []

            conditions = []
            params = []
            for term in terms:
                conditions.append("(LOWER(summary) LIKE ? OR LOWER(tool_name) LIKE ?)")
                params.extend([f"%{term}%", f"%{term}%"])

            where = " AND ".join(conditions)

            if source:
                where += " AND source = ?"
                params.append(source)
            if agent:
                where += " AND agent = ?"
                params.append(agent)
            if session_id:
                where += " AND session_id = ?"
                params.append(session_id)

            params.append(limit)

            rows = self.obs_db.execute(
                f"SELECT id, session_id, timestamp, source, tool_name, agent, summary "
                f"FROM observations "
                f"WHERE status = 'processed' AND {where} "
                f"ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()

            return [
                {
                    "id": f"obs-{r['id']}",
                    "obs_id": r["id"],
                    "summary": self._truncate_summary(r["summary"]),
                    "source": r["source"],
                    "tool": r["tool_name"],
                    "agent": r["agent"],
                    "timestamp": r["timestamp"],
                    "session_id": r["session_id"][:12] + "..." if r["session_id"] else None,
                    "score": 0,  # LIKE match doesn't have a score
                    "origin": "observations",
                }
                for r in rows
            ]
        except Exception:
            return []

    def _search_vector_store(self, query: str, limit: int) -> list[dict]:
        """Search vector store using FTS5."""
        try:
            # Import the vector store module
            sys.path.insert(0, str(Path(__file__).parent))
            from unified_vector_store import get_vector_store
            store = get_vector_store(self.vec_db_path)
            results = store.search(query, limit=limit)
            return [
                {
                    "id": r["id"],
                    "summary": self._truncate_summary(r["text"]),
                    "collection": r.get("collection", "unknown"),
                    "timestamp": r.get("created_at"),
                    "score": r.get("score", 0),
                    "origin": "vector_store",
                }
                for r in results
            ]
        except Exception:
            return []

    def _search_session_summaries(self, query: str, limit: int) -> list[dict]:
        """Search the session_summaries table for matching session-level summaries."""
        try:
            terms = query.lower().split()
            if not terms:
                return []

            conditions = []
            params = []
            for term in terms:
                conditions.append(
                    "(LOWER(ss.summary) LIKE ? OR LOWER(ss.key_decisions) LIKE ? "
                    "OR LOWER(ss.entities_mentioned) LIKE ?)"
                )
                params.extend([f"%{term}%", f"%{term}%", f"%{term}%"])

            where = " AND ".join(conditions)
            params.append(limit)

            rows = self.obs_db.execute(
                f"SELECT ss.id, ss.session_id, ss.summary, ss.key_decisions, "
                f"ss.entities_mentioned, ss.created_at, s.user_prompt, s.agent "
                f"FROM session_summaries ss "
                f"LEFT JOIN sessions s ON s.id = ss.session_id "
                f"WHERE {where} "
                f"ORDER BY ss.id DESC LIMIT ?",
                params,
            ).fetchall()

            # Count matching terms for a rough relevance score
            return [
                {
                    "id": f"session-{r['session_id'][:12]}",
                    "obs_id": None,
                    "summary": self._truncate_summary(r["summary"], 200),
                    "source": "session_summary",
                    "tool": None,
                    "agent": r["agent"],
                    "timestamp": r["created_at"],
                    "session_id": r["session_id"][:12] + "..." if r["session_id"] else None,
                    "score": 0,  # Will be normalized later
                    "origin": "session_summary",
                    "key_decisions": r["key_decisions"],
                    "entities_mentioned": r["entities_mentioned"],
                }
                for r in rows
            ]
        except Exception as e:
            logger.debug(f"Session summary search failed: {e}")
            return []

    def _truncate_summary(self, text: Optional[str], max_len: int = 120) -> str:
        """Truncate text to create a compact summary line."""
        if not text:
            return ""
        clean = text.replace("\n", " ").strip()
        if len(clean) > max_len:
            return clean[:max_len] + "..."
        return clean

    # ── L2: Timeline (chronological context) ────────────────────────────────

    def timeline(
        self,
        observation_id: int,
        window: int = 5,
    ) -> list[dict]:
        """
        L2: Chronological context around a specific observation.

        Returns the target observation plus `window` observations before
        and after it in the same session, giving conversational context.

        Token cost: ~100 tokens per item.
        """
        # Get the target observation
        target = self.obs_db.execute(
            "SELECT id, session_id, timestamp, source, tool_name, agent, summary "
            "FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()

        if not target:
            return []

        session_id = target["session_id"]
        target_id = target["id"]

        # Get surrounding observations in the same session
        rows = self.obs_db.execute(
            "SELECT id, session_id, timestamp, source, tool_name, agent, summary "
            "FROM observations "
            "WHERE session_id = ? AND id BETWEEN ? AND ? "
            "ORDER BY id ASC",
            (session_id, target_id - window, target_id + window),
        ).fetchall()

        return [
            {
                "id": r["id"],
                "is_target": r["id"] == target_id,
                "source": r["source"],
                "tool": r["tool_name"],
                "agent": r["agent"],
                "summary": self._truncate_summary(r["summary"], 200),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    # ── L3: Full details ────────────────────────────────────────────────────

    def get_details(self, observation_ids: list[int]) -> list[dict]:
        """
        L3: Full observation details. Only fetch what you actually need.

        Returns complete raw_input, raw_output, and summary for the
        specified observation IDs.

        Token cost: variable (full text).
        """
        if not observation_ids:
            return []

        placeholders = ",".join("?" for _ in observation_ids)
        rows = self.obs_db.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders})",
            observation_ids,
        ).fetchall()

        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "timestamp": r["timestamp"],
                "source": r["source"],
                "tool_name": r["tool_name"],
                "agent": r["agent"],
                "raw_input": r["raw_input"],
                "raw_output": r["raw_output"],
                "summary": r["summary"],
                "status": r["status"],
                "vector_synced": bool(r["vector_synced"]),
            }
            for r in rows
        ]

    # ── Convenience methods ─────────────────────────────────────────────────

    def recent_observations(
        self,
        limit: int = 20,
        hours: int = 48,
    ) -> list[dict]:
        """Get recent observations as compact index (for session start injection)."""
        rows = self.obs_db.execute(
            "SELECT id, session_id, timestamp, source, tool_name, agent, summary "
            "FROM observations "
            "WHERE status = 'processed' "
            f"AND timestamp > datetime('now', '-{hours} hours') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        return [
            {
                "id": r["id"],
                "summary": self._truncate_summary(r["summary"]),
                "source": r["source"],
                "tool": r["tool_name"],
                "agent": r["agent"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    def session_summary(self, session_id: str) -> dict:
        """Get session info with observation count and summary."""
        session = self.obs_db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

        if not session:
            return {"error": f"Session {session_id} not found"}

        obs_count = self.obs_db.execute(
            "SELECT COUNT(*) as c FROM observations WHERE session_id = ?",
            (session_id,),
        ).fetchone()["c"]

        tools_used = self.obs_db.execute(
            "SELECT tool_name, COUNT(*) as c FROM observations "
            "WHERE session_id = ? AND tool_name IS NOT NULL "
            "GROUP BY tool_name ORDER BY c DESC",
            (session_id,),
        ).fetchall()

        return {
            "session_id": session["id"],
            "agent": session["agent"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "status": session["status"],
            "user_prompt": session["user_prompt"],
            "observation_count": obs_count,
            "tools_used": {r["tool_name"]: r["c"] for r in tools_used},
        }

    def save_memory(self, content: str, metadata: Optional[dict] = None) -> str:
        """Manually save a memory for future retrieval."""
        sys.path.insert(0, str(Path(__file__).parent))
        from unified_vector_store import get_vector_store

        store = get_vector_store(self.vec_db_path)
        mem_id = f"manual-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        store.add_knowledge(mem_id, content, metadata)
        return f"kg-{mem_id}"

    # ── Knowledge Graph Integration ──────────────────────────────────────

    @property
    def kg(self):
        """Lazy-load KnowledgeGraph. Returns None if unavailable."""
        if self._kg is not None:
            return self._kg
        if self._kg_unavailable:
            return None
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from knowledge_graph import KnowledgeGraph
            self._kg = KnowledgeGraph()
            if self._kg.graph.number_of_nodes() == 0:
                logger.info("Knowledge graph loaded but empty — graph features inactive")
            return self._kg
        except Exception as e:
            logger.warning("Knowledge graph unavailable — graph features disabled: %s", e)
            self._kg_unavailable = True
            return None

    def _extract_entities_from_text(self, text: str) -> list[str]:
        """Extract entity IDs from text by matching against the knowledge graph.

        Tokenizes text into words and common bigrams/trigrams, checks each
        against the KG entity index using resolve_entity() for alias resolution.
        Returns list of matched canonical entity IDs (deduplicated).

        Performance note: builds a pre-computed index of entity IDs and display
        names on first call for fast substring matching, avoiding repeated
        fuzzy SequenceMatcher calls across many candidates.
        """
        if not self.kg or self.kg.graph.number_of_nodes() == 0:
            return []

        # Build entity lookup index once (cached on KG instance for reuse)
        if not hasattr(self.kg, '_entity_lookup'):
            lookup = {}  # normalized_name -> canonical_id
            for node_id, attrs in self.kg.graph.nodes(data=True):
                lookup[node_id] = node_id
                display = attrs.get("display_name", "")
                if display:
                    lookup[display.lower().strip()] = node_id
            # Add aliases
            try:
                conn = self.kg._get_conn()
                try:
                    for row in conn.execute("SELECT alias, canonical_id FROM aliases"):
                        lookup[row["alias"]] = row["canonical_id"]
                finally:
                    conn.close()
            except Exception:
                pass
            self.kg._entity_lookup = lookup

        text_lower = text.lower()
        # Tokenize into words (strip punctuation)
        words = [
            w.strip(".,;:!?\"'()[]{}") for w in text_lower.split()
            if len(w.strip(".,;:!?\"'()[]{}")) >= 2
        ]

        # Build candidates: individual words + bigrams + trigrams
        candidates = list(words)
        for i in range(len(words) - 1):
            candidates.append(f"{words[i]} {words[i + 1]}")
        for i in range(len(words) - 2):
            candidates.append(f"{words[i]} {words[i + 1]} {words[i + 2]}")

        # Check each candidate against the pre-built index (exact + alias only)
        # Skip fuzzy matching here for performance — exact/alias is sufficient
        # for automated extraction from summaries
        matched = []
        seen_candidates = set()
        lookup = self.kg._entity_lookup

        for candidate in candidates:
            if candidate in seen_candidates or len(candidate) < 3:
                continue
            seen_candidates.add(candidate)

            # Normalize: spaces to underscores (matching _normalize_id behavior)
            normalized = candidate.replace(" ", "_")

            # Direct lookup (exact match or alias)
            canonical = lookup.get(normalized) or lookup.get(candidate)
            if canonical and canonical not in matched:
                matched.append(canonical)

        return matched

    def _enrich_with_graph_context(self, results: list[dict]) -> list[dict]:
        """Add graph_context field to search results based on entity matches.

        For each result, scans its summary for entity names in the knowledge
        graph, then fetches 1-hop relationships for matched entities.
        """
        if not self.kg or self.kg.graph.number_of_nodes() == 0:
            return results

        for r in results:
            summary = r.get("summary", "")
            if not summary:
                continue

            entities = self._extract_entities_from_text(summary)
            if not entities:
                continue

            relationships = []
            related_entities = set()

            for entity_id in entities:
                # Get outgoing and incoming relationships (1-hop)
                rels = self.kg.get_relationships(entity_id, direction="both")
                for rel in rels:
                    relationships.append({
                        "type": rel.get("type", rel.get("rel_type", "unknown")),
                        "target": rel.get("target") if rel.get("source") == entity_id else rel.get("source"),
                        "context": rel.get("context"),
                    })

                # Get 1-hop neighbors
                neighbors = self.kg.get_neighbors(entity_id, hops=1)
                related_entities.update(neighbors)

            # Remove entities already found from related set
            related_entities -= set(entities)

            r["graph_context"] = {
                "entities_found": entities,
                "relationships": relationships,
                "related_entities": list(related_entities),
            }

        return results

    def search_with_context(
        self,
        query: str,
        limit: int = 15,
        graph_depth: int = 1,
        source: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> list[dict]:
        """Search with knowledge graph augmentation.

        1. Runs standard search() for base results
        2. Enriches results with graph context (entity matches + relationships)
        3. Performs graph-first expansion: extracts entities from query,
           finds related entities in KG, searches for observations mentioning them
        4. Merges expanded results (marked with origin="graph_expansion")

        Args:
            query: Natural language search query
            limit: Maximum results to return
            graph_depth: How many hops to traverse for expansion (1 or 2)
            source: Filter by observation source type
            agent: Filter by agent name
        """
        # Clamp graph_depth
        graph_depth = max(1, min(2, graph_depth))

        # Step 1: Standard search
        base_results = self.search(query, limit=limit, source=source, agent=agent)

        # Step 2: Enrich with graph context
        base_results = self._enrich_with_graph_context(base_results)

        # Step 3: Graph-first expansion (if KG available)
        if not self.kg or self.kg.graph.number_of_nodes() == 0:
            return base_results

        query_entities = self._extract_entities_from_text(query)
        if not query_entities:
            return base_results

        # Find related entities via graph traversal
        expanded_entity_names = set()
        for entity_id in query_entities:
            neighbors = self.kg.get_neighbors(entity_id, hops=graph_depth)
            for neighbor in neighbors:
                # Get display name if available, else use ID
                node_data = self.kg.graph.nodes.get(neighbor, {})
                display = node_data.get("display_name", neighbor)
                expanded_entity_names.add(display)

        if not expanded_entity_names:
            return base_results

        # Search for observations mentioning related entities
        # Build a query from the expanded entity names
        existing_ids = {r.get("obs_id") or r.get("id") for r in base_results}
        expansion_results = []

        for entity_name in expanded_entity_names:
            # Search for each related entity (small limit to avoid overwhelming)
            try:
                entity_results = self.search(
                    entity_name.replace("_", " "),
                    limit=3,
                    source=source,
                    agent=agent,
                )
                for r in entity_results:
                    result_key = r.get("obs_id") or r.get("id")
                    if result_key not in existing_ids:
                        r["origin"] = "graph_expansion"
                        r["expanded_from"] = entity_name
                        # Slightly lower score for expanded results
                        r["score"] = r.get("score", 0) * 0.8
                        expansion_results.append(r)
                        existing_ids.add(result_key)
            except Exception:
                continue

        # Enrich expansion results with graph context too
        if expansion_results:
            expansion_results = self._enrich_with_graph_context(expansion_results)

        # Merge and sort
        all_results = base_results + expansion_results
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_results[:limit]

    def close(self):
        """Close database connections."""
        if self._obs_conn:
            self._obs_conn.close()
        if self._vec_conn:
            self._vec_conn.close()


# ── CLI for testing ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    retriever = MemoryRetriever()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python memory_retriever.py search <query>")
        print("  python memory_retriever.py graph-search <query>")
        print("  python memory_retriever.py timeline <obs_id>")
        print("  python memory_retriever.py details <obs_id> [obs_id2 ...]")
        print("  python memory_retriever.py recent [hours]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "search":
        query = " ".join(sys.argv[2:])
        results = retriever.search(query)
        print(f"L1 Search: '{query}' — {len(results)} results")
        for r in results:
            print(f"  [{r['id']}] {r.get('tool', r.get('collection', '?'))}: {r['summary']}")

    elif cmd == "graph-search":
        query = " ".join(sys.argv[2:])
        results = retriever.search_with_context(query)
        print(f"Graph Search: '{query}' — {len(results)} results")
        for r in results:
            origin = r.get("origin", "?")
            expanded = f" (via {r['expanded_from']})" if r.get("expanded_from") else ""
            print(f"  [{r['id']}] [{origin}]{expanded} {r['summary']}")
            gc = r.get("graph_context")
            if gc:
                if gc.get("entities_found"):
                    print(f"    entities: {', '.join(gc['entities_found'])}")
                if gc.get("relationships"):
                    for rel in gc["relationships"][:3]:
                        print(f"    -> {rel['type']}: {rel['target']}")
                if gc.get("related_entities"):
                    print(f"    related: {', '.join(gc['related_entities'][:5])}")

    elif cmd == "timeline":
        obs_id = int(sys.argv[2])
        context = retriever.timeline(obs_id)
        print(f"L2 Timeline around obs #{obs_id} — {len(context)} items")
        for r in context:
            marker = ">>>" if r.get("is_target") else "   "
            print(f"  {marker} [{r['id']}] {r.get('tool', '?')}: {r['summary']}")

    elif cmd == "details":
        obs_ids = [int(x) for x in sys.argv[2:]]
        details = retriever.get_details(obs_ids)
        print(f"L3 Details for {len(details)} observations")
        for d in details:
            print(f"\n--- Observation #{d['id']} ({d['source']}) ---")
            print(f"Tool: {d['tool_name']}")
            print(f"Input: {(d['raw_input'] or '')[:200]}")
            print(f"Output: {(d['raw_output'] or '')[:200]}")
            print(f"Summary: {d['summary']}")

    elif cmd == "recent":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 48
        results = retriever.recent_observations(hours=hours)
        print(f"Recent observations (last {hours}h): {len(results)}")
        for r in results:
            print(f"  [{r['id']}] {r.get('tool', '?')}: {r['summary']}")

    retriever.close()
