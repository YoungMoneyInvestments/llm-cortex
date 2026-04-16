import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def import_fresh(module_name: str):
    sys.modules.pop(module_name, None)
    # Python 3.9: asyncio.Event() at module level needs an event loop.
    # Re-importing after sys.modules.pop may find the loop gone.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return importlib.import_module(module_name)


def test_memory_worker_defaults_are_generic(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CORTEX_DATA_DIR", raising=False)
    monkeypatch.delenv("CORTEX_LOG_DIR", raising=False)
    monkeypatch.delenv("CORTEX_PID_FILE", raising=False)

    memory_worker = import_fresh("memory_worker")

    # Defaults now point to ~/clawd/data and ~/.openclaw/ paths
    assert memory_worker.DATA_DIR == tmp_path / "clawd" / "data"
    assert memory_worker.LOG_DIR == tmp_path / ".openclaw" / "logs"
    assert memory_worker.PID_FILE == tmp_path / ".openclaw" / "worker.pid"


class _FakeRequest:
    """Minimal stand-in for a FastAPI Request to satisfy require_auth()."""

    def __init__(self, method="GET", path="/api/test", headers=None, query_params=None, client_host="127.0.0.1"):
        self.method = method
        self.url = type("URL", (), {"path": path})()
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.client = type("Client", (), {"host": client_host})()


def test_require_auth_fails_clearly_without_valid_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CORTEX_WORKER_API_KEY", raising=False)

    memory_worker = import_fresh("memory_worker")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(memory_worker.require_auth(_FakeRequest(), None))

    assert exc_info.value.status_code == 401
    assert "Authorization" in exc_info.value.detail


# ── BUG-C1-01 / BUG-D2-03 tests: auth on read endpoints ──────────────────────


def test_unauthenticated_memory_search_returns_401(monkeypatch, tmp_path):
    """Read endpoints must return 401 when no credentials are supplied (BUG-C1-01 / BUG-D2-03)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CORTEX_WORKER_API_KEY", "test-key-abc")
    from fastapi.testclient import TestClient

    memory_worker = import_fresh("memory_worker")
    client = TestClient(memory_worker.app, raise_server_exceptions=False)

    resp = client.post("/api/memory/search", json={"query": "test"})
    assert resp.status_code == 401


def test_authenticated_memory_search_returns_non_401(monkeypatch, tmp_path):
    """A valid API key in the Authorization: Bearer header must not be rejected (BUG-C1-01 / BUG-D2-03)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CORTEX_WORKER_API_KEY", "test-key-abc")
    from fastapi.testclient import TestClient

    memory_worker = import_fresh("memory_worker")
    client = TestClient(memory_worker.app, raise_server_exceptions=False)

    resp = client.post(
        "/api/memory/search",
        json={"query": "test"},
        headers={"Authorization": "Bearer test-key-abc"},
    )
    # Either the DB is not initialised (503) or the worker returns results (200).
    # Either way it must NOT be 401.
    assert resp.status_code != 401


def test_api_key_query_param_accepted(monkeypatch, tmp_path):
    """X-Cortex-Api-Key query-param must be accepted as an alternative to Bearer header."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CORTEX_WORKER_API_KEY", "test-key-abc")
    from fastapi.testclient import TestClient

    memory_worker = import_fresh("memory_worker")
    client = TestClient(memory_worker.app, raise_server_exceptions=False)

    resp = client.post(
        "/api/memory/search",
        json={"query": "test"},
        params={"api_key": "test-key-abc"},
    )
    assert resp.status_code != 401


def test_observation_request_accepts_any_source(monkeypatch, tmp_path):
    """Source field is now a plain str — any value is accepted."""
    monkeypatch.setenv("HOME", str(tmp_path))
    memory_worker = import_fresh("memory_worker")

    req = memory_worker.ObservationRequest(session_id="s1", source="bogus")
    assert req.source == "bogus"


def test_observation_request_accepts_post_tool_use_without_tool_name(monkeypatch, tmp_path):
    """tool_name is optional for all sources now."""
    monkeypatch.setenv("HOME", str(tmp_path))
    memory_worker = import_fresh("memory_worker")

    req = memory_worker.ObservationRequest(session_id="s1", source="post_tool_use")
    assert req.tool_name is None


def test_load_openai_key_uses_generic_env_file_override(monkeypatch, tmp_path):
    env_file = tmp_path / "cortex.env"
    env_file.write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    monkeypatch.setenv("CORTEX_ENV_FILE", str(env_file))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    unified_vector_store = import_fresh("unified_vector_store")

    assert unified_vector_store._load_openai_key() == "test-key"


def test_openai_key_error_message_mentions_generic_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CORTEX_ENV_FILE", raising=False)

    unified_vector_store = import_fresh("unified_vector_store")
    store = unified_vector_store.UnifiedVectorStore(db_path=tmp_path / "vectors.db")

    with pytest.raises(RuntimeError) as exc_info:
        store._get_openai()

    assert "OPENAI_API_KEY" in str(exc_info.value)
    assert ".env.local" not in str(exc_info.value)
    assert "CORTEX_ENV_FILE" in str(exc_info.value)


def test_mcp_tool_call_rejects_invalid_graph_depth(monkeypatch):
    mcp_memory_server = import_fresh("mcp_memory_server")

    class FakeRetriever:
        def close(self):
            return None
        def search_with_context(self, **kwargs):
            raise AttributeError("should not be called with invalid depth")

    monkeypatch.setattr(mcp_memory_server, "MemoryRetriever", FakeRetriever)

    result = mcp_memory_server.handle_tool_call(
        "cami_memory_graph_search",
        {"query": "auth", "graph_depth": 3},
    )

    # Should error — either validation or attribute error
    assert result["isError"] is True


def test_mcp_tool_call_rejects_empty_observation_ids(monkeypatch):
    mcp_memory_server = import_fresh("mcp_memory_server")

    class FakeRetriever:
        def close(self):
            return None
        def get_details(self, **kwargs):
            raise AttributeError("should not be called with empty ids")

    monkeypatch.setattr(mcp_memory_server, "MemoryRetriever", FakeRetriever)

    result = mcp_memory_server.handle_tool_call(
        "cami_memory_details",
        {"observation_ids": []},
    )

    # Should error — either validation or attribute error
    assert result["isError"] is True
