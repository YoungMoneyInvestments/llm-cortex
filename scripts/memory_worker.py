#!/usr/bin/env python3
"""
Memory Worker Service — Background observation processor (Layer 0)

Receives observations from lifecycle hooks (PostToolUse, UserPromptSubmit,
SessionEnd) via HTTP, queues them in SQLite, and processes them asynchronously
(summarization, vector embedding, knowledge graph extraction).

Usage:
    python memory_worker.py              # Start on default port
    CORTEX_WORKER_PORT=9100 python memory_worker.py  # Custom port

Configure:
    CORTEX_WORKSPACE    — Project root (default: ~/cortex)
    CORTEX_WORKER_PORT  — HTTP port (default: 7778)

Dependencies: pip install fastapi uvicorn pydantic
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
WORKER_PORT = int(os.environ.get("CORTEX_WORKER_PORT", "7778"))
DATA_DIR = WORKSPACE / "data"
DB_PATH = DATA_DIR / "cortex-observations.db"
PID_FILE = WORKSPACE / ".worker.pid"
LOG_DIR = WORKSPACE / "logs"
LOG_FILE = LOG_DIR / "memory-worker.log"

# How often to process pending observations (seconds)
PROCESS_INTERVAL = 5

# ── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("cortex-worker")

# ── Database ────────────────────────────────────────────────────────────────


def init_db() -> sqlite3.Connection:
    """Initialize SQLite database with required tables."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,          -- 'post_tool_use', 'user_prompt', 'session_end'
            tool_name TEXT,                -- tool name for post_tool_use
            agent TEXT DEFAULT 'main',     -- which agent generated this
            raw_input TEXT,                -- raw input (truncated for size)
            raw_output TEXT,               -- raw output (truncated for size)
            summary TEXT,                  -- compressed summary (filled async)
            status TEXT DEFAULT 'pending', -- 'pending', 'processed', 'failed'
            vector_synced INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            agent TEXT DEFAULT 'main',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            user_prompt TEXT,              -- first user prompt
            summary TEXT,                  -- session summary (filled at end)
            observation_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'   -- 'active', 'ended', 'summarized'
        );

        CREATE TABLE IF NOT EXISTS session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            key_decisions TEXT,            -- JSON array
            entities_mentioned TEXT,        -- JSON array
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
        CREATE INDEX IF NOT EXISTS idx_obs_status ON observations(status);
        CREATE INDEX IF NOT EXISTS idx_obs_timestamp ON observations(timestamp);
        CREATE INDEX IF NOT EXISTS idx_obs_tool ON observations(tool_name);
        CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
    """)
    conn.commit()
    return conn


# ── Global state ────────────────────────────────────────────────────────────

db: Optional[sqlite3.Connection] = None
processor_task: Optional[asyncio.Task] = None
shutdown_event = asyncio.Event()

# ── Pydantic models ─────────────────────────────────────────────────────────


class ObservationRequest(BaseModel):
    """Observation from a hook."""
    session_id: str
    source: str  # 'post_tool_use', 'user_prompt', 'session_end'
    tool_name: Optional[str] = None
    agent: str = "main"
    raw_input: Optional[str] = None
    raw_output: Optional[str] = None

    # Truncation limits to avoid bloating the DB
    MAX_INPUT_LEN: int = Field(default=4000, exclude=True)
    MAX_OUTPUT_LEN: int = Field(default=8000, exclude=True)

    def truncated_input(self) -> Optional[str]:
        if self.raw_input and len(self.raw_input) > self.MAX_INPUT_LEN:
            return self.raw_input[:self.MAX_INPUT_LEN] + "\n... [truncated]"
        return self.raw_input

    def truncated_output(self) -> Optional[str]:
        if self.raw_output and len(self.raw_output) > self.MAX_OUTPUT_LEN:
            return self.raw_output[:self.MAX_OUTPUT_LEN] + "\n... [truncated]"
        return self.raw_output


class SessionStartRequest(BaseModel):
    """Register a new session."""
    session_id: str
    agent: str = "main"
    user_prompt: Optional[str] = None


class SessionEndRequest(BaseModel):
    """End a session and trigger summarization."""
    session_id: str


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    pending_observations: int
    total_observations: int
    active_sessions: int


# ── Background processor ───────────────────────────────────────────────────


async def process_pending_observations():
    """Background task: process pending observations.

    Currently generates rule-based summaries from raw data.
    Future: AI compression, vector embedding, graph extraction.
    """
    while not shutdown_event.is_set():
        try:
            if db is None:
                await asyncio.sleep(PROCESS_INTERVAL)
                continue

            # Get pending observations
            rows = db.execute(
                "SELECT id, source, tool_name, agent, raw_input, raw_output "
                "FROM observations WHERE status = 'pending' ORDER BY id LIMIT 20"
            ).fetchall()

            for row in rows:
                obs_id = row["id"]
                try:
                    summary = _generate_summary(
                        source=row["source"],
                        tool_name=row["tool_name"],
                        agent=row["agent"],
                        raw_input=row["raw_input"],
                        raw_output=row["raw_output"],
                    )

                    db.execute(
                        "UPDATE observations SET summary = ?, status = 'processed', "
                        "processed_at = ? WHERE id = ?",
                        (summary, datetime.now(timezone.utc).isoformat(), obs_id),
                    )
                    db.commit()

                    # Sync to vector store (if available)
                    await _sync_to_vector_store(obs_id, summary, row)

                except Exception as e:
                    logger.error(f"Failed to process observation {obs_id}: {e}")
                    db.execute(
                        "UPDATE observations SET status = 'failed' WHERE id = ?",
                        (obs_id,),
                    )
                    db.commit()

        except Exception as e:
            logger.error(f"Processor loop error: {e}")

        await asyncio.sleep(PROCESS_INTERVAL)


def _generate_summary(
    source: str,
    tool_name: Optional[str],
    agent: str,
    raw_input: Optional[str],
    raw_output: Optional[str],
) -> str:
    """Generate a concise summary from raw observation data.

    Phase 1: Rule-based extraction (fast, zero-cost).
    Phase 2 (future): AI compression via LLM call.
    """
    parts = []

    if source == "post_tool_use":
        if tool_name:
            parts.append(f"[{agent}] Used tool: {tool_name}")
        if raw_input:
            input_preview = raw_input[:200].replace("\n", " ").strip()
            parts.append(f"Input: {input_preview}")
        if raw_output:
            output_preview = raw_output[:300].replace("\n", " ").strip()
            parts.append(f"Result: {output_preview}")

    elif source == "user_prompt":
        if raw_input:
            parts.append(f"[User] {raw_input[:500]}")

    elif source == "session_end":
        parts.append(f"[{agent}] Session ended")
        if raw_input:
            parts.append(raw_input[:500])

    return " | ".join(parts) if parts else f"[{agent}] {source} observation"


async def _sync_to_vector_store(obs_id: int, summary: str, row: sqlite3.Row):
    """Sync processed observation to the vector store.

    Imports unified_vector_store lazily to avoid startup dependency.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from unified_vector_store import get_vector_store

        store = get_vector_store()
        store.add_observation(
            obs_id=str(obs_id),
            text=summary,
            metadata={
                "source": row["source"],
                "tool_name": row["tool_name"] or "",
                "agent": row["agent"] or "main",
            },
        )

        db.execute(
            "UPDATE observations SET vector_synced = 1 WHERE id = ?",
            (obs_id,),
        )
        db.commit()

    except ImportError:
        pass  # Vector store not installed — skip silently
    except Exception as e:
        logger.warning(f"Vector sync failed for obs {obs_id}: {e}")


# ── FastAPI app ─────────────────────────────────────────────────────────────

start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage worker lifecycle."""
    global db, processor_task

    logger.info(f"Cortex Worker starting on port {WORKER_PORT}")

    # Write PID file
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Initialize database
    db = init_db()
    logger.info(f"Database initialized at {DB_PATH}")

    # Start background processor
    processor_task = asyncio.create_task(process_pending_observations())
    logger.info("Background processor started")

    yield

    # Shutdown
    logger.info("Shutting down...")
    shutdown_event.set()
    if processor_task:
        processor_task.cancel()
        try:
            await processor_task
        except asyncio.CancelledError:
            pass

    if db:
        db.close()

    if PID_FILE.exists():
        PID_FILE.unlink()

    logger.info("Cortex Worker stopped")


app = FastAPI(
    title="Cortex Memory Worker",
    description="Background observation processor for Claude Cortex",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    pending = db.execute(
        "SELECT COUNT(*) as c FROM observations WHERE status = 'pending'"
    ).fetchone()["c"]
    total = db.execute("SELECT COUNT(*) as c FROM observations").fetchone()["c"]
    active = db.execute(
        "SELECT COUNT(*) as c FROM sessions WHERE status = 'active'"
    ).fetchone()["c"]

    return HealthResponse(
        status="healthy",
        uptime_seconds=round(time.time() - start_time, 1),
        pending_observations=pending,
        total_observations=total,
        active_sessions=active,
    )


@app.post("/api/observations")
async def receive_observation(req: ObservationRequest):
    """Receive an observation from a hook. Returns immediately."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO observations (session_id, timestamp, source, tool_name, "
        "agent, raw_input, raw_output) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            req.session_id,
            now,
            req.source,
            req.tool_name,
            req.agent,
            req.truncated_input(),
            req.truncated_output(),
        ),
    )

    # Update session observation count
    db.execute(
        "UPDATE sessions SET observation_count = observation_count + 1 "
        "WHERE id = ?",
        (req.session_id,),
    )
    db.commit()

    logger.info(
        f"Observation received: session={req.session_id[:8]}... "
        f"source={req.source} tool={req.tool_name}"
    )
    return {"status": "queued"}


@app.post("/api/sessions/start")
async def start_session(req: SessionStartRequest):
    """Register a new session."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO sessions (id, agent, started_at, user_prompt) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET user_prompt = excluded.user_prompt",
        (req.session_id, req.agent, now, req.user_prompt),
    )
    db.commit()

    logger.info(f"Session started: {req.session_id[:8]}... agent={req.agent}")
    return {"status": "started", "session_id": req.session_id}


@app.post("/api/sessions/end")
async def end_session(req: SessionEndRequest):
    """End a session and mark for summarization."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "UPDATE sessions SET ended_at = ?, status = 'ended' WHERE id = ?",
        (now, req.session_id),
    )
    db.commit()

    count = db.execute(
        "SELECT COUNT(*) as c FROM observations WHERE session_id = ?",
        (req.session_id,),
    ).fetchone()["c"]

    logger.info(
        f"Session ended: {req.session_id[:8]}... observations={count}"
    )
    return {"status": "ended", "observation_count": count}


@app.get("/api/observations/recent")
async def get_recent_observations(
    limit: int = 20,
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
):
    """Get recent observations with optional filters."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    query = "SELECT * FROM observations WHERE 1=1"
    params = []

    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    if source:
        query += " AND source = ?"
        params.append(source)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(query, params).fetchall()
    return {"observations": [dict(r) for r in rows]}


@app.get("/api/sessions/recent")
async def get_recent_sessions(limit: int = 10):
    """Get recent sessions."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    rows = db.execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/api/stats")
async def get_stats():
    """Get worker statistics."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    stats = {}
    stats["total_observations"] = db.execute(
        "SELECT COUNT(*) as c FROM observations"
    ).fetchone()["c"]
    stats["pending_observations"] = db.execute(
        "SELECT COUNT(*) as c FROM observations WHERE status = 'pending'"
    ).fetchone()["c"]
    stats["processed_observations"] = db.execute(
        "SELECT COUNT(*) as c FROM observations WHERE status = 'processed'"
    ).fetchone()["c"]
    stats["failed_observations"] = db.execute(
        "SELECT COUNT(*) as c FROM observations WHERE status = 'failed'"
    ).fetchone()["c"]
    stats["vector_synced"] = db.execute(
        "SELECT COUNT(*) as c FROM observations WHERE vector_synced = 1"
    ).fetchone()["c"]
    stats["total_sessions"] = db.execute(
        "SELECT COUNT(*) as c FROM sessions"
    ).fetchone()["c"]
    stats["active_sessions"] = db.execute(
        "SELECT COUNT(*) as c FROM sessions WHERE status = 'active'"
    ).fetchone()["c"]

    # Top tools
    tool_rows = db.execute(
        "SELECT tool_name, COUNT(*) as c FROM observations "
        "WHERE tool_name IS NOT NULL GROUP BY tool_name ORDER BY c DESC LIMIT 10"
    ).fetchall()
    stats["top_tools"] = {r["tool_name"]: r["c"] for r in tool_rows}

    return stats


# ── Signal handling ─────────────────────────────────────────────────────────


def handle_signal(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "memory_worker:app",
        host="127.0.0.1",
        port=WORKER_PORT,
        log_level="info",
        access_log=False,
    )
