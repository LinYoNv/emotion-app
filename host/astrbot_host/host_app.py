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
            f"· 已初始化 {len(self.initialized)} · 拓展页 {len(self.pages)}",
        ]
        if self.failed:
            lines.append(f"  加载失败 {len(self.failed)}：")
            lines += [f"    ❌ {n} → {e}" for n, e in self.failed]
        if self.init_failed:
            lines.append(f"  初始化失败 {len(self.init_failed)}：")
            lines += [f"    ❌ {n} → {e}" for n, e in self.init_failed]
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
    ) -> None:
        self.plugin_dirs = [Path(d) for d in plugin_dirs]
        #: 传给加载器的镜像目录（`None` = 不镜像，直接在源目录加载）
        self.mirror_root = mirror_root
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

    # ---- 调用（Web 层用它派发） ----

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
