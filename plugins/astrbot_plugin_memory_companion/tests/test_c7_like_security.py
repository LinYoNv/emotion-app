from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from core.models import MemoryRecord
from core.store import MemoryStore


def run(coro):
    return asyncio.run(coro)


class LikeSecurityTests(unittest.TestCase):
    def test_like_pattern_escapes_wildcards_and_backslashes(self):
        self.assertEqual(r"%100\%\_ready\\now%", MemoryStore._like_pattern("100%_ready\\now"))

    def test_memory_query_treats_percent_and_underscore_as_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            store.initialize()
            try:
                run(store.insert_memory(MemoryRecord(
                    id="literal",
                    scope="private",
                    session_id="private:user-1",
                    content="needle%_literal",
                )))
                run(store.insert_memory(MemoryRecord(
                    id="wildcard",
                    scope="private",
                    session_id="private:user-1",
                    content="needleXliteral",
                )))
                rows = run(store.list_memories(query="needle%_literal", limit=10))
                self.assertEqual(["literal"], [row.id for row in rows])
            finally:
                store.close()

    def test_knowledge_term_search_treats_percent_and_underscore_as_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            store.initialize()
            try:
                run(store.upsert_knowledge_node(node_type="topic", label="alpha%_literal"))
                run(store.upsert_knowledge_node(node_type="topic", label="alphaXliteral"))
                labels = run(store.related_knowledge_terms(["alpha%_literal"], limit=10))
                self.assertEqual(["alpha%_literal"], labels)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
