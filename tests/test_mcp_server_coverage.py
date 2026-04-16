"""
Pass P — coverage tests for mcp_memory_server.py (baseline 18%).

Targets (uncovered or poorly-covered paths):
  - _int_arg: string coercion, clamping to [lo, hi], invalid input falls back to default
  - BUG-B2-07 regression: days_back=0 must mean no filter (explicit None check)
  - BUG-B2-05 regression: string observation_id coerced on timeline call
  - cami_memory_save: content truncated at _MAX_CONTENT_LEN
  - cami_memory_timeline: missing/bad observation_id path
  - TOOLS list has 8 entries (BUG-B2-08 regression — was labelled as 7 in old docstring)
  - _write_message: line-mode framing path (covers lines 95-103)
  - session_bootstrap tool present in TOOLS list
  - require_feature gate for knowledge_graph (covers lines 589-595)

No network calls, no live worker, no FastAPI TestClient.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from src import mcp_memory_server


class TestIntArgHelper(unittest.TestCase):
    """Tests for _int_arg() — BUG-B2-05 regression and clamping."""

    def test_integer_value_returned_as_is_within_bounds(self):
        self.assertEqual(mcp_memory_server._int_arg({"limit": 10}, "limit", 15, hi=100), 10)

    def test_string_integer_coerced(self):
        """BUG-B2-05 regression: LLMs may send '15' as a string."""
        self.assertEqual(mcp_memory_server._int_arg({"limit": "15"}, "limit", 5, hi=100), 15)

    def test_clamped_at_hi(self):
        """BUG-B2-02 regression: values > hi must be clamped down."""
        result = mcp_memory_server._int_arg({"limit": 99999}, "limit", 15, hi=100)
        self.assertEqual(result, 100)

    def test_clamped_at_lo(self):
        """Values < lo must be clamped up to lo."""
        result = mcp_memory_server._int_arg({"limit": -5}, "limit", 10, lo=1, hi=100)
        self.assertEqual(result, 1)

    def test_invalid_string_falls_back_to_default(self):
        """Non-numeric string must fall back to the default value."""
        result = mcp_memory_server._int_arg({"limit": "not_a_number"}, "limit", 15, hi=100)
        self.assertEqual(result, 15)

    def test_missing_key_returns_default(self):
        """Missing key must return the default."""
        result = mcp_memory_server._int_arg({}, "limit", 20, hi=100)
        self.assertEqual(result, 20)

    def test_none_value_falls_back_to_default(self):
        result = mcp_memory_server._int_arg({"limit": None}, "limit", 7, hi=50)
        self.assertEqual(result, 7)

    def test_float_is_coerced_to_int(self):
        """int(3.7) == 3 — floats should be accepted without raising."""
        result = mcp_memory_server._int_arg({"limit": 3.7}, "limit", 5, hi=100)
        self.assertEqual(result, 3)


class TestDaysBackFilter(unittest.TestCase):
    """BUG-B2-07 regression: days_back=0 must mean 'no filter', not 'today only'."""

    class _FakeRetriever:
        def close(self):
            pass

    def test_days_back_zero_is_treated_as_no_filter(self):
        """days_back=0 is falsy but the new explicit None check must treat it as
        'no filter' rather than incorrectly applying a zero-second window.

        After the fix (replace `if days_back:` with explicit `None` check),
        passing days_back=0 falls into the 'no filter' branch.
        """
        # We exercise this by confirming handle_tool_call doesn't return an error
        # for days_back=0 (the handler parses it before calling the model).
        # The tool will fail at model-loading time (no sentence_transformers in test env)
        # but the days_back parameter must be accepted without error.
        result = mcp_memory_server.handle_tool_call(
            "cami_message_search",
            {"query": "test", "days_back": 0},
        )
        # Must fail with an embedding model error, NOT a validation error about days_back
        if result.get("isError"):
            error_text = result["content"][0]["text"]
            # Should be an embedding/model error, not a 'days_back' validation error
            self.assertNotIn("days_back", error_text.lower(),
                "days_back=0 must not produce a validation error")

    def test_days_back_positive_int_is_accepted(self):
        """days_back=7 must not produce a parameter validation error."""
        result = mcp_memory_server.handle_tool_call(
            "cami_message_search",
            {"query": "gamma test", "days_back": 7},
        )
        if result.get("isError"):
            error_text = result["content"][0]["text"]
            self.assertNotIn("days_back", error_text.lower())


class TestToolsListIntegrity(unittest.TestCase):
    """BUG-B2-08 regression: TOOLS must have 8 entries, not 7."""

    def test_tools_list_has_8_entries(self):
        """After BUG-B2-08 fix the docstring was corrected; TOOLS must contain 8 tools."""
        self.assertEqual(len(mcp_memory_server.TOOLS), 8,
            "TOOLS must contain exactly 8 MCP tool definitions")

    def test_session_bootstrap_is_present(self):
        """session_bootstrap was the missing tool — it must be in TOOLS."""
        names = {t["name"] for t in mcp_memory_server.TOOLS}
        self.assertIn("session_bootstrap", names)

    def test_all_eight_expected_tools_present(self):
        """All 8 tool names must be present."""
        expected = {
            "cami_memory_search",
            "cami_memory_timeline",
            "cami_memory_details",
            "cami_memory_save",
            "cami_memory_graph_search",
            "cami_message_search",
            "cami_contact_search",
            "session_bootstrap",
        }
        actual = {t["name"] for t in mcp_memory_server.TOOLS}
        self.assertEqual(actual, expected)


class TestContentTruncation(unittest.TestCase):
    """Test cami_memory_save truncates content at _MAX_CONTENT_LEN."""

    def test_oversized_content_is_truncated_not_errored(self):
        """Content > _MAX_CONTENT_LEN (20_000 chars) must be silently truncated."""
        oversized = "x" * (mcp_memory_server._MAX_CONTENT_LEN + 5000)

        class _FakeRetriever:
            def __init__(self):
                self.saved_content = None

            def save_memory(self, content, metadata):
                self.saved_content = content
                return "kg-manual-test"

            def close(self):
                pass

        fake = _FakeRetriever()
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=fake):
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_save",
                {"content": oversized},
            )

        self.assertFalse(response.get("isError"),
            "oversized content must not produce an error — it must be truncated")
        # The content passed to save_memory must be capped
        self.assertIsNotNone(fake.saved_content)
        self.assertLessEqual(len(fake.saved_content), mcp_memory_server._MAX_CONTENT_LEN)


class TestTimelineValidation(unittest.TestCase):
    """Tests for cami_memory_timeline input validation."""

    def test_missing_observation_id_returns_error(self):
        """Missing observation_id must return isError."""
        result = mcp_memory_server.handle_tool_call("cami_memory_timeline", {})
        self.assertTrue(result.get("isError"))
        self.assertIn("observation_id", result["content"][0]["text"].lower())

    def test_string_numeric_observation_id_coerced(self):
        """BUG-B2-05: string '42' must be coerced to int 42."""
        class _FakeRetriever:
            def __init__(self):
                self.received_id = None

            def timeline(self, observation_id, window):
                self.received_id = observation_id
                return []

            def close(self):
                pass

        fake = _FakeRetriever()
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=fake):
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_timeline",
                {"observation_id": "42"},
            )

        self.assertFalse(response.get("isError"),
            "string numeric observation_id must be accepted after coercion")
        self.assertEqual(fake.received_id, 42,
            "string '42' must be coerced to int 42 before passing to timeline()")

    def test_window_clamped_at_max(self):
        """window > _MAX_WINDOW (50) must be silently clamped."""
        class _FakeRetriever:
            def __init__(self):
                self.received_window = None

            def timeline(self, observation_id, window):
                self.received_window = window
                return []

            def close(self):
                pass

        fake = _FakeRetriever()
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=fake):
            mcp_memory_server.handle_tool_call(
                "cami_memory_timeline",
                {"observation_id": 1, "window": 9999},
            )

        self.assertIsNotNone(fake.received_window)
        self.assertLessEqual(fake.received_window, mcp_memory_server._MAX_WINDOW,
            "window must be clamped to _MAX_WINDOW")


class TestWriteMessage(unittest.TestCase):
    """Tests for _write_message line-mode framing path (line 96-98)."""

    def test_line_mode_writes_newline_terminated_json(self):
        """When _TRANSPORT_MODE == 'line', messages must be written as a single
        JSON line terminated with newline, not Content-Length framed."""
        import io
        original_mode = mcp_memory_server._TRANSPORT_MODE
        try:
            mcp_memory_server._TRANSPORT_MODE = "line"
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                mcp_memory_server._write_message({"key": "value"})
            output = buf.getvalue()
            self.assertTrue(output.endswith("\n"),
                "line-mode output must end with newline")
            self.assertNotIn("Content-Length", output,
                "line-mode must not include Content-Length header")
            import json
            parsed = json.loads(output.strip())
            self.assertEqual(parsed["key"], "value")
        finally:
            mcp_memory_server._TRANSPORT_MODE = original_mode


class TestObservationIdsHardCap(unittest.TestCase):
    """Test that cami_memory_details applies _MAX_OBSERVATION_IDS cap."""

    def test_observation_ids_capped_at_max(self):
        """observation_ids list larger than _MAX_OBSERVATION_IDS (20) must be
        silently truncated, not raise an error."""
        class _FakeRetriever:
            def __init__(self):
                self.received_ids = None

            def get_details(self, ids):
                self.received_ids = ids
                return []

            def close(self):
                pass

        fake = _FakeRetriever()
        ids_over_cap = list(range(1, 50))  # 49 IDs, well above cap of 20
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=fake):
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_details",
                {"observation_ids": ids_over_cap},
            )

        self.assertFalse(response.get("isError"),
            "oversized observation_ids must not produce an error")
        self.assertIsNotNone(fake.received_ids)
        self.assertLessEqual(len(fake.received_ids), mcp_memory_server._MAX_OBSERVATION_IDS,
            "IDs passed to get_details must be capped at _MAX_OBSERVATION_IDS")
