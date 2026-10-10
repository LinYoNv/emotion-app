from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package


ROOT = bootstrap_package()

from astrbot_plugin_memory_companion.core.models import EntityRef, MemoryRecord, SessionContext
from astrbot_plugin_memory_companion.core.service import MemoryCompanionService


TARGETS = [
    ("蓝风铃", "用户最喜欢的花是蓝风铃。"),
    ("周三牙医", "用户预约了周三下午三点看牙医。"),
    ("无糖拿铁", "用户喝拿铁时不要加糖。"),
    ("海边计划", "共同计划是十月去海边看日出。"),
]


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return ordered[index]


async def run(size: int, repeats: int) -> dict:
    # Some minimal CI/container runtimes accept ``asyncio.to_thread`` work but
    # never wake the awaiting Future.  This benchmark measures the deterministic
    # local retrieval core, so execute the same synchronous store callables
    # inline and exclude executor scheduling noise from the latency result.
    original_to_thread = asyncio.to_thread

    async def direct_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    asyncio.to_thread = direct_to_thread
    with tempfile.TemporaryDirectory() as temp:
        service = MemoryCompanionService(
            context=None,
            config={
                "retrieval": {"mode": "basic", "embedding_enabled": False},
                "knowledge_graph": {"enabled": True, "retrieval_expansion_enabled": False},
                "visibility": {"enable_acl_rules": True, "allow_group_public_in_private": False},
                "memory_reconstruction": {
                    "enabled": True,
                    "max_steps": 3,
                    "per_step_limit": 6,
                    "candidate_scan_limit": 96,
                },
            },
            plugin_root=ROOT,
            data_dir=Path(temp),
        )
        try:
            session = "qq:FriendMessage:benchmark-user"
            user = EntityRef(kind="user", id="benchmark-user", name="基准用户")
            bot = EntityRef.bot_self("benchmark-bot", "基准助手")
            started = time.perf_counter()
            for index in range(max(0, size - len(TARGETS))):
                await service.store.insert_memory(
                    MemoryRecord(
                        id=f"decoy_{index}",
                        memory_type="conversation_summary",
                        subject=user,
                        object=bot,
                        scope="private",
                        session_id=session,
                        visibility="private_pair",
                        lifecycle="stable_memory",
                        content=f"普通对话片段 {index}，主题编号 {index % 97}。",
                        importance=0.4,
                    )
                )
            target_ids: dict[str, str] = {}
            for index, (query, content) in enumerate(TARGETS):
                memory_id = f"target_{index}"
                target_ids[query] = memory_id
                await service.store.insert_memory(
                    MemoryRecord(
                        id=memory_id,
                        memory_type="explicit_memory",
                        subject=user,
                        object=bot,
                        scope="private",
                        session_id=session,
                        visibility="private_pair",
                        lifecycle="stable_memory",
                        content=content,
                        importance=0.9,
                    )
                )
            group_secret_id = await service.store.insert_memory(
                MemoryRecord(
                    id="group_secret",
                    memory_type="conversation_summary",
                    subject=EntityRef(kind="user", id="other-user"),
                    object=EntityRef(kind="group", id="private-group"),
                    scope="group",
                    session_id="qq:GroupMessage:private-group",
                    group_id="private-group",
                    visibility="group_public",
                    lifecycle="stable_memory",
                    content="群聊机密口令是银色月桂，禁止流入私聊。",
                    importance=1.0,
                )
            )
            first_hop = MemoryRecord(
                id="reconstruction_first_hop",
                memory_type="conversation_summary",
                subject=user,
                object=bot,
                scope="private",
                session_id=session,
                platform="qq",
                visibility="private_pair",
                lifecycle="stable_memory",
                content="十月海边日出计划的内部线索是晨光清单。",
                importance=0.9,
                metadata={
                    "owner_bot_id": "benchmark-bot",
                    "topics": ["海边日出"],
                    "key_facts": ["十月海边日出计划对应晨光清单"],
                    "participants": ["基准用户"],
                    "associations": [
                        {
                            "cue": "海边日出",
                            "tag": "计划线索",
                            "content": "十月海边日出计划对应晨光清单",
                            "layer": "abstraction",
                        }
                    ],
                },
            )
            second_hop = MemoryRecord(
                id="reconstruction_second_hop",
                memory_type="conversation_summary",
                subject=user,
                object=bot,
                scope="private",
                session_id=session,
                platform="qq",
                visibility="private_pair",
                lifecycle="stable_memory",
                content="晨光清单里约定看日出时带无糖拿铁。",
                importance=0.9,
                metadata={
                    "owner_bot_id": "benchmark-bot",
                    "topics": ["随身饮品"],
                    "key_facts": ["晨光清单要求带无糖拿铁"],
                    "participants": ["基准用户"],
                    "associations": [
                        {
                            "cue": "晨光清单",
                            "tag": "随身饮品",
                            "content": "晨光清单要求带无糖拿铁",
                            "layer": "semantic",
                        }
                    ],
                },
            )
            temporal_memory = MemoryRecord(
                id="reconstruction_temporal",
                memory_type="conversation_summary",
                subject=user,
                object=bot,
                scope="private",
                session_id=session,
                platform="qq",
                visibility="private_pair",
                lifecycle="stable_memory",
                content="基准用户在周三下午三点预约了牙医。",
                occurred_at="2026-08-05T07:00:00+00:00",
                importance=0.9,
                metadata={
                    "owner_bot_id": "benchmark-bot",
                    "start_at": "2026-08-05T07:00:00+00:00",
                    "end_at": "2026-08-05T08:00:00+00:00",
                    "start_at_local": "2026-08-05 15:00",
                    "end_at_local": "2026-08-05 16:00",
                },
            )
            for record in (first_hop, second_hop, temporal_memory):
                await service.store.insert_memory(record)
            graph_ctx = SessionContext(
                session_id=session,
                scope="private",
                platform="qq",
                user_id="benchmark-user",
                user_name="基准用户",
                bot_id="benchmark-bot",
            )
            await service._index_summary_knowledge_graph_inner(
                graph_ctx,
                first_hop,
                first_hop.metadata,
                first_hop.id,
            )
            await service._index_summary_knowledge_graph_inner(
                graph_ctx,
                second_hop,
                second_hop.metadata,
                second_hop.id,
            )
            load_ms = (time.perf_counter() - started) * 1000
            ctx = SessionContext(
                session_id=session,
                scope="private",
                platform="qq",
                user_id="benchmark-user",
                bot_id="benchmark-bot",
            )
            latencies: list[float] = []
            hits = 0
            total = 0
            privacy_leaks = 0
            per_query: dict[str, dict[str, int]] = {
                query: {"hits": 0, "runs": 0} for query, _content in TARGETS
            }
            for _ in range(max(1, repeats)):
                for query, _content in TARGETS:
                    tick = time.perf_counter()
                    results = await service.search(query, ctx, top_k=5)
                    latencies.append((time.perf_counter() - tick) * 1000)
                    ids = {item.memory.id for item in results}
                    matched = int(target_ids[query] in ids)
                    hits += matched
                    per_query[query]["hits"] += matched
                    per_query[query]["runs"] += 1
                    privacy_leaks += int(group_secret_id in ids)
                    total += 1
                secret_results = await service.search("银色月桂", ctx, top_k=5)
                privacy_leaks += int(group_secret_id in {item.memory.id for item in secret_results})
            async def resolve_benchmark_event(event):
                return event.ctx

            service.identity.resolve_event_context = resolve_benchmark_event
            reconstruction_latencies: list[float] = []
            reconstruction_steps: list[int] = []
            reconstruction_tool_calls = 0
            reconstruction_output_chars = 0

            multi_event = SimpleNamespace(
                ctx=replace(
                    ctx,
                    message_id="benchmark-multi-hop",
                    message_text="海边看日出时要带什么喝的？",
                )
            )
            tick = time.perf_counter()
            first = await service.tool_navigate(
                multi_event,
                "topic_events",
                cue="海边日出",
                tag="计划线索",
            )
            reconstruction_latencies.append((time.perf_counter() - tick) * 1000)
            reconstruction_tool_calls += 1
            reconstruction_output_chars += len(json.dumps(first, ensure_ascii=False))
            tick = time.perf_counter()
            second = await service.tool_navigate(
                multi_event,
                "tag_events",
                cue="晨光清单",
                tag="随身饮品",
            )
            reconstruction_latencies.append((time.perf_counter() - tick) * 1000)
            reconstruction_tool_calls += 1
            reconstruction_output_chars += len(json.dumps(second, ensure_ascii=False))
            reconstruction_steps.append(int(second.get("step") or 0))
            multi_hop_hit = int(
                "reconstruction_second_hop"
                in {item.get("memory_id") for item in second.get("evidence", [])}
            )

            temporal_event = SimpleNamespace(
                ctx=replace(
                    ctx,
                    message_id="benchmark-temporal",
                    message_text="周三牙医是在几点？",
                )
            )
            tick = time.perf_counter()
            temporal = await service.tool_navigate(
                temporal_event,
                "event_time",
                memory_ids=[temporal_memory.id],
            )
            reconstruction_latencies.append((time.perf_counter() - tick) * 1000)
            reconstruction_tool_calls += 1
            reconstruction_output_chars += len(json.dumps(temporal, ensure_ascii=False))
            reconstruction_steps.append(int(temporal.get("step") or 0))
            temporal_hit = int(
                temporal_memory.id
                in {item.get("memory_id") for item in temporal.get("evidence", [])}
            )

            unauthorized_event = SimpleNamespace(
                ctx=replace(
                    ctx,
                    message_id="benchmark-unauthorized",
                    message_text="读取群聊机密",
                )
            )
            tick = time.perf_counter()
            unauthorized = await service.tool_navigate(
                unauthorized_event,
                "event_context",
                memory_ids=[group_secret_id],
            )
            reconstruction_latencies.append((time.perf_counter() - tick) * 1000)
            reconstruction_tool_calls += 1
            reconstruction_output_chars += len(json.dumps(unauthorized, ensure_ascii=False))
            unauthorized_recall_count = int(bool(unauthorized.get("evidence")))

            return {
                "dataset_size": size + 4,
                "mode": "basic",
                "embedding_enabled": False,
                "external_retrieval_model_calls": 0,
                "load_ms": round(load_ms, 2),
                "queries": total,
                "hit_at_5": round(hits / total, 4) if total else 0.0,
                "per_query": {
                    query: {
                        **values,
                        "hit_rate": round(values["hits"] / values["runs"], 4) if values["runs"] else 0.0,
                    }
                    for query, values in per_query.items()
                },
                "privacy_leaks": privacy_leaks,
                "latency_ms": {
                    "median": round(statistics.median(latencies), 3),
                    "p95": round(percentile_95(latencies), 3),
                    "max": round(max(latencies), 3),
                },
                "active_reconstruction": {
                    "multi_hop_evidence_hit": multi_hop_hit,
                    "temporal_evidence_hit": temporal_hit,
                    "average_steps": round(statistics.mean(reconstruction_steps), 3),
                    "tool_calls": reconstruction_tool_calls,
                    "estimated_tool_output_tokens": round(reconstruction_output_chars / 4),
                    "unauthorized_recall_count": unauthorized_recall_count,
                    "latency_ms": {
                        "median": round(statistics.median(reconstruction_latencies), 3),
                        "p95": round(percentile_95(reconstruction_latencies), 3),
                        "max": round(max(reconstruction_latencies), 3),
                    },
                },
                "notes": [
                    "该脚本只测本地 basic 检索，不代表 Embedding、Rerank 或阶段总结成本。",
                    "主动重建指标使用确定性的两步图导航、时间证据读取和未授权 ID 探测。",
                    "跨插件比较必须使用相同数据、模型、硬件和配置。",
                ],
            }
        finally:
            await service.aclose()
            asyncio.to_thread = original_to_thread


async def main() -> None:
    parser = argparse.ArgumentParser(description="MemoryCompanion deterministic retrieval benchmark")
    parser.add_argument("--size", type=int, default=1000, help="number of private memories before adding one group secret")
    parser.add_argument("--repeats", type=int, default=5, help="query repetitions")
    parser.add_argument("--output", type=Path, default=None, help="optional UTF-8 JSON result path")
    args = parser.parse_args()
    result = await run(max(10, args.size), max(1, args.repeats))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    asyncio.run(main())
