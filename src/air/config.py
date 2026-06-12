"""
AIR Configuration — Dataclass-based config with environment variable overrides.

Adapted for llm-cortex: reads from cortex-observations.db for telemetry,
writes routing rules to air_routes.db in the cortex data directory.

All settings have sensible defaults and can be overridden via environment
variables prefixed with AIR_ (or CORTEX_ / ANTHROPIC_ where shared).

Usage:
    from src.air.config import AIRConfig

    cfg = AIRConfig.from_env()
    print(cfg.cortex_db_path)    # ~/.cortex/data/cortex-observations.db
    print(cfg.routes_db_path)    # ~/.cortex/data/air_routes.db

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


def _optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def _path_from_env(name: str, default: Path) -> Path:
    value = _optional_env(name)
    return Path(value).expanduser() if value else default


def _float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float, got {raw!r}") from exc


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


logger = logging.getLogger("cortex-air")


@dataclass(frozen=True)
class AIRConfig:
    """Immutable configuration for the Adaptive Inference Routing framework."""

    # Paths
    data_dir: Path
    cortex_db_path: Path      # cortex-observations.db (read-only source)
    routes_db_path: Path      # air_routes.db (AIR's own routing rules)

    # Legacy alias for modules that reference config.db_path
    @property
    def db_path(self) -> Path:
        return self.routes_db_path

    # Classifier
    classifier_mode: Literal["api", "local"]

    # Confidence tuning
    confidence_init: float
    confidence_reward: float
    confidence_penalty: float
    confidence_decay_rate: float

    # Lifecycle thresholds
    prune_threshold: float
    inject_threshold_high: float
    inject_threshold_low: float

    # Cold-start guard
    cold_start_cycles: int

    # Cross-project routing
    cross_project_threshold: float

    # API key (optional — required only when classifier_mode == "api")
    anthropic_api_key: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "AIRConfig":
        """Build config from environment variables with sensible defaults."""

        data_dir = _path_from_env(
            "CORTEX_DATA_DIR", Path.home() / ".cortex" / "data"
        )

        cortex_db_path = _path_from_env(
            "CORTEX_DB_PATH", data_dir / "cortex-observations.db"
        )

        routes_db_path = _path_from_env(
            "AIR_DB_PATH", data_dir / "air_routes.db"
        )

        classifier_mode_raw = os.environ.get(
            "AIR_CLASSIFIER_MODE", "local"
        ).strip().lower()
        if classifier_mode_raw not in ("api", "local"):
            raise RuntimeError(
                f"AIR_CLASSIFIER_MODE must be 'api' or 'local', "
                f"got {classifier_mode_raw!r}"
            )

        api_key = _optional_env("ANTHROPIC_API_KEY")

        cfg = cls(
            data_dir=data_dir,
            cortex_db_path=cortex_db_path,
            routes_db_path=routes_db_path,
            classifier_mode=classifier_mode_raw,  # type: ignore[arg-type]
            confidence_init=_float_from_env("AIR_CONFIDENCE_INIT", 0.5),
            confidence_reward=_float_from_env("AIR_CONFIDENCE_REWARD", 0.1),
            confidence_penalty=_float_from_env("AIR_CONFIDENCE_PENALTY", 0.2),
            confidence_decay_rate=_float_from_env(
                "AIR_CONFIDENCE_DECAY_RATE", 0.95
            ),
            prune_threshold=_float_from_env("AIR_PRUNE_THRESHOLD", 0.2),
            inject_threshold_high=_float_from_env(
                "AIR_INJECT_THRESHOLD_HIGH", 0.7
            ),
            inject_threshold_low=_float_from_env(
                "AIR_INJECT_THRESHOLD_LOW", 0.5
            ),
            cold_start_cycles=_int_from_env("AIR_COLD_START_CYCLES", 10),
            cross_project_threshold=_float_from_env(
                "AIR_CROSS_PROJECT_THRESHOLD", 0.9
            ),
            anthropic_api_key=api_key,
        )

        logger.info("AIR config loaded: %s", cfg)
        if cfg.classifier_mode == "api" and not cfg.anthropic_api_key:
            logger.warning(
                "AIR_CLASSIFIER_MODE is 'api' but ANTHROPIC_API_KEY is not "
                "set — classifier calls will fail until a key is provided."
            )

        return cfg

    def __repr__(self) -> str:
        key_display = "***masked***" if self.anthropic_api_key else "None"
        return (
            f"AIRConfig("
            f"data_dir={self.data_dir!r}, "
            f"cortex_db_path={self.cortex_db_path!r}, "
            f"routes_db_path={self.routes_db_path!r}, "
            f"classifier_mode={self.classifier_mode!r}, "
            f"confidence_init={self.confidence_init}, "
            f"confidence_reward={self.confidence_reward}, "
            f"confidence_penalty={self.confidence_penalty}, "
            f"confidence_decay_rate={self.confidence_decay_rate}, "
            f"prune_threshold={self.prune_threshold}, "
            f"inject_threshold_high={self.inject_threshold_high}, "
            f"inject_threshold_low={self.inject_threshold_low}, "
            f"cold_start_cycles={self.cold_start_cycles}, "
            f"cross_project_threshold={self.cross_project_threshold}, "
            f"anthropic_api_key={key_display}"
            f")"
        )
