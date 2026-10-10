"""Typed prompt authoring and rendering for Private Companion."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from types import MappingProxyType
from xml.sax.saxutils import escape


class PromptRenderMode(str, Enum):
    """Wire formats supported by the canonical prompt renderer."""

    CONVERSATION_XML = "conversation_xml"
    LABELED_BLOCK = "labeled_block"
    LABELED_INLINE = "labeled_inline"
    BODY_ONLY = "body_only"
    EXACT = "exact"
    PHOTO_PROMPT = "photo_prompt"


class PromptLabelStyle(str, Enum):
    """Non-canonical labels required by existing background prompt wires."""

    SQUARE = "square"
    COLON = "colon"
    FULLWIDTH_COLON = "fullwidth_colon"


@dataclass(frozen=True, slots=True)
class PromptText:
    """Ordered text fragments joined without implicit whitespace changes."""

    parts: tuple[Any, ...]
    separator: str = ""


@dataclass(frozen=True, slots=True)
class PromptHeadingRef:
    """A typed reference to a labeled heading inside prompt body text."""

    title: str
    newline: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _validate_prompt_title(self.title))
        if not isinstance(self.newline, bool):
            raise TypeError("prompt heading reference newline must be bool")


@dataclass(frozen=True, slots=True)
class PromptGroup:
    """Ordered structured content rendered without flattening child types."""

    parts: tuple[Any, ...]
    separator: str = "\n\n"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A validated template whose variables remain typed until rendering."""

    template: str
    variables: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        template = str(self.template)
        variables = dict(self.variables)
        referenced: set[str] = set()
        try:
            parsed = tuple(string.Formatter().parse(template))
        except ValueError as exc:
            raise ValueError(f"invalid prompt template: {exc}") from exc
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not field_name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
                raise ValueError(f"invalid prompt template variable: {field_name!r}")
            if format_spec or conversion:
                raise ValueError("prompt template variables do not support format specs or conversions")
            referenced.add(field_name)
        supplied = set(variables)
        missing = sorted(referenced - supplied)
        unused = sorted(supplied - referenced)
        if missing:
            raise ValueError(f"missing prompt template variables: {', '.join(missing)}")
        if unused:
            raise ValueError(f"unused prompt template variables: {', '.join(unused)}")
        for name, value in variables.items():
            _validate_prompt_content(
                value,
                location=f"template variable {name!r}",
            )
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "variables", MappingProxyType(variables))


@dataclass(frozen=True, slots=True)
class PromptCData:
    """Content that must remain visibly literal inside conversation XML."""

    content: Any


@dataclass(frozen=True, slots=True)
class ExactText:
    """A byte-sensitive text contract that must not be normalized or escaped."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("exact prompt text must be str")


@dataclass(frozen=True, slots=True)
class PhotoPromptContent:
    """Typed positive/negative payload owned by the photo prompt domain."""

    positive: str = ""
    negative: str = ""
    domain_source: str = ""
    protected: bool = False
    sanitize_conflicts: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.positive, str):
            raise TypeError("photo prompt positive content must be str")
        if not isinstance(self.negative, str):
            raise TypeError("photo prompt negative content must be str")
        if not isinstance(self.domain_source, str) or not self.domain_source:
            raise ValueError("photo prompt domain source must be a non-empty str")
        if not isinstance(self.protected, bool):
            raise TypeError("photo prompt protected flag must be bool")
        if self.sanitize_conflicts is not None and not isinstance(
            self.sanitize_conflicts,
            bool,
        ):
            raise TypeError("photo prompt sanitize_conflicts must be bool or None")


@dataclass(frozen=True, slots=True)
class PromptLabel:
    """One typed label override for a document part."""

    style: PromptLabelStyle
    separator: ExactText = field(default_factory=lambda: ExactText("\n"))

    def __post_init__(self) -> None:
        if not isinstance(self.style, PromptLabelStyle):
            raise TypeError("prompt label style must be PromptLabelStyle")
        if not isinstance(self.separator, ExactText):
            raise TypeError("prompt label separator must be ExactText")


@dataclass(frozen=True, slots=True)
class PromptRenderSpec:
    """Typed wire-layout controls for one prompt document part."""

    mode: PromptRenderMode | None = None
    label: PromptLabel | None = None
    prefix: ExactText | None = None
    prefix_separator: ExactText = field(default_factory=lambda: ExactText("\n"))
    separator_before: ExactText = field(default_factory=lambda: ExactText("\n\n"))
    trim: bool = False

    def __post_init__(self) -> None:
        if self.mode is not None and not isinstance(self.mode, PromptRenderMode):
            raise TypeError("prompt render spec mode must be PromptRenderMode or None")
        if self.label is not None and not isinstance(self.label, PromptLabel):
            raise TypeError("prompt render spec label must be PromptLabel or None")
        if self.prefix is not None and not isinstance(self.prefix, ExactText):
            raise TypeError("prompt render spec prefix must be ExactText or None")
        if not isinstance(self.prefix_separator, ExactText):
            raise TypeError("prompt render spec prefix separator must be ExactText")
        if not isinstance(self.separator_before, ExactText):
            raise TypeError("prompt render spec separator must be ExactText")
        if not isinstance(self.trim, bool):
            raise TypeError("prompt render spec trim must be bool")
        if self.label is not None and self.mode not in {None, PromptRenderMode.BODY_ONLY}:
            raise ValueError("prompt label overrides require body-only rendering")


@dataclass(frozen=True, slots=True)
class PromptField:
    """One named structured field."""

    name: str
    value: Any

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _validate_xml_name(self.name, kind="prompt field"),
        )


@dataclass(frozen=True, slots=True)
class PromptList:
    """An explicitly named ordered collection."""

    items: tuple[Any, ...]
    tag: str = "items"
    item_tag: str = "item"
    prefix: str = ""
    separator: str = "\n"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tag",
            _validate_xml_name(self.tag, kind="prompt list"),
        )
        object.__setattr__(
            self,
            "item_tag",
            _validate_xml_name(self.item_tag, kind="prompt list item"),
        )


@dataclass(frozen=True, slots=True)
class XmlElement:
    """Typed XML node; callers provide data, never pre-rendered XML."""

    tag: str
    attrs: Mapping[str, Any] = field(default_factory=dict)
    text: Any = None
    children: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        tag = _validate_xml_name(self.tag, kind="XML element")
        if not isinstance(self.attrs, Mapping):
            raise TypeError("XML attributes must be a mapping")
        normalized_attrs: dict[str, Any] = {}
        for key, value in self.attrs.items():
            normalized_key = _validate_xml_name(key, kind="XML attribute")
            if isinstance(
                value,
                (
                    Mapping,
                    list,
                    tuple,
                    set,
                    XmlElement,
                    PromptGroup,
                    PromptText,
                    PromptTemplate,
                    PromptCData,
                ),
            ):
                raise TypeError(f"XML attribute {key!r} must be scalar")
            if value is not None and not isinstance(value, (str, bool, int, float)):
                raise TypeError(f"XML attribute {key!r} must be scalar")
            normalized_attrs[normalized_key] = value
        normalized_children = tuple(self.children)
        allowed_children = (
            XmlElement,
            PromptGroup,
            PromptHeadingRef,
            PromptText,
            PromptTemplate,
            PromptCData,
            ExactText,
            str,
        )
        if not all(isinstance(child, allowed_children) for child in normalized_children):
            raise TypeError("XML children must be typed prompt content")
        object.__setattr__(self, "tag", tag)
        object.__setattr__(self, "attrs", MappingProxyType(normalized_attrs))
        object.__setattr__(self, "children", normalized_children)


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One strictly identified, immutable prompt-authoring unit."""

    key: str
    title: str
    source: str
    content: Any
    children: tuple["PromptSection", ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        key = _validate_prompt_identity(self.key, kind="key", limit=160)
        title = _validate_prompt_title(self.title)
        source = _validate_prompt_identity(self.source, kind="source", limit=80)
        children = tuple(self.children)
        if not all(isinstance(child, PromptSection) for child in children):
            raise TypeError("prompt section children must contain only PromptSection values")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("prompt section metadata must be a mapping")
        _validate_prompt_content(self.content, location=f"section {key!r} content")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def __copy__(self) -> "PromptSection":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "PromptSection":
        return PromptSection(
            key=self.key,
            title=self.title,
            source=self.source,
            content=copy.deepcopy(self.content, memo),
            children=copy.deepcopy(self.children, memo),
            metadata=copy.deepcopy(dict(self.metadata), memo),
        )


@dataclass(frozen=True, slots=True)
class PromptDocumentPart:
    """One authored section plus its sink-specific rendering contract."""

    section: PromptSection
    render_spec: PromptRenderSpec | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.section, PromptSection):
            raise TypeError("prompt document part requires PromptSection")
        if self.render_spec is not None and not isinstance(
            self.render_spec,
            PromptRenderSpec,
        ):
            raise TypeError("prompt document part render spec must be PromptRenderSpec or None")


@dataclass(frozen=True, slots=True)
class PromptDocument:
    """A channel-aware collection; all authored content remains sections."""

    system: tuple[PromptSection, ...] = ()
    user: tuple[PromptSection, ...] = ()
    system_parts: tuple[PromptDocumentPart, ...] = ()
    user_parts: tuple[PromptDocumentPart, ...] = ()
    system_render: PromptRenderSpec | None = None
    user_render: PromptRenderSpec | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        system_sections = tuple(self.system)
        user_sections = tuple(self.user)
        if not all(isinstance(item, PromptSection) for item in (*system_sections, *user_sections)):
            raise TypeError("prompt document channels must contain PromptSection instances")
        system_parts = tuple(self.system_parts) or tuple(
            PromptDocumentPart(section=section) for section in system_sections
        )
        user_parts = tuple(self.user_parts) or tuple(
            PromptDocumentPart(section=section) for section in user_sections
        )
        if not all(
            isinstance(item, PromptDocumentPart)
            for item in (*system_parts, *user_parts)
        ):
            raise TypeError("prompt document parts must contain PromptDocumentPart instances")
        if tuple(part.section for part in system_parts) != system_sections:
            raise ValueError("prompt document system parts do not match system sections")
        if tuple(part.section for part in user_parts) != user_sections:
            raise ValueError("prompt document user parts do not match user sections")
        if self.system_render is not None and not isinstance(
            self.system_render,
            PromptRenderSpec,
        ):
            raise TypeError("prompt document system render must be PromptRenderSpec or None")
        if self.user_render is not None and not isinstance(
            self.user_render,
            PromptRenderSpec,
        ):
            raise TypeError("prompt document user render must be PromptRenderSpec or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("prompt document metadata must be a mapping")
        object.__setattr__(self, "system", system_sections)
        object.__setattr__(self, "user", user_sections)
        object.__setattr__(self, "system_parts", system_parts)
        object.__setattr__(self, "user_parts", user_parts)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _validate_xml_name(value: Any, *, kind: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{kind} must be str")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", value):
        raise ValueError(f"invalid {kind} name: {value!r}")
    return value


def _validate_prompt_identity(value: Any, *, kind: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"prompt {kind} must be str")
    if not value or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError(f"invalid prompt {kind}: {value!r}")
    return value


def _validate_prompt_title(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("prompt title must be str")
    if (
        not value
        or len(value) > 80
        or value != value.strip()
        or any(char in value for char in "\r\n\t")
    ):
        raise ValueError(f"invalid prompt title: {value!r}")
    return value


def _validate_prompt_content(value: Any, *, location: str) -> None:
    if isinstance(value, PromptSection):
        raise TypeError(f"{location} cannot contain PromptSection; use children")
    if isinstance(value, Mapping):
        raise TypeError(f"{location} cannot contain raw Mapping content")
    if isinstance(value, (PromptText, PromptGroup)):
        for index, item in enumerate(value.parts):
            _validate_prompt_content(item, location=f"{location} part {index}")
        return
    if isinstance(value, PromptHeadingRef):
        return
    if isinstance(value, PhotoPromptContent):
        return
    if isinstance(value, PromptTemplate):
        for name, item in value.variables.items():
            _validate_prompt_content(
                item,
                location=f"{location} variable {name!r}",
            )
        return
    if isinstance(value, PromptCData):
        _validate_prompt_content(value.content, location=f"{location} CDATA")
        return
    if isinstance(value, PromptField):
        _validate_prompt_content(value.value, location=f"{location} field {value.name!r}")
        return
    if isinstance(value, PromptList):
        for index, item in enumerate(value.items):
            _validate_prompt_content(item, location=f"{location} item {index}")
        return
    if isinstance(value, XmlElement):
        if value.text is not None:
            _validate_prompt_content(value.text, location=f"{location} XML text")
        for index, child in enumerate(value.children):
            _validate_prompt_content(child, location=f"{location} XML child {index}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_prompt_content(item, location=f"{location} item {index}")
        return
    if value is None or isinstance(value, (str, bool, int, float, ExactText)):
        return
    raise TypeError(f"{location} has unsupported type {type(value).__name__}")


def prompt_text(*parts: Any, separator: str = "") -> PromptText:
    return PromptText(parts=tuple(parts), separator=str(separator))


def prompt_heading_ref(title: str, *, newline: bool = False) -> PromptHeadingRef:
    return PromptHeadingRef(title=title, newline=newline)


def prompt_group(*parts: Any, separator: str = "\n\n") -> PromptGroup:
    return PromptGroup(parts=tuple(parts), separator=str(separator))


def prompt_cdata(content: Any) -> PromptCData:
    return PromptCData(content=content)


def exact_text(text: str) -> ExactText:
    return ExactText(text=text)


def prompt_field(name: str, value: Any) -> PromptField:
    return PromptField(name=name, value=value)


def prompt_list(
    items: Iterable[Any],
    *,
    tag: str = "items",
    item_tag: str = "item",
    prefix: str = "",
    separator: str = "\n",
) -> PromptList:
    return PromptList(
        items=tuple(items),
        tag=tag,
        item_tag=item_tag,
        prefix=str(prefix),
        separator=str(separator),
    )


_MISSING = object()


def prompt_section(
    *,
    key: str,
    title: str,
    source: str,
    content: Any = _MISSING,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    children: Iterable[PromptSection] = (),
    metadata: Mapping[str, Any] | None = None,
) -> PromptSection:
    """Create one strictly identified canonical prompt section."""

    if template is not None and content is not _MISSING:
        raise TypeError("prompt_section accepts either content or template, not both")
    if template is not None:
        if not isinstance(template, str):
            raise TypeError("prompt section template must be str")
        content = PromptTemplate(template=template, variables=dict(variables or {}))
    elif variables:
        raise TypeError("prompt_section variables require template")
    elif content is _MISSING:
        raise TypeError("prompt_section requires content or template")
    return PromptSection(
        key=key,
        title=title,
        source=source,
        content=content,
        children=tuple(children),
        metadata=dict(metadata or {}),
    )


def prompt_document(
    *,
    system: Iterable[PromptSection | PromptDocumentPart] = (),
    user: Iterable[PromptSection | PromptDocumentPart] = (),
    system_render: PromptRenderSpec | None = None,
    user_render: PromptRenderSpec | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PromptDocument:
    def normalize(
        values: Iterable[PromptSection | PromptDocumentPart],
    ) -> tuple[tuple[PromptSection, ...], tuple[PromptDocumentPart, ...]]:
        parts: list[PromptDocumentPart] = []
        for value in values:
            if isinstance(value, PromptSection):
                parts.append(PromptDocumentPart(section=value))
            elif isinstance(value, PromptDocumentPart):
                parts.append(value)
            else:
                raise TypeError(
                    "prompt document channels require PromptSection or PromptDocumentPart values"
                )
        return tuple(part.section for part in parts), tuple(parts)

    system_sections, system_parts = normalize(system)
    user_sections, user_parts = normalize(user)
    return PromptDocument(
        system=system_sections,
        user=user_sections,
        system_parts=system_parts,
        user_parts=user_parts,
        system_render=system_render,
        user_render=user_render,
        metadata=dict(metadata or {}),
    )


def prompt_document_part(
    section: PromptSection,
    *,
    render_spec: PromptRenderSpec | None = None,
) -> PromptDocumentPart:
    return PromptDocumentPart(section=section, render_spec=render_spec)


def xml_element(
    tag: str,
    *,
    attrs: Mapping[str, Any] | None = None,
    text: Any = None,
    children: Iterable[Any] = (),
) -> XmlElement:
    return XmlElement(
        tag=tag,
        attrs=dict(attrs or {}),
        text=text,
        children=tuple(children),
    )


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, PromptCData):
        return _has_content(value.content)
    if isinstance(value, ExactText):
        return bool(value.text)
    if isinstance(value, PhotoPromptContent):
        return bool(value.positive or value.negative)
    if isinstance(value, PromptTemplate):
        return bool(value.template)
    if isinstance(value, PromptGroup):
        return any(_has_content(item) for item in value.parts)
    if isinstance(value, PromptText):
        return any(_has_content(item) for item in value.parts)
    if isinstance(value, PromptField):
        return _has_content(value.value)
    if isinstance(value, PromptList):
        return bool(value.items)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True


def _xml_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = "".join(
        char
        for char in str(value)
        if (
            char in "\t\n\r"
            or 0x20 <= ord(char) <= 0xD7FF
            or 0xE000 <= ord(char) <= 0xFFFD
            or 0x10000 <= ord(char) <= 0x10FFFF
        )
    )
    return text


def _xml_text(value: Any) -> str:
    return escape(_xml_string(value))


def _xml_attribute(value: Any) -> str:
    return escape(_xml_string(value), {'"': "&quot;", "'": "&apos;"})


def _plain_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, ExactText):
        return value.text
    if isinstance(value, PromptCData):
        return _plain_content(value.content)
    if isinstance(value, PromptTemplate):
        parts: list[str] = []
        for literal, field_name, _format_spec, _conversion in string.Formatter().parse(value.template):
            parts.append(literal)
            if field_name is not None:
                parts.append(_plain_content(value.variables[field_name]))
        return "".join(parts)
    if isinstance(value, PromptHeadingRef):
        return f"【{value.title}】" + ("\n" if value.newline else "")
    if isinstance(value, PhotoPromptContent):
        raise TypeError("PhotoPromptContent requires PHOTO_PROMPT rendering")
    if isinstance(value, PromptGroup):
        return value.separator.join(_plain_content(part) for part in value.parts)
    if isinstance(value, PromptText):
        return value.separator.join(_plain_content(part) for part in value.parts)
    if isinstance(value, PromptField):
        return f"{value.name}: {_plain_content(value.value)}"
    if isinstance(value, PromptList):
        return value.separator.join(f"{value.prefix}{_plain_content(item)}" for item in value.items)
    if isinstance(value, XmlElement):
        return _render_xml_element(value)
    if isinstance(value, Mapping):
        raise TypeError("raw Mapping is not prompt content; serialize it explicitly")
    if isinstance(value, (list, tuple)):
        return "\n".join(_plain_content(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cdata_text(value: Any) -> str:
    return _xml_string(_plain_content(value)).replace("]]>", "]]]]><![CDATA[>")


def _list_item_tag(parent: str) -> str:
    return {
        "history": "message",
        "constraints": "constraint",
        "items": "item",
        "evidence": "item",
    }.get(parent, "item")


def _render_xml_value(tag: str, value: Any) -> str:
    safe_tag = _validate_xml_name(tag, kind="XML value")
    if isinstance(value, PromptField):
        return _render_xml_value(value.name, value.value)
    if isinstance(value, PromptList):
        body = "".join(_render_xml_value(value.item_tag, item) for item in value.items)
        return f"<{value.tag}>{body}</{value.tag}>"
    if isinstance(value, XmlElement):
        return f"<{safe_tag}>{_render_xml_element(value)}</{safe_tag}>"
    if isinstance(value, Mapping):
        raise TypeError("raw Mapping is not XML prompt content; use typed XML nodes")
    if isinstance(value, (list, tuple)):
        item_tag = _list_item_tag(safe_tag)
        body = "".join(_render_xml_value(item_tag, item) for item in value)
        return f"<{safe_tag}>{body}</{safe_tag}>"
    if isinstance(value, PromptCData):
        return f"<{safe_tag}><![CDATA[{_cdata_text(value.content)}]]></{safe_tag}>"
    if isinstance(value, ExactText):
        raise ValueError("ExactText cannot be embedded in conversation XML")
    return f"<{safe_tag}>{_xml_text(_plain_content(value))}</{safe_tag}>"


def _render_xml_child(value: Any) -> str:
    if isinstance(value, PromptSection):
        return _render_xml_section(value)
    if isinstance(value, XmlElement):
        return _render_xml_element(value)
    if isinstance(value, PromptCData):
        return f"<![CDATA[{_cdata_text(value.content)}]]>"
    if isinstance(value, ExactText):
        raise ValueError("ExactText cannot be embedded in conversation XML")
    return _xml_text(_plain_content(value))


def _render_xml_element(element: XmlElement) -> str:
    attrs = "".join(
        f' {key}="{_xml_attribute(value)}"'
        for key, value in element.attrs.items()
        if value is not None
    )
    body = _render_xml_child(element.text) if element.text is not None else ""
    body += "".join(_render_xml_child(child) for child in element.children)
    if not body:
        return f"<{element.tag}{attrs}/>"
    return f"<{element.tag}{attrs}>{body}</{element.tag}>"


def _render_xml_content(value: Any) -> str:
    if isinstance(value, PromptCData):
        return f"<![CDATA[{_cdata_text(value.content)}]]>"
    if isinstance(value, ExactText):
        raise ValueError("ExactText requires the exact render mode")
    if isinstance(value, PromptGroup):
        separator = _xml_text(value.separator)
        return separator.join(_render_xml_content(item) for item in value.parts)
    if isinstance(value, PromptSection):
        raise TypeError("nested PromptSection must be declared through children")
    if isinstance(value, XmlElement):
        return _render_xml_element(value)
    if isinstance(value, PromptField):
        return _render_xml_value(value.name, value.value)
    if isinstance(value, PromptList):
        return _render_xml_value(value.tag, value)
    if isinstance(value, Mapping):
        raise TypeError("raw Mapping is not XML prompt content; use typed XML nodes")
    if isinstance(value, (list, tuple)):
        return "".join(_render_xml_value("item", item) for item in value)
    return _xml_text(_plain_content(value))


def _coerce_render_mode(value: PromptRenderMode | str) -> PromptRenderMode:
    if isinstance(value, PromptRenderMode):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return PromptRenderMode(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported prompt render mode: {value!r}") from exc


def _render_conversation_xml(sections: Sequence[PromptSection]) -> str:
    visible = tuple(section for section in sections if _section_has_content(section))
    if not visible:
        return ""
    body = "".join(_render_xml_section(section) for section in visible)
    return f"<private_companion_context>{body}</private_companion_context>"


def _section_has_content(section: PromptSection) -> bool:
    return _has_content(section.content) or any(
        _section_has_content(child) for child in section.children
    )


def _render_xml_section(section: PromptSection) -> str:
    body = _render_xml_content(section.content)
    body += "".join(
        _render_xml_section(child)
        for child in section.children
        if _section_has_content(child)
    )
    return f'<section title="{_xml_attribute(section.title)}">{body}</section>'


def _render_labeled_section(section: PromptSection, *, inline: bool) -> str:
    if not _section_has_content(section):
        return ""
    separator = "" if inline else "\n"
    body = _plain_content(section.content)
    child_blocks = [
        _render_labeled_section(child, inline=False)
        for child in section.children
        if _section_has_content(child)
    ]
    content = "\n\n".join(part for part in (body, *child_blocks) if part)
    return f"【{section.title}】{separator}{content}"


def _render_labeled(sections: Sequence[PromptSection], *, inline: bool) -> str:
    return "\n\n".join(
        _render_labeled_section(section, inline=inline)
        for section in sections
    )


def _render_body_section(section: PromptSection) -> str:
    if not _section_has_content(section):
        return ""
    body = _plain_content(section.content)
    child_blocks = [
        _render_labeled_section(child, inline=False)
        for child in section.children
        if _section_has_content(child)
    ]
    return "\n\n".join(part for part in (body, *child_blocks) if part)


def _render_body_only(sections: Sequence[PromptSection]) -> str:
    return "\n\n".join(
        content
        for section in sections
        if (content := _render_body_section(section))
    )


def _render_exact(sections: Sequence[PromptSection]) -> str:
    bodies: list[str] = []
    for section in sections:
        if section.children:
            raise TypeError("exact render mode does not support child sections")
        if not isinstance(section.content, ExactText):
            raise TypeError("exact render mode requires ExactText content")
        bodies.append(section.content.text)
    return "".join(bodies)


def _render_photo_prompt(sections: Sequence[PromptSection]) -> str:
    """Render an already-authoritative photo payload without XML semantics.

    The photo pipeline owns positive/negative ordering and NAI weights. During
    Complete assembled wires may use ExactText, while authored photo sections
    carry PhotoPromptContent without changing this public mode.
    """

    if any(section.children for section in sections):
        raise TypeError("photo prompt render mode does not support child sections")
    bodies: list[str] = []
    for section in sections:
        if isinstance(section.content, ExactText):
            bodies.append(section.content.text)
        elif isinstance(section.content, PhotoPromptContent):
            bodies.append(section.content.positive)
        else:
            bodies.append(_render_body_section(section))
    return "\n\n".join(body for body in bodies if body)


def _fingerprint_scalar(value: Any) -> list[Any]:
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    raise TypeError(f"unsupported fingerprint scalar: {type(value).__name__}")


def _fingerprint_content(value: Any) -> Any:
    if isinstance(value, PromptText):
        return [
            "text",
            value.separator,
            [_fingerprint_content(item) for item in value.parts],
        ]
    if isinstance(value, PromptGroup):
        return [
            "group",
            value.separator,
            [_fingerprint_content(item) for item in value.parts],
        ]
    if isinstance(value, PromptTemplate):
        return [
            "template",
            value.template,
            [
                [name, _fingerprint_content(item)]
                for name, item in sorted(value.variables.items())
            ],
        ]
    if isinstance(value, PromptHeadingRef):
        return ["heading_ref", value.title, value.newline]
    if isinstance(value, PhotoPromptContent):
        return [
            "photo_prompt",
            value.positive,
            value.negative,
            value.domain_source,
            value.protected,
            value.sanitize_conflicts,
        ]
    if isinstance(value, PromptCData):
        return ["cdata", _fingerprint_content(value.content)]
    if isinstance(value, ExactText):
        return ["exact", value.text]
    if isinstance(value, PromptField):
        return ["field", value.name, _fingerprint_content(value.value)]
    if isinstance(value, PromptList):
        return [
            "list_node",
            value.tag,
            value.item_tag,
            value.prefix,
            value.separator,
            [_fingerprint_content(item) for item in value.items],
        ]
    if isinstance(value, XmlElement):
        return [
            "xml",
            value.tag,
            [
                [name, _fingerprint_scalar(item)]
                for name, item in sorted(value.attrs.items())
            ],
            _fingerprint_content(value.text) if value.text is not None else None,
            [_fingerprint_content(item) for item in value.children],
        ]
    if isinstance(value, list):
        return ["list", [_fingerprint_content(item) for item in value]]
    if isinstance(value, tuple):
        return ["tuple", [_fingerprint_content(item) for item in value]]
    if isinstance(value, Mapping):
        raise TypeError("raw Mapping is not prompt content")
    return _fingerprint_scalar(value)


def _fingerprint_section_payload(section: PromptSection) -> list[Any]:
    return [
        "section",
        section.key,
        section.title,
        section.source,
        _fingerprint_content(section.content),
        [_fingerprint_section_payload(child) for child in section.children],
    ]


def prompt_section_fingerprint(section: PromptSection) -> str:
    """Return a stable metadata-independent fingerprint for one section tree."""

    if not isinstance(section, PromptSection):
        raise TypeError("prompt_section_fingerprint requires PromptSection")
    payload = json.dumps(
        _fingerprint_section_payload(section),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def render_prompt_content(
    content: Any,
    *,
    mode: PromptRenderMode | str = PromptRenderMode.BODY_ONLY,
) -> str:
    """Render one typed content node without inventing a section identity."""

    _validate_prompt_content(content, location="prompt content")
    render_mode = _coerce_render_mode(mode)
    if isinstance(content, PhotoPromptContent) and render_mode is not PromptRenderMode.PHOTO_PROMPT:
        raise TypeError("PhotoPromptContent requires PHOTO_PROMPT rendering")
    if render_mode is PromptRenderMode.BODY_ONLY:
        return _plain_content(content)
    if render_mode is PromptRenderMode.CONVERSATION_XML:
        return _render_xml_content(content)
    if render_mode is PromptRenderMode.EXACT:
        if not isinstance(content, ExactText):
            raise TypeError("exact render mode requires ExactText content")
        return content.text
    if render_mode is PromptRenderMode.PHOTO_PROMPT:
        if isinstance(content, ExactText):
            return content.text
        if isinstance(content, PhotoPromptContent):
            return content.positive
        return _plain_content(content)
    raise ValueError("labeled prompt content requires a section title")


def render_prompt_sections(
    sections: Iterable[PromptSection],
    *,
    mode: PromptRenderMode | str = PromptRenderMode.CONVERSATION_XML,
) -> str:
    """Render authored sections without changing business-content spacing."""

    payload = tuple(sections)
    if not all(isinstance(section, PromptSection) for section in payload):
        raise TypeError("render_prompt_sections requires PromptSection values")
    render_mode = _coerce_render_mode(mode)
    if render_mode is PromptRenderMode.CONVERSATION_XML:
        return _render_conversation_xml(payload)
    if render_mode is PromptRenderMode.LABELED_BLOCK:
        return _render_labeled(payload, inline=False)
    if render_mode is PromptRenderMode.LABELED_INLINE:
        return _render_labeled(payload, inline=True)
    if render_mode is PromptRenderMode.BODY_ONLY:
        return _render_body_only(payload)
    if render_mode is PromptRenderMode.EXACT:
        return _render_exact(payload)
    if render_mode is PromptRenderMode.PHOTO_PROMPT:
        return _render_photo_prompt(payload)
    raise AssertionError(f"unhandled prompt render mode: {render_mode}")


def _render_prompt_label(section: PromptSection, label: PromptLabel) -> str:
    if label.style is PromptLabelStyle.SQUARE:
        return f"[{section.title}]"
    if label.style is PromptLabelStyle.COLON:
        return f"{section.title}:"
    if label.style is PromptLabelStyle.FULLWIDTH_COLON:
        return f"{section.title}："
    raise AssertionError(f"unhandled prompt label style: {label.style}")


def _render_prompt_document_channel(
    parts: Sequence[PromptDocumentPart],
    *,
    default_mode: PromptRenderMode | str,
    default_spec: PromptRenderSpec | None,
) -> str:
    if not parts:
        return ""
    if default_spec is None and all(part.render_spec is None for part in parts):
        return render_prompt_sections(
            (part.section for part in parts),
            mode=default_mode,
        )

    inherited_mode = _coerce_render_mode(default_mode)
    rendered_document = ""
    for part in parts:
        spec = part.render_spec or default_spec or PromptRenderSpec()
        mode = spec.mode or inherited_mode
        if spec.label is not None:
            body = render_prompt_sections(
                (part.section,),
                mode=PromptRenderMode.BODY_ONLY,
            )
            label = _render_prompt_label(part.section, spec.label)
            rendered = (
                f"{label}{spec.label.separator.text}{body}"
                if body
                else label
            )
        else:
            rendered = render_prompt_sections((part.section,), mode=mode)
        if spec.trim:
            rendered = rendered.strip()
        if spec.prefix is not None and rendered:
            rendered = (
                f"{spec.prefix.text}{spec.prefix_separator.text}{rendered}"
            )
        if not rendered:
            continue
        rendered_document = (
            f"{rendered_document}{spec.separator_before.text}{rendered}"
            if rendered_document
            else rendered
        )
    return rendered_document


def render_prompt_document(
    document: PromptDocument,
    *,
    mode: PromptRenderMode | str | None = None,
    system_mode: PromptRenderMode | str = PromptRenderMode.LABELED_BLOCK,
    user_mode: PromptRenderMode | str = PromptRenderMode.LABELED_BLOCK,
) -> dict[str, str]:
    if not isinstance(document, PromptDocument):
        raise TypeError("render_prompt_document requires PromptDocument")
    if mode is not None:
        system_mode = mode
        user_mode = mode
    return {
        "system": _render_prompt_document_channel(
            document.system_parts,
            default_mode=system_mode,
            default_spec=document.system_render,
        ),
        "user": _render_prompt_document_channel(
            document.user_parts,
            default_mode=user_mode,
            default_spec=document.user_render,
        ),
    }


__all__ = [
    "ExactText",
    "PromptCData",
    "PromptDocument",
    "PromptDocumentPart",
    "PromptField",
    "PromptGroup",
    "PromptHeadingRef",
    "PromptLabel",
    "PromptLabelStyle",
    "PromptList",
    "PhotoPromptContent",
    "PromptRenderMode",
    "PromptRenderSpec",
    "PromptSection",
    "PromptTemplate",
    "PromptText",
    "XmlElement",
    "exact_text",
    "prompt_cdata",
    "prompt_document",
    "prompt_document_part",
    "prompt_field",
    "prompt_group",
    "prompt_heading_ref",
    "prompt_list",
    "prompt_section",
    "prompt_section_fingerprint",
    "prompt_text",
    "render_prompt_document",
    "render_prompt_content",
    "render_prompt_sections",
    "xml_element",
]
