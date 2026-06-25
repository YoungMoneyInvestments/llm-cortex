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

Hook scripts default to `claude-code` when `CORTEX_AGENT_NAME` is not set. MCP save tools default to `main` unless the MCP client passes `CORTEX_AGENT_NAME` in its environment. Set the variable explicitly for every client to avoid attribution collapse.

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

If your Codex build supports lifecycle hooks, add Cortex write hooks in `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "CORTEX_AGENT_NAME=codex /path/to/cortex/hooks/user_prompt_submit.sh",
        "timeout": 3,
        "async": true
      }]
    }],
    "PostToolUse": [{
      "hooks": [{
        "type": "command",
        "command": "CORTEX_AGENT_NAME=codex /path/to/cortex/hooks/post_tool_use.sh",
        "timeout": 3,
        "async": true
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "CORTEX_AGENT_NAME=codex /path/to/cortex/hooks/session_end.sh",
        "timeout": 5,
        "async": true
      }]
    }]
  }
}
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

Cursor currently uses Cortex through MCP tools. There is no verified Cursor lifecycle hook equivalent to Claude Code's `PostToolUse` / `UserPromptSubmit` / `SessionEnd` in this setup, so automatic observation capture is not assumed. Add a Cursor rule that tells the agent to search first and save durable facts with `cami_memory_save`; keep `CORTEX_AGENT_NAME=cursor` in the MCP env block.

### Gemini CLI (`~/.gemini/settings.json`)

```json
{
  "mcpServers": {
    "cortex-memory": {
      "command": "python3",
      "args": ["/path/to/cortex/src/mcp_memory_server.py"],
      "env": {
        "CORTEX_AGENT_NAME": "gemini"
      }
    }
  }
}
```

Gemini currently uses Cortex through MCP tools. There is no verified Gemini lifecycle hook equivalent to Claude Code's hooks in this setup, so automatic observation capture is not assumed. Put the memory contract in `~/.gemini/GEMINI.md`: search first, use timeline/details only as needed, and save only durable decisions/resolved bugs/user corrections.

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
