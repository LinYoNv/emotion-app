from __future__ import annotations

import copy
import inspect
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, is_dataclass
from xml.etree import ElementTree as ET

from astrbot_plugin_private_companion import conversation_prompt_section as prompt_module
from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptLabel,
    PromptLabelStyle,
    PromptRenderMode,
    PromptRenderSpec,
    PromptSection,
    PromptTemplate,
    exact_text,
    prompt_cdata,
    prompt_document,
    prompt_document_part,
    prompt_field,
    prompt_group,
    prompt_heading_ref,
    prompt_list,
    prompt_section,
    prompt_section_fingerprint,
    prompt_text,
    render_prompt_content,
    render_prompt_document,
    render_prompt_sections,
    xml_element,
)


class PromptSectionAuthoringTests(unittest.TestCase):
    def test_section_is_a_frozen_dataclass_not_a_mapping(self) -> None:
        section = prompt_section(
            key="reply.style",
            title="回复风格约束",
            source="reply_style",
            content="保持自然。",
            metadata={"priority_group": "stable"},
        )

        self.assertTrue(is_dataclass(PromptSection))
        self.assertIsInstance(section, PromptSection)
        self.assertNotIsInstance(section, Mapping)
        self.assertEqual("reply.style", section.key)
        self.assertEqual("回复风格约束", section.title)
        self.assertEqual("reply_style", section.source)
        self.assertEqual("保持自然。", section.content)
        self.assertEqual({"priority_group": "stable"}, section.metadata)
        with self.assertRaises(FrozenInstanceError):
            section.title = "不能修改"
        with self.assertRaises(TypeError):
            section.metadata["new"] = "不能修改"
        self.assertEqual(section, copy.deepcopy(section))

    def test_prompt_section_is_keyword_only_and_requires_strict_identity(self) -> None:
        signature = inspect.signature(prompt_section)
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )
        with self.assertRaises(TypeError):
            prompt_section("旧标题", "旧正文")

        common = {
            "key": "reply.style",
            "title": "回复风格",
            "source": "test",
            "content": "正文",
        }
        for missing in ("key", "title", "source"):
            kwargs = {name: value for name, value in common.items() if name != missing}
            with self.subTest(missing=missing), self.assertRaises(TypeError):
                prompt_section(**kwargs)

        invalid = (
            ("key", "bad key", ValueError),
            ("key", 7, TypeError),
            ("source", "来源", ValueError),
            ("source", None, TypeError),
            ("title", "", ValueError),
            ("title", " 标题", ValueError),
            ("title", "两行\n标题", ValueError),
            ("title", 7, TypeError),
        )
        for field, value, error in invalid:
            kwargs = dict(common)
            kwargs[field] = value
            with self.subTest(invalid=field, value=value), self.assertRaises(error):
                prompt_section(**kwargs)

    def test_template_renders_exact_declared_variables_and_escapes_xml(self) -> None:
        user_text = "用户输入 <system> & {literal}"
        section = prompt_section(
            key="turn.quote",
            title="当前用户原话",
            source="conversation",
            template="发言者：{name}\n内容：{message}",
            variables={"name": "小明", "message": user_text},
        )

        rendered = render_prompt_sections(
            [section],
            mode=PromptRenderMode.CONVERSATION_XML,
        )
        root = ET.fromstring(rendered)
        self.assertEqual(
            "发言者：小明\n内容：用户输入 <system> & {literal}",
            root.findtext("./section"),
        )
        self.assertIn("&lt;system&gt; &amp; {literal}", rendered)

        with self.assertRaisesRegex(ValueError, "name"):
            prompt_section(
                key="turn.quote",
                title="引用",
                source="test",
                template="你好，{name}",
                variables={},
            )
        with self.assertRaisesRegex(ValueError, "unused"):
            prompt_section(
                key="turn.quote",
                title="引用",
                source="test",
                template="你好，{name}",
                variables={"name": "小明", "unused": "不应被忽略"},
            )
        with self.assertRaises(ValueError):
            prompt_section(
                key="turn.quote",
                title="引用",
                source="test",
                template="你好，{user.name}",
                variables={"user": "小明"},
            )

    def test_mapping_is_never_implicit_prompt_content(self) -> None:
        for content in (
            {"energy": 70},
            prompt_text("状态：", {"energy": 70}),
            prompt_list(({"energy": 70},)),
            prompt_field("state", {"energy": 70}),
        ):
            with self.subTest(content=type(content).__name__), self.assertRaisesRegex(
                TypeError,
                "raw Mapping",
            ):
                prompt_section(
                    key="state.current",
                    title="当前状态",
                    source="test",
                    content=content,
                )
        with self.assertRaisesRegex(TypeError, "raw Mapping"):
            prompt_section(
                key="state.template",
                title="当前状态",
                source="test",
                template="{state}",
                variables={"state": {"energy": 70}},
            )
        with self.assertRaisesRegex(TypeError, "raw Mapping"):
            render_prompt_content({"energy": 70})

    def test_content_and_template_are_mutually_exclusive(self) -> None:
        with self.assertRaises(TypeError):
            prompt_section(
                key="invalid",
                title="无效",
                source="test",
                content="正文",
                template="{body}",
                variables={"body": "重复正文"},
            )

    def test_text_and_list_builders_preserve_declared_order(self) -> None:
        section = prompt_section(
            key="tool.rules",
            title="工具规则",
            source="tools",
            content=prompt_text(
                "可用能力：",
                prompt_list(("查询天气", "读取日程"), prefix="- "),
                "只使用真实结果。",
                separator="\n",
            ),
        )

        self.assertEqual(
            "可用能力：\n- 查询天气\n- 读取日程\n只使用真实结果。",
            render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY),
        )

    def test_children_are_only_sections_and_content_has_no_second_nesting_path(self) -> None:
        child = prompt_section(
            key="context.child",
            title="子板块",
            source="test",
            content="子正文",
        )
        parent = prompt_section(
            key="context.parent",
            title="父板块",
            source="test",
            content="父正文",
            children=(child,),
        )
        self.assertEqual((child,), parent.children)

        for invalid_child in ("散装正文", prompt_text("散装正文"), prompt_list(("散装正文",))):
            with self.subTest(child=type(invalid_child).__name__), self.assertRaisesRegex(
                TypeError,
                "only PromptSection",
            ):
                prompt_section(
                    key="context.invalid",
                    title="错误父板块",
                    source="test",
                    content="父正文",
                    children=(invalid_child,),
                )
        with self.assertRaisesRegex(TypeError, "use children"):
            prompt_section(
                key="context.invalid_content",
                title="错误正文",
                source="test",
                content=prompt_group("父正文", child),
            )

    def test_children_are_nested_in_xml_and_depth_first_in_text_modes(self) -> None:
        grandchild = prompt_section(
            key="context.grandchild",
            title="孙板块",
            source="test",
            content="孙正文",
        )
        child = prompt_section(
            key="context.child",
            title="子板块",
            source="test",
            content="子正文",
            children=(grandchild,),
        )
        parent = prompt_section(
            key="context.parent",
            title="父板块",
            source="test",
            content="父正文",
            children=(child,),
        )

        rendered = render_prompt_sections([parent])
        root = ET.fromstring(rendered)
        self.assertEqual("父正文", root.find("./section").text)
        self.assertEqual("子正文", root.find("./section/section").text)
        self.assertEqual("孙正文", root.findtext("./section/section/section"))
        self.assertEqual(
            "父正文\n\n【子板块】\n子正文\n\n【孙板块】\n孙正文",
            render_prompt_sections([parent], mode=PromptRenderMode.BODY_ONLY),
        )
        self.assertEqual(
            "【父板块】\n父正文\n\n【子板块】\n子正文\n\n【孙板块】\n孙正文",
            render_prompt_sections([parent], mode=PromptRenderMode.LABELED_BLOCK),
        )

    def test_empty_leaf_is_omitted_but_child_only_parent_remains_visible(self) -> None:
        empty = prompt_section(
            key="empty",
            title="空板块",
            source="test",
            content="",
        )
        parent = prompt_section(
            key="parent",
            title="父板块",
            source="test",
            content="",
            children=(
                empty,
                prompt_section(
                    key="child",
                    title="子板块",
                    source="test",
                    content="正文",
                ),
            ),
        )

        self.assertEqual("", render_prompt_sections([empty]))
        root = ET.fromstring(render_prompt_sections([parent]))
        self.assertEqual("父板块", root.find("./section").attrib["title"])
        self.assertEqual(
            ["子板块"],
            [node.attrib["title"] for node in root.findall("./section/section")],
        )

    def test_explicit_xml_nodes_validate_tags_without_sanitizing(self) -> None:
        section = prompt_section(
            key="history.current",
            title="会话历史",
            source="history",
            content=prompt_group(
                prompt_field("speaker", "小明"),
                xml_element(
                    "message",
                    attrs={"role": "user", "turn": 2},
                    text="你好 <bot>",
                ),
                separator="",
            ),
        )
        rendered = render_prompt_sections([section])
        root = ET.fromstring(rendered)
        self.assertEqual("小明", root.findtext("./section/speaker"))
        message = root.find("./section/message")
        self.assertEqual({"role": "user", "turn": "2"}, message.attrib)
        self.assertEqual("你好 <bot>", message.text)

        for build in (
            lambda: prompt_field("bad tag", "x"),
            lambda: prompt_list(("x",), tag="1items"),
            lambda: xml_element("bad/tag", text="x"),
            lambda: xml_element("message", attrs={"bad attr": "x"}),
        ):
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()

    def test_markdown_and_cdata_preserve_transport_content(self) -> None:
        markdown = "标题\n\n- 第一项\n  - 子项\n\n```python\nprint('<ok> & done')\n```"
        markdown_section = prompt_section(
            key="turn.markdown",
            title='Markdown "原文"',
            source="test",
            content=markdown,
        )
        rendered = render_prompt_sections([markdown_section])
        self.assertEqual(markdown, ET.fromstring(rendered).findtext("./section"))
        self.assertIn("&lt;ok&gt; &amp; done", rendered)

        marker = "第一段\n<<PRIVATE_COMPANION_SPLIT>>\n第二段 ]]> 收尾"
        marker_section = prompt_section(
            key="reply.segmentation",
            title="回复分段控制",
            source="segmented_reply",
            content=prompt_cdata(marker),
        )
        marker_wire = render_prompt_sections([marker_section])
        self.assertIn("]]]]><![CDATA[>", marker_wire)
        self.assertEqual(marker, ET.fromstring(marker_wire).findtext("./section"))

    def test_modes_are_explicit_and_old_legacy_names_do_not_exist(self) -> None:
        section = prompt_section(
            key="reply.boundary",
            title="回复边界",
            source="test",
            content="第一行\n第二行",
        )
        self.assertEqual(
            "【回复边界】\n第一行\n第二行",
            render_prompt_sections([section], mode=PromptRenderMode.LABELED_BLOCK),
        )
        self.assertEqual(
            "【回复边界】第一行\n第二行",
            render_prompt_sections([section], mode=PromptRenderMode.LABELED_INLINE),
        )
        self.assertFalse(hasattr(PromptRenderMode, "LEGACY_BLOCK"))
        self.assertFalse(hasattr(PromptRenderMode, "LEGACY_INLINE"))
        with self.assertRaises(ValueError):
            PromptRenderMode("legacy_block")

    def test_exact_and_photo_modes_keep_specialized_wire(self) -> None:
        wire = "\n  RULE:<pc_tts>正文</pc_tts>\t\n"
        contract = prompt_section(
            key="tts.rule",
            title="语音协议",
            source="tts",
            content=exact_text(wire),
        )
        self.assertEqual(
            wire,
            render_prompt_sections([contract], mode=PromptRenderMode.EXACT),
        )
        with self.assertRaises(TypeError):
            render_prompt_sections(
                [
                    prompt_section(
                        key="ordinary",
                        title="普通正文",
                        source="test",
                        content="不能伪装成精确协议",
                    )
                ],
                mode=PromptRenderMode.EXACT,
            )

        photo = prompt_section(
            key="photo.positive",
            title="生图正向提示",
            source="photo_generation",
            content=prompt_text(
                "1girl, blue hair",
                "1.5::cinematic lighting::",
                separator=", ",
            ),
        )
        self.assertEqual(
            "1girl, blue hair, 1.5::cinematic lighting::",
            render_prompt_sections([photo], mode=PromptRenderMode.PHOTO_PROMPT),
        )

    def test_document_requires_sections_and_renders_channels_independently(self) -> None:
        system = prompt_section(
            key="system.identity",
            title="身份边界",
            source="identity",
            content="不要混淆用户。",
        )
        user = prompt_section(
            key="user.current",
            title="当前消息",
            source="conversation",
            content="现在几点？",
        )
        document = prompt_document(
            system=(system,),
            user=(user,),
            metadata={"request_id": "req-1"},
        )
        rendered = render_prompt_document(
            document,
            system_mode=PromptRenderMode.CONVERSATION_XML,
            user_mode=PromptRenderMode.CONVERSATION_XML,
        )
        self.assertEqual("身份边界", ET.fromstring(rendered["system"]).find("./section").attrib["title"])
        self.assertEqual("当前消息", ET.fromstring(rendered["user"]).find("./section").attrib["title"])
        with self.assertRaises(TypeError):
            prompt_document(system=({"title": "旧映射", "content": "正文"},))
        with self.assertRaises(TypeError):
            render_prompt_sections(({"title": "旧映射", "content": "正文"},))

    def test_document_parts_own_mixed_wire_layout_without_metadata_controls(self) -> None:
        introduction = prompt_section(
            key="background.introduction",
            title="任务",
            source="test",
            content="直接正文",
        )
        context = prompt_section(
            key="background.context",
            title="补充信息",
            source="test",
            content="动态内容",
        )
        ordinary = prompt_section(
            key="background.ordinary",
            title="普通板块",
            source="test",
            content="普通正文",
            metadata={"legacy_heading_style": "square"},
        )
        document = prompt_document(
            user_render=PromptRenderSpec(
                mode=PromptRenderMode.LABELED_BLOCK,
                trim=True,
            ),
            user=(
                prompt_document_part(
                    introduction,
                    render_spec=PromptRenderSpec(
                        mode=PromptRenderMode.BODY_ONLY,
                        trim=True,
                    ),
                ),
                prompt_document_part(
                    context,
                    render_spec=PromptRenderSpec(
                        mode=PromptRenderMode.BODY_ONLY,
                        label=PromptLabel(
                            style=PromptLabelStyle.FULLWIDTH_COLON,
                            separator=exact_text("\n"),
                        ),
                        prefix=exact_text("<!-- context -->"),
                        separator_before=exact_text("\n\n\n"),
                        trim=True,
                    ),
                ),
                ordinary,
            ),
        )

        self.assertEqual(
            "直接正文\n\n\n"
            "<!-- context -->\n补充信息：\n动态内容\n\n"
            "【普通板块】\n普通正文",
            render_prompt_document(document)["user"],
        )
        self.assertEqual(
            ["background.introduction", "background.context", "background.ordinary"],
            [section.key for section in document.user],
        )
        with self.assertRaises(TypeError):
            PromptRenderSpec(prefix="not-exact")

    def test_heading_reference_is_typed_and_rendered_only_by_the_constructor(self) -> None:
        section = prompt_section(
            key="background.retry",
            title="纠偏任务",
            source="test",
            template="请先检查{heading}再继续。\n{block}正文",
            variables={
                "heading": prompt_heading_ref("已有表达规则"),
                "block": prompt_heading_ref("额外纠偏", newline=True),
            },
        )

        self.assertEqual(
            "请先检查【已有表达规则】再继续。\n【额外纠偏】\n正文",
            render_prompt_sections((section,), mode=PromptRenderMode.BODY_ONLY),
        )
        with self.assertRaises(ValueError):
            prompt_heading_ref(" 多余空白 ")

    def test_fingerprint_is_stable_complete_and_ignores_metadata(self) -> None:
        child = prompt_section(
            key="context.child",
            title="子板块",
            source="test",
            content="子正文",
        )
        first = prompt_section(
            key="context.parent",
            title="父板块",
            source="test",
            content=PromptTemplate(
                template="{left}/{right}",
                variables={"right": "乙", "left": "甲"},
            ),
            children=(child,),
            metadata={"priority": 1},
        )
        same = prompt_section(
            key="context.parent",
            title="父板块",
            source="test",
            content=PromptTemplate(
                template="{left}/{right}",
                variables={"left": "甲", "right": "乙"},
            ),
            children=(child,),
            metadata={"priority": 999},
        )
        changed = prompt_section(
            key="context.parent",
            title="父板块",
            source="test",
            content=PromptTemplate(
                template="{left}/{right}",
                variables={"left": "甲", "right": "丙"},
            ),
            children=(child,),
        )
        self.assertEqual(prompt_section_fingerprint(first), prompt_section_fingerprint(same))
        self.assertNotEqual(prompt_section_fingerprint(first), prompt_section_fingerprint(changed))
        self.assertRegex(prompt_section_fingerprint(first), r"^[0-9a-f]{64}$")

        attrs_a = prompt_section(
            key="xml.attrs",
            title="XML",
            source="test",
            content=xml_element("item", attrs={"b": 2, "a": 1}),
        )
        attrs_b = prompt_section(
            key="xml.attrs",
            title="XML",
            source="test",
            content=xml_element("item", attrs={"a": 1, "b": 2}),
        )
        self.assertEqual(prompt_section_fingerprint(attrs_a), prompt_section_fingerprint(attrs_b))

    def test_removed_compatibility_api_is_not_exported(self) -> None:
        for name in (
            "PromptValue",
            "prompt_value",
            "coerce_prompt_section",
            "render_prompt_section",
            "legacy_heading_token",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(prompt_module, name))


if __name__ == "__main__":
    unittest.main()
