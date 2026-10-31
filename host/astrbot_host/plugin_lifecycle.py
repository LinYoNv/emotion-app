# -*- coding: utf-8 -*-
"""插件生命周期：**拆解 / 热重载 / 卸载**。

三条能力共用一个核心 —— `detach_plugin()`。**这是刻意的单一职责**，
因为「重载」和「卸载」的区别只在最后一步（重新 load vs 删掉目录），
而"从注册表里摘干净"这一步**必须逐字相同**：

    重载：detach → purge sys.modules → load_one → initialize
    卸载：detach → purge sys.modules → **删掉**源码目录（配置/数据按勾选项）

## 为什么必须有一条共用的拆解路径（踩过的事故）

本项目最贵的一类 bug 是**静默失败**。插件登记物散在**五处**：
`star_handlers_registry`（事件 handler）、`llm_tools`（function calling）、
`context.registered_web_apis`（+ 按下标平行的 `_web_api_owners` 归属表）、
`context._register_tasks`（+ 平行的 `_task_owners`）、`star_map` / `star_registry`。
任何一处漏摘，症状都**不是报错**：

* handler 漏摘 → 重载后**同一条指令执行两次**（旧实例的 partial 还挂在注册表里），
  而 `handler_full_name` 只有一个，注册表里**看不出重复**；
* LLM 工具漏摘 → 模型看到**两个同名工具**，严格上游直接 400；
* Web API 漏摘 → 面板打到**上个实例的方法**（`self` 是已 terminate 的对象）；
* `_web_api_owners` 没跟着缩位 → **归属整体错位**，`plugin_detail()` 的
  "这个插件登记了几个 API" 从此算错，且**不会报错**。

⇒ 所以摘除**必须**同时摘"平行表"，且**幂等**（重载失败重试时不能把别人的东西删掉）。

## 与官方实现的差异（都是刻意的，见 `docs/PLUGIN_ACTIONS_CONTRACT.md` §0）

1. **删源码目录我们与官方一致**：都直接删（官方 `remove_dir()` → `shutil.rmtree`，
   `core/utils/io.py:53-60`）。配置与数据同样由 `delete_config` / `delete_data`
   两个勾选项显式决定，**默认都不删** ⇒ 卸载后重新安装，配置与记忆还在。
   （我一度加过"回收站"，被情绪否掉 —— 见 `uninstall_plugin()` 的 docstring。）
2. 官方有 `reserved`（保留插件）概念；我们**没有**（全部是用户自己放的插件）。
3. 官方 `reload()` 走它自己的 `load()`；我们走 `PluginLoader.load_one()`
   —— 我们的加载器有整树清 `sys.modules`、别名安装、`companion_v2` 归零这些
   官方没有的硬约束（见 `plugin_loader._purge_plugin_modules` 的说明）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import shutil
from pathlib import Path
from typing import Any

#: 与 `host_app.py` 同一个 logger 名 —— 宿主日志的过滤是按这个名字配的，
#: 用 `__name__` 会让这些行**跑不进** `astrbot` 那条通道（排障时会以为"没执行"）。
logger = logging.getLogger("astrbot")

#: 生命周期写操作的**串行锁缓存**（键 = 事件循环 id，见 `_lifecycle_lock()`）。
#:
#: 🔴🔴 为什么必须有它（2026-10-31，来自 jp-vps 上 Hermes 的实战教训
#:    commit `c07501ec41`：「并发重载互相踩掉 MCP 工具 ⇒ 全量重扫必须串行加锁」）：
#:
#: 重载/卸载都在**改注册表的同一批对象**（`star_handlers_registry` / `llm_tools` /
#: `registered_web_apis` / `sys.modules`），而它们是**全局单例、靠"先删后加"**
#: 维持一致的。两个请求交错执行时会出现这种序列：
#:
#:     A: detach(plugin_A)          # 删掉 A 的 handler
#:     B: detach(plugin_A)          # 又删一遍（幂等，无害）
#:     A: purge sys.modules + load  # 重新加载 A
#:     B: purge sys.modules + load  # **把 A 刚加载的模块从 sys.modules 里清掉**
#:     A: 注册 handler/tool         # 往**已被清空的模块命名空间**注册
#:     ⇒ 注册表里出现"半套 A"：handler 在、tool 不在（或反之），**不报错**
#:
#: 这是"能力少了一半但页面正常"的典型形态，事后极难归因。
#: 加锁的代价是重载要排队（实测单插件重载 < 1s），收益是**永远只有一个写者在动**。
#:
#: ⚠️ **每个事件循环一把锁**，不是全进程一把（2026-10-31 实测踩到）：
#:    一把模块级 `asyncio.Lock()` 被第一个 `asyncio.run()` 绑上循环后，
#:    第二个 `asyncio.run()` 再用它就是
#:    `RuntimeError: <Lock ...> is bound to a different event loop`
#:    —— 而它**只在第二次起停时才炸**，单跑一次永远绿（实测：同一进程里
#:    `asyncio.run()` 两次，第二次必然抛这个 RuntimeError）。
#:    pytest 每个用例一条新循环、宿主也可以多次起停，所以必须按循环隔离。
#:    真正需要串行的是"同一个循环里的并发请求" —— 那正是 Web 服务的形态
#:    （一个 Hypercorn 循环）：锁与注册表同层，同一时刻只有一个写者在动。
_LOCKS: "dict[int, asyncio.Lock]" = {}


def _lifecycle_lock() -> "asyncio.Lock":
    """取**当前事件循环**的生命周期锁（懒建）。

    ⚠️ 不能在模块顶层 `asyncio.Lock()`：那会把锁绑死在 import 时（可能没有）的
       循环上，后续每条新循环都用不了 —— 见 `_LOCKS` 的实测现场。
    """
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[id(loop)] = lock
    return lock


class PluginLifecycleError(RuntimeError):
    """生命周期操作的失败。

    ⚠️ **带 `code` 字段**而不是让调用方去匹配文案：路由层要靠它决定 HTTP 状态码，
    匹配文案会在改文案时**静默变成 500**（本项目踩过"否定式断言的字面串"那类坑）。
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class DetachResult:
    """一次拆解的账（**每项都要能对上**，否则又成了"看起来拆干净了"）。"""

    handlers: int = 0
    llm_tools: int = 0
    web_apis: int = 0
    tasks: int = 0
    #: 终止旧实例时抛的异常（**不静默**：插件 terminate 失败可能留资源）
    terminate_error: str = ""

    @property
    def total(self) -> int:
        return self.handlers + self.llm_tools + self.web_apis + self.tasks

    def as_dict(self) -> dict[str, Any]:
        return {
            "handlers": self.handlers,
            "llm_tools": self.llm_tools,
            "web_apis": self.web_apis,
            "tasks": self.tasks,
        }


@dataclass
class ReloadResult:
    plugin_id: str
    state: str
    handler_count: int
    llm_tool_bind_count: int
    web_api_count: int
    #: ⚠️ `default_factory=DetachResult`（不是 `dict`）—— 写错时
    #:    `result.detached.as_dict()` 会在**成功路径**上抛
    #:    `AttributeError: 'dict' object has no attribute 'as_dict'`，
    #:    也就是"功能其实成功了、接口 500"。第一版就是这么错的。
    detached: DetachResult = field(default_factory=DetachResult)
    #: 加载器/插件自己给出的错误（`state="failed"` 时非空）
    error: str = ""
    traceback: str = ""


@dataclass
class UninstallResult:
    plugin_id: str
    #: 被删掉的源码目录（**删完已不存在**，留着给用户对账/写日志）
    source_path: str
    #: ⚠️ 与 `ReloadResult.detached` 同一个坑：写 `dict` 会让**成功路径**炸。
    detached: DetachResult = field(default_factory=DetachResult)
    #: 被**一并删掉**的可选产物（`None` = 用户没勾，或本来就不存在）。
    #:
    #: ⚠️ 返回的是**已删除**的路径 —— 面板文案必须说"已删除"，不能说"已移走"，
    #: 否则用户会去找一个不存在的地方。
    config_file: str | None = None
    data_dir: str | None = None
    #: 停用名单里被剔除的 module_path（空 = 它本来就没被停用）
    removed_from_inactivated: str = ""


# ---------------------------------------------------------------------------
# ① 拆解
# ---------------------------------------------------------------------------


async def detach_plugin(host: Any, plugin_id: str) -> DetachResult:
    """把一个插件的**全部登记物**从宿主摘掉（幂等）。**不碰磁盘**。

    Args:
        host: `PluginHost` 实例（要 `loader` / `context`）。
        plugin_id: 插件目录名（`PluginRecord.dir_name`）。

    Returns:
        各项摘除计数。**全 0 是合法结果**（重复拆解、或该插件本来就没注册东西）。

    Raises:
        PluginLifecycleError: `not_loaded` —— 宿主里没有这个插件。

    ⚠️ **是 async 的**，因为必须真的 `await instance.terminate()`。
       第一版写成了同步函数，遇到 awaitable 就 `close()` 掉 —— 那是**掩盖**
       而不是修：`close()` 只是丢弃协程，插件一行收尾代码都不会执行
       （陪伴的 terminate 会停后台调度器、关 sqlite 连接），
       接着我们就把它的模块从 `sys.modules` 里清了 ⇒ **资源泄漏 + 悬挂任务**。

    ⚠️ 顺序是**从外向里**：先终止实例（让它自己收尾、取消定时器），
       再摘登记物。反过来会让"terminate 里还想注销什么"找不到自己的登记物。
    """
    loader = getattr(host, "loader", None)
    if loader is None:
        raise PluginLifecycleError("unsupported", "插件宿主尚未完成加载（没有 loader）")
    rec = loader.records.get(plugin_id)
    if rec is None:
        raise PluginLifecycleError("not_loaded", f"没有这个插件：{plugin_id}")

    out = DetachResult()

    # ---- ① 终止实例（异常只留痕，不中断）----
    # ⚠️ 用**同一个超时**，与宿主启动时 initialize 的判据一致 —— 插件在
    #    terminate 里做网络请求的坑和 initialize 里是一样的（实测 media_parser
    #    联网下载字体 181 秒），一个插件的抖动不许把整个面板请求挂住。
    import inspect

    instance = rec.instance
    if instance is not None:
        terminate = getattr(instance, "terminate", None)
        if callable(terminate):
            timeout = float(getattr(host, "initialize_timeout", 20.0))
            try:
                result = terminate()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=timeout)
            except asyncio.TimeoutError:
                out.terminate_error = f"TimeoutError: terminate() 超过 {timeout:.0f}s"
                logger.error(
                    "插件 %s 的 terminate() 超时（%.0fs，继续拆解）", plugin_id, timeout
                )
            except Exception as exc:  # noqa: BLE001
                out.terminate_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "插件 %s 的 terminate() 失败（继续拆解）：%s", plugin_id, exc
                )
    #: 实例已拆，重载时不能复用
    rec.instance = None

    # ---- ② 事件 handler ----
    from .registry import star_handlers_registry

    # 🔴 不能直接 `remove_plugin(plugin_id)`（2026-10-31 实测踩到）。
    #    `remove_plugin(prefix)` 的判据是**裸字符串前缀**
    #    （`registry.py:211-215`：`h.handler_module_path.startswith(prefix)`），
    #    而 `plugin_id` 是**目录名**、不带结尾的点 ⇒ 它会命中
    #    `probe_plugin` **和** `probe_plugin_extra` 两个插件。
    #    现场：合成插件 `probe_plugin` 与 `probe_plugin_extra` 同时加载，
    #    拆前者时后者的 handler 全被删掉，而卸载/重载**照常返回成功**
    #    （这正是本项目最贵的那类静默失败）。
    #    ⇒ 删之前先把归属算准（`_owned_by` 判 `<dir>` 与 `<dir>.`），
    #      再逐条按**对象身份**删，最后汇报条数。
    owned_handlers = [
        h
        for h in star_handlers_registry.all_handlers()
        if _owned_by(getattr(h, "handler_module_path", None), plugin_id)
    ]
    for handler in owned_handlers:
        star_handlers_registry.remove(handler)
    out.handlers = len(owned_handlers)

    # ---- ③ LLM 工具 ----
    from .registry import llm_tools

    tool_names = [
        spec.name
        for spec in llm_tools.func_list
        if _owned_by(getattr(spec, "handler_module_path", None), plugin_id)
    ]
    for name in tool_names:
        llm_tools.remove_func(name)
    out.llm_tools = len(tool_names)

    # ---- ④ Web API（含**按下标平行**的归属表）----
    ctx = getattr(host, "context", None)
    if ctx is not None:
        out.web_apis = _drop_owned_web_apis(ctx, plugin_id)
        out.tasks = _drop_owned_tasks(ctx, plugin_id)

    # ---- ⑤ star_map / star_registry ----
    from .compat.core.star.base import star_map, star_registry

    module_path = rec.module_path
    # 🔴🔴 **必须先把 metadata 对象取出来，再 pop**（2026-10-31 修的真 bug）。
    #
    #    原来写的是：
    #        star_map.pop(module_path, None)
    #        star_registry[:] = [md for md in star_registry
    #                            if md is not star_map.get(module_path)]
    #    `pop` 之后 `star_map.get(module_path)` **恒为 None** ⇒ 过滤条件恒真
    #    ⇒ **一条都没删**，而且不报错。症状是重载后 `star_map` 里没有它、
    #    `star_registry` 里还留着旧的那条 ⇒ 插件被列两次 / terminate 被调两次。
    #    这是"照抄逻辑但顺序反了"的典型，**只有逐条断言注册表内容才能发现**。
    metadata = star_map.pop(module_path, None)
    if metadata is not None:
        # ⚠️ 按**身份**删而不是按 name：同名插件可能存在（不同目录），按 name 会删错。
        star_registry[:] = [md for md in star_registry if md is not metadata]

    logger.info(
        "插件已拆解：%s（handlers=%d tools=%d web_apis=%d tasks=%d）%s",
        plugin_id,
        out.handlers,
        out.llm_tools,
        out.web_apis,
        out.tasks,
        f"｜terminate 异常：{out.terminate_error}" if out.terminate_error else "",
    )
    return out


def _owned_by(value: Any, plugin_id: str) -> bool:
    """`handler_module_path` / `module_path` 是否属于这个插件。

    ⚠️ 判据用**顶层包名前缀**（`<dir>.`）而不是精确相等：
    插件可以在子模块里注册（实测陪伴的 `atrelay` 就挂在
    `<dir>.atrelay`），精确相等会漏掉它 —— 而漏掉**不报错**，只是重载后重复注册。
    """
    if not isinstance(value, str) or not value:
        return False
    return value == plugin_id or value.startswith(f"{plugin_id}.")


def _drop_owned_web_apis(ctx: Any, plugin_id: str) -> int:
    """摘掉该插件登记的 Web API，**并同步缩位归属表**。

    🔴 `registered_web_apis` 与 `_web_api_owners` **按下标平行**
    （见 `Context.__init__` 的说明）。只删前者 = 归属整体错位，
    症状是别人的 API 被算到这个插件头上（**不报错**）。

    ⚠️ 必须**从后往前**删，否则前面的下标会在删除过程中失效。
    """
    apis = getattr(ctx, "registered_web_apis", None)
    if not isinstance(apis, list):
        return 0
    owners = getattr(ctx, "_web_api_owners", None)
    drop: list[int] = []
    for idx, api in enumerate(apis):
        owner = owners[idx] if isinstance(owners, list) and idx < len(owners) else ""
        # 归属是**目录名**（`set_current_plugin` 传的是 `path.name`），不是 module_path
        if owner == plugin_id:
            drop.append(idx)
    for idx in reversed(drop):
        del apis[idx]
        if isinstance(owners, list) and idx < len(owners):
            del owners[idx]
    return len(drop)


def _drop_owned_tasks(ctx: Any, plugin_id: str) -> int:
    """摘掉该插件登记的���台任务（同样**同步缩位**）。

    ⚠️ 任务取出来之后**要 cancel**：留着就是"拆了插件但它的协程还在跑"
    —— 那正是 QQ 桥那次事故的形态（`disabled: true` 之后定时器仍在空转）。
    """
    tasks = getattr(ctx, "_register_tasks", None)
    if not isinstance(tasks, list):
        return 0
    owners = getattr(ctx, "_task_owners", None)
    drop: list[int] = []
    for idx, _ in enumerate(tasks):
        owner = owners[idx] if isinstance(owners, list) and idx < len(owners) else ""
        if owner == plugin_id:
            drop.append(idx)
    for idx in reversed(drop):
        task = tasks[idx]
        # 🔴 两种形态都要收干净，**漏一种就泄漏**：
        #   ① 已经是 Task/Future（有人 create_task 过）→ `cancel()`
        #   ② 还是**裸协程**（`context.register_task(asyncio.sleep(0), ...)`
        #      的调用方给的就是协程对象）→ 它没有 cancel，只能 `close()`。
        #      不关的话 CPython 会在 GC 时打
        #      `RuntimeWarning: coroutine '...' was never awaited`，
        #      而且协程里 `finally` 的清理逻辑**一行都不会执行**。
        cancel = getattr(task, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                logger.warning("取消插件 %s 的后台任务失败", plugin_id, exc_info=True)
        else:
            close = getattr(task, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "关闭插件 %s 的未启动协程失败", plugin_id, exc_info=True
                    )
        del tasks[idx]
        if isinstance(owners, list) and idx < len(owners):
            del owners[idx]
    return len(drop)


# ---------------------------------------------------------------------------
# ② 热重载
# ---------------------------------------------------------------------------


async def reload_plugin(host: Any, plugin_id: str) -> ReloadResult:
    """把一个**已加载**的插件就地重载。**不需要重启服务。**

    ⚠️ 与「升级」的区别：升级换了磁盘上的代码但**不重载**（要重启）；
        重载是**同一个目录重新 import**。两者组合起来才是"升级完点一下重载"。

    Raises:
        PluginLifecycleError:
            `inactivated` —— 插件在停用名单里，重载不会让它跑起来（要重启）
            `not_loaded`  —— 没有这个插件
    """
    loader = getattr(host, "loader", None)
    if loader is None:
        raise PluginLifecycleError("unsupported", "插件宿主尚未完成加载（没有 loader）")
    rec = loader.records.get(plugin_id)
    if rec is None:
        raise PluginLifecycleError("not_loaded", f"没有这个插件：{plugin_id}")
    if rec.inactivated:
        # 🔴 409 而不是"重载成功但什么都没变"：后者是**假成功**，
        #    用户会以为点了就好，实际它从没被 import 过。
        raise PluginLifecycleError(
            "inactivated",
            "这个插件处于停用状态，重载不会让它跑起来 —— 请先在「插件启停」里启用，"
            "然后重启服务。",
        )

    path = Path(str(rec.path))
    # 🔴 整段（拆解 → 清缓存 → 重新 import → 注册）**必须在锁内**。
    #    只锁一半等于没锁：另一个请求可以在"清缓存"和"重新 import"之间插进来，
    #    把我们刚 import 的模块从 `sys.modules` 里清掉
    #    （详见 `_LIFECYCLE_LOCK` 的注释与 jp-vps 上 Hermes 的 commit `c07501ec41`）。
    async with _lifecycle_lock():
        detached = await detach_plugin(host, plugin_id)

        # ⚠️ 整树清 `sys.modules` **必须**调加载器那一个实现：
        #    它知道 `companion_v2` / `astrbot_host` 两个顶层包要一起清、
        #    也知道别名条目 `data.plugins.<dir>` 要重清（见它的 docstring）。
        #    ⚠️ `force=True` 是**重载语义的关键**：默认判据是"来源目录变了才清"，
        #       而重载同一个目录时 `__file__` 本来就指向这个根 ⇒ 一条都不清 ⇒
        #       子模块不会重新执行 ⇒ 重载后跑的还是旧代码（实测现场见该函数的
        #       docstring：`VERSION_TAG` 改了但实例属性仍是 OLD）。
        from .loader.plugin_loader import PluginLoader

        PluginLoader._purge_plugin_modules(plugin_id, path, force=True)  # noqa: SLF001

        # 🔴 `force=True` 要**一路传下去**：`load_one` 内部还会再清一次
        #    （`_import_plugin` 在来源不同源时），那次若不传 force 会用默认范围
        #    ⇒ 把 `companion_v2` 重新清掉，本次修复形同虚设。
        #    实测：只改 `_purge_plugin_modules` 的 prefixes 时，判据
        #    `test_reload_does_not_purge_shared_packages_owned_by_another_plugin`
        #    仍红，打点显示是 `force=False` 那一次清掉的。
        new_rec = await loader.load_one(path, force=True)
        loader.records[plugin_id] = new_rec

        # 初始化（宿主配了 auto_initialize 才做 —— 与启动路径同一判据，不另编一套）
        if getattr(host, "auto_initialize", False) and new_rec.ok:
            await host._initialize_reloaded(  # noqa: SLF001（同包内）
                new_rec, timeout=float(getattr(host, "initialize_timeout", 20.0))
            )

        await host._rebuild_report()  # noqa: SLF001

    result = ReloadResult(
        plugin_id=plugin_id,
        state=new_rec.state,
        handler_count=new_rec.handler_count,
        llm_tool_bind_count=new_rec.llm_tool_bind_count,
        web_api_count=new_rec.web_api_count,
        detached=detached,
        error=(new_rec.error or "").strip().splitlines()[-1] if new_rec.error else "",
        traceback=new_rec.traceback or "",
    )
    if not new_rec.ok:
        raise PluginLifecycleError(
            "reload_failed",
            result.error or "插件重载失败（插件没有给出错误信息）",
            traceback=result.traceback,
            detached=detached.as_dict(),
        )
    return result


# ---------------------------------------------------------------------------
# ③ 卸载（**直接删**，配置与数据由两个勾选项决定）
# ---------------------------------------------------------------------------


async def uninstall_plugin(
    host: Any,
    plugin_id: str,
    *,
    delete_config: bool = False,
    delete_data: bool = False,
    data_root: Path | None = None,
) -> UninstallResult:
    """卸载一个插件：**直接删掉源码目录**，配置与持久化数据按勾选项处理。

    Args:
        delete_config: 连 `data/plugins/config/<id>_config.json` 一起删（默认 **不删**）。
        delete_data: 连插件持久化数据目录一起删（默认 **不删**）。
        data_root: 插件数据根（`data/plugins`）。不给则从 `star_tools` 取。

    Raises:
        PluginLifecycleError:
            `unsupported`       —— 解析不到源码目录（**拒绝执行，绝不猜**）
            `not_loaded`        —— 没有这个插件
            `uninstall_failed`  —— 删目录失败（`extra.original` 里是原始异常文本）

    ## 🔴 为什么**不做**回收站 / 不自动备份（情绪 2026-10-31 定，我改回来的）

    我第一版把源码"移入回收站"以求可恢复，被情绪一句话否掉：
    「备份什么啊，什么都要备份那不就慢了。回收站也不要了，astrbot 卸载插件的时候
    有二次选择，如果插件的配置文件没有删除重新安装就可以继续使用。」

    他说得对，而且理由比"快慢"更本质：

    * **插件代码是可再生的** —— `metadata.yaml` 里有 `repo:`，面板「重新安装」
      一条命令就能从仓库拉回同一个版本。把可再生资源存进回收站 = 解决错了对象。
    * **真正不可再生的是配置与持久化数据**（记忆库、关系库、用户设置、
      上传的参考图）。这两样恰好由 `delete_config` / `delete_data` **显式决定**，
      默认**都不删** ⇒ 用户「卸载 → 重新安装」后，配置还在、记忆还在，
      插件开箱即用。这正是官方 4.27.5 的设计（`star_manager.py:1537-1573`）。
    * 回收站还额外造了一个**用户永远不知道该何时清理**的目录 ——
      不清理会一直占盘，清理又怕删错。这是净负债。

    ⇒ 官方语义 + 两个勾选项就是最优解，不要再加一层"暂存"。

    ## 失败时的状态（不留"半拆"）

    顺序是「**先拆内存，再删磁盘**」：

        ① detach + purge + 摘记录 + 清停用名单   —— 纯内存，不会失败
        ② 删源码目录                            —— 这一步才可能失败（权限/占用）
        ③ 可选产物（配置/数据）                  —— 失败只 warning

    这样万一 ② 失败，插件**已经从宿主摘干净**（不在跑、不在面板清单里），
    磁盘上还剩个目录 —— 用户看到的是"插件还在磁盘上但已不运行"，
    **可以再删一次**。反过来（先删目录再拆）若拆到一半失败，
    就会留下"目录没了、注册表还有"的鬼影，重启后又冒出来，极难归因。
    """
    loader = getattr(host, "loader", None)
    if loader is None:
        raise PluginLifecycleError("unsupported", "插件宿主尚未完成加载（没有 loader）")
    rec = loader.records.get(plugin_id)
    if rec is None:
        raise PluginLifecycleError("not_loaded", f"没有这个插件：{plugin_id}")

    source = resolve_source_dir(loader, plugin_id)
    if source is None:
        # 🔴 **绝不猜**：删错目录是这台机器上最不可接受的错误之一。
        raise PluginLifecycleError(
            "unsupported",
            "解析不到这个插件的源码目录（可能开了源码镜像）。为避免删错目录，已拒绝。",
        )

    root = data_root if data_root is not None else _data_root()

    # 🔴 整段（拆解 → 删目录 → 清缓存 → 摘记录 → 清停用名单）**必须在锁内**。
    #    理由同 `reload_plugin`：这些步骤全在改**进程级单例注册表**，
    #    并发的重载/卸载交错会把彼此的"先删后加"打乱（详见 `_LOCKS`）。
    async with _lifecycle_lock():
        # ---- ① 纯内存：拆登记物（**先拆内存，再删磁盘**）----
        #
        # ⚠️ 顺序与"先删目录"的取舍：
        #    拆登记物是**不会失败**的（都是内存操作），删目录才会失败（权限/占用）。
        #    先拆内存 ⇒ 万一后面删目录失败，插件已经从宿主摘干净，
        #    用户看到的是"插件还在磁盘上、但已经不在运行"，**可以再删一次**；
        #    反过来先删目录再拆，若拆到一半失败就留下"目录没了、注册表还有"的鬼影。
        detached = await detach_plugin(host, plugin_id)
        from .loader.plugin_loader import PluginLoader

        PluginLoader._purge_plugin_modules(plugin_id, source, force=True)  # noqa: SLF001

        loader.records.pop(plugin_id, None)
        await host._rebuild_report()  # noqa: SLF001

        # ---- ② 停用名单里剔除它（走注入的写端口，宿主不直接写配置）----
        removed = host.forget_inactivated(plugin_id)

        # ---- ③ 删源码目录（**直接删，不进回收站**）----
        #
        # 🔴🔴 **为什么不再进回收站**（情绪 2026-10-31 定，我改回来的）：
        #    插件代码是**可再生的** —— `metadata.yaml` 里有 `repo:`，
        #    「重新安装」一条命令就能从仓库拉回来。真正不可再生的是
        #    **配置与持久化数据**（记忆库、关系库、用户设置），而那两样
        #    由 `delete_config` / `delete_data` 两个勾选项**显式**决定。
        #    回收站保住的恰好是"本来就能重新下载的东西"，等于解决错了对象，
        #    还额外引入了一个用户永远不知道该何时清理的目录。
        #    官方 4.27.5 也是直接 `remove_dir()`（`core/utils/io.py:53-60`）。
        try:
            _remove_tree(source)
        except OSError as exc:
            raise PluginLifecycleError(
                "uninstall_failed",
                f"插件已从宿主卸载，但**删除源码目录失败**：{exc}\n"
                f"目录仍在：{source}\n（可以再试一次删除，或手动删掉它）",
                original=f"{type(exc).__name__}: {exc}",
                source_path=str(source),
            ) from exc

        # ---- ④ 可选产物：由两个勾选项决定（默认都不删）----
        #
        # 🔴 这两项**失败只 warning**，不抛错：源码已经删了，插件**确实**已卸载，
        #    此时抛错会让用户以为"没删掉"，而它其实删掉了。
        config_file: str | None = None
        if delete_config:
            cfg = root / "config" / f"{plugin_id}_config.json"
            if cfg.is_file():
                try:
                    cfg.unlink()
                    config_file = str(cfg)
                except OSError as exc:
                    logger.warning(
                        "插件已卸载，但删除配置文件失败（它仍在 %s）：%s", cfg, exc
                    )

        data_dir: str | None = None
        if delete_data:
            # 官方会同时清 `plugin_data` 与 `plugins_data` 两个目录
            # （`star_manager.py:1554-1573`）。我们的 `StarTools.get_data_dir()`
            # 建的是 `data/plugins/<plugin_name>`（见 `star_tools.py`），
            # 所以**以我们的根为准**，另加官方那两个名字做兼容 ——
            # 插件若是照官方路径自己建的目录，也要能删到。
            for candidate in _data_dir_candidates(root, plugin_id):
                if candidate.is_dir():
                    try:
                        _remove_tree(candidate)
                        data_dir = str(candidate)
                    except OSError as exc:
                        logger.warning(
                            "插件已卸载，但删除数据目录失败（它仍在 %s）：%s",
                            candidate,
                            exc,
                        )

    logger.info(
        "插件已卸载：%s（handlers=%d tools=%d apis=%d｜配置删除=%s 数据删除=%s）",
        plugin_id,
        detached.handlers,
        detached.llm_tools,
        detached.web_apis,
        bool(config_file),
        bool(data_dir),
    )
    return UninstallResult(
        plugin_id=plugin_id,
        source_path=str(source),
        detached=detached,
        config_file=config_file,
        data_dir=data_dir,
        removed_from_inactivated=removed,
    )


def _remove_tree(path: Path) -> None:
    """删除一个文件或目录树（**符号链接只摘链接，不跟进去**）。

    🔴 这条防护不是洁癖：`shutil.rmtree` 遇到指向目录的软链会
       **拒绝**（Python 3.12 起 `rmtree` 对 symlink 直接抛）或跟进去删真身，
       取决于写法。插件目录完全可能是个软链（开发时常见：
       `plugins/x -> ~/dev/x`）—— 跟进去删就等于删掉了开发目录。
       所以：**是链接就只 unlink 链接本身**。
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _data_dir_candidates(root: Path, plugin_id: str) -> list[Path]:
    """插件数据目录的**所有可能位置**（我们的 + 官方那两个名字）。

    ⚠️ 返回**列表**而不是单个路径：官方 `_cleanup_plugin_optional_artifacts`
       会同时清 `plugin_data` 与 `plugins_data`（`star_manager.py:1556`），
       而我们的 `StarTools.get_data_dir()` 用的是 `<data_root>/<plugin_name>`。
       只删一个会留下另一份 —— 用户勾了"删除持久化数据"却发现磁盘上还有。
    """
    base = root.parent
    return [
        root / plugin_id,                    # 我们的：data/plugins/<id>
        base / "plugin_data" / plugin_id,    # 官方其一
        base / "plugins_data" / plugin_id,   # 官方其二
    ]


def resolve_source_dir(loader: Any, plugin_id: str) -> Path | None:
    """在**源码根**里找到 `<root>/<plugin_id>`；找不到返回 `None`。

    🔴🔴 **必须用 `loader.requested_dirs`，不是 `loader.plugin_dirs`**。
       开了源码镜像（`mirror_sources=True`，**默认开**）时
       `plugin_dirs` 指向**副本目录** `data/plugin_src` —— 照它删，
       删掉的只是副本，**源码原地不动**：用户看到"删除成功"，重启后插件又回来了。
       这是"看起来成功了其实没做"的典型形态。

    🔴 **返回绝对路径，但【不解符号链接】**（2026-10-31 两次修正）：
       ① 先用 `resolve()`，结果把软链解成了真身 ⇒ 删除时删掉了真身目录（真 bug）；
       ② 改用 `absolute()`：补全相对路径、**保留软链本身**。
       配置允许写**相对路径**（`EMOTION_PLUGINS_PLUGIN_DIRS=plugins`，默认就是相对
       的）。原样拼给面板时 `source_path` 是 `plugins/astrbot_plugin_x` ——
       相对路径的基准是**进程 cwd**，而删除二次确认弹窗要拿它告诉用户
       "我删的到底是哪一份"。服务从别的目录起时，面板显示的和真正会被移走的
       不是同一个东西。契约 §1 的样例也是绝对路径。
       ⚠️ 用 `resolve()` 而不是 `absolute()`：它同时消掉 `..` 和软链，
          而插件目录恰恰可能通过相对路径/软链登记进来。
    """
    roots = getattr(loader, "requested_dirs", None) or []
    for root in roots:
        candidate = Path(str(root)) / plugin_id
        if (candidate / "main.py").is_file():
            # 🔴🔴 **只做 `absolute()`，绝不做 `resolve()`**（2026-10-31 真 bug）。
            #
            #    `resolve()` 会把**符号链接解开**（`plugins/x -> ~/dev/x`
            #    会变成 `~/dev/x`）。然后 `_remove_tree()` 拿到的是真身路径：
            #      · `path.is_symlink()` 为 **False**（它已经是真身了）
            #      · 于是走 `shutil.rmtree(真身)` ⇒ **删掉用户的开发目录**
            #    实测就是这个后果（判据 `test_symlinked_plugin_dir_unlinks_only_the_link`
            #    当场抓到 `keep.txt` 被删）。而用户以为只是"卸载一个插件"。
            #
            #    `absolute()` 只补全相对路径、**不解链接** ⇒ 软链仍是软链，
            #    `_remove_tree()` 能正确只 unlink 链接本身。
            #    契约 §1 要的"绝对路径"正是 `absolute()` 的语义，不是 `resolve()`。
            try:
                return candidate.absolute()
            except OSError:  # 极少数平台异常 → 退回原样
                return candidate
    # 🔴🔴 **没有兜底**（2026-10-31 修）。第一版还回落到 `loader.plugin_dirs`，
    #    理由是"万一插件是直接放进 data/plugin_src 的"。但那个兜底**恰好是错的**：
    #    开镜像时 `plugin_dirs` 指向**副本目录**，照它移走 ⇒
    #      · 真正的源码原地不动 → 用户看到"删除成功"，**重启后插件又回来了**；
    #      · 下次启动镜像再复制一遍，副本里那个被删的目录反而"被恢复"。
    #    两种都是"看起来成功了其实没做"。
    #    ⇒ 解析不到就是解析不到，由调用方回 409 `unsupported`，
    #      并明确告诉用户"可能开了源码镜像"。**绝不猜。**
    return None


def _data_root() -> Path:
    """插件数据根（`data/plugins`）。"""
    from .compat.core.star.star_tools import get_data_root

    return Path(get_data_root())


__all__ = [
    "DetachResult",
    "PluginLifecycleError",
    "ReloadResult",
    "UninstallResult",
    "detach_plugin",
    "reload_plugin",
    "resolve_source_dir",
    "uninstall_plugin",
]
