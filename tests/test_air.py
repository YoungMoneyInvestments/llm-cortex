"""
Tests for AIR (Adaptive Inference Routing) integrated into llm-cortex.

Port of the 37 original AIR tests adapted for cortex schema:
  - Config reads cortex_db_path + routes_db_path
  - Harvester reads from cortex-observations.db (or test fixture)
  - Storage only holds routing_rules (no tool_events duplication)
  - Compiler uses CortexHarvester for event queries

Plus integration tests for end-to-end pipeline and CLI.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.air.config import AIRConfig
from src.air.storage import RoutingStorage
from src.air.harvester import CortexHarvester
from src.air.classifier import IntentClassifier
from src.air.compiler import PatternCompiler
from src.air.scorer import ConfidenceScorer
from src.air.router import RoutingRouter
from src.air.injector import RouteInjector


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def config(tmp_dir):
    return AIRConfig(
        data_dir=tmp_dir,
        cortex_db_path=tmp_dir / "cortex-test.db",
        routes_db_path=tmp_dir / "air-routes-test.db",
        classifier_mode="local",
        confidence_init=0.5,
        confidence_reward=0.1,
        confidence_penalty=0.2,
        confidence_decay_rate=0.95,
        prune_threshold=0.2,
        inject_threshold_high=0.7,
        inject_threshold_low=0.5,
        cold_start_cycles=10,
        cross_project_threshold=0.9,
        anthropic_api_key=None,
    )


@pytest.fixture
def cortex_db(config):
    """Create a test cortex-observations.db with sample tool events."""
    db_path = config.cortex_db_path
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
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
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _insert_observation(db_path, session_id, tool_name, raw_input="",
                        raw_output="", summary="", source="post_tool_use",
                        timestamp=None):
    """Helper: insert a single observation into the test cortex DB."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO observations
           (session_id, timestamp, source, tool_name, raw_input, raw_output, summary)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, timestamp, source, tool_name, raw_input, raw_output, summary),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def storage(config):
    return RoutingStorage(db_path=config.routes_db_path)


@pytest.fixture
def scorer(config):
    return ConfidenceScorer(config)


@pytest.fixture
def harvester(config, cortex_db):
    return CortexHarvester(config, cortex_db_path=cortex_db)


@pytest.fixture
def classifier(config):
    return IntentClassifier(config)


@pytest.fixture
def router(storage, scorer, config):
    return RoutingRouter(storage, scorer, config)


@pytest.fixture
def compiler(harvester, storage, classifier, config, scorer):
    return PatternCompiler(harvester, storage, classifier, config, scorer)


@pytest.fixture
def injector(router, storage, scorer, config):
    return RouteInjector(router, storage, scorer, config)


# ── Config Tests (3) ───────────────────────────────────────────────────


class TestConfig:
    def test_defaults(self, config):
        assert config.classifier_mode == "local"
        assert config.confidence_init == 0.5
        assert config.confidence_reward == 0.1
        assert config.confidence_penalty == 0.2
        assert config.prune_threshold == 0.2
        assert config.inject_threshold_high == 0.7
        assert config.inject_threshold_low == 0.5

    def test_db_path_alias(self, config):
        """db_path property should return routes_db_path for compatibility."""
        assert config.db_path == config.routes_db_path

    def test_from_env(self, monkeypatch, tmp_dir):
        monkeypatch.setenv("AIR_CLASSIFIER_MODE", "local")
        monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_dir))
        cfg = AIRConfig.from_env()
        assert cfg.classifier_mode == "local"
        assert cfg.cortex_db_path == tmp_dir / "cortex-observations.db"
        assert cfg.routes_db_path == tmp_dir / "air_routes.db"


# ── Storage Tests (10) ────────────────────────────────────────────────


class TestStorage:
    def test_add_and_get_rule(self, storage):
        rule_id = storage.add_rule({
            "trigger_pattern": "commit changes",
            "trigger_hash": "abc123",
            "optimal_route": "git add . && git commit",
            "confidence": 0.8,
            "classifier_source": "local",
        })
        assert rule_id.startswith("air-")

        rule = storage.get_rule(rule_id)
        assert rule is not None
        assert rule["trigger_pattern"] == "commit changes"
        assert rule["confidence"] == 0.8

    def test_get_rule_by_trigger(self, storage):
        storage.add_rule({
            "trigger_pattern": "find files",
            "trigger_hash": "hash_find",
            "optimal_route": "Glob: **/*.py",
            "confidence": 0.75,
        })
        rule = storage.get_rule_by_trigger("hash_find")
        assert rule is not None
        assert rule["optimal_route"] == "Glob: **/*.py"

    def test_update_rule(self, storage):
        rule_id = storage.add_rule({
            "trigger_pattern": "test pattern",
            "trigger_hash": "hash_test",
            "optimal_route": "pytest",
            "confidence": 0.5,
        })
        success = storage.update_rule(rule_id, {"confidence": 0.9, "hit_count": 5})
        assert success

        rule = storage.get_rule(rule_id)
        assert rule["confidence"] == 0.9
        assert rule["hit_count"] == 5

    def test_delete_rule(self, storage):
        rule_id = storage.add_rule({
            "trigger_pattern": "delete me",
            "trigger_hash": "hash_del",
            "optimal_route": "noop",
        })
        assert storage.delete_rule(rule_id)
        assert storage.get_rule(rule_id) is None

    def test_prune_rules(self, storage):
        storage.add_rule({
            "trigger_pattern": "low confidence",
            "trigger_hash": "hash_low",
            "optimal_route": "noop",
            "confidence": 0.1,
        })
        storage.add_rule({
            "trigger_pattern": "high confidence",
            "trigger_hash": "hash_high",
            "optimal_route": "good route",
            "confidence": 0.9,
        })
        pruned = storage.prune_rules(threshold=0.2)
        assert pruned == 1

        stats = storage.get_stats()
        assert stats["rule_count"] == 1

    def test_injectable_rules(self, storage):
        for i in range(5):
            storage.add_rule({
                "trigger_pattern": f"pattern {i}",
                "trigger_hash": f"hash_{i}",
                "optimal_route": f"route {i}",
                "confidence": 0.3 + (i * 0.15),
            })
        high = storage.get_injectable_rules(threshold=0.7)
        assert len(high) >= 1
        assert all(r["confidence"] >= 0.7 for r in high)

    def test_record_hit(self, storage):
        rule_id = storage.add_rule({
            "trigger_pattern": "hit me",
            "trigger_hash": "hash_hit",
            "optimal_route": "action",
            "confidence": 0.6,
            "hit_count": 0,
        })
        storage.record_hit(rule_id)
        rule = storage.get_rule(rule_id)
        assert rule["hit_count"] == 1
        assert rule["last_used"] is not None

    def test_lookup_by_hash(self, storage):
        storage.add_rule({
            "trigger_pattern": "lookup test",
            "trigger_hash": "hash_lookup",
            "optimal_route": "found it",
            "confidence": 0.8,
        })
        matches = storage.lookup_by_hash("hash_lookup")
        assert len(matches) == 1
        assert matches[0]["optimal_route"] == "found it"

    def test_stats(self, storage):
        stats = storage.get_stats()
        assert "rule_count" in stats
        assert "total_hits" in stats
        assert "total_misses" in stats

    def test_get_all_rules(self, storage):
        storage.add_rule({
            "trigger_pattern": "rule a",
            "trigger_hash": "hash_a",
            "optimal_route": "route a",
            "confidence": 0.6,
        })
        rules = storage.get_all_rules()
        assert len(rules) == 1

    def test_list_active_rules_no_project_excludes_project_scoped(self, storage):
        """list_active_rules(project_id=None) must NOT return project-scoped rules.

        Without this fix, get_injection_stats() would count rules from every
        project in global stats, inflating high/medium counts.
        """
        # Add a global rule (no project_id)
        storage.add_rule({
            "trigger_pattern": "global rule",
            "trigger_hash": "hash_global",
            "optimal_route": "global route",
            "confidence": 0.8,
            "project_id": None,
        })
        # Add a project-scoped rule
        storage.add_rule({
            "trigger_pattern": "project rule",
            "trigger_hash": "hash_proj",
            "optimal_route": "project route",
            "confidence": 0.8,
            "project_id": "my-project",
        })

        # No project context: must return only the global rule
        global_rules = storage.list_active_rules(project_id=None)
        assert len(global_rules) == 1
        assert global_rules[0]["trigger_pattern"] == "global rule"

        # With project context: must return both (project + global)
        project_rules = storage.list_active_rules(project_id="my-project")
        assert len(project_rules) == 2


# ── Scorer Tests (8) ──────────────────────────────────────────────────


class TestScorer:
    def test_reward(self, scorer):
        assert scorer.reward(0.5) == 0.6
        assert scorer.reward(0.95) == 1.0

    def test_penalize(self, scorer):
        assert scorer.penalize(0.5) == 0.3
        assert scorer.penalize(0.1) == 0.0

    def test_asymmetry(self, scorer):
        """Penalty must hit harder than reward."""
        base = 0.5
        rewarded = scorer.reward(base)
        penalized = scorer.penalize(base)
        assert (base - penalized) > (rewarded - base)

    def test_decay(self, scorer):
        conf = scorer.decay(1.0, days_since_use=7.0)
        assert 0.94 < conf < 0.96

    def test_should_prune(self, scorer):
        assert scorer.should_prune(0.1)
        assert not scorer.should_prune(0.3)

    def test_should_inject_high(self, scorer):
        assert scorer.should_inject_high(0.8)
        assert not scorer.should_inject_high(0.6)

    def test_should_inject_low(self, scorer):
        assert scorer.should_inject_low(0.6)
        assert not scorer.should_inject_low(0.8)
        assert not scorer.should_inject_low(0.3)

    def test_update_dispatch(self, scorer):
        assert scorer.update(0.5, success=True) == 0.6
        assert scorer.update(0.5, success=False) == 0.3

    def test_effective_confidence(self, scorer):
        now = datetime.now(timezone.utc).isoformat()
        rule = {"confidence": 0.8, "last_used": now}
        eff = scorer.effective_confidence(rule)
        assert 0.79 < eff <= 0.8

    def test_effective_confidence_no_last_used(self, scorer):
        rule = {"confidence": 0.8}
        assert scorer.effective_confidence(rule) == 0.8


# ── Harvester Tests (5) ──────────────────────────────────────────────


class TestHarvester:
    def test_empty_session(self, harvester):
        events = harvester.get_events_by_session("nonexistent")
        assert events == []

    def test_get_events_by_session(self, harvester, cortex_db):
        now = datetime.now(timezone.utc)
        _insert_observation(cortex_db, "sess-1", "Bash",
                           raw_input='{"command": "ls"}',
                           summary="Listed files",
                           timestamp=now.isoformat())
        _insert_observation(cortex_db, "sess-1", "Read",
                           raw_input='{"file_path": "/tmp/x"}',
                           summary="Read file /tmp/x",
                           timestamp=(now + timedelta(seconds=1)).isoformat())

        events = harvester.get_events_by_session("sess-1")
        assert len(events) == 2
        assert events[0]["tool_name"] == "Bash"
        assert events[0]["sequence_num"] == 0
        assert events[1]["tool_name"] == "Read"
        assert events[1]["sequence_num"] == 1

    def test_error_detection_from_summary(self, harvester, cortex_db):
        _insert_observation(cortex_db, "sess-2", "Edit",
                           summary="Error: old_string not found in file")
        events = harvester.get_events_by_session("sess-2")
        assert len(events) == 1
        assert events[0]["success"] == 0

    def test_success_detection(self, harvester, cortex_db):
        _insert_observation(cortex_db, "sess-3", "Bash",
                           summary="Command executed successfully")
        events = harvester.get_events_by_session("sess-3")
        assert len(events) == 1
        assert events[0]["success"] == 1

    def test_get_recent_session_ids(self, harvester, cortex_db):
        now = datetime.now(timezone.utc)
        _insert_observation(cortex_db, "sess-recent", "Bash",
                           timestamp=now.isoformat())
        _insert_observation(cortex_db, "sess-old", "Bash",
                           timestamp=(now - timedelta(hours=100)).isoformat())

        recent = harvester.get_recent_session_ids(hours=48)
        assert "sess-recent" in recent
        assert "sess-old" not in recent

    def test_event_count(self, harvester, cortex_db):
        _insert_observation(cortex_db, "sess-c", "Bash")
        _insert_observation(cortex_db, "sess-c", "Read")
        assert harvester.get_event_count() == 2


# ── Classifier Tests (3) ─────────────────────────────────────────────


class TestClassifier:
    def test_classify_commit(self, classifier):
        result = classifier.classify("commit the changes", [
            {"tool": "Bash", "args": {"command": "git add ."}, "result": "ok"},
        ])
        assert result["intent"] == "git_commit"
        assert result["trigger_hash"]
        assert result["confidence"] > 0

    def test_classify_unknown(self, classifier):
        result = classifier.classify("xyzzy plugh", [])
        assert result["intent"] == "unknown"
        assert result["confidence"] == 0.0

    def test_classify_search(self, classifier):
        result = classifier.classify("find the config file", [
            {"tool": "Grep", "args": {}, "result": "found"},
        ])
        assert result["intent"] == "file_search"


# ── Router Tests (6) ─────────────────────────────────────────────────


class TestRouter:
    def test_lookup_no_match(self, router):
        result = router.lookup("something random")
        assert result is None

    def test_lookup_match(self, router, storage):
        normalized = router._normalize_message("commit the changes")
        trigger_hash = router._compute_hash(normalized)
        storage.add_rule({
            "trigger_pattern": "commit the changes",
            "trigger_hash": trigger_hash,
            "optimal_route": "git add . && git commit",
            "confidence": 0.8,
        })
        result = router.lookup("commit the changes")
        assert result is not None
        assert result["optimal_route"] == "git add . && git commit"

    def test_match_alias(self, router, storage):
        normalized = router._normalize_message("run tests")
        trigger_hash = router._compute_hash(normalized)
        storage.add_rule({
            "trigger_pattern": "run tests",
            "trigger_hash": trigger_hash,
            "optimal_route": "pytest",
            "confidence": 0.75,
        })
        result = router.match("run tests")
        assert result is not None

    def test_record_outcome_success(self, router, storage):
        normalized = router._normalize_message("search files")
        trigger_hash = router._compute_hash(normalized)
        rule_id = storage.add_rule({
            "trigger_pattern": "search files",
            "trigger_hash": trigger_hash,
            "optimal_route": "Glob: **/*",
            "confidence": 0.6,
        })
        new_conf = router.record_outcome(rule_id, success=True)
        assert new_conf == 0.7

    def test_record_outcome_failure(self, router, storage):
        normalized = router._normalize_message("bad route")
        trigger_hash = router._compute_hash(normalized)
        rule_id = storage.add_rule({
            "trigger_pattern": "bad route",
            "trigger_hash": trigger_hash,
            "optimal_route": "wrong thing",
            "confidence": 0.6,
        })
        new_conf = router.record_outcome(rule_id, success=False)
        assert abs(new_conf - 0.4) < 1e-9

    def test_record_outcome_failure_increments_miss_count(self, router, storage):
        """miss_count must be incremented when record_outcome(success=False)."""
        normalized = router._normalize_message("miss count test")
        trigger_hash = router._compute_hash(normalized)
        rule_id = storage.add_rule({
            "trigger_pattern": "miss count test",
            "trigger_hash": trigger_hash,
            "optimal_route": "wrong route",
            "confidence": 0.6,
            "miss_count": 0,
        })
        router.record_outcome(rule_id, success=False)
        rule = storage.get_rule(rule_id)
        assert rule["miss_count"] == 1

    def test_record_outcome_success_does_not_increment_miss_count(self, router, storage):
        """hit_count increments on success but miss_count stays 0."""
        normalized = router._normalize_message("hit count test")
        trigger_hash = router._compute_hash(normalized)
        rule_id = storage.add_rule({
            "trigger_pattern": "hit count test",
            "trigger_hash": trigger_hash,
            "optimal_route": "correct route",
            "confidence": 0.6,
            "miss_count": 0,
        })
        router.record_outcome(rule_id, success=True)
        rule = storage.get_rule(rule_id)
        assert rule["miss_count"] == 0

    def test_resolve_conflicts(self, router):
        matches = [
            {"rule_id": "a", "confidence": 0.6, "last_used": "", "hit_count": 5},
            {"rule_id": "b", "confidence": 0.9, "last_used": "", "hit_count": 2},
        ]
        best = router.resolve_conflicts(matches)
        assert best["rule_id"] == "b"

    def test_normalize_message(self):
        result = RoutingRouter._normalize_message("Commit the changes!!!")
        assert result == "commit the changes"


# ── Compiler Tests (4) ───────────────────────────────────────────────


class TestCompiler:
    def test_compile_empty_session(self, compiler):
        rules = compiler.compile_session("nonexistent-session")
        assert rules == []

    def test_compile_session_with_pattern(self, compiler, cortex_db):
        """Simulate a miss-then-recover pattern in cortex observations."""
        now = datetime.now(timezone.utc)
        # Failed tool call (Skill)
        _insert_observation(cortex_db, "pattern-sess", "Skill",
                           raw_input="commit changes",
                           summary="Error: Unknown skill 'commit'",
                           timestamp=now.isoformat())
        # Successful recovery (Bash)
        _insert_observation(cortex_db, "pattern-sess", "Bash",
                           raw_input='{"command": "git add . && git commit -m \\"fix\\""}',
                           summary="Committed successfully",
                           timestamp=(now + timedelta(seconds=2)).isoformat())

        rules = compiler.compile_session("pattern-sess")
        assert len(rules) >= 1
        assert rules[0]["optimal_route"] == "Bash"

    def test_compile_session_no_pattern(self, compiler, cortex_db):
        """Sessions with no failures should produce no rules."""
        now = datetime.now(timezone.utc)
        _insert_observation(cortex_db, "clean-sess", "Bash",
                           summary="OK",
                           timestamp=now.isoformat())
        _insert_observation(cortex_db, "clean-sess", "Read",
                           summary="File read OK",
                           timestamp=(now + timedelta(seconds=1)).isoformat())

        rules = compiler.compile_session("clean-sess")
        assert rules == []

    def test_compile_recent(self, compiler, cortex_db):
        """compile_recent should find patterns across sessions."""
        now = datetime.now(timezone.utc)
        _insert_observation(cortex_db, "recent-1", "Edit",
                           summary="Error: old_string not found",
                           timestamp=now.isoformat())
        _insert_observation(cortex_db, "recent-1", "Edit",
                           raw_input='{"file_path": "/tmp/x", "old_string": "abc", "new_string": "def"}',
                           summary="Edit performed successfully",
                           timestamp=(now + timedelta(seconds=2)).isoformat())

        rules = compiler.compile_recent(hours=1)
        assert len(rules) >= 1


# ── Injector Tests (4) ───────────────────────────────────────────────


class TestInjector:
    def test_generate_empty_section(self, injector):
        section = injector.generate_claudemd_section()
        assert "AIR" in section
        assert "<!-- AIR:START -->" in section
        assert "<!-- AIR:END -->" in section

    def test_inject_claudemd_creates_section(self, injector, tmp_dir):
        claude_md = tmp_dir / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nSome instructions.\n")

        injector.inject_claudemd(claude_md)
        content = claude_md.read_text()
        assert "<!-- AIR:START -->" in content
        assert "<!-- AIR:END -->" in content

    def test_inject_claudemd_replaces_section(self, injector, tmp_dir):
        claude_md = tmp_dir / "CLAUDE.md"
        claude_md.write_text(
            "# My Project\n\n"
            "<!-- AIR:START -->\nold content\n<!-- AIR:END -->\n\n"
            "More instructions.\n"
        )

        injector.inject_claudemd(claude_md)
        content = claude_md.read_text()
        assert "old content" not in content
        assert "More instructions." in content

    def test_injection_stats(self, injector):
        stats = injector.get_injection_stats()
        assert "high_confidence_rules" in stats
        assert "medium_confidence_rules" in stats
        assert "total_rules" in stats
        assert "cold_start_complete" in stats

    def test_cold_start_false_when_no_rules(self, injector):
        """cold_start_complete must be False when no rules have been compiled."""
        stats = injector.get_injection_stats()
        assert stats["cold_start_complete"] is False

    def test_cold_start_true_with_harvester_above_threshold(
        self, injector, harvester, cortex_db
    ):
        """cold_start_complete must be True when session count >= cold_start_cycles."""
        from datetime import datetime, timezone, timedelta
        import sqlite3
        # cold_start_cycles=10 in config; insert 10 distinct sessions
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(cortex_db))
        for i in range(10):
            conn.execute(
                "INSERT INTO observations "
                "(session_id, timestamp, source, tool_name) "
                "VALUES (?, ?, 'post_tool_use', 'Bash')",
                (f"cold-start-sess-{i}", (now - timedelta(seconds=i)).isoformat()),
            )
        conn.commit()
        conn.close()

        stats = injector.get_injection_stats(harvester=harvester)
        assert stats["cold_start_complete"] is True

    def test_cold_start_false_with_harvester_below_threshold(
        self, injector, harvester, cortex_db
    ):
        """cold_start_complete must be False when session count < cold_start_cycles."""
        from datetime import datetime, timezone
        import sqlite3
        # Only 3 sessions, cold_start_cycles=10
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(cortex_db))
        for i in range(3):
            conn.execute(
                "INSERT INTO observations "
                "(session_id, timestamp, source, tool_name) "
                "VALUES (?, ?, 'post_tool_use', 'Bash')",
                (f"cold-start-small-{i}", now.isoformat()),
            )
        conn.commit()
        conn.close()

        stats = injector.get_injection_stats(harvester=harvester)
        assert stats["cold_start_complete"] is False


# ── Decay Batch Tests (2) ────────────────────────────────────────────


class TestDecayBatch:
    def test_decay_batch_empty(self, scorer, storage):
        result = scorer.apply_decay_batch(storage)
        assert result == {"decayed": 0, "pruned": 0, "unchanged": 0}

    def test_decay_batch_prunes_old_rules(self, scorer, storage):
        old_time = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        storage.add_rule({
            "trigger_pattern": "ancient rule",
            "trigger_hash": "hash_ancient",
            "optimal_route": "old route",
            "confidence": 0.3,
            "last_used": old_time,
        })
        result = scorer.apply_decay_batch(storage)
        assert result["pruned"] == 1


# ── Integration Tests (3) ────────────────────────────────────────────


class TestIntegration:
    def test_full_pipeline(self, compiler, storage, injector, cortex_db, tmp_dir):
        """End-to-end: insert observations -> compile -> inject."""
        now = datetime.now(timezone.utc)
        # Create a miss-recover pattern
        _insert_observation(cortex_db, "e2e-sess", "Skill",
                           raw_input="search for config files",
                           summary="Error: Unknown skill 'search'",
                           timestamp=now.isoformat())
        _insert_observation(cortex_db, "e2e-sess", "Grep",
                           raw_input='{"pattern": "config"}',
                           summary="Found 3 matches",
                           timestamp=(now + timedelta(seconds=2)).isoformat())

        # Compile
        rules = compiler.compile_session("e2e-sess")
        assert len(rules) >= 1

        # Inject
        claude_md = tmp_dir / "CLAUDE.md"
        claude_md.write_text("# Test\n")
        injector.inject_claudemd(claude_md)
        content = claude_md.read_text()
        assert "AIR:START" in content

    def test_reinforcement_across_sessions(self, compiler, storage, cortex_db):
        """Same pattern in multiple sessions should reinforce the rule."""
        for i in range(3):
            sid = f"reinforce-{i}"
            now = datetime.now(timezone.utc)
            _insert_observation(cortex_db, sid, "Skill",
                               raw_input="commit",
                               summary="Error: Unknown skill",
                               timestamp=now.isoformat())
            _insert_observation(cortex_db, sid, "Bash",
                               raw_input='{"command": "git commit -m fix"}',
                               summary="Committed",
                               timestamp=(now + timedelta(seconds=2)).isoformat())
            compiler.compile_session(sid)

        stats = storage.get_stats()
        # Should have at least one rule with hit_count > 1
        rules = storage.get_all_rules()
        max_hits = max(r["hit_count"] for r in rules)
        assert max_hits >= 2

    def test_cli_smoke(self):
        """Verify air_cli.py runs without error."""
        import subprocess
        result = subprocess.run(
            ["python3", "scripts/air_cli.py", "stats"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
            timeout=15,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "rule_count" in data
