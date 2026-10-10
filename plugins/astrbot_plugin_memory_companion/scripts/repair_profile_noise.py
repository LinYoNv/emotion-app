from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import re
import secrets
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_models = importlib.import_module("core.models")
_profile_quality = importlib.import_module("core.profile_quality")
_store = importlib.import_module("core.store")
MemoryRecord = _models.MemoryRecord
clean_text = _models.clean_text
normalize_profile_value = _profile_quality.normalize_profile_value
profile_quality_decision = _profile_quality.profile_quality_decision
profile_rejection_reason = _profile_quality.profile_rejection_reason
MemoryStore = _store.MemoryStore

APPLY_CONFIRMATION = "APPLY_PROFILE_REPAIR"
ROLLBACK_CONFIRMATION = "ROLLBACK_PROFILE_REPAIR"
DEFAULT_RULE_VERSION = "profile_noise_repair_v1"

_SINGLE_VALUE_DIMENSIONS = MemoryStore.PROFILE_SINGLE_VALUE_DIMENSIONS
_LEGACY_DIMENSIONS = {
    "称呼": "preferred_address",
    "生日": "birthday",
    "职业": "occupation",
    "专业/学业": "education",
    "星座/血型": "zodiac_or_blood_type",
    "习惯": "habit",
    "喜欢": "preference",
    "最爱": "preference",
    "讨厌": "preference",
    "不喜欢": "preference",
    "过敏/禁忌": "avoidance",
    "边界": "boundary",
    "雷区": "boundary",
}
_NOISE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "third_party_statement",
        re.compile(
            r"(?:(?:老板娘|老板|同事|别人|有人|他|她)"
            r"(?:说|提到|提及|描述|介绍|转述|叫|喊|总叫|总喊)|听说|据说)"
        ),
    ),
    (
        "action_context",
        re.compile(r"(?:叫我|喊我).{0,8}(?:去|来|跟车|跟着|上班|工作|做|干活)"),
    ),
    (
        "negated_request",
        re.compile(r"(?:不许|别|不要|不能|怎么还|老是|总是).{0,8}(?:叫我|喊我|称呼我)"),
    ),
    (
        "one_off_request",
        re.compile(r"(?:叫我|喊我|称呼我).{0,12}(?:几声|一次|这次|今天)"),
    ),
    (
        "temporary_preference",
        re.compile(
            r"我\s*(?:很|最|真的)?\s*(?:喜欢|讨厌|不喜欢)\s*"
            r"(?:今天|今日|这次|这回|本次|此次|刚才|当前|现在)(?:的)?"
            r"(?:午饭|晚饭|早饭|早餐|互动|对话|聊天|会议|安排|体验|服务|回复|回答)"
        ),
    ),
)


def _validated_operation_id(value: Any) -> str:
    operation_id = MemoryStore._valid_profile_operation_id(value)
    if not operation_id:
        raise ValueError("invalid profile repair operation id")
    return operation_id


def _record_state(record: MemoryRecord) -> str:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    state = clean_text(
        metadata.get("profile_state")
        or metadata.get("profile_status")
        or metadata.get("status"),
        32,
    ).lower()
    if state:
        return state
    if record.lifecycle == "archived":
        return "superseded" if record.supersedes_id else "rejected"
    if record.review_status == "pending":
        return "candidate"
    return "active"


def _legacy_profile_fields(record: MemoryRecord) -> tuple[str, str, str]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    dimension = clean_text(metadata.get("profile_dimension"), 80).lower()
    value = clean_text(metadata.get("profile_value"), 240)
    normalized = normalize_profile_value(metadata.get("normalized_value"))
    if dimension and value and normalized:
        return dimension, value, normalized

    tags = [clean_text(tag, 80) for tag in (record.tags or [])]
    label = next((tag for tag in tags if tag in _LEGACY_DIMENSIONS), "")
    if label:
        dimension = _LEGACY_DIMENSIONS[label]
        marker = f"{label} "
        content = clean_text(record.content, 4000)
        value = clean_text(
            content.split(marker, 1)[1] if marker in content else "", 240
        )
    return dimension, value, normalize_profile_value(value)


def _metadata_patch(record: MemoryRecord) -> dict[str, Any]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    dimension, value, normalized = _legacy_profile_fields(record)
    patch: dict[str, Any] = {}
    if dimension and not clean_text(metadata.get("profile_dimension"), 80):
        patch["profile_dimension"] = dimension
    if value and not clean_text(metadata.get("profile_value"), 240):
        patch["profile_value"] = value
    if normalized and not clean_text(metadata.get("normalized_value"), 240):
        patch["normalized_value"] = normalized
    if not clean_text(metadata.get("profile_polarity"), 40):
        labels = {clean_text(tag, 80) for tag in (record.tags or [])}
        polarity = ""
        if labels & {"喜欢", "最爱"}:
            polarity = "like"
        elif labels & {"讨厌", "不喜欢"}:
            polarity = "dislike"
        elif labels & {"过敏/禁忌", "边界", "雷区"}:
            polarity = "avoid"
        elif "称呼" in labels:
            polarity = "address"
        elif "习惯" in labels:
            polarity = "habit"
        if polarity:
            patch["profile_polarity"] = polarity
    if dimension and not clean_text(metadata.get("profile_cardinality"), 20):
        patch["profile_cardinality"] = (
            "single" if dimension in _SINGLE_VALUE_DIMENSIONS else "multi"
        )
    return patch


def _noise_reason(record: MemoryRecord) -> str:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    explicit_reason = clean_text(
        metadata.get("rejection_reason")
        or metadata.get("profile_rejection_reason")
        or metadata.get("quality_rejection_reason"),
        120,
    )
    if explicit_reason:
        return explicit_reason
    if metadata.get("quality_gate_passed") is False:
        return "profile_quality_rejected"
    source = " ".join(
        [
            clean_text(record.evidence, 2000),
            clean_text(metadata.get("profile_value"), 240),
            clean_text(record.content, 2000),
        ]
    )
    for candidate_text in (record.evidence, record.content):
        rejected_reason = profile_rejection_reason(candidate_text)
        if rejected_reason:
            return rejected_reason
    for reason, pattern in _NOISE_PATTERNS:
        if pattern.search(source):
            return reason
    return ""


def _domain_key(record: MemoryRecord) -> tuple[str, ...]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    return (
        clean_text(record.platform, 80).lower(),
        clean_text(record.subject.kind, 40).lower(),
        clean_text(record.subject.id, 120),
        clean_text(record.object.kind, 40).lower(),
        clean_text(record.object.id, 120),
        clean_text(record.scope, 40).lower(),
        clean_text(record.group_id, 120),
        clean_text(record.visibility, 40).lower(),
        clean_text(metadata.get("owner_bot_id"), 120),
    )


def _evidence_refs(record: MemoryRecord) -> list[str]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    values: list[Any] = []
    for key in ("profile_evidence_refs", "source_memory_ids", "evidence_refs"):
        if isinstance(metadata.get(key), list):
            values.extend(metadata[key])
    source_memory_id = clean_text(metadata.get("source_memory_id"), 160)
    if source_memory_id:
        values.append(source_memory_id)
    elif not values and record.message_id:
        values.append(record.message_id)
    return list(
        dict.fromkeys(
            clean_text(value, 160) for value in values if clean_text(value, 160)
        )
    )


def _quality_score(record: MemoryRecord) -> float:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    try:
        score = float(metadata.get("extraction_quality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0


def _record_rank(record: MemoryRecord) -> tuple[int, float, str, str]:
    return (
        1 if _record_state(record) == "active" else 0,
        _quality_score(record),
        clean_text(record.occurred_at or record.created_at, 80),
        record.id,
    )


def _single_value_rank(record: MemoryRecord) -> tuple[int, str, float, str]:
    return (
        1 if _record_state(record) == "active" else 0,
        clean_text(record.occurred_at or record.created_at, 80),
        _quality_score(record),
        record.id,
    )


def _entry(record: MemoryRecord, *, action: str, reason: str) -> dict[str, Any]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    dimension, value, normalized = _legacy_profile_fields(record)
    return {
        "record_kind": "memory",
        "record_id": record.id,
        "memory_id": record.id,
        "fingerprint": MemoryStore.profile_memory_fingerprint(record),
        "expected_fingerprint": MemoryStore.profile_memory_fingerprint(record),
        "memory_type": record.memory_type,
        "user_id": record.subject.id,
        "scope": record.scope,
        "extractor": clean_text(metadata.get("extractor"), 40).lower(),
        "profile_dimension": dimension,
        "profile_polarity": clean_text(metadata.get("profile_polarity"), 40).lower(),
        "candidate_value": value,
        "normalized_value": normalized,
        "profile_state": _record_state(record),
        "quality_score": _quality_score(record),
        "matched_rule": reason,
        "evidence_refs": _evidence_refs(record),
        "proposed_action": action,
        "action": action,
        "reason": reason,
        "metadata_patch": _metadata_patch(record),
        "canonical_id": "",
        "canonical_expected_fingerprint": "",
        "group_id": MemoryStore.profile_repair_group_id(record.id),
        "target_state": "",
    }


def _portrait_noise_reason(fact: dict[str, Any]) -> str:
    summary = clean_text(fact.get("claim_summary"), 1000)
    source = f"我{summary.lstrip()}"
    rejected_reason = profile_rejection_reason(source)
    if rejected_reason:
        return rejected_reason
    for reason, pattern in _NOISE_PATTERNS:
        if pattern.search(source):
            return reason
    return ""


def _portrait_rank(fact: dict[str, Any]) -> tuple[str, float, str]:
    try:
        confidence = float(fact.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    return (
        clean_text(
            fact.get("last_evidence_at")
            or fact.get("updated_at")
            or fact.get("created_at"),
            80,
        ),
        max(0.0, min(1.0, confidence)),
        clean_text(fact.get("id"), 120),
    )


def _portrait_entry(
    fact: dict[str, Any], *, action: str, reason: str
) -> dict[str, Any]:
    fact_id = clean_text(fact.get("id"), 120)
    fingerprint = MemoryStore.profile_portrait_fact_fingerprint(fact)
    evidence_refs: list[Any] = []
    raw_evidence = fact.get("evidence_hashes")
    if isinstance(raw_evidence, list):
        evidence_refs = raw_evidence
    elif isinstance(raw_evidence, str):
        try:
            loaded = json.loads(raw_evidence)
        except (TypeError, ValueError):
            loaded = []
        if isinstance(loaded, list):
            evidence_refs = loaded
    return {
        "record_kind": "portrait_fact",
        "record_id": fact_id,
        "memory_id": "",
        "fingerprint": fingerprint,
        "expected_fingerprint": fingerprint,
        "expected_queue_fingerprint": clean_text(fact.get("queue_fingerprint"), 80),
        "memory_type": "portrait_fact",
        "person_id": clean_text(fact.get("person_id"), 80),
        "scope": clean_text(fact.get("source_scope"), 80),
        "extractor": clean_text(fact.get("producer_version"), 80),
        "profile_dimension": clean_text(fact.get("dimension"), 80).lower(),
        "profile_polarity": "",
        "candidate_value": clean_text(fact.get("claim_summary"), 180),
        "normalized_value": clean_text(fact.get("normalized_claim_hash"), 80),
        "profile_state": clean_text(fact.get("status"), 40).lower(),
        "quality_score": _portrait_rank(fact)[1],
        "matched_rule": reason,
        "evidence_refs": [
            clean_text(item, 160) for item in evidence_refs if clean_text(item, 160)
        ],
        "proposed_action": action,
        "action": action,
        "reason": reason,
        "metadata_patch": {},
        "canonical_id": "",
        "canonical_expected_fingerprint": "",
        "canonical_expected_queue_fingerprint": "",
        "group_id": MemoryStore.profile_repair_group_id(fact_id),
        "target_state": "",
    }


def _build_portrait_repair_entries(
    facts: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    eligible: list[dict[str, Any]] = []
    for source in facts:
        if not isinstance(source, dict):
            continue
        fact = dict(source)
        fact_id = clean_text(fact.get("id"), 120)
        if not fact_id:
            continue
        status = clean_text(fact.get("status"), 40).lower()
        version = clean_text(fact.get("producer_version"), 80).lower()
        if status in {"pending", "rejected", "superseded", "archived"}:
            entries[fact_id] = _portrait_entry(
                fact, action="keep", reason="already_inactive"
            )
            continue
        noise = _portrait_noise_reason(fact)
        if noise:
            entry = _portrait_entry(fact, action="archive", reason=noise)
            entry["target_state"] = "rejected"
            entries[fact_id] = entry
            continue
        if version == "req036.rule.v1":
            entries[fact_id] = _portrait_entry(
                fact,
                action="pending",
                reason="legacy_portrait_requires_review",
            )
            continue
        if status != "active":
            entries[fact_id] = _portrait_entry(
                fact, action="pending", reason="portrait_state_not_active"
            )
            continue
        eligible.append(fact)
        entries[fact_id] = _portrait_entry(
            fact, action="keep", reason="portrait_quality_compatible"
        )

    single_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for fact in eligible:
        dimension = clean_text(fact.get("dimension"), 80).lower()
        if dimension not in _SINGLE_VALUE_DIMENSIONS:
            continue
        key = (
            clean_text(fact.get("person_id"), 80),
            dimension,
            clean_text(fact.get("source_scope"), 80),
        )
        single_groups.setdefault(key, []).append(fact)

    for group in single_groups.values():
        ordered = sorted(group, key=_portrait_rank, reverse=True)
        canonical = ordered[0]
        canonical_id = clean_text(canonical.get("id"), 120)
        canonical_fp = MemoryStore.profile_portrait_fact_fingerprint(canonical)
        group_id = MemoryStore.profile_repair_group_id(canonical_id)
        for fact in ordered[1:]:
            entry = _portrait_entry(
                fact,
                action="archive",
                reason="profile_value_superseded",
            )
            entry["target_state"] = "superseded"
            entry["canonical_id"] = canonical_id
            entry["canonical_expected_fingerprint"] = canonical_fp
            entry["canonical_expected_queue_fingerprint"] = clean_text(
                canonical.get("queue_fingerprint"), 80
            )
            entry["group_id"] = group_id
            entries[clean_text(fact.get("id"), 120)] = entry

    return [
        entries[clean_text(fact.get("id"), 120)]
        for fact in facts
        if isinstance(fact, dict) and clean_text(fact.get("id"), 120) in entries
    ]


def build_repair_plan(
    records: Sequence[MemoryRecord],
    portrait_facts: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_id = {record.id: record for record in records}
    entries: dict[str, dict[str, Any]] = {}
    eligible: list[MemoryRecord] = []
    for record in records:
        if record.lifecycle == "archived" or _record_state(record) in {
            "rejected",
            "superseded",
        }:
            entries[record.id] = _entry(
                record, action="keep", reason="already_inactive"
            )
            continue
        noise = _noise_reason(record)
        if noise:
            entries[record.id] = _entry(record, action="archive", reason=noise)
            entries[record.id]["target_state"] = "rejected"
            continue
        valid, reason = profile_quality_decision(record, require_active=True)
        if not valid:
            entries[record.id] = _entry(record, action="pending", reason=reason)
            continue
        eligible.append(record)
        entries[record.id] = _entry(
            record, action="keep", reason="profile_quality_passed"
        )

    exact_groups: dict[tuple[str, ...], list[MemoryRecord]] = {}
    for record in eligible:
        dimension, _value, normalized = _legacy_profile_fields(record)
        if not dimension or not normalized:
            entries[record.id] = _entry(
                record,
                action="pending",
                reason="profile_group_key_missing",
            )
            continue
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        polarity = clean_text(metadata.get("profile_polarity"), 40).lower()
        exact_groups.setdefault(
            (*_domain_key(record), dimension, polarity, normalized),
            [],
        ).append(record)

    dimension_groups: dict[
        tuple[str, ...], list[tuple[MemoryRecord, list[MemoryRecord]]]
    ] = {}
    for key, group in exact_groups.items():
        ordered = sorted(group, key=_record_rank, reverse=True)
        canonical = ordered[0]
        dimension_key = (*key[:-2],)
        dimension_groups.setdefault(dimension_key, []).append((canonical, ordered))

    for dimension_key, value_groups in dimension_groups.items():
        dimension = dimension_key[-1]
        cardinality = clean_text(
            (value_groups[0][0].metadata or {}).get("profile_cardinality"),
            20,
        ).lower()
        single = cardinality == "single" or dimension in _SINGLE_VALUE_DIMENSIONS
        ordered_groups = sorted(
            value_groups,
            key=(
                lambda item: (
                    max(_single_value_rank(member) for member in item[1])
                    if single
                    else _record_rank(item[0])
                )
            ),
            reverse=True,
        )
        winning_id = (
            ordered_groups[0][0].id if single and len(ordered_groups) > 1 else ""
        )
        for group_index, (canonical, members) in enumerate(ordered_groups):
            losing_value = bool(winning_id and group_index > 0)
            if losing_value:
                winner = by_id[winning_id]
                winner_fp = MemoryStore.profile_memory_fingerprint(winner)
                group_id = MemoryStore.profile_repair_group_id(winning_id)
                for member in members:
                    entry = _entry(
                        member,
                        action="archive",
                        reason="profile_value_superseded",
                    )
                    entry["target_state"] = "superseded"
                    entry["canonical_id"] = winning_id
                    entry["canonical_expected_fingerprint"] = winner_fp
                    entry["group_id"] = group_id
                    entries[member.id] = entry
                continue
            entries[canonical.id] = _entry(
                canonical,
                action="keep",
                reason="canonical_profile_fact",
            )
            canonical_fp = MemoryStore.profile_memory_fingerprint(canonical)
            group_id = MemoryStore.profile_repair_group_id(canonical.id)
            for duplicate in members[1:]:
                entry = _entry(
                    duplicate,
                    action="merge",
                    reason="duplicate_profile_fact",
                )
                entry["canonical_id"] = canonical.id
                entry["canonical_expected_fingerprint"] = canonical_fp
                entry["group_id"] = group_id
                entries[duplicate.id] = entry

    ordered_entries = [entries[record.id] for record in records if record.id in entries]
    ordered_entries.extend(_build_portrait_repair_entries(portrait_facts or []))
    counts: dict[str, int] = {"keep": 0, "merge": 0, "pending": 0, "archive": 0}
    for item in ordered_entries:
        counts[item["proposed_action"]] += 1
    return {
        "schema_version": "profile.noise.repair.preview.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only": True,
        "record_count": len(ordered_entries),
        "counts": counts,
        "plan_fingerprint": MemoryStore.profile_repair_plan_fingerprint(
            ordered_entries
        ),
        "records": ordered_entries,
    }


async def preview(
    db_path: Path,
    *,
    user_id: str = "",
    scope: str = "",
    memory_types: list[str] | None = None,
    extractors: list[str] | None = None,
    person_id: str = "",
    limit: int = 5000,
) -> dict[str, Any]:
    store = MemoryStore(db_path, read_only=True)
    try:
        store.initialize()
        records = await store.list_rule_profile_memories(
            user_id=user_id,
            scope=scope,
            memory_types=memory_types,
            extractors=extractors,
            include_archived=True,
            limit=limit,
        )
        portrait_facts = []
        if not user_id or person_id:
            portrait_facts = await store.list_rule_portrait_facts(
                person_id=person_id,
                source_scope=scope,
                include_inactive=True,
                limit=limit,
            )
        return build_repair_plan(records, portrait_facts)
    finally:
        store.close()


async def apply_plan(
    db_path: Path,
    plan: dict[str, Any],
    *,
    confirmation: str,
    operation_id: str = "",
    rule_version: str = DEFAULT_RULE_VERSION,
) -> dict[str, Any]:
    if confirmation != APPLY_CONFIRMATION:
        raise ValueError(f"apply requires --confirm {APPLY_CONFIRMATION}")
    operation_id = _validated_operation_id(
        clean_text(operation_id, 120)
        or (
            "profile_repair_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "_"
            + secrets.token_hex(4)
        )
    )
    actions = [
        item
        for item in plan.get("records", [])
        if isinstance(item, dict)
        and item.get("action") in {"keep", "merge", "pending", "archive"}
    ]
    expected_plan_fingerprint = clean_text(plan.get("plan_fingerprint"), 80)
    actual_plan_fingerprint = MemoryStore.profile_repair_plan_fingerprint(actions)
    if (
        expected_plan_fingerprint
        and expected_plan_fingerprint != actual_plan_fingerprint
    ):
        raise ValueError("repair plan fingerprint mismatch")
    store = MemoryStore(db_path)
    try:
        store.initialize()
        backup = await asyncio.to_thread(store.backup, f".before_{operation_id}")
        result = await store.apply_profile_repairs(
            operation_id=operation_id,
            rule_version=rule_version,
            actions=actions,
            backup_path=str(backup),
        )
        result["preview_counts"] = plan.get("counts", {})
        return result
    finally:
        store.close()


async def rollback(
    db_path: Path,
    *,
    operation_id: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != ROLLBACK_CONFIRMATION:
        raise ValueError(
            f"rollback requires --rollback-confirm {ROLLBACK_CONFIRMATION}"
        )
    operation_id = _validated_operation_id(operation_id)
    store = MemoryStore(db_path)
    try:
        store.initialize()
        backup = await asyncio.to_thread(
            store.backup, f".before_rollback_{operation_id}"
        )
        return await store.rollback_profile_repairs(
            operation_id=operation_id,
            rollback_backup_path=str(backup),
        )
    finally:
        store.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview and repair rule-produced profile noise"
    )
    parser.add_argument(
        "--db", required=True, type=Path, help="Path to memory_companion.db"
    )
    parser.add_argument("--user", default="", help="Exact subject user id")
    parser.add_argument("--person", default="", help="Exact portrait person id")
    parser.add_argument(
        "--scope", choices=("", "private", "group", "unknown"), default=""
    )
    parser.add_argument("--memory-type", action="append", default=[])
    parser.add_argument("--extractor", action="append", default=[])
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--rule-version", default=DEFAULT_RULE_VERSION)
    parser.add_argument("--rollback", metavar="OPERATION_ID", default="")
    parser.add_argument("--rollback-confirm", default="")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.resolve()
    if args.rollback:
        return await rollback(
            db_path,
            operation_id=args.rollback,
            confirmation=args.rollback_confirm,
        )
    plan = await preview(
        db_path,
        user_id=args.user,
        scope=args.scope,
        memory_types=args.memory_type,
        extractors=args.extractor,
        person_id=args.person,
        limit=args.limit,
    )
    if args.apply:
        return await apply_plan(
            db_path,
            plan,
            confirmation=args.confirm,
            operation_id=args.operation_id,
            rule_version=args.rule_version,
        )
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
