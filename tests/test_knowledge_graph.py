import tempfile
import unittest
from pathlib import Path

from src.knowledge_graph import KnowledgeGraph


class KnowledgeGraphTests(unittest.TestCase):
    def test_relationships_persist_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "knowledge.db"

            graph = KnowledgeGraph(db_path=db_path)
            graph.add_relationship(
                "Alice Smith",
                "works_on",
                "Project Atlas",
                context="Initial discovery",
                strength=0.75,
                origin="tests",
            )

            outgoing = graph.get_relationships("Alice Smith", rel_type="works_on")

            self.assertEqual(len(outgoing), 1)
            self.assertEqual(outgoing[0]["target"], "project_atlas")
            self.assertEqual(outgoing[0]["context"], "Initial discovery")
            self.assertEqual(outgoing[0]["origin"], "tests")
            self.assertTrue(graph.entity_exists("Project Atlas"))

            reloaded = KnowledgeGraph(db_path=db_path)
            self.assertEqual(
                reloaded.find_path("Alice Smith", "Project Atlas"),
                ["alice_smith", "project_atlas"],
            )
            self.assertEqual(
                reloaded.get_entity("Alice Smith")["display_name"],
                "Alice Smith",
            )
