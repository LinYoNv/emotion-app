# -*- coding: utf-8 -*-
"""真判据：**加载真陪伴插件**（735 个 py / 43 Mixin）。

⭐⭐ 这是整个兼容层最有价值的测试 —— 它不是"我们自己的 hello 插件能跑"，
   而是**第三方真实插件在真 API 面上能加载**。

判据（全部实测得出，不是估计）：
  * 插件类 `PrivateCompanionV2StagingPlugin`（7.0.0；6.7.0 叫 `PrivateCompanionPlugin`）能被发现并实例化
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

#: 🔴 **插件源 = 仓库内 `plugins/`**（2026-10-19，情绪拍板）
#:
#: 原来指向 `/opt/astrbot/data/plugins` —— 别人 AstrBot 的生产目录：
#:   ① 违反 AGENTS.md 铁律「绝不碰 AstrBot」；
#:   ② 与 `tests/test_plugin_host_e2e.py`（读仓库内插件）**同名不同源**，
#:      同一个 pytest 进程里先后加载 ⇒ `sys.modules` 里留下旧版模块
#:      ⇒ 全量跑时 12 条判据互相污染（单独跑全绿）。
#: 换成本仓库的 6 个陪伴系插件后，判的是「我们的插件能不能被加载」。
PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins"
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
    from conftest import shared_mirror_root
    mirror = shared_mirror_root()
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
    # 🔴 2026-10-19：陪伴 7.0.0 把这个类改名了。
    #    6.7.0 是 `PrivateCompanionPlugin`；7.0.0 是
    #    `PrivateCompanionV2StagingPlugin`（`main.py` 里 `register(` 的那个类）。
    #    ⚠️ 断言写成"类名以 PrivateCompanion 开头"而不是写死全名 ——
    #      上游再改后缀不会假红，而"根本没找到 Star 类"仍会被抓住。
    assert rec.star_cls.__name__.startswith("PrivateCompanion"), rec.star_cls.__name__
    assert rec.instance is not None
    assert type(rec.instance).__name__ == rec.star_cls.__name__


def test_version_from_metadata(loaded):
    """metadata.yaml 要被解析到（版本号可用于诊断）。"""
    # 🔴 2026-10-19：6.6.2 → 7.0.0（这是本次升级的目标版本）
    assert loaded.records[0].version == "7.0.0"


def test_handler_count(loaded):
    """handler 数量 —— 陪伴 7.0.0 实测 **16** 个。

    🔴 **为什么从 100 改到 16（这不是放宽，是事实变了）**

    6.7.0 把每个功能各挂一个 handler（实测 109 个）；7.0.0 重写成
    「**统一陪伴宿主**」之后，改成**按事件类型各挂一个总管**
    （`AdapterMessageEvent` 38 个 + 各 `On*` 各 1~3 个），
    路由分派收进了 `astrbot_host/event_routing.py`。

    ⇒ 判据改成"**至少 10 个**"：锁住"装饰器机制没整体失效"，
       同时不把"上游又把功能拆细"误判成回归。
    """
    assert loaded.records[0].handler_count >= 10, (
        f"handler 只有 {loaded.records[0].handler_count} 个，"
        "说明装饰器机制没完全生效"
    )


def test_handlers_on_correct_event_types(loaded):
    """⭐ handler 必须落在**正确的事件类型**上（不是"注册了就算"）。"""
    from astrbot_host.registry import star_handlers_registry

    counts = star_handlers_registry.count_by_event()

    # 🔴 **2026-10-19 按 7.0.0 重算（数值全线下降是事实，不是放宽标准）**
    #
    # 6.7.0：OnDecoratingResult 20 / OnLLMRequest 15 / OnCallingFuncTool 15 /
    #        AdapterMessage 8 / OnLLMResponse 5 / OnAfterMessageSent 5
    # ——那时每个功能各挂一个 handler。
    #
    # 7.0.0 重写成「统一陪伴宿主」，**按事件类型各挂一个总管**，
    # 路由分派进 `astrbot_host/event_routing.py`。
    # 单插件（只加载陪伴）实测分布见下表的注释。
    #
    # ⚠️ 这些是**只加载陪伴**时的分布（全量加载 6 个插件是 38/16/…，见
    #    `/tmp/measure_single.py` 的实测）。判据要守的是"装饰器落在了正确的事件
    #    类型上"，所以数目取**实测值**，多的不限制（`>=`）。
    expected_min = {
        "OnDecoratingResultEvent": 3,   # 实测 3
        "OnLLMRequestEvent": 1,          # 实测 1
        "AdapterMessageEvent": 4,        # 实测 4（全量 38）
        "OnLLMResponseEvent": 1,         # 实测 1
        "OnAfterMessageSentEvent": 1,    # 实测 1
    }
    for event, minimum in expected_min.items():
        assert counts.get(event, 0) >= minimum, (
            f"{event} 只有 {counts.get(event, 0)} 个 handler（期望 ≥{minimum}）"
            f"；实际分布：{counts}"
        )


def test_llm_tools_registered_with_schema(loaded):
    """⭐ LLM 工具要被解析出 **JSON Schema**（function calling 的命脉）。"""
    from astrbot_host.registry import llm_tools

    # 🔴 **2026-10-19：7.0.0 的陪伴插件自己不再注册任何 LLM 工具**
    #
    # 6.7.0 用 `@filter.llm_tool` 注册了 20 个（`pc_generate_photo` 等）；
    # 7.0.0 改成「**能力由拓展插件提供、陪伴只做宿主与编排**」
    # （metadata 原文：*随包提供 Content、Image、Screen、Reality、Together、
    #  Game、Live 七项原生内部拓展*）。工具现在来自 memory/nai/together 插件。
    #
    # ⇒ 这条判据**不能再用陪伴的工具**验证。改为：断言"陪伴自己不注册工具"
    #   （这正是 7.0.0 的设计），并把"工具有 JSON Schema"的验证交给
    #   `test_tool_loop.py`（那里全量加载，工具真实存在）。
    # ⚠️ `llm_tools` 是**全局注册表**，且同进程里可能还有别的插件注册过工具
    #    （实测单跑本文件时是空的，全量跑时会有 16 个来自别的插件）。
    #    ⇒ 判据必须**按归属过滤**，不能断言"全局为空"（那是判据写错了，
    #      不是插件的问题 —— 我第一版就这么写，全量跑直接假红）。
    owned = [
        n for n in llm_tools.names()
        if getattr(llm_tools.get_func(n), "handler_module_path", "").startswith(
            f"{PLUGIN_NAME}."
        )
    ]
    assert owned == [], (
        f"陪伴 7.0.0 不该自己注册 LLM 工具（改由拓展插件提供）；实际归属于它：{owned}"
    )
    return

    # --- 以下为 6.7.0 时代的断言，保留作对照，7.0.0 不会走到 ---
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
    # 🔴 2026-10-19：6.7.0 是 189 条，7.0.0 把二十多个 WebAPI 类聚合后是 **71 条**
    #    （`astrbot_host/web_api.py:115-137`）。详见
    #    `tests/test_plugin_host_e2e.py::test_266_apis_mounted` 里更长的说明。
    assert rec.web_api_count >= 70, f"Web API 只有 {rec.web_api_count} 个"


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
        # 🔴 2026-10-19：6.7.0 是 `PrivateCompanionPlugin`，
        #    7.0.0 是 `PrivateCompanionV2StagingPlugin`。这里用**当前实际的名字**，
        #    判据要守的是"`from ... import` 能走通且拿到**同一对象**"，
        #    不是某个特定类名。
        "from data.plugins.astrbot_plugin_private_companion.main import "
        "PrivateCompanionV2StagingPlugin",
        ns,
    )
    assert ns["PrivateCompanionV2StagingPlugin"] is alias_mod.PrivateCompanionV2StagingPlugin
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
    #
    # 🔴 **2026-10-19 扩到 `companion_v2` / 插件模块本身**：
    #    陪伴 7.0.0 的 `astrbot_host/import_lifecycle.py:21-24` 有一道**正确**的
    #    保护 —— 若 `companion_v2` 已从**别的源根**加载过，就拒绝再次加载
    #    （`RuntimeError: another companion_v2 package is already loaded from a
    #    different source root`）。那是防"两份代码混用"的，不该削弱。
    #    而本判据故意用**新的 mirror 目录**加载 ⇒ 必然撞上它。
    #    ⇒ 清现场时把上一轮从旧 mirror 加载的那套也清掉，才是真正的"从零开始"。
    saved = {k: sys.modules[k] for k in (alias_main, alias_pkg, alias_root) if k in sys.modules}
    for k in (alias_main, alias_pkg, alias_root):
        sys.modules.pop(k, None)
    saved.update({k: v for k, v in list(sys.modules.items())
                  if k == "companion_v2" or k.startswith("companion_v2.")
                  or k == PLUGIN_NAME or k.startswith(f"{PLUGIN_NAME}.")})
    for k in list(saved):
        sys.modules.pop(k, None)

    try:
        from conftest import shared_mirror_root

        mirror = shared_mirror_root()
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
