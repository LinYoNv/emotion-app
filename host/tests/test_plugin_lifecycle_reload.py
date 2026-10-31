# -*- coding: utf-8 -*-
"""热重载（`reload_plugin`）判据 —— 「重载真的换掉了代码」这件事的证明。

## 为什么这一组特别容易写假

「重载成功」有三种**看起来都对**的假象：

1. **handler 数量没变** —— 数量相等 ≠ 组成正确。旧 handler 没摘干净时，
   `handler_count` 反而**更大**（旧的 + 新的），没人会当它是错的；
2. **实例换了 id，但代码还是旧的** —— 这就是本项目 2026-10-31 实测到的真 bug：
   `_purge_plugin_modules` 的默认判据是"来源目录变了才清"，而重载**同一个目录**
   时 `__file__` 本来就指向这个根 ⇒ 一条都不清 ⇒ `load_one` 走 `reload` 分支
   ⇒ **子模块根本不重新执行**。现场：
       `helper.py` 从 `VERSION_TAG='OLD'` 改成 `'NEW'`
       重载前 `instance.tag = OLD` → 重载后 `instance.tag = **OLD**`
   ⇒ 所以本文件用「**子模块**里的常量」作判据，而不是只改 `main.py`
     （只改 `main.py` 时 `reload` 会重跑顶层模块，假象照样绿）；
3. **重载失败了，但接口返回成功** —— 检查"记录被删掉了"就会错过
   "记录没了" vs "记录还在且状态 failed" 的区别。

## 判据的两条硬要求

* `handler_count` **必须等于注册表里的实际条数**（不是大于等于）；
* 失败时**旧记录必须换成 failed 状态而不是消失** —— 消失会让面板显示
  "没有这个插件"，用户以为被删了。
"""

from __future__ import annotations

import asyncio

import pytest

from _plugin_lifecycle_fixtures import (  # noqa: E402
    PROBE_PLUGIN,
    build_host,
    data_root,  # noqa: F401
    owned_handlers_of,
    registry_snapshot,
    source_root,  # noqa: F401
    SYNTHETIC_PLUGINS,
    teardown_host,
    three_plugins,  # noqa: F401
    two_plugins,  # noqa: F401
    web_api_routes,
    write_plugin,
)

#: 一条**带子模块**的插件：`main.py` 从 `.helper` 读常量。
#:
#: 🔴 为什么必须用子模块：`importlib.reload` 只重跑**顶层**模块，子模块不重新执行
#:    （官方文档原文：*submodules ... are not reloaded*）。用顶层常量做判据
#:    测不出"整树清 sys.modules 失效"，而那正是本项目的真 bug。
#: 子模块判据插件的目录名（**从唯一来源里取**，见 `SYNTHETIC_PLUGINS` 的说明）
SUB_PROBE_NAME = "lc_sub_probe"
assert SUB_PROBE_NAME in SYNTHETIC_PLUGINS, (
    "新造插件必须登记进 SYNTHETIC_PLUGINS，否则收尾清不掉它"
)

SUBMODULE_PLUGIN = '''
from astrbot.api.star import Context, Star, register
from .helper import VERSION_TAG


@register("lc_sub_probe", "probe-author", "子模块判据插件", "0.0.1")
class SubProbe(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.tag = VERSION_TAG

    async def initialize(self):
        return None

    async def terminate(self):
        return None
'''


# ---------------------------------------------------------------------------
# ① 成功路径：新实例 + 真 terminate + 计数与注册表一致
# ---------------------------------------------------------------------------


async def test_reload_replaces_instance_and_calls_old_terminate(two_plugins):
    """重载后：实例是**新对象**、旧实例的 `terminate()` **真被 await 过**。"""
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    old = host.loader.records["lc_probe"].instance
    assert old is not None

    calls: list[str] = []

    async def terminate():
        calls.append("terminated")

    old.terminate = terminate

    result = await reload_plugin(host, "lc_probe")

    assert calls == ["terminated"], "旧实例的 terminate() 没有被真正 await"
    new = host.loader.records["lc_probe"].instance
    assert new is not None and new is not old, "重载后应换成新实例"
    assert id(new) != id(old)
    assert result.state == "loaded", result.error


async def test_handler_count_matches_registry_exactly(two_plugins):
    """`handler_count` **等于**注册表里的实际条数（不是"大于零"）。

    🔴 这条守"计数漂移"：漏摘旧 handler 时计数会变得更大，
       而**没有任何测试会因此变红**（大于零仍然成立）。
    """
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    result = await reload_plugin(host, "lc_probe")

    actual = owned_handlers_of("lc_probe")
    assert result.handler_count == len(actual), (
        f"handler_count={result.handler_count} 与注册表实际 {len(actual)} 不符"
    )
    # 记录里的数字也必须一致（面板读的是记录）
    assert host.loader.records["lc_probe"].handler_count == len(actual)


async def test_reload_picks_up_submodule_edits(source_root, data_root):
    """🔴 **子模块**的新代码必须生效（这是热重载的**唯一**实质判据）。

    现场（修复前实测）：`helper.py` 改成 `NEW`，重载后 `instance.tag` 仍是 `OLD`
    —— 因为 `_purge_plugin_modules` 认为"来源没变"就一个都不清，
    于是 `importlib.reload` 只重跑 `main.py`、`helper` 还是缓存里那个。

    ⚠️ 这个用例**自己起宿主**（不复用 `two_plugins`）：它需要"插件一开始
       就带子模块"的形状，而公共夹具里的合成插件是单文件的。
    """
    from astrbot_host.plugin_lifecycle import reload_plugin

    write_plugin(
        source_root,
        SUB_PROBE_NAME,
        SUBMODULE_PLUGIN,
        extra_files={"helper.py": "VERSION_TAG = 'OLD'\n"},
    )
    host = await build_host(source_root, data_root)
    try:
        assert host.loader.records[SUB_PROBE_NAME].instance.tag == "OLD"

        # 只改**子模块**（`main.py` 一个字不动）
        (source_root / SUB_PROBE_NAME / "helper.py").write_text(
            "VERSION_TAG = 'NEW'\n", encoding="utf-8"
        )

        await reload_plugin(host, SUB_PROBE_NAME)

        assert host.loader.records[SUB_PROBE_NAME].instance.tag == "NEW", (
            "重载后仍在跑旧代码 —— 整树清 sys.modules 没生效"
            "（子模块不会被 importlib.reload 重新执行）"
        )
    finally:
        await teardown_host(host)


async def test_reload_picks_up_main_edits_and_drops_old_handlers(two_plugins):
    """改了 `main.py` 的指令名再重载：**新名字在、旧名字不在**（集合比对）。"""
    from astrbot_host.plugin_lifecycle import reload_plugin
    from astrbot_host.registry import star_handlers_registry

    host, probe_dir, _bystander = two_plugins
    main = probe_dir / "main.py"
    main.write_text(
        main.read_text(encoding="utf-8").replace(
            '@filter.command("lc_alpha")', '@filter.command("lc_gamma")'
        ),
        encoding="utf-8",
    )

    await reload_plugin(host, "lc_probe")

    cmds: set[str] = set()
    for handler in star_handlers_registry.all_handlers():
        for flt in handler.event_filters or ():
            name = getattr(flt, "command_name", None)
            if name:
                cmds.add(name)
    assert "lc_gamma" in cmds, f"重载没读到新代码：{sorted(cmds)}"
    assert "lc_alpha" not in cmds, f"旧代码的 handler 没被清掉：{sorted(cmds)}"


async def test_reload_keeps_other_plugins_intact(two_plugins):
    """重载 A 之后，B 的四类登记物**一条不少**。"""
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context
    before = registry_snapshot()
    kept = {n for n in before["handlers"] if n.startswith("lc_bystander")}
    assert kept

    await reload_plugin(host, "lc_probe")

    after = registry_snapshot()
    assert kept <= after["handlers"], f"重载 A 把 B 的 handler 弄丢了：{kept - after['handlers']}"
    assert "lc_bystander_tool" in after["tools"], after["tools"]
    assert "lc_bystander/page/info" in web_api_routes(ctx)


async def test_reload_is_repeatable(two_plugins):
    """连着重载三次，注册表**逐条收敛**（不叠加、不残留）。

    🔴 重复重载是真实路径（用户改一次配置点一次重载）。若拆解不幂等，
       第二、三次就会叠出重复 handler —— 而"数量变大"看起来像"注册成功了"。
    """
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    await reload_plugin(host, "lc_probe")
    first = owned_handlers_of("lc_probe")

    await reload_plugin(host, "lc_probe")
    await reload_plugin(host, "lc_probe")

    assert owned_handlers_of("lc_probe") == first, (
        f"重复重载后 handler 集合变了：{owned_handlers_of('lc_probe') ^ first}"
    )
    assert len(first) == host.loader.records["lc_probe"].handler_count


# ---------------------------------------------------------------------------
# ② 失败路径：reload_failed，且记录**还在**（换成 failed）
# ---------------------------------------------------------------------------


async def test_reload_failed_plugin_reports_failed_state_not_disappearance(two_plugins):
    """把插件改成坏代码再重载 → `reload_failed`，记录**换成 failed 而不是消失**。

    🔴 "记录消失"会让面板显示"没有这个插件"，用户以为被删了，
       于是去看回收站 —— 而那里什么都没有。
    """
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, reload_plugin

    host, probe_dir, _bystander = two_plugins
    (probe_dir / "main.py").write_text(
        "raise RuntimeError('判据用：故意炸')\n", encoding="utf-8"
    )

    with pytest.raises(PluginLifecycleError) as exc:
        await reload_plugin(host, "lc_probe")

    assert exc.value.code == "reload_failed"
    assert "判据用：故意炸" in exc.value.message, exc.value.message
    # 必须带 traceback（否则用户只看到一句话，无从排查）
    assert exc.value.extra.get("traceback"), exc.value.extra

    rec = host.loader.records.get("lc_probe")
    assert rec is not None, "失败后记录消失了（应换成 failed 状态）"
    assert rec.state == "failed", rec.state
    assert "lc_probe" in [n for n, _ in host.report.failed]
    assert "lc_bystander" in host.loader.records, "失败重载波及了邻居"


async def test_reload_initialize_failure_is_reload_failed_not_crash(two_plugins):
    """重载时插件 `initialize()` 抛异常 → `reload_failed`，**不是 NameError**。

    🔴 现场（2026-10-31 实测，修复前）：`_initialize_reloaded()` 里用了
       `traceback.format_exc()` 而 `host_app.py` **没有 import traceback**
       ⇒ 插件 `initialize()` 一失败就抛
       `NameError: name 'traceback' is not defined`，
       **把插件的真实错误盖掉**，路由层拿到的是一个 code=None 的裸异常
       ⇒ 接口 500，日志里看不出是哪个插件、为什么失败。

       实测对照（同一探针，只差那一行 import）：
          修复前：重载抛 NameError | code = None       | traceback 为空
          修复后：重载抛 PluginLifecycleError | code = reload_failed | traceback 有内容
    """
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, reload_plugin

    host, probe_dir, _bystander = two_plugins
    main = probe_dir / "main.py"
    main.write_text(
        main.read_text(encoding="utf-8").replace(
            "    async def initialize(self):\n        return None",
            "    async def initialize(self):\n"
            "        raise RuntimeError('判据用：initialize 故意炸')",
        ),
        encoding="utf-8",
    )
    assert "initialize 故意炸" in main.read_text(encoding="utf-8"), "替换没生效"

    with pytest.raises(PluginLifecycleError) as exc:
        await reload_plugin(host, "lc_probe")

    # ① 必须是我们的异常类型（不是 NameError / AttributeError 这类实现 bug）
    assert isinstance(exc.value, PluginLifecycleError), (
        f"抛的是 {type(exc.value).__name__} —— initialize 失败路径上有实现 bug"
    )
    assert exc.value.code == "reload_failed"
    assert "initialize 故意炸" in exc.value.message, exc.value.message
    # ② traceback 必须留得下来（否则用户只看到一句话，无从排查）
    assert exc.value.extra.get("traceback"), exc.value.extra
    # ③ 记录换成 failed，邻居不受影响
    rec = host.loader.records.get("lc_probe")
    assert rec is not None and rec.state == "failed", rec.state if rec else None
    assert "lc_bystander" in host.loader.records


async def test_reload_refuses_inactivated_plugin(two_plugins):
    """停用的插件重载 → `inactivated`（**不许**报"成功但什么都没变"）。"""
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, reload_plugin

    host, _probe, _bystander = two_plugins
    host.loader.records["lc_probe"].inactivated = True

    with pytest.raises(PluginLifecycleError) as exc:
        await reload_plugin(host, "lc_probe")
    assert exc.value.code == "inactivated"


async def test_reload_unknown_plugin_raises_not_loaded(two_plugins):
    """没有这个插件 → `not_loaded`。"""
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, reload_plugin

    host, _probe, _bystander = two_plugins
    with pytest.raises(PluginLifecycleError) as exc:
        await reload_plugin(host, "no_such_plugin")
    assert exc.value.code == "not_loaded"


# ---------------------------------------------------------------------------
# ③ 结果对象自身的形状（写错过一次）
# ---------------------------------------------------------------------------


async def test_reload_result_detached_is_detach_result(two_plugins):
    """`ReloadResult.detached` 必须是 `DetachResult`（**默认值也一样**）。

    🔴 守的是 `default_factory` 写成 `dict` 的错：那种情况下
       `result.detached.as_dict()` 抛 AttributeError ⇒ 功能其实成功了、接口 500。

    ⚠️ **只断言"返回值是 DetachResult"抓不住它**（2026-10-31 注入实测）：
       `reload_plugin` 成功路径上**总是显式传** `detached=detached`，
       所以 `default_factory` 永远用不到 —— 类型标注写成 `dict` 也没人发现。
       注入"把 `default_factory` 改回 `dict`"时，12 条判据**全绿**。
       ⇒ 必须**绕过显式传参**、直接盯类级默认值。那才是 `default_factory`
         真正起作用的地方（也是失败路径 —— 抛 `reload_failed` 时构造
         `ReloadResult` 没有 detached 可传 —— 所依赖的那个默认值）。
    """
    from astrbot_host.plugin_lifecycle import DetachResult, ReloadResult, reload_plugin

    # ① 类级默认值（不看 `reload_plugin` 有没有显式传参）
    probe = ReloadResult(
        plugin_id="x", state="loaded", handler_count=0,
        llm_tool_bind_count=0, web_api_count=0,
    )
    assert isinstance(probe.detached, DetachResult), (
        f"ReloadResult.detached 的默认值是 {type(probe.detached).__name__}，"
        "应是 DetachResult（失败路径就是这么构造的）"
    )
    assert set(probe.detached.as_dict()) == {
        "handlers", "llm_tools", "web_apis", "tasks",
    }

    # ② 真实重载后的返回值
    host, _probe, _bystander = two_plugins
    result = await reload_plugin(host, "lc_probe")
    assert isinstance(result.detached, DetachResult), type(result.detached)
    assert set(result.detached.as_dict()) == {
        "handlers", "llm_tools", "web_apis", "tasks",
    }


# ---------------------------------------------------------------------------
# ④ 装配：`PluginHost` 的薄方法必须真的委托（不能只是存在）
# ---------------------------------------------------------------------------


async def test_host_thin_method_delegates_to_lifecycle(two_plugins):
    """`host.reload_plugin()` 是**真委托**：注册表里能看到它做过的痕迹。"""
    from astrbot_host import plugin_lifecycle

    host, _probe, _bystander = two_plugins
    calls: list[str] = []
    original = plugin_lifecycle.reload_plugin

    async def spy(host_arg, plugin_id):
        calls.append(plugin_id)
        return await original(host_arg, plugin_id)

    plugin_lifecycle.reload_plugin = spy
    try:
        result = await host.reload_plugin("lc_probe")
    finally:
        plugin_lifecycle.reload_plugin = original

    assert calls == ["lc_probe"], "薄方法没走到 plugin_lifecycle.reload_plugin"
    assert result.state == "loaded"


async def test_reload_report_is_projection_of_records(two_plugins):
    """重载后 `report` 与 `records` **一致**（`report` 永远是投影）。"""
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    await reload_plugin(host, "lc_probe")

    assert host.report.loaded == len([r for r in host.loader.records.values() if r.ok])
    assert set(host.report.loaded_plugins) == {
        r.dir_name for r in host.loader.records.values() if r.ok
    }


async def test_concurrent_reloads_are_serialized(two_plugins):
    """两个重载**同时**发起时必须**串行**（不能交错）。

    🔴 不加锁的交错序列（A、B 同时重载**不同**插件）：
        A 拆 A → B 拆 B → A 清 sys.modules + 重新 import → B 清 + import
        ⇒ `sys.modules` 是进程级共享的，两边会在对方的"清缓存/重新 import"
        **中间**插入，注册表最后剩"半套"（handler 在、工具不在），**不报错**。
        教训来自 jp-vps 上 Hermes 的 `c07501ec41`。
    """
    from astrbot_host import plugin_lifecycle

    host, _probe, _bystander = two_plugins
    concurrent = {"now": 0, "peak": 0}
    original = plugin_lifecycle.detach_plugin

    async def counting_detach(host_arg, plugin_id):
        concurrent["now"] += 1
        concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
        try:
            await asyncio.sleep(0.02)  # 把窗口撑宽，交错**一定会**发生（不加锁时）
            return await original(host_arg, plugin_id)
        finally:
            concurrent["now"] -= 1

    plugin_lifecycle.detach_plugin = counting_detach
    try:
        await asyncio.gather(
            plugin_lifecycle.reload_plugin(host, "lc_probe"),
            plugin_lifecycle.reload_plugin(host, "lc_bystander"),
        )
    finally:
        plugin_lifecycle.detach_plugin = original

    assert concurrent["peak"] == 1, (
        f"两次重载重叠了（峰值 {concurrent['peak']}）—— 锁没生效"
    )
    for plugin_id in ("lc_probe", "lc_bystander"):
        rec = host.loader.records[plugin_id]
        assert rec.state == "loaded", f"{plugin_id} 重载后状态 {rec.state}"
        assert rec.handler_count > 0, f"{plugin_id} 重载后没有任何 handler"


async def test_lifecycle_lock_survives_a_second_event_loop(two_plugins):
    """生命周期锁必须能在**第二个事件循环**里用。

    🔴 现场（2026-10-31 实测）：第一版把锁写成**模块级单例**
       `_LIFECYCLE_LOCK = asyncio.Lock()`，被第一个 `asyncio.run()` 绑上循环后，
       第二次起停就是
       `RuntimeError: <asyncio.locks.Lock object ...> is bound to a different
        event loop`。
       而它**只在第二次起停时才炸** —— 单跑一次永远绿，pytest 每个用例一条
       新循环、宿主也可以多次起停，所以这是必然会踩到的那一类。

    ⚠️ **不能真的再 `asyncio.run()` 一次**（pytest-asyncio 的用例本身已经在循环里，
       嵌套 `run()` 会报 "asyncio.run() cannot be called from a running event loop"）。
       改成直接读实现：锁**按循环**缓存 ⇒ 不同 `id(loop)` 得不同对象，
       而同一循环内两次取得**同一个**（串行语义不丢）。
    """
    from astrbot_host import plugin_lifecycle

    # ① 同一循环里两次取到的是同一把（串行语义的前提）
    first = plugin_lifecycle._lifecycle_lock()
    second = plugin_lifecycle._lifecycle_lock()
    assert first is second, "同一事件循环里取到了两把不同的锁 —— 串行语义就没了"

    # ② 缓存键是**循环身份**，不是全进程一个常量
    assert isinstance(plugin_lifecycle._LOCKS, dict), (
        "锁必须是按循环缓存的 dict；写成模块级单例会在第二次起停时炸"
        "（RuntimeError: ... is bound to a different event loop）"
    )
    assert id(asyncio.get_running_loop()) in plugin_lifecycle._LOCKS
