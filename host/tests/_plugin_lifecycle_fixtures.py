# -*- coding: utf-8 -*-
"""生命周期三个测试文件**共用**的合成插件与夹具。

## 为什么抽成单独一份

三个文件（detach / reload / uninstall）要守的是**同一个核心**（拆解），
如果各自复制一套合成插件，那么"这个插件注册了哪些东西"就会有三份定义 ——
改了其中一份，另外两份的判据会**继续绿**（它们守的是旧形状），
而"三份都绿"会被读成"三种能力都对"。本项目已踩过
「同一概念两条写入路径」，这里从源头避免。

⚠️ 本文件**不以 `test_` 开头**（pytest 不会把它当测试模块收集）。
⚠️ 全部用**临时目录 + 自造插件** —— 不依赖仓库 `plugins/` 下那 6 个真插件
   （它们在 CI / 别的机器上可能不存在，那样判据会变成"跳过" = 假通过）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sys

import pytest

logger = logging.getLogger(__name__)

HOST_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = HOST_DIR.parent
for _entry in (str(HOST_DIR), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


# ---------------------------------------------------------------------------
# 合成插件
# ---------------------------------------------------------------------------

#: 本套判据造出来的**全部**合成插件目录名 —— **收尾清理的唯一来源**。
#:
#: 🔴 为什么必须是常量而不是在 `teardown_host()` 里再写一遍字面量
#:    （2026-10-31 实测踩到，代价是"反序运行就红"）：
#:    收尾按前缀清注册表，而前缀清单是**手写**的 `("lc_probe", "lc_bystander")`
#:    —— 漏了 `lc_sub_probe`（reload 文件里那条子模块判据造的插件）。
#:    于是它每次加载都往 `star_map` / `llm_tools` 里留一条，
#:    而"没有残留"的判据只在**它之前**跑过别人的用例时才会红：
#:        正常顺序（detach 先跑）→ 绿
#:        反序（uninstall/reload 先跑）→ 红：`star_map 里还残留：['lc_sub_probe.main']`
#:    ⇒ **单跑绿、换序红**，正是「判据不许依赖跨测试漂移的进程级状态」。
#:    现在加插件只改这一处，收尾自动覆盖。
SYNTHETIC_PLUGINS: tuple[str, ...] = ("lc_probe", "lc_bystander", "lc_sub_probe")

#: 「五样都注册」的插件：2 条指令 + 1 个 LLM 工具 + 1 条 Web API + 1 个后台任务。
#:
#: 🔴 为什么必须**五样都注册**：拆解要摘五处（handlers / llm_tools /
#:    web_apis / tasks / star_map），**任何一处漏摘都不报错**，
#:    只测一处等于只守一处。
#:
#: ⚠️ 后台任务用**真 Task**（不是裸协程）：宿主目前不 await 登记的裸协程
#:    （真实现 `core_lifecycle.py:322` 会统一 await，我们还没接）。
#:    裸协程会让每个用例收尾刷 `RuntimeWarning: coroutine 'sleep' was never awaited`，
#:    把真失败淹掉。裸协程形态由 `test_detach_closes_a_bare_coroutine_task` 专守。
PROBE_PLUGIN = '''
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
import asyncio


@register("probe", "probe-author", "判据用合成插件", "0.0.1")
class ProbePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        context.register_web_api(
            "lc_probe/probe/ping", self.ping_api, ["GET"], "判据用路由"
        )
        context.register_task(asyncio.ensure_future(asyncio.sleep(0)), "判据用后台任务")

    @filter.command("lc_alpha")
    async def alpha(self, event: AstrMessageEvent):
        """第一条指令"""

    @filter.command("lc_beta")
    async def beta(self, event: AstrMessageEvent):
        """第二条指令"""

    @filter.llm_tool(name="lc_recall")
    async def recall(self, event: AstrMessageEvent, query: str):
        """判据用工具
        Args:
            query(string): 查询词
        """
        return "ok"

    async def ping_api(self):
        return {"ok": True}

    async def initialize(self):
        return None

    async def terminate(self):
        return None
'''

#: 第二个插件 —— 守「拆 A 不能动到 B」。
#:
#: 🔴 这条是整套判据里**最重要的**：它守的风险是"删错对象"。
#:    本项目真实事故（按裸字符串前缀删 handler）会在这里现形。
BYSTANDER_PLUGIN = '''
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent


@register("bystander", "probe-author", "旁观者插件", "0.0.1")
class BystanderPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        context.register_web_api(
            "lc_bystander/page/info", self.info_api, ["GET"], "旁观者路由"
        )

    @filter.command("bystander_cmd")
    async def cmd(self, event: AstrMessageEvent):
        """旁观者指令"""

    @filter.llm_tool(name="lc_bystander_tool")
    async def tool(self, event: AstrMessageEvent, text: str):
        """旁观者工具
        Args:
            text(string): 文本
        """
        return "ok"

    async def info_api(self):
        return {"ok": True}

    async def initialize(self):
        return None

    async def terminate(self):
        return None
'''

#: 名字是 `lc_probe` 的**前缀邻居** —— 专门守"按裸前缀删会误伤"。
#:
#: 🔴 现场（2026-10-31 实测）：`star_handlers_registry.remove_plugin("lc_probe")`
#:    的判据是 `handler_module_path.startswith("lc_probe")`，**没有结尾的点**，
#:    所以它同时命中 `lc_probe_extra`。拆解照常返回成功，而邻居的 handler
#:    全没了。这类 bug 只有"造一个前缀邻居"才测得出来。
PREFIX_NEIGHBOR_PLUGIN = '''
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent


@register("neighbor", "probe-author", "前缀邻居插件", "0.0.1")
class NeighborPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)

    @filter.command("lc_neighbor_cmd")
    async def cmd(self, event: AstrMessageEvent):
        """邻居指令"""

    async def initialize(self):
        return None

    async def terminate(self):
        return None
'''


def write_plugin(
    root: Path,
    dir_name: str,
    body: str,
    *,
    version: str = "0.0.1",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """在 `root` 下造一个最小可加载的插件目录。"""
    plugin = root / dir_name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "main.py").write_text(body, encoding="utf-8")
    (plugin / "metadata.yaml").write_text(
        f"name: {dir_name}\nversion: {version}\ndesc: 判据用\n", encoding="utf-8"
    )
    for rel, text in (extra_files or {}).items():
        target = plugin / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return plugin


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """插件**源码**根（加载器从这里 `discover()`）。"""
    root = tmp_path / "plugins"
    root.mkdir()
    return root


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """插件**数据**根（= `data/plugins`）。

    ⚠️ **卸载是直接删**（契约 §3，情绪 2026-10-31 定：不做回收站）。
       判据要断言的正是"目录不存在"，以及"没有留下任何暂存目录"。
    """
    root = tmp_path / "data" / "plugins"
    root.mkdir(parents=True)
    return root


async def build_host(source_root: Path, data_root: Path, *, mirror_root: Path | None = None):
    """起一个**真宿主**（不 mock —— 判据要看到注册表与磁盘的实物）。

    ⚠️ 默认**不镜像**：判据要验"源码目录被移走"，镜像会把它藏进副本里
       （那正是 `resolve_source_dir` 要防的那个错）。
    """
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.host_app import PluginHost

    set_data_root(data_root)
    host = PluginHost(
        plugin_dirs=[source_root],
        mirror_root=mirror_root,
        auto_initialize=True,
    )
    await host.start(ports=HostPorts(config={}), install_shadow=True)
    return host


async def teardown_host(host) -> None:
    """停宿主**并把它的登记物从进程级注册表里清干净**。

    🔴 为什么不能只 `await host.stop()`（2026-10-31 实测踩到）：
       `stop()` 只调各插件的 `terminate()`，**不动注册表**；而
       `star_handlers_registry` / `llm_tools` / `star_map` 都是**进程级单例**
       ⇒ 本轮造出来的合成插件会活到 pytest 退出，之后任何按前缀扫描注册表的
       用例都会看见它。

       现场：本文件的 `lc_probe_extra` 泄漏进
       `test_plugin_lifecycle.py::test_detach_removes_every_registration_of_that_plugin`
       （它按 `startswith("probe_plugin")` 数自己的 handler），
       报 "拆解后仍残留 handler：probe_plugin_extra.main_cmd" ——
       **单跑绿、全量红**。这就是「判据不许依赖跨测试漂移的进程级状态」那条
      通则的现场。

       ⇒ 两道修，缺一不可：
         ① 合成插件用独有前缀 `lc_*`（不再与别的文件撞名）；
         ② 收尾**显式清注册表**（本函数）。只做 ① 的话，下一个按宽前缀
            扫描的文件照样会被污染，只是这次没撞上而已。
    """
    if host is not None:
        try:
            await host.stop()
        except Exception:  # noqa: BLE001 — 收尾失败不该改掉用例结论
            logger.warning("测试收尾：host.stop() 失败", exc_info=True)

    from astrbot_host.compat.core.star.base import star_map, star_registry
    from astrbot_host.registry import llm_tools, star_handlers_registry

    # ⚠️ **工具必须按 `handler_module_path` 归属清，不能按工具名前缀清**
    #    （2026-10-31 实测踩到）：工具名是插件自己起的（合成插件里叫 `lc_recall`），
    #    与插件目录名 `lc_probe` **毫无关系**。按工具名前缀清 ⇒ `lc_recall`
    #    留在表里（名字不以 `lc_probe` 开头），而它绑的 handler 已经被删
    #    ⇒ 表里躺着一个**指向不存在模块的工具**。
    #    第一版写的就是 `n.startswith(prefix)`，被本文件那条
    #    `test_teardown_really_clears_process_level_registries` 当场抓住。
    prefixes = SYNTHETIC_PLUGINS
    for prefix in prefixes:
        # `remove_plugin` 是**裸前缀**匹配 ⇒ 顺带清掉 `lc_probe_extra`
        #（收尾阶段不需要保留任何 `lc_*` 东西，正是想要的）。
        star_handlers_registry.remove_plugin(prefix)
    for spec in list(llm_tools.func_list):
        owner = getattr(spec, "handler_module_path", None) or ""
        if any(owner == p or owner.startswith(f"{p}.") for p in prefixes):
            llm_tools.remove_func(spec.name)
    for module_path in [p for p in star_map if any(p.startswith(x) for x in prefixes)]:
        metadata = star_map.pop(module_path, None)
        if metadata is not None and metadata in star_registry:
            star_registry.remove(metadata)


@pytest.fixture
async def two_plugins(source_root, data_root):
    """`lc_probe` + `lc_bystander` 都加载好，产出 `(host, probe, bystander)`。"""
    probe = write_plugin(source_root, "lc_probe", PROBE_PLUGIN)
    bystander = write_plugin(source_root, "lc_bystander", BYSTANDER_PLUGIN)
    host = await build_host(source_root, data_root)
    try:
        assert set(host.loader.records) == {"lc_probe", "lc_bystander"}, (
            host.report.summary()
        )
        yield host, probe, bystander
    finally:
        await teardown_host(host)


@pytest.fixture
async def three_plugins(source_root, data_root):
    """再加一个**前缀邻居** `lc_probe_extra`，用来守"按裸前缀删会误伤"。"""
    probe = write_plugin(source_root, "lc_probe", PROBE_PLUGIN)
    neighbor = write_plugin(source_root, "lc_probe_extra", PREFIX_NEIGHBOR_PLUGIN)
    bystander = write_plugin(source_root, "lc_bystander", BYSTANDER_PLUGIN)
    host = await build_host(source_root, data_root)
    try:
        assert set(host.loader.records) == {
            "lc_probe",
            "lc_probe_extra",
            "lc_bystander",
        }, host.report.summary()
        yield host, probe, neighbor, bystander
    finally:
        await teardown_host(host)


# ---------------------------------------------------------------------------
# 注册表快照（**集合**，不是计数）
# ---------------------------------------------------------------------------


def registry_snapshot() -> dict[str, set[str]]:
    """注册表的**逐条**快照。

    🔴 判据必须用集合比较：`count 相等 ≠ 组成正确`（本项目通则），
       而且拆解错时最常见的形态恰恰是"总数没变、归属错了"。
    """
    from astrbot_host.registry import llm_tools, star_handlers_registry

    handlers = {h.handler_full_name for h in star_handlers_registry.all_handlers()}
    tools = {spec.name for spec in llm_tools.func_list}
    return {"handlers": handlers, "tools": tools}


def web_api_routes(ctx) -> list[str]:
    return [api[0] for api in ctx.registered_web_apis]


def web_api_owners(ctx) -> list[str]:
    """归属表逐条快照 —— 与 `registered_web_apis` **按下标平行**。"""
    return list(getattr(ctx, "_web_api_owners", []))


def owned_handlers_of(plugin_id: str) -> set[str]:
    """某个插件当前的 handler 全名集合（**逐条**，用于差集比对）。"""
    from astrbot_host.registry import star_handlers_registry

    return {
        h.handler_full_name
        for h in star_handlers_registry.all_handlers()
        if h.handler_module_path == plugin_id
        or h.handler_module_path.startswith(f"{plugin_id}.")
    }


def freeze_task(coro) -> asyncio.Future:
    """造一个"永远不完成"的真 Task（用于观察它有没有被 cancel）。"""
    return asyncio.ensure_future(coro)
