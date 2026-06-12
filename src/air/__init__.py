"""
AIR — Adaptive Inference Routing for llm-cortex.

Learns from tool-call telemetry in cortex-observations.db, detects
miss-then-recover patterns, compiles routing rules with confidence
scoring, and injects optimized dispatch hints into sessions.

Patent-pending system (provisional filed 2026-03-14).

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

__version__ = "0.1.0"
