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
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot")


def _plugins_data_root() -> Path:
    """插件数据根（`data/plugins`）—— 与 `star_tools._data_root` 同一个来源。

    ⚠️ 走 `get_data_root()` 而不是重新拼路径：插件自己也是从那里拿目录的，
       另拼一份必然在某次配置变更后与插件手里的那份漂移。
    """
    from .compat.core.star.star_tools import get_data_root

    return Path(get_data_root())

#: 插件「配置文件」上传的单文件上限（真实现用 `MAX_FILE_BYTES`；这里 12 MiB，
#: 够放参考图这类用途，又不至于让面板卡住）。
_MAX_CONFIG_FILE_BYTES = 12 * 1024 * 1024

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
        inactivated_writer: Any = None,
    ) -> None:
        self.plugin_dirs = [Path(d) for d in plugin_dirs]
        #: 传给加载器的镜像目录（`None` = 不镜像，直接在源目录加载）
        self.mirror_root = mirror_root
        #: 🔴 传给加载器的**停用名单**（模块路径，三种写法都收 —— 见
        #: `PluginLoader.normalize_module_path`）。由装配层从配置读出来再传进来：
        #: 单向依赖铁律 —— `host/` 不许反过来 import `emotion_app`。
        self.inactivated = list(inactivated or ())
        #: 🔴 **停用名单的写端口**（`Callable[[str], str]`：传入要剔除的插件目录名，
        #: 返回被剔除的 module_path）。
        #:
        #: 为什么是回调而不是让宿主直接写配置：`host/` 不许反向 import
        #: `emotion_app`（单向依赖铁律，见上一段的注释）。而**卸载插件时
        #: 必须**把它从停用名单里剔掉 —— 否则下次启动会报
        #: `停用名单里有 N 项对不上任何插件目录`，用户会追一条**指向已删插件**的
        #: 假警告。
        #: ⚠️ 没注入时 `forget_inactivated()` **如实 warning 并返回空串**，
        #:    不假装清理成功（本项目铁律：能力缺失必须显式暴露）。
        self.inactivated_writer = inactivated_writer
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

        await self._rebuild_report(load_report=load_report)

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

    async def _rebuild_report(self, *, load_report: Any = None) -> None:
        """按 `loader.records` **重算** `self.report`（启动 / 重载 / 卸载共用）。

        🔴 为什么必须收敛成一处（2026-10-31）：

        重载与卸载都会改 `loader.records`。如果它们各自去改 `report` 的几个字段，
        就会出现"面板说 6 个插件、实际 5 个"这种**没有任何报错**的漂移
        —— 而这正是本项目最忌的静默不一致。现在三条路径都只调这一个方法，
        `report` 永远等于 `records` 的投影。

        Args:
            load_report: 启动时传入（含 `unmatched_inactivated` 这类**只能由
                 加载过程算出**的信息 —— records 推不出来）。重载/卸载时传
                 `None`：那种情况下停用名单的 unmatched 由
                 `HostPorts` 侧重新判（见 `PluginHost.forget_inactivated`）。
        """
        records = list(self.loader.records.values()) if self.loader else []

        loaded = [r for r in records if r.ok]
        failed = [r for r in records if r.state == "failed"]
        inactivated = [r for r in records if r.state == "inactivated"]

        self.report.loaded = len(loaded)
        self.report.loaded_plugins = [r.dir_name for r in loaded]
        self.report.failed = [
            (r.dir_name, (r.error or "").strip().splitlines()[-1] if r.error else "")
            for r in failed
        ]
        # 🔴 停用的插件**单独一列**（不进 `loaded`、也不进 `failed`）——
        #    状态报告里必须能区分"没跑"是因为停用，还是因为坏了。
        self.report.inactivated = [r.module_path for r in inactivated]

        if load_report is not None:
            self.report.unmatched_inactivated = list(
                getattr(load_report, "unmatched_inactivated", []) or []
            )
        else:
            # 重载/卸载后：重新判"名单里有没有对不上任何插件的项"。
            # ⚠️ 不重新判就会留着一个已经删掉的插件名，而加载器下次启动才报 ——
            #    "删掉一条记录 ≠ 它不会再出现"（本项目已记）。
            known = {r.module_path for r in records}
            self.report.unmatched_inactivated = sorted(
                p for p in (self.loader.inactivated if self.loader else set())
                if p not in known
            )

        # 归档已装载的 API —— `_collect_apis` 直接读 `context.registered_web_apis`，
        # 而 detach 已经把该插件的条目摘掉了，所以这里天然是"剩下那些"。
        self.report.mounted = self._collect_apis(load_report)

        # 🔴 **拓展页发现**：只收集"入口 HTML 真的存在"的页。
        #    情绪报的「插件没有配置入口」根因就在这 —— 上一轮只挂了 API，
        #    没有把插件声明的 `pages:` 变成可访问的页面路由。
        from .plugin_pages import discover_pages

        self.report.pages = discover_pages(
            loaded,
            plugin_root_of=lambda rec: rec.path,
        )
        if self.report.pages and load_report is not None:
            # ⚠️ 只在**启动**那条路径打这一行：重载/卸载每次都打会把日志淹掉，
            #    而"发现 N 个拓展页"只在启动时有诊断价值。
            logger.info(
                "发现 %d 个插件拓展页：%s",
                len(self.report.pages),
                ", ".join(f"{p.plugin_id}/{p.page_name}" for p in self.report.pages),
            )

    async def _initialize_reloaded(self, rec: Any, *, timeout: float) -> None:
        """给**重载后**的插件补一次 `initialize()`（与启动路径同一套隔离 + 超时）。

        ⚠️ 不直接复用 `_initialize_all()`：那个收的是 records 列表、并且把结果
           append 到 `report.initialized`。重载是**单个**插件，语义不同
           （而且重载失败必须能告诉用户是哪一步失败的）。
        """
        import asyncio
        import time

        started = time.monotonic()
        try:
            await asyncio.wait_for(rec.instance.initialize(), timeout=timeout)
        except asyncio.TimeoutError:
            msg = f"TimeoutError: initialize() 超过 {timeout:.0f}s 未返回"
            rec.error = msg
            logger.error("插件 initialize() 超时（重载）：%s → %s", rec.dir_name, msg)
            return
        except Exception as exc:  # noqa: BLE001
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.traceback = traceback.format_exc()
            logger.error(
                "插件 initialize() 失败（重载）：%s\n%s", rec.dir_name, rec.traceback
            )
            return
        elapsed = time.monotonic() - started
        if elapsed >= timeout * 0.5:
            logger.warning(
                "插件 initialize() 偏慢（重载）：%s 用了 %.1fs（上限 %.0fs）",
                rec.dir_name,
                elapsed,
                timeout,
            )
        if rec.dir_name not in self.report.initialized:
            self.report.initialized.append(rec.dir_name)

    def forget_inactivated(self, plugin_id: str) -> str:
        """把一个插件的 module_path 从**已保存的停用名单**里剔除，返回被剔除的值。

        🔴 为什么卸载时必须做：停用名单里的元素是 module_path。插件目录被移走后，
        那条元素就"对不上任何插件"了 —— 下次启动会报
        `停用名单里有 N 项对不上任何插件目录（拼写错误？）`。
        用户看到的是一条**指向已删插件**的警告，而它其实是正常的。

        ⚠️ 写配置这件事**必须走宿主的注入口**（`inactivated_writer`），
           不许在这里直接 import `emotion_app` 去写盘 —— `host/` 单向依赖
           `emotion_app` 是不允许的（见 `PluginHost` 的 `config_profile_path` 注释）。
        """
        writer = getattr(self, "inactivated_writer", None)
        if not callable(writer):
            # 如实：没注入口就说明"没能清理名单"，而不是假装清理了
            logger.warning(
                "没有注入 inactivated_writer —— 停用名单里 %s 的残留项未被清理"
                "（下次启动会报 unmatched）",
                plugin_id,
            )
            return ""
        return str(writer(plugin_id) or "")

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

            {"id": 目录名, "module_path": "<目录名>.main", "name": 名字,
             "display_name": 中文显示名（可空）, "pages": [拓展页...],
             "version": ..., "state": "loaded|failed|inactivated|not_loaded",
             "error": "..."}

        🔴 `display_name` 与 `pages` 是 2026-10-09 为**侧边栏插件分组**补的：
           侧边栏要按"有拓展页的插件"列出子项，需要①中文名②哪些插件有页面。
           ⚠️ 以前没有这两项，前端只能拿 `name`（可能是目录名）去猜 ——
              而 `pages` 更是**完全拿不到**（只有单独一条 `/plugins/pages` 接口）。
           ⚠️ 不做成"只列有页面的插件"：侧边栏也许将来要列全部插件，
              过滤是**显示方**的事（数据层给全，语义单一）。
        """
        if self.loader is None:
            return []
        #: 源码目录解析**每轮只做一次前缀判断**就够（`resolve_source_dir` 内部
        #: 只 stat 几个 `<root>/<dir>/main.py`），但六个插件 × 两个根 = 12 次
        #: stat 也在同一个请求里跑 —— 所以提到循环外，一次算好。
        from .plugin_lifecycle import resolve_source_dir

        out: list[dict[str, Any]] = []
        for rec in self.loader.records.values():
            source_path = resolve_source_dir(self.loader, rec.dir_name)
            out.append(
                {
                    "id": rec.dir_name,
                    "module_path": rec.module_path,
                    "name": rec.name,
                    # ⚠️ **可能为空**（不是每个插件都声明 display_name）。
                    #    显示方用 `display_name || name` 退化 —— 数据层不硬填。
                    "display_name": rec.display_name,
                    "version": rec.version,
                    # 🔴 照 AstrBot 真卡片（`InstalledPluginsTab`）补的三个字段：
                    #    那张卡片要显示 **logo / 分类 / 标签**，没有它们做不出来。
                    #    `icon` 是**相对插件目录**的文件名（不是 URL），前端拼下载地址。
                    "author": rec.author,
                    "desc": rec.desc,
                    "icon": rec.icon,
                    "category": rec.category,
                    "tags": list(rec.tags),
                    "state": rec.state,
                    # 拓展页（`pages/` 目录扫出来的，照真实现）。侧边栏分组要用。
                    # ⚠️ 形状已由 `discover_plugin_pages` 定好：`{name, title}`。
                    "pages": [dict(pg) for pg in rec.pages],
                    "error": (rec.error or "").strip().splitlines()[-1]
                    if rec.error
                    else "",
                    # ---- 2026-10-31：卡片动作菜单的**可用性判据** ----
                    #
                    # 🔴 为什么由后端算而不是前端自己判：
                    #    前端只有面板看到的那点信息（`state`/`module_path`），
                    #    判"能不能重载"还要知道①它是不是被停用（停用了重载没意义）
                    #    ②源码目录能不能解析出来（开了镜像时 `plugin_dirs` 指向副本）；
                    #    判"能不能删"更是必须解析**真实源码目录**。
                    #    前端自己猜 = 又一处"两边各算一份、迟早漂移"。
                    #    ⚠️ 而且**不可用时必须给出原因**：只给一个灰按钮，
                    #    用户会以为"这功能坏了"，而不是"这个插件不支持"。
                    "can_reload": self._can_reload(rec),
                    "reloadable_reason": self._reload_blocker(rec),
                    "can_uninstall": source_path is not None,
                    "uninstallable_reason": ""
                    if source_path is not None
                    else "解析不到源码目录（可能开了源码镜像），为避免删错目录已禁用",
                    #: 会被移动/删除的那个目录 —— 二次确认弹窗**必须**显示它，
                    #: 否则用户无法判断"我要删的到底是哪一份"。
                    "source_path": str(source_path) if source_path else "",
                    #: 卸载时会**连带删除**的可选产物路径（面板二次确认要逐条列出来）。
                    #:
                    #: 🔴 这是给"勾了才会删"的两项用的：用户在确认框里必须看到
                    #: **具体会删掉哪个文件/目录**，而不是一句"配置和数据"。
                    #: 默认勾选为 false ⇒ 这两项不删 ⇒ 卸载后重新安装，
                    #: 配置与记忆还在（官方同此语义）。
                    "config_path": str(self._plugin_config_file(rec.dir_name)),
                    "data_path": str(self._plugin_data_dir(rec.dir_name)),
                }
            )
        return out

    def _plugin_config_file(self, plugin_id: str) -> Path:
        """插件的配置文件路径（**可能不存在** —— 只是"如果要删，会是这个"）。

        照官方命名：`<plugin_config_path>/<root_dir_name>_config.json`
        （`star_manager.py:1538-1541`）。我们的数据根是 `data/plugins`，
        所以是 `data/plugins/config/<id>_config.json`。
        """
        return _plugins_data_root() / "config" / f"{plugin_id}_config.json"

    def _plugin_data_dir(self, plugin_id: str) -> Path:
        """插件的持久化数据目录（**可能不存在**）。

        ⚠️ 与 `StarTools.get_data_dir()` **同一口径**（它建的就是
           `<data_root>/<plugin_name>`）—— 两边不一致会让"删除持久化数据"
           删不到用户真正的数据（官方就有这个毛病，见契约 §0）。
        """
        return _plugins_data_root() / plugin_id

    def _reload_blocker(self, rec: Any) -> str:
        """重载不了时**说明原因**（空串 = 可以重载）。

        ⚠️ 判据是契约 §1 那两条，**从数据推出来**，不是拍板名单：
        `can_reload = 有 main.py + 不是 inactivated`。
        """
        if rec.inactivated:
            return "插件处于停用状态，重载不会让它跑起来 —— 请先启用并重启服务"
        if self.loader is None:
            return "插件宿主尚未完成加载"
        # 🔴 「有 main.py」这一条必须查**记录指向的那个目录**（`rec.path`），
        #    不是源码根：加载器可能正从镜像副本里加载，而那份才是"正在跑的代码"。
        #    删掉 `main.py` 的插件重载只会得到一个"没有 Star 子类"的失败，
        #    而原因和按钮状态都会指错地方。
        if not (Path(str(rec.path)) / "main.py").is_file():
            return "插件目录里没有 main.py（入口文件缺失），重载不可能会成功"
        return ""

    def _can_reload(self, rec: Any) -> bool:
        return not self._reload_blocker(rec)

    # ---- 生命周期薄方法（委托给 `plugin_lifecycle`，Web 层直接用这三个） ----
    #
    # 🔴 为什么还要包一层"薄方法"（路由明明可以直接 import 那个模块）：
    #    ① 装配测试可以只依赖 `PluginHost` 这一个对象，不必知道实现住在哪个模块；
    #    ② 将来若换实现（比如改成子进程重载），只改这三行里的转发；
    #    ③ 这三个名字与 `plugins_view()` 的字段、契约里的路由**一一对应**，
    #       读代码时"面板那个按钮调的是什么"不用跨文件拼。
    #    ⚠️ 它们**不含任何逻辑**：判断、错误分类、磁盘操作全在 `plugin_lifecycle`
    #       —— 那样"同一概念两条实现路径"就不会从这两行开始长出来。

    async def reload_plugin(self, plugin_id: str) -> Any:
        """就地重载一个插件（**不需要重启服务**）。返回 `ReloadResult`。

        Raises:
            `plugin_lifecycle.PluginLifecycleError`（带 `code`）——
            `inactivated` / `not_loaded` / `unsupported` / `reload_failed`。
        """
        from .plugin_lifecycle import reload_plugin as _reload_plugin

        return await _reload_plugin(self, plugin_id)

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> Any:
        """把插件源码目录**移入回收站**并从宿主摘干净。返回 `UninstallResult`。

        Raises:
            `plugin_lifecycle.PluginLifecycleError`（带 `code`）——
            `not_loaded` / `unsupported` / `uninstall_failed`。
        """
        from .plugin_lifecycle import uninstall_plugin as _uninstall_plugin

        return await _uninstall_plugin(
            self,
            plugin_id,
            delete_config=delete_config,
            delete_data=delete_data,
        )

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

    def plugin_config(self, plugin_id: str) -> dict[str, Any] | None:
        """读一个插件的配置（照真实现 `GET /api/v1/plugins/config`）。

        ## 真实现形状（`config_service.py:844-863` + `api/plugins.py:726-732`）

        ::

            {
              "plugin_name": "<插件目录名>",
              "metadata": {                      # ⚠️ 无 schema 时是 None
                 "<插件目录名>": {
                    "description": "<插件名> 配置",
                    "type": "object",
                    "items": <插件 _conf_schema.json 的原文>,
                 }
              },
              "config": <当前实际值>,
              "i18n": {},
            }

        🔴 **`metadata.items` 就是插件自带的 `_conf_schema.json` 原文** ——
           配置项的标签/类型/默认值/hint/可选项**全部由插件自己声明**，
           宿主不猜、不补。这一条决定了前端能画出通用表单。

        Returns:
            上述 dict；**插件不存在时返回 `None`**（由调用方转 404 ——
            不许在这里编个空壳，那会让"插件不存在"看起来像"插件没配置"）。
        """
        if self.loader is None:
            return None
        rec = self.loader.records.get(plugin_id)
        if rec is None:
            return None

        cfg = rec.config
        # ⚠️ `_build_plugin_config()` 失败时会退回**裸 dict**（并已记 ERROR）——
        #    那种情况下没有 `schema` 可用，如实报 `metadata=None`，
        #    而不是假装"这个插件没有配置项"（两者给用户的下一步动作完全不同）。
        schema = getattr(cfg, "schema", None)
        metadata: dict[str, Any] | None = None
        if isinstance(schema, dict):
            metadata = {
                plugin_id: {
                    "description": f"{plugin_id} 配置",
                    "type": "object",
                    "items": schema,
                }
            }

        # 当前值：`AstrBotConfig` 是 `dict` 子类，转成纯 dict 交出去。
        current: dict[str, Any] = {}
        if isinstance(cfg, dict):
            current = {k: v for k, v in cfg.items()}
        elif cfg is not None:
            logger.warning(
                "插件 %s 的配置对象不是 dict（%s）—— 面板将显示为空",
                plugin_id,
                type(cfg).__name__,
            )

        return {
            "plugin_name": plugin_id,
            "metadata": metadata,
            "config": current,
            "i18n": {},
        }

    async def save_plugin_config(self, plugin_id: str, config: dict[str, Any]) -> str:
        """保存插件的配置，**并热重载该插件**（照真实现）。

        真实现（`config_service.py:989-1018`）：`validate_config` →
        `config.save_config(...)` → **`await plugin_manager.reload(plugin_name)`**，
        然后回一句「保存插件 X 成功~ 机器人正在热重载插件。」

        🔴 **为什么必须重载插件**（不是顺手多做的事）：
            插件在 `__init__` 里就把配置读进自己的一堆属性了；只改文件不重载，
            用户在面板上改的**不会生效**，而他看到的是"保存成功"
            —— 正是本项目最忌的"静默不生效"。

        ⚠️ 我们**没有** `validate_config`（真实现那个是给全局配置做校验的）。
           所以这里不做额外校验 —— **不假装校验过**。

        Returns:
            给用户看的一句话（中文，照真实现口吻）。

        Raises:
            KeyError: 插件不存在。
            RuntimeError: 该插件没有可写配置对象（存不了，**不静默**）。
        """
        if self.loader is None:
            raise KeyError(plugin_id)
        rec = self.loader.records.get(plugin_id)
        if rec is None:
            raise KeyError(plugin_id)

        cfg = rec.config
        save = getattr(cfg, "save_config", None)
        if not callable(save):
            # 🔴 **不静默**：这说明 `_build_plugin_config` 退回了裸 dict，
            #    保存必然无效 —— 必须让用户看到"这个插件存不了"，
            #    而不是回一句"已保存"然后什么都没发生。
            raise RuntimeError(
                f"插件 {plugin_id} 的配置对象不可写（没有 save_config）—— "
                "它可能没有 _conf_schema.json，或加载时构造失败，请查看服务日志。"
            )

        # ⚠️ 浅合并（照真实现 `AstrBotConfig.save_config` → `self.update(...)`）：
        #    只覆盖前端传来的键，其余保留。**不是**先清空再写。
        save(config)

        # ==================================================================
        # 🔴 关于"热重载"：**我们目前没有这个能力，如实说，不假装**。
        #
        # 真实现会 `await plugin_manager.reload(plugin_name)`，文案是
        # 「保存插件 X 成功~ 机器人正在热重载插件。」。
        #
        # ⚠️ 我第一版写成了 `getattr(self.loader, "reload", None)` +
        #    `inspect.isawaitable(...)` —— 那是**我假设的 API**：
        #    全库 grep 确认 `PluginLoader` **没有 `reload`/`unload`**
        #    （只有 `load_all`/`load_one`/`initialize_all`）。
        #    它永远不会抛错、也永远不会生效 ⇒ 静默地走"没有热重载能力"那个分支，
        #    而日志里连一行都不打 —— 这正是本项目最忌的"编造 API + 静默"。
        #
        # ✅ 现在的做法：**如实告知需要重启**，并给出与插件启停一致的措辞
        #    （`config_profile.py` 里"改完需要重启服务才生效"是同一个约定）。
        #    将来真做了重载，改这一处即可。
        # ==================================================================
        logger.info(
            "插件 %s 的配置已保存（需要重启服务才能生效）",
            plugin_id,
            extra={"data": {"plugin": plugin_id, "keys": sorted(config.keys())[:20]}},
        )
        return (
            f"插件 {plugin_id} 的配置已保存。"
            "⚠️ 本服务**尚未实现插件热重载**，需要**重启服务**后生效。"
        )

    # ======================================================================
    # 插件「配置文件」管理（照真实现 `ConfigFileService`，2026-10-09 补）
    # ======================================================================
    #
    # 真实现（`dashboard/api/plugins.py:762-814` + `services/config_service.py:1145-1215`）：
    #
    #   GET    /api/v1/plugins/config-files?plugin_id=&config_key=   列出
    #   POST   /api/v1/plugins/config-files?plugin_id=&config_key=   上传
    #   DELETE /api/v1/plugins/config-files?plugin_id=               删除
    #
    # 🔴 **它管理的不是随便什么文件，而是 `_conf_schema.json` 里 `type: "file"`
    #    那些配置项对应的文件** —— 这是最容易误解的一点。
    #    接口里那个 `config_key` 就是**配置项的键路径**（`openai_director_fallback_images`），
    #    文件落在 **插件目录** 的 `files/<键路径>/` 下。
    #
    # ⚠️ 实测本机真有这种配置项（不是空壳能力）：
    #    `astrbot_plugin_nai_image` 的 `openai_director_fallback_images`
    #    （`file_types: [png, jpg, jpeg, webp]`，语义是"精准参考兜底参考图"）。
    #
    # ⚠️ 真实现上传后**只落盘、不写配置值** —— 用户在配置页把
    #    `files/<key>/<name>` 填进那个数组才算真正用上。两个动作是分开的，
    #    这里**保持同一语义**（不替用户做决定）。

    @staticmethod
    def _config_key_to_folder(key_path: str) -> str:
        """把配置键路径转成目录名（照真实现 `_config_key_to_folder`）。

        ⚠️ 与真实现逐字对齐：按 `.` 切分、每段过一遍白名单清洗、用 `/` 连接。
           空则回落 `_`。
        """
        parts = [
            "".join(c for c in seg if c.isalnum() or c in "_-")
            for seg in str(key_path or "").split(".")
            if seg
        ]
        parts = [p for p in parts if p]
        return "/".join(parts) if parts else "_"

    @staticmethod
    def _schema_item(schema: Any, key_path: str) -> dict[str, Any] | None:
        """按 `key_path` 在 schema 里找配置项（照真实现 `_get_schema_item`）。

        ⚠️ 支持点分路径（`a.b.c`）逐层下钻 —— 真实现也是这么找的，
           因为 `file` 类型的字段**可能嵌在 `object` 里**。
        """
        if not isinstance(schema, dict) or not key_path:
            return None
        # 快路径：顶层直接命中
        if key_path in schema and isinstance(schema[key_path], dict):
            return schema[key_path]
        # 点分下钻：每一段要么是**普通键**（`object` 的 `items` 里），
        # 要么本身就是 schema 的一层。两种形状都得走通 ——
        # ⚠️ 我第一版只处理了"键"那一种，`a.b`（`a` 是 object 字段、
        #    `b` 在它的 `items` 里）**找不到**，而那正是插件里最常见的嵌套形状。
        cur: Any = schema
        for seg in str(key_path).split("."):
            if not isinstance(cur, dict):
                return None
            if seg in cur and isinstance(cur[seg], dict):
                cur = cur[seg]
            elif isinstance(cur.get("items"), dict) and seg in cur["items"]:
                nxt = cur["items"][seg]
                cur = nxt
            else:
                return None
        return cur if isinstance(cur, dict) else None

    def _plugin_file_dir(self, plugin_id: str, config_key: str) -> Path | None:
        """插件里某个配置项的文件目录：`<插件目录>/files/<key路径>/`。

        ⚠️ 返回 `None` = 插件不存在（由调用方转 404）。
        ⚠️ **不做目录穿越防护以外的路径拼接** —— `_config_key_to_folder` 已把
           每段洗成白名单字符，所以拼出来的路径不可能逃出插件目录。
        """
        if self.loader is None:
            return None
        rec = self.loader.records.get(plugin_id)
        if rec is None:
            return None
        return Path(rec.path) / "files" / self._config_key_to_folder(config_key)

    def list_plugin_config_files(self, plugin_id: str, config_key: str) -> dict[str, Any] | None:
        """列出一个 `file` 型配置项已经有哪些文件。

        Returns:
            `{"files": ["files/<key>/<name>", ...]}`；插件不存在返回 `None`。
            ⚠️ 目录不存在时返回**空列表**（不是 `None`）—— "这个插件还没有文件"
               与"没有这个插件"是两件事。

        ⚠️ 路径形态与真实现一致：**相对插件目录**且以 `files/` 开头
           （真实现 `config_service.py:1176-1180` 就是这么给的，
            因为配置值里存的就是这个相对路径）。
        """
        root = self._plugin_file_dir(plugin_id, config_key)
        if root is None:
            return None
        if not root.is_dir():
            return {"files": []}
        out: list[str] = []
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out.append(p.relative_to(root.parent.parent).as_posix())
        return {"files": out}

    def plugin_config_file_meta(
        self, plugin_id: str, config_key: str
    ) -> dict[str, Any] | None:
        """该配置项**是不是** `file` 型，以及允许的扩展名。

        🔴 为什么单独有这个：真实现把校验写成
           `if not meta or meta.get("type") != "file": raise ValueError(...)`
           —— 上传/删除都得先过这一关，否则就是"给一个不能存文件的地方存文件"。
           收成一处，三条路由共用（本项目「同一概念不要多条路径」）。

        Returns:
            `{"file_types": [...], "description": str}`；插件不存在或该项
            不是 file 型时返回 `None`。
        """
        if self.loader is None:
            return None
        rec = self.loader.records.get(plugin_id)
        if rec is None:
            return None
        schema = getattr(rec.config, "schema", None)
        meta = self._schema_item(schema, config_key)
        if not isinstance(meta, dict) or meta.get("type") != "file":
            return None
        raw_types = meta.get("file_types") or []
        types = [str(t).lstrip(".").lower() for t in raw_types if str(t).strip()]
        return {
            "file_types": types,
            "description": str(meta.get("description") or ""),
        }

    async def save_plugin_config_file(
        self, plugin_id: str, config_key: str, filename: str, data: bytes
    ) -> dict[str, Any]:
        """保存一个上传的文件到 `<插件目录>/files/<key>/<filename>`。

        🔴 **扩展名必须校验**（照真实现 `config_service.py:1039-1060`）：
           不在 `file_types` 里就拒绝。否则用户能把 `.exe` 传进一个"只收图片"
           的配置项，而插件那边读图失败时大概率是**一行日志**（静默）。

        ⚠️ 文件名清洗：只保留基名 + 白名单字符。**必须**——
           真实现有 `_sanitize_filename`，而路径穿越（`../../`）正是
           这类接口最经典的洞。
        ⚠️ 大小上限与真实现同量级（真实现 `MAX_FILE_BYTES`）。

        Returns:
            `{"uploaded": "<相对路径>", "size": int}`。
        Raises:
            `ValueError` —— 扩展名不符/文件名为空/过大（**消息要能直接给用户看**）。
        """
        meta = self.plugin_config_file_meta(plugin_id, config_key)
        if meta is None:
            raise ValueError("该配置项不存在，或者它不是 file 类型（不能存文件）")
        root = self._plugin_file_dir(plugin_id, config_key)
        assert root is not None  # meta 非 None ⇒ 插件一定存在

        safe = Path(str(filename or "").replace("\\", "/")).name
        safe = "".join(c for c in safe if c.isalnum() or c in "._- ")
        safe = safe.strip().strip(".")
        if not safe:
            raise ValueError("文件名非法（清洗后为空）")

        exts = meta["file_types"]
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if exts and ext not in exts:
            raise ValueError(
                f"不支持的文件类型：.{ext}（这个配置项只收 {', '.join('.' + e for e in exts)}）"
            )
        if len(data) > _MAX_CONFIG_FILE_BYTES:
            raise ValueError(
                f"文件过大：{len(data)} 字节，上限 {_MAX_CONFIG_FILE_BYTES} 字节"
            )
        if not data:
            raise ValueError("文件是空的")

        root.mkdir(parents=True, exist_ok=True)
        target = root / safe
        target.write_bytes(data)
        rel = target.relative_to(root.parent.parent).as_posix()
        logger.info(
            "插件配置文件已上传",
            extra={"data": {"plugin": plugin_id, "key": config_key,
                            "path": rel, "size": len(data)}},
        )
        return {"uploaded": rel, "size": len(data)}

    def delete_plugin_config_file(
        self, plugin_id: str, config_key: str, rel_path: str
    ) -> dict[str, Any]:
        """删掉一个已上传的文件。

        🔴 **两道校验，缺一不可**：
           ① 路径必须落在**这个配置项自己的目录**里（`files/<key>/`）——
              否则传 `files/其它key/x.png` 就能删别人的文件；
              传 `../../main.py` 就能删插件本体。
              （真实现 `config_service.py:1105` 写死 `startswith("files/")`，
               我们更严：必须匹配**该项的**目录。）
           ② 文件必须存在 —— 不存在要**报错**，不能静默成功
              （"删了但没删掉"是最难查的一类）。
        """
        root = self._plugin_file_dir(plugin_id, config_key)
        if root is None:
            raise ValueError(f"没有这个插件：{plugin_id}")

        norm = str(rel_path or "").replace("\\", "/").lstrip("/")
        if not norm:
            raise ValueError("缺少 path 参数")
        parts = [p for p in norm.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise ValueError(f"非法路径：{rel_path}")
        target = root.parent.parent / norm
        # 🔴 **这一道校验同时挡住两类越界，所以它是唯一的那个**：
        #    ① 删**同插件别的配置项**的文件（`files/other_key/x.png`）；
        #    ② 删**插件本体**（`files/../../main.py`，不过上面已先挡一道）。
        #
        # ⚠️ 我最初还写了一版"字符串前缀必须等于 `files/<key>/`"的检查 ——
        #    它和这一道**在语义上完全重叠**（凡是前缀不对的，`relative_to` 也必拒），
        #    注入验证时"C3 抓不住"就暴露了这点：把前缀检查改成 `if False`，
        #    判据**照样全绿**，因为 `relative_to` 独立地挡住了同一个输入。
        #    **两条同一职责的校验 = 谁也证明不了谁有用**（本项目「同一概念不要
        #    多条路径」的另一面）。⇒ 删掉重叠的那条，只留这一道，
        #    于是判据能真正证明**这一道**在工作。
        #
        # ⚠️ 用 `resolve()` 而不是字符串比较：能顺带挡住软链之类的绕过。
        #    用 `root` 而不是 `root.parent.parent` 做基准 —— 后者会让
        #    `files/other/x.png` 的 **字符串** 前缀恰好"看起来对"。
        try:
            target.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValueError(
                f"路径不属于这个配置项（应在 {root.name}/ 下）：{rel_path}"
            ) from None

        if not target.is_file():
            raise ValueError(f"文件不存在：{rel_path}")
        target.unlink()
        logger.info(
            "插件配置文件已删除",
            extra={"data": {"plugin": plugin_id, "key": config_key, "path": norm}},
        )
        return {"deleted": norm}
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


# ---------------------------------------------------------------------------
# 会话人设 → **委托给插件自己写**（2026-10-31，仿 AstrBot 的单一数据源）
# ---------------------------------------------------------------------------

#: 陪伴插件的账号/会话接口前缀（它自己登记的路由，见
#: `astrbot_host/account_session_api.py:19` 的 `PREFIX`）。
_COMPANION_PREFIX = "astrbot_plugin_private_companion/accounts"


class _InternalRequest:
    """给 `call_api` 用的**最小请求桩**（内部调用没有真实 HTTP 请求）。

    ⚠️ 只需要 `method` / `path` / `headers` / `cookies` / `body()`：
       插件那几个 handler 读的就是这几项（`PluginRequest` 其余字段给默认值即可）。
    ⚠️ **不带 query**：插件的 `_admin()` 会 `if request.query: raise`，
       造一个带 query 的桩会被它当场拒掉（`account_config_invalid`）。
    """

    def __init__(self, path: str, payload: dict[str, Any] | None = None) -> None:
        self.method = "POST"
        self.path = path
        self.headers: dict[str, str] = {"content-type": "application/json"}
        self.cookies: dict[str, str] = {}
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    async def body(self) -> bytes:
        return self._raw


async def set_session_persona_via_plugin(
    host: Any, *, umo: str, persona_id: str
) -> dict[str, Any]:
    """把「某个 umo 的会话人设」**交给陪伴插件自己写**。

    ## 为什么不直接改它的存储

    陪伴插件把会话人设存在 `sp` 的
    `(scope="umo", scope_id=<umo>, key="session_service_config")` 里
    （`astrbot_host/native.py:174`），字段名 `persona_id`。
    我第一版是**直接往 `plugin_conversations` 写** `persona_id` —— 那是错的：

      * 那是**插件的私有表**，字段语义（`persona_id` vs `persona_override`）
        由插件定义，写错了**不报错、只是不生效**；
      * 插件还有一层 `session_service_config` 优先级更高，
        直接改表根本压不过它。

    ✅ 正确做法：调**插件自己的接口** `POST .../accounts/session`
       （`account_session_api.py:98` 的 `save_session`）。它内部会：
         ① 校验 persona 是否在**已加载的人设**里；
         ② 若该会话受管（managed），还要求该 persona 已在 Bot 绑定里
            （否则 `account_session_persona_unmapped`）；
         ③ 写 `session_service_config["persona_id"]` 并刷新它的内存缓存。
       ⇒ 语义、校验、缓存失效全由插件负责，**我们只传意图**。
       这也正是 AstrBot 的做法（面板写、插件读，同一套接口）。

    Args:
        host: `PluginHost`。
        umo: 会话的 unified_msg_origin（插件按它认会话）。
        persona_id: 人设**名字**（AstrBot 规范就是 name，不是 UUID）。
            传空串 = 解绑（插件会 `pop("persona_id")`）。

    Returns:
        插件原样返回的 dict（`{"status": "ok", "data": {"saved": True, ...}}`）。

    Raises:
        LookupError: 插件没登记该路由（宿主没起 / 插件版本不对）。
        RuntimeError: 插件返回了 `status != ok`（**带上它的 code**，
            绝不静默当成功 —— 那会让用户以为改好了、其实没改）。
    """
    if host is None or getattr(host, "context", None) is None:
        raise RuntimeError("插件宿主尚未启动，无法把会话人设交给陪伴插件")

    # 先按 umo 找到这个会话的 `session_key`（**不自己拼**：那是插件的内部格式，
    # 它随时可能改；`GET /accounts` 会把它算好给我们）。
    listed = await host.call_api(
        _COMPANION_PREFIX,
        request_obj=_InternalRequest(f"/{_COMPANION_PREFIX}"),
        method="GET",
        username="owner",
    )
    data = listed.get("data") if isinstance(listed, dict) else None
    discovered = (data or {}).get("discovered") or {}
    candidates: list[dict[str, Any]] = []
    for group in discovered.values():
        if isinstance(group, list):
            candidates.extend(row for row in group if isinstance(row, dict))
        elif isinstance(group, dict):
            candidates.extend(row for row in group.values() if isinstance(row, dict))
    target = next(
        (row for row in candidates
         if str(row.get("unified_msg_origin") or "") == str(umo)
         and row.get("session_key")),
        None,
    )
    if target is None:
        raise RuntimeError(
            f"陪伴插件里找不到 umo={umo!r} 对应的会话（它还没见过这条会话？"
            f"先在 QQ 里说一句话，或到陪伴面板的「账号与会话」里确认）"
        )

    saved = await host.call_api(
        f"{_COMPANION_PREFIX}/session",
        request_obj=_InternalRequest(
            f"/{_COMPANION_PREFIX}/session",
            {"session_key": target["session_key"], "persona_id": str(persona_id or "")},
        ),
        method="POST",
        username="owner",
    )
    if isinstance(saved, dict) and str(saved.get("status")) != "ok":
        # 🔴 插件的具体原因**原样透出**（`account_session_persona_unmapped` 之类
        #    都指向一个明确的下一步动作），不要笼统说"保存失败"。
        err = saved.get("data") if isinstance(saved.get("data"), dict) else {}
        raise RuntimeError(
            f"陪伴插件拒绝了这次人设修改：{err.get('code') or saved.get('status')}"
            f"（{err.get('message') or err.get('reason') or ''}）"
        )
    return saved
