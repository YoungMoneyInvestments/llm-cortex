from pathlib import Path

from src.credential_manager import CredentialManager
from src.subscription import (
    DEFAULT_TIER,
    Provider,
    SubscriptionTier,
    get_tier_config,
    parse_tier,
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
