# -*- coding: utf-8 -*-
"""真判据：**加载真陪伴插件**（735 个 py / 43 Mixin）。

⭐⭐ 这是整个兼容层最有价值的测试 —— 它不是"我们自己的 hello 插件能跑"，
   而是**第三方真实插件在真 API 面上能加载**。

判据（全部实测得出，不是估计）：
  * 插件类 `PrivateCompanionPlugin` 能被发现并实例化
  * **≥100 个 handler** 注册到**正确的事件类型**
  * **≥15 个 LLM 工具**（`pc_generate_photo` 等）解析出 schema
  * **≥150 个 Web API** 登记
  * `@filter.llm_tool` 的 docstring → JSON Schema 解析成功

⚠️ 依赖磁盘上的真插件 → 用 `skipif` 保护（环境缺插件时跳过而非失败）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

PLUGIN_ROOT = Path("/opt/astrbot/data/plugins")
PLUGIN_NAME = "astrbot_plugin_private_companion"
PLUGIN_DIR = PLUGIN_ROOT / PLUGIN_NAME

pytestmark = pytest.mark.skipif(
    not PLUGIN_DIR.is_dir(),
    reason=f"真插件不存在：{PLUGIN_DIR}（本测试需要磁盘上的陪伴插件）",
)


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """加载真插件一次（module 级复用 —— 加载要几十秒）。"""
    import asyncio

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    # 🔴 必须镜像：`PLUGIN_ROOT` 是别人 AstrBot 的生产目录，
    #    插件会回写自己的源码目录（见 `source_mirror` 模块说明）。
    mirror = tmp_path_factory.mktemp("plugin-src-mirror")
    loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)

    report = asyncio.run(
        loader.load_all(ports=HostPorts(config={}), only=[PLUGIN_NAME])
    )
    assert report.records, "加载器没有发现插件"
    return report


def test_plugin_loads(loaded):
    """插件必须**加载成功**（失败时打印完整堆栈，便于定位缺哪个 API）。"""
    rec = loaded.records[0]
    assert rec.ok, f"插件加载失败：{rec.error}\n{rec.traceback}"


def test_plugin_class_and_instance(loaded):
    rec = loaded.records[0]
    assert rec.star_cls is not None
    assert rec.star_cls.__name__ == "PrivateCompanionPlugin"
    assert rec.instance is not None
    assert type(rec.instance).__name__ == "PrivateCompanionPlugin"


def test_version_from_metadata(loaded):
    """metadata.yaml 要被解析到（版本号可用于诊断）。"""
    assert loaded.records[0].version == "6.6.2"


def test_handler_count(loaded):
    """handler 数量 —— 真插件实测 109 个。"""
    assert loaded.records[0].handler_count >= 100, (
        f"handler 只有 {loaded.records[0].handler_count} 个，"
        "说明装饰器机制没完全生效"
    )


def test_handlers_on_correct_event_types(loaded):
    """⭐ handler 必须落在**正确的事件类型**上（不是"注册了就算"）。"""
    from astrbot_host.registry import star_handlers_registry

    counts = star_handlers_registry.count_by_event()

    # 这些是真插件实测使用的事件类型，各有最低数量要求
    expected_min = {
        "OnDecoratingResultEvent": 20,
        "OnLLMRequestEvent": 15,
        "OnCallingFuncToolEvent": 15,
        "AdapterMessageEvent": 8,
        "OnLLMResponseEvent": 5,
        "OnAfterMessageSentEvent": 5,
    }
    for event, minimum in expected_min.items():
        assert counts.get(event, 0) >= minimum, (
            f"{event} 只有 {counts.get(event, 0)} 个 handler（期望 ≥{minimum}）"
            f"；实际分布：{counts}"
        )


def test_llm_tools_registered_with_schema(loaded):
    """⭐ LLM 工具要被解析出 **JSON Schema**（function calling 的命脉）。"""
    from astrbot_host.registry import llm_tools

    names = llm_tools.names()
    assert len(names) >= 15, f"LLM 工具只有 {len(names)} 个：{names}"

    # 生图工具必须存在且有正确 schema
    photo = llm_tools.get_func("pc_generate_photo")
    assert photo is not None, f"缺少生图工具 pc_generate_photo；实际：{names}"
    schema = photo.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "pc_generate_photo"
    assert schema["function"]["description"], "工具描述不能为空（来自 docstring）"
    props = schema["function"]["parameters"]["properties"]
    assert props, "生图工具应该有参数（来自 docstring 的 Args 段）"


def test_web_apis_registered(loaded):
    """陪伴面板的 Web API 要被登记（190 个）。"""
    rec = loaded.records[0]
    assert rec.web_api_count >= 150, f"Web API 只有 {rec.web_api_count} 个"


def test_plugin_can_read_config(loaded):
    """插件必须能通过 `context.get_config()` 读到配置对象。

    ⚠️ 实测踩到：传 `None` 会让插件 `__init__` 深处 AttributeError。
    """
    inst = loaded.records[0].instance
    cfg = inst.context.get_config()
    assert cfg is not None, "get_config() 不能返回 None"
    assert hasattr(cfg, "get"), "配置对象必须支持 .get()（插件大量使用）"


def test_plugin_logger_works(loaded):
    """插件要有自己的 logger（`Star.__init__` 的副作用）。"""
    inst = loaded.records[0].instance
    assert inst.logger is not None
