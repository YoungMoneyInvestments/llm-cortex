"""
Pass U — AICompressor edge-case tests.

Covers 4 demonstrable bugs in AICompressor / _call_ai_for_summary:

  U-01: 5xx responses not treated as transient (no backoff set)
  U-02: _call_ai_for_summary missing _record_success on 200 and _record_failure in except
  U-03: data["content"][0]["text"] crashes on non-text first block or empty content array
  U-04: _parse_typed_response fallback returns unbounded raw_text, polluting DB/vectors

Tests are pure unit tests — no FastAPI TestClient, no HTTP calls.
AICompressor is instantiated directly; httpx.AsyncClient is replaced with AsyncMock.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import httpx


def _make_response(status_code: int, body: str | dict | None = None) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if body is None:
        resp.text = ""
        resp.json.return_value = {}
    elif isinstance(body, str):
        resp.text = body
        resp.json.side_effect = ValueError("not JSON")
    else:
        resp.text = json.dumps(body)
        resp.json.return_value = body
    return resp


def _make_compressor_with_client(mock_post_side_effect=None, mock_post_return=None):
    """Return an AICompressor whose _client.post is mocked.

    _ensure_client is also patched to always return True so the mock client
    is used regardless of whether auth-profiles exist on disk.
    """
    # Import fresh each time to avoid module-level state leaking between tests
    if "memory_worker" in sys.modules:
        mw = sys.modules["memory_worker"]
    else:
        import memory_worker as mw

    compressor = mw.AICompressor()

    client_mock = AsyncMock()
    if mock_post_return is not None:
        client_mock.post = AsyncMock(return_value=mock_post_return)
    elif mock_post_side_effect is not None:
        client_mock.post = AsyncMock(side_effect=mock_post_side_effect)
    else:
        client_mock.post = AsyncMock(return_value=_make_response(200, {
            "content": [{"type": "text", "text": '{"type":"episodic","content":"ok","entities":[]}'}]
        }))

    compressor._client = client_mock
    compressor._token = "fake-token"

    # Patch _ensure_client so it always returns True without reading disk
    async def _noop_ensure_client():
        return True

    compressor._ensure_client = _noop_ensure_client

    return compressor, mw


def run(coro):
    """Run a coroutine synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Bug U-01: 5xx responses not given backoff
# ─────────────────────────────────────────────────────────────────────────────

class TestFivexxBackoff(unittest.TestCase):
    """Bug U-01: 500/502/503/504 must set _backoff_until, not just log and return None."""

    def _compress(self, status_code: int):
        resp = _make_response(status_code, "Server Error")
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "inp", "out"))
        return compressor, result

    def _assert_backoff_set(self, status_code: int):
        compressor, result = self._compress(status_code)
        self.assertIsNone(result, f"HTTP {status_code} must return None")
        self.assertGreater(
            compressor._backoff_until,
            time.monotonic(),
            f"HTTP {status_code} must set _backoff_until in the future (no backoff: bug U-01)"
        )

    def test_500_sets_backoff(self):
        self._assert_backoff_set(500)

    def test_502_sets_backoff(self):
        self._assert_backoff_set(502)

    def test_503_sets_backoff(self):
        self._assert_backoff_set(503)

    def test_504_sets_backoff(self):
        self._assert_backoff_set(504)

    def test_429_still_sets_backoff(self):
        """Regression: 429 behaviour must be preserved."""
        compressor, result = self._compress(429)
        self.assertIsNone(result)
        self.assertGreater(compressor._backoff_until, time.monotonic())

    def test_529_still_sets_backoff(self):
        """Regression: 529 behaviour must be preserved."""
        compressor, result = self._compress(529)
        self.assertIsNone(result)
        self.assertGreater(compressor._backoff_until, time.monotonic())


# ─────────────────────────────────────────────────────────────────────────────
# Bug U-02: _call_ai_for_summary missing _record_success / _record_failure
# ─────────────────────────────────────────────────────────────────────────────

class TestCallAiForSummaryStateTracking(unittest.TestCase):
    """Bug U-02: _call_ai_for_summary must call _record_success on 200 and
    _record_failure when an exception is thrown."""

    def _get_module(self):
        import memory_worker as mw
        return mw

    def test_200_clears_degraded_state(self):
        """A successful _call_ai_for_summary must reset _consecutive_failures."""
        mw = self._get_module()
        if mw.ai_compressor is None:
            self.skipTest("ai_compressor not initialised (no auth profile in CI)")

        compressor = mw.ai_compressor

        # Manually force degraded state
        original_failures = compressor._consecutive_failures
        compressor._consecutive_failures = 5
        compressor._degraded_since = "2099-01-01T00:00:00+00:00"

        good_resp = _make_response(200, {
            "content": [{"type": "text", "text": "summary text"}]
        })
        compressor._token = "fake-token"
        compressor._client = AsyncMock()
        compressor._client.post = AsyncMock(return_value=good_resp)

        result = run(mw._call_ai_for_summary("test prompt"))

        self.assertEqual(
            compressor._consecutive_failures,
            0,
            "_call_ai_for_summary 200 must call _record_success (failures not reset: bug U-02)"
        )
        # Restore
        compressor._consecutive_failures = original_failures

    def test_exception_records_failure(self):
        """An exception inside _call_ai_for_summary must increment _consecutive_failures."""
        mw = self._get_module()
        if mw.ai_compressor is None:
            self.skipTest("ai_compressor not initialised (no auth profile in CI)")

        compressor = mw.ai_compressor
        original_failures = compressor._consecutive_failures

        compressor._consecutive_failures = 0
        compressor._token = "fake-token"
        compressor._client = AsyncMock()
        compressor._client.post = AsyncMock(side_effect=RuntimeError("connection refused"))

        result = run(mw._call_ai_for_summary("test prompt"))

        self.assertIsNone(result)
        self.assertGreater(
            compressor._consecutive_failures,
            0,
            "_call_ai_for_summary exception must call _record_failure (bug U-02)"
        )
        # Restore
        compressor._consecutive_failures = original_failures


class TestCallAiForSummaryStateMockCompressor(unittest.TestCase):
    """Bug U-02 variant: test with a fully local mock compressor to avoid live state."""

    def _run_summary_with_mock_compressor(self, compressor, mw, prompt="test"):
        """Patch ai_compressor module-level and call _call_ai_for_summary."""
        orig = mw.ai_compressor
        mw.ai_compressor = compressor
        try:
            return run(mw._call_ai_for_summary(prompt))
        finally:
            mw.ai_compressor = orig

    def test_200_resets_consecutive_failures(self):
        import memory_worker as mw
        good_resp = _make_response(200, {
            "content": [{"type": "text", "text": "summary text"}]
        })
        compressor, _ = _make_compressor_with_client(mock_post_return=good_resp)
        compressor._consecutive_failures = 7  # degraded

        self._run_summary_with_mock_compressor(compressor, mw, "test prompt")

        self.assertEqual(
            compressor._consecutive_failures,
            0,
            "_call_ai_for_summary 200 must call _record_success (bug U-02)"
        )

    def test_exception_increments_consecutive_failures(self):
        import memory_worker as mw
        compressor, _ = _make_compressor_with_client(
            mock_post_side_effect=RuntimeError("network error")
        )
        compressor._consecutive_failures = 0

        self._run_summary_with_mock_compressor(compressor, mw, "test prompt")

        self.assertGreater(
            compressor._consecutive_failures,
            0,
            "_call_ai_for_summary except must call _record_failure (bug U-02)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug U-03: data["content"][0]["text"] assumes first block is text
# ─────────────────────────────────────────────────────────────────────────────

class TestContentBlockExtraction(unittest.TestCase):
    """Bug U-03: content block extraction must tolerate non-text first blocks
    and empty content arrays."""

    def test_empty_content_array_returns_none(self):
        """An empty content array must not crash with IndexError."""
        resp = _make_response(200, {"content": []})
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "in", "out"))
        # Must not raise; must return None (or empty string) gracefully
        # Before the fix this raises KeyError/IndexError (bug U-03)
        self.assertIsNone(
            result,
            "empty content array must return None, not crash (bug U-03)"
        )

    def test_first_block_tool_use_second_block_text(self):
        """When first block is tool_use and second is text, the text must be used."""
        resp = _make_response(200, {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "calculator", "input": {}},
                {"type": "text", "text": '{"type":"fact","content":"extracted text","entities":[]}'},
            ]
        })
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "in", "out"))
        self.assertIsNotNone(
            result,
            "text block after non-text block must be found (bug U-03)"
        )
        self.assertIn("extracted text", result)

    def test_no_text_block_at_all_returns_none(self):
        """If all content blocks are non-text, must return None gracefully."""
        resp = _make_response(200, {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "fn", "input": {}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
            ]
        })
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "in", "out"))
        self.assertIsNone(
            result,
            "no text block must return None, not crash (bug U-03)"
        )

    def test_normal_text_block_still_works(self):
        """Regression: standard single text block must still parse correctly."""
        resp = _make_response(200, {
            "content": [{"type": "text", "text": '{"type":"episodic","content":"done","entities":[]}'}]
        })
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "in", "out"))
        self.assertEqual(result, "done")

    def test_missing_content_key_entirely(self):
        """If Anthropic returns JSON without 'content' key at all, must not crash."""
        resp = _make_response(200, {"id": "msg_123", "type": "message"})
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "in", "out"))
        self.assertIsNone(result, "missing content key must return None (bug U-03)")

    def test_non_json_200_body_returns_none(self):
        """A 200 with non-JSON body must not crash; must record failure."""
        resp = _make_response(200, None)
        resp.text = "Internal proxy error"
        resp.json.side_effect = ValueError("not json")
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)
        result = run(compressor.compress("test", "Read", "main", "in", "out"))
        self.assertIsNone(result, "non-JSON 200 must return None, not crash (bug U-03)")

    def test_call_ai_for_summary_empty_content(self):
        """_call_ai_for_summary must also tolerate empty content array."""
        import memory_worker as mw
        resp = _make_response(200, {"content": []})
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)

        orig = mw.ai_compressor
        mw.ai_compressor = compressor
        try:
            result = run(mw._call_ai_for_summary("test"))
        finally:
            mw.ai_compressor = orig

        self.assertIsNone(result, "_call_ai_for_summary empty content must return None (bug U-03)")

    def test_call_ai_for_summary_non_text_first_block(self):
        """_call_ai_for_summary must find text block even if first block is non-text."""
        import memory_worker as mw
        resp = _make_response(200, {
            "content": [
                {"type": "thinking", "thinking": "let me think..."},
                {"type": "text", "text": "session summary result"},
            ]
        })
        compressor, _ = _make_compressor_with_client(mock_post_return=resp)

        orig = mw.ai_compressor
        mw.ai_compressor = compressor
        try:
            result = run(mw._call_ai_for_summary("test"))
        finally:
            mw.ai_compressor = orig

        self.assertEqual(result, "session summary result",
            "_call_ai_for_summary must find text block after non-text block (bug U-03)")


# ─────────────────────────────────────────────────────────────────────────────
# Bug U-04: _parse_typed_response fallback returns unbounded raw_text
# ─────────────────────────────────────────────────────────────────────────────

class TestParseTypedResponseTruncation(unittest.TestCase):
    """Bug U-04: fallback content from non-JSON responses must be bounded."""

    def setUp(self):
        import memory_worker as mw
        self.parse = mw.AICompressor._parse_typed_response

    def test_valid_json_returns_content_unchanged(self):
        """Regression: valid JSON with short content must be returned as-is."""
        raw = '{"type": "fact", "content": "short summary", "entities": ["foo"]}'
        content, mtype, entities = self.parse(raw)
        self.assertEqual(content, "short summary")
        self.assertEqual(mtype, "fact")

    def test_invalid_json_fallback_is_truncated(self):
        """Non-JSON raw_text must be truncated to at most 300 chars in fallback path."""
        raw = "A" * 5000
        content, mtype, entities = self.parse(raw)
        self.assertLessEqual(
            len(content),
            300,
            f"Fallback content must be bounded (got {len(content)} chars): bug U-04"
        )
        self.assertEqual(mtype, "episodic")

    def test_json_with_missing_content_field_fallback_is_truncated(self):
        """JSON that has no 'content' key falls back to raw_text; must truncate."""
        raw = json.dumps({"type": "fact", "entities": ["thing"]}) + "X" * 2000
        # This is actually non-parseable JSON (trailing chars after the dict),
        # so falls through the JSONDecodeError path
        content, mtype, entities = self.parse(raw)
        self.assertLessEqual(
            len(content),
            300,
            f"Malformed-JSON fallback must be bounded: bug U-04"
        )

    def test_empty_content_field_falls_back_to_truncated_raw(self):
        """JSON with content='' falls back to raw_text; must truncate if raw is long."""
        big_raw = '{"type": "fact", "content": "", "entities": []}' + "Z" * 3000
        content, mtype, entities = self.parse(big_raw)
        # The 'or raw_text' fallback fires when content is empty string
        # But the raw_text itself is huge — it must be capped
        self.assertLessEqual(
            len(content),
            300,
            f"Empty-content-field fallback must be bounded: bug U-04"
        )

    def test_normal_short_non_json_text_passthrough(self):
        """A short (< 300 char) non-JSON text must pass through without modification."""
        raw = "edited /foo/bar.py: added retry logic"
        content, mtype, entities = self.parse(raw)
        self.assertEqual(content, raw)

    def test_unicode_heavy_content_truncated_correctly(self):
        """Non-JSON with unicode/emoji content must truncate by char count, not bytes."""
        raw = "こんにちは" * 100  # 500 chars, ~1500 bytes
        content, mtype, entities = self.parse(raw)
        self.assertLessEqual(len(content), 300)

    def test_code_block_wrapped_json_still_parses(self):
        """Regression: triple-backtick JSON must still parse through the fence-strip path."""
        raw = '```json\n{"type": "decision", "content": "used X", "entities": []}\n```'
        content, mtype, entities = self.parse(raw)
        self.assertEqual(content, "used X")
        self.assertEqual(mtype, "decision")


if __name__ == "__main__":
    unittest.main()
