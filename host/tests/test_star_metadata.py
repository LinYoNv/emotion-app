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

PLUGIN_ROOT = Path("/opt/astrbot/data/plugins")
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
    mirror = tmp_path_factory.mktemp("star-meta-src-mirror")
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


def test_bridge_is_found_end_to_end(loaded):
    """⭐⭐ **端到端判据**：陪伴实例的 `_memory_companion_bridge()` **不是 None**。

    这是本 bug 的最终验收 —— 它同时经过
    `get_all_stars()` → `md.star_cls` / `md.module` 两条读法。

    ⚠️ **不许 skip**：插件在磁盘上、也加载成功了，桥拿不到就是**失败**。
    """
    loader, _ = loaded
    inst = loader.records[PRIVATE_PLUGIN].instance

    bridge = inst._memory_companion_bridge()
    assert bridge is not None, (
        "陪伴拿不到记忆插件的桥 —— `StarMetadata` 的运行时字段又没填了。\n"
        f"最后状态：{getattr(inst, '_bridge_last_status', {})}"
    )
    # 桥要**可用**，不只是"非 None"（`available=False` 的桥等于没桥）
    status = dict(getattr(inst, "_bridge_last_status", {}))
    assert status.get("reason") != "bridge_missing", (
        f"桥找到了但状态说 bridge_missing（自相矛盾）：{status}"
    )
    assert status.get("available") is True, f"桥不可用：{status}"
    assert status.get("capability_state") == "available", (
        f"capability_state 不是 available：{status.get('capability_state')!r}"
    )
    # 契约版本 —— 实测补字段后是 3
    assert str(status.get("contract_version") or "") == "3", (
        f"契约版本不是 3：{status.get('contract_version')!r}"
    )


def test_presence_reports_loaded(loaded):
    """⭐ `_memory_companion_presence()["loaded"] is True`。

    它读的就是 `metadata.star_cls is not None` —— 字段没填时恒为 `False`，
    而面板上表现为"记忆插件没装/没加载"（**误诊**）。
    """
    loader, _ = loaded
    inst = loader.records[PRIVATE_PLUGIN].instance

    # 先拿一次桥，让 `_bridge_last_status` 有值（与产品运行顺序一致）
    inst._memory_companion_bridge()
    presence = inst._memory_companion_presence()

    assert presence["loaded"] is True, (
        f"陪伴认为记忆插件『未加载』：{presence}\n"
        "→ 根因在 `StarMetadata.star_cls` 没填（它读 `star_cls is not None`）"
    )
    assert presence["detected"] is True, f"没检测到记忆插件：{presence}"
    assert presence["installed"] is True, f"没检测到已安装：{presence}"
    assert presence["activated"] is True, f"插件未被激活：{presence}"


def test_req041_availability_is_not_blocked_by_missing_bridge(loaded):
    """诚实形式：`_req041_scoped_archive_available()` 的真值目前是 `False`，
    **但原因必须是插件自己的业务状态**（迁移未开始 / 同步器未初始化），
    **不能**是 `bridge_missing`。

    ⚠️ 这条**不**断言它必须为 True —— 那是插件侧业务状态（迁移 paused/未开始），
        不是本轮要修的东西。本轮要守的是"**桥找得到**"。
    """
    loader, _ = loaded
    inst = loader.records[PRIVATE_PLUGIN].instance

    bridge = inst._memory_companion_bridge()
    assert bridge is not None, "桥都没拿到，后面的判断没有意义"

    # 桥在 → 这个能力项即便为 False，也必须是插件自己的迁移状态所致
    status = getattr(inst, "req041_migration_status", None)
    assert isinstance(status, dict), (
        f"`req041_migration_status` 不是 dict：{status!r} —— 迁移状态读不到"
    )
    assert str(status.get("code") or ""), (
        f"迁移状态没有 code，无法解释 `_req041_scoped_archive_available()` 为何为 False：{status}"
    )

    # 反向：桥不可用时它会因为"没桥"而不可用 —— 这条区分得开两种 False
    last = dict(getattr(inst, "_bridge_last_status", {}))
    assert last.get("reason") != "bridge_missing", (
        f"`_req041` 相关的不可用原因里出现了 bridge_missing：{last}"
    )
