#!/usr/bin/env python3
"""
Cortex Memory MCP Server — Streamable HTTP transport.

Runs as a persistent daemon so all Codex/Cursor sessions share one process
instead of each spawning their own copy.

Port: 37781
URL:  http://127.0.0.1:37781/mcp

Usage:
    python mcp_memory_server_sse.py [--port 37781]

Managed by launchd: com.clawd.mcp-memory-sse
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Add scripts dir to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

# Load env vars before importing the server (sets OPENAI_API_KEY etc.)
from mcp_memory_server import handle_tool_call, _load_env

_load_env()

# Set agent name for memory saves — all Codex sessions appear as "codex"
os.environ.setdefault("CORTEX_AGENT_NAME", os.environ.get("CORTEX_AGENT_NAME", "codex"))

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

mcp = FastMCP("cortex-memory", host="127.0.0.1", port=37781)


def _call(name: str, args: dict) -> str:
    result = handle_tool_call(name, args)
    return result["content"][0]["text"]


@mcp.tool()
def cami_memory_search(
    query: str,
    limit: int = 15,
    source: Optional[str] = None,
    agent: Optional[str] = None,
) -> str:
    """Search Cami's memory across observations, conversations, and knowledge.
    Returns a compact index of results (~20 tokens each). Use this first,
    then drill into specific results with cami_memory_timeline or cami_memory_details."""
    return _call("cami_memory_search", {"query": query, "limit": limit, "source": source, "agent": agent})


@mcp.tool()
def cami_memory_timeline(observation_id: int, window: int = 5) -> str:
    """Get temporal context around a specific memory observation.
    Returns surrounding observations for chronological understanding."""
    return _call("cami_memory_timeline", {"observation_id": observation_id, "window": window})


@mcp.tool()
def cami_memory_details(observation_ids: list[int]) -> str:
    """Get full text of specific memory observations by ID.
    Use after cami_memory_search to retrieve complete content."""
    return _call("cami_memory_details", {"observation_ids": observation_ids})


@mcp.tool()
def cami_memory_save(
    content: str,
    tags: Optional[list[str]] = None,
    agent: Optional[str] = None,
) -> str:
    """Save a memory for future retrieval."""
    return _call("cami_memory_save", {"content": content, "tags": tags, "agent": agent})


@mcp.tool()
def cami_memory_graph_search(query: str, limit: int = 15, graph_depth: int = 1) -> str:
    """Graph-augmented memory search with entity expansion.
    Traverses the knowledge graph to find related memories beyond keyword matches."""
    return _call("cami_memory_graph_search", {"query": query, "limit": limit, "graph_depth": graph_depth})


@mcp.tool()
def cami_message_search(
    query: str,
    limit: int = 5,
    source: Optional[str] = None,
    contact: Optional[str] = None,
    days_back: Optional[int] = None,
) -> str:
    """Search iMessage and Discord conversation history using semantic similarity.
    Finds relevant message chunks from Cameron's conversations across platforms."""
    return _call("cami_message_search", {
        "query": query,
        "limit": limit,
        "source": source,
        "contact": contact,
        "days_back": days_back,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cortex Memory MCP server (streamable HTTP)")
    parser.add_argument("--port", type=int, default=37781)
    args = parser.parse_args()

    mcp.settings.port = args.port
    print(f"Cortex Memory MCP server listening on http://127.0.0.1:{args.port}/mcp", file=sys.stderr)
    mcp.run(transport="streamable-http")
