# Multi-LLM Setup

LLM Cortex supports multiple LLM agents writing to the same memory database. Each agent tags its observations with a unique identifier, so you can filter by who wrote what while still sharing a unified knowledge base.

## How It Works

Set the `CORTEX_AGENT_NAME` environment variable before launching hooks or the MCP server. Each LLM writes its agent name into every observation, session, and saved memory.

```
CORTEX_AGENT_NAME=claude-code   # Claude Code
CORTEX_AGENT_NAME=codex         # OpenAI Codex CLI
CORTEX_AGENT_NAME=cursor        # Cursor IDE
CORTEX_AGENT_NAME=gemini        # Gemini CLI
CORTEX_AGENT_NAME=cami          # Custom agent
```

If not set, defaults to `main`.

## Configuration by LLM

### Claude Code (`~/.claude/settings.json`)

Prefix each hook command with the env var:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "CORTEX_AGENT_NAME=claude-code /path/to/cortex/hooks/user-prompt.sh"
      }]
    }],
    "PostToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "CORTEX_AGENT_NAME=claude-code /path/to/cortex/hooks/post-tool-use.sh"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "CORTEX_AGENT_NAME=claude-code /path/to/cortex/hooks/session-end.sh"
      }]
    }]
  },
  "mcpServers": {
    "cortex-memory": {
      "command": "python3",
      "args": ["/path/to/cortex/src/mcp_memory_server.py"],
      "env": {
        "CORTEX_AGENT_NAME": "claude-code"
      }
    }
  }
}
```

### Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.cortex-memory]
command = "python3"
args = ["/path/to/cortex/src/mcp_memory_server.py"]
startup_timeout_sec = 20

[mcp_servers.cortex-memory.env]
CORTEX_AGENT_NAME = "codex"
```

### Cursor IDE (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "cortex-memory": {
      "command": "python3",
      "args": ["/path/to/cortex/src/mcp_memory_server.py"],
      "env": {
        "CORTEX_AGENT_NAME": "cursor"
      }
    }
  }
}
```

### Any LLM via REST API

Any HTTP client can write observations directly:

```bash
curl -X POST http://127.0.0.1:37778/api/observations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CORTEX_WORKER_API_KEY" \
  -d '{
    "session_id": "my-session-123",
    "source": "post_tool_use",
    "tool_name": "Edit",
    "agent": "my-custom-agent",
    "raw_input": "...",
    "raw_output": "..."
  }'
```

## Querying by Agent

### MCP Tools

```
cami_memory_search(query="risk management", agent="claude-code")
```

### REST API

```bash
curl -X POST http://127.0.0.1:37778/api/memory/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk management", "agent": "claude-code"}'
```

### Direct SQL

```sql
SELECT * FROM observations WHERE agent = 'codex' ORDER BY id DESC LIMIT 20;
```

## Architecture

```
                    ┌─────────────┐
                    │  Cortex DB  │
                    │  (SQLite)   │
                    └──────┬──────┘
                           │
                    ┌──────┴──────┐
                    │   Worker    │
                    │ :37778/api  │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
   ┌──────┴──────┐  ┌─────┴─────┐  ┌──────┴──────┐
   │ Claude Code │  │   Codex   │  │   Cursor    │
   │ agent:      │  │ agent:    │  │ agent:      │
   │ claude-code │  │ codex     │  │ cursor      │
   └─────────────┘  └───────────┘  └─────────────┘
```

All agents share one database. The `agent` field on every row enables filtering and attribution without requiring separate storage.
