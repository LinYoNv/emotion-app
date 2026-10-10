# -*- coding: utf-8 -*-
"""插件详情（`PluginHost.plugin_detail()`）判据 —— 照真 AstrBot 的 `by-id` 形状。

## 真实现依据（本机真源码副本，只读引用）

    /root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/
      * `dashboard/services/plugin_service.py:45-52`  `PLUGIN_COMPONENT_TYPE_ORDER`
      * `:223-256`  `get_plugin_detail()` → `{**serialize_plugin_base(), "components": [...]}`
      * `:664-793`  `get_plugin_components_info()` 六类各自的字段
      * `:99-109`   `translated_event_type` 中文映射（九项）
      * `:1924-1981` README 只找 `README.md`

## 本文件守什么（每条都能失败）

这里最容易写错的是**归属**：把别家插件的 handler/工具算到这个插件头上，
详情页就会**撒谎**（而它看起来完全正常）。所以：

  ① 六类的分类判据（指令 / 指令组 / 正则 / 事件监听器 / 钩子 / LLM 工具）
  ② **归属隔离** —— 两个插件各自只看到自己的东西（不是"非空"就算数）
  ③ `PLUGIN_COMPONENT_TYPE_ORDER` 排序真的生效
  ④ 不存在的插件返回 `None`（不能编空壳，否则 404 变 200）
  ⑤ 响应**可 JSON 化**（临时字段里塞过函数对象 → `jsonify` 会 500）
  ⑥ 真插件上的**实测条数**（不是"≥0"那种假判据）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


# ===========================================================================
# 自造插件：确定性（不依赖磁盘上的真插件）
# ===========================================================================

PLUGIN_A = '''\
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.filter.regex import RegexFilter


class ProbeA(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("回声")
    async def echo_cmd(self, event: AstrMessageEvent, word: str = "默认"):
        yield event.plain_result(word)

    @filter.command("私密")
    @filter.permission_type(PermissionType.ADMIN)
    async def admin_cmd(self, event: AstrMessageEvent):
        yield event.plain_result("ok")

    @filter.regex(r"^你好.*")
    async def greet_regex(self, event: AstrMessageEvent):
        yield event.plain_result("hi")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def plain_listener(self, event: AstrMessageEvent):
        pass

    @filter.on_llm_request()
    async def on_req(self, event: AstrMessageEvent, req):
        pass

    @filter.llm_tool(name="tool_alpha")
    async def tool_alpha(self, event: AstrMessageEvent, query: str):
        """Alpha 工具的描述。

        Args:
            query(string): 查询词
        """
        yield event.plain_result("a")
'''

PLUGIN_B = '''\
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class ProbeB(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("只属于B")
    async def only_b(self, event: AstrMessageEvent):
        yield event.plain_result("b")

    @filter.llm_tool(name="tool_beta")
    async def tool_beta(self, event: AstrMessageEvent, q: str):
        """Beta 工具的描述。

        Args:
            q(string): 查询词
        """
        yield event.plain_result("b")
'''


def _write(root: Path, name: str, body: str, readme: str | None = None) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "metadata.yaml").write_text(
        f"name: {name}\nversion: 1.2.3\nauthor: 测试作者\ndesc: 测试简介\n",
        encoding="utf-8",
    )
    (pkg / "main.py").write_text(body, encoding="utf-8")
    if readme is not None:
        (pkg / "README.md").write_text(readme, encoding="utf-8")
    return pkg


def _make_host(tmp_path: Path, *, auto_initialize: bool = False):
    """建一个**真**的 PluginHost（装影子包 + 加载两个自造插件）。"""
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.host_app import PluginHost
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    root = tmp_path / "plugins"
    root.mkdir()
    _write(root, "alpha_probe", PLUGIN_A, readme="# Alpha\n\n这是 alpha 的说明。\n")
    _write(root, "beta_probe", PLUGIN_B)

    host = PluginHost(
        plugin_dirs=[str(root)],
        auto_initialize=auto_initialize,
        mirror_root=None,
    )
    asyncio.run(host.start(ports=HostPorts()))
    return host, root


@pytest.fixture
def host(tmp_path):
    h, _ = _make_host(tmp_path)
    yield h
    asyncio.run(h.stop())


# ===========================================================================
# ① 分类判据
# ===========================================================================


def test_missing_plugin_returns_none(host):
    """④ 不存在的插件 → `None`（调用方才能转 404，而不是给个空壳）。"""
    assert host.plugin_detail("__no_such_plugin__") is None


def test_components_sorted_by_type_order(host):
    """③ 排序照真实现 `PLUGIN_COMPONENT_TYPE_ORDER`。"""
    d = host.plugin_detail("alpha_probe")
    order = {t: i for i, t in enumerate(host.COMPONENT_TYPE_ORDER)}
    seq = [order[c["type"]] for c in d["components"]]
    assert seq == sorted(seq), f"排序不对：{[c['type'] for c in d['components']]}"
    # 而且六类的相对顺序必须是 page < skill < command < llm_tool < listener < hook
    assert host.COMPONENT_TYPE_ORDER == (
        "page", "skill", "command", "llm_tool", "listener", "hook",
    ), "类型顺序与真实现 `plugin_service.py:45-52` 不一致"


def test_command_classification(host):
    """① 指令 / 需管理员 / 正则 / 事件监听器 / 钩子 各自分对。"""
    d = host.plugin_detail("alpha_probe")
    by_type: dict[str, list] = {}
    for c in d["components"]:
        by_type.setdefault(c["type"], []).append(c)

    cmds = {c["cmd"]: c for c in by_type.get("command", [])}
    # 普通指令
    assert "回声" in cmds, f"没识别出「回声」：{list(cmds)}"
    assert cmds["回声"]["display_type"] == "指令"
    # 需管理员的指令（`@filter.permission_type(PermissionType.ADMIN)`）
    assert cmds["私密"]["has_admin"] is True, "管理员指令没被标出来"
    assert cmds["回声"]["has_admin"] is False, "普通指令被误标成管理员"
    # 正则（`display_type` 必须是「正则匹配」，cmd 是正则原文）
    rgx = [c for c in by_type["command"] if c["display_type"] == "正则匹配"]
    assert rgx and rgx[0]["cmd"].startswith("^你好"), f"正则没识别：{rgx}"
    # 事件监听器（AdapterMessageEvent 但没有 command/regex 过滤器）
    assert by_type.get("listener"), "普通监听器没被识别成 listener"
    # 钩子（@filter.on_llm_request → OnLLMRequestEvent）
    hooks = by_type.get("hook") or []
    assert any(c["event_type"] == "OnLLMRequestEvent" for c in hooks), \
        f"钩子没识别：{[c['event_type'] for c in hooks]}"
    assert any(c["event_type_h"] == "LLM 请求时" for c in hooks), "中文映射没生效"


def test_llm_tool_listed_once_with_real_name(host):
    """🔴 LLM 工具**只列一次**，且用工具表里的真名。

    为什么单列这一条（实测抓到的真 bug）：同一件事在两侧名字不同 ——
      handler 侧 `handler_name` 可能带 `_tool` 后缀，工具表 `spec.name` 不带。
      按**名字**去重会一个都不命中 → 同一工具列两遍。
      去重键必须是 handler **函数对象**（实测两侧是同一个 `partial` 对象）。
    """
    d = host.plugin_detail("alpha_probe")
    tools = [c for c in d["components"] if c["type"] == "llm_tool"]
    names = [c["name"] for c in tools]
    assert len(names) == len(set(names)), f"工具重复了：{names}"
    assert "tool_alpha" in names, f"没列出 tool_alpha：{names}"
    # 描述必须来自工具表/docstring（不是「无描述」）
    alpha = next(c for c in tools if c["name"] == "tool_alpha")
    assert "Alpha 工具的描述" in alpha["description"], \
        f"工具描述没取到（可能是从 handler 侧拿了空 desc）：{alpha['description']!r}"
    assert alpha["cmd"] == "自动触发", "照真实现，LLM 工具的 cmd 是「自动触发」"
    assert alpha["display_type"] == "无", "照真实现，LLM 工具的 display_type 是「无」"


# ===========================================================================
# ② 归属隔离（最容易写错、也最会"撒谎"的一条）
# ===========================================================================


def test_ownership_isolation(host):
    """② 两个插件各自**只**看到自己的指令与工具。"""
    a = host.plugin_detail("alpha_probe")
    b = host.plugin_detail("beta_probe")

    a_cmds = {c["cmd"] for c in a["components"] if c["type"] == "command"}
    b_cmds = {c["cmd"] for c in b["components"] if c["type"] == "command"}
    assert "回声" in a_cmds and "只属于B" not in a_cmds, f"alpha 串到别家：{a_cmds}"
    assert "只属于B" in b_cmds and "回声" not in b_cmds, f"beta 串到别家：{b_cmds}"

    a_tools = {c["name"] for c in a["components"] if c["type"] == "llm_tool"}
    b_tools = {c["name"] for c in b["components"] if c["type"] == "llm_tool"}
    assert a_tools == {"tool_alpha"}, f"alpha 的工具集不对：{a_tools}"
    assert b_tools == {"tool_beta"}, f"beta 的工具集不对：{b_tools}"

    # 每条 component 都不能带别家的 handler 名
    for c in a["components"]:
        hn = c.get("handler_name") or ""
        assert "only_b" not in hn and "tool_beta" not in hn, f"串了：{c}"


# ===========================================================================
# ⑤ 可 JSON 化（临时字段里塞过函数对象 → jsonify 会 500）
# ===========================================================================


def test_detail_is_json_serializable(host):
    """⑤ 详情必须能直接 `jsonify` —— 不留任何函数对象/临时字段。"""
    for pid in ("alpha_probe", "beta_probe"):
        d = host.plugin_detail(pid)
        text = json.dumps(d, ensure_ascii=False)   # 会抛 TypeError（若留了函数对象）
        assert text
        for c in d["components"]:
            leaked = [k for k in c if k.startswith("_")]
            assert not leaked, f"临时字段漏进响应：{leaked}（{pid}）"


# ===========================================================================
# ⑥ 真插件上的实测条数（不是 "≥0" 那种假判据）
# ===========================================================================

def _plugin_source_root() -> Path:
    """插件**源码**根目录 —— 从配置读，**不写死**（2026-10-10 修）。

    🔴 原来硬编码 `data/plugin_src`（我早期镜像别人的副本目录）。
       改自建 `plugins/` 后它没了 ⇒ 这几条判据**静默 skip**
       （全量冒出来 `4 skipped`）。**跳过判据 = 假通过**是本项目红线，
       所以这里**找不到就抛异常**，让"判据没跑"响起来。
    """
    import os

    raw = os.environ.get("EMOTION_PLUGINS_PLUGIN_DIRS", "")
    cands = [Path(x.strip()) for x in raw.split(";") if x.strip()]
    if not cands:
        cands = [Path(__file__).resolve().parent.parent.parent / "plugins"]
    for c in cands:
        c = c if c.is_absolute() else Path(__file__).resolve().parent.parent.parent / c
        if c.is_dir():
            return c
    raise AssertionError(
        f"找不到插件源码目录（判据**不能**跳过）：候选={[str(c) for c in cands]}"
    )


REAL_ROOT = _plugin_source_root()
REAL_PLUGIN = REAL_ROOT / "astrbot_plugin_private_companion"

# 🔴 **不 skip**：插件是我们自带的（`plugins/` 随仓库走），它**必须**在。
#    真不见了就是有人删错了东西 —— 那是要当场响的事故，不是"环境不具备"。
if not (REAL_PLUGIN / "main.py").is_file():
    raise AssertionError(
        f"自带的陪伴插件不存在：{REAL_PLUGIN} —— "
        f"plugins/ 是随仓库走的，缺了就是被删错，**不许跳过判据**"
    )


@pytest.fixture(scope="module")
def real_host(tmp_path_factory):
    """加载**真**陪伴插件（module 级复用 —— 加载要几十秒）。"""
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.host_app import PluginHost
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()

    # 🔴🔴 **先把插件模块从 `sys.modules` 里逐出去，再从零加载**
    #    （2026-10-10 修，这是本条判据"单跑绿、全量红"的根因）。
    #
    #    为什么必须逐：`@filter.command` / `@filter.llm_tool` 这些装饰器
    #    **只在模块首次 import 时执行一次**。而套件里有多处
    #    `star_handlers_registry.clear()`（`test_compat_regressions` /
    #    `test_dispatch` / `test_llm_hooks` / `test_plugin_activation_api`，共 9 处）
    #    ⇒ 清掉之后，本夹具虽然"重新加载"插件，但模块已在 `sys.modules`，
    #      装饰器**不会重跑** ⇒ 注册表仍是空的 ⇒
    #      `handler_count=0` / `llm_tool_bind_count=0`，
    #      而 `components` 读的是**现场注册表**（也是空）——但 `plugin_detail`
    #      会把"插件声明的工具表"列出来 ⇒ 出现 `列出的(7) != 绑定数(0)`。
    #
    #    ⚠️ 这正是本项目记录在案的坑：「判据不许依赖跨测试漂移的进程级状态」。
    #       ✅ 修法不是把断言放宽（那是掩盖），而是**让夹具自己造出确定的前提**：
    #          逐出模块 ⇒ 装饰器必然重跑 ⇒ 计数确定。
    import sys as _sys

    for _name in [
        n for n in list(_sys.modules)
        if n.split(".")[0].startswith("astrbot_plugin_") or n.startswith("data.plugins.")
    ]:
        _sys.modules.pop(_name, None)

    # 🔴 必须镜像：真插件会回写自己的源码目录（`source_mirror` 模块说明）。
    mirror = tmp_path_factory.mktemp("detail-mirror")
    host = PluginHost(
        plugin_dirs=[str(REAL_ROOT)],
        auto_initialize=False,
        mirror_root=str(mirror),
    )
    asyncio.run(host.start(ports=HostPorts()))
    yield host
    asyncio.run(host.stop())


def test_real_companion_components(real_host):
    # 🔴 **不 skip**：陪伴插件是我们自带的（`plugins/` 随仓库走），必须在。
    """⑥ 真陪伴插件的六类**实测条数**。

    ⚠️ 这些数字是**实测**（`tools/probe_plugin_detail.py` 输出），不是估计。
       插件升级后数字会变 —— 那时应该更新这条断言并**在报告里说明**，
       而不是把断言改成 `>0`（那就成了永远为真的假判据）。
    """
    d = real_host.plugin_detail("astrbot_plugin_private_companion")
    assert d is not None
    by_type: dict[str, int] = {}
    for c in d["components"]:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1

    # 🔴🔴 **这里不能断言"绝对条数"，只能断言"下界"**（我在这里连栽两次）。
    #
    #    原因：`star_handlers_registry` 是**进程级单例**，而
    #    `host/tests/test_compat_regressions.py` 会 `clear()` 它（多处）。
    #    插件模块已被 `sys.modules` 缓存 → 装饰器**不会重跑** → 被清掉的那些
    #    handler 不会自动回来。于是同一个插件的注册数**取决于测试执行顺序**：
    #      实测 单跑本文件：`hook=77`
    #           全量跑时  ：`hook=76`（`atrelay` 子模块那个 `OnLLMResponseEvent` 不在）
    #    ⇒ 写死 `== 76` 会让**单跑红、全量绿**；写死 `== 77` 反之。
    #      这正是本项目反复踩的「**判据不许依赖可变的持久状态**」。
    #
    #    正确做法：
    #      * 绝对条数用 **`>=` 下界**（只锁"插件没退化到几乎没注册东西"，
    #        这能抓住"兼容层把 handler 弄丢了"这类真回归）；
    #      * "有没有漏/多"交给下面那条**与注册表现场快照的集合比较**（同源，不漂）。
    assert by_type.get("command") == 3, f"指令数变了：{by_type}"
    # 🔴 **能写精确值了**（2026-10-10）：`real_host` 夹具现在会先把插件模块
    #    从 `sys.modules` 逐出再加载 ⇒ 装饰器必然重跑 ⇒ 计数**确定**，
    #    不再随"套件里 9 处 `star_handlers_registry.clear()`"漂移。
    #    ⚠️ 本条经历过 `22`（单跑）→ 全量 `7`（被 clear 过、装饰器没重跑）；
    #       最后的修法是**让夹具造出确定前提**，而不是把断言放宽 ——
    #       放宽成下界会**掩盖真回归**（少注册几个工具照样绿）。
    assert by_type.get("llm_tool") == 22, f"LLM 工具数变了：{by_type}"
    # ⚠️ **2026-10-08 从 1 改成 2 —— 不是产品回归，是旧实现漏列**（已在报告里说明）：
    #    本插件 `pages/` 下有**两个真实目录**（`companion-panel` 与 `陪伴面板`，
    #    各含 `index.html`），而 `metadata.yaml` **只声明了 `companion-panel`**。
    #    旧实现只读 metadata ⇒ 漏掉「陪伴面板」；改成扫目录后正确地列出 2 个。
    #    真 AstrBot 的 `discover_plugin_pages()` 本来就是**扫目录**的。
    #    ⇒ `2` 是**正确答案**（实测 `pages/` 目录清单 = ['companion-panel','陪伴面板']）。
    assert by_type.get("page") == 2, f"拓展页数变了：{by_type}"
    assert by_type.get("listener") >= 11, f"监听器数退化了：{by_type}"
    # ⚠️ 同上：夹具确定 ⇒ 可以写精确值（原来写 `>= 76` 是因为会漂到 76/77）
    assert by_type.get("hook") == 77, f"钩子数变了：{by_type}"

    # ⭐ 数量自洽判据（⚠️ **注意减掉 page**）：
    #     `components` = **handler 类**（command/llm_tool/listener/hook）
    #                   + **非 handler 类**（page —— 它来自 metadata，没注册 handler）。
    #     而 `handler_count` 只数 handler。所以要比的是**去掉 page 之后**的条数。
    #
    #     🔴 这里我被**巧合**骗过一次：早先 `len(components)==handler_count`
    #        看起来是 113==113，其实是"112 个 handler + 1 个 page 恰好补平"，
    #        而**真正的 handler 是 112、少列了 1 个**（子模块 atrelay 那个）。
    #        ⇒ **数量相等 ≠ 组成正确**；数量判据只能当辅助，
    #          真判据是下面那条**逐条集合相等**。
    listed_handlers = [c for c in d["components"] if c["type"] != "page"]
    assert len(listed_handlers) == d["handler_count"], (
        f"列出的 handler 数({len(listed_handlers)}) != handler_count({d['handler_count']}) "
        "—— 要么漏了、要么重复了"
    )
    # ⚠️ 同一条自洽判据也适用于 llm_tool（2026-10-10 加）：
    #    "列出的 llm_tool 条数"必须等于"**同一次加载里**绑定了本插件实例的个数"。
    #    ⚠️ 两边都在同一个 `d` 里取 ⇒ **同源不漂**，所以可以用 `==`（不是下界）。
    listed_tools = [c for c in d["components"] if c["type"] == "llm_tool"]
    assert len(listed_tools) == d["llm_tool_bind_count"], (
        f"列出的 llm_tool({len(listed_tools)}) != llm_tool_bind_count({d['llm_tool_bind_count']})"
        " —— 说明【列出的】与【真绑上实例的】对不上"
    )
    # 🔴🔴 **逐条集合相等**，不是"数量相等"（我在这里被数量骗过一次）：
    #     数量曾经恰好是 113 == 113，但**组成是错的** ——
    #     子模块 `astrbot_plugin_private_companion.atrelay` 里那个 handler
    #     没被列进来，而它的位置被 1 个 `page` 组件补平了。
    #     ⇒ **数量相等 ≠ 组成正确**。判据必须是"逐条集合相等"。
    #
    # ⚠️ 必须**现场从注册表取**"应该有哪些"，别把名字**写死**在测试里：
    #    `star_handlers_registry` 是**进程级单例**，而
    #    `host/tests/test_compat_regressions.py` 会 `clear()` 它（多处）。
    #    pytest 在同一进程里跑 ⇒ 本测试的夹具**重新加载**后注册表内容
    #    取决于"哪些模块这次真的重新注册了"。
    #    我第一版把 `atrelay` 那个名字**写死**成指名断言，结果：
    #    单跑本文件 ✅、全量跑 ❌（那时它不在）——**判据依赖了跨测试的漂移状态**。
    #    正确写法就是下面这两行：拿注册表现场快照与详情列出的做**集合比较**，
    #    两边同源 ⇒ 无论注册表处于什么状态，这条都只判"详情有没有漏/多"。
    from astrbot_host.registry import star_handlers_registry  # noqa: PLC0415

    pkg = d["module_path"].split(".")[0]
    want = {
        h.handler_full_name
        for h in star_handlers_registry.all_handlers()
        if h.handler_module_path == pkg or h.handler_module_path.startswith(f"{pkg}.")
    }
    got = {c["handler_full_name"] for c in d["components"] if c.get("handler_full_name")}
    assert got == want, (
        f"详情列出的 handler 集合与注册表不一致\n"
        f"  缺（注册表有、详情没列）：{sorted(want - got)}\n"
        f"  多（详情列了、注册表没有）：{sorted(got - want)}"
    )
    # 子模块（`<插件包>.<子模块>`）里的 handler 也必须在 —— 用**现场数据**判，
    # 而不是写死某个名字（见上）。这条能抓住"只匹配 `<dir>.main`"那个 bug：
    # 那种实现下，属于子模块的项会全部缺失。
    sub_module_handlers = {
        h.handler_full_name
        for h in star_handlers_registry.all_handlers()
        if h.handler_module_path.startswith(f"{pkg}.")
        and h.handler_module_path != f"{pkg}.main"
    }
    if sub_module_handlers:
        assert sub_module_handlers <= got, (
            f"子模块里的 handler 没被列进详情：{sorted(sub_module_handlers - got)}"
        )
    # 工具条数必须与"绑定成功的工具数"一致（本项目最贵那类 bug 的探针）
    assert by_type.get("llm_tool") == d["llm_tool_bind_count"], (
        f"列出的工具({by_type.get('llm_tool')}) != 绑定数({d['llm_tool_bind_count']})"
    )
    names = {c["cmd"] for c in d["components"] if c["type"] == "command"}
    assert "陪伴" in names, f"没列出「陪伴」指令：{names}"


def test_real_readme(real_host):
    """⑥b 真插件的 README 能取到（照真实现只找 `README.md`）。"""
    r = real_host.plugin_readme("astrbot_plugin_private_companion")
    assert r is not None, "陪伴插件有 README.md，不该取不到"
    text, truncated = r
    assert len(text) > 1000, f"README 内容太短，可疑：{len(text)}"
    assert truncated is False

    # 反向：不存在的插件 → None（不能编一个空 README）
    assert real_host.plugin_readme("__no_such_plugin__") is None


def test_real_memory_companion_no_duplicate_tools(real_host):
    # 🔴 **不 skip**（同 `test_real_readme`）：它也是我们自带的插件，必须在。
    """⑥c 回归：`memory_companion` 的工具曾经被列**两遍**（7 → 14）。

    根因：它 handler 侧的 `handler_name` 是 `..._recall_tool`，
    而工具表 `spec.name` 是 `..._recall` —— 按**名字**去重一个都不命中。
    这条判据锁死"名字不同但对象相同"这种形状（与陪伴插件同源的那类）。
    """
    d = real_host.plugin_detail("astrbot_plugin_memory_companion")
    assert d is not None, "memory_companion 应该能被加载"
    tools = [c["name"] for c in d["components"] if c["type"] == "llm_tool"]
    assert len(tools) == len(set(tools)), f"工具重复：{tools}"
    assert len(tools) == d["llm_tool_bind_count"], (
        f"列出的工具({len(tools)}) != 绑定数({d['llm_tool_bind_count']})：{tools}"
    )
    # 🔴 **不写死条数**（2026-10-10 修）。
    #    原来写 `== 7`，那是按 2.1.4 实测的；插件升到 **2.3.1**（从 GitHub 拉的）后
    #    变成 **11** 个工具 ⇒ 判据红。**那不是 bug，是插件正常长了 4 个工具。**
    #
    #    ⚠️ 本项目的教训（已记进 `AGENTS.md`）：
    #       "判据的期望值要能**现场取得**，别写死一个受版本/执行顺序影响的值"。
    #       这条判据**真正**要守的是"**没有重复列出**"（上面两行已覆盖：
    #       `len(tools) == len(set(tools))` + `== llm_tool_bind_count`）。
    #       条数只要是个**下界**就够了 —— 锁"没退化到几乎没列出东西"。
    assert len(tools) == 11, f"memory_companion 的工具数变了（2.3.1 实测 11）：{tools}"
    # 同样要**减掉 page** 再比（memory_companion 没有 pages，故这里两者恰好相等 ——
    # 但写成减 page 的形式才不会在它将来声明了页面时变成假红）
    listed_handlers = [c for c in d["components"] if c["type"] != "page"]
    assert len(listed_handlers) == d["handler_count"], (
        f"列出的 handler 数({len(listed_handlers)}) != handler_count({d['handler_count']})"
    )
