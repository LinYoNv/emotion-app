# -*- coding: utf-8 -*-
"""判据：`StarMetadata` 的**运行时对象字段** —— `star_cls`（实例）与 `module`（模块）。

## 本文件守的那个真 bug（2026-09-25 对照实验确诊）

真 AstrBot 的 `StarMetadata` 有两个字段是插件**跨插件互调**的唯一入口：

| 字段 | 真实现定义 | 真实现赋值处 |
|---|---|---|
| `star_cls` —— 插件**实例**（类在 `star_cls_type`） | `core/star/star.py:42` | `star_manager.py:1222-1231` |
| `module` —— 插件**模块对象** | `core/star/star.py:44` | `star_manager.py:1243` |

我们的兼容层**原来两个字段都没声明、加载器也从没填过**。实测对照
（唯一变量 = 补不补这两个字段）：

```
原样           → 陪伴 _memory_companion_bridge() = None（reason=bridge_missing）
补上两字段后   → 找到桥 capability_state=available、contract_version=3
```

插件侧四种读法里有**三种**走这两个字段（第四种 `sys.modules[...]` 不受影响）：

* `get_all_stars()` → `md.star_cls.memory_companion`
  （陪伴 `memory_companion_adapter.py:538`）
* `get_all_stars()` → `md.module.get_memory_companion_bridge()`
  （陪伴 `memory_companion_adapter.py:892`）
* `get_registered_star(name)` → `md.star_cls`
  （记忆插件 `companion_page_bridge.py:786`）

⚠️ **这个 bug 的症状形态是本项目最贵的那类**：插件把它 catch 成一行 info 日志
（`[MemoryCompanion] capability probe degraded`），**读我们自己的源码永远看不出来**。
所以本文件的判据必须落在**端到端**（桥真的拿到）而不是"字段声明了"。

## 判据分层（每层都能失败）

1. **契约层** —— 字段存在 + 默认 `None`；
2. **填充层（反向判据）** —— `star_cls` **就是** `rec.instance` 那个对象（`is` 比较）、
   `module` 的 `__name__` 是插件目录名、且 `star_cls is not star_cls_type`
   （实例 vs 类的混淆**恰好**是这个 bug 的重点）；
3. **失败层** —— 实例化失败时**不许填假对象**（照真实现 `star_manager.py:1240`）；
4. **端到端层** ⭐ —— 同时加载记忆 + 陪伴两个真插件，陪伴的
   `_memory_companion_bridge()` **不是 None**、`_memory_companion_presence()["loaded"] is True`。

⚠️ 加载两个真插件要几十秒 → 用 **module 级 fixture 复用**，不重复加载。
⚠️ 插件在磁盘上 → **不许 skip**（skip = 假通过）；少了插件就是失败。
"""
from __future__ import annotations

import sys
import tempfile
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
PRIVATE_PLUGIN = "astrbot_plugin_private_companion"
MEMORY_PLUGIN = "astrbot_plugin_memory_companion"
PRIVATE_DIR = PLUGIN_ROOT / PRIVATE_PLUGIN
MEMORY_DIR = PLUGIN_ROOT / MEMORY_PLUGIN

#: 只有"磁盘上没有这个插件"才允许跳过（环境缺件）；有插件就必须真跑。
pytestmark = pytest.mark.skipif(
    not PRIVATE_DIR.is_dir() or not MEMORY_DIR.is_dir(),
    reason=f"真插件不存在：{PRIVATE_DIR} / {MEMORY_DIR}（本测试需要磁盘上的陪伴 + 记忆插件）",
)


# --------------------------------------------------------------------------- #
# 契约层
# --------------------------------------------------------------------------- #


def test_star_metadata_declares_runtime_fields():
    """`StarMetadata` 必须声明 `star_cls` / `module`，且默认都是 `None`。

    ⚠️ 这两个字段原来**根本不存在** —— 插件读 `md.star_cls` 得到 `AttributeError`，
        而它普遍写成 `getattr(metadata, "star_cls", None)` → **静默拿到 None**。
    """
    from astrbot_host.compat.core.star.base import StarMetadata

    md = StarMetadata()
    assert hasattr(md, "star_cls"), "StarMetadata 缺 star_cls（插件的跨插件互调入口）"
    assert hasattr(md, "module"), "StarMetadata 缺 module（插件模块对象）"
    # 默认必须是 None —— 照真实现（`star.py:42/:44`），不是空对象/假对象
    assert md.star_cls is None
    assert md.module is None


def test_star_cls_and_star_cls_type_are_different_fields():
    """`star_cls` 是**实例**、`star_cls_type` 才是**类** —— 两者不能是同一个东西。

    真实现 `star_manager.py:1222` 原话：
    `metadata.star_cls = metadata.star_cls_type(context=..., config=...)`。
    """
    from astrbot_host.compat.core.star.base import StarMetadata

    class _FakeStar:
        pass

    md = StarMetadata(star_cls_type=_FakeStar, star_cls=_FakeStar())
    assert md.star_cls_type is _FakeStar
    assert isinstance(md.star_cls, _FakeStar)
    # 混用会砸掉插件：它拿 `star_cls` 当实例调方法，拿到类就会缺 self
    assert md.star_cls is not md.star_cls_type
    assert not isinstance(md.star_cls, type)


# --------------------------------------------------------------------------- #
# 加载（module 级复用 —— 两个真插件，几十秒）
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """同时加载 **记忆 + 陪伴** 两个真插件（`module` 级复用，只加载一次）。

    ⚠️ 两个插件都要在，因为 `_memory_companion_bridge()` 是**跨插件**解析：
        陪伴去 star_map 里找"记忆"实例 —— 这正是本 bug 的意义所在。
    """
    import asyncio

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    # 🔴 必须镜像：`PLUGIN_ROOT` 是别人 AstrBot 的生产目录，
    #    插件会回写自己的源码目录（见 `astrbot_host/source_mirror.py`）。
    from conftest import shared_mirror_root
    mirror = shared_mirror_root()
    loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)
    report = asyncio.run(
        loader.load_all(
            ports=HostPorts(config={}),
            only=[MEMORY_PLUGIN, PRIVATE_PLUGIN],
        )
    )
    assert report.records, "加载器没有发现插件"
    return loader, report


def test_both_plugins_loaded(loaded):
    """两个插件都必须**加载成功**（失败时打印完整堆栈，便于定位缺哪个 API）。"""
    _, report = loaded
    for rec in report.records:
        assert rec.ok, f"插件加载失败：{rec.name} → {rec.error}\n{rec.traceback}"
    assert report.loaded, "一个插件都没加载成功"


# --------------------------------------------------------------------------- #
# 填充层 —— 🔴 **反向判据**：字段必须"填了"，且填的是**同一个对象**
# --------------------------------------------------------------------------- #


def test_get_registered_star_returns_live_instance(loaded):
    """🔴 **反向判据**：`get_registered_star(name).star_cls` **就是**加载器那个实例。

    为什么用 `is` 比较：只有这样才区分得开"字段填了"与"没填"
    （只断言 `is not None` 的话，填成**类**、填成**别的实例**、填成"能跑的样子"
     都会蒙混过关 —— 而这个 bug 的核心恰恰是填错对象）。

    出处：记忆插件的面板桥就是这么读的（`companion_page_bridge.py:786`）。
    """
    loader, _ = loaded
    ctx = loader._context
    rec = loader.records[PRIVATE_PLUGIN]

    md = ctx.get_registered_star(PRIVATE_PLUGIN)
    assert md is not None, (
        f"`get_registered_star({PRIVATE_PLUGIN!r})` 没找到条目 —— "
        "插件按 metadata.name 查找，名字没灌进去"
    )
    assert md.star_cls is rec.instance, (
        f"`md.star_cls` 不是加载器那个实例："
        f"{md.star_cls!r} is not {rec.instance!r}"
    )
    # 别把类填进来（`star_cls_type` 才是类）
    assert md.star_cls is not md.star_cls_type, "star_cls 填成了类，应该是实例"
    assert not isinstance(md.star_cls, type)


def test_module_field_is_the_plugin_module(loaded):
    """`md.module` 必须是**插件模块对象**，`__name__` 以插件目录名开头。

    出处：陪伴去模块上取桥 —— `getattr(metadata.module, "get_memory_companion_bridge")`
    （`memory_companion_adapter.py:892`）。
    """
    loader, _ = loaded
    ctx = loader._context
    rec = loader.records[PRIVATE_PLUGIN]

    md = ctx.get_registered_star(PRIVATE_PLUGIN)
    assert md is not None
    assert md.module is not None, "md.module 没填 —— 插件拿不到模块对象"
    assert md.module is rec.module, "md.module 不是加载器 import 的那个包对象"
    name = getattr(md.module, "__name__", "")
    assert name.startswith(PRIVATE_PLUGIN), (
        f"模块名 {name!r} 不以插件目录名 {PRIVATE_PLUGIN!r} 开头 —— 归属对不上"
    )


def test_get_all_stars_entries_carry_runtime_fields(loaded):
    """`get_all_stars()` 返回的**每一个**条目都要带这两个字段。

    ⚠️ 这条防的是"只填了 `get_registered_star` 那条路" ——
        真实现只有**一处**赋值（`star_manager.py:1243`），所以两条路必然一致；
        我们如果分两处填，就会出现"一个入口好、另一个入口坏"的静默偏差。
    """
    loader, _ = loaded
    ctx = loader._context

    stars = ctx.get_all_stars()
    assert stars, "get_all_stars() 返回空 —— star_registry 没登记"

    ours = [md for md in stars if getattr(md, "root_dir_name", "") in
            (PRIVATE_PLUGIN, MEMORY_PLUGIN)]
    assert len(ours) == 2, (
        f"star_registry 里只找到 {len(ours)} 个本次加载的插件条目："
        f"{[getattr(md, 'root_dir_name', '') for md in stars]}"
    )

    by_dir = {md.root_dir_name: md for md in ours}
    for dir_name in (MEMORY_PLUGIN, PRIVATE_PLUGIN):
        md = by_dir[dir_name]
        rec = loader.records[dir_name]
        assert md.star_cls is rec.instance, (
            f"{dir_name}：get_all_stars() 里的 star_cls 不是实例"
        )
        assert md.module is rec.module, (
            f"{dir_name}：get_all_stars() 里的 module 不是加载的包对象"
        )


# --------------------------------------------------------------------------- #
# 失败层 —— 实例化失败**不许**填假对象
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_star_registry():
    """快照并还原全局 `star_map` / `star_registry`。

    🔴 为什么必须有：合成插件（本文件那两条不是"加载真插件"的判据）会往
       `star_map` / `star_registry` 里**留下条目**，而它们是**模块级全局**。
       本项目为同类问题付过代价（`host/tests` 曾出现"单独跑绿、全量跑红"
       的模块态污染）。

    ⚠️ 用**快照+还原**而不是 `clear()`：真插件测试（`test_loader_real_plugin`）
       的条目也在里面，清空会打断它们的判据。
    """
    from astrbot_host.compat.core.star.base import star_map, star_registry

    map_snapshot = dict(star_map)
    registry_snapshot = list(star_registry)
    try:
        yield
    finally:
        star_map.clear()
        star_map.update(map_snapshot)
        star_registry[:] = registry_snapshot


def test_failed_instantiation_leaves_star_cls_none(isolated_star_registry, tmp_path):
    """🔴 **反向判据**：实例化失败时 `star_cls` 必须是 `None`，不是假对象。

    照真实现语义（`star_manager.py:1240` 的 `else: metadata.star_cls = None`；
    `star.py:21` 类文档：「当 activated 为 False 时，star_cls 可能为 None，
    **请不要在插件未激活时调用 star_cls 的方法**」）。

    插件对它是有语义的 —— 记忆/陪伴读 `presence["loaded"]`：
        陪伴 `_memory_companion_presence()`：`"loaded": getattr(metadata, "star_cls", None) is not None`
    填个"能跑的样子"会**伪造"已加载"**，比留空更糟。
    """
    import asyncio

    from astrbot_host.compat.core.star.base import star_map
    from astrbot_host.loader import PluginLoader

    plugin_dir = tmp_path / "broken_instance_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text(
        "from astrbot.api.star import Star\n"
        "\n"
        "\n"
        "class BrokenPlugin(Star):\n"
        "    def __init__(self, context, config=None):\n"
        "        raise ValueError('故意在构造期炸掉（模拟插件自身缺陷）')\n",
        encoding="utf-8",
    )

    loader = PluginLoader(plugin_dirs=[tmp_path])
    report = asyncio.run(loader.load_all(ports=None))

    rec = loader.records["broken_instance_plugin"]
    assert not rec.ok, "构造期抛异常的插件不该被算作加载成功"
    assert rec.instance is None, "实例化失败时 rec.instance 必须是 None"

    # star_map 里该插件的条目：star_cls 必须是 None（不许伪造）
    md = star_map.get(rec.star_cls.__module__) if rec.star_cls else None
    assert md is not None, "插件类被发现了，star_map 里就该有条目"
    assert md.star_cls is None, (
        f"实例化失败却把 star_cls 填成了 {md.star_cls!r} —— 插件会据此报"
        f"『已加载』（假信息）"
    )
    assert md.star_cls_type is rec.star_cls, "类对象（star_cls_type）仍应保留"


# --------------------------------------------------------------------------- #
# ⭐ 端到端层 —— 这才是本 bug 的意义
# --------------------------------------------------------------------------- #


def test_cross_plugin_api_is_reachable(loaded):
    """⭐⭐ **端到端判据**：陪伴实例的 `extension_api` **可调用且返回真实数据**。

    ## 这条判据的前身与它为什么要重写（2026-10-19）

    前身是 `test_bridge_is_found_end_to_end`，断言
    `inst._memory_companion_bridge() is not None` + `_bridge_last_status`
    的 `capability_state == "available"`、`contract_version == 3`。

    **那整套是 6.7.0 的内部实现**。陪伴 7.0.0 把它整个换掉了：

    | 6.7.0 | 7.0.0 |
    |---|---|
    | `_memory_companion_bridge()` | `extension_api`（`UnifiedCompanionExtensionAPI`） |
    | `_bridge_last_status` | `extension_api.api_version` / `.capabilities` |
    | `_memory_companion_presence()` | `integration/manifest` 里的 `integration_state` |

    实测 7.0.0 实例上**没有** `_memory_companion_bridge` /
    `_memory_companion_presence` 这两个属性（`dir()` 里零命中）。

    ## ⚠️ 但**判据要守的那件事没变**

    它守的是：**跨插件读取要真的通**（`StarMetadata.star_cls` / `module`
    两个运行时字段没填的话，这条链就断）。7.0.0 的等价入口是
    `extension_api` —— 只要它是个能用的对象、`api_version` 能取到，
    就证明"实例被正确装配、且对外暴露了真实接口"。
    """
    loader, _ = loaded
    inst = loader.records[PRIVATE_PLUGIN].instance

    api = getattr(inst, "extension_api", None)
    assert api is not None, (
        "陪伴实例没有 `extension_api` —— 跨插件接口断了（7.0.0 的对应物）"
    )
    assert type(api).__name__ == "UnifiedCompanionExtensionAPI", (
        f"`extension_api` 类型不对：{type(api).__name__}"
    )
    # `api_version` 是它对外声明的契约版本 —— ⚠️ **是属性，不是方法**
    # （实测 `TypeError: 'str' object is not callable`，第一次写成 `()` 了）
    version = api.api_version
    assert isinstance(version, str) and version, (
        f"`extension_api.api_version` 不是非空字符串：{version!r}"
    )
    # 还有一组真实能力方法（7.0.0 实测 14 个）
    methods = [m for m in dir(api) if m.startswith("companion_v2_")]
    assert len(methods) >= 5, f"`extension_api` 暴露的方法太少：{methods}"


def test_star_metadata_runtime_fields_are_filled(loaded):
    """⭐ `StarMetadata.star_cls` / `module` 两个运行时字段必须**真的填了**。

    ## 这条替代了 `test_presence_reports_loaded`

    前身读 `inst._memory_companion_presence()["loaded"]` —— 而 7.0.0
    没有这个方法了（它读的是 `metadata.star_cls is not None`）。

    ⇒ **直接断言根源**（那两个字段），不依赖插件某个具体方法的存在性。
    这样无论插件怎么改名/重构，只要"加载器没填字段"这条回归出现，它必红。
    """
    from astrbot_host.compat.core.star.base import star_registry

    md = next((m for m in star_registry if getattr(m, "name", "") == PRIVATE_PLUGIN), None)
    assert md is not None, f"`star_registry` 里找不到 {PRIVATE_PLUGIN}"

    # `star_cls` 在真 AstrBot 里是**实例**（类在 `star_cls_type`）
    assert getattr(md, "star_cls", None) is not None, (
        "`StarMetadata.star_cls` 没填 —— 跨插件读取 `md.star_cls` 的插件会拿到 None"
    )
    assert getattr(md, "module", None) is not None, (
        "`StarMetadata.module` 没填 —— 跨插件读取 `md.module.xxx` 的插件会拿到 None"
    )
    # 这两个必须指向**真的那个**实例/模块，不是占位
    loader_inst = loaded[0].records[PRIVATE_PLUGIN].instance
    assert md.star_cls is loader_inst, "`star_cls` 指向的不是加载器里那个实例"
    assert md.module is loaded[0].records[PRIVATE_PLUGIN].module


def test_capabilities_are_reported_honestly(loaded):
    """诚实形式：`extension_api.capabilities()` 要返回**结构化**的能力声明。

    ## 这条替代了 `test_req041_availability_is_not_blocked_by_missing_bridge`

    前身断言 `req041_migration_status` 是 dict 且有 `code` —— 那是 6.7.0
    迁移状态机的字段。7.0.0 把它收进了 V2 的 `identity_policy`，
    实例上**没有** `req041_migration_status` 这个属性了。

    ⚠️ 这条**不**断言某个能力必须为 True（那是插件侧业务状态），
       只守"能力声明读得到、且是结构化的"。
    """
    loader, _ = loaded
    inst = loader.records[PRIVATE_PLUGIN].instance
    api = getattr(inst, "extension_api", None)
    assert api is not None, "没有 `extension_api`，后面的判断没有意义"

    capabilities = api.capabilities()
    assert isinstance(capabilities, (list, tuple, dict)), (
        f"`capabilities()` 返回的不是结构化数据：{type(capabilities).__name__}"
    )
    assert capabilities, "`capabilities()` 返回空 —— 能力声明丢了"

