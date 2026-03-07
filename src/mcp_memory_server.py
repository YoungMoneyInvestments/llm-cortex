#!/usr/bin/env python3
"""
MCP Memory Server — Exposes Cortex memory retrieval as MCP tools.

Provides 5 MCP tools that follow the token-efficient 3-layer pattern:
  1. cami_memory_search       — L1: Compact index search (~20 tokens/result)
  2. cami_memory_timeline     — L2: Chronological context (~100 tokens/item)
  3. cami_memory_details      — L3: Full observation text (variable)
  4. cami_memory_save         — Save a memory for future retrieval
  5. cami_memory_graph_search — Graph-augmented search with entity expansion

Usage:
    # Start as MCP server (stdio transport)
    python mcp_memory_server.py

    # Configure in MCP settings (e.g., Claude Desktop or OpenClaw):
    {
      "mcpServers": {
        "cortex-memory": {
          "command": "/path/to/your/venv/bin/python3",
          "args": ["/path/to/cortex/src/mcp_memory_server.py"]
        }
      }
    }
"""

import json
import sys
from pathlib import Path

# Add scripts dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from memory_retriever import MemoryRetriever

# ── MCP Protocol (stdio transport) ─────────────────────────────────────────
#
# Implements the MCP protocol using stdin/stdout JSON-RPC 2.0.
# This is the simplest transport — no HTTP server needed.


def send_response(request_id, result):
    """Send a JSON-RPC 2.0 response."""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }
    msg = json.dumps(response)
    sys.stdout.write(f"Content-Length: {len(msg)}\r\n\r\n{msg}")
    sys.stdout.flush()


def send_error(request_id, code, message):
    """Send a JSON-RPC 2.0 error response."""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    msg = json.dumps(response)
    sys.stdout.write(f"Content-Length: {len(msg)}\r\n\r\n{msg}")
    sys.stdout.flush()


# ── Tool definitions ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "cami_memory_search",
        "description": (
            "Search Cortex memory across observations, conversations, and knowledge. "
            "Returns a compact index of results (~20 tokens each). Use this first, "
            "then drill into specific results with cami_memory_timeline or cami_memory_details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default: 15)",
                    "default": 15,
                },
                "source": {
                    "type": "string",
                    "description": "Filter by source type: post_tool_use, user_prompt, session_end",
                    "enum": ["post_tool_use", "user_prompt", "session_end"],
                },
                "agent": {
                    "type": "string",
                    "description": "Filter by agent: main, codebot, finbot, docbot, etc.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "cami_memory_timeline",
        "description": (
            "Get chronological context around a specific observation. "
            "Shows the observation plus surrounding tool calls in the same session. "
            "~100 tokens per item. Use after cami_memory_search to understand context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {
                    "type": "integer",
                    "description": "The numeric observation ID from search results",
                },
                "window": {
                    "type": "integer",
                    "description": "Number of surrounding observations to include (default: 5)",
                    "default": 5,
                },
            },
            "required": ["observation_id"],
        },
    },
    {
        "name": "cami_memory_details",
        "description": (
            "Get full details for specific observations including raw input/output. "
            "Variable token cost — only fetch what you actually need. "
            "Use after search and timeline to get the complete picture."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of observation IDs to get full details for",
                },
            },
            "required": ["observation_ids"],
        },
    },
    {
        "name": "cami_memory_save",
        "description": (
            "Manually save a memory, fact, or decision for future retrieval. "
            "Use this to explicitly remember something important."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory content to save",
                },
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags for categorization",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "cami_memory_graph_search",
        "description": (
            "Search memory with knowledge graph augmentation. Finds related entities "
            "and expands the search to include connected concepts. Returns base results "
            "enriched with entity relationships, plus graph-expanded results from "
            "related entities in the knowledge graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default: 15)",
                    "default": 15,
                },
                "graph_depth": {
                    "type": "integer",
                    "description": "How many hops to traverse in the knowledge graph (1 or 2, default: 1)",
                    "default": 1,
                },
            },
            "required": ["query"],
        },
    },
]


# ── Tool handlers ───────────────────────────────────────────────────────────


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle a tool call and return the result."""
    retriever = MemoryRetriever()

    try:
        if name == "cami_memory_search":
            results = retriever.search(
                query=arguments["query"],
                limit=arguments.get("limit", 15),
                source=arguments.get("source"),
                agent=arguments.get("agent"),
            )
            # Lightweight graph enrichment for L1 — just detect entity names
            results = retriever._enrich_with_graph_context(results)
            # Format as compact text for token efficiency
            lines = [f"Found {len(results)} results for '{arguments['query']}':\n"]
            for r in results:
                tool_info = f" [{r.get('tool', r.get('collection', '?'))}]" if r.get('tool') or r.get('collection') else ""
                obs_id = r.get('obs_id', '')
                line = f"  #{obs_id}{tool_info}: {r['summary']}"
                # Compact graph context: just entity names for L1
                gc = r.get("graph_context")
                if gc and gc.get("entities_found"):
                    line += f"  [KG: {', '.join(gc['entities_found'])}]"
                lines.append(line)
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "cami_memory_timeline":
            context = retriever.timeline(
                observation_id=arguments["observation_id"],
                window=arguments.get("window", 5),
            )
            lines = [f"Timeline around observation #{arguments['observation_id']}:\n"]
            for r in context:
                marker = ">>>" if r.get("is_target") else "   "
                lines.append(
                    f"  {marker} #{r['id']} [{r.get('tool', '?')}]: {r['summary']}"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "cami_memory_details":
            details = retriever.get_details(arguments["observation_ids"])
            parts = []
            for d in details:
                parts.append(
                    f"--- Observation #{d['id']} ({d['source']}) ---\n"
                    f"Tool: {d['tool_name']}\n"
                    f"Agent: {d['agent']}\n"
                    f"Time: {d['timestamp']}\n"
                    f"Input: {d['raw_input'] or '(none)'}\n"
                    f"Output: {d['raw_output'] or '(none)'}\n"
                    f"Summary: {d['summary']}"
                )
            return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

        elif name == "cami_memory_save":
            metadata = {}
            if arguments.get("tags"):
                metadata["tags"] = arguments["tags"]
            mem_id = retriever.save_memory(arguments["content"], metadata)
            return {
                "content": [
                    {"type": "text", "text": f"Memory saved with ID: {mem_id}"}
                ]
            }

        elif name == "cami_memory_graph_search":
            results = retriever.search_with_context(
                query=arguments["query"],
                limit=arguments.get("limit", 15),
                graph_depth=arguments.get("graph_depth", 1),
            )
            lines = [f"Graph search: '{arguments['query']}' — {len(results)} results\n"]
            for r in results:
                tool_info = f" [{r.get('tool', r.get('collection', '?'))}]" if r.get('tool') or r.get('collection') else ""
                obs_id = r.get('obs_id', '')
                origin = r.get('origin', '?')
                expanded = f" (via {r['expanded_from']})" if r.get("expanded_from") else ""
                line = f"  #{obs_id}{tool_info} [{origin}]{expanded}: {r['summary']}"
                lines.append(line)
                # Show graph context: entities and relationships
                gc = r.get("graph_context")
                if gc:
                    if gc.get("entities_found"):
                        lines.append(f"    entities: {', '.join(gc['entities_found'])}")
                    if gc.get("relationships"):
                        for rel in gc["relationships"][:3]:
                            ctx = f" ({rel['context']})" if rel.get("context") else ""
                            lines.append(f"    -> {rel['type']}: {rel['target']}{ctx}")
                        if len(gc["relationships"]) > 3:
                            lines.append(f"    ... +{len(gc['relationships']) - 3} more relationships")
                    if gc.get("related_entities"):
                        related = gc["related_entities"][:5]
                        suffix = f" +{len(gc['related_entities']) - 5} more" if len(gc["related_entities"]) > 5 else ""
                        lines.append(f"    related: {', '.join(related)}{suffix}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }

    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Error: {str(e)}"}],
            "isError": True,
        }
    finally:
        retriever.close()


# ── MCP Protocol handler ───────────────────────────────────────────────────


def read_message() -> dict:
    """Read a JSON-RPC message from stdin (Content-Length framing)."""
    # Read headers
    content_length = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            raise EOFError("stdin closed")
        line = line.strip()
        if not line:
            break
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":")[1].strip())

    if content_length == 0:
        raise ValueError("No Content-Length header")

    # Read body
    body = sys.stdin.read(content_length)
    return json.loads(body)


def handle_message(msg: dict):
    """Handle a single JSON-RPC message."""
    method = msg.get("method", "")
    request_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        send_response(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "cortex-memory",
                "version": "0.1.0",
            },
        })

    elif method == "notifications/initialized":
        pass  # No response needed for notifications

    elif method == "tools/list":
        send_response(request_id, {"tools": TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = handle_tool_call(tool_name, arguments)
        send_response(request_id, result)

    elif method == "ping":
        send_response(request_id, {})

    elif request_id is not None:
        send_error(request_id, -32601, f"Method not found: {method}")


def main():
    """Run the MCP server on stdio."""
    import logging
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    while True:
        try:
            msg = read_message()
            handle_message(msg)
        except EOFError:
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
