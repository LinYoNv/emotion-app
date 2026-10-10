from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "_conf_schema.json"
I18N_ROOT = ROOT / ".astrbot-plugin" / "i18n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_strict_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )


def _iter_schema_fields(
    mapping: dict[str, Any],
    path: tuple[str, ...] = (),
    visible: bool = True,
) -> Iterator[tuple[tuple[str, ...], dict[str, Any], bool]]:
    for key, value in mapping.items():
        if not isinstance(value, dict):
            continue

        field_path = path + (key,)
        field_visible = visible and not bool(value.get("invisible", False))
        yield field_path, value, field_visible

        items = value.get("items")
        if isinstance(items, dict):
            yield from _iter_schema_fields(items, field_path, field_visible)

        template_schema = value.get("template_schema")
        if isinstance(template_schema, dict):
            yield from _iter_schema_fields(
                template_schema,
                field_path + ("template_schema",),
                field_visible,
            )


def _schema_node(schema: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    node: Any = schema
    for index, key in enumerate(path):
        if not isinstance(node, dict):
            return None
        if index == 0:
            node = node.get(key)
        elif isinstance(node.get("items"), dict) and key in node["items"]:
            node = node["items"][key]
        elif isinstance(node.get("template_schema"), dict) and key in node["template_schema"]:
            node = node["template_schema"][key]
        else:
            return None
    return node if isinstance(node, dict) else None


def _i18n_node(locale: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    node: Any = locale.get("config", {})
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def _contains_han(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _is_default_compatible(field: dict[str, Any]) -> bool:
    if "default" not in field:
        return True

    value = field["default"]
    field_type = field.get("type")
    if field_type == "bool":
        return isinstance(value, bool)
    if field_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type in {"string", "text"}:
        return isinstance(value, str)
    if field_type in {"list", "template_list"}:
        return isinstance(value, list)
    if field_type == "object":
        return isinstance(value, dict)
    return True


def test_schema_and_i18n_files_are_strict_json_without_duplicate_keys() -> None:
    _load_strict_json(SCHEMA_PATH)
    for path in sorted(I18N_ROOT.glob("*.json")):
        assert isinstance(_load_strict_json(path), dict), path


def test_schema_defaults_and_option_contracts_are_type_safe() -> None:
    schema = _load_strict_json(SCHEMA_PATH)
    for path, field, _visible in _iter_schema_fields(schema):
        assert _is_default_compatible(field), path

        options = field.get("options")
        if not isinstance(options, list):
            continue
        if "default" in field:
            assert field["default"] in options, path

        for label_key in ("labels", "option_labels"):
            labels = field.get(label_key)
            if labels is not None:
                assert isinstance(labels, list), (path, label_key)
                assert len(labels) == len(options), (path, label_key)


def test_chinese_i18n_paths_exist_and_only_override_display_metadata() -> None:
    schema = _load_strict_json(SCHEMA_PATH)
    zh_cn = _load_strict_json(I18N_ROOT / "zh-CN.json")
    config = zh_cn.get("config", {})
    assert isinstance(config, dict)

    display_attrs = {"description", "hint", "labels", "name"}

    def walk(node: dict[str, Any], path: tuple[str, ...] = ()) -> None:
        if path:
            schema_field = _schema_node(schema, path)
            assert schema_field is not None, path

            for attr in display_attrs.intersection(node):
                if attr in {"description", "hint", "name"}:
                    assert isinstance(node[attr], str), (path, attr)

            options = schema_field.get("options")
            labels = node.get("labels")
            if labels is not None:
                assert isinstance(options, list), path
                assert isinstance(labels, list), path
                assert len(labels) == len(options), path
                assert all(isinstance(label, str) and label.strip() for label in labels), path
            assert "options" not in node, (path, "options")

        for key, value in node.items():
            if key in display_attrs:
                continue
            assert isinstance(value, dict), (path, key)
            walk(value, path + (key,))

    walk(config)


def test_every_visible_select_has_aligned_chinese_labels_or_schema_labels() -> None:
    schema = _load_strict_json(SCHEMA_PATH)
    zh_cn = _load_strict_json(I18N_ROOT / "zh-CN.json")

    for path, field, visible in _iter_schema_fields(schema):
        if not visible or not isinstance(field.get("options"), list):
            continue

        labels = field.get("labels") or field.get("option_labels")
        localized = _i18n_node(zh_cn, path)
        if isinstance(localized, dict) and isinstance(localized.get("labels"), list):
            labels = localized["labels"]

        assert isinstance(labels, list), path
        assert len(labels) == len(field["options"]), path
        assert all(isinstance(label, str) and label.strip() for label in labels), path


def test_visible_schema_copy_does_not_leave_standalone_english_description_or_hint() -> None:
    schema = _load_strict_json(SCHEMA_PATH)
    zh_cn = _load_strict_json(I18N_ROOT / "zh-CN.json")

    for path, field, visible in _iter_schema_fields(schema):
        if not visible:
            continue
        localized = _i18n_node(zh_cn, path) or {}
        for attr in ("description", "hint"):
            value = field.get(attr)
            if not isinstance(value, str) or not any(
                char.isascii() and char.isalpha() for char in value
            ):
                continue
            translated = localized.get(attr, value)
            assert _contains_han(translated) or not any(
                char.isascii() and char.isalpha() for char in translated
            ), (path, attr, translated)


def test_machine_options_are_not_redeclared_by_i18n() -> None:
    zh_cn = _load_strict_json(I18N_ROOT / "zh-CN.json")

    def walk(node: dict[str, Any]) -> None:
        for value in node.values():
            if not isinstance(value, dict):
                continue
            assert "options" not in value
            walk(value)

    walk(zh_cn.get("config", {}))
