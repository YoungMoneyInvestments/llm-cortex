import unittest
from unittest.mock import patch

from src import mcp_memory_server


class FakeRetriever:
    def search(self, **_kwargs):
        return [
            {
                "obs_id": 7,
                "tool": "Write",
                "summary": "Saved alpha memory",
                "graph_context": {"entities_found": ["alpha"]},
            }
        ]

    def _enrich_with_graph_context(self, results):
        return results

    def close(self):
        return None


class MCPPMemoryServerTests(unittest.TestCase):
    def test_search_tool_formats_compact_text_response(self) -> None:
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=FakeRetriever()):
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_search",
                {"query": "alpha", "limit": 5},
            )

        text = response["content"][0]["text"]

        self.assertIn("Found 1 results for 'alpha':", text)
        self.assertIn("#7 [Write]: Saved alpha memory", text)
        self.assertIn("[KG: alpha]", text)

    # ── Validation tests — no mocks required (pure handler logic) ────────────

    def test_search_rejects_missing_query(self) -> None:
        response = mcp_memory_server.handle_tool_call("cami_memory_search", {})
        self.assertTrue(response.get("isError"))

    def test_search_rejects_empty_query(self) -> None:
        response = mcp_memory_server.handle_tool_call("cami_memory_search", {"query": "  "})
        self.assertTrue(response.get("isError"))

    def test_search_string_limit_coerced(self) -> None:
        """LLMs occasionally send limit as a string — should be accepted."""
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=FakeRetriever()):
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_search", {"query": "test", "limit": "3"}
            )
        self.assertFalse(response.get("isError"))

    def test_search_limit_capped_at_max(self) -> None:
        """Huge limit must be silently capped, not cause an error."""
        with patch.object(mcp_memory_server, "MemoryRetriever", return_value=FakeRetriever()):
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_search", {"query": "test", "limit": 999999}
            )
        self.assertFalse(response.get("isError"))

    def test_graph_depth_3_rejected_without_mock(self) -> None:
        """graph_depth=3 should be rejected by handler validation alone."""
        response = mcp_memory_server.handle_tool_call(
            "cami_memory_graph_search", {"query": "test", "graph_depth": 3}
        )
        self.assertTrue(response.get("isError"))
        self.assertIn("graph_depth", response["content"][0]["text"])

    def test_graph_depth_0_rejected(self) -> None:
        response = mcp_memory_server.handle_tool_call(
            "cami_memory_graph_search", {"query": "test", "graph_depth": 0}
        )
        self.assertTrue(response.get("isError"))

    def test_observation_ids_empty_rejected_without_mock(self) -> None:
        """Empty observation_ids must be rejected by handler validation."""
        response = mcp_memory_server.handle_tool_call(
            "cami_memory_details", {"observation_ids": []}
        )
        self.assertTrue(response.get("isError"))
        self.assertIn("observation_ids", response["content"][0]["text"])

    def test_observation_ids_string_ints_coerced(self) -> None:
        """observation_ids may arrive as strings from some LLMs."""
        with patch.object(mcp_memory_server, "MemoryRetriever") as MockR:
            mock_instance = MockR.return_value
            mock_instance.get_details.return_value = []
            mock_instance.close.return_value = None
            # Returns empty but should not error on string ids
            response = mcp_memory_server.handle_tool_call(
                "cami_memory_details", {"observation_ids": ["7", "8"]}
            )
            mock_instance.get_details.assert_called_once_with([7, 8])

    def test_save_rejects_empty_content(self) -> None:
        response = mcp_memory_server.handle_tool_call(
            "cami_memory_save", {"content": "   "}
        )
        self.assertTrue(response.get("isError"))

    def test_timeline_rejects_string_observation_id_non_numeric(self) -> None:
        response = mcp_memory_server.handle_tool_call(
            "cami_memory_timeline", {"observation_id": "not-an-int"}
        )
        self.assertTrue(response.get("isError"))

    def test_graph_schema_has_enum_for_depth(self) -> None:
        """Tool schema for cami_memory_graph_search must declare enum on graph_depth."""
        tool = next(
            t for t in mcp_memory_server.TOOLS if t["name"] == "cami_memory_graph_search"
        )
        depth_schema = tool["inputSchema"]["properties"]["graph_depth"]
        self.assertEqual(sorted(depth_schema["enum"]), [1, 2])
