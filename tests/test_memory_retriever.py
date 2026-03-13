import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.memory_retriever import MemoryRetriever
from src.unified_vector_store import UnifiedVectorStore


def create_observations_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                tool_name TEXT,
                agent TEXT DEFAULT 'main',
                raw_input TEXT,
                raw_output TEXT,
                summary TEXT,
                status TEXT DEFAULT 'pending',
                vector_synced INTEGER DEFAULT 0
            );

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                agent TEXT DEFAULT 'main',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                user_prompt TEXT,
                summary TEXT,
                observation_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                key_decisions TEXT,
                entities_mentioned TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )

        conn.execute(
            """
            INSERT INTO sessions (id, started_at, status, user_prompt)
            VALUES (?, ?, ?, ?)
            """,
            ("session-1", "2026-03-13T12:00:00+00:00", "ended", "remember alpha"),
        )
        conn.executemany(
            """
            INSERT INTO observations (
                id, session_id, timestamp, source, tool_name, agent,
                raw_input, raw_output, summary, status, vector_synced
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "session-1",
                    "2026-03-13T12:01:00+00:00",
                    "user_prompt",
                    None,
                    "main",
                    "alpha question",
                    None,
                    "alpha investigation started",
                    "processed",
                    0,
                ),
                (
                    2,
                    "session-1",
                    "2026-03-13T12:02:00+00:00",
                    "post_tool_use",
                    "Write",
                    "main",
                    "draft",
                    "saved",
                    "alpha release checklist saved",
                    "processed",
                    1,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO session_summaries (
                session_id, summary, key_decisions, entities_mentioned
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "session-1",
                "alpha work summary",
                '["ship alpha"]',
                '["alpha"]',
            ),
        )
        conn.commit()
    finally:
        conn.close()


class MemoryRetrieverTests(unittest.TestCase):
    def test_search_timeline_and_details_use_temp_databases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            obs_db_path = Path(temp_dir) / "observations.db"
            vec_db_path = Path(temp_dir) / "vectors.db"
            create_observations_db(obs_db_path)

            store = UnifiedVectorStore(db_path=vec_db_path)
            self.addCleanup(store.close)
            store.add_knowledge("manual-1", "alpha knowledge note", {"kind": "manual"})

            retriever = MemoryRetriever(obs_db_path=obs_db_path, vec_db_path=vec_db_path)
            self.addCleanup(lambda: retriever._obs_conn and retriever._obs_conn.close())
            self.addCleanup(lambda: retriever._vec_conn and retriever._vec_conn.close())

            results = retriever.search("alpha", limit=10)
            timeline = retriever.timeline(2, window=1)
            details = retriever.get_details([2])

            origins = {result["origin"] for result in results}

            self.assertIn("observations", origins)
            self.assertIn("vector_store", origins)
            self.assertEqual([item["id"] for item in timeline], [1, 2])
            self.assertTrue(timeline[-1]["is_target"])
            self.assertEqual(details[0]["tool_name"], "Write")
            self.assertEqual(details[0]["raw_output"], "saved")
