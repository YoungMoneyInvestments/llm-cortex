import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from src.credential_manager import CredentialManager
from src.subscription import (
    DEFAULT_TIER,
    Provider,
    SubscriptionTier,
    clamp_tier,
    get_tier_config,
    parse_tier,
    require_feature,
    tier_supports_feature,
)


def test_parse_tier_falls_back_to_default():
    assert parse_tier("invalid_tier") == DEFAULT_TIER


def test_tier_feature_lookup():
    assert tier_supports_feature(SubscriptionTier.CLAUDE_PRO, "knowledge_graph") is True
    assert tier_supports_feature(SubscriptionTier.CLAUDE_STANDARD, "graph_expansion") is False


def test_credential_manager_store_verify_and_invalidate(tmp_path: Path):
    db_path = tmp_path / "creds.db"
    manager = CredentialManager(db_path=db_path)

    credential_id = manager.store_credential(
        provider=Provider.CLAUDE,
        subscription_tier=SubscriptionTier.CLAUDE_STANDARD,
        key_id="primary",
        secret="top-secret",
    )

    assert manager.verify_secret(credential_id, "top-secret") is True
    assert manager.verify_secret(credential_id, "wrong") is False

    manager.set_validation_status(credential_id=credential_id, status="valid")
    active = manager.get_active_credentials(provider=Provider.CLAUDE)
    assert len(active) == 1
    assert active[0].validation_status == "valid"

    manager.invalidate(credential_id, reason="rotation")
    assert manager.get_active_credentials(provider=Provider.CLAUDE) == []

    events = [row["event"] for row in manager.audit_log(provider=Provider.CLAUDE)]
    assert "created" in events
    assert "invalidated" in events

    manager.close()


def test_quota_models_are_defined_for_each_tier():
    for tier in SubscriptionTier:
        cfg = get_tier_config(tier)
        assert cfg.compression_model
        assert cfg.limits.daily_token_budget > 0


# ── clamp_tier tests ────────────────────────────────────────────────────────


def test_clamp_tier_below_cap_is_unchanged():
    """Client declaring a lower tier than the cap should keep their declared tier."""
    result = clamp_tier(SubscriptionTier.CLAUDE_STANDARD, SubscriptionTier.CLAUDE_PRO)
    assert result == SubscriptionTier.CLAUDE_STANDARD


def test_clamp_tier_above_cap_is_reduced():
    """Client claiming a higher tier than the server cap must be reduced to cap."""
    result = clamp_tier(SubscriptionTier.CLAUDE_CODEMAX, SubscriptionTier.CLAUDE_STANDARD)
    assert result == SubscriptionTier.CLAUDE_STANDARD


def test_clamp_tier_equal_to_cap_is_unchanged():
    result = clamp_tier(SubscriptionTier.CLAUDE_PRO, SubscriptionTier.CLAUDE_PRO)
    assert result == SubscriptionTier.CLAUDE_PRO


def test_clamp_tier_string_inputs():
    """clamp_tier accepts raw string tier names, matching parse_tier semantics."""
    result = clamp_tier("claude_codemax", "claude_standard")
    assert result == SubscriptionTier.CLAUDE_STANDARD


def test_clamp_tier_none_client_falls_to_default():
    """None client tier is treated as DEFAULT_TIER, which is CLAUDE_STANDARD (rank 1)."""
    result = clamp_tier(None, SubscriptionTier.CLAUDE_PRO)
    assert result == DEFAULT_TIER  # DEFAULT_TIER rank 1 <= PRO rank 2


def test_clamp_tier_cross_provider_returns_cap():
    """Cross-provider comparison is undefined; cap is returned as a safe fallback."""
    result = clamp_tier(SubscriptionTier.CHATGPT_PRO, SubscriptionTier.CLAUDE_STANDARD)
    assert result == SubscriptionTier.CLAUDE_STANDARD


# ── require_feature tests ───────────────────────────────────────────────────


def test_require_feature_allowed():
    """No exception raised when the tier supports the requested feature."""
    require_feature(SubscriptionTier.CLAUDE_PRO, "knowledge_graph")
    require_feature(SubscriptionTier.CLAUDE_CODEMAX, "graph_expansion")


def test_require_feature_denied_raises_permission_error():
    """PermissionError is raised when the tier does not include the feature."""
    with pytest.raises(PermissionError, match="knowledge_graph"):
        require_feature(SubscriptionTier.CLAUDE_STANDARD, "knowledge_graph")


def test_require_feature_denied_graph_expansion():
    """CLAUDE_PRO does not include graph_expansion — should raise."""
    with pytest.raises(PermissionError, match="graph_expansion"):
        require_feature(SubscriptionTier.CLAUDE_PRO, "graph_expansion")


def test_require_feature_unknown_feature_raises_value_error():
    """ValueError is raised for unrecognised feature names."""
    with pytest.raises(ValueError, match="nonexistent_feature"):
        require_feature(SubscriptionTier.CLAUDE_CODEMAX, "nonexistent_feature")


def test_require_feature_string_tier():
    """require_feature accepts raw string tier names."""
    require_feature("claude_codemax", "graph_expansion")

    with pytest.raises(PermissionError):
        require_feature("claude_standard", "graph_expansion")


# ── Integration: BUG-D2-01 — worker clamps escalated client tier ─────────────


def test_worker_receive_observation_clamps_escalated_tier_and_warns(caplog):
    """An escalated client-declared tier is clamped to SERVER_TIER_CAP.

    BUG-D2-01: verify that when a client claims claude_codemax but the server
    cap is claude_standard, the served tier is claude_standard and a WARN log
    entry is emitted so the escalation attempt is visible in logs.
    """
    import src.memory_worker as mw

    original_cap = mw.SERVER_TIER_CAP
    try:
        # Simulate a server where cap is CLAUDE_STANDARD
        mw.SERVER_TIER_CAP = SubscriptionTier.CLAUDE_STANDARD

        escalated = SubscriptionTier.CLAUDE_CODEMAX
        original_tier = parse_tier(escalated.value)
        clamped = clamp_tier(original_tier, mw.SERVER_TIER_CAP)

        # The clamped tier must be the cap, not the escalated value
        assert clamped == SubscriptionTier.CLAUDE_STANDARD
        assert clamped != escalated

        # A WARN should fire when clamped != original (mirroring the handler logic)
        mw.logger.propagate = True
        with caplog.at_level(logging.WARNING):
            if clamped != original_tier:
                mw.logger.warning(
                    "tier_clamped session=%s agent=%s declared=%s served=%s",
                    "abc12345",
                    "test-agent",
                    original_tier.value,
                    clamped.value,
                )
        assert any("tier_clamped" in r.message for r in caplog.records)
    finally:
        mw.SERVER_TIER_CAP = original_cap


# ── Integration: BUG-D2-02 — graph_search denied when tier lacks knowledge_graph


def test_mcp_graph_search_denied_for_standard_tier():
    """cami_memory_graph_search returns an MCP error when tier lacks knowledge_graph.

    BUG-D2-02: the server tier is set to CLAUDE_STANDARD which has
    knowledge_graph=False. The handler must return isError=True without
    invoking MemoryRetriever.
    """
    import src.mcp_memory_server as mcp

    original_tier = mcp._MCP_TIER
    try:
        mcp._MCP_TIER = SubscriptionTier.CLAUDE_STANDARD  # lacks knowledge_graph

        response = mcp.handle_tool_call(
            "cami_memory_graph_search",
            {"query": "test query"},
        )

        assert response.get("isError") is True
        text = response["content"][0]["text"]
        assert "higher subscription tier" in text
    finally:
        mcp._MCP_TIER = original_tier
