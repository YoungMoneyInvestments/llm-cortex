"""Subscription tier definitions and lookup helpers for LLM Cortex."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable


class Provider(str, Enum):
    CLAUDE = "claude"
    CHATGPT = "chatgpt"
    GEMINI = "gemini"


class SubscriptionTier(str, Enum):
    CLAUDE_STANDARD = "claude_standard"
    CLAUDE_PRO = "claude_pro"
    CLAUDE_CODEMAX = "claude_codemax"
    CHATGPT_STANDARD = "chatgpt_standard"
    CHATGPT_PRO = "chatgpt_pro"
    GEMINI_STANDARD = "gemini_standard"
    GEMINI_PRO = "gemini_pro"


@dataclass(frozen=True)
class TierFeatures:
    compression: bool = True
    vector_search: bool = True
    knowledge_graph: bool = False
    graph_expansion: bool = False


@dataclass(frozen=True)
class TierLimits:
    observations_per_minute: int
    api_calls_per_minute: int
    daily_token_budget: int


@dataclass(frozen=True)
class TierConfig:
    tier: SubscriptionTier
    provider: Provider
    rank: int
    limits: TierLimits
    features: TierFeatures
    compression_model: str
    retention_days: int


TIER_CONFIGS: Dict[SubscriptionTier, TierConfig] = {
    SubscriptionTier.CLAUDE_STANDARD: TierConfig(
        tier=SubscriptionTier.CLAUDE_STANDARD,
        provider=Provider.CLAUDE,
        rank=1,
        limits=TierLimits(60, 40, 250_000),
        features=TierFeatures(compression=True, vector_search=True),
        compression_model="claude-sonnet-4-6",
        retention_days=14,
    ),
    SubscriptionTier.CLAUDE_PRO: TierConfig(
        tier=SubscriptionTier.CLAUDE_PRO,
        provider=Provider.CLAUDE,
        rank=2,
        limits=TierLimits(120, 80, 600_000),
        features=TierFeatures(compression=True, vector_search=True, knowledge_graph=True),
        compression_model="claude-sonnet-4-6",
        retention_days=30,
    ),
    SubscriptionTier.CLAUDE_CODEMAX: TierConfig(
        tier=SubscriptionTier.CLAUDE_CODEMAX,
        provider=Provider.CLAUDE,
        rank=3,
        limits=TierLimits(240, 160, 1_200_000),
        features=TierFeatures(
            compression=True,
            vector_search=True,
            knowledge_graph=True,
            graph_expansion=True,
        ),
        compression_model="claude-opus-4-1",
        retention_days=60,
    ),
    SubscriptionTier.CHATGPT_STANDARD: TierConfig(
        tier=SubscriptionTier.CHATGPT_STANDARD,
        provider=Provider.CHATGPT,
        rank=1,
        limits=TierLimits(60, 40, 250_000),
        features=TierFeatures(compression=True, vector_search=True),
        compression_model="gpt-4.1-mini",
        retention_days=14,
    ),
    SubscriptionTier.CHATGPT_PRO: TierConfig(
        tier=SubscriptionTier.CHATGPT_PRO,
        provider=Provider.CHATGPT,
        rank=2,
        limits=TierLimits(160, 120, 900_000),
        features=TierFeatures(
            compression=True,
            vector_search=True,
            knowledge_graph=True,
            graph_expansion=True,
        ),
        compression_model="gpt-5-mini",
        retention_days=45,
    ),
    SubscriptionTier.GEMINI_STANDARD: TierConfig(
        tier=SubscriptionTier.GEMINI_STANDARD,
        provider=Provider.GEMINI,
        rank=1,
        limits=TierLimits(50, 30, 200_000),
        features=TierFeatures(compression=True, vector_search=True),
        compression_model="gemini-2.5-flash",
        retention_days=14,
    ),
    SubscriptionTier.GEMINI_PRO: TierConfig(
        tier=SubscriptionTier.GEMINI_PRO,
        provider=Provider.GEMINI,
        rank=2,
        limits=TierLimits(140, 100, 800_000),
        features=TierFeatures(
            compression=True,
            vector_search=True,
            knowledge_graph=True,
            graph_expansion=True,
        ),
        compression_model="gemini-2.5-pro",
        retention_days=45,
    ),
}

DEFAULT_TIER = SubscriptionTier.CLAUDE_STANDARD


def parse_tier(value: str | None) -> SubscriptionTier:
    if not value:
        return DEFAULT_TIER
    normalized = value.strip().lower()
    for tier in SubscriptionTier:
        if normalized == tier.value:
            return tier
    return DEFAULT_TIER


def get_tier_config(tier: SubscriptionTier | str | None) -> TierConfig:
    normalized = parse_tier(tier.value if isinstance(tier, SubscriptionTier) else tier)
    return TIER_CONFIGS[normalized]


def tier_supports_feature(tier: SubscriptionTier | str | None, feature: str) -> bool:
    cfg = get_tier_config(tier)
    return bool(getattr(cfg.features, feature, False))


def tiers_for_provider(provider: Provider | str) -> list[TierConfig]:
    normalized = Provider(provider)
    return sorted(
        [c for c in TIER_CONFIGS.values() if c.provider == normalized],
        key=lambda c: c.rank,
    )


def top_tier(provider: Provider | str) -> TierConfig:
    provider_tiers = tiers_for_provider(provider)
    if not provider_tiers:
        raise ValueError(f"No subscription tiers configured for provider: {provider}")
    return max(provider_tiers, key=lambda c: c.rank)


def all_tiers() -> Iterable[TierConfig]:
    return TIER_CONFIGS.values()


def clamp_tier(
    client_declared: SubscriptionTier | str | None,
    server_cap: SubscriptionTier | str | None,
) -> SubscriptionTier:
    """Return the lower-ranked of two tiers, by rank within the same provider.

    Use this on write endpoints that accept a client-supplied ``subscription_tier``
    field.  The server knows the account's maximum allowed tier; any tier the
    client claims above that cap is silently reduced to the cap, preventing
    privilege escalation via a crafted request body.

    If ``client_declared`` and ``server_cap`` are from different providers the
    function falls back to ``server_cap`` — cross-provider comparisons are
    undefined.

    Args:
        client_declared: Tier the client is requesting (untrusted input).
        server_cap: Maximum tier the server is willing to grant.

    Returns:
        The lesser of the two tiers, or ``server_cap`` when providers differ.
    """
    client_cfg = get_tier_config(client_declared)
    cap_cfg = get_tier_config(server_cap)
    if client_cfg.provider != cap_cfg.provider:
        return cap_cfg.tier
    return client_cfg.tier if client_cfg.rank <= cap_cfg.rank else cap_cfg.tier


def require_feature(tier: SubscriptionTier | str | None, feature: str) -> None:
    """Raise ``PermissionError`` when *tier* does not include *feature*.

    Intended for use in request handlers and tool dispatchers that must gate
    access to premium features (``knowledge_graph``, ``graph_expansion``).
    Raises loudly rather than silently skipping so callers cannot accidentally
    proceed after a forgotten check.

    Args:
        tier: Subscription tier to test.
        feature: Attribute name on :class:`TierFeatures` (e.g.
            ``"knowledge_graph"``, ``"graph_expansion"``).

    Raises:
        PermissionError: When the tier does not support the requested feature.
        ValueError: When *feature* is not a recognised :class:`TierFeatures`
            attribute.
    """
    cfg = get_tier_config(tier)
    if not hasattr(cfg.features, feature):
        raise ValueError(
            f"Unknown feature {feature!r}. "
            f"Valid features: {list(cfg.features.__dataclass_fields__.keys())}"
        )
    if not getattr(cfg.features, feature):
        raise PermissionError(
            f"Subscription tier {cfg.tier.value!r} does not include feature "
            f"{feature!r}. Upgrade to a higher tier to access this feature."
        )
