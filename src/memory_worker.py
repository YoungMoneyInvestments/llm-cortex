#!/usr/bin/env python3
"""
Memory Worker Service — Background observation processor for Cortex.

Receives observations from lifecycle hooks (PostToolUse, UserPromptSubmit,
SessionEnd) via HTTP, queues them in SQLite, and processes them asynchronously
(AI compression, vector embedding, knowledge graph extraction).

Inspired by claude-mem's async worker pattern, adapted for a Python-first stack.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator, model_validator
import uvicorn

# ── Config ──────────────────────────────────────────────────────────────────

def _optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def _path_from_env(name: str, default: Optional[Path]) -> Optional[Path]:
    value = _optional_env(name)
    if value:
        return Path(value).expanduser()
    return default


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _default_cortex_home() -> Path:
    return Path.home() / ".cortex"


def _derive_camirouter_models_url(chat_url: Optional[str]) -> Optional[str]:
    if not chat_url:
        return None
    stripped = chat_url.rstrip("/")
    suffix = "/chat/completions"
    if stripped.endswith(suffix):
        return stripped[: -len(suffix)] + "/models"
    return stripped + "/models"


WORKER_PORT = _int_from_env("CORTEX_WORKER_PORT", 37778)
DATA_DIR = _path_from_env("CORTEX_DATA_DIR", _default_cortex_home() / "data")
DB_PATH = DATA_DIR / "cortex-observations.db"
PID_FILE = _path_from_env("CORTEX_PID_FILE", _default_cortex_home() / "worker.pid")
LOG_DIR = _path_from_env("CORTEX_LOG_DIR", _default_cortex_home() / "logs")
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
CORTEX_API_KEY = _optional_env("CORTEX_WORKER_API_KEY")

# ── Rate limiting config ───────────────────────────────────────────────
RATE_LIMIT_MAX = 100           # max observations per minute per session_id
RATE_LIMIT_WINDOW = 60         # window in seconds
RATE_LIMIT_CLEANUP_INTERVAL = 300  # cleanup old entries every 5 minutes

# ── AI compression config ────────────────────────────────────────────
AI_MODEL = "claude-sonnet-4-6"
AI_MAX_TOKENS = 1024
AI_COMPRESSION_DELAY = 0.1       # seconds between API calls (rate control)
AI_FAILURE_ALERT_THRESHOLD = 3   # consecutive failures before alerting
AI_BACKOFF_BASE = 30             # base backoff seconds after rate limit
AI_BACKOFF_MAX = 600             # max backoff seconds (10 minutes)
AI_RECOVERY_PROBE_INTERVAL = 120 # seconds between health probes when degraded
AUTH_PROFILES_PATH = _path_from_env("CORTEX_AUTH_PROFILES_PATH", None)

# ── NER (Named Entity Recognition) config ────────────────────────────────
NER_ENABLED = os.environ.get("CORTEX_NER_ENABLED", "true").lower() == "true"

# ── CamiRouter config (preferred path — avoids OAuth rate limit contention) ──
CAMIROUTER_URL = _optional_env("CAMIROUTER_URL")
CAMIROUTER_MODELS_URL = _derive_camirouter_models_url(CAMIROUTER_URL)
CAMIROUTER_API_KEY = _optional_env("CAMIROUTER_API_KEY")
CAMIROUTER_MODEL = os.environ.get("CAMIROUTER_MODEL", "sonnet")

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


# ── API Authentication ─────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
):
    """Dependency that enforces bearer token auth on POST endpoints."""
    if not CORTEX_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "CORTEX_WORKER_API_KEY is not configured. Set the same bearer token "
                "in the worker and hook environments before using POST endpoints."
            ),
        )
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

# ── Pydantic models ─────────────────────────────────────────────────────────


class ObservationRequest(BaseModel):
    """Observation from a hook."""
    session_id: str
    source: Literal["post_tool_use", "user_prompt", "session_end"]
    tool_name: Optional[str] = None
    agent: str = "main"
    raw_input: Optional[str] = None
    raw_output: Optional[str] = None

    # Truncation limits to avoid bloating the DB
    MAX_INPUT_LEN: int = Field(default=4000, exclude=True)
    MAX_OUTPUT_LEN: int = Field(default=8000, exclude=True)

    @field_validator("session_id", "agent")
    @classmethod
    def _validate_nonempty_fields(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("tool_name")
    @classmethod
    def _normalize_tool_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _validate_tool_name_for_source(self):
        if self.source == "post_tool_use" and not self.tool_name:
            raise ValueError("tool_name is required when source='post_tool_use'")
        return self

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

    @field_validator("session_id", "agent")
    @classmethod
    def _validate_nonempty_fields(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SessionEndRequest(BaseModel):
    """End a session and trigger summarization."""
    session_id: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    pending_observations: int
    total_observations: int
    active_sessions: int
    configuration_issues: List[str] = Field(default_factory=list)


def get_configuration_issues() -> list[str]:
    """Return required configuration issues that affect the public worker surface."""
    issues: list[str] = []
    if not CORTEX_API_KEY:
        issues.append(
            "CORTEX_WORKER_API_KEY is not configured, so authenticated POST endpoints are unavailable."
        )
    if CAMIROUTER_URL and not CAMIROUTER_API_KEY:
        issues.append(
            "CAMIROUTER_URL is set without CAMIROUTER_API_KEY; router compression is disabled."
        )
    if CAMIROUTER_API_KEY and not CAMIROUTER_URL:
        issues.append(
            "CAMIROUTER_API_KEY is set without CAMIROUTER_URL; router compression is disabled."
        )
    return issues


# ── Background processor ───────────────────────────────────────────────────


async def process_pending_observations():
    """Background task: process pending observations.

    Currently generates simple summaries from raw data.
    Future: AI compression, vector embedding, graph extraction.
    """
    consecutive_errors = 0
    while not shutdown_event.is_set():
        try:
            if db is None or db_lock is None:
                await asyncio.sleep(PROCESS_INTERVAL)
                continue

            # Serialize DB access with route handlers
            async with db_lock:
                rows = db.execute(
                    "SELECT id, source, tool_name, agent, raw_input, raw_output "
                    "FROM observations WHERE status = 'pending' ORDER BY id LIMIT 20"
                ).fetchall()

            processed_count = 0
            for row in rows:
                if shutdown_event.is_set():
                    break
                obs_id = row["id"]
                try:
                    source = row["source"]
                    tool_name = row["tool_name"]
                    raw_input = row["raw_input"]

                    # Determine if this observation should get AI compression
                    use_ai = (
                        ai_compressor is not None
                        and ai_compressor.is_available()
                        and (
                            source == "user_prompt"
                            or source == "session_end"
                            or tool_name in HIGH_SIGNAL_TOOLS
                        )
                        # Skip task-notification system prompts
                        and not (
                            source == "user_prompt"
                            and raw_input
                            and raw_input.startswith("<task-notification>")
                        )
                    )

                    summary = None
                    if use_ai:
                        summary = await ai_compressor.compress(
                            source=source,
                            tool_name=tool_name,
                            agent=row["agent"],
                            raw_input=raw_input,
                            raw_output=row["raw_output"],
                        )
                        if summary is None:
                            ai_compressor._fallback_count += 1

                    if summary is None:  # AI failed or not applicable
                        summary = _generate_summary_rule_based(
                            source=source,
                            tool_name=tool_name,
                            agent=row["agent"],
                            raw_input=raw_input,
                            raw_output=row["raw_output"],
                        )

                    async with db_lock:
                        db.execute(
                            "UPDATE observations SET summary = ?, status = 'processed', "
                            "processed_at = ? WHERE id = ?",
                            (summary, datetime.now(timezone.utc).isoformat(), obs_id),
                        )
                        db.commit()

                    # Sync to vector store (if available)
                    await _sync_to_vector_store(obs_id, summary, row)

                    # NER: extract entities and link in knowledge graph
                    if (
                        NER_ENABLED
                        and entity_extractor is not None
                        and kg is not None
                        and (
                            source == "user_prompt"
                            or source == "session_end"
                            or tool_name in HIGH_SIGNAL_TOOLS
                        )
                        and summary
                        and len(summary) > 50
                    ):
                        await _extract_and_link_entities(
                            obs_id, summary,
                            row["raw_input"], row["raw_output"],
                        )

                    processed_count += 1

                    # Rate control between AI calls
                    if use_ai:
                        await asyncio.sleep(AI_COMPRESSION_DELAY)

                except Exception as e:
                    logger.error(f"Failed to process observation {obs_id}: {e}", exc_info=True)
                    try:
                        async with db_lock:
                            db.execute(
                                "UPDATE observations SET status = 'failed' WHERE id = ?",
                                (obs_id,),
                            )
                            db.commit()
                    except Exception:
                        pass  # Don't crash the loop for cleanup failures

            if processed_count > 0:
                logger.info(f"Processed {processed_count} observations")

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
    """AI-powered observation compression with dual-path routing.

    Primary path: an OpenAI-compatible router configured through
    CORTEX_ROUTER_URL / CAMIROUTER_URL so deployments can pick their own
    endpoint and credentials without changing code.

    Fallback path: Direct Anthropic OAuth — used if CamiRouter is down.
    """

    ANTHROPIC_URL = "https://api.anthropic.com/v1/messages?beta=true"
    OAUTH_HEADERS = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    }

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._router_client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._ai_count = 0
        self._fallback_count = 0
        self._consecutive_failures = 0
        self._alerted = False          # True once alert fires; reset on recovery
        self._last_failure_reason: Optional[str] = None
        self._degraded_since: Optional[str] = None
        self._backoff_until: float = 0  # time.monotonic() after which we can retry
        self._last_probe_time: float = 0  # last time we probed during degraded state
        self._using_router = False      # True when CamiRouter is the active path
        self._router_available = True   # False if CamiRouter probe failed

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

    async def _ensure_router_client(self) -> bool:
        """Set up or verify CamiRouter client."""
        try:
            if not CAMIROUTER_URL or not CAMIROUTER_API_KEY:
                return False
            if self._router_client is None:
                self._router_client = httpx.AsyncClient(
                    headers={
                        "Authorization": f"Bearer {CAMIROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )
            # Quick health check if we haven't confirmed availability
            if not self._router_available:
                resp = await self._router_client.get(
                    CAMIROUTER_MODELS_URL,
                    timeout=3.0,
                )
                self._router_available = resp.status_code == 200
            return self._router_available
        except Exception:
            self._router_available = False
            return False

    async def _ensure_client(self) -> bool:
        """Load or refresh the OAuth token from auth-profiles (fallback path)."""
        try:
            if AUTH_PROFILES_PATH is None:
                logger.debug(
                    "OAuth fallback disabled because CORTEX_AUTH_PROFILES_PATH is not configured"
                )
                return False

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
        """Compress an observation with AI. Tries CamiRouter first, falls back to OAuth."""
        # Track probe time when degraded (for self-healing probes)
        self._last_probe_time = time.monotonic()

        prompt = self._build_prompt(source, tool_name, agent, raw_input, raw_output)

        # Try CamiRouter first (separate rate limit pool)
        result = await self._compress_via_router(prompt)
        if result is not None:
            self._ai_count += 1
            self._using_router = True
            self._record_success()
            return result

        # Fall back to direct Anthropic OAuth
        result = await self._compress_via_oauth(prompt)
        if result is not None:
            self._ai_count += 1
            self._using_router = False
            self._record_success()
            return result

        return None

    async def _compress_via_router(self, prompt: str) -> Optional[str]:
        """Try compression through CamiRouter (OpenAI-compatible)."""
        try:
            if not await self._ensure_router_client():
                return None

            payload = {
                "model": CAMIROUTER_MODEL,
                "max_tokens": AI_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
            resp = await self._router_client.post(CAMIROUTER_URL, json=payload)

            if resp.status_code == 429:
                logger.debug("CamiRouter rate limited, trying OAuth fallback")
                return None

            if resp.status_code != 200:
                logger.debug(f"CamiRouter {resp.status_code}, trying OAuth fallback")
                self._router_available = False
                return None

            data = resp.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content")
            if text:
                return text
            return None

        except (httpx.ConnectError, httpx.TimeoutException):
            self._router_available = False
            return None
        except Exception as e:
            logger.debug(f"CamiRouter error: {e}")
            self._router_available = False
            return None

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
        """Build the compression prompt."""
        return (
            "Compress this tool observation into a dense, searchable summary.\n"
            "Capture: WHAT happened, WHY it matters, KEY details "
            "(file paths, function names, decisions, errors, outcomes).\n"
            "Drop: boilerplate, redundant context, formatting artifacts, "
            "system prompt noise.\n\n"
            f"Source: {source}\n"
            f"Tool: {tool_name or 'N/A'}\n"
            f"Agent: {agent}\n"
            f"Input: {raw_input or 'N/A'}\n"
            f"Output: {raw_output or 'N/A'}"
        )


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
        "Cortex",
        "PostgreSQL", "SQLite", "NetworkX", "FastAPI", "Uvicorn",
        "Playwright", "ChromaDB", "Tailscale", "Docker", "Nginx",
    }
    _KNOWN_SYSTEMS_LOWER: Dict[str, str] = {s.lower(): s for s in KNOWN_SYSTEMS}

    # ── Known project names ──
    KNOWN_PROJECTS: Set[str] = {
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

        entity_extractor._relationships_created_total += relationships_created

        logger.debug(
            "NER obs=%d: extracted %d entities, %d co_mentioned relationships",
            obs_id, len(entities), relationships_created,
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


async def _generate_session_summary(session_id: str):
    """Generate and store a summary for a completed session.

    Tries AI compression first (via CamiRouter/OAuth), falls back to
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
    """Call AI (CamiRouter or OAuth) with a custom prompt for session summary."""
    if ai_compressor is None:
        return None

    # Try CamiRouter first
    try:
        if await ai_compressor._ensure_router_client():
            payload = {
                "model": CAMIROUTER_MODEL,
                "max_tokens": AI_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
            resp = await ai_compressor._router_client.post(CAMIROUTER_URL, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content")
                if text:
                    return text
    except Exception as e:
        logger.debug(f"CamiRouter session summary failed: {e}")

    # Try OAuth fallback
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
    global db, db_lock, processor_task, retention_task, summarization_task, retention_manager, ai_compressor, entity_extractor, kg

    logger.info(f"Cortex Worker starting on port {WORKER_PORT}")

    # Write PID file
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Initialize database and async lock
    db = init_db()
    db_lock = asyncio.Lock()
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
        db.close()

    if PID_FILE.exists():
        PID_FILE.unlink()

    logger.info("Cortex Worker stopped")


app = FastAPI(
    title="Cortex Memory Worker",
    description="Background observation processor for the Cortex memory system",
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
        status="healthy" if proc_status == "running" and not get_configuration_issues() else "degraded",
        uptime_seconds=round(time.time() - start_time, 1),
        pending_observations=pending,
        total_observations=total,
        active_sessions=active,
        configuration_issues=get_configuration_issues(),
    )


@app.post("/api/observations", dependencies=[Depends(require_auth)])
async def receive_observation(req: ObservationRequest):
    """Receive an observation from a hook. Returns immediately (fire-and-forget)."""
    if db is None or db_lock is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Rate limit check
    if not _check_rate_limit(req.session_id):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX} observations per minute per session",
        )

    now = datetime.now(timezone.utc).isoformat()

    async with db_lock:
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


@app.post("/api/sessions/start", dependencies=[Depends(require_auth)])
async def start_session(req: SessionStartRequest):
    """Register a new session."""
    if db is None or db_lock is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    now = datetime.now(timezone.utc).isoformat()

    async with db_lock:
        # Upsert — session might already exist from a hook that fired before this
        db.execute(
            "INSERT INTO sessions (id, agent, started_at, user_prompt) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET user_prompt = excluded.user_prompt",
            (req.session_id, req.agent, now, req.user_prompt),
        )
        db.commit()

    logger.info(f"Session started: {req.session_id[:8]}... agent={req.agent}")
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
        "active_path": "camirouter" if ai_compressor._using_router else "oauth",
        "router_available": ai_compressor._router_available,
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

    # AI compression stats
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
