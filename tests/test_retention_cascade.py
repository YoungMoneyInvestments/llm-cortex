"""
BUG-C1-06: RetentionManager._chunked_delete must cascade-clean memcells.

Tests:
  - Orphaned IDs are removed from memcells.observation_ids after deletion.
  - A memcell whose last live observation is deleted is itself deleted.
  - A memcell that keeps at least one live observation is preserved.
  - The cascade is correctly scoped: non-deleted IDs in other memcells are untouched.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(path: Path) -> sqlite3.Connection:
    """Create a minimal cortex-observations.db schema in tmp_path."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
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
            vector_synced INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            processed_at TEXT,
            subscription_tier TEXT DEFAULT 'claude_standard',
            memory_type TEXT,
            entities TEXT
        );
        CREATE TABLE IF NOT EXISTS memcells (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            observation_ids TEXT NOT NULL,
            summary TEXT,
            chunk_type TEXT DEFAULT 'auto',
            timestamp TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS foresight (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            prediction TEXT NOT NULL,
            evidence TEXT,
            valid_until TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            used INTEGER DEFAULT 0
        );
        """
    )
    conn.commit()
    return conn


def _insert_obs(conn: sqlite3.Connection, obs_id: int, session: str = "sess1") -> int:
    conn.execute(
        "INSERT INTO observations (id, session_id, timestamp, source, status) "
        "VALUES (?, ?, datetime('now'), 'post_tool_use', 'processed')",
        (obs_id, session),
    )
    conn.commit()
    return obs_id


def _insert_memcell(conn: sqlite3.Connection, obs_ids: list[int], session: str = "sess1") -> int:
    cur = conn.execute(
        "INSERT INTO memcells (session_id, observation_ids, timestamp) VALUES (?, ?, datetime('now'))",
        (session, json.dumps(obs_ids)),
    )
    conn.commit()
    return cur.lastrowid


def _get_mc_obs_ids(conn: sqlite3.Connection, mc_id: int) -> list[int] | None:
    row = conn.execute("SELECT observation_ids FROM memcells WHERE id = ?", (mc_id,)).fetchone()
    if row is None:
        return None
    return json.loads(row["observation_ids"])


# ---------------------------------------------------------------------------
# Fixture: RetentionManager wired to a temp DB
# ---------------------------------------------------------------------------

@pytest.fixture()
def retention_env(tmp_path, monkeypatch):
    """Provide a RetentionManager instance backed by a fresh temp database."""
    import memory_worker as mw

    # Python 3.9: asyncio.Lock() requires a running event loop at construction time.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    db_path = tmp_path / "test_obs.db"
    test_conn = _fresh_db(db_path)
    test_lock = asyncio.Lock()

    monkeypatch.setattr(mw, "db", test_conn)
    monkeypatch.setattr(mw, "db_lock", test_lock)
    monkeypatch.setattr(mw, "DB_PATH", db_path)

    mgr = mw.RetentionManager()
    yield mgr, test_conn

    test_conn.close()
    loop.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cascade_removes_deleted_id_from_mixed_memcell(retention_env):
    """After deleting obs 1, memcell [1, 2] becomes [2] (obs 2 still lives)."""
    mgr, conn = retention_env

    _insert_obs(conn, 1)
    _insert_obs(conn, 2)
    mc_id = _insert_memcell(conn, [1, 2])

    asyncio.run(mgr._chunked_delete([1]))

    remaining = _get_mc_obs_ids(conn, mc_id)
    assert remaining == [2], f"Expected [2] but got {remaining}"
    # obs 1 is gone from observations
    assert conn.execute("SELECT id FROM observations WHERE id = 1").fetchone() is None
    # obs 2 still exists
    assert conn.execute("SELECT id FROM observations WHERE id = 2").fetchone() is not None


def test_cascade_deletes_empty_memcell(retention_env):
    """After deleting the only observation in a memcell, the memcell row is deleted."""
    mgr, conn = retention_env

    _insert_obs(conn, 10)
    mc_id = _insert_memcell(conn, [10])

    asyncio.run(mgr._chunked_delete([10]))

    # memcell row must be gone
    assert _get_mc_obs_ids(conn, mc_id) is None, "Memcell should have been deleted"
    # observation must be gone too
    assert conn.execute("SELECT id FROM observations WHERE id = 10").fetchone() is None


def test_cascade_leaves_unrelated_memcell_intact(retention_env):
    """Deleting obs 5 must not touch a memcell that only references obs 6 and 7."""
    mgr, conn = retention_env

    _insert_obs(conn, 5)
    _insert_obs(conn, 6)
    _insert_obs(conn, 7)
    mc_target = _insert_memcell(conn, [5])      # will be deleted
    mc_intact = _insert_memcell(conn, [6, 7])   # must survive unchanged

    asyncio.run(mgr._chunked_delete([5]))

    assert _get_mc_obs_ids(conn, mc_target) is None, "Targeted memcell should be deleted"
    assert _get_mc_obs_ids(conn, mc_intact) == [6, 7], "Unrelated memcell must be unchanged"


def test_cascade_handles_already_dead_ids_in_memcell(retention_env):
    """
    A memcell referencing [100, 200] where 100 never existed must not crash;
    after deleting obs 200, the row should be removed (no live IDs remain).
    """
    mgr, conn = retention_env

    # Only 200 exists in observations; 100 is already a ghost.
    _insert_obs(conn, 200)
    mc_id = _insert_memcell(conn, [100, 200])

    asyncio.run(mgr._chunked_delete([200]))

    # Both IDs are now dead — memcell must be removed.
    assert _get_mc_obs_ids(conn, mc_id) is None, "Memcell with all-dead IDs should be deleted"
