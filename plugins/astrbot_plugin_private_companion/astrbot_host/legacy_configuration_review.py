"""Read-only configuration comparisons and value-free migration selections."""

from functools import lru_cache
import json
from pathlib import Path

from companion_v2.execution.models import TaskError, digest
from companion_v2.legacy.configuration_migration import (
    ACCOUNT_CONFIGURATION_TARGETS, plugin_configuration_defaults,
)
from companion_v2.legacy.native_extension_configuration import setting_value
from companion_v2.legacy.reality_migration import REALITY_SETTING_DEFAULTS, reality_setting_value


def field_path(intent, field):
    return field if intent.target_kind == "plugin_config" else intent.group_id + "." + field


def selection_id(intent, field):
    return digest(["legacy-configuration-selection.v1", intent.scope, intent.group_id,
                   intent.target_kind, intent.source_id, field])


def selection_unit(intent, field):
    """Connections, credentials and routes must retain their validated combination."""
    if intent.target_kind in {"weather_host_settings", "image_output_config", "native_extension_config"}:
        return digest([intent.scope, intent.group_id, intent.target_kind, intent.source_id])
    if intent.target_kind == "plugin_config" and field in ACCOUNT_CONFIGURATION_TARGETS:
        return "account-bindings"
    return selection_id(intent, field)


@lru_cache(maxsize=8)
def _schema(name):
    root = Path(__file__).resolve().parents[1]
    path = root / "_conf_schema.json" if name == "plugin" else (
        root / "astrbot_host" / "native_extensions" / name / "settings-schema.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _label(schema, field):
    entry = None
    for part in field.split("."):
        entry = schema.get(part) if isinstance(schema, dict) else None
        schema = entry.get("items", {}) if isinstance(entry, dict) else {}
    return entry.get("description", "") if isinstance(entry, dict) else ""


def _current_values(api, session, intent):
    kind = intent.target_kind
    if kind in {"plugin_config", "weather_host_settings"}:
        defaults = plugin_configuration_defaults()
        values = {}
        for field in intent.changes:
            path = field_path(intent, field)
            present, value = api._plugin_config_value(api.controller.config, path)
            values[field] = value if present else defaults.get(path)
        return values
    if kind == "semantic_config":
        return session.application.semantics.store.configuration_snapshot()["values"]
    if kind == "image_output_config":
        return api._image_extension_api().output_configuration()["settings"]
    if kind == "reality_config":
        config = api._reality_runtime().config
        return {field: reality_setting_value(config, field, REALITY_SETTING_DEFAULTS.get(field))
                for field in intent.changes}
    if kind == "native_extension_config":
        config = api.controller.native_extensions.service(intent.group_id).config
        defaults = api._native_extension_baselines().get(intent.group_id, {}).get("defaults", {})
        return {field: setting_value(config, field, setting_value(defaults, field))
                for field in intent.changes}
    target = api._scope_target(session, intent)
    snapshot = session.application.configuration._snapshot(
        api._operator(), target, intent.group_id, api.controller.now(), "configuration.read")
    return {field: snapshot.fields[field_path(intent, field)].value for field in intent.changes}


def configuration_rows(api, session, intents, differences):
    rows = []
    seen = set()
    for intent in intents:
        try:
            current = _current_values(api, session, intent)
        except (AttributeError, KeyError, TypeError, ValueError, TaskError):
            current = {}
        for field, value in sorted(intent.changes.items()):
            path = field_path(intent, field)
            seen.add(path)
            identifier = selection_id(intent, field)
            schema = _schema("plugin" if intent.target_kind in {"plugin_config", "weather_host_settings"}
                             else intent.group_id)
            label = _label(schema, path if intent.target_kind == "weather_host_settings" else field)
            rows.append({
                "id": identifier, "selection_id": identifier,
                "selection_unit": selection_unit(intent, field), "field": path,
                "label": label, "group_id": intent.group_id, "scope": intent.scope,
                "before": api._migration_display_value(field, value, target_kind=intent.target_kind),
                "after": api._migration_display_value(field, current.get(field), target_kind=intent.target_kind),
                "current_known": field in current,
                "status": "same" if field in current and current[field] == value else
                          "confirm" if intent.confidence.get(field) == "medium" else "merge",
                "sources": list(intent.sources.get(field, ())),
            })
    by_field = {row["field"]: row for row in rows}
    grouped = {}
    for item in differences:
        path = item.get("field") or item.get("field_id") or item.get("source_key") or ""
        if path:
            grouped.setdefault(path, []).append(item)
    for path, items in grouped.items():
        # Conversion notes precede the host's effective comparison. Prefer the
        # latter, so a note cannot hide a preserved or already identical value.
        item = max(enumerate(items), key=lambda pair: (
            "after" in pair[1], "before" in pair[1], pair[0]))[1]
        note = item.get("explain") or item.get("summary") or ""
        if path in seen:
            if path in by_field and note:
                by_field[path].setdefault("note", note)
            continue
        if not path:
            continue
        disposition, kind = item.get("disposition"), item.get("kind")
        status = ("same" if kind == "already_present" else
                  "keep" if kind in {"preserved_v2", "already_represented", "host_managed", "mode_preserved"}
                  or disposition == "implemented_elsewhere" else
                  "archive" if disposition in {"not_migrated", "not_applicable"}
                  or kind == "preserved_not_executable" else "keep")
        row = {"id": digest(["legacy-configuration-note.v1", path, kind, disposition]),
               "field": path, "before": item.get("before"), "after": item.get("after"),
               "current_known": "after" in item, "status": status, "note": note,
               "source_key": item.get("source_key", ""), "audit": bool(item.get("audit")),
               "disposition": disposition, "kind": kind}
        rows.append(row)
        seen.add(path)
    # A connected combination cannot be partly recommended and partly unconfirmed.
    pending_units = {row.get("selection_unit") for row in rows if row["status"] == "confirm"}
    for row in rows:
        if row.get("selection_unit") in pending_units:
            row["status"] = "confirm"
    return rows


def selected_intents(api, session, intents, edit_tokens, selected):
    """Accept only identifiers from the recomputed plan, never client values."""
    from dataclasses import replace

    available = {selection_id(intent, field): (intent, field)
                 for intent in intents for field in intent.changes}
    if (not isinstance(selected, list) or len(selected) > 4096
            or any(not isinstance(value, str) or value not in available for value in selected)
            or len(set(selected)) != len(selected)):
        raise TaskError("legacy_selection_invalid", "配置选择不属于当前预览，请重新读取配置对照。")
    chosen = set(selected)
    units = {}
    for identifier, (intent, field) in available.items():
        units.setdefault(selection_unit(intent, field), set()).add(identifier)
    if any(chosen & fields and not fields <= chosen for fields in units.values()):
        raise TaskError("legacy_selection_invalid", "账号绑定、模型连接与凭据需要作为一组沿用或保留。")
    result = []
    tokens = dict(edit_tokens)
    for intent in intents:
        changes = {field: value for field, value in intent.changes.items()
                   if selection_id(intent, field) in chosen}
        if not changes:
            continue
        candidate = replace(intent, changes=changes,
                            confidence={key: value for key, value in intent.confidence.items() if key in changes},
                            sources={key: value for key, value in intent.sources.items() if key in changes})
        if intent.target_kind == "plugin_config":
            if not api._plugin_config_valid(api.controller.config, changes):
                raise TaskError("legacy_selection_invalid", "所选宿主设置无法组成有效配置，请一起选择关联项。")
        elif intent.target_kind not in {"weather_host_settings", "image_output_config",
                                        "native_extension_config", "reality_config", "semantic_config"}:
            receipt = session.application.configuration.preview_changes(
                api._operator(), target=api._scope_target(session, candidate), group_id=candidate.group_id,
                changes={field_path(candidate, key): value for key, value in changes.items()},
                observed_at=api.controller.now())
            if not receipt.is_success:
                raise TaskError("legacy_selection_invalid", "所选配置缺少关联项：" + receipt.failure.reason)
        original_key = api._intent_key(intent)
        if original_key in edit_tokens:
            tokens[api._intent_key(candidate)] = edit_tokens[original_key]
        result.append(candidate)
    return result, tokens
