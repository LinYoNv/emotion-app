# -*- coding: utf-8 -*-
"""拆解（`detach_plugin`）判据 —— 「摘干净」这件事的**逐条**证明。

## 为什么这一组是整套生命周期判据的地基

重载与卸载都只有两种结局：**要么拆干净了，要么留下半套登记物**。
而"留下半套"在本项目里是**不报错**的（静默失败最贵）：

* handler 漏摘 → 同一条指令执行两次，而 `handler_full_name` 只有一个，
  注册表里**看不出重复**；
* LLM 工具漏摘 → 模型看到两个同名工具，严格上游直接 400；
* Web API 漏摘 → 面板打到**上个实例的方法**（`self` 已 terminate）；
* `_web_api_owners` 没跟着缩位 → **归属整体错位**，`plugin_detail()` 从此算错；
* `star_map` 里 pop 了但 `star_registry` 没删 → 插件被列两次、terminate 被调两次。

⇒ 所以本文件的判据全部是「**逐条集合相等**」，计数只当辅助。
   （`项目更新.md` 通则：计数相等 ≠ 组成正确。）

## 判据守的**反向**：不能多删

「拆 A 不许动 B」比「拆 A 要拆干净」更重要 —— 少删只是不干净，
**多删是把别人的能力删掉**，而且同样不报错。本文件为此**专门造了一个前缀邻居**
`lc_probe_extra`（见 `_plugin_lifecycle_fixtures.PREFIX_NEIGHBOR_PLUGIN` 的说明）。
"""

from __future__ import annotations

import asyncio

import pytest

from _plugin_lifecycle_fixtures import (  # noqa: E402
    PROBE_PLUGIN,
    build_host,
    data_root,  # noqa: F401 — 夹具必须显式 import 进本模块才被 pytest 收集
    owned_handlers_of,
    registry_snapshot,
    source_root,  # noqa: F401 — 同上（`two_plugins` 依赖它，依赖也要在本模块可见）
    three_plugins,  # noqa: F401
    two_plugins,  # noqa: F401
    web_api_owners,
    web_api_routes,
    write_plugin,
)


# ---------------------------------------------------------------------------
# ① 拆干净：五处登记物逐条不剩
# ---------------------------------------------------------------------------


async def test_detach_removes_handler_set_exactly(two_plugins):
    """handler 按**集合**清空（不是"数量对上了"）。"""
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    before = owned_handlers_of("lc_probe")
    # 前置：这个合成插件**确实**注册了 handler，否则下面的断言是空判
    assert before, f"合成插件没有注册 handler，判据无效：{registry_snapshot()}"

    result = await detach_plugin(host, "lc_probe")

    assert owned_handlers_of("lc_probe") == set(), (
        f"拆解后仍残留：{owned_handlers_of('lc_probe')}"
    )
    assert result.handlers == len(before), (
        f"计数 {result.handlers} != 实际摘掉 {len(before)}"
    )


async def test_detach_removes_llm_tools(two_plugins):
    """LLM 工具表里那条也走（**按名字**逐条比对）。"""
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    before = registry_snapshot()["tools"]
    assert "lc_recall" in before, before

    result = await detach_plugin(host, "lc_probe")

    after = registry_snapshot()["tools"]
    assert "lc_recall" not in after, after
    assert result.llm_tools == 1, result.llm_tools


async def test_detach_removes_web_api_and_keeps_owner_table_aligned(two_plugins):
    """Web API 摘掉，且**归属表同步缩位**（两张表按下标平行）。

    🔴 只删 `registered_web_apis` 而不删 `_web_api_owners` 的后果是
       "之后所有归属往前错一格" —— 没有报错，详情页会把 A 的 API 算给 B。
       所以判据同时要求①长度相等②剩下的每一条归属**逐条**还对得上。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context
    assert len(web_api_routes(ctx)) == len(web_api_owners(ctx)), "前置：两表本来就不等长"
    assert "lc_probe/probe/ping" in web_api_routes(ctx)

    await detach_plugin(host, "lc_probe")

    routes, owners = web_api_routes(ctx), web_api_owners(ctx)
    assert "lc_probe/probe/ping" not in routes, routes
    assert len(routes) == len(owners), f"缩位不同步：routes={routes} owners={owners}"
    for route, owner in zip(routes, owners, strict=True):
        if route.startswith("lc_bystander"):
            assert owner == "lc_bystander", f"{route} 的归属错了：{owner}"


async def test_detach_removes_tasks_and_cancels_them(two_plugins):
    """后台任务被摘掉，**并且真的被 cancel**（不是只从表里 del）。

    🔴 "拆了插件但它的协程还在跑"是 QQ 桥那次的形态
       （`disabled: true` 之后定时器仍在空转）。
       `del` 掉一个 Task 并不会停它 —— 必须 `cancel()`。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context

    started = asyncio.Event()
    cancelled: list[str] = []

    async def forever():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append("cancelled")
            raise

    task = asyncio.ensure_future(forever())
    await started.wait()  # 等它**真的进了**协程体，否则 cancel 会落在"还没开始"上
    ctx.register_task(task, "判据用长跑任务")

    result = await detach_plugin(host, "lc_probe")

    # ⚠️ 给取消一个调度机会：`cancel()` 只是打标记，协程体在下一轮才醒
    for _ in range(5):
        await asyncio.sleep(0)
    assert task.cancelled() or task.done(), "任务没有被取消（还在跑）"
    assert cancelled == ["cancelled"], f"协程的 CancelledError 分支没跑到：{cancelled}"
    assert task not in ctx._register_tasks, "任务还留在登记表里"
    # ⚠️ 是 **2** 不是 1：合成插件在 `__init__` 里自己也登记了 1 个任务
    #    （`PROBE_PLUGIN` 的 `ensure_future(asyncio.sleep(0))`），这里是它 + 本用例。
    assert result.tasks == 2, result.tasks


async def test_detach_removes_star_map_and_registry_entry(two_plugins):
    """`star_map` / `star_registry` 都要摘 —— **按对象身份**比对。

    🔴 这条守的是一个**顺序 bug**：第一版先 `star_map.pop()`、再用
       `star_map.get()` 去过滤 `star_registry` —— pop 之后 get 恒为 None
       ⇒ 过滤条件恒真 ⇒ 一条都没删，且不报错。
       ⚠️ 只断言 `module_path not in star_map` 抓不住它（pop 已经成功了），
       必须断言 `star_registry` 里**没有那个 metadata 对象**。
    """
    from astrbot_host.compat.core.star.base import star_map, star_registry
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    module_path = host.loader.records["lc_probe"].module_path
    metadata = star_map.get(module_path)
    assert metadata is not None, f"前置：star_map 里没有 {module_path}"
    assert metadata in star_registry, "前置：star_registry 里没有它，判据无效"

    bystander_md = star_map.get(host.loader.records["lc_bystander"].module_path)
    assert bystander_md in star_registry

    await detach_plugin(host, "lc_probe")

    assert module_path not in star_map
    assert metadata not in star_registry, "star_registry 里还留着它（pop 后取值的顺序 bug）"
    assert bystander_md in star_registry, "误删了旁观者的 metadata"


# ---------------------------------------------------------------------------
# ② 不多删：前缀邻居与旁观者一条不少
# ---------------------------------------------------------------------------


async def test_detach_does_not_touch_prefix_neighbor(three_plugins):
    """🔴 **前缀邻居** `lc_probe_extra` 必须毫发无伤。

    这条守的是最容易被忽略的"多删"：`remove_plugin("lc_probe")` 用裸字符串
    前缀匹配（没有结尾的点），会连带删掉 `lc_probe_extra` 的 handler。
    **它不报错、返回值还更大，看起来"删得更干净"。**
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _neighbor, _bystander = three_plugins
    neighbor_before = owned_handlers_of("lc_probe_extra")
    assert neighbor_before, "前置：邻居插件没注册 handler，判据无效"

    await detach_plugin(host, "lc_probe")

    assert owned_handlers_of("lc_probe_extra") == neighbor_before, (
        f"前缀邻居的 handler 被误删："
        f"{neighbor_before - owned_handlers_of('lc_probe_extra')}"
    )


async def test_detach_does_not_touch_bystander(two_plugins):
    """旁观者的四类登记物**一条不少**（集合相等，含工具与 Web API）。"""
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context
    before = registry_snapshot()
    bystander_handlers = {
        n for n in before["handlers"] if n.startswith("lc_bystander")
    }
    assert bystander_handlers

    await detach_plugin(host, "lc_probe")

    after = registry_snapshot()
    assert bystander_handlers <= after["handlers"], (
        f"旁观者 handler 被误删：{bystander_handlers - after['handlers']}"
    )
    assert "lc_bystander_tool" in after["tools"], after["tools"]
    assert "lc_bystander/page/info" in web_api_routes(ctx), web_api_routes(ctx)


# ---------------------------------------------------------------------------
# ③ 幂等
# ---------------------------------------------------------------------------


async def test_detach_is_idempotent(two_plugins):
    """再拆一次：**不报错、计数全 0、注册表一字不差**。

    ⚠️ 为什么这条重要：重载失败后重试是常见路径。若第二次拆解把"别人的东西"
       当自己的再删一遍，重试就成了**破坏性操作**。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    first = await detach_plugin(host, "lc_probe")
    assert first.total > 0, "第一次拆解应该摘掉东西"

    snapshot_after_first = registry_snapshot()
    routes_after_first = web_api_routes(host.context)

    second = await detach_plugin(host, "lc_probe")

    assert second.as_dict() == {
        "handlers": 0,
        "llm_tools": 0,
        "web_apis": 0,
        "tasks": 0,
    }, f"第二次拆解不该再摘掉东西：{second.as_dict()}"
    assert registry_snapshot() == snapshot_after_first, "第二次拆解改了注册表"
    assert web_api_routes(host.context) == routes_after_first


# ---------------------------------------------------------------------------
# ④ terminate 的异常处理（只 warning，但要留痕）
# ---------------------------------------------------------------------------


async def test_terminate_exception_is_recorded_not_swallowed(two_plugins):
    """插件 `terminate()` 抛异常 → **继续拆解**，但结果里必须留下痕迹。

    🔴 语义是"不中断"，不是"当没发生"：`terminate` 失败的插件可能留着资源
       （定时器/sqlite 连接）。吞掉异常 = 排障时看不到任何线索。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    old = host.loader.records["lc_probe"].instance

    async def boom():
        raise RuntimeError("判据用：terminate 故意炸")

    old.terminate = boom

    result = await detach_plugin(host, "lc_probe")

    assert "判据用：terminate 故意炸" in result.terminate_error, result.terminate_error
    # 拆解本身仍然完成（不中断）
    assert owned_handlers_of("lc_probe") == set()


async def test_terminate_timeout_is_recorded(two_plugins):
    """`terminate()` 卡住 → 超时后**继续**，并在结果里写明是超时。"""
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    host.initialize_timeout = 0.05  # 把上限压到可测的量级
    old = host.loader.records["lc_probe"].instance

    async def hang():
        await asyncio.sleep(3600)

    old.terminate = hang

    result = await detach_plugin(host, "lc_probe")

    assert "TimeoutError" in result.terminate_error, result.terminate_error
    assert owned_handlers_of("lc_probe") == set()


# ---------------------------------------------------------------------------
# ⑤ 错误要用自定义异常 + code
# ---------------------------------------------------------------------------


async def test_detach_unknown_plugin_raises_with_code(two_plugins):
    """没有这个插件 → `not_loaded`（**带 code**，不让调用方去匹配文案）。"""
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, detach_plugin

    host, _probe, _bystander = two_plugins
    with pytest.raises(PluginLifecycleError) as exc:
        await detach_plugin(host, "no_such_plugin")
    assert exc.value.code == "not_loaded"


async def test_detach_without_loader_raises_unsupported(source_root, data_root):
    """宿主还没起（`loader is None`）→ `unsupported`（不是 AttributeError）。"""
    from astrbot_host.host_app import PluginHost
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, detach_plugin

    bare = PluginHost(plugin_dirs=[source_root], auto_initialize=False)
    with pytest.raises(PluginLifecycleError) as exc:
        await detach_plugin(bare, "lc_probe")
    assert exc.value.code == "unsupported"


# ---------------------------------------------------------------------------
# ⑥ 裸协程形态的后台任务（真实泄漏点）
# ---------------------------------------------------------------------------


async def test_detach_closes_a_bare_coroutine_task(two_plugins):
    """登记的是**裸协程**时，拆解必须 `close()` 它。

    🔴 真实泄漏：`register_task()` 收任意 awaitable，调用方传裸协程很常见。
       裸协程没有 `cancel()`，只 `del` ⇒
       ① CPython 在 GC 时打 `RuntimeWarning: coroutine ... was never awaited`；
       ② 协程体 `finally` 里的清理逻辑**一行都不执行**。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context

    async def body():
        await asyncio.sleep(3600)

    coro = body()
    ctx.register_task(coro, "判据用裸协程")
    assert coro in ctx._register_tasks

    await detach_plugin(host, "lc_probe")

    assert coro not in ctx._register_tasks, "裸协程还留在任务表里"
    assert coro.cr_frame is None, "裸协程没有被 close（cr_frame 还在 = 还没关闭）"


# ---------------------------------------------------------------------------
# ⑦ 测试自身不许污染进程级注册表
# ---------------------------------------------------------------------------


async def test_teardown_really_clears_process_level_registries(two_plugins):
    """夹具收尾必须**真的清注册表**（否则污染下一个测试文件）。

    🔴 现场（2026-10-31 实测）：`host.stop()` 只调 `terminate()`，**不动注册表**，
       而 `star_handlers_registry` / `llm_tools` / `star_map` 都是**进程级单例**。
       泄漏出去的合成插件会让另一个文件里按前缀扫描的判据**单跑绿、全量红**
       （实测报 `拆解后仍残留 handler: probe_plugin_extra.main_cmd`）。

       这道修有两半，必须分别守住：
         ① 合成插件用**独有前缀** `lc_*`（不撞名）；
         ② 收尾**显式清注册表**（这一条守 ②，因为 ① 靠命名看不出来）。

    ⚠️ 这条用例**自己调 teardown**（夹具的 cleanups 要到用例结束才跑），
       所以它在调之前先记下"有东西"，调之后断言"没了"。
    """
    from astrbot_host.compat.core.star.base import star_map
    from astrbot_host.registry import llm_tools, star_handlers_registry

    from _plugin_lifecycle_fixtures import teardown_host

    host, _probe, _bystander = two_plugins

    # 前置：确实有东西会被清（否则下面的"清干净了"是空断言）
    assert any(p.startswith("lc_") for p in star_map), sorted(star_map)
    assert any(n.startswith("lc_") for n in llm_tools.names()), llm_tools.names()
    assert any(
        h.handler_module_path.startswith("lc_")
        for h in star_handlers_registry.all_handlers()
    )

    await teardown_host(host)

    assert not any(p.startswith("lc_") for p in star_map), (
        f"star_map 里还残留：{sorted(p for p in star_map if p.startswith('lc_'))}"
    )
    assert not any(n.startswith("lc_") for n in llm_tools.names()), (
        f"llm_tools 里还残留：{llm_tools.names()}"
    )
    assert not any(
        h.handler_module_path.startswith("lc_")
        for h in star_handlers_registry.all_handlers()
    ), "handler 注册表里还残留 lc_* 条目"
