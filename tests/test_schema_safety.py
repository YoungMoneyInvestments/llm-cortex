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
    return importlib.import_module(module_name)


def test_memory_worker_defaults_are_generic(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CORTEX_DATA_DIR", raising=False)
    monkeypatch.delenv("CORTEX_LOG_DIR", raising=False)
    monkeypatch.delenv("CORTEX_PID_FILE", raising=False)

    memory_worker = import_fresh("memory_worker")

    assert memory_worker.DATA_DIR == tmp_path / ".cortex" / "data"
    assert memory_worker.LOG_DIR == tmp_path / ".cortex" / "logs"
    assert memory_worker.PID_FILE == tmp_path / ".cortex" / "worker.pid"


def test_require_auth_fails_clearly_without_configured_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CORTEX_WORKER_API_KEY", raising=False)

    memory_worker = import_fresh("memory_worker")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(memory_worker.require_auth(None))

    assert exc_info.value.status_code == 503
    assert "CORTEX_WORKER_API_KEY" in exc_info.value.detail


def test_observation_request_rejects_invalid_source():
    memory_worker = import_fresh("memory_worker")

    with pytest.raises(ValidationError):
        memory_worker.ObservationRequest(session_id="s1", source="bogus")


def test_observation_request_requires_tool_name_for_post_tool_use():
    memory_worker = import_fresh("memory_worker")

    with pytest.raises(ValidationError):
        memory_worker.ObservationRequest(session_id="s1", source="post_tool_use")


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

    monkeypatch.setattr(mcp_memory_server, "MemoryRetriever", FakeRetriever)

    result = mcp_memory_server.handle_tool_call(
        "cami_memory_graph_search",
        {"query": "auth", "graph_depth": 3},
    )

    assert result["isError"] is True
    assert "graph_depth" in result["content"][0]["text"]


def test_mcp_tool_call_rejects_empty_observation_ids(monkeypatch):
    mcp_memory_server = import_fresh("mcp_memory_server")

    class FakeRetriever:
        def close(self):
            return None

    monkeypatch.setattr(mcp_memory_server, "MemoryRetriever", FakeRetriever)

    result = mcp_memory_server.handle_tool_call(
        "cami_memory_details",
        {"observation_ids": []},
    )

    assert result["isError"] is True
    assert "observation_ids" in result["content"][0]["text"]
