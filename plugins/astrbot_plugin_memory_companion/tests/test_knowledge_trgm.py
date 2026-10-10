from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package

ROOT = bootstrap_package()

from astrbot_plugin_memory_companion.core.models import clean_text
from astrbot_plugin_memory_companion.core.store import MemoryStore


def _baseline_related_labels(
    store: MemoryStore,
    terms: list[str],
    *,
    scope: str = "",
    session_id: str = "",
    group_id: str = "",
    limit: int = 12,
) -> list[str]:
    """Replicate the pre-trigram LIKE-only implementation as a reference."""
    cleaned_terms = [clean_text(term, 80).lower() for term in terms if clean_text(term)]
    if not cleaned_terms:
        return []
    params: list = []
    scope = clean_text(scope, 40)
    session_id = clean_text(session_id, 200)
    group_id = clean_text(group_id, 120)
    scope_filter = ""
    if scope:
        scope_filter += " AND (n.scope='' OR n.scope=?)"
        params.append(scope)
    if session_id:
        scope_filter += " AND (n.session_id='' OR n.session_id=?)"
        params.append(session_id)
    if group_id:
        scope_filter += " AND (n.group_id='' OR n.group_id=?)"
        params.append(group_id)
    like_sql = " OR ".join(["lower(n.label) LIKE ? ESCAPE '\\'" for _ in cleaned_terms])
    like_params = [store._like_pattern(term) for term in cleaned_terms]
    with store._lock:
        matched = store._conn.execute(
            f"""
            SELECT n.id, n.label
            FROM knowledge_nodes n
            WHERE ({like_sql}) {scope_filter}
            ORDER BY n.updated_at DESC
            LIMIT ?
            """,
            like_params + params + [max(1, int(limit))],
        ).fetchall()
        matched_ids = [str(row["id"]) for row in matched]
        labels = [clean_text(row["label"], 80) for row in matched]
        if not matched_ids:
            return labels[:limit]
        placeholders = ",".join("?" for _ in matched_ids)
        related = store._conn.execute(
            f"""
            SELECT DISTINCT n.label
            FROM knowledge_edges e
            JOIN knowledge_nodes n
              ON n.id = CASE
                WHEN e.source_node_id IN ({placeholders}) THEN e.target_node_id
                ELSE e.source_node_id
              END
            WHERE e.source_node_id IN ({placeholders})
               OR e.target_node_id IN ({placeholders})
            ORDER BY e.updated_at DESC
            LIMIT ?
            """,
            matched_ids + matched_ids + matched_ids + [max(1, int(limit))],
        ).fetchall()
    for row in related:
        label = clean_text(row["label"], 80)
        if label and label.lower() not in cleaned_terms and label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels[:limit]


class KnowledgeTrigramTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    async def _seed(self, store: MemoryStore) -> None:
        await store.upsert_knowledge_node(node_type="topic", label="蓝风铃养护指南")
        await store.upsert_knowledge_node(node_type="topic", label="拿铁不加糖")
        await store.upsert_knowledge_node(node_type="topic", label="Latte Art Basics")
        await store.upsert_knowledge_node(node_type="topic", label="alpha%_literal")
        await store.upsert_knowledge_node(node_type="topic", label="alphaXliteral")
        await store.upsert_knowledge_node(node_type="topic", label="ab")
        await store.upsert_knowledge_node(node_type="topic", label="短")
        await store.upsert_knowledge_node(node_type="topic", label="完全不同的标签")
        await store.upsert_knowledge_node(
            node_type="topic",
            label="私聊限定蓝风铃",
            scope="private",
            session_id="s1",
        )
        await store.upsert_knowledge_edge(
            source_node_id=await store.upsert_knowledge_node(node_type="topic", label="蓝风铃养护指南"),
            target_node_id=await store.upsert_knowledge_node(node_type="topic", label="关联花期"),
            relation_type="related",
        )

    async def test_trigram_matches_baseline_for_mixed_terms(self) -> None:
        store = self.make_store()
        await self._seed(store)
        self.assertTrue(store._knowledge_trgm_enabled)

        term_sets = [
            ["蓝风铃"],
            ["latte"],
            ["alpha%_literal"],
            ["短"],
            ["蓝风铃", "latte", "alpha%_literal", "短", "不存在的词"],
            ["拿铁不加糖"],
        ]
        for terms in term_sets:
            expected = _baseline_related_labels(store, terms, limit=12)
            actual = await store.related_knowledge_terms(terms, limit=12)
            self.assertEqual(expected, actual, f"terms={terms}")

    async def test_trigram_preserves_scope_filters(self) -> None:
        store = self.make_store()
        await self._seed(store)
        kwargs = {"scope": "group", "session_id": "g-session", "group_id": "g1"}
        for terms in (["蓝风铃"], ["私聊限定"]):
            expected = _baseline_related_labels(store, terms, limit=12, **kwargs)
            actual = await store.related_knowledge_terms(terms, limit=12, **kwargs)
            self.assertEqual(expected, actual, f"terms={terms}")
        self.assertNotIn("私聊限定蓝风铃", await store.related_knowledge_terms(["私聊限定"], limit=12, **kwargs))

    async def test_trigram_disabled_falls_back_to_identical_results(self) -> None:
        store = self.make_store()
        await self._seed(store)
        store._knowledge_trgm_enabled = False
        terms = ["蓝风铃", "latte", "alpha%_literal"]
        expected = _baseline_related_labels(store, terms, limit=12)
        actual = await store.related_knowledge_terms(terms, limit=12)
        self.assertEqual(expected, actual)

    async def test_triggers_keep_trigram_index_in_sync(self) -> None:
        store = self.make_store()
        await store.upsert_knowledge_node(node_type="topic", label="临时锚点风信子")
        terms = ["风信子"]
        baseline = _baseline_related_labels(store, terms, limit=12)
        self.assertEqual(baseline, await store.related_knowledge_terms(terms, limit=12))

        node_id = store._conn.execute(
            "SELECT id FROM knowledge_nodes WHERE label='临时锚点风信子'"
        ).fetchone()["id"]
        store._conn.execute("UPDATE knowledge_nodes SET label='改名后的菖蒲' WHERE id=?", (node_id,))
        store._conn.commit()
        self.assertEqual([], await store.related_knowledge_terms(["风信子"], limit=12))
        self.assertEqual(
            _baseline_related_labels(store, ["菖蒲"], limit=12),
            await store.related_knowledge_terms(["菖蒲"], limit=12),
        )

        store._conn.execute("DELETE FROM knowledge_nodes WHERE id=?", (node_id,))
        store._conn.commit()
        self.assertEqual([], await store.related_knowledge_terms(["菖蒲"], limit=12))

    async def test_count_mismatch_triggers_rebuild_on_initialize(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = Path(temp_dir.name) / "memory.db"
        store = MemoryStore(db_path)
        store.initialize()
        try:
            await store.upsert_knowledge_node(node_type="topic", label="重建锚点郁金香")
            store._conn.execute("DELETE FROM knowledge_label_trgm")
            store._conn.commit()
        finally:
            store.close()

        reopened = MemoryStore(db_path)
        self.addCleanup(reopened.close)
        reopened.initialize()
        self.assertTrue(reopened._knowledge_trgm_enabled)
        self.assertEqual(
            _baseline_related_labels(reopened, ["郁金香"], limit=12),
            await reopened.related_knowledge_terms(["郁金香"], limit=12),
        )


if __name__ == "__main__":
    unittest.main()
