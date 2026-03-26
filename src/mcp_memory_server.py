#!/usr/bin/env python3
"""
MCP Memory Server — Exposes Cortex memory retrieval as MCP tools.

Provides 6 MCP tools that follow the token-efficient 3-layer pattern:
  1. cami_memory_search       — L1: Compact index search (~20 tokens/result)
  2. cami_memory_timeline     — L2: Chronological context (~100 tokens/item)
  3. cami_memory_details      — L3: Full observation text (variable)
  4. cami_memory_save         — Save a memory for future retrieval
  5. cami_memory_graph_search — Graph-augmented search with entity expansion
  6. cami_message_search      — Semantic search over iMessage/Discord via pgvector

Usage:
    # Start as MCP server (stdio transport)
    python mcp_memory_server.py

    # Configure in MCP settings (e.g., Claude Desktop or OpenClaw):
    {
      "mcpServers": {
        "cortex-memory": {
          "command": "/Users/cameronbennion/clawd/venv/bin/python3",
          "args": ["/Users/cameronbennion/clawd/scripts/mcp_memory_server.py"]
        }
      }
    }
"""

import json
import os
import sys
from pathlib import Path

# Add scripts dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from memory_retriever import MemoryRetriever

# Input validation helpers inserted by CI patch
VALID_TOOL_NAMES = {"cami_memory_graph_search","cami_memory_details","cami_memory_search"}

def _validation_error(msg: str):
    return {"isError": True, "content": [{"type": "text", "text": msg}]}


# ── MCP Protocol (stdio transport) ─────────────────────────────────────────
#
# Implements the MCP protocol using stdin/stdout JSON-RPC 2.0.
# This is the simplest transport — no HTTP server needed.

_TRANSPORT_MODE = "content-length"


def _write_message(payload: dict):
    """Write a JSON-RPC payload using the negotiated transport framing."""
    msg = json.dumps(payload)
    if _TRANSPORT_MODE == "line":
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
        return

    sys.stdout.write(f"Content-Length: {len(msg)}\r\n\r\n{msg}")
    sys.stdout.flush()


def send_response(request_id, result):
    """Send a JSON-RPC 2.0 response."""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }
    _write_message(response)


def send_error(request_id, code, message):
    """Send a JSON-RPC 2.0 error response."""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    _write_message(response)


def _load_env():
    """Load environment variables from .env.local"""
    env_path = Path.home() / "clawd" / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


# ── Tool definitions ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "cami_memory_search",
        "description": (
            "Search Cami's memory across observations, conversations, and knowledge. "
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
                "agent": {
                    "type": "string",
                    "description": "Agent identifier (e.g., claude-code, codex, cursor, cami). Auto-detected from CORTEX_AGENT_NAME env var if not provided.",
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
    {
        "name": "cami_message_search",
        "description": (
            "Search iMessage and Discord conversation history using semantic similarity. "
            "Finds relevant message chunks from Cameron's conversations across platforms. "
            "Returns matching conversation snippets with sender, timestamp, and platform info."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query (e.g., 'discussion about trading strategy with Jake')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default: 5)",
                    "default": 5,
                },
                "source": {
                    "type": "string",
                    "description": "Filter by message source",
                    "enum": ["imessage", "discord"],
                },
                "contact": {
                    "type": "string",
                    "description": "Filter by contact/participant name (partial match)",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Only search messages from the last N days",
                },
            },
            "required": ["query"],
        },
    },
]


# ── Tool handlers ───────────────────────────────────────────────────────────


def handle_tool_call(name: str, arguments: dict) -> dict:

    # Early validation before touching retriever
    if name not in VALID_TOOL_NAMES:
        return _validation_error(f"Unknown tool: {name}")
    if name == "cami_memory_graph_search":
        gd = int(arguments.get("graph_depth", 1))
        if gd not in (0,1,2):
            return _validation_error("Invalid parameter: graph_depth must be 0,1,2")
    if name == "cami_memory_details":
        ids = arguments.get("observation_ids", None)
        if not ids:
            return _validation_error("Missing required parameter: observation_ids")

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
            agent = arguments.get("agent") or os.environ.get("CORTEX_AGENT_NAME", "main")
            metadata["agent"] = agent
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

        elif name == "cami_message_search":
            _load_env()

            import psycopg2
            import openai

            openai_key = os.environ.get("OPENAI_API_KEY")
            if not openai_key:
                return {
                    "content": [{"type": "text", "text": "Error: OPENAI_API_KEY not found"}],
                    "isError": True,
                }

            client = openai.OpenAI(api_key=openai_key)

            # Embed the query
            resp = client.embeddings.create(
                input=[arguments["query"]], model="text-embedding-3-small"
            )
            query_vec = resp.data[0].embedding

            # Build SQL query with filters
            conn = psycopg2.connect(
                host="100.67.112.3",
                port=5432,
                dbname="tradingcore",
                user="trading_user",
                password="TradingCore2025!",
            )
            try:
                cur = conn.cursor()

                conditions = []
                params = []

                if arguments.get("source"):
                    conditions.append("source_type = %s")
                    params.append(arguments["source"])

                if arguments.get("contact"):
                    conditions.append("metadata::text ILIKE %s")
                    params.append(f"%{arguments['contact']}%")

                if arguments.get("days_back"):
                    conditions.append("created_at > NOW() - (%s * INTERVAL '1 day')")
                    params.append(arguments["days_back"])

                where = (" AND " + " AND ".join(conditions)) if conditions else ""
                limit = arguments.get("limit", 5)

                # Format the vector as a string for pgvector
                vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"

                # Fetch extra rows to allow post-filtering by similarity threshold
                fetch_limit = max(limit * 4, 40)
                cur.execute(
                    f"""
                    SELECT content_preview, source_type, source_id, metadata,
                           1 - (embedding <=> %s::vector) as similarity,
                           created_at
                    FROM vec.embeddings
                    WHERE 1=1 {where}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    [vec_str] + params + [vec_str, fetch_limit],
                )

                rows = cur.fetchall()

                if not rows:
                    return {
                        "content": [{"type": "text", "text": "No matching messages found."}]
                    }

                # --- Similarity threshold + recency-weighted ranking ---
                SIMILARITY_THRESHOLD = 0.35
                from datetime import datetime, timezone

                def _recency_score(created_at):
                    """Return recency score 1.0/0.8/0.6/0.4 based on age."""
                    if created_at is None:
                        return 0.5
                    try:
                        if isinstance(created_at, str):
                            # Parse ISO string; handle both naive and aware
                            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        else:
                            ts = created_at
                        # Make timezone-aware if naive
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        age_days = (datetime.now(timezone.utc) - ts).days
                        if age_days <= 7:
                            return 1.0
                        elif age_days <= 30:
                            return 0.8
                        elif age_days <= 90:
                            return 0.6
                        else:
                            return 0.4
                    except Exception:
                        return 0.5

                def _composite_score(similarity, created_at):
                    return similarity * 0.7 + _recency_score(created_at) * 0.3

                # Filter by threshold, then sort by composite score, then trim to limit
                filtered = [
                    row for row in rows if row[4] >= SIMILARITY_THRESHOLD
                ]
                filtered.sort(key=lambda r: _composite_score(r[4], r[5]), reverse=True)
                filtered = filtered[:limit]

                if not filtered:
                    return {
                        "content": [{"type": "text", "text": f"No matching messages found (similarity threshold: {SIMILARITY_THRESHOLD})."}]
                    }

                lines = [f"Found {len(filtered)} matching conversation(s):\n"]
                for i, row in enumerate(filtered, 1):
                    chunk_text, source, source_id, metadata, similarity, created_at = row
                    meta = (
                        metadata
                        if isinstance(metadata, dict)
                        else json.loads(metadata) if metadata else {}
                    )
                    start_time = meta.get("start_time")
                    end_time = meta.get("end_time")
                    # Use created_at as date if meta times not available
                    date_val = start_time or (created_at.isoformat() if hasattr(created_at, 'isoformat') else str(created_at) if created_at else None)

                    def _fmt_ts(ts):
                        if ts is None:
                            return "?"
                        if isinstance(ts, str):
                            return ts[:16] if len(ts) >= 16 else ts
                        if hasattr(ts, "strftime"):
                            return ts.strftime("%Y-%m-%d %H:%M")
                        return str(ts)
                    time_str = ""
                    if start_time or end_time:
                        time_str = f" ({_fmt_ts(start_time)} - {_fmt_ts(end_time)})"

                    composite = _composite_score(similarity, created_at)

                    # Truncate chunk text for display
                    display_text = (
                        (chunk_text or "")[:500] + "..." if len(chunk_text or "") > 500 else (chunk_text or "")
                    )

                    lines.append(
                        f"--- [{source}] Match {i} (similarity: {similarity:.3f}, score: {composite:.3f}){time_str} ---"
                    )
                    if date_val:
                        lines.append(f"Date: {_fmt_ts(date_val)}")
                    if meta.get("participants"):
                        lines.append(
                            f"Participants: {', '.join(meta['participants'][:5])}"
                        )
                    elif meta.get("channel"):
                        lines.append(f"Channel: {meta['channel']}")
                    lines.append(display_text)
                    lines.append("")

                return {"content": [{"type": "text", "text": "\n".join(lines)}]}
            finally:
                conn.close()

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
    """Read a JSON-RPC message from stdin with framing auto-detection."""
    global _TRANSPORT_MODE

    first_line = sys.stdin.readline()
    while first_line and not first_line.strip():
        first_line = sys.stdin.readline()

    if not first_line:
        raise EOFError("stdin closed")

    stripped = first_line.strip()

    if stripped.lower().startswith("content-length:"):
        _TRANSPORT_MODE = "content-length"
        content_length = int(stripped.split(":", 1)[1].strip())
        while True:
            line = sys.stdin.readline()
            if not line:
                raise EOFError("stdin closed")
            if line in ("\r\n", "\n", ""):
                break
        body = sys.stdin.read(content_length)
        if not body:
            raise EOFError("stdin closed")
        return json.loads(body)

    _TRANSPORT_MODE = "line"
    return json.loads(stripped)


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
