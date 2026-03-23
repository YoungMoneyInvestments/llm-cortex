"""
Adaptive Inference Routing (AIR) — Personalized tool dispatch optimization.

Observes tool-call patterns over time, identifies recurring miss-then-recover
sequences, and compiles a personalized routing table that short-circuits
unnecessary lookups. Reduces latency, lowers token spend, and gets measurably
smarter per user over time — without requiring changes to model weights.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

from src.air.config import AIRConfig
from src.air.storage import RoutingStorage
from src.air.harvester import TelemetryHarvester
from src.air.compiler import PatternCompiler
from src.air.classifier import IntentClassifier
from src.air.router import RoutingRouter
from src.air.scorer import ConfidenceScorer
from src.air.injector import RouteInjector

__all__ = [
    "AIRConfig",
    "RoutingStorage",
    "TelemetryHarvester",
    "PatternCompiler",
    "IntentClassifier",
    "RoutingRouter",
    "ConfidenceScorer",
    "RouteInjector",
]

__version__ = "0.1.0"
