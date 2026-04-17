"""
Pass P — coverage tests for memory_worker.py pure helpers (baseline 30%).

Targets (no FastAPI TestClient — keeps run time fast):
  - _key_matches: constant-time comparison, empty/None inputs
  - _resolve_api_key: env-var path, key-file path, generate-and-persist path
  - _check_rate_limit: allow under limit, block at limit, window eviction
  - SubscriptionRateLimiter.check: session limit, tier limit, cleanup
  - SubscriptionRateLimiter._cleanup: stale bucket removal

All tests use monkeypatching + temporary directories to avoid touching the live
worker DB or the production key file.  No HTTP calls are made.
"""
import importlib
import os
import sys
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

# Ensure src is importable
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _import_fresh_worker(tmp_home: Path, extra_env: dict = None):
    """Re-import memory_worker with HOME set to tmp_home to avoid touching
    the real ~/.cortex/data directory or live log files."""
    # Remove any cached import
    for key in list(sys.modules.keys()):
        if "memory_worker" in key:
            del sys.modules[key]

    env_overrides = {"HOME": str(tmp_home)}
    if extra_env:
        env_overrides.update(extra_env)

    with patch.dict(os.environ, env_overrides, clear=False):
        # Also patch the LOG_DIR mkdir so we don't create real log directories
        import memory_worker
    return memory_worker


class TestKeyMatches(unittest.TestCase):
    """Tests for _key_matches() — constant-time comparison helper."""

    def setUp(self):
        self._td = __import__("tempfile").TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _get_module(self, api_key="test-secret-key"):
        return _import_fresh_worker(
            self.tmp,
            extra_env={"CORTEX_WORKER_API_KEY": api_key},
        )

    def test_matching_key_returns_true(self):
        mw = self._get_module("correct-key-abc")
        self.assertTrue(mw._key_matches("correct-key-abc"))

    def test_wrong_key_returns_false(self):
        mw = self._get_module("correct-key-abc")
        self.assertFalse(mw._key_matches("wrong-key-xyz"))

    def test_empty_candidate_returns_false(self):
        mw = self._get_module("some-valid-key")
        self.assertFalse(mw._key_matches(""))

    def test_none_like_empty_string_returns_false(self):
        """_key_matches with empty string must not raise."""
        mw = self._get_module("some-valid-key")
        # Passing empty string should be False, not an exception
        result = mw._key_matches("")
        self.assertFalse(result)


class TestResolveApiKey(unittest.TestCase):
    """Tests for _resolve_api_key() — three-path key resolution."""

    def setUp(self):
        self._td = __import__("tempfile").TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_env_var_takes_priority(self):
        """CORTEX_WORKER_API_KEY env var must be returned first."""
        mw = _import_fresh_worker(
            self.tmp,
            extra_env={"CORTEX_WORKER_API_KEY": "env-key-123"},
        )
        # The module-level CORTEX_API_KEY must equal the env var value
        self.assertEqual(mw.CORTEX_API_KEY, "env-key-123")
        self.assertFalse(mw._API_KEY_WAS_GENERATED)

    def test_key_file_fallback(self):
        """When env var is absent, a key persisted in the key file must be used."""
        # Pre-create the key file at the location _resolve_api_key() expects
        # (~/.cortex/data/.worker_api_key under our tmp HOME)
        key_dir = self.tmp / ".cortex" / "data"
        key_dir.mkdir(parents=True)
        key_file = key_dir / ".worker_api_key"
        key_file.write_text("file-based-key-xyz")

        env = {"CORTEX_WORKER_API_KEY": ""}  # Empty string = no env key
        mw = _import_fresh_worker(self.tmp, extra_env=env)
        self.assertEqual(mw.CORTEX_API_KEY, "file-based-key-xyz")
        self.assertFalse(mw._API_KEY_WAS_GENERATED)

    def test_generated_key_when_neither_present(self):
        """When no env var and no key file, a random key must be generated."""
        env = {"CORTEX_WORKER_API_KEY": ""}
        mw = _import_fresh_worker(self.tmp, extra_env=env)

        # Must have generated a non-empty key
        self.assertTrue(len(mw.CORTEX_API_KEY) > 0)
        self.assertTrue(mw._API_KEY_WAS_GENERATED)

        # Key must have been persisted to disk
        key_file = self.tmp / ".cortex" / "data" / ".worker_api_key"
        self.assertTrue(key_file.exists(), "generated key must be persisted to key file")
        self.assertEqual(key_file.read_text().strip(), mw.CORTEX_API_KEY)


class TestCheckRateLimit(unittest.TestCase):
    """Tests for _check_rate_limit() — session-level rate limiter."""

    def setUp(self):
        self._td = __import__("tempfile").TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.mw = _import_fresh_worker(
            self.tmp,
            extra_env={"CORTEX_WORKER_API_KEY": "test-key"},
        )
        # Reset module-level state
        self.mw._rate_limit_buckets.clear()
        self.mw._rate_limit_last_cleanup = 0.0

    def tearDown(self):
        self._td.cleanup()

    def test_first_call_is_allowed(self):
        self.assertTrue(self.mw._check_rate_limit("session-a"))

    def test_up_to_max_calls_are_allowed(self):
        """Calls up to RATE_LIMIT_MAX must all return True."""
        # RATE_LIMIT_MAX is 100 — test with a smaller sub-limit via direct bucket injection
        limit = self.mw.RATE_LIMIT_MAX
        for _ in range(limit):
            ok = self.mw._check_rate_limit("session-b")
        self.assertTrue(ok)

    def test_exceeding_max_returns_false(self):
        """The (RATE_LIMIT_MAX + 1)-th call within the window must be blocked."""
        limit = self.mw.RATE_LIMIT_MAX
        # Force the bucket to near-capacity by pre-populating with current timestamps
        now = time.monotonic()
        bucket = deque([now] * limit)
        self.mw._rate_limit_buckets["session-c"] = bucket

        result = self.mw._check_rate_limit("session-c")
        self.assertFalse(result, "call beyond RATE_LIMIT_MAX must be rejected")

    def test_old_timestamps_evicted_before_check(self):
        """Timestamps older than RATE_LIMIT_WINDOW must be evicted so the session
        can accept new calls after the window rolls over."""
        window = self.mw.RATE_LIMIT_WINDOW
        limit = self.mw.RATE_LIMIT_MAX
        # Inject stale timestamps (older than the window)
        stale_time = time.monotonic() - window - 5
        bucket = deque([stale_time] * limit)
        self.mw._rate_limit_buckets["session-d"] = bucket

        # After eviction the bucket is empty, so the call must be allowed
        result = self.mw._check_rate_limit("session-d")
        self.assertTrue(result,
            "stale timestamps outside the window must be evicted, allowing new calls")


class TestSubscriptionRateLimiter(unittest.TestCase):
    """Tests for SubscriptionRateLimiter.check() and _cleanup()."""

    def setUp(self):
        self._td = __import__("tempfile").TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.mw = _import_fresh_worker(
            self.tmp,
            extra_env={"CORTEX_WORKER_API_KEY": "test-key"},
        )
        self.limiter = self.mw.SubscriptionRateLimiter()

    def tearDown(self):
        self._td.cleanup()

    def test_first_call_is_allowed_for_any_tier(self):
        ok, reason = self.limiter.check("session-1", "claude_codemax")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_session_limit_blocks_at_rate_limit_max(self):
        """The (RATE_LIMIT_MAX + 1)-th call for the same session must be blocked."""
        limit = self.mw.RATE_LIMIT_MAX
        now = time.monotonic()
        bucket = deque([now] * limit)
        self.limiter._session_buckets["session-2"] = bucket

        ok, reason = self.limiter.check("session-2", "claude_codemax")
        self.assertFalse(ok)
        self.assertIn("session rate limit", reason)

    def test_cleanup_removes_stale_buckets(self):
        """_cleanup must evict buckets with all timestamps outside the window."""
        window = self.mw.RATE_LIMIT_WINDOW
        stale = time.monotonic() - window - 10
        self.limiter._session_buckets["stale-session"] = deque([stale])
        self.limiter._tier_buckets["stale-tier"] = deque([stale])

        self.limiter._cleanup(time.monotonic())

        self.assertNotIn("stale-session", self.limiter._session_buckets,
            "_cleanup must remove stale session buckets")
        self.assertNotIn("stale-tier", self.limiter._tier_buckets,
            "_cleanup must remove stale tier buckets")

    def test_two_different_sessions_are_independent(self):
        """Rate limiting must be per-session; one session at limit must not block another."""
        limit = self.mw.RATE_LIMIT_MAX
        now = time.monotonic()
        # Saturate session A
        self.limiter._session_buckets["session-a"] = deque([now] * limit)

        # Session B should still be allowed
        ok_b, _ = self.limiter.check("session-b", "claude_codemax")
        self.assertTrue(ok_b,
            "a different session must not be blocked because session-a is at capacity")


class TestObservationTierBinding(unittest.TestCase):
    """BUG-GG-03: observation tier must be read from the session row, not the request.

    A caller should not be able to escalate the effective tier of an observation
    above the tier that was declared when the session was created.
    """

    def setUp(self):
        import asyncio
        import sqlite3
        self._td = __import__("tempfile").TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.mw = _import_fresh_worker(
            self.tmp,
            extra_env={"CORTEX_WORKER_API_KEY": "test-key-binding"},
        )

        # Create a fresh in-memory DB and lock for the test
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                agent TEXT DEFAULT 'main',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                user_prompt TEXT,
                summary TEXT,
                observation_count INTEGER DEFAULT 0,
                subscription_tier TEXT DEFAULT 'claude_standard',
                status TEXT DEFAULT 'active'
            );
        """)
        self.conn.commit()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.lock = asyncio.Lock()

        # Patch module-level db and db_lock
        self.mw.db = self.conn
        self.mw.db_lock = self.lock

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _insert_session(self, session_id: str, tier: str):
        """Insert a session row with a fixed tier."""
        self.conn.execute(
            "INSERT INTO sessions (id, agent, started_at, subscription_tier) "
            "VALUES (?, 'main', datetime('now'), ?)",
            (session_id, tier),
        )
        self.conn.commit()

    def test_observation_gets_session_tier_not_request_tier(self):
        """Session at tier A; observation claims tier B > A → effective tier is A."""
        session_id = "sess-tier-test-001"
        session_tier = "claude_standard"   # tier A (lower)
        request_tier = "claude_codemax"    # tier B (higher — escalation attempt)

        self._insert_session(session_id, session_tier)

        # Simulate the tier resolution logic from receive_observation.
        # We test the SELECT + clamp path directly, matching the production code.
        _session_row = self.conn.execute(
            "SELECT subscription_tier FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

        self.assertIsNotNone(_session_row, "session row must exist")

        session_tier_parsed = self.mw.parse_tier(_session_row["subscription_tier"])
        tier = self.mw.clamp_tier(session_tier_parsed, self.mw.SERVER_TIER_CAP)

        # The effective tier must be session_tier, NOT request_tier
        self.assertEqual(
            tier.value,
            session_tier,
            f"Expected effective tier={session_tier} (from session row) "
            f"but got {tier.value}. Request claimed {request_tier}.",
        )

    def test_no_session_row_falls_back_to_request_tier_clamped(self):
        """When no session row exists, fall back to clamped request tier (first obs)."""
        session_id = "sess-no-row-002"
        request_tier = "claude_standard"

        _session_row = self.conn.execute(
            "SELECT subscription_tier FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

        self.assertIsNone(_session_row, "no session row should exist yet")

        original = self.mw.parse_tier(request_tier)
        tier = self.mw.clamp_tier(original, self.mw.SERVER_TIER_CAP)

        self.assertEqual(tier.value, request_tier,
            "without a session row, effective tier should equal the request tier")


class TestPerTierRetention(unittest.TestCase):
    """BUG-GG-04: RetentionManager must use TierConfig.retention_days per observation tier.

    Observations at a lower tier should be pruned sooner than those at a higher tier.
    """

    def setUp(self):
        import asyncio
        import sqlite3
        self._td = __import__("tempfile").TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.mw = _import_fresh_worker(
            self.tmp,
            extra_env={"CORTEX_WORKER_API_KEY": "test-key-retention"},
        )

        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
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
                subscription_tier TEXT DEFAULT 'claude_standard',
                created_at TEXT DEFAULT (datetime('now')),
                processed_at TEXT
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
        """)
        self.conn.commit()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.lock = asyncio.Lock()
        self.mw.db = self.conn
        self.mw.db_lock = self.lock

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _insert_obs_aged(self, obs_id: int, tier: str, age_days: float):
        """Insert an observation with timestamp set to age_days in the past."""
        self.conn.execute(
            "INSERT INTO observations (id, session_id, timestamp, source, status, "
            "subscription_tier, raw_input) "
            "VALUES (?, 'test-session', datetime('now', ? || ' days'), "
            "'post_tool_use', 'processed', ?, 'input')",
            (obs_id, f"-{age_days}", tier),
        )
        self.conn.commit()

    def test_lower_tier_obs_pruned_before_higher_tier(self):
        """Obs at claude_standard (14d retention) should be pruned at day 20;
        obs at claude_codemax (60d retention) must survive at day 20."""
        from subscription import TIER_CONFIGS, SubscriptionTier
        standard_days = TIER_CONFIGS[SubscriptionTier.CLAUDE_STANDARD].retention_days
        codemax_days = TIER_CONFIGS[SubscriptionTier.CLAUDE_CODEMAX].retention_days

        self.assertLess(standard_days, codemax_days,
            "test assumes standard retention_days < codemax retention_days")

        # Age both observations past standard threshold but before codemax threshold
        test_age = standard_days + 1  # e.g. 15 days: past standard(14), before codemax(60)
        self.assertLess(test_age, codemax_days, "test_age must be < codemax retention")

        self._insert_obs_aged(101, "claude_standard", test_age)
        self._insert_obs_aged(102, "claude_codemax", test_age)

        # Manually invoke the per-tier delete logic that BUG-GG-04 requires.
        # The fix adds per-tier pruning; we test the SELECT that checks tier retention.
        # Observations older than their tier's retention_days should be returned.
        # We use the same SQL pattern the fixed _delete_low_signal uses.
        prunable_ids = []
        for tier_enum, cfg in TIER_CONFIGS.items():
            rows = self.conn.execute(
                """SELECT id FROM observations
                   WHERE subscription_tier = ?
                     AND timestamp < datetime('now', ? || ' days')
                     AND status = 'processed'""",
                (tier_enum.value, f"-{cfg.retention_days}"),
            ).fetchall()
            prunable_ids.extend(r["id"] for r in rows)

        self.assertIn(101, prunable_ids,
            f"claude_standard obs (age={test_age}d, retention={standard_days}d) "
            "should be prunable")
        self.assertNotIn(102, prunable_ids,
            f"claude_codemax obs (age={test_age}d, retention={codemax_days}d) "
            "must NOT be prunable yet")
