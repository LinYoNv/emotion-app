# -*- coding: utf-8 -*-
"""影子包与加载器的行为测试（用自带的 hello 插件）。

判据（覆盖容易写错、且错了会静默失效的地方）：
  * 影子包安装/卸载 + **拒绝覆盖真 astrbot**
  * 5 类装饰器都能注册
  * `@filter.llm_tool` 的 docstring → JSON Schema
  * `@filter.command` 的**参数解析**（含必填缺失报错）
  * `EventMessageType.ALL` 是**组合值**（位值 7，不是独立位）
  * handler 排序按 priority **降序**
  * `LLMResponse.completion_text` 的 **property 语义**（优先 result_chain）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
HELLO_DIR = HOST_DIR / "examples"


@pytest.fixture(scope="module", autouse=True)
def _shadow():
    """安装影子包（module 级）。"""
    from astrbot_host.shadow import install_shadow_package, uninstall_shadow_package

    uninstall_shadow_package()
    assert install_shadow_package() is True
    yield
    uninstall_shadow_package()


# ---------------------------------------------------------------------------
# 影子包
# ---------------------------------------------------------------------------


def test_shadow_install_is_idempotent():
    from astrbot_host.shadow import install_shadow_package

    # 已装 → 返回 False（不重复注册）
    assert install_shadow_package() is False


def test_shadow_refuses_to_clobber_foreign_astrbot(monkeypatch):
    """🔴 真 astrbot 已占用时必须**拒绝**，不能静默覆盖。

    静默覆盖会让"到底跑在谁上面"不可知 —— 比报错贵得多。
    """
    import types

    from astrbot_host.shadow import install_shadow_package, uninstall_shadow_package

    uninstall_shadow_package()
    fake = types.ModuleType("astrbot")
    fake.__file__ = "/somewhere/real/astrbot/__init__.py"
    sys.modules["astrbot"] = fake
    try:
        with pytest.raises(RuntimeError, match="已被占用"):
            install_shadow_package()
        # force 可以覆盖（显式意图）
        assert install_shadow_package(force=True) is True
    finally:
        uninstall_shadow_package()
        install_shadow_package()


def test_core_api_surface_importable():
    """插件最常用的 import 路径必须全通。"""
    import importlib

    for mod in (
        "astrbot.api", "astrbot.api.star", "astrbot.api.event",
        "astrbot.api.event.filter", "astrbot.api.message_components",
        "astrbot.api.provider", "astrbot.core", "astrbot.core.star",
        "astrbot.core.star.star", "astrbot.core.star.star_handler",
        "astrbot.core.star.register", "astrbot.core.platform.astrbot_message",
        "astrbot.core.provider.entities", "astrbot.core.db.po",
        "astrbot.core.agent.message", "astrbot.core.file_token_service",
    ):
        importlib.import_module(mod)


def test_same_class_across_import_paths():
    """🔴 两条 import 路径必须拿到**同一个类**（否则 isinstance 跨模块失败）。"""
    from astrbot.api.message_components import Plain as A
    from astrbot.core.message.components import Plain as B

    assert A is B


# ---------------------------------------------------------------------------
# 装饰器与事件总线
# ---------------------------------------------------------------------------


def test_fourteen_decorators_exported():
    """14 个装饰器（实测清单）必须都在。"""
    from astrbot.api.event import filter

    required = [
        "llm_tool", "on_decorating_result", "on_llm_request",
        "event_message_type", "on_llm_response", "after_message_sent",
        "command", "on_agent_done", "permission_type", "on_agent_begin",
        "command_group", "on_plugin_loaded", "on_plugin_unloaded",
        "on_llm_tool_respond",
    ]
    missing = [n for n in required if not hasattr(filter, n)]
    assert not missing, f"缺少装饰器：{missing}"


def test_event_type_has_16_members():
    """EventType 必须 16 个成员（少一个会让插件 import 失败）。"""
    from astrbot_host.registry import EventType

    assert len(list(EventType)) == 16


def test_event_message_type_all_is_composite():
    """🔴 `ALL` 必须是**组合值**（位值 7），不是独立位 —— 实测踩过。"""
    from astrbot.core.star.filter.event_message_type import EventMessageType

    assert EventMessageType.ALL == (
        EventMessageType.GROUP_MESSAGE
        | EventMessageType.PRIVATE_MESSAGE
        | EventMessageType.OTHER_MESSAGE
    )
    assert EventMessageType.ALL.value == 7


def test_handler_priority_descending():
    """🔴 排序是 **priority 降序**（大者先）—— 真实现如此，`__lt__` 反而是升序。"""
    from astrbot_host.registry import EventType, StarHandlerMetadata, StarHandlerRegistry

    reg = StarHandlerRegistry()

    def mk(name: str, prio: int):
        return StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name=name, handler_name=name,
            handler_module_path="t", handler=mk,
            extras_configs={"priority": prio},
        )

    reg.append(mk("low", 1))
    reg.append(mk("high", 10))
    reg.append(mk("mid", 5))
    order = [h.handler_name for h in reg.all_handlers()]
    assert order == ["high", "mid", "low"], f"排序错：{order}"


# ---------------------------------------------------------------------------
# 指令解析（含失败路径）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hello(loaded_loader):
    return loaded_loader


@pytest.fixture(scope="module")
def loaded_loader():
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader

    loader = PluginLoader(plugin_dirs=[HELLO_DIR])
    report = asyncio.run(
        loader.load_all(ports=HostPorts(config={"hi": 1}), only=["hello_plugin"])
    )
    assert report.loaded, f"hello 插件加载失败：{report.summary()}"
    asyncio.run(loader.initialize_all(report))
    return report


def test_llm_tool_schema_from_docstring(loaded_loader):
    """docstring 的 `Args:` 要被解析成 JSON Schema。"""
    from astrbot_host.registry import llm_tools

    tool = llm_tools.get_func("get_greeting_count")
    assert tool is not None
    schema = tool.to_openai_schema()
    assert schema["function"]["description"] == "查询已经打过多少次招呼。"


def test_llm_tool_with_params(loaded_loader):
    """带参数的工具有正确的 properties 与 required。"""
    from astrbot_host.registry import llm_tools

    # hello 插件的工具无参；用一个含参的验证解析（内联构造）
    from astrbot_host.compat.core.star.register.star_handler import register_llm_tool

    @register_llm_tool(name="_t_probe")
    async def probe(event, city: str, days: int = 3):
        """查天气。

        Args:
            city(string): 城市名
            days(number): 天数
        """
        return "ok"

    spec = llm_tools.get_func("_t_probe")
    props = spec.to_openai_schema()["function"]["parameters"]["properties"]
    assert props["city"]["type"] == "string"
    assert props["city"]["description"] == "城市名"
    assert props["days"]["type"] == "number"
    llm_tools.remove("_t_probe")


def test_command_missing_required_param_raises(loaded_loader):
    """🔴 必填参数缺失要**抛 ValueError**（照真实现），不静默给默认值。

    ⚠️ 判据来源：用**真 AstrBot** 对拍确认过 ——
       真实现 `validate_and_convert_params([], {...})` 会抛
       `ValueError: 必要参数缺失。该指令完整参数: ...`。

    ⚠️ 这里**不能**用 hello 插件的 `hello_cmd` 测 ——
       它的参数有默认值（`name: str = "世界"`），所以不触发该分支。
       （第一版测试就是这么写错的：以为"没传参"就等于"必填缺失"。）
    """
    import inspect

    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

    async def handler(self, event, keyword: str, limit: int = 5):
        """测试用 handler。

        Args:
            keyword(string): 关键词
            limit(int): 数量
        """

    md = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name="t", handler_name="handler",
        handler_module_path="t", handler=handler, event_filters=[],
    )
    flt = CommandFilter("测试", None, md)

    # 参数表：必填的是类型，可选的是默认值
    assert flt.handler_params == {"keyword": str, "limit": 5}
    assert flt.print_types() == "keyword(str),limit(int)=5"

    with pytest.raises(ValueError, match="必要参数缺失"):
        flt.validate_and_convert_params([], flt.handler_params)

    # 有值时正常转换（int 会被转成 int）
    got = flt.validate_and_convert_params(["猫", "3"], flt.handler_params)
    assert got == {"keyword": "猫", "limit": 3}
    assert isinstance(got["limit"], int), "int 参数必须真的转成 int"


def test_command_parses_and_executes(loaded_loader):
    """⭐ 端到端：指令 → 参数解析 → **真的调用插件方法** → 拿到产出。"""
    from astrbot.api.event import AstrMessageEvent
    import astrbot.core.star.star_handler as sh
    from astrbot_host.registry import star_handlers_registry

    hs = star_handlers_registry.get_handlers_by_event_type(sh.EventType.AdapterMessageEvent)
    h = next(x for x in hs if x.handler_name == "hello_cmd")

    ev = AstrMessageEvent(message_str="你好 芙芙")
    ev.is_at_or_wake_command = True
    assert h.event_filters[0].filter(ev, None) is True
    assert ev.get_extra("parsed_params") == {"name": "芙芙"}

    # 🔴 照真实现的调用方式：**只传 event**，不手动传实例。
    #    真实现 `pipeline/context_utils.py:37` 就是 `handler(event, *args, **kwargs)`；
    #    插件实例是**加载时**由 `functools.partial` 绑上去的
    #    （`star_manager.py:1268`，我们对应 `PluginLoader._bind_handlers_to_instance`）。
    #    ⚠️ 本测试原来写的是 `h.handler(inst, ev, ...)` —— 那是在**手工补偿**
    #       我们当时"没绑定"的缺口。绑定补上后，再传 inst 就会
    #       `got multiple values for argument 'name'`（实测踩到）。
    async def run():
        out = []
        async for chunk in h.handler(ev, **ev.get_extra("parsed_params")):
            out.append(str(chunk))
        return out

    out = asyncio.run(run())
    assert out and "芙芙" in out[0], f"插件产出异常：{out}"


# ---------------------------------------------------------------------------
# provider entities（易写错的 property 语义）
# ---------------------------------------------------------------------------


def test_llm_response_prefers_result_chain():
    """🔴 `completion_text` 读时**优先 result_chain**（实测踩过这个坑）。"""
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astr_message_event import MessageChain
    from astrbot.core.provider.entities import LLMResponse

    r = LLMResponse(role="assistant", completion_text="私有字段")
    assert r.completion_text == "私有字段"

    r2 = LLMResponse(
        role="assistant",
        result_chain=MessageChain([Plain("链里的")]),
        completion_text="会被忽略",
    )
    assert r2.completion_text == "链里的", "有 result_chain 时必须优先读链"


def test_llm_response_setter_rewrites_chain():
    """setter 在有链时**改写链**（清 Plain 再插到最前），照真实现。"""
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astr_message_event import MessageChain
    from astrbot.core.provider.entities import LLMResponse

    chain = MessageChain([Plain("旧的")])
    r = LLMResponse(role="assistant", result_chain=chain)
    r.completion_text = "新的"
    assert r.completion_text == "新的"
    assert len(chain.chain) == 1, f"旧 Plain 应被清掉：{chain.chain}"


def test_provider_request_fields():
    """`ProviderRequest` 字段名必须准（提示词注入靠它）。"""
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest(system_prompt="你是芙芙", contexts=[{"role": "user", "content": "hi"}])
    assert req.system_prompt == "你是芙芙"
    assert req.contexts[0]["content"] == "hi"


def test_config_is_a_mapping(loaded_loader):
    """插件配置必须是可 `.get()` 的映射（传 None 会让插件深处崩）。"""
    inst = loaded_loader.loaded[0].instance
    cfg = inst.context.get_config()
    assert cfg is not None and hasattr(cfg, "get")


def test_loader_failure_records_traceback():
    """加载失败要**保留完整堆栈**（不然没法排查缺哪个 API）。"""
    import tempfile

    from astrbot_host.loader import PluginLoader

    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "broken_plugin"
        bad.mkdir()
        (bad / "main.py").write_text(
            "import astrbot_core_that_does_not_exist\n", encoding="utf-8"
        )
        loader = PluginLoader(plugin_dirs=[td])
        report = asyncio.run(loader.load_all())
        assert report.failed, "坏插件应当被记为失败"
        rec = report.failed[0]
        assert rec.error, "要有错误摘要"
        assert "ModuleNotFoundError" in rec.error
        assert rec.traceback, "要保留完整堆栈"
