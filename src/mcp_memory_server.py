#!/usr/bin/env python3
"""
MCP Memory Server — Exposes Cortex memory retrieval as MCP tools.

Provides 8 MCP tools that follow the token-efficient 3-layer pattern:
  1. cami_memory_search       — L1: Compact index search (~20 tokens/result)
  2. cami_memory_timeline     — L2: Chronological context (~100 tokens/item)
  3. cami_memory_details      — L3: Full observation text (variable)
  4. cami_memory_save         — Save a memory for future retrieval
  5. cami_memory_graph_search — Graph-augmented search with entity expansion
  6. cami_message_search      — Semantic search over iMessage/Discord via sqlite-vec
  7. cami_contact_search      — Search unified contacts database
  8. session_bootstrap        — Bootstrap session with current time + 48h history

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
import logging
import os
import sqlite3
import sys
from pathlib import Path

# Add scripts dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from memory_retriever import MemoryRetriever

logger = logging.getLogger("cortex-mcp")

# Hard caps applied to all tool limit/window/graph_depth params.
# These prevent the LLM from accidentally requesting thousands of results
# that would flood the client context window.
_MAX_LIMIT = 100          # cami_memory_search, cami_memory_graph_search, cami_message_search, cami_contact_search
_MAX_WINDOW = 50          # cami_memory_timeline window
_MAX_OBSERVATION_IDS = 20 # cami_memory_details
_MAX_CONTENT_LEN = 20_000 # cami_memory_save content


def _int_arg(arguments: dict, key: str, default: int, *, lo: int = 1, hi: int) -> int:
    """Parse an integer argument, coercing strings, clamping to [lo, hi]."""
    raw = arguments.get(key, default)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


# Lazy-loaded sentence-transformer model for message search (384-dim, all-MiniLM-L6-v2)
_ST_MODEL = None

def _get_st_model():
    global _ST_MODEL
    if _ST_MODEL is None:
        from sentence_transformers import SentenceTransformer
        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _ST_MODEL

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
                    "enum": ["imessage", "discord", "facebook", "instagram"],
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
    {
        "name": "cami_contact_search",
        "description": (
            "Search Cameron's unified contacts database. Find people by name, phone, email, "
            "relationship type, or notes. Returns structured contact profiles with communication "
            "history across iMessage, Discord, Facebook, and Instagram."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, phone number, email, or keyword to search for",
                },
                "relationship_type": {
                    "type": "string",
                    "description": "Filter by: family, friend, business, family_business, romantic, acquaintance",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "session_bootstrap",
        "description": (
            "Bootstrap a new session with current time, user profile, recent activity, "
            "and session history from the last 48 hours. Call this at the start of every "
            "session before responding to the user's first prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours of history to include (default: 48)",
                    "default": 48,
                },
            },
            "required": [],
        },
    },
]


# ── Contact search ─────────────────────────────────────────────────────────

CONTACTS_DB_PATH = "/Users/cameronbennion/clawd/data/unified_contacts.db"


def search_contacts(query: str, relationship_type: str = None, limit: int = 10) -> str:
    """Search the unified contacts database and return formatted results."""
    if not os.path.exists(CONTACTS_DB_PATH):
        return "Contacts DB not built yet. Run build_unified_contacts_db.py"

    conn = sqlite3.connect(f"file:{CONTACTS_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        rows = []
        stripped = query.strip()

        # Detect phone number queries (digits only, 10-11 chars)
        digits_only = stripped.replace("-", "").replace("(", "").replace(")", "").replace(" ", "").replace("+", "")
        is_phone = digits_only.isdigit() and 10 <= len(digits_only) <= 11

        if is_phone:
            cur.execute(
                "SELECT contact_id FROM phone_lookup WHERE phone_normalized LIKE ?",
                (f"%{digits_only[-10:]}%",),
            )
            contact_ids = [r["contact_id"] for r in cur.fetchall()]
            if contact_ids:
                placeholders = ",".join("?" * len(contact_ids))
                sql = f"SELECT * FROM contacts WHERE id IN ({placeholders})"
                params = list(contact_ids)
                if relationship_type:
                    sql += " AND relationship_type = ?"
                    params.append(relationship_type)
                sql += " LIMIT ?"
                params.append(limit)
                cur.execute(sql, params)
                rows = cur.fetchall()

        elif "@" in stripped:
            cur.execute(
                "SELECT contact_id FROM email_lookup WHERE email LIKE ?",
                (f"%{stripped}%",),
            )
            contact_ids = [r["contact_id"] for r in cur.fetchall()]
            if contact_ids:
                placeholders = ",".join("?" * len(contact_ids))
                sql = f"SELECT * FROM contacts WHERE id IN ({placeholders})"
                params = list(contact_ids)
                if relationship_type:
                    sql += " AND relationship_type = ?"
                    params.append(relationship_type)
                sql += " LIMIT ?"
                params.append(limit)
                cur.execute(sql, params)
                rows = cur.fetchall()

        else:
            # Try FTS5 first
            try:
                fts_query = stripped.replace('"', '""')
                sql = (
                    "SELECT c.* FROM contacts_fts fts "
                    "JOIN contacts c ON c.id = fts.rowid "
                    "WHERE contacts_fts MATCH ?"
                )
                params = [f'"{fts_query}"']
                if relationship_type:
                    sql += " AND c.relationship_type = ?"
                    params.append(relationship_type)
                sql += " LIMIT ?"
                params.append(limit)
                cur.execute(sql, params)
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # FTS table may not exist
                rows = []

            # Fall back to LIKE on display_name if FTS returned nothing
            if not rows:
                sql = "SELECT * FROM contacts WHERE display_name LIKE ?"
                params = [f"%{stripped}%"]
                if relationship_type:
                    sql += " AND relationship_type = ?"
                    params.append(relationship_type)
                sql += " LIMIT ?"
                params.append(limit)
                cur.execute(sql, params)
                rows = cur.fetchall()

        if not rows:
            return f'No contacts found matching "{query}".'

        lines = [f'Found {len(rows)} contact(s) matching "{query}":\n']
        for i, row in enumerate(rows, 1):
            row_dict = dict(row)
            display_name = row_dict.get("display_name", "Unknown")
            rel_type = row_dict.get("relationship_type", "unknown")
            notes = row_dict.get("notes", "")
            profile_path = row_dict.get("profile_path", "")
            total_messages = row_dict.get("total_messages", 0)

            # Parse JSON arrays safely
            def _parse_json_list(val):
                if not val:
                    return []
                try:
                    parsed = json.loads(val)
                    return parsed if isinstance(parsed, list) else [str(parsed)]
                except (json.JSONDecodeError, TypeError):
                    return [str(val)] if val else []

            phones = _parse_json_list(row_dict.get("phones"))
            emails = _parse_json_list(row_dict.get("emails"))
            platforms = _parse_json_list(row_dict.get("platforms"))
            aliases = _parse_json_list(row_dict.get("aliases"))

            lines.append(f"{i}. **{display_name}** ({rel_type})")
            phone_str = ", ".join(phones) if phones else "—"
            email_str = ", ".join(emails) if emails else "—"
            lines.append(f"   Phone: {phone_str} | Email: {email_str}")
            platform_str = ", ".join(platforms) if platforms else "—"
            lines.append(f"   Platforms: {platform_str} | Messages: {total_messages or 0}")
            if aliases:
                lines.append(f"   Aliases: {', '.join(aliases)}")
            if notes:
                # Truncate long notes
                note_display = notes[:200] + "..." if len(notes) > 200 else notes
                lines.append(f"   Notes: {note_display}")
            if profile_path:
                lines.append(f"   Profile: {profile_path}")
            lines.append("")

        return "\n".join(lines)
    finally:
        conn.close()


# ── Tool handlers ───────────────────────────────────────────────────────────


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle a tool call and return the result."""
    retriever = None

    try:
        retriever = MemoryRetriever()

        if name == "cami_memory_search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return {"content": [{"type": "text", "text": "Error: 'query' must be a non-empty string"}], "isError": True}
            limit = _int_arg(arguments, "limit", 15, lo=1, hi=_MAX_LIMIT)
            results = retriever.search(
                query=query,
                limit=limit,
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
            try:
                obs_id = int(arguments["observation_id"])
            except (KeyError, TypeError, ValueError):
                return {"content": [{"type": "text", "text": "Error: 'observation_id' must be an integer"}], "isError": True}
            window = _int_arg(arguments, "window", 5, lo=1, hi=_MAX_WINDOW)
            context = retriever.timeline(
                observation_id=obs_id,
                window=window,
            )
            lines = [f"Timeline around observation #{obs_id}:\n"]
            for r in context:
                marker = ">>>" if r.get("is_target") else "   "
                lines.append(
                    f"  {marker} #{r['id']} [{r.get('tool', '?')}]: {r['summary']}"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "cami_memory_details":
            raw_ids = arguments.get("observation_ids")
            if not isinstance(raw_ids, list) or len(raw_ids) == 0:
                return {"content": [{"type": "text", "text": "Error: 'observation_ids' must be a non-empty list of integers"}], "isError": True}
            try:
                obs_ids = [int(x) for x in raw_ids[:_MAX_OBSERVATION_IDS]]
            except (TypeError, ValueError):
                return {"content": [{"type": "text", "text": "Error: 'observation_ids' must contain only integers"}], "isError": True}
            details = retriever.get_details(obs_ids)
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
            content = arguments.get("content")
            if not isinstance(content, str) or not content.strip():
                return {"content": [{"type": "text", "text": "Error: 'content' must be a non-empty string"}], "isError": True}
            content = content[:_MAX_CONTENT_LEN]
            metadata = {}
            if arguments.get("tags"):
                metadata["tags"] = arguments["tags"]
            agent = arguments.get("agent") or os.environ.get("CORTEX_AGENT_NAME", "main")
            metadata["agent"] = agent
            mem_id = retriever.save_memory(content, metadata)
            return {
                "content": [
                    {"type": "text", "text": f"Memory saved with ID: {mem_id}"}
                ]
            }

        elif name == "cami_memory_graph_search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return {"content": [{"type": "text", "text": "Error: 'query' must be a non-empty string"}], "isError": True}
            limit = _int_arg(arguments, "limit", 15, lo=1, hi=_MAX_LIMIT)
            try:
                graph_depth = int(arguments.get("graph_depth", 1))
            except (TypeError, ValueError):
                graph_depth = -1
            if graph_depth not in (1, 2):
                return {"content": [{"type": "text", "text": "Error: 'graph_depth' must be 1 or 2"}], "isError": True}
            results = retriever.search_with_context(
                query=query,
                limit=limit,
                graph_depth=graph_depth,
            )
            lines = [f"Graph search: '{query}' — {len(results)} results\n"]
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
            import struct
            import time as _time
            from datetime import datetime

            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return {"content": [{"type": "text", "text": "Error: 'query' must be a non-empty string"}], "isError": True}
            source_filter = arguments.get("source")  # "imessage", "discord", or None
            contact_filter = arguments.get("contact")
            # days_back: explicit None means no filter; 0 or negative means no filter too
            _raw_days = arguments.get("days_back")
            days_back = None
            if _raw_days is not None:
                try:
                    _days_int = int(_raw_days)
                    if _days_int > 0:
                        days_back = _days_int
                except (TypeError, ValueError):
                    pass
            limit = _int_arg(arguments, "limit", 5, lo=1, hi=_MAX_LIMIT)
            SIMILARITY_THRESHOLD = 0.30

            IMESSAGE_DB = Path.home() / "clawd/data/imessage-embeddings.db"
            DISCORD_DB = Path.home() / "clawd/data/discord-embeddings.db"
            FACEBOOK_DB = Path.home() / "clawd/data/facebook-embeddings.db"
            INSTAGRAM_DB = Path.home() / "clawd/data/instagram-embeddings.db"

            # Embed query using sentence-transformers all-MiniLM-L6-v2 (384-dim)
            # — same model used to build both embedding DBs
            try:
                model = _get_st_model()
                query_vec = model.encode([query])[0].tolist()
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"Error loading embedding model: {e}"}],
                    "isError": True,
                }

            embedding_bytes = struct.pack(f"{len(query_vec)}f", *query_vec)

            def _load_sqlite_vec(conn):
                conn.enable_load_extension(True)
                import sqlite_vec
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

            def _recency_score(ts_seconds):
                if ts_seconds is None:
                    return 0.5
                try:
                    age_days = (_time.time() - ts_seconds) / 86400
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

            def _fmt_ts(ts_seconds):
                if ts_seconds is None:
                    return "?"
                try:
                    return datetime.fromtimestamp(ts_seconds).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return str(ts_seconds)

            all_results = []

            # --- iMessage search ---
            if source_filter in (None, "imessage") and IMESSAGE_DB.exists():
                try:
                    iconn = sqlite3.connect(str(IMESSAGE_DB))
                    iconn.row_factory = sqlite3.Row
                    _load_sqlite_vec(iconn)

                    time_cutoff = (_time.time() - days_back * 86400) if days_back else None
                    conditions, extra_params = [], []
                    if contact_filter:
                        conditions.append(
                            "(c.contact_identifier LIKE ? OR c.contact_name LIKE ?)"
                        )
                        extra_params += [f"%{contact_filter}%", f"%{contact_filter}%"]
                    if time_cutoff:
                        conditions.append("c.end_time >= ?")
                        extra_params.append(int(time_cutoff))

                    where = ("AND " + " AND ".join(conditions)) if conditions else ""
                    fetch_limit = max(limit * 4, 40)

                    rows = iconn.execute(
                        f"""
                        SELECT c.chunk_text, c.contact_identifier, c.contact_name,
                               c.start_time, c.end_time,
                               vec_distance_cosine(e.embedding, ?) as distance
                        FROM chunk_embeddings e
                        JOIN chunks c ON c.id = e.chunk_id
                        WHERE 1=1 {where}
                        ORDER BY distance
                        LIMIT ?
                        """,
                        [embedding_bytes] + extra_params + [fetch_limit],
                    ).fetchall()
                    iconn.close()

                    for row in rows:
                        similarity = 1.0 - (row["distance"] / 2.0)
                        if similarity >= SIMILARITY_THRESHOLD:
                            all_results.append({
                                "source": "imessage",
                                "text": row["chunk_text"] or "",
                                "contact": row["contact_name"] or row["contact_identifier"] or "Unknown",
                                "channel": None,
                                "start_time": row["start_time"],
                                "end_time": row["end_time"],
                                "similarity": similarity,
                            })
                except Exception as e:
                    all_results.append({
                        "source": "imessage",
                        "text": f"[iMessage search error: {e}]",
                        "contact": None, "channel": None,
                        "start_time": None, "end_time": None,
                        "similarity": 0.0,
                    })

            # --- Facebook / Instagram search (same schema as iMessage) ---
            for _plat, _db in [("facebook", FACEBOOK_DB), ("instagram", INSTAGRAM_DB)]:
                if source_filter not in (None, _plat) or not _db.exists():
                    continue
                try:
                    pconn = sqlite3.connect(str(_db))
                    pconn.row_factory = sqlite3.Row
                    _load_sqlite_vec(pconn)

                    time_cutoff = (_time.time() - days_back * 86400) if days_back else None
                    conditions, extra_params = [], []
                    if contact_filter:
                        conditions.append(
                            "(c.conversation_name LIKE ? OR c.participants LIKE ?)"
                        )
                        extra_params += [f"%{contact_filter}%", f"%{contact_filter}%"]
                    if time_cutoff:
                        conditions.append("c.end_time >= ?")
                        extra_params.append(int(time_cutoff))

                    where = ("AND " + " AND ".join(conditions)) if conditions else ""
                    fetch_limit = max(limit * 4, 40)

                    rows = pconn.execute(
                        f"""
                        SELECT c.chunk_text, c.conversation_name, c.participants,
                               c.start_time, c.end_time,
                               vec_distance_cosine(e.embedding, ?) as distance
                        FROM chunk_embeddings e
                        JOIN chunks c ON c.id = e.chunk_id
                        WHERE 1=1 {where}
                        ORDER BY distance
                        LIMIT ?
                        """,
                        [embedding_bytes] + extra_params + [fetch_limit],
                    ).fetchall()
                    pconn.close()

                    for row in rows:
                        similarity = 1.0 - (row["distance"] / 2.0)
                        if similarity >= SIMILARITY_THRESHOLD:
                            try:
                                parts = json.loads(row["participants"] or "[]")
                                contact_display = ", ".join(p for p in parts if p != "Cameron Bennion")[:60] or row["conversation_name"]
                            except Exception:
                                contact_display = row["conversation_name"] or "Unknown"
                            all_results.append({
                                "source": _plat,
                                "text": row["chunk_text"] or "",
                                "contact": contact_display,
                                "channel": None,
                                "start_time": row["start_time"],
                                "end_time": row["end_time"],
                                "similarity": similarity,
                            })
                except Exception as e:
                    all_results.append({
                        "source": _plat,
                        "text": f"[{_plat} search error: {e}]",
                        "contact": None, "channel": None,
                        "start_time": None, "end_time": None,
                        "similarity": 0.0,
                    })

            # --- Discord search ---
            if source_filter in (None, "discord") and DISCORD_DB.exists():
                try:
                    dconn = sqlite3.connect(str(DISCORD_DB))
                    dconn.row_factory = sqlite3.Row
                    _load_sqlite_vec(dconn)

                    time_cutoff = (_time.time() - days_back * 86400) if days_back else None
                    conditions, extra_params = [], []
                    if contact_filter:
                        conditions.append("c.channel_name LIKE ?")
                        extra_params.append(f"%{contact_filter}%")
                    if time_cutoff:
                        conditions.append("c.end_time >= ?")
                        extra_params.append(int(time_cutoff))

                    where = ("AND " + " AND ".join(conditions)) if conditions else ""
                    fetch_limit = max(limit * 4, 40)

                    rows = dconn.execute(
                        f"""
                        SELECT c.text, c.channel_name, c.guild_id,
                               c.start_time, c.end_time,
                               vec_distance_cosine(e.embedding, ?) as distance
                        FROM chunk_embeddings e
                        JOIN chunks c ON c.id = e.chunk_id
                        WHERE 1=1 {where}
                        ORDER BY distance
                        LIMIT ?
                        """,
                        [embedding_bytes] + extra_params + [fetch_limit],
                    ).fetchall()
                    dconn.close()

                    for row in rows:
                        similarity = 1.0 - (row["distance"] / 2.0)
                        if similarity >= SIMILARITY_THRESHOLD:
                            all_results.append({
                                "source": "discord",
                                "text": row["text"] or "",
                                "contact": None,
                                "channel": row["channel_name"] or row["guild_id"],
                                "start_time": row["start_time"],
                                "end_time": row["end_time"],
                                "similarity": similarity,
                            })
                except Exception as e:
                    all_results.append({
                        "source": "discord",
                        "text": f"[Discord search error: {e}]",
                        "contact": None, "channel": None,
                        "start_time": None, "end_time": None,
                        "similarity": 0.0,
                    })

            if not all_results:
                src_note = f" in {source_filter}" if source_filter else ""
                return {
                    "content": [{"type": "text", "text": f"No matching messages found{src_note} (threshold: {SIMILARITY_THRESHOLD})."}]
                }

            def _composite(r):
                return r["similarity"] * 0.7 + _recency_score(r.get("end_time")) * 0.3

            all_results.sort(key=_composite, reverse=True)
            all_results = all_results[:limit]

            lines = [f"Found {len(all_results)} matching conversation(s):\n"]
            for i, r in enumerate(all_results, 1):
                time_str = (
                    f" ({_fmt_ts(r['start_time'])} - {_fmt_ts(r['end_time'])})"
                    if r.get("start_time") else ""
                )
                composite = _composite(r)
                lines.append(
                    f"--- [{r['source']}] Match {i} (similarity: {r['similarity']:.3f}, score: {composite:.3f}){time_str} ---"
                )
                if r.get("contact"):
                    lines.append(f"Participants: {r['contact']}")
                if r.get("channel"):
                    lines.append(f"Channel: {r['channel']}")
                display = r["text"][:500] + "..." if len(r["text"]) > 500 else r["text"]
                lines.append(display)
                lines.append("")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "cami_contact_search":
            contact_query = arguments.get("query")
            if not isinstance(contact_query, str) or not contact_query.strip():
                return {"content": [{"type": "text", "text": "Error: 'query' must be a non-empty string"}], "isError": True}
            contact_limit = _int_arg(arguments, "limit", 10, lo=1, hi=_MAX_LIMIT)
            result_text = search_contacts(
                query=contact_query,
                relationship_type=arguments.get("relationship_type"),
                limit=contact_limit,
            )
            return {"content": [{"type": "text", "text": result_text}]}

        elif name == "session_bootstrap":
            import subprocess
            hours = _int_arg(arguments, "hours", 48, lo=1, hi=720)
            script = Path(__file__).parent / "context_loader.py"
            try:
                result = subprocess.run(
                    [sys.executable, str(script), "--hours", str(hours)],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(Path(__file__).parent.parent),
                )
                output = result.stdout.strip()
                if result.returncode != 0 and result.stderr:
                    output += f"\n[stderr: {result.stderr[:200]}]"
                if not output:
                    output = "[session_bootstrap: no output from context_loader]"
            except subprocess.TimeoutExpired:
                output = "[session_bootstrap: context_loader timed out after 30s]"
            except Exception as e:
                output = f"[session_bootstrap error: {e}]"
            return {"content": [{"type": "text", "text": output}]}

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }

    except Exception as e:
        logger.exception("Tool %s failed: %s", name, e)
        return {
            "content": [{"type": "text", "text": f"Error: {str(e)}"}],
            "isError": True,
        }
    finally:
        if retriever is not None:
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
