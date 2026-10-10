from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package


ROOT = bootstrap_package()

from astrbot_plugin_memory_companion.core.store import MemoryStore


class KnowledgePathStoreTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    async def add_path(
        self,
        store: MemoryStore,
        *,
        source_type: str,
        source_label: str,
        target_type: str,
        target_label: str,
        memory_id: str,
        tag: str = "",
        confidence: float = 0.7,
        evidence: str = "候选证据",
    ) -> str:
        source_node = await store.upsert_knowledge_node(
            node_type=source_type,
            label=source_label,
            metadata={"side": "source"},
        )
        target_node = await store.upsert_knowledge_node(
            node_type=target_type,
            label=target_label,
            metadata={"side": "target"},
        )
        return await store.upsert_knowledge_edge(
            source_node_id=source_node,
            target_node_id=target_node,
            relation_type="routes_to",
            source_memory_id=memory_id,
            evidence=evidence,
            confidence=confidence,
            metadata={"associative_tag": tag, "content_layer": "semantic"},
        )

    async def test_query_returns_one_hop_path_with_parsed_metadata(self) -> None:
        store = self.make_store()
        edge_id = await self.add_path(
            store,
            source_type="cue",
            source_label="小王",
            target_type="memory",
            target_label="小王喜欢无糖拿铁",
            memory_id="memory-coffee",
            tag="饮食偏好",
            evidence="小王说自己只喝无糖拿铁",
        )

        paths = await store.query_knowledge_paths(["小王"])

        self.assertEqual(1, len(paths))
        path = paths[0]
        self.assertEqual(edge_id, path["edge_id"])
        self.assertEqual("memory-coffee", path["source_memory_id"])
        self.assertEqual(("cue", "小王"), (path["source_type"], path["source_label"]))
        self.assertEqual(
            ("memory", "小王喜欢无糖拿铁"),
            (path["target_type"], path["target_label"]),
        )
        self.assertEqual("routes_to", path["relation_type"])
        self.assertEqual("小王说自己只喝无糖拿铁", path["evidence"])
        self.assertEqual({"side": "source"}, path["source_metadata"])
        self.assertEqual({"side": "target"}, path["target_metadata"])
        self.assertEqual(
            {"associative_tag": "饮食偏好", "content_layer": "semantic"},
            path["edge_metadata"],
        )

    async def test_filters_can_start_from_tag_type_or_memory_id(self) -> None:
        store = self.make_store()
        await self.add_path(
            store,
            source_type="cue",
            source_label="午饭",
            target_type="memory",
            target_label="在面馆吃了牛肉面",
            memory_id="memory-lunch",
            tag="饮食经历",
        )
        await self.add_path(
            store,
            source_type="person",
            source_label="小李",
            target_type="topic",
            target_label="旅行",
            memory_id="memory-travel",
            tag="出行计划",
        )

        by_tag = await store.query_knowledge_paths([], tag="饮食")
        by_type = await store.query_knowledge_paths([], node_type="topic")
        by_memory = await store.query_knowledge_paths([], memory_ids=["memory-lunch"])
        combined = await store.query_knowledge_paths(
            ["小李"],
            tag="出行",
            node_type="topic",
            memory_ids=["memory-travel", "not-present"],
        )

        self.assertEqual(["memory-lunch"], [item["source_memory_id"] for item in by_tag])
        self.assertEqual(["memory-travel"], [item["source_memory_id"] for item in by_type])
        self.assertEqual(["memory-lunch"], [item["source_memory_id"] for item in by_memory])
        self.assertEqual(["memory-travel"], [item["source_memory_id"] for item in combined])

    async def test_terms_are_literal_and_empty_filters_do_not_scan(self) -> None:
        store = self.make_store()
        await self.add_path(
            store,
            source_type="cue",
            source_label="100%_确定",
            target_type="memory",
            target_label="包含特殊符号",
            memory_id="memory-literal",
        )
        await self.add_path(
            store,
            source_type="cue",
            source_label="普通线索",
            target_type="memory",
            target_label="不应被通配符命中",
            memory_id="memory-other",
        )

        literal = await store.query_knowledge_paths(["%_"])

        self.assertEqual(["memory-literal"], [item["source_memory_id"] for item in literal])
        self.assertEqual([], await store.query_knowledge_paths([]))
        self.assertEqual([], await store.query_knowledge_paths([], memory_ids=[]))

    async def test_results_are_ranked_and_hard_limited(self) -> None:
        store = self.make_store()
        for index in range(205):
            await self.add_path(
                store,
                source_type="cue",
                source_label="共同线索",
                target_type="memory",
                target_label=f"记忆 {index}",
                memory_id=f"memory-{index}",
                confidence=index / 205,
            )

        paths = await store.query_knowledge_paths(["共同线索"], limit=10_000)

        self.assertEqual(200, len(paths))
        confidences = [float(item["confidence"]) for item in paths]
        self.assertEqual(sorted(confidences, reverse=True), confidences)


if __name__ == "__main__":
    unittest.main()
