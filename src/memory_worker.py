#!/usr/bin/env python3
"""
Memory Worker Service — Background observation processor for Cortex.

Receives observations from lifecycle hooks (PostToolUse, UserPromptSubmit,
SessionEnd) via HTTP, queues them in SQLite, and processes them asynchronously
(AI compression, vector embedding, knowledge graph extraction).

Inspired by claude-mem's async worker pattern, adapted for Cami's Python stack.

Usage:
    # Start the worker
    python memory_worker.py

    # Or via the launcher script
    ./start_worker.sh

Port: 37778 (37777 reserved for claude-mem)
"""

import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
import uvicorn

# Ensure scripts dir is on path for memory_retriever
sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_retriever import MemoryRetriever
from subscription import DEFAULT_TIER, get_tier_config, parse_tier

# ── Config ──────────────────────────────────────────────────────────────────

WORKER_PORT = 37778
DATA_DIR = Path.home() / "clawd" / "data"
DB_PATH = DATA_DIR / "cortex-observations.db"
# Canonical vector DB lives in ~/.cortex/data — shared singleton path used by
# unified_vector_store.py module default (CORTEX_DATA_DIR env var, else ~/.cortex/data).
# Keep separate from DATA_DIR to avoid split-brain: observations stay in clawd/data,
# vectors stay in .cortex/data. (BUG-006 fix, Pass 5)
VEC_DATA_DIR = Path(
    os.environ.get("CORTEX_DATA_DIR", str(Path.home() / ".cortex" / "data"))
)
PID_FILE = Path.home() / ".openclaw" / "worker.pid"
LOG_DIR = Path.home() / ".openclaw" / "logs"
LOG_FILE = LOG_DIR / "memory-worker.log"

# How often to process pending observations (seconds)
PROCESS_INTERVAL = 5

# ── Retention config ─────────────────────────────────────────────────────
RETENTION_INTERVAL = 3600          # Run cleanup every hour
RETENTION_BATCH_SIZE = 500         # Rows per DB operation chunk
RETENTION_FULL_DAYS = 7            # Keep everything
RETENTION_TRIM_DAYS = 30           # Trim raw fields for high-signal

HIGH_SIGNAL_TOOLS = frozenset({
    "WebSearch", "WebFetch", "Write", "Edit", "Task",
    "mcp__plugin_episodic-memory_episodic-memory__search",
    "mcp__plugin_episodic-memory_episodic-memory__read",
})

# ── API Authentication config ──────────────────────────────────────────
CORTEX_API_KEY = os.environ.get("CORTEX_WORKER_API_KEY", "")

# ── Rate limiting config ───────────────────────────────────────────────
RATE_LIMIT_MAX = 100           # max observations per minute per session_id
RATE_LIMIT_WINDOW = 60         # window in seconds
RATE_LIMIT_CLEANUP_INTERVAL = 300  # cleanup old entries every 5 minutes
DEFAULT_SUBSCRIPTION_TIER = os.environ.get("CORTEX_SUBSCRIPTION_TIER", DEFAULT_TIER.value)

# ── AI compression config ────────────────────────────────────────────
AI_MODEL = "claude-sonnet-4-6"
AI_MAX_TOKENS = 1024
AI_COMPRESSION_DELAY = 0.1       # seconds between API calls (rate control)
AI_FAILURE_ALERT_THRESHOLD = 3   # consecutive failures before alerting
AI_BACKOFF_BASE = 30             # base backoff seconds after rate limit
AI_BACKOFF_MAX = 600             # max backoff seconds (10 minutes)
AI_RECOVERY_PROBE_INTERVAL = 120 # seconds between health probes when degraded
AUTH_PROFILES_PATH = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"

# ── NER (Named Entity Recognition) config ────────────────────────────────
NER_ENABLED = os.environ.get("CORTEX_NER_ENABLED", "true").lower() == "true"


# ── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

# Configure loggers ONCE — guard against re-import by uvicorn.
# NOTE: start_worker.sh redirects stdout/stderr >> LOG_FILE, so we only
# need the FileHandler here. StreamHandler would cause duplicates.
logger = logging.getLogger("cortex-worker")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _fh = logging.FileHandler(LOG_FILE)
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
    logger.propagate = False

    vec_logger = logging.getLogger("cortex-vectors")
    vec_logger.setLevel(logging.INFO)
    vec_logger.addHandler(_fh)
    vec_logger.propagate = False

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
            summary TEXT,                  -- AI-compressed summary (filled async)
            status TEXT DEFAULT 'pending', -- 'pending', 'processed', 'failed'
            vector_synced INTEGER DEFAULT 0,
            subscription_tier TEXT DEFAULT 'claude_standard',
            created_at TEXT DEFAULT (datetime('now')),
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            agent TEXT DEFAULT 'main',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            user_prompt TEXT,              -- first user prompt
            summary TEXT,                  -- AI session summary (filled at end)
            observation_count INTEGER DEFAULT 0,
            subscription_tier TEXT DEFAULT 'claude_standard',
            status TEXT DEFAULT 'active'   -- 'active', 'ended', 'summarized'
        );

        CREATE TABLE IF NOT EXISTS quota_usage (
            usage_date TEXT NOT NULL,
            subscription_tier TEXT NOT NULL,
            observations_count INTEGER DEFAULT 0,
            estimated_tokens INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (usage_date, subscription_tier)
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

        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,  -- 'expertise', 'preference', 'style', 'context'
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(category, key) ON CONFLICT REPLACE
        );

        CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
        CREATE INDEX IF NOT EXISTS idx_obs_status ON observations(status);
        CREATE INDEX IF NOT EXISTS idx_obs_timestamp ON observations(timestamp);
        CREATE INDEX IF NOT EXISTS idx_obs_tool ON observations(tool_name);
        CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
        CREATE INDEX IF NOT EXISTS idx_profile_category ON profile(category);

        CREATE TABLE IF NOT EXISTS memcells (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            observation_ids TEXT NOT NULL,
            summary TEXT,
            chunk_type TEXT DEFAULT 'auto',
            timestamp TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_memcells_session ON memcells(session_id);

        CREATE TABLE IF NOT EXISTS foresight (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            prediction TEXT NOT NULL,
            evidence TEXT,
            valid_until TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            used INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_foresight_valid ON foresight(valid_until);
    """)

    # Backward-compatible migrations for existing DBs
    _ensure_column_exists(conn, "observations", "subscription_tier", "TEXT DEFAULT 'claude_standard'")
    _ensure_column_exists(conn, "sessions", "subscription_tier", "TEXT DEFAULT 'claude_standard'")
    conn.commit()

    # Tier indexes must come AFTER the ALTER TABLE migrations above
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_obs_tier ON observations(subscription_tier)",
        "CREATE INDEX IF NOT EXISTS idx_quota_tier ON quota_usage(subscription_tier, usage_date)",
    ):
        try:
            conn.execute(idx_sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()

    # Add memory_type and entities columns to observations if not present.
    # SQLite does not support IF NOT EXISTS on ALTER TABLE — use try/except.
    for col_def in (
        "ALTER TABLE observations ADD COLUMN memory_type TEXT",
        "ALTER TABLE observations ADD COLUMN entities TEXT",
    ):
        try:
            conn.execute(col_def)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    return conn


def _ensure_column_exists(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add column to an existing table when the column is missing."""
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ── Global state ────────────────────────────────────────────────────────────

db: Optional[sqlite3.Connection] = None
db_lock: Optional[asyncio.Lock] = None  # serialize DB access across async tasks
processor_task: Optional[asyncio.Task] = None
retention_task: Optional[asyncio.Task] = None
summarization_task: Optional[asyncio.Task] = None
retention_manager: Optional["RetentionManager"] = None
ai_compressor: Optional["AICompressor"] = None
shutdown_event = asyncio.Event()

# Rate limiter: {session_id: deque of timestamps}
_rate_limit_buckets: dict[str, deque] = {}
_rate_limit_last_cleanup: float = 0.0
quota_manager: Optional["QuotaManager"] = None


# ── API Authentication ─────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
):
    """Dependency that enforces bearer token auth on POST endpoints."""
    if credentials is None or credentials.credentials != CORTEX_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing Authorization: Bearer <key> header",
        )
    return credentials


# ── Rate Limiting ──────────────────────────────────────────────────────────


def _check_rate_limit(session_id: str) -> bool:
    """Check if session_id is within rate limit. Returns True if allowed."""
    global _rate_limit_last_cleanup

    now = time.monotonic()

    # Periodic cleanup of stale buckets
    if now - _rate_limit_last_cleanup > RATE_LIMIT_CLEANUP_INTERVAL:
        _rate_limit_last_cleanup = now
        cutoff = now - RATE_LIMIT_WINDOW
        stale = [k for k, v in _rate_limit_buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _rate_limit_buckets[k]

    bucket = _rate_limit_buckets.get(session_id)
    if bucket is None:
        bucket = deque()
        _rate_limit_buckets[session_id] = bucket

    # Evict timestamps outside the window
    cutoff = now - RATE_LIMIT_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= RATE_LIMIT_MAX:
        return False

    bucket.append(now)
    return True


class SubscriptionRateLimiter:
    """Per-tier and per-session in-memory rate limiter."""

    def __init__(self):
        self._tier_buckets: dict[str, deque] = {}
        self._session_buckets: dict[str, deque] = {}
        self._last_cleanup: float = 0.0

    def _prune(self, bucket: deque, cutoff: float):
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _bucket_for(self, storage: dict[str, deque], key: str) -> deque:
        bucket = storage.get(key)
        if bucket is None:
            bucket = deque()
            storage[key] = bucket
        return bucket

    def check(self, session_id: str, tier: str) -> tuple[bool, str]:
        now = time.monotonic()
        if now - self._last_cleanup > RATE_LIMIT_CLEANUP_INTERVAL:
            self._cleanup(now)
            self._last_cleanup = now

        cutoff = now - RATE_LIMIT_WINDOW
        tier_cfg = get_tier_config(tier)

        session_bucket = self._bucket_for(self._session_buckets, session_id)
        self._prune(session_bucket, cutoff)
        if len(session_bucket) >= RATE_LIMIT_MAX:
            return False, (
                f"session rate limit exceeded: max {RATE_LIMIT_MAX} observations "
                f"per {RATE_LIMIT_WINDOW}s"
            )

        tier_bucket = self._bucket_for(self._tier_buckets, tier_cfg.tier.value)
        self._prune(tier_bucket, cutoff)
        if len(tier_bucket) >= tier_cfg.limits.observations_per_minute:
            return False, (
                f"subscription tier {tier_cfg.tier.value} exhausted: max "
                f"{tier_cfg.limits.observations_per_minute} observations per minute"
            )

        session_bucket.append(now)
        tier_bucket.append(now)
        return True, ""

    def _cleanup(self, now: float):
        cutoff = now - RATE_LIMIT_WINDOW
        for buckets in (self._session_buckets, self._tier_buckets):
            stale = [k for k, v in buckets.items() if not v or v[-1] < cutoff]
            for key in stale:
                del buckets[key]


class QuotaManager:
    """Tracks per-tier daily estimated token usage."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def estimate_tokens(raw_input: Optional[str], raw_output: Optional[str]) -> int:
        # lightweight estimation to avoid expensive tokenization in hot path
        chars = len(raw_input or "") + len(raw_output or "")
        return max(1, chars // 4)

    def consume(self, tier: str, raw_input: Optional[str], raw_output: Optional[str]) -> tuple[bool, int, int]:
        tier_cfg = get_tier_config(tier)
        budget = tier_cfg.limits.daily_token_budget
        tokens = self.estimate_tokens(raw_input, raw_output)
        usage_date = self._today()
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO quota_usage (usage_date, subscription_tier, observations_count, "
            "estimated_tokens, updated_at) VALUES (?, ?, 0, 0, ?) "
            "ON CONFLICT(usage_date, subscription_tier) DO NOTHING",
            (usage_date, tier_cfg.tier.value, now),
        )
        row = self._conn.execute(
            "SELECT estimated_tokens FROM quota_usage WHERE usage_date = ? AND subscription_tier = ?",
            (usage_date, tier_cfg.tier.value),
        ).fetchone()
        used = int(row["estimated_tokens"]) if row else 0
        if used + tokens > budget:
            return False, used, budget

        self._conn.execute(
            "UPDATE quota_usage SET observations_count = observations_count + 1, "
            "estimated_tokens = estimated_tokens + ?, updated_at = ? "
            "WHERE usage_date = ? AND subscription_tier = ?",
            (tokens, now, usage_date, tier_cfg.tier.value),
        )
        return True, used + tokens, budget


subscription_rate_limiter = SubscriptionRateLimiter()

# ── Pydantic models ─────────────────────────────────────────────────────────


class ObservationRequest(BaseModel):
    """Observation from a hook."""
    session_id: str
    source: str  # 'post_tool_use', 'user_prompt', 'session_end'
    tool_name: Optional[str] = None
    agent: str = "main"
    subscription_tier: str = DEFAULT_SUBSCRIPTION_TIER
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
    subscription_tier: str = DEFAULT_SUBSCRIPTION_TIER
    user_prompt: Optional[str] = None


class SessionEndRequest(BaseModel):
    """End a session and trigger summarization."""
    session_id: str


class MemorySearchRequest(BaseModel):
    """Cortex memory search (L1)."""
    query: str
    limit: Optional[int] = 15
    source: Optional[str] = None
    agent: Optional[str] = None


class MemoryTimelineRequest(BaseModel):
    """Cortex memory timeline (L2) around an observation."""
    observation_id: int
    window: Optional[int] = 5


class MemoryDetailsRequest(BaseModel):
    """Cortex memory full details (L3)."""
    observation_ids: List[int]


class MemorySaveRequest(BaseModel):
    """Save an explicit memory for future retrieval."""
    content: str
    tags: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    pending_observations: int
    total_observations: int
    active_sessions: int


# ── MemCell boundary detection ────────────────────────────────────────────────

# Tool categories for topic-shift detection
_TOOL_CATEGORY_MAP = {
    "Read": "read", "Glob": "read", "Grep": "read",
    "Edit": "write", "Write": "write",
    "Bash": "exec",
    "WebFetch": "web", "WebSearch": "web",
    "Task": "task",
}

_MEMCELL_TIME_WINDOW = 90   # seconds — observations within this gap stay together
_MEMCELL_MAX_SIZE = 5       # max observations per chunk


def _tool_category(tool_name: Optional[str]) -> str:
    """Map a tool name to a broad category for topic-shift detection."""
    if tool_name is None:
        return "other"
    # Handle MCP tools (mcp__server__tool) as their own category
    if tool_name.startswith("mcp__"):
        return "mcp"
    return _TOOL_CATEGORY_MAP.get(tool_name, "other")


def _detect_topic_boundaries(observations: list[dict]) -> list[list[dict]]:
    """Group raw observations into coherent topic chunks before AI compression.

    Rules:
    - Observations within 90 seconds of each other stay in the same chunk.
    - Max 5 observations per chunk.
    - A new chunk starts when the tool category changes significantly
      (e.g., read → exec, write → web).

    Args:
        observations: List of dicts with at least 'timestamp', 'tool_name' keys.

    Returns:
        List of chunks, each chunk being a list of observation dicts.
    """
    if not observations:
        return []

    chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    prev_ts: Optional[float] = None
    prev_category: Optional[str] = None

    for obs in observations:
        # Parse timestamp to epoch float for gap calculation
        try:
            ts_str = obs.get("timestamp", "")
            # Handle both 'Z' and '+00:00' suffixes
            ts_str_clean = ts_str.replace("Z", "+00:00")
            ts_epoch = datetime.fromisoformat(ts_str_clean).timestamp()
        except (ValueError, TypeError):
            ts_epoch = None

        category = _tool_category(obs.get("tool_name"))

        # Decide whether to start a new chunk
        start_new = False
        if len(current_chunk) >= _MEMCELL_MAX_SIZE:
            start_new = True
        elif prev_ts is not None and ts_epoch is not None:
            gap = ts_epoch - prev_ts
            if gap > _MEMCELL_TIME_WINDOW:
                start_new = True
            elif prev_category is not None and category != prev_category:
                # Topic shift: different tool category
                meaningful_categories = {"read", "write", "exec", "web", "task", "mcp"}
                if category in meaningful_categories and prev_category in meaningful_categories:
                    start_new = True

        if start_new and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []

        current_chunk.append(obs)
        prev_ts = ts_epoch if ts_epoch is not None else prev_ts
        prev_category = category

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


# ── Foresight extraction ──────────────────────────────────────────────────────


async def _extract_foresight(
    session_id: str,
    summary: str,
    compressor: "AICompressor",
) -> Optional[dict]:
    """Make a second AI call to extract time-bounded predictions from a chunk summary.

    Returns parsed foresight dict or None if the call fails or yields nothing.
    """
    if not summary or not summary.strip():
        return None

    prompt = (
        "Given this work summary, what will this user likely need in future sessions? "
        "Extract 0-2 time-bounded predictions. Return JSON only:\n"
        "{\"foresight\": [{\"prediction\": \"...\", \"evidence\": \"...\", \"valid_days\": 7}]}\n"
        "If no strong predictions, return {\"foresight\": []}.\n\n"
        f"Summary: {summary}"
    )

    try:
        raw = await _call_ai_for_summary(prompt)
        if not raw:
            return None

        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)

        parsed = json.loads(clean)
        return parsed
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug(f"Foresight parse failed: {e}")
        return None
    except Exception as e:
        logger.debug(f"Foresight extraction failed: {e}")
        return None


async def _store_foresight(session_id: str, foresight_data: dict):
    """Persist foresight predictions to the foresight table."""
    if db is None or db_lock is None:
        return

    predictions = foresight_data.get("foresight", [])
    if not predictions:
        return

    now = datetime.now(timezone.utc)
    rows_to_insert = []
    for item in predictions:
        prediction = item.get("prediction", "").strip()
        if not prediction:
            continue
        evidence = item.get("evidence", "")
        valid_days = int(item.get("valid_days", 7))
        valid_until = (now + timedelta(days=valid_days)).date().isoformat()
        rows_to_insert.append((session_id, prediction, evidence, valid_until))

    if not rows_to_insert:
        return

    async with db_lock:
        db.executemany(
            "INSERT INTO foresight (session_id, prediction, evidence, valid_until) "
            "VALUES (?, ?, ?, ?)",
            rows_to_insert,
        )
        db.commit()

    logger.debug(f"Stored {len(rows_to_insert)} foresight prediction(s) for session {session_id[:8]}...")


# ── Background processor ───────────────────────────────────────────────────


async def process_pending_observations():
    """Background task: process pending observations in coherent topic chunks.

    Fetches pending observations, groups them into MemCell chunks via
    _detect_topic_boundaries(), then compresses each chunk together.
    The resulting summary is written to all observations in the chunk and
    a memcells row is inserted.  Foresight extraction runs after each chunk.
    """
    consecutive_errors = 0
    while not shutdown_event.is_set():
        try:
            if db is None or db_lock is None:
                await asyncio.sleep(PROCESS_INTERVAL)
                continue

            # Fetch pending observations — include session_id and timestamp for chunking
            async with db_lock:
                rows = db.execute(
                    "SELECT id, session_id, timestamp, source, tool_name, agent, "
                    "raw_input, raw_output "
                    "FROM observations WHERE status = 'pending' ORDER BY id LIMIT 20"
                ).fetchall()

            if not rows:
                consecutive_errors = 0
                await asyncio.sleep(PROCESS_INTERVAL)
                continue

            # Convert sqlite3.Row → plain dicts so _detect_topic_boundaries can use them
            obs_dicts = [dict(r) for r in rows]

            # Group into coherent topic chunks
            chunks = _detect_topic_boundaries(obs_dicts)

            processed_count = 0
            for chunk in chunks:
                if shutdown_event.is_set():
                    break

                # Determine the representative session_id for the chunk (first obs wins)
                chunk_session_id = chunk[0].get("session_id", "")
                chunk_obs_ids = [obs["id"] for obs in chunk]

                # Determine if this chunk should get AI compression:
                # true if *any* observation in the chunk qualifies.
                def _obs_qualifies(obs: dict) -> bool:
                    source = obs.get("source", "")
                    tool_name = obs.get("tool_name")
                    raw_input = obs.get("raw_input") or ""
                    if source in ("user_prompt", "session_end"):
                        if source == "user_prompt" and raw_input.startswith("<task-notification>"):
                            return False
                        return True
                    return tool_name in HIGH_SIGNAL_TOOLS

                use_ai = (
                    ai_compressor is not None
                    and ai_compressor.is_available()
                    and any(_obs_qualifies(obs) for obs in chunk)
                )

                chunk_summary: Optional[str] = None
                chunk_memory_type: Optional[str] = None
                chunk_entities_json: Optional[str] = None
                try:
                    if use_ai:
                        # For multi-observation chunks, compress the whole chunk together.
                        # Build a combined observation text from the chunk.
                        if len(chunk) == 1:
                            obs = chunk[0]
                            chunk_summary = await ai_compressor.compress(
                                source=obs["source"],
                                tool_name=obs.get("tool_name"),
                                agent=obs.get("agent", "main"),
                                raw_input=obs.get("raw_input"),
                                raw_output=obs.get("raw_output"),
                            )
                            if chunk_summary is None:
                                ai_compressor._fallback_count += 1
                            else:
                                chunk_memory_type = ai_compressor._last_memory_type
                                chunk_entities_json = ai_compressor._last_entities_json
                        else:
                            # Multi-obs chunk: build a condensed combined text and compress once
                            combined_parts = []
                            for obs in chunk:
                                tn = obs.get("tool_name") or obs.get("source", "")
                                inp = (obs.get("raw_input") or "")[:400]
                                out = (obs.get("raw_output") or "")[:400]
                                combined_parts.append(
                                    f"[{tn}] input={inp!r} output={out!r}"
                                )
                            combined_text = "\n".join(combined_parts)
                            # Use the first observation's metadata for the compress call,
                            # but pass the combined text as raw_input.
                            first = chunk[0]
                            chunk_summary = await ai_compressor.compress(
                                source=first["source"],
                                tool_name=first.get("tool_name"),
                                agent=first.get("agent", "main"),
                                raw_input=combined_text,
                                raw_output=None,
                            )
                            if chunk_summary is None:
                                ai_compressor._fallback_count += 1
                            else:
                                chunk_memory_type = ai_compressor._last_memory_type
                                chunk_entities_json = ai_compressor._last_entities_json

                    if chunk_summary is None:
                        # Rule-based fallback: join per-obs rule-based summaries
                        rb_parts = [
                            _generate_summary_rule_based(
                                source=obs["source"],
                                tool_name=obs.get("tool_name"),
                                agent=obs.get("agent", "main"),
                                raw_input=obs.get("raw_input"),
                                raw_output=obs.get("raw_output"),
                            )
                            for obs in chunk
                        ]
                        chunk_summary = " | ".join(rb_parts)
                        chunk_memory_type = "episodic"
                        chunk_entities_json = json.dumps([])

                    now_iso = datetime.now(timezone.utc).isoformat()

                    # Mark all observations in this chunk as processed with the chunk summary
                    async with db_lock:
                        placeholders = ",".join("?" * len(chunk_obs_ids))
                        db.execute(
                            f"UPDATE observations SET summary = ?, status = 'processed', "
                            f"processed_at = ?, memory_type = ?, entities = ? "
                            f"WHERE id IN ({placeholders})",
                            [chunk_summary, now_iso, chunk_memory_type, chunk_entities_json]
                            + chunk_obs_ids,
                        )

                        # Insert memcell record
                        db.execute(
                            "INSERT INTO memcells (session_id, observation_ids, summary, "
                            "chunk_type, timestamp) VALUES (?, ?, ?, ?, ?)",
                            (
                                chunk_session_id,
                                json.dumps(chunk_obs_ids),
                                chunk_summary,
                                "auto",
                                now_iso,
                            ),
                        )
                        db.commit()

                    # Sync each observation to vector store and run NER
                    for obs, row in zip(chunk, rows):
                        obs_id = obs["id"]
                        source = obs["source"]
                        tool_name = obs.get("tool_name")
                        # find the sqlite3.Row matching this obs_id
                        matching_row = next((r for r in rows if r["id"] == obs_id), None)
                        if matching_row is not None:
                            await _sync_to_vector_store(obs_id, chunk_summary, matching_row)

                        if (
                            NER_ENABLED
                            and entity_extractor is not None
                            and kg is not None
                            and (
                                source == "user_prompt"
                                or source == "session_end"
                                or tool_name in HIGH_SIGNAL_TOOLS
                            )
                            and chunk_summary
                            and len(chunk_summary) > 50
                        ):
                            await _extract_and_link_entities(
                                obs_id, chunk_summary,
                                obs.get("raw_input"), obs.get("raw_output"),
                            )

                    # Foresight extraction — fire after each chunk, non-blocking
                    if ai_compressor is not None and ai_compressor.is_available() and chunk_summary:
                        try:
                            foresight_data = await _extract_foresight(
                                chunk_session_id, chunk_summary, ai_compressor
                            )
                            if foresight_data:
                                await _store_foresight(chunk_session_id, foresight_data)
                        except Exception as fe:
                            logger.debug(f"Foresight extraction skipped: {fe}")

                    processed_count += len(chunk)

                    # Rate control between AI calls
                    if use_ai:
                        await asyncio.sleep(AI_COMPRESSION_DELAY)

                except Exception as e:
                    # Mark all observations in this chunk as failed
                    logger.error(
                        f"Failed to process chunk {chunk_obs_ids}: {e}", exc_info=True
                    )
                    try:
                        async with db_lock:
                            placeholders = ",".join("?" * len(chunk_obs_ids))
                            db.execute(
                                f"UPDATE observations SET status = 'failed' WHERE id IN ({placeholders})",
                                chunk_obs_ids,
                            )
                            db.commit()
                    except Exception:
                        pass

            if processed_count > 0:
                logger.info(f"Processed {processed_count} observations in {len(chunks)} chunk(s)")

            consecutive_errors = 0  # Reset on successful iteration

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Processor loop error (#{consecutive_errors}): {e}", exc_info=True)
            # Back off on repeated errors to avoid tight error loops
            if consecutive_errors > 5:
                await asyncio.sleep(PROCESS_INTERVAL * 6)  # 30s backoff

        await asyncio.sleep(PROCESS_INTERVAL)


def _generate_summary_rule_based(
    source: str,
    tool_name: Optional[str],
    agent: str,
    raw_input: Optional[str],
    raw_output: Optional[str],
) -> str:
    """Rule-based summary extraction (fast, zero-cost fallback)."""
    parts = []

    if source == "post_tool_use":
        if tool_name:
            parts.append(f"[{agent}] Used tool: {tool_name}")
        if raw_input:
            # Extract key info from input (first 200 chars)
            input_preview = raw_input[:200].replace("\n", " ").strip()
            parts.append(f"Input: {input_preview}")
        if raw_output:
            # Extract key info from output (first 300 chars)
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


class AICompressor:
    """AI-powered observation compression via direct Anthropic OAuth."""

    ANTHROPIC_URL = "https://api.anthropic.com/v1/messages?beta=true"
    OAUTH_HEADERS = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    }

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._ai_count = 0
        self._fallback_count = 0
        self._consecutive_failures = 0
        self._alerted = False          # True once alert fires; reset on recovery
        self._last_failure_reason: Optional[str] = None
        self._degraded_since: Optional[str] = None
        self._backoff_until: float = 0  # time.monotonic() after which we can retry
        self._last_probe_time: float = 0  # last time we probed during degraded state
        # Typed extraction fields — set after each successful compress() call
        self._last_memory_type: Optional[str] = None
        self._last_entities_json: Optional[str] = None

    def _record_success(self):
        """Reset failure tracking on successful compression."""
        if self._consecutive_failures > 0:
            logger.info(
                f"AI compression recovered after {self._consecutive_failures} failures"
            )
            if self._alerted:
                self._send_alert(
                    "Cortex AI Compression Recovered",
                    f"Back online after {self._consecutive_failures} consecutive failures.",
                    sound=False,
                )
        self._consecutive_failures = 0
        self._alerted = False
        self._last_failure_reason = None
        self._degraded_since = None

    def _record_failure(self, reason: str, is_rate_limit: bool = False):
        """Track consecutive failures with exponential backoff for rate limits."""
        self._consecutive_failures += 1
        self._last_failure_reason = reason
        if self._degraded_since is None:
            self._degraded_since = datetime.now(timezone.utc).isoformat()

        # Exponential backoff: 30s, 60s, 120s, 240s, capped at 600s
        if is_rate_limit:
            delay = min(
                AI_BACKOFF_BASE * (2 ** (self._consecutive_failures - 1)),
                AI_BACKOFF_MAX,
            )
            self._backoff_until = time.monotonic() + delay
            logger.info(
                f"Rate limited — backing off {delay:.0f}s "
                f"(failure #{self._consecutive_failures})"
            )

        if self._consecutive_failures >= AI_FAILURE_ALERT_THRESHOLD and not self._alerted:
            self._alerted = True
            logger.error(
                f"AI compression DEGRADED: {self._consecutive_failures} consecutive "
                f"failures. Reason: {reason}"
            )
            self._send_alert(
                "Cortex AI Compression Degraded",
                f"{self._consecutive_failures} consecutive failures. "
                f"Falling back to rule-based summaries.\n"
                f"Reason: {reason}\n"
                f"Check: curl http://localhost:37778/api/compression/status",
            )

    def is_available(self) -> bool:
        """Check if AI compression should be attempted right now.

        Returns True if:
        - Not in backoff period, OR
        - Degraded but enough time passed to probe (self-healing)
        """
        now = time.monotonic()

        # Still in backoff window — skip
        if now < self._backoff_until:
            return False

        # If degraded, only allow periodic probes to test recovery
        if self._consecutive_failures >= AI_FAILURE_ALERT_THRESHOLD:
            if now - self._last_probe_time < AI_RECOVERY_PROBE_INTERVAL:
                return False
            # Allow a probe — will be marked as probe time on next compress()
            return True

        return True

    @staticmethod
    def _send_alert(title: str, message: str, sound: bool = True):
        """Send a macOS notification."""
        try:
            script = (
                f'display notification "{message}" '
                f'with title "{title}"'
            )
            if sound:
                script += ' sound name "Funk"'
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning(f"Failed to send alert notification: {e}")

    async def _ensure_client(self) -> bool:
        """Load or refresh the OAuth token from auth-profiles (fallback path)."""
        try:
            if not AUTH_PROFILES_PATH.exists():
                logger.warning(f"Auth profiles not found: {AUTH_PROFILES_PATH}")
                return False

            data = json.loads(AUTH_PROFILES_PATH.read_text())
            token = data.get("profiles", {}).get("anthropic:default", {}).get("token")
            if not token:
                logger.warning("No anthropic:default token in auth-profiles")
                return False

            if token != self._token:
                self._token = token
                if self._client:
                    await self._client.aclose()
                self._client = httpx.AsyncClient(
                    headers={
                        **self.OAUTH_HEADERS,
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=30.0,
                )
                logger.info("AI compressor OAuth client refreshed")

            return True
        except Exception as e:
            logger.warning(f"Failed to load auth profile: {e}")
            return False

    async def compress(
        self,
        source: str,
        tool_name: Optional[str],
        agent: str,
        raw_input: Optional[str],
        raw_output: Optional[str],
    ) -> Optional[str]:
        """Compress an observation with AI via direct Anthropic OAuth.

        Returns the content string (dense summary).  After a successful call,
        self._last_memory_type and self._last_entities_json are set so the
        caller can persist typed fields without changing the return signature.
        """
        # Reset typed fields for this call
        self._last_memory_type = None
        self._last_entities_json = None

        # Track probe time when degraded (for self-healing probes)
        self._last_probe_time = time.monotonic()

        prompt = self._build_prompt(source, tool_name, agent, raw_input, raw_output)

        raw_result = await self._compress_via_oauth(prompt)
        if raw_result is not None:
            self._ai_count += 1
            self._record_success()

        if raw_result is None:
            return None

        # Parse the structured JSON response; populate typed fields
        content, memory_type, entities_json = self._parse_typed_response(raw_result)
        self._last_memory_type = memory_type
        self._last_entities_json = entities_json
        return content

    async def _compress_via_oauth(self, prompt: str) -> Optional[str]:
        """Try compression through direct Anthropic OAuth."""
        if not await self._ensure_client():
            self._record_failure("auth profile unavailable")
            return None

        payload = {
            "model": AI_MODEL,
            "max_tokens": AI_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            resp = await self._client.post(self.ANTHROPIC_URL, json=payload)

            if resp.status_code == 401:
                logger.warning("AI compression 401 — refreshing token and retrying")
                self._token = None
                if await self._ensure_client():
                    resp = await self._client.post(self.ANTHROPIC_URL, json=payload)
                else:
                    self._record_failure("401 + token refresh failed")
                    return None

            if resp.status_code == 429:
                reason = f"HTTP 429: {resp.text[:200]}"
                logger.warning(f"OAuth rate limited: {reason}")
                self._record_failure(reason, is_rate_limit=True)
                return None

            if resp.status_code == 529:
                reason = "HTTP 529: API overloaded"
                logger.warning(f"OAuth: {reason}")
                self._record_failure(reason, is_rate_limit=True)
                return None

            if resp.status_code != 200:
                reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(f"OAuth compression: {reason}")
                self._record_failure(reason)
                return None

            data = resp.json()
            return data["content"][0]["text"]

        except httpx.TimeoutException:
            logger.warning("OAuth compression timed out")
            self._record_failure("timeout", is_rate_limit=True)
            return None
        except Exception as e:
            logger.warning(f"OAuth compression failed: {e}")
            self._record_failure(str(e))
            return None

    def _build_prompt(
        self,
        source: str,
        tool_name: Optional[str],
        agent: str,
        raw_input: Optional[str],
        raw_output: Optional[str],
    ) -> str:
        """Build the compression prompt requesting structured JSON output."""
        observation_text = (
            f"Source: {source}\n"
            f"Tool: {tool_name or 'N/A'}\n"
            f"Agent: {agent}\n"
            f"Input: {raw_input or 'N/A'}\n"
            f"Output: {raw_output or 'N/A'}"
        )
        return (
            "Compress this Claude Code work into a structured memory. "
            "Return ONLY valid JSON, no other text:\n"
            "{\n"
            '  "type": "episodic|decision|preference|fact",\n'
            '  "content": "dense summary under 200 chars",\n'
            '  "entities": ["list", "of", "key", "entities"]\n'
            "}\n\n"
            "Types:\n"
            "- episodic: what happened (actions taken, files changed)\n"
            "- decision: a choice made or approach chosen\n"
            "- preference: user likes/dislikes/working style\n"
            "- fact: a technical fact learned\n\n"
            f"Observation:\n{observation_text}"
        )

    @staticmethod
    def _parse_typed_response(raw_text: str) -> tuple[str, Optional[str], Optional[str]]:
        """Parse a typed JSON compression response.

        Returns (content, memory_type, entities_json).
        Falls back gracefully if JSON is invalid or fields are missing.
        """
        VALID_TYPES = {"episodic", "decision", "preference", "fact"}
        try:
            clean = raw_text.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)
            parsed = json.loads(clean)
            content = str(parsed.get("content", raw_text)).strip() or raw_text
            memory_type = parsed.get("type")
            if memory_type not in VALID_TYPES:
                memory_type = "episodic"
            entities = parsed.get("entities", [])
            if not isinstance(entities, list):
                entities = []
            # Coerce all entity items to strings
            entities = [str(e) for e in entities if e]
            return content, memory_type, json.dumps(entities)
        except (json.JSONDecodeError, TypeError, KeyError):
            return raw_text, "episodic", json.dumps([])


async def _sync_to_vector_store(obs_id: int, summary: str, row: sqlite3.Row):
    """Sync processed observation to ChromaDB vector store.

    Imports unified_vector_store lazily to avoid startup dependency.
    """
    try:
        # Lazy import — vector store may not be initialized yet
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
        # Vector store not yet built — skip silently
        pass
    except Exception as e:
        logger.warning(f"Vector sync failed for obs {obs_id}: {e}")


# ── Named Entity Recognition (NER) ────────────────────────────────────────


class EntityExtractor:
    """Lightweight rule-based + regex Named Entity Recognition.

    Extracts people, projects, tools/systems, files, and companies from
    observation text. No heavy ML dependencies — regex patterns only.
    Designed for < 10ms per observation.
    """

    # ── Known systems / tools (case-insensitive matching) ──
    KNOWN_SYSTEMS: Set[str] = {
        "BrokerBridge", "TradingCore", "OpenClaw", "CamiRouter",
        "MoltyTrades", "Cortex", "Ralph", "IBC", "IBKR",
        "PostgreSQL", "SQLite", "NetworkX", "FastAPI", "Uvicorn",
        "Playwright", "ChromaDB", "Tailscale", "Docker", "Nginx",
        "KPL", "Strat", "VWAP",
    }
    _KNOWN_SYSTEMS_LOWER: Dict[str, str] = {s.lower(): s for s in KNOWN_SYSTEMS}

    # ── Known project names ──
    KNOWN_PROJECTS: Set[str] = {
        "Magnum Opus", "Young Money Investments", "YMI",
        "Magnum Opus Capital",
    }
    _KNOWN_PROJECTS_LOWER: Dict[str, str] = {p.lower(): p for p in KNOWN_PROJECTS}

    # ── Compiled regex patterns ──

    # File paths: /something/something.ext or ~/something
    RE_FILE_PATH = re.compile(
        r'(?:~|/)[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,10}'
    )

    # GitHub repo paths: owner/repo (at least one slash, alphanumeric + hyphens)
    RE_GITHUB_REPO = re.compile(
        r'\b([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)\b'
    )

    # ~/Projects/ directory references
    RE_PROJECT_DIR = re.compile(
        r'~/Projects/([A-Za-z0-9_.-]+)'
    )

    # MCP tool names: mcp__server__tool_name
    RE_MCP_TOOL = re.compile(
        r'\b(mcp__[a-z0-9_-]+__[a-z0-9_-]+)\b'
    )

    # People patterns: "talked to [Name]", "meeting with [Name]", "from [Name]"
    # Trigger words are case-insensitive; captured Name requires uppercase start
    RE_PEOPLE_CONTEXT = re.compile(
        r'(?i:talked?\s+(?:to|with)|meeting\s+with|from|'
        r'asked|told|emailed?|messaged?|called|'
        r'spoke\s+(?:to|with)|chat(?:ted)?\s+with)\s+'
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})',
        re.MULTILINE,
    )

    # @mentions (e.g. @Cameron, @username)
    RE_AT_MENTION = re.compile(
        r'@([A-Za-z][A-Za-z0-9_]{1,30})\b'
    )

    # CamelCase names in code contexts (2-3 words, not common code patterns)
    RE_CAMELCASE_NAME = re.compile(
        r'\b([A-Z][a-z]{2,}(?:[A-Z][a-z]{2,}){1,2})\b'
    )

    # Company patterns: "Something Inc", "Something LLC", etc.
    # Limit company name to 1-4 capitalized words before the suffix
    RE_COMPANY = re.compile(
        r'\b((?:[A-Z][A-Za-z0-9&]+\s+){1,4}'
        r'(?:Inc\.?|LLC|Corp\.?|Capital|Fund|'
        r'Investments?|Partners?|Holdings?|'
        r'Services?|Group|Labs?))\b'
    )

    # Common code/class names to EXCLUDE from people detection
    _CAMELCASE_EXCLUDE: Set[str] = {
        "FastAPI", "AsyncClient", "BaseModel", "HTTPException",
        "ObservationRequest", "SessionStartRequest", "SessionEndRequest",
        "HealthResponse", "RetentionManager", "AICompressor",
        "KnowledgeGraph", "EntityExtractor", "NetworkXNoPath",
        "MultiDiGraph", "SequenceMatcher", "CancelledError",
        "TimeoutException", "ConnectError", "JSONDecodeError",
        "TypeError", "KeyError", "ValueError", "ImportError",
        "FileHandler", "StreamHandler", "NoneType", "DataFrame",
    }

    def __init__(self):
        self._entities_extracted_total = 0
        self._relationships_created_total = 0
        self._last_extraction_time: Optional[str] = None

    def extract(
        self,
        summary: Optional[str],
        raw_input: Optional[str],
        raw_output: Optional[str],
    ) -> List[Tuple[str, str]]:
        """Extract (entity_name, entity_type) tuples from observation text.

        Combines results from all text fields, deduplicates by normalized name.
        Returns a list of unique (name, type) tuples.
        """
        # Combine all text for extraction
        texts = []
        if summary:
            texts.append(summary)
        if raw_input:
            texts.append(raw_input[:2000])  # Limit for performance
        if raw_output:
            texts.append(raw_output[:2000])

        if not texts:
            return []

        combined = "\n".join(texts)
        seen: Dict[str, str] = {}  # normalized_name -> (display_name, type)

        # Extract each entity type
        self._extract_files(combined, seen)
        self._extract_mcp_tools(combined, seen)
        self._extract_known_systems(combined, seen)
        self._extract_known_projects(combined, seen)
        self._extract_project_dirs(combined, seen)
        self._extract_people(combined, seen)
        self._extract_companies(combined, seen)
        self._extract_tickers(combined, seen)
        self._extract_strategy_refs(combined, seen)

        return [(name, etype) for name, etype in seen.items()]

    def _extract_files(self, text: str, seen: Dict[str, str]):
        """Extract file paths."""
        for match in self.RE_FILE_PATH.finditer(text):
            path = match.group(0)
            if len(path) > 5 and not path.endswith("/."):
                # Skip common non-file patterns
                if any(path.endswith(ext) for ext in (".0", ".1", ".2")):
                    continue
                seen.setdefault(path, "tool")  # Files are tools/artifacts

    def _extract_mcp_tools(self, text: str, seen: Dict[str, str]):
        """Extract MCP tool names."""
        for match in self.RE_MCP_TOOL.finditer(text):
            seen.setdefault(match.group(1), "tool")

    def _extract_known_systems(self, text: str, seen: Dict[str, str]):
        """Match against known system/tool names."""
        text_lower = text.lower()
        for lower_name, display_name in self._KNOWN_SYSTEMS_LOWER.items():
            if lower_name in text_lower:
                seen.setdefault(display_name, "system")

    def _extract_known_projects(self, text: str, seen: Dict[str, str]):
        """Match against known project names."""
        text_lower = text.lower()
        for lower_name, display_name in self._KNOWN_PROJECTS_LOWER.items():
            if lower_name in text_lower:
                seen.setdefault(display_name, "project")

    def _extract_project_dirs(self, text: str, seen: Dict[str, str]):
        """Extract ~/Projects/name references."""
        for match in self.RE_PROJECT_DIR.finditer(text):
            project_name = match.group(1)
            if len(project_name) > 2:
                seen.setdefault(project_name, "project")

    def _extract_people(self, text: str, seen: Dict[str, str]):
        """Extract people names from contextual patterns and @mentions."""
        # Contextual patterns ("talked to Cameron", etc.)
        for match in self.RE_PEOPLE_CONTEXT.finditer(text):
            name = match.group(1).strip()
            if len(name) > 2 and name not in self._CAMELCASE_EXCLUDE:
                seen.setdefault(name, "person")

        # @mentions
        for match in self.RE_AT_MENTION.finditer(text):
            mention = match.group(1)
            if len(mention) > 2:
                seen.setdefault(mention, "person")

    def _extract_companies(self, text: str, seen: Dict[str, str]):
        """Extract company names."""
        for match in self.RE_COMPANY.finditer(text):
            company = match.group(1).strip()
            # Avoid capturing things already tagged as projects
            if company.lower() not in self._KNOWN_PROJECTS_LOWER:
                seen.setdefault(company, "company")

    # ── Ticker extraction ──

    # Known tickers whitelist — avoids matching common English words
    KNOWN_TICKERS: Set[str] = {
        # Major indices / futures
        "SPY", "SPX", "QQQ", "IWM", "DIA", "VIX",
        "ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM",
        # Commodities futures
        "CL", "GC", "SI", "NG", "HG", "ZB", "ZN", "ZC", "ZS", "ZW",
        # Micro futures
        "MCL", "MGC", "MBT",
        # Crypto futures
        "BTC", "ETH",
        # Currency futures
        "6E", "6J", "6B", "6A", "6C", "6S",
        # Popular stocks
        "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "TSLA", "NVDA", "META",
        "AMD", "INTC", "NFLX", "BABA", "PLTR", "SOFI", "COIN", "MARA",
        "RIOT", "SQ", "PYPL", "SHOP", "SNAP", "UBER", "LYFT", "ABNB",
        "DKNG", "CRWD", "NET", "SNOW", "RBLX", "RIVN", "LCID", "NIO",
        "BA", "F", "GM", "JPM", "GS", "MS", "BAC", "WFC", "C",
        "XOM", "CVX", "PFE", "JNJ", "UNH", "ABBV", "MRK", "LLY",
        "WMT", "COST", "HD", "TGT", "LOW", "SBUX", "MCD", "DIS",
        "V", "MA", "AXP",
    }

    # Common English words that look like tickers — exclude these
    _TICKER_EXCLUDE: Set[str] = {
        "I", "A", "IT", "IS", "IN", "ON", "AT", "TO", "BY", "OR",
        "AN", "IF", "DO", "GO", "NO", "SO", "UP", "US", "WE", "HE",
        "BE", "ME", "MY", "AM", "AS", "OF", "AI", "OK", "PM", "AM",
        "ALL", "AND", "ARE", "BUT", "CAN", "DID", "FOR", "GET", "HAS",
        "HAD", "HER", "HIS", "HOW", "ITS", "LET", "MAY", "NEW", "NOT",
        "NOW", "OLD", "ONE", "OUR", "OUT", "OWN", "RAN", "RUN", "SAY",
        "SET", "SHE", "THE", "TOO", "TRY", "TWO", "USE", "WAS", "WAY",
        "WHO", "WHY", "WIN", "YET", "YOU",
        "API", "CLI", "CPU", "CSS", "CSV", "DB", "DNS", "ENV", "FTP",
        "GUI", "HTML", "HTTP", "IDE", "IP", "JSON", "LOG", "OS", "PC",
        "PDF", "RAM", "RGB", "SDK", "SQL", "SSH", "SSL", "TCP", "TLS",
        "UDP", "UI", "URL", "USB", "UTC", "VM", "VPN", "VPS", "XML",
        "NER", "MCP", "TTL", "WAL", "FTS", "ORM", "AWS", "GCP",
    }

    # $TICKER pattern (always captured regardless of whitelist)
    RE_DOLLAR_TICKER = re.compile(r'\$([A-Z]{1,5})\b')

    # Standalone uppercase ticker (requires whitelist match)
    RE_BARE_TICKER = re.compile(r'\b([A-Z][A-Z0-9]{0,4})\b')

    def _extract_tickers(self, text: str, seen: Dict[str, str]):
        """Extract stock/futures tickers from text.

        Captures $TICKER patterns unconditionally and bare uppercase
        tickers only if they appear in the KNOWN_TICKERS whitelist.
        """
        # $TICKER — always capture (explicit intent)
        for match in self.RE_DOLLAR_TICKER.finditer(text):
            ticker = match.group(1)
            if ticker not in self._TICKER_EXCLUDE:
                seen.setdefault(ticker, "ticker")

        # Bare uppercase — only if in whitelist
        for match in self.RE_BARE_TICKER.finditer(text):
            ticker = match.group(1)
            if ticker in self.KNOWN_TICKERS and ticker not in self._TICKER_EXCLUDE:
                seen.setdefault(ticker, "ticker")

    # ── Strategy reference extraction ──

    RE_STRATEGY_REF = re.compile(
        r'(?:strategy|setup|system|approach)\s+(?:called\s+|named\s+)?'
        r'["\']?([A-Z][A-Za-z0-9_-]+)["\']?',
        re.IGNORECASE,
    )

    def _extract_strategy_refs(self, text: str, seen: Dict[str, str]):
        """Extract strategy/setup name references."""
        for match in self.RE_STRATEGY_REF.finditer(text):
            name = match.group(1).strip()
            if len(name) > 1 and name not in self._CAMELCASE_EXCLUDE:
                seen.setdefault(name, "system")

    # ── Directed relationship extraction ──

    # Pattern: "messaged X", "texted X", "DMed X", "replied to X"
    RE_MESSAGED = re.compile(
        r'(?:messaged|texted|DMed|DM\'?d|replied\s+to|pinged|sent\s+(?:a\s+)?message\s+to)\s+'
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})',
        re.IGNORECASE,
    )

    # Pattern: "trade with X", "X's account", "X's strategy", "trading X's"
    RE_TRADED_WITH = re.compile(
        r'(?:trad(?:e|ed|ing)\s+(?:with|for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})|'
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\'s\s+'
        r'(?:account|strategy|position|portfolio|trade|setup))',
        re.IGNORECASE,
    )

    # Pattern: "X is in Y", "X joined Y", "X is a member of Y"
    RE_MEMBER_OF = re.compile(
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+'
        r'(?:is\s+(?:in|a\s+member\s+of|part\s+of)|joined|belongs?\s+to)\s+'
        r'([A-Z][A-Za-z0-9_ ]+)',
        re.IGNORECASE,
    )

    # Pattern: "X from Company", "X at Company", "X works at Company"
    RE_WORKS_AT = re.compile(
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+'
        r'(?:from|at|works?\s+(?:at|for))\s+'
        r'([A-Z][A-Za-z0-9& ]+(?:Inc\.?|LLC|Corp\.?|Capital|Fund|'
        r'Investments?|Partners?|Holdings?|Services?|Group|Labs?)?)',
        re.IGNORECASE,
    )

    # Pattern: Discord @role mentions
    RE_DISCORD_ROLE = re.compile(
        r'@([A-Za-z][A-Za-z0-9_ ]{2,30})\s+role',
        re.IGNORECASE,
    )

    def _extract_directed_relationships(
        self,
        entities: List[Tuple[str, str]],
        text: str,
        obs_id: int,
    ):
        """Extract directed relationships from text patterns.

        Creates messaged, discussed, traded_with, member_of, and works_at
        relationships in the knowledge graph based on regex pattern matching.
        """
        if kg is None:
            return

        relationships_created = 0

        # Build lookup sets for entity classification
        person_entities = {name for name, etype in entities if etype == "person"}
        system_entities = {name for name, etype in entities if etype in ("system", "project")}
        company_entities = {name for name, etype in entities if etype == "company"}
        ticker_entities = {name for name, etype in entities if etype == "ticker"}

        # ── messaged ──
        for match in self.RE_MESSAGED.finditer(text):
            target = match.group(1).strip()
            if target in self._CAMELCASE_EXCLUDE:
                continue
            # Find a person entity that could be the source (default to Cameron)
            source = "Cameron"
            kg.add_relationship(
                source, "messaged", target,
                context=f"obs:{obs_id}",
                strength=0.7,
                observation_id=obs_id,
            )
            relationships_created += 1

        # ── traded_with ──
        for match in self.RE_TRADED_WITH.finditer(text):
            person = (match.group(1) or match.group(2) or "").strip()
            if not person or person in self._CAMELCASE_EXCLUDE:
                continue
            kg.add_relationship(
                "Cameron", "traded_with", person,
                context=f"obs:{obs_id}",
                strength=0.7,
                observation_id=obs_id,
            )
            relationships_created += 1

        # ── member_of ──
        for match in self.RE_MEMBER_OF.finditer(text):
            person = match.group(1).strip()
            group = match.group(2).strip()
            if person in self._CAMELCASE_EXCLUDE:
                continue
            kg.add_relationship(
                person, "member_of", group,
                context=f"obs:{obs_id}",
                strength=0.7,
                observation_id=obs_id,
            )
            relationships_created += 1

        # ── works_at ──
        for match in self.RE_WORKS_AT.finditer(text):
            person = match.group(1).strip()
            company = match.group(2).strip()
            if person in self._CAMELCASE_EXCLUDE:
                continue
            kg.add_relationship(
                person, "works_at", company,
                context=f"obs:{obs_id}",
                strength=0.7,
                observation_id=obs_id,
            )
            relationships_created += 1

        # ── discussed: person + system/project co-occurrence ──
        for person in person_entities:
            for system in system_entities:
                kg.add_relationship(
                    person, "discussed", system,
                    context=f"obs:{obs_id}",
                    strength=0.5,
                    observation_id=obs_id,
                )
                relationships_created += 1

        # ── discussed: person + ticker co-occurrence ──
        for person in person_entities:
            for ticker in ticker_entities:
                kg.add_relationship(
                    person, "discussed", ticker,
                    context=f"obs:{obs_id}",
                    strength=0.5,
                    observation_id=obs_id,
                )
                relationships_created += 1

        return relationships_created

    def get_stats(self) -> dict:
        """Return NER statistics."""
        return {
            "enabled": NER_ENABLED,
            "entities_extracted_total": self._entities_extracted_total,
            "relationships_created_total": self._relationships_created_total,
            "last_extraction_time": self._last_extraction_time,
        }


# Global NER state
entity_extractor: Optional[EntityExtractor] = None
kg = None  # KnowledgeGraph instance — initialized in lifespan


async def _extract_and_link_entities(
    obs_id: int,
    summary: Optional[str],
    raw_input: Optional[str],
    raw_output: Optional[str],
):
    """Extract entities via NER and link them in the knowledge graph.

    1. Extracts entities via regex patterns
    2. Adds them to the knowledge graph (idempotent)
    3. Creates co_mentioned relationships between entities in the same observation
    4. Logs what was extracted at DEBUG level
    """
    if entity_extractor is None or kg is None:
        return

    try:
        entities = entity_extractor.extract(summary, raw_input, raw_output)

        if not entities:
            return

        # Add each entity to the knowledge graph (idempotent)
        for name, etype in entities:
            kg.add_entity(name, etype)

        entity_extractor._entities_extracted_total += len(entities)
        entity_extractor._last_extraction_time = datetime.now(timezone.utc).isoformat()

        # Create co_mentioned relationships between all pairs
        relationships_created = 0
        if len(entities) >= 2:
            # Limit pairwise relationships to avoid combinatorial explosion
            # on observations with many entities — cap at 10 entities
            capped = entities[:10]
            for i in range(len(capped)):
                for j in range(i + 1, len(capped)):
                    name_a, _ = capped[i]
                    name_b, _ = capped[j]
                    kg.add_relationship(
                        name_a, "co_mentioned", name_b,
                        context=f"obs:{obs_id}",
                        strength=0.5,
                        observation_id=obs_id,
                    )
                    relationships_created += 1

        # Extract directed relationships from text patterns
        combined_text = "\n".join(
            t for t in [summary, raw_input, raw_output] if t
        )
        directed_count = entity_extractor._extract_directed_relationships(
            entities, combined_text, obs_id,
        ) or 0
        relationships_created += directed_count

        entity_extractor._relationships_created_total += relationships_created

        logger.debug(
            "NER obs=%d: extracted %d entities, %d relationships "
            "(%d co_mentioned, %d directed)",
            obs_id, len(entities), relationships_created,
            relationships_created - directed_count, directed_count,
        )

    except Exception as e:
        logger.warning("NER extraction failed for obs %d: %s", obs_id, e)


# ── Retention manager ──────────────────────────────────────────────────


LOW_SIGNAL_TOOLS = frozenset({"Read", "Glob", "Grep", "Bash", "TaskOutput"})


class RetentionManager:
    """Tiered retention: delete low-signal rows, trim raw fields on old high-signal."""

    def __init__(self):
        self.last_run_stats: Optional[dict] = None

    async def run_once(self, dry_run: bool = False) -> dict:
        """Run one full retention pass. Returns stats dict."""
        stats = {
            "low_signal_deleted": 0,
            "high_signal_trimmed": 0,
            "vector_docs_deleted": 0,
            "dry_run": dry_run,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            # Phase 1: Delete low-signal observations 7d+ old
            deleted, vec_deleted = await self._delete_low_signal(dry_run=dry_run)
            stats["low_signal_deleted"] = deleted
            stats["vector_docs_deleted"] = vec_deleted

            # Phase 2: Trim raw fields on high-signal observations 30d+ old
            trimmed = await self._trim_high_signal(dry_run=dry_run)
            stats["high_signal_trimmed"] = trimmed

        except Exception as e:
            logger.error(f"Retention pass failed: {e}", exc_info=True)
            stats["error"] = str(e)

        stats["completed_at"] = datetime.now(timezone.utc).isoformat()
        self.last_run_stats = stats
        logger.info(
            f"Retention complete: deleted={stats['low_signal_deleted']}, "
            f"trimmed={stats['high_signal_trimmed']}, "
            f"vectors_removed={stats['vector_docs_deleted']}, "
            f"dry_run={dry_run}"
        )
        return stats

    async def _delete_low_signal(self, dry_run: bool = False) -> tuple[int, int]:
        """Delete low-signal observations older than RETENTION_FULL_DAYS."""
        if db is None or db_lock is None:
            return 0, 0

        async with db_lock:
            rows = db.execute(
                """SELECT id FROM observations
                   WHERE timestamp < datetime('now', ? || ' days')
                     AND status = 'processed'
                     AND (
                       (tool_name IS NULL AND source = 'post_tool_use')
                       OR tool_name IN ('Read','Glob','Grep','Bash','TaskOutput')
                       OR (source = 'user_prompt' AND raw_input LIKE '<task-notification>%')
                     )""",
                (f"-{RETENTION_FULL_DAYS}",),
            ).fetchall()
            all_ids = [r["id"] for r in rows]

        if not all_ids:
            return 0, 0

        if dry_run:
            return len(all_ids), len(all_ids)

        deleted = await self._chunked_delete(all_ids)
        vec_deleted = await self._delete_from_vector_store(all_ids)
        return deleted, vec_deleted

    async def _trim_high_signal(self, dry_run: bool = False) -> int:
        """NULL raw_input/raw_output on high-signal observations older than RETENTION_TRIM_DAYS."""
        if db is None or db_lock is None:
            return 0

        async with db_lock:
            rows = db.execute(
                """SELECT id FROM observations
                   WHERE timestamp < datetime('now', ? || ' days')
                     AND status = 'processed'
                     AND (raw_input IS NOT NULL OR raw_output IS NOT NULL)""",
                (f"-{RETENTION_TRIM_DAYS}",),
            ).fetchall()
            all_ids = [r["id"] for r in rows]

        if not all_ids or dry_run:
            return len(all_ids)

        return await self._chunked_update(all_ids)

    async def _chunked_delete(self, ids: list[int]) -> int:
        """DELETE observations in batches, releasing db_lock between batches."""
        total = 0
        for i in range(0, len(ids), RETENTION_BATCH_SIZE):
            batch = ids[i:i + RETENTION_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            async with db_lock:
                db.execute(f"DELETE FROM observations WHERE id IN ({placeholders})", batch)
                db.commit()
            total += len(batch)
            if i + RETENTION_BATCH_SIZE < len(ids):
                await asyncio.sleep(0.1)
        return total

    async def _chunked_update(self, ids: list[int]) -> int:
        """NULL raw fields in batches, releasing db_lock between batches."""
        total = 0
        for i in range(0, len(ids), RETENTION_BATCH_SIZE):
            batch = ids[i:i + RETENTION_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            async with db_lock:
                db.execute(
                    f"UPDATE observations SET raw_input = NULL, raw_output = NULL "
                    f"WHERE id IN ({placeholders})",
                    batch,
                )
                db.commit()
            total += len(batch)
            if i + RETENTION_BATCH_SIZE < len(ids):
                await asyncio.sleep(0.1)
        return total

    async def _delete_from_vector_store(self, obs_ids: list[int]) -> int:
        """Delete corresponding vector store documents."""
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from unified_vector_store import get_vector_store
            store = get_vector_store()

            doc_ids = [f"obs-{oid}" for oid in obs_ids]
            for i in range(0, len(doc_ids), RETENTION_BATCH_SIZE):
                batch = doc_ids[i:i + RETENTION_BATCH_SIZE]
                store.delete(batch)
            return len(doc_ids)
        except ImportError:
            return 0
        except Exception as e:
            logger.warning(f"Vector store cleanup failed: {e}")
            return 0

    async def get_stats(self) -> dict:
        """Get current retention statistics."""
        if db is None or db_lock is None:
            return {"error": "Database not initialized"}

        stats = {}
        async with db_lock:
            stats["total_observations"] = db.execute(
                "SELECT COUNT(*) as c FROM observations"
            ).fetchone()["c"]

            stats["age_0_7d"] = db.execute(
                "SELECT COUNT(*) as c FROM observations "
                "WHERE timestamp >= datetime('now', '-7 days')"
            ).fetchone()["c"]
            stats["age_7_30d"] = db.execute(
                "SELECT COUNT(*) as c FROM observations "
                "WHERE timestamp < datetime('now', '-7 days') "
                "AND timestamp >= datetime('now', '-30 days')"
            ).fetchone()["c"]
            stats["age_30d_plus"] = db.execute(
                "SELECT COUNT(*) as c FROM observations "
                "WHERE timestamp < datetime('now', '-30 days')"
            ).fetchone()["c"]

            stats["low_signal_delete_candidates"] = db.execute(
                """SELECT COUNT(*) as c FROM observations
                   WHERE timestamp < datetime('now', '-7 days')
                     AND status = 'processed'
                     AND (
                       (tool_name IS NULL AND source = 'post_tool_use')
                       OR tool_name IN ('Read','Glob','Grep','Bash','TaskOutput')
                       OR (source = 'user_prompt' AND raw_input LIKE '<task-notification>%')
                     )"""
            ).fetchone()["c"]

            stats["high_signal_trim_candidates"] = db.execute(
                """SELECT COUNT(*) as c FROM observations
                   WHERE timestamp < datetime('now', '-30 days')
                     AND status = 'processed'
                     AND (raw_input IS NOT NULL OR raw_output IS NOT NULL)"""
            ).fetchone()["c"]

            try:
                stats["db_size_mb"] = round(DB_PATH.stat().st_size / (1024 * 1024), 2)
            except OSError:
                stats["db_size_mb"] = None

        stats["last_run"] = self.last_run_stats
        return stats


async def run_retention_loop():
    """Background loop that runs retention cleanup every RETENTION_INTERVAL."""
    await asyncio.sleep(60)  # Startup delay
    logger.info("Retention loop started")

    while not shutdown_event.is_set():
        try:
            if retention_manager:
                await retention_manager.run_once()
        except Exception as e:
            logger.error(f"Retention loop error: {e}", exc_info=True)

        for _ in range(RETENTION_INTERVAL):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)


# ── Session Summarization ──────────────────────────────────────────────────


async def _update_user_profile(session_id: str, session_summary: str, compressor: "AICompressor"):
    """Extract lasting profile facts from a session summary and upsert into the profile table.

    Called after a session summary is written to the DB. Makes an AI call to extract
    durable facts about the user's expertise, preferences, style, and context.
    Fails silently so it never disrupts the summarization pipeline.
    """
    if db is None or db_lock is None:
        return
    if not session_summary or not session_summary.strip():
        return
    if compressor is None or not compressor.is_available():
        return

    prompt = (
        "From this session summary, extract lasting facts about this user's profile. "
        "Return ONLY valid JSON:\n"
        "{\n"
        '  "expertise": {"area": "description"},\n'
        '  "preference": {"topic": "what they prefer"},\n'
        '  "style": {"aspect": "how they work"},\n'
        '  "context": {"key": "important ongoing context"}\n'
        "}\n"
        "Only include high-confidence, durable facts. Return {} for any category with nothing notable.\n\n"
        f"Session summary:\n{session_summary}"
    )

    try:
        raw = await _call_ai_for_summary(prompt)
        if not raw:
            return

        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)

        extracted: dict = json.loads(clean)
        if not isinstance(extracted, dict):
            return

        valid_categories = {"expertise", "preference", "style", "context"}
        now = datetime.now(timezone.utc).isoformat()

        async with db_lock:
            for category, entries in extracted.items():
                if category not in valid_categories:
                    continue
                if not isinstance(entries, dict):
                    continue
                for key, value in entries.items():
                    if not key or not value:
                        continue
                    db.execute(
                        "INSERT INTO profile (category, key, value, confidence, updated_at) "
                        "VALUES (?, ?, ?, 0.8, ?) "
                        "ON CONFLICT(category, key) DO UPDATE SET "
                        "value = excluded.value, confidence = excluded.confidence, "
                        "updated_at = excluded.updated_at",
                        (category, str(key), str(value), now),
                    )
            db.commit()

        logger.info(f"Profile updated from session {session_id[:8]}...")

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug(f"Profile extraction parse failed for {session_id[:8]}...: {e}")
    except Exception as e:
        logger.debug(f"Profile update failed for {session_id[:8]}...: {e}")


async def _generate_session_summary(session_id: str):
    """Generate and store a summary for a completed session.

    Tries AI compression first (via Anthropic OAuth), falls back to
    rule-based aggregation of tool usage, file paths, and decisions.
    """
    if db is None or db_lock is None:
        return

    try:
        async with db_lock:
            # Fetch all processed observations for this session
            rows = db.execute(
                "SELECT id, source, tool_name, agent, summary, raw_input, raw_output "
                "FROM observations WHERE session_id = ? AND status = 'processed' "
                "ORDER BY id ASC",
                (session_id,),
            ).fetchall()

            session_row = db.execute(
                "SELECT user_prompt, agent FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

        if not rows:
            logger.info(f"Session {session_id[:8]}... has no processed observations, skipping summary")
            return

        user_prompt = session_row["user_prompt"] if session_row else None
        agent = session_row["agent"] if session_row else "main"

        # Try AI summarization
        ai_summary = None
        if (
            ai_compressor is not None
            and ai_compressor.is_available()
            and len(rows) >= 3  # only worth AI-summarizing sessions with substance
        ):
            ai_summary = await _ai_session_summary(session_id, rows, user_prompt, agent)

        if ai_summary:
            overall_summary = ai_summary.get("summary", "")
            key_decisions = json.dumps(ai_summary.get("key_decisions", []))
            entities = json.dumps(ai_summary.get("entities_mentioned", []))
        else:
            # Rule-based fallback
            rb = _rule_based_session_summary(rows, user_prompt, agent)
            overall_summary = rb["summary"]
            key_decisions = json.dumps(rb["key_decisions"])
            entities = json.dumps(rb["entities_mentioned"])

        # Insert into session_summaries and update sessions table
        async with db_lock:
            db.execute(
                "INSERT INTO session_summaries (session_id, summary, key_decisions, entities_mentioned) "
                "VALUES (?, ?, ?, ?)",
                (session_id, overall_summary, key_decisions, entities),
            )
            db.execute(
                "UPDATE sessions SET summary = ?, status = 'summarized' WHERE id = ?",
                (overall_summary, session_id),
            )
            db.commit()

        logger.info(
            f"Session {session_id[:8]}... summarized "
            f"({'AI' if ai_summary else 'rule-based'}, {len(rows)} observations)"
        )

        # Extract durable profile facts from this session's summary
        if overall_summary and ai_compressor is not None:
            await _update_user_profile(session_id, overall_summary, ai_compressor)

    except Exception as e:
        logger.error(f"Session summarization failed for {session_id[:8]}...: {e}", exc_info=True)


async def _ai_session_summary(
    session_id: str,
    rows: list,
    user_prompt: Optional[str],
    agent: str,
) -> Optional[dict]:
    """Use AI to generate a structured session summary. Returns dict or None."""
    try:
        # Build condensed observation feed (limit total text to ~6000 chars)
        obs_lines = []
        total_chars = 0
        for r in rows:
            line = f"[{r['tool_name'] or r['source']}] {r['summary'] or ''}"
            if total_chars + len(line) > 6000:
                obs_lines.append(f"... and {len(rows) - len(obs_lines)} more observations")
                break
            obs_lines.append(line)
            total_chars += len(line)

        prompt = (
            "Summarize this Claude Code session into a structured JSON object.\n\n"
            "Return ONLY valid JSON with these fields:\n"
            '- "summary": string — 2-3 sentence overview of what was accomplished\n'
            '- "key_decisions": string[] — important decisions, conclusions, or outcomes\n'
            '- "entities_mentioned": string[] — file paths, function names, services, tools referenced\n'
            '- "files_changed": string[] — file paths that were created or modified\n\n'
            f"Agent: {agent}\n"
            f"User prompt: {user_prompt or 'N/A'}\n"
            f"Observation count: {len(rows)}\n\n"
            "Observations:\n" + "\n".join(obs_lines)
        )

        # Call AI directly with our structured prompt (bypass compress() which
        # adds its own system prompt for observation compression)
        summary_text = await _call_ai_for_summary(prompt)
        if not summary_text:
            return None

        # Try to parse JSON from the response
        # Strip markdown code fences if present
        clean = summary_text.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)

        parsed = json.loads(clean)
        return parsed

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"AI session summary parse failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"AI session summary failed: {e}")
        return None


async def _call_ai_for_summary(prompt: str) -> Optional[str]:
    """Call AI (Anthropic OAuth) with a custom prompt for session summary."""
    if ai_compressor is None:
        return None

    try:
        if await ai_compressor._ensure_client():
            payload = {
                "model": AI_MODEL,
                "max_tokens": AI_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = await ai_compressor._client.post(ai_compressor.ANTHROPIC_URL, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return data["content"][0]["text"]
            elif resp.status_code in (429, 529):
                ai_compressor._record_failure(
                    f"session summary rate limited ({resp.status_code})",
                    is_rate_limit=True,
                )
            else:
                ai_compressor._record_failure(
                    f"session summary HTTP {resp.status_code}",
                    is_rate_limit=False,
                )
    except Exception as e:
        logger.debug(f"OAuth session summary failed: {e}")

    return None


def _rule_based_session_summary(
    rows: list,
    user_prompt: Optional[str],
    agent: str,
) -> dict:
    """Generate a rule-based session summary from observations."""
    # Aggregate tool usage
    tool_counts: dict[str, int] = {}
    file_paths: set[str] = set()
    decisions: list[str] = []

    for r in rows:
        tool = r["tool_name"]
        if tool:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1

        summary = r["summary"] or ""
        raw_input = r["raw_input"] or ""

        # Extract file paths from summaries and inputs
        for text in (summary, raw_input):
            paths = re.findall(r'(?:/[\w./-]+\.[\w]+)', text)
            file_paths.update(p for p in paths if len(p) > 5)

        # Extract decisions/conclusions from summaries
        if tool in ("Write", "Edit") and summary:
            decisions.append(summary[:150])

    # Build summary text
    top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:5]
    tool_str = ", ".join(f"{t}({c})" for t, c in top_tools)

    parts = [f"[{agent}] Session with {len(rows)} observations."]
    if user_prompt:
        parts.append(f"Task: {user_prompt[:200]}")
    if tool_str:
        parts.append(f"Tools: {tool_str}")
    if file_paths:
        paths_str = ", ".join(sorted(file_paths)[:10])
        parts.append(f"Files: {paths_str}")

    return {
        "summary": " | ".join(parts),
        "key_decisions": decisions[:10],
        "entities_mentioned": sorted(file_paths)[:20],
    }


async def _summarize_unsummarized_sessions():
    """Background task: catch up on sessions that ended but were never summarized."""
    await asyncio.sleep(120)  # Startup delay — let processor handle pending obs first
    logger.info("Session summarization catch-up started")

    while not shutdown_event.is_set():
        try:
            if db is None or db_lock is None:
                await asyncio.sleep(60)
                continue

            async with db_lock:
                rows = db.execute(
                    "SELECT id FROM sessions "
                    "WHERE status = 'ended' "
                    "ORDER BY ended_at ASC LIMIT 5"
                ).fetchall()

            for row in rows:
                if shutdown_event.is_set():
                    break
                await _generate_session_summary(row["id"])
                await asyncio.sleep(2)  # Rate control between summaries

        except Exception as e:
            logger.error(f"Session summarization catch-up error: {e}", exc_info=True)

        # Check every 5 minutes for unsummarized sessions
        for _ in range(300):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)


# ── FastAPI app ─────────────────────────────────────────────────────────────

start_time = time.time()


async def _processor_watchdog():
    """Watchdog that restarts the processor, retention, and summarization loops if they die."""
    global processor_task, retention_task, summarization_task
    while not shutdown_event.is_set():
        await asyncio.sleep(30)
        if processor_task and processor_task.done() and not shutdown_event.is_set():
            exc = processor_task.exception() if not processor_task.cancelled() else None
            logger.error(f"Processor task died unexpectedly! Exception: {exc}")
            logger.info("Restarting processor task...")
            processor_task = asyncio.create_task(process_pending_observations())
        if retention_task and retention_task.done() and not shutdown_event.is_set():
            exc = retention_task.exception() if not retention_task.cancelled() else None
            logger.error(f"Retention task died unexpectedly! Exception: {exc}")
            logger.info("Restarting retention task...")
            retention_task = asyncio.create_task(run_retention_loop())
        if summarization_task and summarization_task.done() and not shutdown_event.is_set():
            exc = summarization_task.exception() if not summarization_task.cancelled() else None
            logger.error(f"Summarization task died unexpectedly! Exception: {exc}")
            logger.info("Restarting summarization task...")
            summarization_task = asyncio.create_task(_summarize_unsummarized_sessions())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage worker lifecycle."""
    global db, db_lock, processor_task, retention_task, summarization_task, retention_manager, ai_compressor, entity_extractor, kg, quota_manager

    logger.info(f"Cortex Worker starting on port {WORKER_PORT}")

    # Write PID file
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Initialize database and async lock
    db = init_db()
    db_lock = asyncio.Lock()
    quota_manager = QuotaManager(db)
    logger.info(f"Database initialized at {DB_PATH}")

    # Initialize AI compressor
    ai_compressor = AICompressor()
    logger.info("AI compressor initialized")

    # Initialize NER + Knowledge Graph
    if NER_ENABLED:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph()
            entity_extractor = EntityExtractor()
            logger.info(
                "NER enabled: knowledge graph loaded (%d entities, %d relationships)",
                kg.graph.number_of_nodes(), kg.graph.number_of_edges(),
            )
        except Exception as e:
            logger.warning("NER initialization failed (continuing without NER): %s", e)
            kg = None
            entity_extractor = None
    else:
        logger.info("NER disabled via CORTEX_NER_ENABLED=false")

    # Start background processor, retention loop, summarization catch-up, and watchdog
    processor_task = asyncio.create_task(process_pending_observations())
    retention_manager = RetentionManager()
    retention_task = asyncio.create_task(run_retention_loop())
    summarization_task = asyncio.create_task(_summarize_unsummarized_sessions())
    watchdog_task = asyncio.create_task(_processor_watchdog())
    logger.info("Background processor + retention + summarization started (with watchdog)")

    yield

    # Shutdown
    logger.info("Shutting down...")
    shutdown_event.set()
    for task in [processor_task, retention_task, summarization_task, watchdog_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    if db:
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        db.close()

    if PID_FILE.exists():
        PID_FILE.unlink()

    logger.info("Cortex Worker stopped")


app = FastAPI(
    title="Cortex Memory Worker",
    description="Background observation processor for Cami's memory system",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    if db is None or db_lock is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    async with db_lock:
        pending = db.execute(
            "SELECT COUNT(*) as c FROM observations WHERE status = 'pending'"
        ).fetchone()["c"]
        total = db.execute("SELECT COUNT(*) as c FROM observations").fetchone()["c"]
        active = db.execute(
            "SELECT COUNT(*) as c FROM sessions WHERE status = 'active'"
        ).fetchone()["c"]

    # Report processor status
    proc_status = "running"
    if processor_task and processor_task.done():
        proc_status = "dead"

    return HealthResponse(
        status="healthy" if proc_status == "running" else "degraded",
        uptime_seconds=round(time.time() - start_time, 1),
        pending_observations=pending,
        total_observations=total,
        active_sessions=active,
    )


@app.post("/api/observations", dependencies=[Depends(require_auth)])
async def receive_observation(req: ObservationRequest):
    """Receive an observation from a hook. Returns immediately (fire-and-forget)."""
    if db is None or db_lock is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    tier_value = parse_tier(req.subscription_tier).value

    # Rate limit check
    allowed, reason = subscription_rate_limiter.check(req.session_id, tier_value)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=reason,
        )

    now = datetime.now(timezone.utc).isoformat()

    async with db_lock:
        if quota_manager is not None:
            quota_ok, used_tokens, budget = quota_manager.consume(
                tier_value, req.truncated_input(), req.truncated_output()
            )
            if not quota_ok:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Daily token quota exceeded for {tier_value}: "
                        f"{used_tokens}/{budget} estimated tokens consumed"
                    ),
                )

        db.execute(
            "INSERT INTO observations (session_id, timestamp, source, tool_name, "
            "agent, raw_input, raw_output, subscription_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                req.session_id,
                now,
                req.source,
                req.tool_name,
                req.agent,
                req.truncated_input(),
                req.truncated_output(),
                tier_value,
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
        f"source={req.source} tool={req.tool_name} tier={tier_value}"
    )
    return {"status": "queued"}


@app.post("/api/sessions/start", dependencies=[Depends(require_auth)])
async def start_session(req: SessionStartRequest):
    """Register a new session."""
    if db is None or db_lock is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    tier_value = parse_tier(req.subscription_tier).value
    now = datetime.now(timezone.utc).isoformat()

    async with db_lock:
        # Upsert — session might already exist from a hook that fired before this
        db.execute(
            "INSERT INTO sessions (id, agent, started_at, user_prompt, subscription_tier) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET user_prompt = excluded.user_prompt, "
            "subscription_tier = excluded.subscription_tier",
            (req.session_id, req.agent, now, req.user_prompt, tier_value),
        )
        db.commit()

    logger.info(
        f"Session started: {req.session_id[:8]}... agent={req.agent} tier={tier_value}"
    )
    return {"status": "started", "session_id": req.session_id}


@app.post("/api/sessions/end", dependencies=[Depends(require_auth)])
async def end_session(req: SessionEndRequest):
    """End a session, mark for summarization, and trigger async summary generation."""
    if db is None or db_lock is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    now = datetime.now(timezone.utc).isoformat()

    async with db_lock:
        db.execute(
            "UPDATE sessions SET ended_at = ?, status = 'ended' WHERE id = ?",
            (now, req.session_id),
        )
        db.commit()

        # Count observations for this session
        count = db.execute(
            "SELECT COUNT(*) as c FROM observations WHERE session_id = ?",
            (req.session_id,),
        ).fetchone()["c"]

    logger.info(
        f"Session ended: {req.session_id[:8]}... observations={count}"
    )

    # Trigger async session summarization (fire-and-forget)
    asyncio.create_task(_generate_session_summary(req.session_id))

    return {"status": "ended", "observation_count": count}


@app.get("/api/observations/recent")
async def get_recent_observations(
    limit: int = 20,
    offset: int = 0,
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
):
    """Get recent observations with optional filters and pagination."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    where = "WHERE 1=1"
    params = []

    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    if source:
        where += " AND source = ?"
        params.append(source)
    if status:
        where += " AND status = ?"
        params.append(status)

    async with db_lock:
        # Get total count
        total = db.execute(f"SELECT COUNT(*) as c FROM observations {where}", params).fetchone()["c"]

        # Fetch page
        query = f"SELECT * FROM observations {where} ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = db.execute(query, params).fetchall()

    return {
        "observations": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/sessions/recent")
async def get_recent_sessions(limit: int = 10, offset: int = 0):
    """Get recent sessions with pagination."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    async with db_lock:
        total = db.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        rows = db.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {
        "sessions": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


# ── Cortex memory retrieval API (for OpenClaw / Cami tools) ───────────────────


def _get_retriever():
    """Return a MemoryRetriever instance (same DB as worker)."""
    return MemoryRetriever(obs_db_path=DB_PATH, vec_db_path=VEC_DATA_DIR / "cortex-vectors.db")


@app.post("/api/memory/search")
async def memory_search(req: MemorySearchRequest):
    """L1: Search cortex memory (compact index). Used by Cami via OpenClaw tools."""
    loop = asyncio.get_event_loop()
    def _run():
        r = _get_retriever()
        results = r.search(
            query=req.query,
            limit=req.limit or 15,
            source=req.source,
            agent=req.agent,
        )
        return [dict(x) for x in results]
    try:
        results = await loop.run_in_executor(None, _run)
        return {"results": results}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("memory search failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/memory/timeline")
async def memory_timeline(req: MemoryTimelineRequest):
    """L2: Chronological context around an observation. Used by Cami via OpenClaw tools."""
    loop = asyncio.get_event_loop()
    def _run():
        r = _get_retriever()
        return r.timeline(observation_id=req.observation_id, window=req.window or 5)
    try:
        context = await loop.run_in_executor(None, _run)
        return {"context": [dict(x) for x in context]}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("memory timeline failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/memory/details")
async def memory_details(req: MemoryDetailsRequest):
    """L3: Full observation details. Used by Cami via OpenClaw tools."""
    if not req.observation_ids:
        return {"details": []}
    loop = asyncio.get_event_loop()
    def _run():
        r = _get_retriever()
        return r.get_details(req.observation_ids)
    try:
        details = await loop.run_in_executor(None, _run)
        return {"details": [dict(x) for x in details]}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("memory details failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/memory/save", dependencies=[Depends(require_auth)])
async def memory_save(req: MemorySaveRequest):
    """Save an explicit memory. Used by Cami via OpenClaw tools."""
    metadata = {}
    if req.tags:
        metadata["tags"] = req.tags
    loop = asyncio.get_event_loop()
    def _run():
        r = _get_retriever()
        return r.save_memory(req.content, metadata)
    try:
        mem_id = await loop.run_in_executor(None, _run)
        return {"id": mem_id, "status": "saved"}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("memory save failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/compression/status")
async def compression_status():
    """AI compression health status. Check this when alerted."""
    if ai_compressor is None:
        return {"status": "disabled", "reason": "AI compressor not initialized"}

    healthy = ai_compressor._consecutive_failures == 0
    now = time.monotonic()
    backoff_remaining = max(0, ai_compressor._backoff_until - now)
    return {
        "status": "healthy" if healthy else "degraded",
        "ai_compressed": ai_compressor._ai_count,
        "ai_fallbacks": ai_compressor._fallback_count,
        "consecutive_failures": ai_compressor._consecutive_failures,
        "alerted": ai_compressor._alerted,
        "last_failure_reason": ai_compressor._last_failure_reason,
        "degraded_since": ai_compressor._degraded_since,
        "backoff_remaining_s": round(backoff_remaining, 1),
        "next_probe_in_s": round(
            max(0, AI_RECOVERY_PROBE_INTERVAL - (now - ai_compressor._last_probe_time)),
            1,
        ),
        "active_path": "oauth",
    }


@app.post("/api/compression/reset", dependencies=[Depends(require_auth)])
async def compression_reset():
    """Manually reset AI compression from degraded state. Use after fixing the root cause."""
    if ai_compressor is None:
        return {"status": "disabled"}

    prev_failures = ai_compressor._consecutive_failures
    ai_compressor._consecutive_failures = 0
    ai_compressor._alerted = False
    ai_compressor._last_failure_reason = None
    ai_compressor._degraded_since = None
    ai_compressor._backoff_until = 0
    ai_compressor._last_probe_time = 0
    logger.info(f"AI compression manually reset (was at {prev_failures} failures)")
    return {"status": "reset", "previous_failures": prev_failures}


@app.get("/api/stats")
async def get_stats():
    """Get worker statistics."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    stats = {}
    async with db_lock:
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

    # AI compression stats (in-memory, no lock needed)
    if ai_compressor:
        stats["ai_compressed"] = ai_compressor._ai_count
        stats["ai_fallbacks"] = ai_compressor._fallback_count

    return stats


# ── Retention endpoints ────────────────────────────────────────────────


@app.get("/api/retention/stats")
async def get_retention_stats():
    """Current retention state: rows by age tier, cleanup candidates, last run."""
    if retention_manager is None:
        raise HTTPException(status_code=503, detail="Retention manager not initialized")
    return await retention_manager.get_stats()


@app.post("/api/retention/run", dependencies=[Depends(require_auth)])
async def trigger_retention(dry_run: bool = False):
    """Manually trigger a retention pass. Use ?dry_run=true to preview."""
    if retention_manager is None:
        raise HTTPException(status_code=503, detail="Retention manager not initialized")
    stats = await retention_manager.run_once(dry_run=dry_run)
    return stats


# ── NER endpoints ──────────────────────────────────────────────────────


@app.get("/api/ner/stats")
async def get_ner_stats():
    """NER extraction statistics: total entities, relationships, last run."""
    if entity_extractor is None:
        return {
            "enabled": NER_ENABLED,
            "status": "disabled" if not NER_ENABLED else "initialization_failed",
            "entities_extracted_total": 0,
            "relationships_created_total": 0,
            "last_extraction_time": None,
            "knowledge_graph": None,
        }

    stats = entity_extractor.get_stats()

    # Include knowledge graph summary if available
    if kg is not None:
        stats["knowledge_graph"] = {
            "entities": kg.graph.number_of_nodes(),
            "relationships": kg.graph.number_of_edges(),
        }
    else:
        stats["knowledge_graph"] = None

    return stats


# ── Profile endpoints ─────────────────────────────────────────────────────


@app.get("/api/profile")
async def get_profile():
    """Return all user profile entries grouped by category, ordered by confidence."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    async with db_lock:
        rows = db.execute(
            "SELECT category, key, value, confidence, updated_at "
            "FROM profile ORDER BY category ASC, confidence DESC"
        ).fetchall()

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        cat = r["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({
            "key": r["key"],
            "value": r["value"],
            "confidence": r["confidence"],
            "updated_at": r["updated_at"],
        })

    return {"profile": grouped, "total_entries": len(rows)}


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
