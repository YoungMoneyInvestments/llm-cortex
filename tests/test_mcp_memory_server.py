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
