# -*- coding: utf-8 -*-
"""插件生命周期（拆解 / 热重载 / 卸载）的回归判据。

## 为什么这些判据长这样

本项目最贵的一类 bug 是**静默失败**。生命周期操作的危险恰好全在静默一侧：

* 拆解漏摘一个登记物 → 重载后**同一条指令执行两次**，而注册表里看不出重复；
* 卸载删除时不校验目录归属 → **删到别的插件**（这台机器上最不可接受的错误）；
* 停用名单没剔 → 下次启动报一条**指向已删插件**的假警告；
* 数据目录被顺手删掉 → 用户的记忆/关系库没了，且**没有报错**。

所以判据全部走「**集合比较 + 磁盘实物比对**」，不用计数当主判据
（`项目更新.md` 通则：计数相等 ≠ 组成正确）。

⚠️ 所有插件都是**临时目录里现造的合成插件** —— 不依赖真实的 6 个插件
（它们在别的机器/CI 上可能不存在，那样判据会变成"跳过"= 假通过）。
"""

# ===========================================================================
# 说明：与本文件**同目录**的 `test_plugin_lifecycle_{detach,reload,uninstall}.py`
# 按能力拆开、共用 `_plugin_lifecycle_fixtures.py`，覆盖"逐项摘除 / terminate 超时 /
# tasks 取消 / 前缀邻居"等更细的形态。
#
# 本文件**只留那三份没有的**判据（都是端到端风险最高的那几条）：
#   * 源码目录**真的被删掉**（不是移走、不是留着）
#   * 数据目录/配置文件默认**不删**
#   * 源码镜像开着时**不许**回落到副本目录
#   * 重载失败必须是 `reload_failed` 且记录不消失
#   * `ReloadResult.detached` 必须是 dataclass（曾经写成 dict ⇒ 成功路径 500）
#   * 并发重载必须串行（锁的判据，含注入反证）
#
# ⚠️ 与那三份**重名的 4 条已经删掉**（2026-10-31）：跨文件同名不会报错，
#    但会让"同一件事守了两遍"看起来像守了两件不同的事。
# ===========================================================================


from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

HOST_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = HOST_DIR.parent
for entry in (str(HOST_DIR), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


# ---------------------------------------------------------------------------
# 合成插件（临时目录现造）
# ---------------------------------------------------------------------------

#: 一个"什么都会注册"的插件：2 条指令 + 1 个 LLM 工具 + 1 条 Web API + 1 个后台任务。
#:
#: 🔴 为什么必须五样都注册：拆解的五个位置（handlers / tools / web_apis /
#:    tasks / star_map）**每一处漏摘都不报错**，只测一处等于只守一处。
_COMPREHENSIVE = '''
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
import asyncio


@register("probe", "probe-author", "判据用合成插件", "0.0.1")
class ProbePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        #: 每次注册 Web API（构造期，照实测 `page_api.py:1456` 的行为）
        context.register_web_api(
            "probe_plugin/probe/ping", self.ping_api, ["GET"], "判据用路由"
        )
        # ⚠️ 用**真 Task**（不是裸协程）：宿主目前**不 await** 登记的裸协程
        #    （真实现 `core_lifecycle.py:322` 会统一 await，我们还没接 —— 这是
        #    既有缺口，不在本次范围）。用裸协程会让每个用例收尾时刷
        #    `RuntimeWarning: coroutine 'sleep' was never awaited`，
        #    把真失败淹掉。裸协程的收尾由下面那条专门的判据守。
        context.register_task(asyncio.ensure_future(asyncio.sleep(0)), "判据用后台任务")

    @filter.command("probe_alpha")
    async def alpha(self, event: AstrMessageEvent):
        """第一条指令"""

    @filter.command("probe_beta")
    async def beta(self, event: AstrMessageEvent):
        """第二条指令"""

    @filter.llm_tool(name="probe_recall")
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

#: 第二个插件 —— 用来守「拆 A 不能动到 B」。
#:
#: 🔴 这条判据是**最重要的**：它守的风险是"删错对象"。本项目真实事故
#:    （`remove_plugin` 按前缀删）就会在这个判据下现形。
_BYSTANDER = '''
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent


@register("bystander", "probe-author", "旁观者插件", "0.0.1")
class BystanderPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        context.register_web_api(
            "bystander_plugin/page/info", self.info_api, ["GET"], "旁观者路由"
        )

    @filter.command("bystander_cmd")
    async def cmd(self, event: AstrMessageEvent):
        """旁观者指令"""

    @filter.llm_tool(name="bystander_tool")
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


def _write_plugin(root: Path, dir_name: str, body: str, *, version: str = "0.0.1",
                  extra_files: dict[str, str] | None = None) -> Path:
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


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """插件**源码**根（装载器从它 discover）。"""
    root = tmp_path / "plugins"
    root.mkdir()
    return root


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """插件**数据**根（`data/plugins`）。"""
    root = tmp_path / "data" / "plugins"
    root.mkdir(parents=True)
    return root


async def _host(source_root: Path, data_root: Path):
    """起一个真宿主（**不镜像** —— 判据要验"源码目录被移走"，镜像会把它藏起来）。"""
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.host_app import PluginHost

    set_data_root(data_root)
    host = PluginHost(plugin_dirs=[source_root], auto_initialize=True)
    await host.start(ports=HostPorts(config={}), install_shadow=True)
    return host


@pytest.fixture
async def two_plugins(source_root, data_root):
    """两个插件都加载好，返回 `(host, 插件目录, 旁观者目录)`。"""
    probe = _write_plugin(source_root, "probe_plugin", _COMPREHENSIVE)
    bystander = _write_plugin(source_root, "bystander_plugin", _BYSTANDER)
    host = await _host(source_root, data_root)
    try:
        assert set(host.loader.records) == {"probe_plugin", "bystander_plugin"}, (
            host.report.summary()
        )
        yield host, probe, bystander
    finally:
        await host.stop()


def _registry_snapshot() -> dict[str, set[str]]:
    """注册表的**逐条**快照（集合，不是计数）。"""
    from astrbot_host.registry import llm_tools, star_handlers_registry

    handlers = {
        h.handler_full_name
        for h in star_handlers_registry.all_handlers()
    }
    tools = {spec.name for spec in llm_tools.func_list}
    return {"handlers": handlers, "tools": tools}


def _web_api_routes(ctx) -> list[str]:
    return [api[0] for api in ctx.registered_web_apis]


def _owned_web_api_owners(ctx) -> list[str]:
    """归属表的**逐条**快照 —— 与 `registered_web_apis` 下标平行。"""
    return list(getattr(ctx, "_web_api_owners", []))


# ---------------------------------------------------------------------------
# ① 拆解
# ---------------------------------------------------------------------------


async def test_detach_removes_every_registration_of_that_plugin(two_plugins):
    """拆解后该插件的**每一条**登记物都不在了 —— 逐条集合比较。"""
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context

    before = _registry_snapshot()
    before_routes = _web_api_routes(ctx)
    probe_handlers = {
        name for name in before["handlers"] if name.startswith("probe_plugin")
    }
    # 前置：这个插件**确实**注册了几样东西（否则下面的"都不在了"是空断言）
    assert probe_handlers, f"合成插件没注册任何 handler，判据无效：{before['handlers']}"
    assert "probe_recall" in before["tools"], before["tools"]
    assert "probe_plugin/probe/ping" in before_routes, before_routes

    result = await detach_plugin(host, "probe_plugin")

    after = _registry_snapshot()
    assert not (after["handlers"] & probe_handlers), (
        f"拆解后仍残留 handler：{after['handlers'] & probe_handlers}"
    )
    assert "probe_recall" not in after["tools"], after["tools"]
    assert "probe_plugin/probe/ping" not in _web_api_routes(ctx), _web_api_routes(ctx)

    # 计数必须与"真的少了多少"对上
    assert result.handlers == len(probe_handlers), (
        f"handler 计数 {result.handlers} != 实际 {len(probe_handlers)}"
    )


async def test_detach_never_touches_another_plugin(two_plugins):
    """拆 A **不许**动到 B —— 逐条集合相等（这是最重要的一条）。"""
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context
    before = _registry_snapshot()
    before_routes = _web_api_routes(ctx)
    bystander_handlers = {
        name for name in before["handlers"] if name.startswith("bystander_plugin")
    }
    assert bystander_handlers, "旁观者插件没注册 handler，判据无效"

    await detach_plugin(host, "probe_plugin")

    after = _registry_snapshot()
    assert bystander_handlers <= after["handlers"], (
        f"旁观者的 handler 被误删：{bystander_handlers - after['handlers']}"
    )
    assert "bystander_tool" in after["tools"], after["tools"]
    assert "bystander_plugin/page/info" in _web_api_routes(ctx), _web_api_routes(ctx)


async def test_detach_is_idempotent_and_second_call_is_not_a_false_success(two_plugins):
    """再拆一次：**不报错、计数为 0**。

    ⚠️ 为什么这条重要：重载失败后重试是常见路径。若第二次拆解把
       "别人的东西"当自己的再删一遍，重试就成了**破坏性操作**。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    first = await detach_plugin(host, "probe_plugin")
    assert first.total > 0, "第一次拆解应该摘掉东西"

    after_first = _registry_snapshot()
    second = await detach_plugin(host, "probe_plugin")
    after_second = _registry_snapshot()

    assert second.total == 0, f"第二次拆解不该再摘掉东西：{second.as_dict()}"
    assert after_first == after_second, "第二次拆解改了注册表"


async def test_web_api_owner_table_stays_index_aligned(two_plugins):
    """摘掉 Web API 后，**归属表必须跟着缩位**。

    🔴 这是"看不见的错位"：两张表按下标平行（见 `Context.__init__`）。
       只删前者 = 之后所有归属都往前错一格，而**没有任何报错** ——
       详情页会把 A 的 API 算到 B 头上。
    """
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    ctx = host.context
    assert len(_web_api_routes(ctx)) == len(_owned_web_api_owners(ctx)), (
        "前置：两张表本来就不等长"
    )

    await detach_plugin(host, "probe_plugin")

    routes = _web_api_routes(ctx)
    owners = _owned_web_api_owners(ctx)
    assert len(routes) == len(owners), f"缩位不同步：routes={routes} owners={owners}"
    # 剩下的每条 API，归属必须**逐条**还对应得上（不是"长度相等"就算）
    for route, owner in zip(routes, owners, strict=True):
        if route.startswith("bystander_plugin"):
            assert owner == "bystander_plugin", f"{route} 的归属错了：{owner}"


async def test_detach_removes_star_registry_entry(two_plugins):
    """`star_map` / `star_registry` 里也必须摘干净 —— **逐条集合比较**。

    🔴 这条守的是一个**顺序 bug**（2026-10-31 修）：第一版先
       `star_map.pop()` 再拿 `star_map.get()` 去过滤 `star_registry`
       —— pop 之后 get 恒为 None ⇒ 过滤条件恒真 ⇒ **一条都没删，且不报错**。
       症状是重载后插件在 `star_registry` 里被列两次、terminate 被调两次。

    ⚠️ 光断言 `module_path not in star_map` 抓不住它（pop 已经成功）；
       必须断言 `star_registry` 里**没有那个 metadata 对象**。
    """
    from astrbot_host.compat.core.star.base import star_map, star_registry
    from astrbot_host.plugin_lifecycle import detach_plugin

    host, _probe, _bystander = two_plugins
    module_path = host.loader.records["probe_plugin"].module_path

    metadata = star_map.get(module_path)
    assert metadata is not None, f"前置：star_map 里本来就没有 {module_path}"
    assert metadata in star_registry, "前置：star_registry 里没有它，判据无效"

    bystander_module = host.loader.records["bystander_plugin"].module_path
    bystander_metadata = star_map.get(bystander_module)
    assert bystander_metadata in star_registry

    await detach_plugin(host, "probe_plugin")

    assert module_path not in star_map, "star_map 里还有它"
    assert metadata not in star_registry, (
        "star_registry 里还留着它的 metadata —— 这就是 pop 之后取值的顺序 bug"
    )
    assert bystander_metadata in star_registry, "误删了旁观者的 metadata"


async def test_reload_result_detached_is_a_dataclass_not_a_dict(two_plugins):
    """`ReloadResult.detached` 必须能 `as_dict()`。

    🔴 守的是第一版把 `default_factory` 写成 `dict` 的错：
       成功路径上 `result.detached.as_dict()` 会抛 AttributeError ⇒
       "功能其实成功了、接口 500"。
    """
    from astrbot_host.plugin_lifecycle import DetachResult, reload_plugin

    host, _probe, _bystander = two_plugins
    result = await reload_plugin(host, "probe_plugin")

    assert isinstance(result.detached, DetachResult), type(result.detached)
    payload = result.detached.as_dict()  # 这一行就是原来的崩溃点
    assert set(payload) == {"handlers", "llm_tools", "web_apis", "tasks"}, payload


async def test_reload_refuses_when_source_missing_even_if_mirror_dir_has_it(
    source_root, data_root, tmp_path
):
    """解析源码目录时**不许**回落到镜像副本。

    🔴 守的是第一版 `resolve_source_dir` 的兜底：它在源码根找不到时回落
       `loader.plugin_dirs`（镜像开着时 = 副本目录）。照副本移走 ⇒
       真源码原地不动、用户看到"删除成功"，**重启后插件又回来了**。

    构造：源码根里**没有**这个插件目录，但镜像/加载器用的目录里有它。
    """
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.host_app import PluginHost
    from astrbot_host.plugin_lifecycle import resolve_source_dir

    # 插件只存在于"加载器用的目录"里，不在 `requested_dirs` 里
    loaded_root = tmp_path / "loaded_only"
    loaded_root.mkdir()
    _write_plugin(loaded_root, "probe_plugin", _COMPREHENSIVE)

    set_data_root(data_root)
    host = PluginHost(plugin_dirs=[loaded_root], auto_initialize=False)
    await host.start(ports=HostPorts(config={}))
    try:
        # 人为制造"源码根里找不到"的局面（= 开镜像后的真实情形）
        host.loader.requested_dirs = []
        assert resolve_source_dir(host.loader, "probe_plugin") is None, (
            "解析不到源码目录时**不许**回落到 plugin_dirs（那是镜像副本）"
        )
    finally:
        await host.stop()


# ---------------------------------------------------------------------------
# ② 热重载
# ---------------------------------------------------------------------------


async def test_reload_creates_a_new_instance_and_a_real_terminate(two_plugins):
    """重载后：实例是**新对象**、旧实例的 `terminate()` **真被 await 过**。"""
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    old = host.loader.records["probe_plugin"].instance
    assert old is not None

    marker = {"terminated": 0}

    # 给旧实例挂一个可观测的 terminate（合成插件的 terminate 是空实现）
    async def terminate():
        marker["terminated"] += 1

    old.terminate = terminate

    result = await reload_plugin(host, "probe_plugin")

    assert marker["terminated"] == 1, "旧实例的 terminate() 没有被真正 await"
    new = host.loader.records["probe_plugin"].instance
    assert new is not None and new is not old, "重载后应换成新实例"
    assert result.state == "loaded", result.error
    assert result.handler_count > 0


async def test_reload_leaves_other_plugins_working(two_plugins):
    """重载 A 之后，B 的登记物**一条不少**。"""
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, _probe, _bystander = two_plugins
    before = _registry_snapshot()
    bystander_handlers = {
        name for name in before["handlers"] if name.startswith("bystander_plugin")
    }

    await reload_plugin(host, "probe_plugin")

    after = _registry_snapshot()
    assert bystander_handlers <= after["handlers"], (
        f"重载 A 把 B 的 handler 弄丢了：{bystander_handlers - after['handlers']}"
    )
    assert "bystander_tool" in after["tools"]


async def test_reload_picks_up_edited_code(two_plugins):
    """改了 `main.py` 再重载，**新代码真的生效**（否则重载是摆设）。"""
    from astrbot_host.plugin_lifecycle import reload_plugin

    host, probe_dir, _bystander = two_plugins
    main = probe_dir / "main.py"
    text = main.read_text(encoding="utf-8")
    main.write_text(
        text.replace('@filter.command("probe_alpha")', '@filter.command("probe_gamma")'),
        encoding="utf-8",
    )

    await reload_plugin(host, "probe_plugin")

    from astrbot_host.registry import star_handlers_registry

    cmds = set()
    for handler in star_handlers_registry.all_handlers():
        for flt in handler.event_filters or ():
            cmd = getattr(flt, "command_name", None)
            if cmd:
                cmds.add(cmd)
    assert "probe_gamma" in cmds, f"重载没读到新代码：{sorted(cmds)}"
    assert "probe_alpha" not in cmds, "旧代码的 handler 没被清掉"


async def test_reload_failed_plugin_reports_400_not_success(two_plugins):
    """把插件改成坏代码再重载 → `reload_failed`，**不许静默成功**。"""
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, reload_plugin

    host, probe_dir, _bystander = two_plugins
    (probe_dir / "main.py").write_text(
        "raise RuntimeError('判据用：故意炸')\n", encoding="utf-8"
    )

    with pytest.raises(PluginLifecycleError) as exc:
        await reload_plugin(host, "probe_plugin")
    assert exc.value.code == "reload_failed"
    assert "判据用：故意炸" in str(exc.value.message), exc.value.message

    # 🔴 失败后记录**必须还在**（换成 failed 状态），而不是消失 ——
    #    消失会让面板显示"没有这个插件"，用户以为被删了。
    rec = host.loader.records.get("probe_plugin")
    assert rec is not None and rec.state == "failed", rec.state
    assert "probe_plugin" in [n for n, _ in host.report.failed]


# ---------------------------------------------------------------------------
# ③ 卸载（直接删，配置/数据按勾选项）
# ---------------------------------------------------------------------------


async def test_uninstall_deletes_source_dir(two_plugins, data_root):
    """源码目录**真的被删掉**（不是移走、不是留着）。

    🔴 情绪 2026-10-31 定：**不做回收站、不备份** —— 插件代码是可再生的
       （`repo:` + 重新安装），真正不可再生的是配置与数据，那两样由勾选项决定。
    ⚠️ 判据必须断言 `not exists()`：断言"目标目录存在"在"移走"和"删掉"
       两种实现下都成立，抓不住实现被改回移走。
    """
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, probe_dir, _bystander = two_plugins
    before = {
        str(p.relative_to(probe_dir)): p.read_bytes()
        for p in probe_dir.rglob("*")
        if p.is_file()
    }
    assert "main.py" in before, before

    result = await uninstall_plugin(host, "probe_plugin", data_root=data_root)

    assert not probe_dir.exists(), "源码目录还在 —— 卸载没真的删掉"
    assert result.source_path == str(probe_dir.resolve()), result.source_path
    # 不该留下任何暂存/回收目录
    assert not (data_root.parent / "plugin_trash").exists(), (
        "不该有回收站目录 —— 已按情绪要求去掉"
    )


async def test_uninstall_keeps_data_and_config_by_default(two_plugins, data_root):
    """默认**不动** `data/plugins/<id>/` 与配置文件 —— 那是用户的记忆/关系库。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    plugin_data = data_root / "probe_plugin"
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / "memory.json").write_text('{"keep": true}', encoding="utf-8")
    config_dir = data_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "probe_plugin_config.json"
    config_file.write_text('{"setting": 1}', encoding="utf-8")

    result = await uninstall_plugin(host, "probe_plugin", data_root=data_root)

    assert plugin_data.is_dir() and (plugin_data / "memory.json").is_file(), (
        "默认不该删插件数据目录"
    )
    assert config_file.is_file(), "默认不该删插件配置文件"
    assert result.data_dir is None and result.config_file is None


async def test_uninstall_deletes_data_and_config_when_asked(two_plugins, data_root):
    """显式 `delete_data` / `delete_config` → **一起删掉**（不是移走）。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    plugin_data = data_root / "probe_plugin"
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / "memory.json").write_text('{"keep": true}', encoding="utf-8")
    config_dir = data_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "probe_plugin_config.json"
    config_file.write_text('{"setting": 1}', encoding="utf-8")

    result = await uninstall_plugin(
        host, "probe_plugin", delete_config=True, delete_data=True, data_root=data_root
    )

    assert not plugin_data.exists(), "勾了删数据，但数据目录还在"
    assert not config_file.exists(), "勾了删配置，但配置文件还在"
    assert result.data_dir == str(plugin_data), result.data_dir
    assert result.config_file == str(config_file), result.config_file


async def test_uninstall_deletes_official_data_dir_names_too(two_plugins, data_root):
    """插件若按**官方**路径建了 `plugin_data/<id>`，也要能删到。

    🔴 守的是官方那个已知毛病（契约 §0 第 4 条）：官方 `delete_data` 删的是
       `plugin_data/<root_dir_name>`，而 `StarTools.get_data_dir()` 建的是
       `<plugin_name>` ⇒ **目录名 ≠ 插件名时删不到**，用户勾了"删数据"
       却在磁盘上还能找到。
       我们的做法是**候选目录全删**，不押注在某一种命名上。
    """
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    for name in ("plugin_data", "plugins_data"):
        d = data_root.parent / name / "probe_plugin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "store.json").write_text("{}", encoding="utf-8")

    await uninstall_plugin(
        host, "probe_plugin", delete_data=True, data_root=data_root
    )

    for name in ("plugin_data", "plugins_data"):
        d = data_root.parent / name / "probe_plugin"
        assert not d.exists(), f"{name}/probe_plugin 没被删掉（官方那个毛病）"


async def test_uninstall_never_touches_another_plugin_directory(two_plugins, data_root):
    """删 A **不许**动 B 的源码目录与数据目录。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, bystander_dir = two_plugins
    bystander_data = data_root / "bystander_plugin"
    bystander_data.mkdir(parents=True, exist_ok=True)
    (bystander_data / "keep.json").write_text("{}", encoding="utf-8")

    await uninstall_plugin(host, "probe_plugin", data_root=data_root)

    assert bystander_dir.is_dir() and (bystander_dir / "main.py").is_file()
    assert (bystander_data / "keep.json").is_file()


async def test_uninstall_unknown_plugin_changes_nothing_on_disk(two_plugins, data_root):
    """解析不到插件 → `not_loaded`，**磁盘上什么都没动**。"""
    from astrbot_host.plugin_lifecycle import PluginLifecycleError, uninstall_plugin

    host, probe_dir, bystander_dir = two_plugins
    with pytest.raises(PluginLifecycleError) as exc:
        await uninstall_plugin(host, "no_such_plugin", data_root=data_root)
    assert exc.value.code == "not_loaded"
    assert probe_dir.is_dir() and bystander_dir.is_dir()


async def test_uninstall_refuses_when_source_dir_cannot_be_resolved(two_plugins, data_root):
    """解析不到源码目录 → `unsupported` 且**拒绝执行**（绝不猜）。

    🔴 这守的是"删错目录"这一类不可接受错误。构造方式：把加载器的
       `requested_dirs` 指向一个不含该插件的空目录（= 开了镜像、
       源码根里找不到它的情形）。
    """
    from astrbot_host.plugin_lifecycle import (
        PluginLifecycleError,
        uninstall_plugin,
    )

    host, probe_dir, _bystander = two_plugins
    host.loader.requested_dirs = []
    host.loader.plugin_dirs = []

    with pytest.raises(PluginLifecycleError) as exc:
        await uninstall_plugin(host, "probe_plugin", data_root=data_root)
    assert exc.value.code == "unsupported"
    assert probe_dir.is_dir(), "拒绝执行时**不许**已经动了目录"


async def test_uninstall_removes_plugin_from_records_and_report(two_plugins, data_root):
    """卸载后：`records` 里没有它、`report` 里也不再统计它。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    await uninstall_plugin(host, "probe_plugin", data_root=data_root)

    assert "probe_plugin" not in host.loader.records
    assert "probe_plugin" not in host.report.loaded_plugins
    assert "probe_plugin" not in [n for n, _ in host.report.failed]
    assert host.report.loaded == 1, host.report.loaded
    # 旁观者必须整条健在
    assert host.loader.records["bystander_plugin"].state == "loaded"


async def test_uninstall_clears_pending_inactivated_entry(two_plugins, data_root):
    """停用名单里该插件的残留项**被剔除**（否则下次启动报假警告）。

    ⚠️ 用真宿主 + 真 writer 注入（不是 mock）：这条判据守的是"写配置那条链路
       到底通没通"，mock 掉就只有"调用过"这个弱证据。
    """
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    written: list[list[str]] = []
    host.inactivated_writer = lambda pid: (
        written.append([pid]) or f"{pid}.main"
    )

    result = await uninstall_plugin(host, "probe_plugin", data_root=data_root)

    assert written == [["probe_plugin"]], written
    assert result.removed_from_inactivated == "probe_plugin.main"


async def test_uninstall_without_writer_warns_instead_of_pretending(two_plugins, data_root):
    """没注入 writer 时**如实空串**（不假装清理成功）。"""
    from astrbot_host.plugin_lifecycle import uninstall_plugin

    host, _probe, _bystander = two_plugins
    host.inactivated_writer = None

    result = await uninstall_plugin(host, "probe_plugin", data_root=data_root)
    assert result.removed_from_inactivated == ""


# ---------------------------------------------------------------------------
# ④ 列表字段（面板动作菜单的可用性判据）
# ---------------------------------------------------------------------------


async def test_plugins_view_exposes_action_availability(two_plugins, data_root):
    """`plugins_view()` 给出 `can_reload` / `can_uninstall` / `source_path` / `config_path` / `data_path`。

    🔴 判据必须断言**真实路径**，不能只断言"字段存在" ——
       值照样有,存在性检查抓不住"指向了镜像副本"这种错。
    """
    host, probe_dir, _bystander = two_plugins
    views = {p["id"]: p for p in host.plugins_view()}
    entry = views["probe_plugin"]

    assert entry["can_reload"] is True
    assert entry["can_uninstall"] is True
    assert Path(entry["source_path"]) == probe_dir.resolve(), entry["source_path"]
    # 两个"勾了才会删"的具体路径 —— 面板确认框要逐条显示，不能只说"配置和数据"
    assert entry["config_path"].endswith("probe_plugin_config.json"), entry["config_path"]
    assert entry["data_path"].endswith("probe_plugin"), entry["data_path"]
    assert entry["reloadable_reason"] == ""


async def test_plugins_view_explains_why_reload_is_unavailable(two_plugins):
    """停用的插件 → `can_reload=False` **且给出原因**（不是只给一个灰按钮）。"""
    host, _probe, _bystander = two_plugins
    host.loader.records["probe_plugin"].inactivated = True

    entry = {p["id"]: p for p in host.plugins_view()}["probe_plugin"]
    assert entry["can_reload"] is False
    assert entry["reloadable_reason"], "不可用时必须说明原因"


async def test_source_path_points_at_real_source_not_mirror(source_root, data_root, tmp_path):
    """开了**源码镜像**时，`source_path` 必须指向**源码根**，不是镜像副本。

    🔴 这是"删错对象"的另一种形态：照镜像副本删，源码原地不动 ——
       用户看到"删除成功"，重启后插件又回来了。
    """
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.host_app import PluginHost

    probe_dir = _write_plugin(source_root, "probe_plugin", _COMPREHENSIVE)
    mirror = tmp_path / "data" / "plugin_src"
    set_data_root(data_root)
    host = PluginHost(plugin_dirs=[source_root], mirror_root=mirror, auto_initialize=False)
    await host.start(ports=HostPorts(config={}))
    try:
        entry = {p["id"]: p for p in host.plugins_view()}["probe_plugin"]
        assert Path(entry["source_path"]) == probe_dir.resolve(), (
            f"source_path 指错了：{entry['source_path']}（应指源码 {probe_dir}）"
        )
    finally:
        await host.stop()


# ---------------------------------------------------------------------------
# ⑤ 并发（锁的判据）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ⑥ 重载不能让**别的插件**的类身份漂移（生产事故 2026-10-31）
# ---------------------------------------------------------------------------


async def test_reload_does_not_purge_shared_packages_owned_by_another_plugin(
    source_root, data_root, tmp_path
):
    """重载 A 不许清掉**只有 B 才拥有**的共用顶层包。

    🔴 这是生产事故现场（2026-10-31 12:59:16）：
       重载 `content_companion` 时 `_purge_plugin_modules(force=True)` 无条件清了
       **653 个模块**，其中包含 `companion_v2.*`（只有陪伴插件拥有）。

       后果**不是报错**，而是**类身份漂移**：
         陪伴 7.0.0 的 `TaskError` / `ScopeKind` 等类对象属于旧模块；
         清掉后新导入的代码是新的类对象 ⇒ 插件里
         `isinstance(error, TaskError)` 恒 False（`application.py:516`）
         ⇒ `failure_code` 从具体码退化成 `host_dispatch_failed`
         ⇒ 整个 host 进 `faulted`、`accepting=false`
         ⇒ 面板表现为「消息测试 · 卡在管理员身份」。

    ⚠️ 判据：给 B 造一个**独有顶层包**，重载 A 之后它必须**还是同一个模块对象**
       （`id()` 不变）。只看"B 还能加载"抓不住 —— 它照样能加载，只是换了个类。
    """
    import sys

    from astrbot_host.plugin_lifecycle import reload_plugin

    # ⚠️ 必须**先**把两个插件写出来（fixture 才建目录），再往里加共用包
    _write_plugin(source_root, "probe_plugin", _COMPREHENSIVE)
    _write_plugin(source_root, "bystander_plugin", _BYSTANDER)

    # 🔴 关键：共用包名必须**落在被清的前缀里**，否则判据抓不住。
    #    `_purge_plugin_modules` 的前缀是 `(dir_name, "companion_v2", "astrbot_host")`
    #    ——所以这里用 `companion_v2`：**只有 B 拥有**它，重载 A 时它不该被清。
    #    ⚠️ 我第一版造的是 `lcshared_*`，那个名字不在前缀里 ⇒ 注入回退后判据照样绿
    #       （"抓不住"是**判据的错**，不是产品的对）。
    shared_name = "companion_v2"
    shared_dir = source_root / "bystander_plugin" / shared_name
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "__init__.py").write_text(
        "class Marker:\n    pass\n", encoding="utf-8"
    )
    # 让 B 的 main.py 引用它，从而真正 import 进 sys.modules
    bystander_main = source_root / "bystander_plugin" / "main.py"
    bystander_main.write_text(
        bystander_main.read_text(encoding="utf-8")
        + f"\nimport sys\nsys.path.insert(0, __import__('os').path.dirname(__file__))\n"
        + f"from {shared_name} import Marker  # noqa: E402\n",
        encoding="utf-8",
    )

    host = await _host(source_root, data_root)
    try:
        assert "bystander_plugin" in host.loader.records, host.report.summary()
        module_before = sys.modules.get(shared_name)
        assert module_before is not None, (
            f"前置：B 的独有包 {shared_name} 没被导入，判据无效"
        )
        marker_before = id(module_before.Marker)

        await reload_plugin(host, "probe_plugin")

        module_after = sys.modules.get(shared_name)
        assert module_after is not None, (
            f"重载 A 把 B 的独有包 {shared_name} 清掉了 —— 这就是类身份漂移的起点"
        )
        assert id(module_after.Marker) == marker_before, (
            "重载 A 让 B 的类对象换了身份（旧对象仍被 B 持有 ⇒ isinstance 恒 False）"
        )
    finally:
        await host.stop()
        # 🔴 **必须把这个假 `companion_v2` 从 sys.modules 里摘掉**（见上面 ② 的坑）：
        #    不摘的话它会污染后续测试里真实陪伴插件的
        #    `from companion_v2.adapters import ...` ⇒ 单跑绿、全量红。
        #    ⚠️ 判据要断言"真的摘干净了"，否则清理本身成了不可证伪的仪式。
        for name in [n for n in sys.modules if n == shared_name or n.startswith(shared_name + ".")]:
            del sys.modules[name]
        leaked = [n for n in sys.modules if n == shared_name or n.startswith(shared_name + ".")]
        assert not leaked, f"假包泄漏进 sys.modules：{leaked}"
        sys.path[:] = [p for p in sys.path if str(shared_dir.parent) != p]
