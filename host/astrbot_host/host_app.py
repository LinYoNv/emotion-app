# -*- coding: utf-8 -*-
"""插件宿主装配 —— 把「插件加载器」和「情绪app Web 服务」**接起来**。

⭐⭐ 这个模块存在的唯一理由（也是之前骨架的缺口）：

   加载器原先只做到「**装**」—— 插件读进来了、handler 登记了、266 个
   Web API 也记在册上，但**没有任何代码去调用它们**。
   就像**电话簿抄好了，但没接电话线**：
   用户点插件面板的按钮 → 404；插件的指令 → 永不触发。

   本模块补的就是「**跑**」和「**挂**」这两件事：

       ┌── 装 ──┐   ┌── 挂 ──┐   ┌── 跑 ──┐
       │ loader │ → │ 本模块 │ → │  handler│
       └────────┘   └────────┘   └────────┘
         已完成        本次新增

🔴 **硬边界**：本模块可以 import `astrbot_host`，
   但 `emotion_app/domain/` **绝不能** import 它（领域层保持干净）。
   所以接线动作**只能发生在这里**（或 web 层），不能渗进领域层。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot")

#: 🔴 **挂载前缀照真实现**（`dashboard/api/plugins.py:388`）：
#:    真 AstrBot 的插件扩展路由是 `/api/plugins/extensions/{plugin_path:path}`，
#:    其中 `plugin_path` 是插件自己登记的路由（如 `astrbot_plugin_x/page/overview`）。
#:    ⚠️ 别自己发明前缀 —— 插件前端（`pages/companion-panel/app.js:1`）里
#:       硬编码的是 `/astrbot_plugin_private_companion/page`，
#:       它由**宿主注入的 apiBase** 决定，不是我们去改前端。
EXTENSION_PREFIX = "/api/plugins/extensions"


@dataclass
class MountedApi:
    """一条已挂载（可被调用）的插件 Web API。"""

    route: str
    methods: list[str]
    desc: str
    owner: str
    handler: Any


@dataclass
class HostReport:
    """装配结果（**如实记录**，便于诊断）。"""

    loaded: int = 0
    #: 已加载插件的目录名（⚠️ 与 `mounted` 不是一回事：
    #:  有些插件**注册 0 条 Web API**（如 image_companion），
    #:  它们加载成功但不会出现在 `mounted` 里 ——
    #:  用 `mounted` 判断"插件是否加载"会得出错误结论（实测踩到）。
    loaded_plugins: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    mounted: list[MountedApi] = field(default_factory=list)
    initialized: list[str] = field(default_factory=list)
    init_failed: list[tuple[str, str]] = field(default_factory=list)
    #: 🔴 加载/初始化期间**插件源码目录**（本应只读）被改动的文件。
    #:  非空 = 有插件回写了自己的源码目录；若那是别人的生产目录，就是污染。
    #:  （`__pycache__` 已排除 —— 那是 Python 正常副作用，不算污染。）
    source_writes: list[Any] = field(default_factory=list)
    #: 可访问的**插件拓展页**（`metadata.yaml` 声明了 `pages:` 且入口 HTML 存在）。
    #: ⚠️ 只有元数据不算数 —— 必须宿主提供页面路由才打得开（见 `plugin_pages.py`）。
    pages: list[Any] = field(default_factory=list)
    #: 🔴 **被停用名单跳过的插件**（模块路径）。
    #: 与 `failed` 是**两条不同的路径**：停用的插件不 import、不实例化、
    #: 不 initialize，也**不算失败**（照真实现 `star_manager.py:1240`）。
    #: 单独列出来 = 「状态报告里可区分」（本项目铁律：不静默）。
    inactivated: list[str] = field(default_factory=list)
    #: 停用名单里**对不上任何插件目录**的元素（拼错 / 插件已删）—— 必须能看见。
    unmatched_inactivated: list[str] = field(default_factory=list)

    @property
    def mounted_count(self) -> int:
        return len(self.mounted)

    def find_page(self, plugin_id: str, page_name: str) -> Any:
        """按 `(插件目录名, 页名)` 找一个拓展页；没有返回 `None`。"""
        for p in self.pages:
            if p.plugin_id == plugin_id and p.page_name == page_name:
                return p
        return None

    def summary(self) -> str:
        lines = [
            f"插件宿主：已加载 {self.loaded} · 已挂载 API {self.mounted_count} "
            f"· 已初始化 {len(self.initialized)} · 拓展页 {len(self.pages)}"
            # ⚠️ 只在非空时加这一段 —— 名单为空时的日志必须与加这个能力**之前逐字一致**
            + (f" · 已停用 {len(self.inactivated)}" if self.inactivated else ""),
        ]
        if self.failed:
            lines.append(f"  加载失败 {len(self.failed)}：")
            lines += [f"    ❌ {n} → {e}" for n, e in self.failed]
        if self.init_failed:
            lines.append(f"  初始化失败 {len(self.init_failed)}：")
            lines += [f"    ❌ {n} → {e}" for n, e in self.init_failed]
        if self.inactivated:
            lines.append(
                f"  已停用 {len(self.inactivated)}（按名单跳过，**不算失败**）："
            )
            lines += [f"    ⏸ {m}" for m in self.inactivated]
        if self.unmatched_inactivated:
            lines.append(
                "  ⚠️ 停用名单里有对不上任何插件的项（拼写错误？）："
                + ", ".join(self.unmatched_inactivated)
            )
        if self.source_writes:
            lines.append(f"  ⚠️ 源码目录被改动 {len(self.source_writes)} 处：")
            lines += [
                f"    • [{w.kind}] {w.path} {w.detail}".rstrip()
                for w in self.source_writes
            ]
        return "\n".join(lines)


class PluginHost:
    """插件宿主：加载 → （可选）初始化 → 暴露 API 调用入口。

    用法::

        host = PluginHost(plugin_dirs=["/opt/astrbot/data/plugins"])
        await host.start(ports=HostPorts(config={}, data_dir="..."))
        # 之后 web 层用 host.call_api(path, method) 派发请求
    """

    def __init__(
        self,
        *,
        plugin_dirs: list[str | Path],
        auto_initialize: bool = True,
        initialize_timeout: float = 20.0,
        mirror_root: str | Path | None = None,
        inactivated: list[str] | None = None,
    ) -> None:
        self.plugin_dirs = [Path(d) for d in plugin_dirs]
        #: 传给加载器的镜像目录（`None` = 不镜像，直接在源目录加载）
        self.mirror_root = mirror_root
        #: 🔴 传给加载器的**停用名单**（模块路径，三种写法都收 —— 见
        #: `PluginLoader.normalize_module_path`）。由装配层从配置读出来再传进来：
        #: 单向依赖铁律 —— `host/` 不许反过来 import `emotion_app`。
        self.inactivated = list(inactivated or ())
        #: 加载器的镜像结果（启动后才有值）
        self.mirror_report: Any = None
        self.auto_initialize = auto_initialize
        #: 单个插件 `initialize()` 的等待上限（秒）。
        #: 🔴 默认 20s 是**实测定的**：正常插件全部 ≤0.01s，
        #:    唯一慢的（media_parser 下载字体）要 180s+ —— 20s 足够区分两者。
        self.initialize_timeout = float(initialize_timeout)
        self.loader: Any = None
        self.context: Any = None
        self.report = HostReport()
        self._started = False

    # ---- 生命周期 ----

    async def start(self, *, ports: Any = None, install_shadow: bool = True) -> HostReport:
        """加载全部插件并（可选）调用 `initialize()`。

        ⚠️ 幂等：重复调用不会重复加载（返回已有 report）。
        """
        if self._started:
            logger.warning("PluginHost.start() 重复调用，已忽略")
            return self.report

        if install_shadow:
            # 🔴 必须**在 import 任何插件之前**装影子包，
            #    否则插件的 `from astrbot...` 会失败。
            from .shadow import install_shadow_package

            install_shadow_package()

        from .compat.core.star.context import HostPorts
        from .loader import PluginLoader
        from .write_guard import SourceSnapshot, warn_on_source_writes

        # 🔴 启动前给**插件源码目录**拍快照。它本应只读，而插件可能在
        #    `__init__`/`initialize` 里回写自己的配置（实测 comfyui_pro 就是）。
        #
        #    两道防线（互补，都要有）：
        #      ① `mirror_root`（默认开）让写入**落在副本里** —— 从根上不发生；
        #      ② 本快照负责**发现**意外 —— 万一镜像被关掉、或有别的路径漏网，
        #         这里会响亮报出来（以前完全静默，靠人工 diff 才发现）。
        before = SourceSnapshot.take(self.plugin_dirs)

        self.loader = PluginLoader(
            plugin_dirs=self.plugin_dirs,
            mirror_root=self.mirror_root,
            inactivated=self.inactivated,
        )
        self.mirror_report = getattr(self.loader, "mirror_report", None)
        load_report = await self.loader.load_all(ports=ports or HostPorts())
        self.context = self.loader._context  # noqa: SLF001（同一包内，故意的）

        self.report.loaded = len(load_report.loaded)
        self.report.loaded_plugins = [r.dir_name for r in load_report.loaded]
        self.report.failed = [
            (r.dir_name, (r.error or "").strip().splitlines()[-1] if r.error else "")
            for r in load_report.failed
        ]
        # 🔴 停用的插件**单独一列**（不进 `loaded`、也不进 `failed`）——
        #    状态报告里必须能区分"没跑"是因为停用，还是因为坏了。
        self.report.inactivated = [r.module_path for r in load_report.inactivated]
        self.report.unmatched_inactivated = list(load_report.unmatched_inactivated)
        self.report.mounted = self._collect_apis(load_report)

        # 🔴 **拓展页发现**：只收集"入口 HTML 真的存在"的页。
        #    情绪报的「插件没有配置入口」根因就在这 —— 上一轮只挂了 API，
        #    没有把插件声明的 `pages:` 变成可访问的页面路由。
        from .plugin_pages import discover_pages

        self.report.pages = discover_pages(
            load_report.loaded,
            plugin_root_of=lambda rec: rec.path,
        )
        if self.report.pages:
            logger.info(
                "发现 %d 个插件拓展页：%s",
                len(self.report.pages),
                ", ".join(f"{p.plugin_id}/{p.page_name}" for p in self.report.pages),
            )

        if self.auto_initialize:
            self.report.initialized, self.report.init_failed = await self._initialize_all(
                load_report.loaded
            )

        # 加载 + 初始化都做完了，此刻比对源码目录
        self.report.source_writes = before.diff(SourceSnapshot.take(self.plugin_dirs))
        warn_on_source_writes(self.report.source_writes)

        self._started = True
        logger.info("插件宿主已启动\n%s", self.report.summary())
        return self.report

    async def stop(self) -> None:
        """调用所有插件的 `terminate()`（**逐个隔离异常**，一个失败不影响其它）。"""
        if not self._started or self.loader is None:
            return
        await self.loader.terminate_all()
        self._started = False
        logger.info("插件宿主已停止")

    async def _initialize_all(self, records: list[Any]) -> tuple[list[str], list[tuple[str, str]]]:
        """逐个调用 `initialize()` —— **失败隔离 + 超时隔离 + 如实记录**。

        🔴 **为什么必须加超时**（实测教训，不是理论担忧）：
            `plugins.auto_initialize` 默认 `True`，而它是**主服务启动路径**上的
            一步（`before_serving` → `host.start()`）。实测某个插件（media_parser）
            的 `initialize()` 会去**联网下载 16MB+17MB 字体**、超时设 180s ——
            实测把服务启动**卡了 181.6 秒**，且在无外网/被墙的机器上会一直重试。
            **一个插件的网络抖动 = 整个聊天服务起不来**，这不可接受。

        ⚠️ 超时**不是静默跳过**（本项目铁律）：超时的插件会如实进入
            `init_failed`，带 `TimeoutError` 与秒数，日志里是 ERROR。

        ⚠️ 用 `asyncio.wait_for` 而非 kill：`wait_for` 会**取消**那个 task。
            插件若吞掉 `CancelledError` 则取消失败 —— 此时按 Python 语义
            `wait_for` 仍会等到它结束，所以这里额外**记录真实耗时**以便识别。
        """
        ok: list[str] = []
        bad: list[tuple[str, str]] = []
        for rec in records:
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    rec.instance.initialize(), timeout=self.initialize_timeout
                )
                ok.append(rec.dir_name)
                elapsed = time.monotonic() - started
                if elapsed >= self.initialize_timeout * 0.5:
                    # 慢但没超时也要留痕 —— 否则"下次就超时了"没人知道
                    logger.warning(
                        "插件 initialize() 偏慢：%s 用了 %.1fs（上限 %.0fs）",
                        rec.dir_name,
                        elapsed,
                        self.initialize_timeout,
                    )
            except asyncio.TimeoutError:
                msg = (
                    f"TimeoutError: initialize() 超过 {self.initialize_timeout:.0f}s 未返回，"
                    "已放弃等待（插件可能在做网络请求；服务继续启动）"
                )
                bad.append((rec.dir_name, msg))
                logger.error("插件 initialize() 超时：%s → %s", rec.dir_name, msg)
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                bad.append((rec.dir_name, msg))
                logger.error("插件 initialize() 失败：%s → %s", rec.dir_name, msg)
        return ok, bad

    # ---- API 收集 ----

    def _collect_apis(self, load_report: Any) -> list[MountedApi]:
        """把登记的路由变成**可调用**的条目（含归属信息，便于诊断）。"""
        out: list[MountedApi] = []
        ctx = self.context
        if ctx is None:
            return out

        # 归属表与 registered_web_apis 下标平行（见 Context.__init__ 的说明）
        for idx, (route, handler, methods, desc) in enumerate(ctx.registered_web_apis):
            try:
                owner = ctx._owner_at(idx)  # noqa: SLF001（同包内）
            except Exception:  # noqa: BLE001
                owner = ""
            out.append(
                MountedApi(
                    route=str(route),
                    methods=[str(m) for m in methods],
                    desc=str(desc or ""),
                    owner=owner or "<unknown>",
                    handler=handler,
                )
            )
        return out

    # ---- 插件清单（面板「插件启停」用） ----

    def plugins_view(self) -> list[dict[str, Any]]:
        """**全部**插件的状态清单（含失败的与被停用的）。

        🔴 与 `report.loaded_plugins` 的区别：那个只有加载成功的；
           这里要给用户看"**为什么**这个插件没跑" —— 所以四种状态都要在
           （`state` 的取值见 `PluginRecord.state`，判据只那一份）。

        每项形状（`id`/`module_path` 是同一件东西的两种写法：
        前者给人看，后者是停用名单里的元素）：

            {"id": 目录名, "module_path": "<目录名>.main", "name": 显示名,
             "version": ..., "state": "loaded|failed|inactivated|not_loaded",
             "error": "..."}
        """
        if self.loader is None:
            return []
        out: list[dict[str, Any]] = []
        for rec in self.loader.records.values():
            out.append(
                {
                    "id": rec.dir_name,
                    "module_path": rec.module_path,
                    "name": rec.name,
                    "version": rec.version,
                    "state": rec.state,
                    "error": (rec.error or "").strip().splitlines()[-1]
                    if rec.error
                    else "",
                }
            )
        return out

    # ---- 调用（Web 层用它派发） ----

    # ---- 插件详情（面板「插件详情」用） ----

    #: 事件类型 → 中文（**逐字照真实现** `plugin_service.py:99-109` 的
    #: `translated_event_type`）。少一项就会让详情页显示成 `EventType.xxx`。
    EVENT_TYPE_ZH: dict[str, str] = {
        "AdapterMessageEvent": "平台消息下发时",
        "OnLLMRequestEvent": "LLM 请求时",
        "OnLLMResponseEvent": "LLM 响应后",
        "OnAgentBeginEvent": "Agent 开始运行时",
        "OnAgentDoneEvent": "Agent 运行完成后",
        "OnDecoratingResultEvent": "回复消息前",
        "OnCallingFuncToolEvent": "函数工具",
        "OnAfterMessageSentEvent": "发送消息后",
        "OnPluginErrorEvent": "插件报错时",
    }

    #: 组件类型顺序（**逐字照真实现** `plugin_service.py:45-52` 的
    #: `PLUGIN_COMPONENT_TYPE_ORDER`）。面板按它分组渲染。
    COMPONENT_TYPE_ORDER: tuple[str, ...] = (
        "page", "skill", "command", "llm_tool", "listener", "hook",
    )

    def _handlers_of(self, module_path: str) -> list[Any]:
        """取某插件注册的全部 handler —— **含子模块**。

        🔴 **不能用注册表的 `get_handlers_by_module_name()`**（它是**精确**相等，
           见 `registry.py:131-145` 那段说明），也不能拿 `module_path` 直接加后缀。

        ⚠️ 这里有个**只看数量发现不了**的坑（我第一版就踩了）：
           `module_path` 是 `<目录名>.main`，若按 `<目录名>.main.` 做前缀，
           **子模块 `<目录名>.atrelay` 永远匹配不上**。而当时总数看起来是对的
           （113 == 113）—— 因为少的那 1 个 handler 被 1 个 `page` 组件
           **恰好补平**了。**数量相等 ≠ 组成正确**。
           判据必须是"某个具体名字在不在"，见
           `host/tests/test_plugin_detail.py::test_real_companion_components`。

        所以前缀取**顶层包名**（= `PluginRecord.dir_name`），与加载器
        `_count_plugin_handlers`（用 `dir_name`）**同一口径** ——
        两边算的必须是同一个集合，否则详情页与 `handler_count` 会长期不一致。
        """
        from astrbot_host.registry import star_handlers_registry

        package = module_path.split(".")[0]   # `<目录名>.main` → `<目录名>`
        prefix = f"{package}."
        return [
            h
            for h in star_handlers_registry.all_handlers()
            if h.handler_module_path == package
            or h.handler_module_path.startswith(prefix)
        ]

    def plugin_detail(self, plugin_id: str) -> dict[str, Any] | None:
        """一个插件的详情（照真实现 `GET /api/v1/plugins/by-id` 的形状）。

        Args:
            plugin_id: 插件**目录名**（`PluginRecord.dir_name`，面板上的 `id`）。

        Returns:
            详情 dict；**没有这个插件时返回 `None`**（由调用方转 404 ——
            不许在这里编一个空壳返回，那会让"插件不存在"看起来像"插件没内容"）。
        """
        from astrbot_host.registry import EventType

        if self.loader is None:
            return None
        rec = self.loader.records.get(plugin_id)
        if rec is None:
            return None

        # 🔴 `partial` 拆包：本项目踩过 —— `functools.partial.__module__` **恒为
        #    `'functools'`**（不是被包函数的模块），按 `__module__` 判归属时
        #    重载后永远匹配不上。这里判归属用的是注册表里的 `handler_module_path`
        #    （在**注册时**就记下了），所以不受这个坑影响；下面这条 `_unwrap`
        #    只用于读函数名（显示用），照旧先拆包。
        def _unwrap(fn: Any) -> Any:
            return getattr(fn, "func", fn)

        handlers = self._handlers_of(rec.module_path)

        components: list[dict[str, Any]] = []

        # ---- page：来自 metadata.yaml 的 `pages:`（已校验入口 HTML 存在） ----
        for page in rec.pages or []:
            page_name = page.get("name", "")
            components.append(
                {
                    "type": "page",
                    "name": page.get("title") or page_name,
                    "title": page.get("title") or page_name,
                    "page_name": page_name,
                    "description": "Plugin Page entry",
                    "plugin_name": rec.dir_name,
                    "plugin_id": rec.dir_name,
                }
            )

        # ---- command / listener / hook：按事件类型 + 过滤器分类 ----
        try:
            from astrbot_host.compat.core.star.filter.command import CommandFilter
            from astrbot_host.compat.core.star.filter.command_group import (
                CommandGroupFilter,
            )
            from astrbot_host.compat.core.star.filter.permission import (
                PermissionType,
                PermissionTypeFilter,
            )
            from astrbot_host.compat.core.star.filter.regex import RegexFilter
        except Exception:  # noqa: BLE001 — 兼容层缺类时**如实降级**，不静默装作没有
            CommandFilter = CommandGroupFilter = RegexFilter = None  # type: ignore[assignment]
            PermissionTypeFilter = PermissionType = None  # type: ignore[assignment]

        def _cmd_of(flt: Any) -> str:
            """过滤器 → 指令名（照真实现的 `cmd` 字段）。"""
            if CommandGroupFilter is not None and isinstance(flt, CommandGroupFilter):
                return flt.group_name
            if CommandFilter is not None and isinstance(flt, CommandFilter):
                # `parent_command_names` 里是父级链，拼出完整指令名
                parents = [p for p in (flt.parent_command_names or []) if p]
                if parents:
                    return " ".join(parents) + " " + flt.command_name
                return flt.command_name
            return ""

        def _is_admin(flt: Any) -> bool:
            if PermissionTypeFilter is None or PermissionType is None:
                return False
            return (
                isinstance(flt, PermissionTypeFilter)
                and flt.permission_type == PermissionType.ADMIN
            )

        for h in handlers:
            et = h.event_type
            filters = list(h.event_filters or [])
            zh = self.EVENT_TYPE_ZH.get(et.name, et.name)
            base = {
                "name": h.handler_name,
                "description": (h.desc or "").strip() or "无描述",
                "event_type": et.name,
                "event_type_h": zh,
                "handler_name": h.handler_name,
                "handler_full_name": h.handler_full_name,
            }

            if et == EventType.OnCallingFuncToolEvent:
                # LLM 工具（function calling）。真实现用 `cmd:"自动触发"`。
                # ⚠️ `_handler_obj` 是**临时**字段（下面拿去工具表里认配对，
                #     `pop` 掉后才出函数）—— 它不进最终响应（函数对象不可 JSON 化）。
                components.append({**base, "type": "llm_tool",
                                   "cmd": "自动触发", "display_type": "无",
                                   "_handler_obj": _unwrap(h.handler)})
                continue

            if et == EventType.AdapterMessageEvent:
                cmd_flts = [f for f in filters
                            if CommandFilter is not None and isinstance(f, CommandFilter)]
                grp_flts = [f for f in filters
                            if CommandGroupFilter is not None and isinstance(f, CommandGroupFilter)]
                rgx_flts = [f for f in filters
                            if RegexFilter is not None and isinstance(f, RegexFilter)]

                if grp_flts:
                    flt = grp_flts[0]
                    components.append({
                        **base, "type": "command", "display_type": "指令组",
                        "cmd": _cmd_of(flt) or "未知",
                        "has_admin": any(_is_admin(f) for f in filters),
                        "subcommands": [
                            {"name": _unwrap(getattr(s, "handler", s)).__name__
                             if hasattr(s, "handler") else str(getattr(s, "command_name", "")),
                             "cmd": _cmd_of(s) or "未知"}
                            for s in (getattr(flt, "sub_command_filters", None) or [])
                        ],
                    })
                    continue

                if cmd_flts:
                    flt = cmd_flts[0]
                    components.append({
                        **base, "type": "command", "display_type": "指令",
                        "cmd": _cmd_of(flt) or "未知",
                        "has_admin": any(_is_admin(f) for f in filters),
                    })
                    continue

                if rgx_flts:
                    components.append({
                        **base, "type": "command", "display_type": "正则匹配",
                        "cmd": getattr(rgx_flts[0], "regex", "") or "未知",
                        "has_admin": any(_is_admin(f) for f in filters),
                    })
                    continue

                components.append({**base, "type": "listener",
                                   "display_type": "事件监听器", "cmd": "未知"})
                continue

            # 其余事件类型 = 钩子（照真实现）
            components.append({**base, "type": "hook", "display_type": zh})

        # ---- llm_tool：第二条来源（工具表本身）+ 给已有的补 `active` ----
        # 🔴 **去重键必须是 handler 函数对象本身，不能是名字**（我第一版就按名字，
        #    实测抓到的假象）：同一件事在两侧的名字**不一样** ——
        #      handler 侧 `handler_name` = `memory_companion_recall_tool`
        #      工具表侧  `spec.name`      = `memory_companion_recall`
        #    按名去重会**一个都不命中** → 同一个工具被列两遍（实测 7 个变 14 个）。
        #    实测两侧的 `getattr(handler, "func", handler)` 是**同一个对象**
        #    （`id()` 完全相同），所以用它当键。
        #
        # 工具表更权威的两件事：① 真实签名与描述（handler 的 `desc` 常为空）；
        # ② `active`（这个工具当前有没有被停用）—— handler 侧没有这个概念。
        tool_by_handler_id: dict[int, Any] = {}
        tool_manager = None
        # 🔴 用 `get_llm_tool_manager()`（真实现里 `Context` 就是这个方法名，
        #    见 `context.py:358` 返回 `provider_manager.llm_tools`）——
        #    **不要**去摸 `context.llm_tools` 这种属性，它不存在（我第一版就写错了）。
        if self.context is not None:
            getter = getattr(self.context, "get_llm_tool_manager", None)
            if callable(getter):
                try:
                    tool_manager = getter()
                except Exception:  # noqa: BLE001
                    logger.exception("取 LLM 工具管理器失败（详情页的工具清单会不完整）")
        if tool_manager is not None:
            for spec in getattr(tool_manager, "func_list", []) or []:
                owner = getattr(spec, "handler_module_path", None)
                raw = getattr(spec, "handler", None)
                unwrapped = _unwrap(raw)
                if owner is None:
                    # 归属不明 → 拿 handler 的模块路径兜底（先拆 partial）
                    owner = getattr(unwrapped, "__module__", None)
                if owner != rec.module_path:
                    continue
                tool_by_handler_id[id(unwrapped)] = spec

        # 给 handler 侧已列出的 llm_tool 补上工具表才有的信息
        for c in components:
            if c["type"] != "llm_tool":
                continue
            # ⚠️ 这里用 `getattr` 读 `_handler_obj`、**不 pop** ——
            #    临时字段的统一清除只交给下面**那一个**地方（单一职责）。
            #    原先这里顺手 pop，后果是那个"兜底清除"**永远走不到**
            #    ⇒ 它成了不可达代码 = 没有防护。这是注入验证当场抓到的：
            #    把兜底那块删掉，测试全绿（因为删的是一段永不执行的代码）。
            spec = tool_by_handler_id.pop(
                id(_unwrap(c.get("_handler_obj"))), None
            )
            if spec is None:
                continue
            if (getattr(spec, "description", "") or "").strip():
                c["description"] = spec.description.strip()
            c["active"] = bool(getattr(spec, "active", True))

        # 工具表里**多出来**的（没有对应 handler 注册）也要列 —— 它们照样能被调用，
        # 漏掉就是"详情页少了东西"（本项目最忌的静默缺失）。
        for spec in tool_by_handler_id.values():
            components.append({
                "type": "llm_tool",
                "name": spec.name,
                "description": (getattr(spec, "description", "") or "").strip() or "无描述",
                "cmd": "自动触发",
                "display_type": "无",
                "event_type": EventType.OnCallingFuncToolEvent.name,
                "event_type_h": self.EVENT_TYPE_ZH.get("OnCallingFuncToolEvent", ""),
                "handler_name": spec.name,
                "active": bool(getattr(spec, "active", True)),
            })

        # ---- skill：我们没有技能体系 → **如实为空**（不编造） ----
        # （真实现读 `SkillManager().list_skills()`；我们没有对应物。）

        # 🔴 **兜底清除下划线开头的临时字段**（`_handler_obj` 是函数对象，
        #    留在响应里会让 `jsonify` 直接抛 `TypeError: not JSON serializable`）。
        #    ⚠️ 不能只依赖"上面那条 pop 一定跑到" —— 没有配对上的工具会走到别的
        #    分支，那种情况下 pop 不会执行。这里统一扫一遍，保证**任何**路径都干净。
        for c in components:
            for k in [k for k in c if k.startswith("_")]:
                c.pop(k, None)

        # ---- 排序（照真实现 PLUGIN_COMPONENT_TYPE_ORDER） ----
        order = {t: i for i, t in enumerate(self.COMPONENT_TYPE_ORDER)}
        components.sort(key=lambda c: order.get(c["type"], 99))

        state_text = {
            "loaded": "运行中",
            "inactivated": "已停用（未加载）",
            "failed": "加载失败",
            "not_loaded": "未加载",
        }.get(rec.state, rec.state)

        return {
            "id": rec.dir_name,
            "name": rec.name,
            "display_name": rec.name,
            "module_path": rec.module_path,
            "dir_name": rec.dir_name,
            "version": rec.version,
            "author": rec.author,
            "desc": rec.desc,
            "state": rec.state,
            "state_text": state_text,
            "error": (rec.error or "").strip().splitlines()[-1] if rec.error else "",
            "traceback": rec.traceback or "",
            "handler_count": rec.handler_count,
            "llm_tool_bind_count": rec.llm_tool_bind_count,
            "web_api_count": rec.web_api_count,
            "task_count": rec.task_count,
            "pages": list(rec.pages or []),
            "inactivated": rec.inactivated,
            "components": components,
        }

    def plugin_readme(self, plugin_id: str, *, limit: int = 200_000) -> tuple[str, bool] | None:
        """读插件的 `README.md`（照真实现 `plugin_service.py:1924-1981` 只找它）。

        Returns:
            `(内容, 是否被截断)`；插件不存在或没有 README 时返回 `None`
            （**如实为空** —— 不编造内容，也不给一个"空的 README"）。
        """
        if self.loader is None:
            return None
        rec = self.loader.records.get(plugin_id)
        if rec is None:
            return None
        for name in ("README.md", "readme.md"):
            p = Path(rec.path) / name
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text) > limit:
                    return text[:limit], True
                return text, False
        return None

    async def call_api(
        self,
        plugin_path: str,
        *,
        request_obj: Any,
        method: str = "GET",
        username: str | None = None,
    ) -> Any:
        """派发一次插件 Web API 调用（照真实现 `_call_plugin_extension`）。

        Args:
            plugin_path: 插件登记的路由（如 `astrbot_plugin_x/page/overview`）。
            request_obj: 底层请求对象（Quart `request`），会被包成 `PluginRequest`。
            method: HTTP 方法。
            username: 已鉴权用户名（我们单用户，通常给固定值）。

        Returns:
            插件返回的原样结果（dict / list / `PluginResponse` / Quart Response）。

        Raises:
            LookupError: 没有匹配的路由（**显式报错，不静默返回空**）。
        """
        from .compat.api.web import PluginRequest, bind_request_context
        from .web_router import match_registered_web_api

        ctx = self.context
        if ctx is None:
            raise RuntimeError("PluginHost 尚未 start()，无法调用插件 API")

        matched = match_registered_web_api(
            ctx.registered_web_apis, plugin_path, method
        )
        if matched is None:
            # ⚠️ 带上"最接近的候选"，否则用户只看到 404 无从下手
            near = [
                a.route for a in self.report.mounted
                if a.route.startswith(f"/{plugin_path.lstrip('/').split('/')[0]}")
            ][:5]
            hint = f"；该插件已挂载的相近路由：{near}" if near else ""
            raise LookupError(f"插件未登记该路由：{method} /{plugin_path}{hint}")

        handler, path_params = matched
        plugin_name = plugin_path.strip("/").split("/", 1)[0] or None

        plugin_request = PluginRequest(
            request_obj,
            path_params=dict(path_params),
            plugin_name=plugin_name,
            username=username,
        )
        # 🔴 把请求绑进 contextvar —— 插件的 handler 是靠
        #    `from astrbot.api.web import request` 这个**全局代理**拿请求的
        #    （实测 `page_api.py:1474`：`await request.get_json(...)`）。
        #    不绑 → 插件一访问 request 就抛 RuntimeError。
        with bind_request_context(plugin_request):
            result = handler(**path_params)
            if hasattr(result, "__await__"):
                result = await result
            return result
