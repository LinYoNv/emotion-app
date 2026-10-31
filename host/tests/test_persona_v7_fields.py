"""Persona v7 bridge fields retain repository values and native cache semantics."""
from __future__ import annotations

import copy
import logging
from types import SimpleNamespace

import pytest

from astrbot_host.persona_manager import HostPersonaManager, _persona_to_v3
from emotion_app.domain.persona import Persona


PROMPT = "\n  You are Avery, a 30-year-old librarian.\nAnswer library questions calmly.  \n\t"


@pytest.mark.parametrize("representation", ["object", "dict"])
@pytest.mark.parametrize("tools", [None, [], ["catalog_lookup"]])
@pytest.mark.parametrize("skills", [None, [], ["library_catalog"]])
def test_personality_fields_preserve_none_empty_and_allowlists(representation, tools, skills):
    fields = {
        "name": "Avery", "system_prompt": PROMPT,
        "begin_dialogs": ["Where is the atlas?", "On the reference shelf."],
        "tools": tools, "skills": skills, "custom_error_message": "The catalog is unavailable.",
    }
    source = Persona(**fields) if representation == "object" else copy.deepcopy(fields)
    projected = _persona_to_v3(source)
    assert projected["prompt"] == PROMPT
    assert projected["begin_dialogs"] == fields["begin_dialogs"]
    assert projected["tools"] == tools
    assert projected["skills"] == skills
    assert projected["custom_error_message"] == fields["custom_error_message"]
    for field in ("tools", "skills"):
        if fields[field] is None:
            assert projected[field] is None
        else:
            assert projected[field] is not (getattr(source, field) if representation == "object" else source[field])


@pytest.mark.parametrize("prompt", [PROMPT, "", " \n\t ", "\r\nexact\r\n"])
@pytest.mark.parametrize("representation", ["object", "dict"])
def test_modern_system_prompt_is_verbatim_even_when_empty(prompt, representation):
    fields = {"name": "Avery", "system_prompt": prompt, "description": "legacy text must not replace it"}
    source = SimpleNamespace(**fields) if representation == "object" else fields
    projected = _persona_to_v3(source)
    assert projected["prompt"] == prompt
    assert "legacy text" not in projected["prompt"]


@pytest.mark.parametrize("dialogs", [[], [" first user ", " first assistant "], ["u1", "a1", "u2", "a2"]])
def test_processed_examples_alternate_roles_and_are_not_saved(dialogs):
    source = Persona(name="Avery", system_prompt=PROMPT, begin_dialogs=list(dialogs))
    projected = _persona_to_v3(source)
    assert projected["_begin_dialogs_processed"] == [
        {"role": "user" if i % 2 == 0 else "assistant", "content": dialog, "_no_save": True}
        for i, dialog in enumerate(dialogs)
    ]
    assert projected["begin_dialogs"] == dialogs
    assert source.begin_dialogs == dialogs


@pytest.mark.parametrize("dialogs", [["one"], ["one", "two", "three"]])
def test_odd_examples_keep_original_values_but_skip_processed_cache(dialogs, caplog):
    source = Persona(name="Avery", system_prompt=PROMPT, begin_dialogs=list(dialogs))
    with caplog.at_level(logging.ERROR, logger="astrbot"):
        projected = _persona_to_v3(source)
    assert projected["_begin_dialogs_processed"] == []
    assert projected["begin_dialogs"] == dialogs
    assert source.begin_dialogs == dialogs
    assert any("条数应该为偶数" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("representation", ["object", "dict"])
async def test_plugin_mutation_does_not_change_repository_or_subsequent_refresh(representation):
    fields = {
        "name": "Avery", "system_prompt": PROMPT,
        "begin_dialogs": ["Question", "Answer"],
        "tools": ["catalog_lookup"], "skills": ["library_catalog"],
        "custom_error_message": "Catalog error.",
    }
    source = Persona(**copy.deepcopy(fields)) if representation == "object" else copy.deepcopy(fields)
    baseline = copy.deepcopy(fields)
    manager = HostPersonaManager(repo=SimpleNamespace(list=None))

    async def list_personas():
        return [source]

    manager._repo.list = list_personas
    await manager.refresh()
    projected = await manager.get_persona("Avery")
    projected["prompt"] = "plugin-only replacement"
    projected["begin_dialogs"][0] = "plugin-only example"
    projected["tools"].append("plugin_only_tool")
    projected["skills"].clear()
    projected["_begin_dialogs_processed"][0]["content"] = "plugin-only processed example"
    projected["_begin_dialogs_processed"][0]["_no_save"] = False
    projected["custom_error_message"] = "plugin-only error"
    for field, expected in baseline.items():
        assert (getattr(source, field) if representation == "object" else source[field]) == expected
    await manager.refresh()
    refreshed = await manager.get_default_persona_v3()
    assert refreshed is not projected
    for field in ("begin_dialogs", "tools", "skills", "custom_error_message"):
        assert refreshed[field] == baseline[field]
    assert refreshed["prompt"] == PROMPT
    assert refreshed["_begin_dialogs_processed"][0] == {
        "role": "user", "content": "Question", "_no_save": True,
    }


@pytest.mark.parametrize("representation", ["object", "dict"])
def test_legacy_without_system_prompt_remains_readable(representation):
    fields = {
        "name": "Legacy Avery", "description": "Avery is an adult librarian.",
        "personality": "Patient and precise.", "scenario": "Working in a public library.",
        "creator_notes": "Synthetic audit persona.",
    }
    source = SimpleNamespace(**fields) if representation == "object" else fields
    projected = _persona_to_v3(source)
    for field in ("description", "personality", "scenario", "creator_notes"):
        assert fields[field] in projected["prompt"]
    assert projected["begin_dialogs"] == []
    assert projected["tools"] is None and projected["skills"] is None


def test_present_invalid_system_prompt_does_not_fall_back_to_legacy():
    with pytest.raises(TypeError, match="system_prompt must be a string"):
        _persona_to_v3({"name": "Avery", "system_prompt": None, "description": "Legacy text"})
