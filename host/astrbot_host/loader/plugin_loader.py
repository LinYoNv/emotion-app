# -*- coding: utf-8 -*-
"""插件加载器 —— 把 AstrBot 插件装进我们的宿主。

⭐⭐ **关键设计：模块命名空间必须照真 AstrBot**（实测 `star_manager.py:783-790`）：

       真实现用 `data.plugins.<插件目录名>`（内置插件用 `astrbot.builtin_stars.<名>`）
       作为插件的**顶层包名**。

   🔴 **为什么这决定了能不能跑**：陪伴是 **735 个 .py 的多文件插件**，
      内部大量使用**相对 import**（`from .helpers import ...`）与
      **跨模块绝对 import**（`from astrbot_plugin_private_companion.main import ...`）。
      * 相对 import 要求顶层包名存在；
      * 绝对 import 要求**插件目录名本身**能作为包被 import。
      → 所以我们的做法是：把插件根目录加入 `sys.path`，
        让 `astrbot_plugin_private_companion` 这类**目录名直接成为包名**
        （这正是真 AstrBot 追加 `data/plugins` 到 sys.path 的效果）。

⚠️ 与真实现的差异（如实记录）：
   真实现把插件挂在 `data.plugins.<dir>` 下并做模块名重写；
   我们**直接用目录名**（更简单，且与插件内部的绝对 import 写法天然兼容）。
   代价：同名插件会冲突 —— 但自用场景只有一个插件集，可接受；
   真出现冲突会**显式报错**（见 `_check_name_conflict`）。
"""
from __future__ import annotations

import functools
import importlib
import importlib.util
import logging
import sys
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot_host.compat.core.star.base import (
    Star,
    StarMetadata,
    star_map,
    star_registry,
)
from astrbot_host.compat.core.star.context import Context, HostPorts
from astrbot_host.registry import star_handlers_registry

logger = logging.getLogger("astrbot.host.loader")

#: 插件元数据文件名（照真实现 `PLUGIN_METADATA_FILENAMES`）
METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")

#: 插件入口模块名（真实现是 `<插件目录>/main.py`，`discover()` 也按它判据）
ENTRY_MODULE_NAME = "main"

#: 插件页面根目录名（照真实现 `plugin_page_service.py:25`）
PLUGIN_PAGE_DIR_NAME = "pages"

#: 插件页面入口文件名（照真实现 `plugin_page_service.py:26`）——
#: **必须有它才算一个页面**（真实现 `discover_plugin_pages` 就是这么判的）
PLUGIN_PAGE_ENTRY_FILE = "index.html"

#: 真 AstrBot 的插件模块路径前缀。真实现 `star_manager.py:1115`：:
#:
#:     path = "data.plugins." if not reserved else "astrbot.builtin_stars."
#:     path += root_dir_name + "." + module_str
#:
#: 我们**不从这儿加载**（我们的顶层包名就是插件目录名，见本模块头部的差异说明），
#: 但停用名单里允许出现这个前缀 —— 这样把生产 AstrBot 的
#: `shared_preferences.inactivated_plugins` 原样抄过来就能用。
REAL_ASTRBOT_MODULE_PREFIX = "data.plugins."


def entry_module_path(dir_name: str) -> str:
    """插件的**入口模块路径** —— 我们宿主的"插件身份"的唯一定义。

    它同时是：`star_map` 的键、handler 的 `handler_module_path`、
    `@filter.llm_tool` 工具的 `handler_module_path`、以及停用名单里的元素。
    四处必须是**同一个字符串**，所以只在这里拼一次。
    """
    return f"{dir_name}.{ENTRY_MODULE_NAME}"


def normalize_module_path(raw: Any) -> str:
    """把停用名单里的一个元素归一成 `entry_module_path()` 的形状。

    🔴 **这是"名单元素算不算命中"的唯一判据** —— 加载器与面板都调它，
       别处不许再写第二套 `in ...` 比较（本项目"同一概念多条写入路径"踩过 3 次）。

    接受三种写法（都能用，这是**输入归一**，不是三套判据）：

    | 写法 | 来源 |
    |---|---|
    | `astrbot_plugin_x.main` | 我们自己的规范写法（面板写出来的就是它） |
    | `astrbot_plugin_x` | 只写目录名 → 补上入口模块 |
    | `data.plugins.astrbot_plugin_x.main` | **真 AstrBot 的写法**，可直接抄 |

    Returns:
        归一后的模块路径；空串 = 这一项没有意义（调用方据此报"对不上任何插件"）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith(REAL_ASTRBOT_MODULE_PREFIX):
        text = text[len(REAL_ASTRBOT_MODULE_PREFIX):]
    if not text:
        return ""
    if "." not in text:
        # 只有一段 = 插件目录名 → 补入口模块（`discover()` 也要求 `main.py` 存在）
        return entry_module_path(text)
    return text


@dataclass
class PluginRecord:
    """一个已加载插件的记录（诊断用）。"""

    name: str
    dir_name: str
    path: Path
    module: Any = None
    star_cls: type | None = None
    instance: Any = None
    version: str = ""
    author: str = ""
    desc: str = ""
    #: 加载过程中的错误（**不静默吞**）
    error: str = ""
    #: 完整堆栈 —— 插件加载失败时用户需要知道是哪一行
    traceback: str = ""
    #: 注册到的 handler 数
    handler_count: int = 0
    #: `@filter.llm_tool` 工具里**绑定了本插件实例**的个数（2026-09-22 补）。
    #: ⚠️ 它和 `handler_count` **不是一回事**：那个数事件 handler，
    #:    这个数"工具 handler 有没有被绑上 self"。为 0 而插件有工具 = 执行必错。
    llm_tool_bind_count: int = 0
    #: 插件登记的 Web API / 任务数
    web_api_count: int = 0
    task_count: int = 0
    #: 插件声明的**拓展页**（`metadata.yaml` 的 `pages:`，照真实现）。
    #: 每项形如 `{"name": "companion-panel", "title": "陪伴面板"}`。
    #: ⚠️ 光有元数据还不够 —— 页面 HTML 在 `<插件>/pages/<name>/index.html`，
    #:    要宿主**提供页面路由**才打得开（这正是"插件没有配置入口"的根因）。
    pages: list[dict[str, str]] = field(default_factory=list)
    #: 🔴 **该插件的配置对象**（真 `AstrBotConfig`：带 `schema` 与 `save_config()`）。
    #:
    #: 为什么必须**留住它**（情绪 2026-10-09 要做「插件配置页」）：
    #:   原来 `_build_plugin_config()` 的返回值**只传给了插件构造器就丢了** ——
    #:   想给面板读配置，只能重新构造或重新读盘。
    #:   真实现把这件事放在 `StarMetadata.config`（`core/star/star.py:54`），
    #:   而我们的 `StarMetadata` 之前**没有这个字段** —— 同一缺口的两半。
    #:
    #: ⚠️ 它同时也是 `save_config()` 的**唯一入口**：面板保存时必须写回**这个**
    #:    对象（而不是另起一份 JSON 写盘），否则又成了"同一概念两条写入路径"。
    config: Any = None
    #: 🔴 是否因**停用名单**被跳过（真实现 `inactivated_plugins`）。
    #: 停用与"加载失败"是**两条不同的路径**：停用的插件不 import、不实例化、
    #: 不 `initialize()`，并且**不计入失败**（真实现 `star_manager.py:1240`
    #: 只把 `star_cls` 置 None，再打一行 "Plugin %s is disabled."）。
    inactivated: bool = False

    @property
    def module_path(self) -> str:
        """本插件在宿主里的模块身份（= `star_map` 的键 / handler 的归属）。"""
        return entry_module_path(self.dir_name)

    @property
    def state(self) -> str:
        """插件当前状态的**唯一判据**（报告、面板、日志都用它）。

        四种取值，互斥且穷尽：
          * `"inactivated"` —— 在停用名单里，我们**没碰过它**（不是失败）
          * `"failed"`      —— 真出错了（import/实例化失败）
          * `"loaded"`      —— 加载成功
          * `"not_loaded"`  —— 既没加载也没报错（不该出现，出现即为异常状态）
        """
        if self.inactivated:
            return "inactivated"
        if self.error:
            return "failed"
        if self.instance is not None:
            return "loaded"
        return "not_loaded"

    @property
    def ok(self) -> bool:
        return self.state == "loaded"


@dataclass
class LoadReport:
    """一次加载的结果汇总。"""

    records: list[PluginRecord] = field(default_factory=list)
    #: 🔴 停用名单里**对不上任何插件目录**的元素（拼错 / 插件已删）。
    #: 必须能看见 —— 静默忽略会让用户以为"已经停用了"，其实一点作用都没有。
    unmatched_inactivated: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> list[PluginRecord]:
        return [r for r in self.records if r.state == "loaded"]

    @property
    def failed(self) -> list[PluginRecord]:
        """⚠️ **不含**被停用的（那是两条不同的路径，见 `PluginRecord.inactivated`）。"""
        return [r for r in self.records if r.state == "failed"]

    @property
    def inactivated(self) -> list[PluginRecord]:
        return [r for r in self.records if r.state == "inactivated"]

    def summary(self) -> str:
        lines = [
            f"插件加载：成功 {len(self.loaded)} / 失败 {len(self.failed)}"
            + (f" / 已停用 {len(self.inactivated)}" if self.inactivated else ""),
        ]
        for r in self.records:
            if r.ok:
                lines.append(
                    f"  ✅ {r.name} v{r.version}  "
                    f"handlers={r.handler_count} web_api={r.web_api_count} "
                    f"tasks={r.task_count} llm_tools_bound={r.llm_tool_bind_count}"
                )
            elif r.inactivated:
                lines.append(f"  ⏸ {r.name}（{r.module_path}）→ 已停用，跳过加载")
            else:
                lines.append(f"  ❌ {r.name}（{r.dir_name}）→ {r.error}")
        if self.unmatched_inactivated:
            lines.append(
                f"  ⚠️ 停用名单里有 {len(self.unmatched_inactivated)} 项对不上任何插件"
                f"（拼写错误？）：{', '.join(self.unmatched_inactivated)}"
            )
        return "\n".join(lines)


class PluginLoader:
    """从目录加载 AstrBot 插件。

    用法::

        loader = PluginLoader(plugin_dirs=["/opt/astrbot/data/plugins"])
        report = await loader.load_all(ports=HostPorts(config={}))
        print(report.summary())
    """

    def __init__(
        self,
        plugin_dirs: list[str | Path],
        *,
        mirror_root: str | Path | None = None,
        inactivated: list[str] | None = None,
    ) -> None:
        """
        Args:
            plugin_dirs: 插件源目录（**只读输入**）。
            mirror_root: 🔴 **强烈建议传**。给定后，本加载器会先把 `plugin_dirs`
                **镜像**到该目录，再从镜像加载。

                为什么存在（真实事故）：插件会在 `__init__`/`initialize` 里
                **回写自己的源码目录**（实测 `comfyui_pro` 把 workflow 列表写回
                `_conf_schema.json`）。而 `plugin_dirs` 很可能是**别人正在跑的
                AstrBot 生产目录** —— 于是我们的运行会覆盖对方的配置，
                且**完全静默**。镜像之后源码目录只读，怎么写都落在副本里。

                ⚠️ 不传也能用（保持骨架的简单性），但加载期间若检测到源目录
                被改写，会打**响亮 warning**（见 `write_guard`）。
            inactivated: 🔴 **停用名单**（照真实现 `star_manager.py:1087`
                `inactivated_plugins = await sp.global_get("inactivated_plugins", [])`）。

                元素是**模块路径**，三种写法都收（见 `normalize_module_path`）。
                命中 = **不 import、不实例化、不调用 `initialize()`**，
                且**不计入失败** —— 停用与"加载失败"是两条不同的路径。

                ⚠️ 名单本身由**装配层**（`emotion_app/web/plugin_host.py`）从配置读出来
                再传进来：`host/` 不许反向依赖 `emotion_app`（单向依赖铁律）。
        """
        self.requested_dirs = [Path(d) for d in plugin_dirs]
        self.plugin_dirs = list(self.requested_dirs)
        self.mirror_report: Any = None

        if mirror_root is not None:
            from astrbot_host.source_mirror import mirror_plugin_sources

            self.mirror_report = mirror_plugin_sources(
                self.requested_dirs, mirror_root
            )
            # 从镜像加载（**不再碰源目录**）
            self.plugin_dirs = [Path(mirror_root).resolve()]

        #: 归一后的停用名单（集合，只用于成员判断）。
        #: ⚠️ 归一化只在这里做一次 —— 判据单一，见 `is_inactivated`。
        self.inactivated: set[str] = {
            p for p in (normalize_module_path(x) for x in (inactivated or ())) if p
        }

        self.records: dict[str, PluginRecord] = {}
        self._context: Context | None = None

    # ---- 停用判据（**只此一份**）----

    def is_inactivated(self, module_path: str) -> bool:
        """该模块路径是否在停用名单里。

        🔴 加载器、加载报告、面板**都调这里**；
           别处不许再写 `x in inactivated_plugins` 之类的第二套比较。
        """
        return module_path in self.inactivated

    # ---- 扫描 ----

    def discover(self) -> list[Path]:
        """找出所有插件目录（含 `main.py` 或 metadata）。

        ⚠️ 判据用"有没有 `main.py`"——真 AstrBot 要求插件入口是 `main.py`。
        """
        found: list[Path] = []
        for root in self.plugin_dirs:
            if not root.is_dir():
                logger.warning("插件目录不存在，已跳过：%s", root)
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                if (child / "main.py").is_file():
                    found.append(child)
        return found

    # ---- 加载 ----

    async def load_all(
        self,
        *,
        ports: HostPorts | None = None,
        only: list[str] | None = None,
        fail_fast: bool = False,
    ) -> LoadReport:
        """加载全部（或指定）插件。

        Args:
            ports: 宿主能力（模型/会话/投递）。**缺哪个就少哪个能力**，
                但不阻止插件加载 —— 照作者「能力不对等要如实 degraded」判据。
            only: 只加载这些目录名（调试用）。
            fail_fast: 首个失败就抛异常（排查用）。
        """
        self._context = Context(ports=ports or HostPorts())
        report = LoadReport()

        for path in self.discover():
            if only and path.name not in only:
                continue
            rec = await self.load_one(path)
            self.records[path.name] = rec
            report.records.append(rec)
            if not rec.ok and not rec.inactivated and fail_fast:
                raise RuntimeError(f"插件 {path.name} 加载失败：{rec.error}")

        # 🔴 名单里"对不上任何插件"的元素要**响亮**报出来（拼错 / 插件已删）。
        #    静默忽略会让用户以为"已经停用了"，其实一点作用都没有 ——
        #    正是本项目最忌的那类静默失败。
        #    ⚠️ `only=` 是调试用的部分加载，那时 records 本来就不全 → 不算 unmatched。
        if not only:
            discovered = {r.module_path for r in report.records}
            report.unmatched_inactivated = sorted(self.inactivated - discovered)
            if report.unmatched_inactivated:
                logger.warning(
                    "停用名单里有 %d 项对不上任何插件目录（拼写错误？）：%s；"
                    "已发现的插件模块路径：%s",
                    len(report.unmatched_inactivated),
                    ", ".join(report.unmatched_inactivated),
                    ", ".join(sorted(discovered)),
                )

        return report

    async def load_one(self, path: Path) -> PluginRecord:
        """加载单个插件目录。**失败不抛异常**（记录进 `PluginRecord.error`）。"""
        rec = PluginRecord(name=path.name, dir_name=path.name, path=path)
        try:
            self._ensure_on_sys_path(path.parent)

            # ---- 只读元数据（不执行任何插件代码）----
            # ⚠️ 顺序上必须先读它：停用的插件也要能报出自己的**显示名/版本**给面板，
            #    否则面板只能显示目录名（用户认不出是哪个插件）。
            meta = self._read_metadata(path)
            rec.name = meta.get("name") or path.name
            rec.version = str(meta.get("version") or "")
            rec.author = str(meta.get("author") or "")
            rec.desc = str(meta.get("desc") or "")
            # 🔴 页面发现必须**扫 `pages/` 目录**，不能只看 metadata 声明（2026-10-08 修）
            rec.pages = discover_plugin_pages(path, meta.get("pages"))

            # 🔴🔴 **停用名单命中 → 到此为止**（照真实现 `star_manager.py:1087/1218/1251`）。
            #
            #    "不加载"在这里的含义（逐条对应真机那四类噪音）：
            #      * 不 `import` → 不会再有 `插件加载失败：meme_manager →
            #        No module named 'requests'`；
            #      * 不实例化、不 `initialize()` → 不会再有
            #        `initialize() 失败：gemini_content_keeper` /
            #        `initialize() 超时：media_parser`；
            #      * handler 根本没注册 → 不会再有
            #        `LLM 钩子执行失败：prompt_tools.main_process_llm_request`。
            #
            #    ⚠️ 位置必须在 `_check_name_conflict` **之前**：那个函数会抛异常
            #       （"模块名冲突"）—— 抛了就变成 `failed`，
            #       而停用的插件**不该出现在失败清单里**（这是判据之一）。
            if self.is_inactivated(rec.module_path):
                rec.inactivated = True
                logger.info(
                    "插件 %s 已停用（名单命中 %s）→ 跳过加载：不 import、不实例化、"
                    "不调用 initialize()，也**不计入失败**",
                    rec.name,
                    rec.module_path,
                )
                return rec

            self._check_name_conflict(path.name)

            # 🔴 **归属标注**：插件在自己的 `__init__` 里调
            #    `context.register_web_api(...)`（实测 `page_api.py:1456`），
            #    所以必须在**实例化前**告诉 Context "现在装的是谁" ——
            #    否则登记物无法归属到插件，加载报告只能报累计值。
            if self._context is not None:
                self._context.set_current_plugin(path.name)

            # ⚠️ **不用"前后差值"统计 handler**（实测踩到）：
            #    插件模块已 import 过时，`importlib` 不会重新执行装饰器
            #    → 差值为 0，看起来"没注册任何 handler"（假象）。
            #    正确做法：**按模块路径统计该插件自己的 handler**。
            module = self._import_plugin(path)
            rec.module = module

            star_cls = self._find_star_class(module)
            if star_cls is None:
                rec.error = (
                    "模块里没有 `Star` 子类 —— 插件必须定义一个继承 "
                    "`astrbot.api.star.Star` 的类（真 AstrBot 同此要求）。"
                )
                return rec
            rec.star_cls = star_cls

            # 把 metadata 灌进 star_map（Star.__init__ 靠它取插件名）
            self._fill_star_metadata(star_cls, rec, meta)

            # 🔴 照真实现（star_manager.py:1208-1213）：**实例化前**注入类属性 ——
            #    插件的 `__init__` 会读 `self.name` / `self.author` / `self.plugin_id`。
            #    漏掉这一步会让插件在构造期拿到 None 而出错。
            self._inject_class_attrs(star_cls, rec, meta)

            # 🔴 配置必须传**真实对象**（照真实现传 `plugin_config`）。
            #    实测踩到：传 None 会让插件 `__init__` 里 `config.get(...)` 直接
            #    AttributeError —— 而且是在 4600 行构造逻辑深处才炸。
            plugin_config = self._build_plugin_config(path, meta)

            # 🔴 **留住配置对象**（情绪 2026-10-09「插件配置页」的前提）。
            #    以前这一行的返回值只往下传一层就没人引用了 ——
            #    面板要读/写插件配置时**无从下手**（只能重新构造或重新读盘，
            #    那必然与插件手里那份漂移）。
            #    真实现是 `StarMetadata.config`（`core/star/star.py:54`）。
            #    ⚠️ 两处都要存：`rec.config` 给宿主读，`StarMetadata.config`
            #       是为了与真实现同形（插件/第三方按那个字段找配置）。
            rec.config = plugin_config
            self._stash_star_config(star_cls, plugin_config)

            try:
                rec.instance = star_cls(context=self._context, config=plugin_config)
            except TypeError as exc:
                # 照真实现（star_manager.py:1221-1229）：只对 TypeError 做无 config 回退
                logger.warning(
                    "插件 %s 实例化 TypeError，尝试无 config 回退：%s", rec.name, exc
                )
                rec.instance = star_cls(context=self._context)

            # 🔴 实例化**之后**才有实例可填 —— 这一步决定插件之间能不能互相找到对方。
            self._fill_star_runtime(star_cls, rec, module)

            # 🔴🔴 **把 handler 绑定到插件实例**（照真实现 `star_manager.py:1260-1271`）。
            #
            # 为什么必须做（P0-2 派发的拦路虎）：
            #   插件方法签名是 `async def cmd(self, event)`。注册时拿到的是
            #   **未绑定的裸函数**（类体里取 `__name__` 得到的是 function），
            #   所以派发时若直接 `await handler.handler(event)`，
            #   **`event` 会被当成 `self`**，真正的 event 参数缺参 → 每个指令都 TypeError。
            #   实测：绑定前注册表里 **203 个 handler 全是裸函数**（partial 计数 0）。
            #
            # 照真实现的三处细节：
            #   ① `metadata.star_cls` 是**实例**（`star_cls_type` 才是类）→ 绑实例不是类；
            #   ② 先用 `handler.handler.func` 还原裸函数 —— **幂等**，
            #      这样同进程重复加载不会叠加多层 partial；
            #   ③ 用 `functools.partial`，不是 `types.MethodType`
            #      （真实现就是这么写的，且 partial 对异步函数同样可用）。
            self._bind_handlers_to_instance(path.name, rec.instance)
            # 🔴 **同一批"绑定生命周期"也要覆盖 `@filter.llm_tool` 的工具**
            #    （真实现 `star_manager.py:1273-1299` 就是这么做的，注释原话
            #    "Apply the same idempotent binding lifecycle to LLM tools"）。
            #    漏了这一步 → 工具 handler 是裸函数（缺 `self`）→ 执行必错，
            #    且错误会被上游宽 `except` 吞成"工具执行失败"（**静默**）。
            rec.llm_tool_bind_count = self._bind_llm_tools_to_instance(
                path.name, rec.instance
            )

            rec.handler_count = self._count_plugin_handlers(path.name, module)
            if self._context is not None:
                # 🔴 **必须按插件归属取，不能取全局长度**（实测踩到）：
                #    `len(context.registered_web_apis)` 是**累计值** ——
                #    报告里 prompt_tools 只有 21 个 handler 却显示 266 个 API，
                #    那其实是前面所有插件累加的结果（虚高且无法定位）。
                rec.web_api_count = len(
                    self._context.registered_web_apis_of(path.name)
                )
                rec.task_count = len(self._context.registered_tasks_of(path.name))

            logger.info(
                "插件已加载：%s v%s（handlers=%d）",
                rec.name, rec.version, rec.handler_count,
            )
        except Exception as exc:  # noqa: BLE001
            # 🔴 关键：**保留完整堆栈**（不只消息）—— 插件加载失败时
            #    用户需要知道是哪一行，光有 "ImportError" 没法排查。
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.traceback = traceback.format_exc()
            logger.error("插件加载失败：%s\n%s", rec.name, rec.traceback)
        return rec

    async def initialize_all(self, report: LoadReport | None = None) -> list[str]:
        """调用所有已加载插件的 `initialize()`。

        Returns:
            失败的插件名列表（空 = 全部成功）。
        """
        failed: list[str] = []
        targets = report.loaded if report else [r for r in self.records.values() if r.ok]
        for rec in targets:
            try:
                await rec.instance.initialize()
                logger.info("插件已初始化：%s", rec.name)
            except Exception as exc:  # noqa: BLE001
                rec.error = f"initialize() 失败：{type(exc).__name__}: {exc}"
                failed.append(rec.name)
                logger.error("插件初始化失败：%s\n%s", rec.name, traceback.format_exc())
        return failed

    async def terminate_all(self) -> None:
        """调用所有插件的 `terminate()`（卸载/退出时）。"""
        for rec in self.records.values():
            if not rec.ok:
                continue
            try:
                await rec.instance.terminate()
            except Exception:  # noqa: BLE001
                logger.warning("插件 terminate 失败：%s", rec.name, exc_info=True)

    # ---- 内部 ----

    @staticmethod
    def _ensure_on_sys_path(directory: Path) -> None:
        """把插件父目录加入 sys.path —— 让插件目录名成为可 import 的包。

        🔴 这一步是**多文件插件能跑的前提**（陪伴内部大量
           `from astrbot_plugin_private_companion.xxx import ...`）。
        """
        d = str(directory.resolve())
        if d not in sys.path:
            sys.path.insert(0, d)
            logger.debug("已加入 sys.path：%s", d)

    @staticmethod
    def _check_name_conflict(dir_name: str) -> None:
        """同名模块冲突要**显式报错**（不静默用错版本）。

        起因：实测踩过同类坑 —— 同名旧副本遮住新接口，
        排查成本很高（真 AstrBot 也有这个问题）。
        """
        existing = sys.modules.get(dir_name)
        if existing is None:
            return
        existing_file = getattr(existing, "__file__", "") or ""
        if existing_file and dir_name not in existing_file:
            raise RuntimeError(
                f"模块名冲突：`{dir_name}` 已指向 {existing_file}。"
                "同名插件/包会导致 import 到错误的实现，请先清理。"
            )

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any]:
        """读 `metadata.yaml`（照真实现 —— 缺了不致命，只少元信息）。"""
        for name in METADATA_FILENAMES:
            f = path / name
            if not f.is_file():
                continue
            try:
                import yaml  # 真实现依赖 pyyaml；我们可选
            except ImportError:
                # 没有 yaml 时用极简解析兜住 name/version/author/desc
                logger.warning(
                    "未安装 pyyaml，%s 用极简解析（只取顶层标量字段）", f.name
                )
                return _lite_yaml(f.read_text(encoding="utf-8"))
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("解析 %s 失败：%s", f, exc)
                return {}
        return {}

    @staticmethod
    def _import_plugin(path: Path) -> Any:
        """import 插件包与它的 `main` 子模块。

        🔴 **必须显式 import `<包>.main`**（实测踩到）：
           真 AstrBot 的插件入口约定是 `<插件目录>/main.py`，
           而 `import <包名>` 只会执行 `__init__.py`（通常为空），
           **插件类根本不会被定义** → 表现为"模块里没有 Star 子类"。
           这个错误信息会把人引向"插件写错了"，而真实原因是"没导入对模块"。

        🔴🔴 **`main.py` 只能执行一次**（2026-09-20 实测修掉的 BUG-4）：
           插件的 `__init__.py` 里普遍写着 `from .main import XxxPlugin`
           （实测 `memory_companion` 就是）→ **import 包的时候 `main.py` 已经被执行了**。
           而这段代码原来接着写：
               if main_name in sys.modules: importlib.reload(...)
           → 首次加载也会走 `reload` → **`main.py` 跑第二遍**。

           后果**不是**"加载失败"，而是**静默重复注册**：
           类体跑两遍 → `@filter.command` / `@permission_type` 各挂两遍
           → handler 的 `event_filters` 里出现**完全相同的重复项**
           （实测 `astrbot_plugin_memory_companion` **23 个** handler 中招）。
           这会让同一个 handler 在派发时被重复激活，
           而"重复"在注册表里看不出来（`handler_full_name` 只有一个）。

        ✅ 正确语义：**首次 import 不 reload；只有它本来就在 `sys.modules` 里
           （= 同进程重复 `load_all`）才 reload**，此时 reload 才有意义（保持新鲜）。

        Returns:
            插件**包**对象（`main` 子模块可作为属性访问）。
        """
        name = path.name
        #: 包**本来就在** sys.modules 里 = 这是同进程的重复加载（才需要 reload）
        was_loaded = name in sys.modules
        if was_loaded:
            pkg = importlib.reload(sys.modules[name])
        else:
            pkg = importlib.import_module(name)

        # ⚠️ 入口模块：真 AstrBot 用 `main.py`；同时把 main 挂成包属性，
        #    这样 `_find_star_class` 能在包命名空间里看到插件类。
        main_name = entry_module_path(name)
        if main_name in sys.modules:
            if was_loaded:
                # 重复加载 → reload 保持新鲜（这才是 reload 的正当用途）
                main_mod = importlib.reload(sys.modules[main_name])
            else:
                # 🔴 首次加载：`main.py` 刚刚已被 `__init__.py` 的
                #    `from .main import ...` 执行过了 —— **直接用，不许再跑一遍**
                main_mod = sys.modules[main_name]
        else:
            # 包不 import main（少见）→ 这时才需要我们主动 import
            main_mod = importlib.import_module(main_name)
        setattr(pkg, "main", main_mod)
        # 把 main 里的公开名字提升到包命名空间（便于 _find_star_class 扫描）
        for attr in dir(main_mod):
            if attr.startswith("_"):
                continue
            if not hasattr(pkg, attr):
                setattr(pkg, attr, getattr(main_mod, attr))

        # 🔴 注册 `data.plugins.<dir>` 别名（W5，2026-10-08 修）
        #    ⚠️ 本方法是 `@staticmethod` —— 用**类名**调用，别写 `self.`（实测 NameError）
        PluginLoader._register_real_astrbot_alias(name, pkg, main_mod)
        return pkg

    @staticmethod
    def _register_real_astrbot_alias(
        dir_name: str, pkg: Any, main_mod: Any
    ) -> None:
        """把插件注册成**真 AstrBot 的模块路径** `data.plugins.<dir>[.main]`。

        ## 为什么必须做（W5 的真根因）
        插件之间会**互相调用**，写法是硬编码真 AstrBot 的模块路径，例如：
            `importlib.import_module("data.plugins.astrbot_plugin_private_companion.main")`
        （实测本项目里这类写法 **40 处**，散在 5 个插件中。）

        而我们的顶层包名是**目录名本身**（`astrbot_plugin_private_companion`，
        见本模块头部说明）⇒ 上面那种 import **100% ModuleNotFoundError**。
        ⚠️ 多数调用点写了两级候选或遍历 `sys.modules` 兜底（那些能活），
           但**只写 `data.plugins.…` 一处**的地方就是**纯失败**。

        ## 为什么用别名而不是改成真 AstrBot 的加载方式
        改加载方式要动 `sys.path` 策略与全部插件内部绝对 import，风险大；
        而**注册一个同对象的别名**是纯增量：插件拿到的还是**同一个模块对象**，
        `isinstance` / 全局状态 / `id()` 全都一致（实测三种 import 写法都指向同一对象）。

        ## 为什么必须逐级 `setdefault`
        🔴 `data/` 是**隐式命名空间包**（`__file__` 为 None），
        **不会自动在 `sys.modules` 里建中间层条目**。
        实测踩到：只写 `sys.modules[alias] = main_mod` 后，
        `setattr(sys.modules["data.plugins.astrbot_plugin_demo"], ...)` → **KeyError**。
        ⇒ 必须逐级确保父包存在。

        ⚠️ 只注册**别名**，不改 `pkg.__name__`（改了会让 `star_map` / handler
           的 `handler_module_path` 与新名字脱节 —— 那是"同一概念多条写入路径"）。
        """
        parts = [*REAL_ASTRBOT_MODULE_PREFIX.rstrip(".").split("."), dir_name]
        try:
            # 1) 逐级确保父包在 sys.modules 里（命名空间包不会自动建）
            for i in range(1, len(parts) + 1):
                sys.modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
            # 2) 逐级挂属性：data.plugins.<dir> → 真 pkg
            sys.modules[".".join(parts)] = pkg  # 关键：用**真对象**覆盖占位
            for i in range(1, len(parts)):
                parent = sys.modules[".".join(parts[:i])]
                setattr(parent, parts[i], sys.modules[".".join(parts[: i + 1])])
            # 3) 入口模块别名：`data.plugins.<dir>.main` → 真 main_mod
            main_alias = f"{'.'.join(parts)}.{ENTRY_MODULE_NAME}"
            sys.modules[main_alias] = main_mod
            # 把 main 挂成别名包的属性（`from data.plugins.<dir> import main` 也能用）
            setattr(pkg, ENTRY_MODULE_NAME, main_mod)
            logger.debug(
                "已注册真 AstrBot 别名：%s → %s（跨插件调用可命中）",
                ".".join(parts),
                getattr(pkg, "__name__", "?"),
            )
        except Exception as exc:  # noqa: BLE001
            # 别名是**锦上添花**，失败不该让插件加载失败 —— 但**不许静默**。
            logger.warning(
                "注册真 AstrBot 模块别名失败（该插件的跨插件调用仍会失败）：%s: %s",
                ".".join(parts),
                exc,
            )

    @staticmethod
    def _find_star_class(module: Any) -> type | None:
        """在模块里找 `Star` 子类。

        ⚠️ 两处兼容（照真实现的发现逻辑）：
          ① 先看模块级 `star_map` 登记（`Star.__init_subclass__` 的副作用）；
          ② 再扫模块属性 —— 因为插件可能在**子模块**里定义插件类
             （如 `main.py` 只做 re-export）。
        """
        md = star_map.get(module.__name__)
        if md is not None and md.star_cls_type is not None:
            return md.star_cls_type

        candidates: list[type] = []
        for attr in dir(module):
            obj = getattr(module, attr, None)
            if isinstance(obj, type) and issubclass(obj, Star) and obj is not Star:
                candidates.append(obj)
        if not candidates:
            return None
        # 多个时取"最像主插件"的：名字含 Plugin 优先，否则取第一个
        for c in candidates:
            if "Plugin" in c.__name__ or "Companion" in c.__name__:
                return c
        return candidates[0]

    @staticmethod
    def _bind_handlers_to_instance(dir_name: str, instance: Any) -> int:
        """把该插件的 handler 绑定到插件实例（照真实现 `star_manager.py:1260-1271`）。

        真实现原代码：
        ```python
        related_handlers = star_handlers_registry.get_handlers_by_module_name(metadata.module_path)
        for handler in related_handlers:
            raw_handler = (handler.handler.func
                           if isinstance(handler.handler, functools.partial)
                           else handler.handler)
            handler.handler = raw_handler                       # ← 先还原（幂等）
            if not plugin_disabled and metadata.star_cls is not None:
                handler.handler = functools.partial(raw_handler, metadata.star_cls)
        ```

        ⚠️ 两个必须照抄的点：
          ① **先还原成裸函数**再绑 —— 否则同进程重复 `load_all` 会叠成
             `partial(partial(f, inst), inst)`，调用时参数错位（比不绑更难查）；
          ② 绑的是**实例**（`star_cls`），不是类。

        Returns:
            实际绑定的 handler 数（诊断用）。
        """
        if instance is None:
            # 插件未激活/实例化失败 → 照真实现**不绑定**（把裸函数留在表里，
            # 让派发时如实失败，而不是伪造一个能跑的样子）。
            return 0
        n = 0
        for handler in star_handlers_registry.get_handlers_by_module_name(
            entry_module_path(dir_name)
        ):
            raw = (
                handler.handler.func
                if isinstance(handler.handler, functools.partial)
                else handler.handler
            )
            handler.handler = functools.partial(raw, instance)
            n += 1
        return n

    @staticmethod
    def _bind_llm_tools_to_instance(dir_name: str, instance: Any) -> int:
        """把 `@filter.llm_tool` 注册的工具 handler 绑到插件实例（**2026-09-22 补**）。

        ⭐⭐ **为什么必须有**（照真实现 `star_manager.py:1273-1299` 的那一段
        "Apply the same idempotent binding lifecycle to LLM tools"）：

        上面 `_bind_handlers_to_instance` 只遍历了 `star_handlers_registry` 里的
        **事件 handler**，而 `@filter.llm_tool` 注册的插件方法走的是**另一张表**
        （`llm_tools`）。两者**不是同一批对象**。

        实测后果（2026-09-22，本轮真 bug）：`llm_tools.get_func("pc_generate_photo").handler`
        是**裸函数**，签名 `(self, event, ...)` —— 缺第一个 `self`。
        → 任何工具执行器调它都必然 `TypeError`。
        ⚠️ 而这条错误会被上游的宽 `except` 吞成"工具执行失败"，
           **读我们自己的源码完全看不出来**（本项目最贵的那类 bug）。

        照抄真实现的四个细节：
          ① 按 `handler.__module__`（或 `handler_module_path`）**匹配归属** ——
             不能无脑给所有工具绑实例（那是别的插件的工具）；
          ② **先还原**成裸函数再绑（幂等：重复 load 不叠 partial）；
          ③ 绑定成功后写回 `ft.handler_module_path`（下次重载靠它认归属）；
          ④ `instance is None` → 照真实现**不绑**（留裸函数，如实失败）。

        Returns:
            实际绑定的工具数（诊断用）。
        """
        from astrbot_host.registry import llm_tools

        module_path = entry_module_path(dir_name)
        n = 0
        for spec in llm_tools.func_list:
            handler = getattr(spec, "handler", None)
            if not handler:
                continue
            # 🔴 **必须先拆掉 partial 再判归属**（2026-09-22 实测踩到）：
            #    `functools.partial` 的 `__module__` 恒为 **'functools'**
            #    （partial 自己的类所在模块），**不是**被包函数的模块。
            #    所以"重载时 handler 已经是 partial"这一步会判不出归属
            #    → 整个绑定被跳过（`bind_count=0`）→ 工具继续指着**上一个插件实例**。
            #
            #    实测证据（同一进程连续 load 两次真陪伴插件）：
            #      第 1 次：bind_count=20，handler_module_path='...main'
            #      第 2 次：bind_count=0， handler_module_path=None
            #                handler.__module__='functools'
            #                handler.func.__module__='astrbot_plugin_private_companion.main'  ← 真归属在这
            #    ⚠️ 这**不是测试假象**：宿主支持重复 `load_all`（重载插件），
            #       重载后工具会用**旧实例**执行 —— 静默用错对象。
            raw = handler.func if isinstance(handler, functools.partial) else handler
            owner = (
                getattr(spec, "handler_module_path", None)
                or getattr(raw, "__module__", None)
            )
            if owner != module_path:
                continue
            spec.handler_module_path = module_path
            spec.handler = raw
            if instance is not None:
                spec.handler = functools.partial(raw, instance)
            n += 1
        return n

    @staticmethod
    def _count_plugin_handlers(dir_name: str, module: Any) -> int:
        """统计**该插件注册的** handler 数（按模块路径过滤）。

        ⚠️ 为什么不数"全局总数"或"前后差值"：
           * 全局总数含别的插件，不是这个插件的成绩；
           * 前后差值在"模块已 import 过"时是 0（装饰器不会重跑）——
             实测就踩到这个：同一个插件第二次加载报 `handler_count=0`，
             看起来像"插件没注册 handler"，其实是统计方法错了。
        """
        prefix = f"{dir_name}."
        return sum(
            1 for h in star_handlers_registry.all_handlers()
            if h.handler_module_path == dir_name
            or h.handler_module_path.startswith(prefix)
        )

    @staticmethod
    def _inject_class_attrs(
        star_cls: type, rec: PluginRecord, meta: dict[str, Any]
    ) -> None:
        """实例化**前**注入类属性（照真实现 `star_manager.py:1208-1213`）。

        真实现从 `plugin_id`（格式 `author/name`）拆出 author 与 name；
        我们直接用 metadata 里的值，**冲突时以 metadata 为准**。
        """
        name = rec.name or rec.dir_name
        author = rec.author or "unknown"
        try:
            setattr(star_cls, "name", name)
            setattr(star_cls, "author", author)
            # 真实现的 plugin_id 是 `author/name` 形式
            setattr(star_cls, "plugin_id", f"{author}/{name}")
        except (AttributeError, TypeError) as exc:
            # 类属性只读等异常 → 留痕但不阻止加载（插件可能用 __slots__）
            logger.debug("注入类属性失败（%s）：%s", name, exc)

    @staticmethod
    def _build_plugin_config(path: Path, meta: dict[str, Any]) -> Any:
        """构建插件配置（照真实现的 `plugin_config`）。

        🔴 **真实现的契约**（`star_manager.py:1159-1165`）::

            AstrBotConfig(
                config_path=<数据目录>/config/<插件目录名>_config.json,
                schema=<插件 _conf_schema.json 的内容>,
            )

        要点（我原来三点都写错了，逐条对照修）：

        ① **必须传 `schema=`，不是自己拍默认值。**
           真实现把整个 schema 交给 `AstrBotConfig`，由它
           `_config_schema_to_default_config()` 递归展开（含嵌套分组、枚举选项）。
           我原来只取**顶层** `default` 且碰到 `type == "object"` 时手工拼一层
           —— 嵌套两层以上就漏，且与真实现的默认值算法不一致。

        ② **必须传 `config_path=`，且指向【我们自己的】数据目录。**
           不传会落到 `ASTRBOT_CONFIG_PATH` 默认值（= AstrBot 生产数据目录里的
           `cmd_config.json`）→ 插件的 `save_config()` 会**去写别人的全局配置**。
           真实现是 `<数据目录>/config/<插件名>_config.json`。

        ③ 🔴 **`config=` 这个 kwarg 根本不存在**（签名是
           `config_path` / `default_config` / `schema`）。
           我原来写的是 `AstrBotConfig(config=cfg)` → **抛 TypeError**
           → 被 `except` 吞掉 → **静默退回裸 dict**。
           后果：插件拿到的 `config` **没有 `save_config()`**，
           于是面板上任何"保存配置"都报
           「配置保存失败，REQ-041 运行值已恢复，但旧配置重新持久化失败」
           —— 而且我们这边**一行日志都没有**（本项目最贵的 bug 类型）。

        ⚠️ **失败时不再静默退回裸 dict**：真出问题要打 ERROR 并如实说明
           "插件将拿到只读配置"，让"保存不了"这件事在日志里查得到。
        """
        schema: dict[str, Any] = {}
        schema_file = path / "_conf_schema.json"
        if schema_file.is_file():
            try:
                import json

                loaded = json.loads(schema_file.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    schema = loaded
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "解析 %s 失败，插件将拿到空配置：%s", schema_file, exc
                )
        else:
            logger.info(
                "插件 %s 没有 _conf_schema.json，使用空配置（插件应自行处理默认值）",
                path.name,
            )

        # 配置文件落在**我们的**数据目录（不是插件源码目录，也不是 AstrBot 的）
        try:
            from astrbot_host.compat.core.star.star_tools import get_data_root

            config_dir = Path(get_data_root()) / "config"
        except Exception:  # noqa: BLE001
            config_dir = path.parent / "_host_config"
        config_path = config_dir / f"{path.name}_config.json"

        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(
                "无法创建插件配置目录 %s：%s —— 插件的『保存配置』将不可用",
                config_dir,
                exc,
            )

        try:
            from astrbot_host.compat.core.config.astrbot_config import AstrBotConfig

            return AstrBotConfig(config_path=str(config_path), schema=schema)
        except Exception as exc:  # noqa: BLE001
            # 🔴 **不静默**：退回裸 dict 意味着插件**存不了配置**，必须留痕。
            logger.error(
                "构造 AstrBotConfig 失败（%s）：%s: %s —— "
                "插件将拿到**只读**配置，面板上的『保存配置』会失败",
                path.name,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return {}

    @staticmethod
    def _stash_star_config(star_cls: type, config: Any) -> None:
        """把配置对象挂到 `StarMetadata.config`（真实现 `core/star/star.py:54`）。

        🔴 **为什么不能只存 `rec.config`**：
           真实现把配置放在**元数据**上（`star_map[module].config`），
           而第三方/插件是按那个字段找配置的。
           只存我们自己的 `PluginRecord` = 形状与真实现不一致，
           "看起来能用"但任何按真实现写的代码都取不到。

        ⚠️ 与 `_fill_star_metadata` 分开：那个负责"名字/作者/版本"这类**静态**
           信息（在实例化**前**就要填好），配置是加载链路后段才有的。
           合成一个函数会让调用时机含糊（早调了 `config` 还是 None）。
        """
        from astrbot_host.compat.core.star.base import StarMetadata, star_map

        md = star_map.get(star_cls.__module__)
        if md is None:
            md = StarMetadata(star_cls_type=star_cls, module_path=star_cls.__module__)
            star_map[star_cls.__module__] = md
        md.config = config

    @staticmethod
    def _fill_star_metadata(
        star_cls: type, rec: PluginRecord, meta: dict[str, Any]
    ) -> None:
        """把 metadata 写进 star_map（`Star.__init__` 靠它取插件名）。"""
        md: StarMetadata | None = star_map.get(star_cls.__module__)
        if md is None:
            md = StarMetadata(star_cls_type=star_cls, module_path=star_cls.__module__)
            star_map[star_cls.__module__] = md
            star_registry.append(md)
        md.name = rec.name
        md.author = rec.author
        md.desc = rec.desc
        md.version = rec.version
        md.root_dir_name = rec.dir_name
        md.activated = True

    @staticmethod
    def _fill_star_runtime(star_cls: type, rec: PluginRecord, module: Any) -> None:
        """把**运行时对象**写进 star_map：`star_cls`（实例）+ `module`（模块）。

        ⭐⭐ **为什么必须有**（照真实现 `star_manager.py:1222-1231` + `:1243`）::

            metadata.star_cls = metadata.star_cls_type(context=..., config=...)
            ...
            metadata.module = module

        这两个字段是插件**跨插件互调**的唯一入口 —— 插件加载完就靠它们找
        "谁是我的同伴"。四种读法里有三种直接依赖本函数（第四种 `sys.modules[...]`
        与本函数无关）：

        | 读法 | 实测出处 |
        |---|---|
        | `get_all_stars()` → `md.star_cls.memory_companion` | 陪伴 `memory_companion_adapter.py:538` |
        | `get_all_stars()` → `md.module.get_memory_companion_bridge()` | 陪伴 `memory_companion_adapter.py:892` |
        | `get_registered_star(name)` → `md.star_cls` | 记忆插件 `companion_page_bridge.py:786` |

        🔴 **实测后果（对照实验，唯一变量 = 补不补这两个字段）**：

            原样           → 陪伴 `_memory_companion_bridge()` = None（reason=bridge_missing）
            补上两字段后   → 找到桥，`capability_state=available`、`contract_version=3`

        ⚠️ 这个 bug 的**症状形态**正是本项目最贵的那类：插件把它 catch 成
           一行 info 日志（`[MemoryCompanion] capability probe degraded`），
           **读我们自己的源码永远看不出来** —— 所以判据必须落在
           `_memory_companion_bridge() is not None` 这种**端到端**上
           （见 `host/tests/test_star_metadata.py`）。

        🔴 **实例化失败时不许填假对象**：照真实现语义，`rec.instance is None`
        （例如插件 import 失败 / 被禁用）时 `star_cls` **保持 `None`** ——
        `star_manager.py:1240` 的 `else: metadata.star_cls = None` 就是这个意思，
        而且 `star.py:21` 的类文档原话是「当 activated 为 False 时，star_cls 可能
        为 None，**请不要在插件未激活时调用 star_cls 的方法**」。
        插件侧对此有明确语义（`_memory_companion_presence()` 的
        `"loaded": getattr(metadata, "star_cls", None) is not None`）——
        填个能跑的样子反而会**伪造"已加载"** 。

        ⚠️ **不要**把 `_bind_handlers_to_instance` / `_bind_llm_tools_to_instance`
           也改到这里：它们读的是 handler registry 里的 `StarHandlerMetadata`，
           与 `StarMetadata` **不是同一种对象**（见它们各自的 docstring）。
        """
        md: StarMetadata | None = star_map.get(star_cls.__module__)
        if md is None:
            # 理论上到不了（`_fill_star_metadata` 已在前面建过条目）；
            # 真到了就如实补一条，不静默跳过。
            logger.warning(
                "star_map 里没有 %s 的条目，运行时字段将新建（可能加载顺序被改动过）",
                star_cls.__module__,
            )
            md = StarMetadata(star_cls_type=star_cls, module_path=star_cls.__module__)
            star_map[star_cls.__module__] = md
            star_registry.append(md)
        # ⚠️ `rec.instance` 为 None 时**就是 None**（照真实现 `star_manager.py:1240`
        #    的 `else: metadata.star_cls = None`）—— 不伪造一个"能跑的样子"。
        md.star_cls = rec.instance
        md.module = module


def discover_plugin_pages(plugin_root: Path, declared: Any) -> list[dict[str, str]]:
    """发现插件的页面 —— **扫 `pages/` 目录为准**，并与 metadata 声明**合并**。

    ## 🔴 为什么必须扫目录（2026-10-08 修的真缺口）
    情绪报：「记忆插件**存在吗**？我看插件面板里面没有呢，**应该是有扩展页的**」——
    查实：`astrbot_plugin_memory_companion`（显示名「我会牢牢记住你」v2.1.4）
    **已加载且运行中**，`pages/记忆面板/index.html`（3095 字节）**真实存在**，
    但它的 `metadata.yaml` **根本没有 `pages:` 段**。

    而**真 AstrBot 的页面发现根本不看 metadata** —— 读
    `dashboard/services/plugin_page_service.py:508 discover_plugin_pages()`：
    它直接 `pages_root = <插件根>/pages`，然后**遍历子目录**，
    每个含 `index.html` 的目录就是一个页面，**`title` 取目录名**。
    ⇒ 我们原来只读 `metadata.pages` 是**错的**，会漏掉所有"有页面但没声明"的插件。

    实测本项目的缺口（扫出来的 vs 我们原来列的）：

    | 插件 | metadata 声明 | `pages/` 实际 | 原来漏掉 |
    |---|---|---|---|
    | `memory_companion` | ❌ 无 | 记忆面板 | **记忆面板** |
    | `nai_image` | ❌ 无 | test-panel | test-panel |
    | `meme_manager` | ❌ 无 | app | app |
    | `private_companion` | ✅ companion-panel | companion-panel · **陪伴面板** | **陪伴面板** |

    ⚠️ 连"声明了"的 `private_companion` 也**漏了 1 个** —— 所以这不是"补几个特例"，
       而是**发现方式本身错了**。（对照：真 AstrBot 的 `star_manager.py:1187` 确实也读
       `metadata_yaml.pages`，但那是**另一条路**；**页面发现（discover）走的是目录扫描**。）

    ## 合并规则
    * **目录扫描是权威**（照真实现）—— 有 `index.html` 才算数；
    * metadata 声明里**目录不存在**的条目**仍然保留**（它在真 AstrBot 里会被
      `get_plugin_page` 判 404，但**保留能让用户看到"声明了却没页面"这个事实**，
      比静默丢弃强 —— 本项目最忌静默）。
    * 同名以**目录扫描**为准（title 用目录名，与真实现一致）。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    # 1) 目录扫描（权威，照真实现 plugin_page_service.py:508-535）
    pages_root = plugin_root / PLUGIN_PAGE_DIR_NAME
    if pages_root.is_dir():
        try:
            dirs = sorted(
                (d for d in pages_root.iterdir() if d.is_dir()),
                key=lambda d: d.name.lower(),
            )
        except OSError as exc:
            logger.warning("扫描插件页目录失败：%s: %s", pages_root, exc)
            dirs = []
        for page_dir in dirs:
            name = page_dir.name.strip()
            # 照真实现 `normalize_plugin_page_name`：只做安全校验（中文名照用），非法就跳过
            if not name or name in {".", ".."} or name.startswith(".") or "/" in name or "\\" in name:
                logger.warning("插件页目录名非法，已跳过：%r（位于 %s）", name, pages_root)
                continue
            # 照真实现：**必须有入口文件**才算一个页面
            if not (page_dir / PLUGIN_PAGE_ENTRY_FILE).is_file():
                logger.warning(
                    "插件页目录缺少 %s，已跳过：%s", PLUGIN_PAGE_ENTRY_FILE, page_dir
                )
                continue
            out.append({"name": name, "title": name})
            seen.add(name)

    # 2) 合并 metadata 声明（补上"声明了但目录不存在"的，便于诊断）
    for item in _normalize_pages(declared):
        if item["name"] in seen:
            continue
        out.append(item)
        seen.add(item["name"])

    return out


def _normalize_pages(raw: Any) -> list[dict[str, str]]:
    """把 `metadata.yaml` 的 `pages:` 归一成 `[{"name","title"}]`。

    照真实现的容错口径：

      * 支持**字符串**写法（`pages: [companion-panel]`）→ `title` 用 name 兜底；
      * 支持 **dict 列表**（最常用）→ 取 `name` 与 `title`；
      * ⚠️ **没有 `name` 的条目直接丢弃**（没名字就定位不到 `pages/<name>/`），
        但要留日志 —— 静默丢配置正是本项目最忌讳的 bug 类型。

    ⚠️ **本函数只是 metadata 那一半**；真正的页面发现是 `discover_plugin_pages()`。
       单独调它**会漏掉没声明但目录存在的页面**（2026-10-08 修的真缺口）。

    🔴 为什么这两个函数必须存在：插件**声明了页面** ≠ 页面**打得开**。
       页面 HTML 在 `<插件>/pages/<name>/index.html`，必须由**宿主提供页面路由**。
       我上一轮只挂了 Web API、没做页面路由，结果情绪看到的就是"插件没有配置入口"。
    """
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
            if name:
                out.append({"name": name, "title": name})
            continue
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if not name:
                logger.warning("插件声明了一个没有 name 的 page，已跳过：%r", item)
                continue
            out.append({"name": name, "title": str(item.get("title") or name)})
            continue
        logger.warning("无法识别的 page 声明，已跳过：%r", item)
    return out


def _lite_yaml(text: str) -> dict[str, Any]:
    """极简 YAML 顶层标量解析（无 pyyaml 时的兜底）。

    ⚠️ 只支持 `key: value` 且 value 无嵌套 —— 够取 name/version/author/desc。
       解析不了的键**直接忽略**（不猜）。
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if not line or line.startswith(("#", " ", "\t", "-")):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip("'\"")
        if v and not v.startswith(("[", "{", "|", ">")):
            out[k.strip()] = v
    return out


__all__ = [
    "ENTRY_MODULE_NAME",
    "LoadReport",
    "PluginLoader",
    "PluginRecord",
    "entry_module_path",
    "normalize_module_path",
]
