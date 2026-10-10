from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.migration_livingmemory import LivingMemoryMigrator
from core.store import MemoryStore


class LivingMemoryMigrationTests(unittest.TestCase):
    def test_import_rebuilds_fts_and_recovers_scope_from_metadata(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "livingmemory.db"
                connection = sqlite3.connect(source)
                connection.execute(
                    "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
                )
                connection.execute(
                    "INSERT INTO documents(text, metadata) VALUES (?, ?)",
                    ("关于紫色杯子的长期记忆", json.dumps({"scope": "private", "user_id": "u1"})),
                )
                connection.commit()
                connection.close()

                store = MemoryStore(root / "memory.db")
                store.initialize()
                try:
                    result = await LivingMemoryMigrator(store, root, root).import_data(
                        configured_path=str(source)
                    )
                    self.assertTrue(result["fts_enabled"])
                    self.assertEqual(1, result["fts_rebuilt"])
                    matches = await store.list_fts_candidate_memories(["紫色", "杯子"])
                    self.assertEqual(1, len(matches))
                    self.assertEqual("private", matches[0].scope)
                    self.assertEqual("private_pair", matches[0].visibility)
                    self.assertEqual("u1", matches[0].object.id)
                finally:
                    store.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
