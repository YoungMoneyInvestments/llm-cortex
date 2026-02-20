#!/usr/bin/env python3
"""
MCP Memory Server — Exposes Cortex memory retrieval as MCP tools (Layer 0)

Provides 4 MCP tools that follow the token-efficient 3-layer pattern:
  1. cortex_memory_search   — L1: Compact index search (~20 tokens/result)
  2. cortex_memory_timeline  — L2: Chronological context (~100 tokens/item)
  3. cortex_memory_details   — L3: Full observation text (variable)
  4. cortex_memory_save      — Save a memory for future retrieval

Usage:
    python mcp_memory_server.py

    # Configure in Claude Code settings or MCP config:
    {
      "mcpServers": {
        "cortex-memory": {
          "command": "python3",
          "args": ["/path/to/cortex/scripts/mcp_memory_server.py"],
          "env": {"CORTEX_WORKSPACE": "/path/to/cortex"}
        }
      }
    }

Configure:
    CORTEX_WORKSPACE  — Project root (default: ~/cortex)
"""

import json
import sys
from pathlib import Path

# Add scripts dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from memory_retriever import MemoryRetriever

# ── MCP Protocol (stdio transport) ─────────────────────────────────────────


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
        "name": "cortex_memory_search",
        "description": (
            "Search memory across observations, conversations, and knowledge. "
            "Returns a compact index of results (~20 tokens each). Use this first, "
            "then drill into specific results with cortex_memory_timeline or cortex_memory_details."
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
            },
            "required": ["query"],
        },
    },
    {
        "name": "cortex_memory_timeline",
        "description": (
            "Get chronological context around a specific observation. "
            "Shows the observation plus surrounding tool calls in the same session. "
            "~100 tokens per item. Use after cortex_memory_search to understand context."
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
        "name": "cortex_memory_details",
        "description": (
            "Get full details for specific observations including raw input/output. "
            "Variable token cost - only fetch what you actually need. "
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
        "name": "cortex_memory_save",
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
]


# ── Tool handlers ───────────────────────────────────────────────────────────


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle a tool call and return the result."""
    retriever = MemoryRetriever()

    try:
        if name == "cortex_memory_search":
            results = retriever.search(
                query=arguments["query"],
                limit=arguments.get("limit", 15),
                source=arguments.get("source"),
                agent=arguments.get("agent"),
            )
            lines = [f"Found {len(results)} results for '{arguments['query']}':\n"]
            for r in results:
                tool_info = f" [{r.get('tool', r.get('collection', '?'))}]" if r.get('tool') or r.get('collection') else ""
                obs_id = r.get('obs_id', '')
                lines.append(f"  #{obs_id}{tool_info}: {r['summary']}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "cortex_memory_timeline":
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

        elif name == "cortex_memory_details":
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

        elif name == "cortex_memory_save":
            metadata = {}
            if arguments.get("tags"):
                metadata["tags"] = arguments["tags"]
            mem_id = retriever.save_memory(arguments["content"], metadata)
            return {
                "content": [
                    {"type": "text", "text": f"Memory saved with ID: {mem_id}"}
                ]
            }

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
        pass

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
