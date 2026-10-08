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


# ---------------------------------------------------------------------------
# 🔴 W5：真 AstrBot 模块别名（跨插件互调）
# ---------------------------------------------------------------------------


def test_real_astrbot_module_alias_is_registered(loaded):
    """插件必须能用**真 AstrBot 的模块路径**被 import 到。

    ## 为什么（W5 的真根因）
    插件之间互相调用的写法是硬编码真 AstrBot 的路径，例如：
        `importlib.import_module("data.plugins.astrbot_plugin_private_companion.main")`
    实测本项目里这类写法 **40 处**。而我们的顶层包名是**目录名本身**
    ⇒ 这些 import 以前 **100% ModuleNotFoundError**，多数调用点靠候选/兜底活着，
    但只写一处的地方就是纯失败（静默降级）。

    ⚠️ 这条判据会真的失败：注释掉 `_register_real_astrbot_alias` 的调用即红。
    """
    import importlib

    alias = "data.plugins.astrbot_plugin_private_companion.main"
    mod = importlib.import_module(alias)  # 拿不到就抛 ModuleNotFoundError = 判据失败
    rec = loaded.records[0]
    # 🔴 必须是**同一个对象**（不是复制品）—— 否则插件里的全局状态会分成两份
    assert mod is rec.module.main, (
        "别名指向了另一个模块对象 —— 插件全局状态会被劈成两份"
    )


def test_alias_supports_from_import(loaded):
    """`from data.plugins.X.main import Y` 这种写法也必须能用。

    只注册 `sys.modules` 条目还不够 —— `from ... import` 会走包属性链，
    所以中间层包必须逐级 `setdefault` 并挂好属性。
    （实测踩到：命名空间包不会自动建中间层条目 → KeyError。）

    ⚠️ **这里比的是「别名模块里的类」而不是 `loaded.records[0].star_cls`。**
       原因（实测踩到，我第一版就栽在这）：插件在**全量跑**时会被重载过
       （`test_compat_regressions.py` 会 `clear()` 注册表并重新加载），
       于是 `rec.star_cls` 可能是**上一次加载**的类对象，而别名模块里是新的
       ⇒ `assert A is B` 报出 `'X' is 'X'` 这种**看着像废话的失败**
       （单跑绿、全量红 —— 正是"判据依赖跨测试漂移的进程级状态"）。
       **不变量应该取「同一时刻现场能取到的」**：别名模块 ⇄ `rec.module.main`。
    """
    import importlib

    alias_mod = importlib.import_module(
        "data.plugins.astrbot_plugin_private_companion.main"
    )
    ns: dict = {}
    exec(
        "from data.plugins.astrbot_plugin_private_companion.main import PrivateCompanionPlugin",
        ns,
    )
    assert ns["PrivateCompanionPlugin"] is alias_mod.PrivateCompanionPlugin
    # 且别名模块必须与记录里的 main 是同一个对象（这才是别名的定义）
    assert alias_mod is loaded.records[0].module.main


def test_alias_does_not_change_canonical_module_name(loaded):
    """别名**不许**改掉插件的规范模块名。

    `star_map` / handler 的 `handler_module_path` 都按规范名索引
    （`entry_module_path()`）；`__name__` 一旦被改，两套索引就脱节了
    —— 那正是"同一概念多条写入路径"。
    """
    rec = loaded.records[0]
    assert rec.module.__name__ == "astrbot_plugin_private_companion"
    assert rec.module.main.__name__ == "astrbot_plugin_private_companion.main"


def test_alias_registers_intermediate_packages_from_scratch(tmp_path_factory):
    """🔴 **别名必须自己逐级建中间包** —— 不复用"别人可能已经建好"的状态。

    ## 为什么单独一条（注入验证抓不到的第三种成因）
    注入「去掉逐级 `setdefault`」在**单跑本文件**时会被抓住，
    但在**全批跑**时抓不住 —— 因为 `data.plugins` 中间包
    **已被前面的测试建好了**，`setdefault` 的缺失就**不再产生可观察差异**。
    ⇒ 判据依赖了**执行顺序**（本项目的老坑）。

    修法：本判据**先把 `data` 相关的 `sys.modules` 条目清掉**，
    从零开始加载 —— 这样"不建中间包"必然暴露。
    ⚠️ 清完在 `finally` 里恢复，别污染同进程的其他测试。

    ⚠️ 这条判据会真的失败：去掉 `_register_real_astrbot_alias` 里那段
       逐级 `setdefault` 即红（**无论执行顺序**）。
    """
    import asyncio
    import importlib

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    alias_pkg = "data.plugins.astrbot_plugin_private_companion"
    alias_main = f"{alias_pkg}.main"
    alias_root = "data.plugins"

    # ① 先清掉**与别名有关**的中间层，模拟"从零开始"的真实安装场景
    saved = {k: sys.modules[k] for k in (alias_main, alias_pkg, alias_root) if k in sys.modules}
    for k in (alias_main, alias_pkg, alias_root):
        sys.modules.pop(k, None)

    try:
        mirror = tmp_path_factory.mktemp("alias-scratch")
        loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)
        rec = asyncio.run(
            loader.load_all(ports=HostPorts(config={}), only=[PLUGIN_NAME])
        ).records[0]
        # ② 中间包必须**由别名逻辑自己建出来**
        assert alias_root in sys.modules, (
            "`data.plugins` 中间包没被建 —— 去掉逐级 setdefault 就会这样"
        )
        mod = importlib.import_module(alias_main)
        assert mod is rec.module.main, "别名没指向同一个模块对象"
    finally:
        # ③ 恢复现场（同进程其他测试还要用）
        for k, v in saved.items():
            sys.modules[k] = v
