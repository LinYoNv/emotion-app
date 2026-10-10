from __future__ import annotations

from astrbot.core.message.components import File, Plain

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class _ComponentEvent:
    def __init__(self, components: list[object]) -> None:
        self._components = components

    def get_messages(self) -> list[object]:
        return list(self._components)


def test_file_only_private_message_is_not_treated_as_empty_event() -> None:
    plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
    event = _ComponentEvent([File(name="report.pdf", file="C:/temporary/report.pdf")])

    assert plugin._private_event_has_nontext_content(event)


def test_empty_plain_component_is_still_treated_as_empty_event() -> None:
    plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
    event = _ComponentEvent([Plain("")])

    assert not plugin._private_event_has_nontext_content(event)


def test_file_component_uses_media_result_delivery() -> None:
    plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
    attachment = File(name="report.pdf", file="C:/temporary/report.pdf")

    assert plugin._chain_has_media_component([Plain("文件在这里"), attachment])
