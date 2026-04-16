from pathlib import Path

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
