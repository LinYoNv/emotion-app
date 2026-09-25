# -*- coding: utf-8 -*-
"""兼容层回归判据 —— 每个测试对应一个**实测发现的真 bug**。

⭐ 本文件的性质：**先有能失败的判据，再改代码**。
   三条 bug 都是 2026-09-20 扫 82 个派发目标时实测发现的，
   写测试时 bug 还在，所以**这三个测试起初必须是红的**（这本身就是注入 bug 验证）。

依据全部来自真 AstrBot 4.27.5 源码，逐条注明行号。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


# ---------------------------------------------------------------------------
# BUG-1：子指令没挂 CommandFilter（真实现挂，我们早退了一行）
# ---------------------------------------------------------------------------
def test_sub_command_handler_gets_command_filter():
    """子指令的 handler **必须**挂上 CommandFilter。

    真实现 `core/star/register/star_handler.py:116-123`：
        handler_md = get_handler_or_create(...)
        if new_command:
            new_command.init_handler_md(handler_md)
            handler_md.event_filters.append(new_command)   # ← 裸指令/子指令都挂

    而我们原来在子指令分支提前 `return awaitable`，只挂进了父指令组。

    ⚠️ 为什么这是**致命的**：真实现派发时 `waking_check/stage.py:182-183`
        `if len(handler.event_filters) == 0: continue`
       → 无过滤器的 handler 被**跳过**。所以这 16 个子指令**永远不执行**
       （不是"每条消息都触发"—— 我一度把方向写反，已纠正）。
    """
    from astrbot_host.compat.api.event import filter as filter_mod
    from astrbot_host.compat.core.star.star_handler import star_handlers_registry
    from astrbot_host.registry import EventType

    star_handlers_registry.clear()

    class FakeStar:
        @filter_mod.command_group("grp")
        def my_group(self):
            pass

        @my_group.command("sub")
        async def my_sub(self, event):
            return "ok"

    handlers = star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent, only_activated=False
    )
    sub = [h for h in handlers if h.handler_name == "my_sub"]
    assert sub, "子指令 handler 没注册上"
    md = sub[0]

    assert md.event_filters, (
        "子指令 handler 的 event_filters 为空 → 派发时会被 `len(...)==0: continue` 跳过，"
        "该指令永不执行（真实现会挂 CommandFilter）"
    )
    names = [n for f in md.event_filters for n in f.get_complete_command_names()]
    assert "grp sub" in names, f"子指令完整名不对：{names}"

    # 真实现：**同一个** filter 对象既进父组、又进 handler 的 event_filters
    cgf = [f for f in md.event_filters if type(f).__name__ == "CommandFilter"][0]
    assert cgf.parent_command_names == ["grp"], cgf.parent_command_names


def test_sub_command_filter_is_shared_with_parent_group():
    """真实现把**同一个** CommandFilter 同时放进父指令组与 handler。

    （父组那份用于 `print_cmd_tree` 帮助树与 WebUI 命令列表；
      handler 那份用于派发。两份缺一不可。）
    """
    from astrbot_host.compat.api.event import filter as filter_mod
    from astrbot_host.compat.core.star.star_handler import star_handlers_registry
    from astrbot_host.registry import EventType

    star_handlers_registry.clear()

    class FakeStar:
        @filter_mod.command_group("g2")
        def g2(self):
            pass

        @g2.command("s2")
        async def s2(self, event):
            return None

    handlers = star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent, only_activated=False
    )
    md = [h for h in handlers if h.handler_name == "s2"][0]
    handler_flt = [f for f in md.event_filters if type(f).__name__ == "CommandFilter"][0]

    # 找父组（带 CommandGroupFilter 的那个 handler）
    grp_md = [h for h in handlers if h.handler_name == "g2"][0]
    grp_flt = [f for f in grp_md.event_filters if type(f).__name__ == "CommandGroupFilter"][0]

    assert grp_flt.sub_command_filters, "父指令组里没有子指令过滤器"
    assert handler_flt is grp_flt.sub_command_filters[0], (
        "父组与 handler 应当共享**同一个** CommandFilter 对象（照真实现）"
    )


# ---------------------------------------------------------------------------
# BUG-2：is_stopped 是方法，且 _force_stopped 不被 clear_result 重置
# ---------------------------------------------------------------------------
def test_is_stopped_is_method_with_force_stopped_semantics():
    """`is_stopped` 必须是**方法**，且语义为 `_force_stopped or _result.is_stopped()`。

    真实现 `astr_message_event.py:58` 注释原话：
        self._force_stopped: bool = False
        "独立的停止标志，不依赖 _result，不会被 clear_result() 重置"

    `:359-365`：
        def is_stopped(self) -> bool:
            if self._force_stopped: return True
            if self._result is None: return False
            return self._result.is_stopped()

    ⚠️ 我们原来把 `_stopped` 做成**字段**，
       插件写 `if event.is_stopped:`（漏括号）会**恒为真** → 静默吞掉所有消息。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    ev = AstrMessageEvent()
    assert callable(ev.is_stopped), "is_stopped 必须是方法（真实现 :359）"

    assert ev.is_stopped() is False, "初始未终止"

    ev.stop_event()
    assert ev.is_stopped() is True

    # 🔴 关键判据：clear_result() **不能**重置 _force_stopped
    ev.clear_result()
    assert ev.is_stopped() is True, (
        "clear_result() 后仍应为 stopped —— 真实现靠独立的 _force_stopped "
        "（:58 注释「不会被 clear_result() 重置」）。若这里变 False，就是把它做成了字段语义"
    )

    # continue_event() 才恢复
    ev.continue_event()
    assert ev.is_stopped() is False


def test_is_stopped_reflects_result_stop_flag():
    """`is_stopped()` 也要反映 `_result.is_stopped()`（真实现 :364）。"""
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.message.message_event_result import MessageEventResult

    ev = AstrMessageEvent()
    res = MessageEventResult()
    res.stop_event()
    ev.set_result(res)

    assert ev.is_stopped() is True, "结果里标了 stop_event，事件也应是 stopped"


# ---------------------------------------------------------------------------
# BUG-3：plain_result 是"纯构造"，不写 _result
# ---------------------------------------------------------------------------
def test_plain_result_does_not_mutate_event_result():
    """`plain_result()` 只构造结果，**不改** event 的 `_result`。

    真实现 `astr_message_event.py:399-401`：
        def plain_result(self, text: str) -> MessageEventResult:
            return MessageEventResult().message(text)      # ← 没有 self._result = ...

    为什么重要：真实现的执行循环靠 `call_handler`（`context_utils.py:71-73`）
    在 **yield 那一刻**才 `event.set_result(ret)`，并且**每轮 handler 后
    `event.clear_result()`**（`star_request.py:53`）。
    我们提前写入 `_result` → "上一个 handler 的结果"与"清除"语义全部错位。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    ev = AstrMessageEvent()
    ret = ev.plain_result("hi")

    # 返回值本身要有内容
    assert str(ret) == "hi", f"plain_result 应返回含文本的结果，实际 {str(ret)!r}"
    # 🔴 但事件上的 _result 不能被它改写
    assert ev.get_result() is None, (
        "plain_result() 不该写 event._result（真实现 :399 是纯构造）"
    )


def test_chain_result_does_not_mutate_event_result():
    """`chain_result()` 同 plain_result —— 纯构造（真实现 :412-418）。"""
    from astrbot_host.compat.core.platform.astr_message_event import (
        AstrMessageEvent,
        MessageChain,
    )
    from astrbot_host.compat.core.message.components import Plain

    ev = AstrMessageEvent()
    ret = ev.chain_result([Plain("a"), Plain("b")])
    assert str(ret) == "ab"
    assert ev.get_result() is None, "chain_result() 不该写 event._result"


# ---------------------------------------------------------------------------
# 反面判据：四个"插件自身 bug"的 API 我们**不许**提供
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["plain", "wait", "send_message", "get_user_id"])
def test_plugin_side_bugs_are_not_polyfilled(name: str):
    """`plain` / `wait` / `send_message` / `get_user_id` 真实现**都没有**（实测）。

    证据：用真 AstrBot 解释器 `hasattr(AstrMessageEvent, n)` 全为 False，
    且遍历 `astrbot.core.platform` 下**全部事件子类（0 个）**也没有。

    → 它们是**插件自身 bug**（在真 AstrBot 上同样 AttributeError）。
      **我们不许补** —— 补了就是伪造能力（作者判据：不伪造 delivered/能力）。

    ⚠️ 这条判据的价值：防止将来有人"顺手兼容一下"，让我们的宿主与真宿主行为分叉。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    assert not hasattr(AstrMessageEvent, name), (
        f"`{name}` 在真 AstrBot 的 AstrMessageEvent 上不存在（实测），"
        f"我们不应提供 —— 否则宿主行为与真实现分叉，且属伪造能力"
    )


# ---------------------------------------------------------------------------
# BUG-4：插件 main.py 被【执行两次】→ 装饰器挂两遍 → 过滤器重复
# ---------------------------------------------------------------------------
def test_plugin_module_executed_once(tmp_path, monkeypatch):
    """加载插件时 `<包>/main.py` 只能**执行一次**。

    🔴 实测根因（`loader/plugin_loader.py:_import_plugin`）：
       ① `import_module(name)` 执行 `__init__.py`，
          而多数插件的 `__init__.py` 里写着 `from .main import XxxPlugin`
          → **`main.py` 在这里被第一次执行**；
       ② 紧接着代码看到 `"<包>.main" in sys.modules` 为真 → 走
          `importlib.reload(...)` → **`main.py` 第二次执行**。

    后果（实测）：类体跑两遍 → `@filter.command` / `@permission_type`
    各挂两遍 → handler 的 `event_filters` 出现**完全相同的重复项**
    （实测 `astrbot_plugin_memory_companion` 有 **23 个** handler 中招，
     每个的 `CommandFilter` + `PermissionTypeFilter` 各两份）。
    `reload` 本意是支持"同进程重复加载保持新鲜"，但首次加载时是**多余**的。
    """
    import importlib
    import sys as _sys

    # 造一个"__init__.py 里 import .main"的最小插件（这正是真实插件的样子）
    pkg = tmp_path / "fake_plugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from .main import FakePlugin\n__all__ = ['FakePlugin']\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text(
        "EXEC_COUNT = globals().get('EXEC_COUNT', 0) + 1\n"
        "class FakePlugin:\n    pass\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    for k in [k for k in list(_sys.modules) if k.startswith("fake_plugin")]:
        del _sys.modules[k]

    from astrbot_host.loader.plugin_loader import PluginLoader

    mod = PluginLoader._import_plugin(pkg)
    main_mod = _sys.modules["fake_plugin.main"]

    assert main_mod.EXEC_COUNT == 1, (
        f"`main.py` 被执行了 {main_mod.EXEC_COUNT} 次（应为 1）。"
        "重复执行会让插件类体的装饰器挂两遍 → 过滤器重复"
    )

    # 再加载一次（模拟同进程重复 load_all）：这时才需要 reload 保持新鲜
    mod2 = PluginLoader._import_plugin(pkg)
    assert _sys.modules["fake_plugin.main"].EXEC_COUNT == 2, (
        "第二次调用 _import_plugin 应当 reload（保持同进程重复加载的新鲜度）"
    )


def test_no_duplicate_filters_on_real_plugins():
    """真插件加载后，**不允许**出现"同一个 handler 挂两个一模一样过滤器"。

    这是 BUG-4 在真实数据上的判据（`memory_companion` 的 23 个 `mcomp` 子指令）。

    ⚠️ **本测试必须自己清理全局状态**（实测踩到）：
       `star_handlers_registry` 与 `sys.modules` 都是**模块级单例**。
       如果本测试把真插件 import 进来后不管，后续测试（`test_loader_*`）
       会拿到"已在 sys.modules 里"的包 → 走的加载分支不同 →
       出现**只在跑全量时才失败**的假故障。
       （我第一版就是这样把 12 个原本通过的测试搞挂了 —— 不是产品 bug，是测试污染。）
    """
    from pathlib import Path

    import pytest as _pytest

    root = Path("/opt/astrbot/data/plugins")
    if not root.is_dir():
        _pytest.skip(f"插件目录不存在：{root}")

    import asyncio
    import sys as _sys
    import tempfile

    HOST = Path(__file__).resolve().parent.parent
    _sys.path.insert(0, str(HOST))

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.registry import EventType, star_handlers_registry
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    star_handlers_registry.clear()

    # 快照 → 跑 → finally 还原（免得污染后续测试）
    plugin_names = {q.name for q in root.iterdir() if q.is_dir()}
    saved = {
        k: v for k, v in list(_sys.modules.items())
        if k.split(".")[0] in plugin_names
    }
    try:
        with tempfile.TemporaryDirectory() as mirror:
            loader = PluginLoader(plugin_dirs=[str(root)], mirror_root=mirror)
            asyncio.run(loader.load_all(ports=HostPorts(config={})))

        bad = []
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        ):
            seen = []
            for f in h.event_filters or []:
                names = getattr(f, "get_complete_command_names", None)
                key = (type(f).__name__, str(names()) if callable(names) else "")
                if key in seen:
                    bad.append(
                        f"{h.handler_module_path.split('.')[-1]}::{h.handler_name} "
                        f"重复 {key[0]}"
                    )
                seen.append(key)

        assert not bad, (
            f"发现 {len(bad)} 个 handler 挂了重复过滤器（BUG-4：main.py 被跑了两遍）\n"
            + "\n".join(f"  - {b}" for b in bad[:10])
        )
    finally:
        # 还原 sys.modules（移除本测试新导入的插件模块）
        for k in [k for k in list(_sys.modules) if k.split(".")[0] in plugin_names]:
            if k not in saved:
                del _sys.modules[k]
        _sys.modules.update(saved)
        star_handlers_registry.clear()



# ---------------------------------------------------------------------------
# BUG-5：注册表缺 __iter__ / __len__ / get_handlers_by_module_name
# ---------------------------------------------------------------------------
def test_registry_is_iterable_and_sized():
    """`star_handlers_registry` 必须**可迭代**且有 `__len__`。

    🔴 实测发现（服务日志里的一行 WARNING 牵出来的）：
       启动时插件打印
         `[主链] 修复热更新残留回调绑定失败: 'StarHandlerRegistry' object is not iterable`

    真实现 `core/star/star_handler.py` 里 `StarHandlerRegistry` 有魔法方法
    `__iter__` / `__len__`（`hasattr` 实测 True），且**真 AstrBot 自己在多处迭代它**：
       * `core/platform/sources/telegram/tg_adapter.py:344`
       * `core/platform/sources/discord/discord_platform_adapter.py:412`
       * `core/star/command_management.py:256` / `:381`
    → 插件照抄这个用法（`for handler_md in star_handlers_registry:`）就会炸。

    ⚠️ 我原来只有 `all_handlers()`，**那是自造方法名**，真实现里没有 ——
       插件的代码不会去调它。**"我提供了等价能力"不等于"接口面一致"。**
    """
    from astrbot_host.registry import StarHandlerRegistry

    reg = StarHandlerRegistry()
    assert hasattr(reg, "__iter__"), "注册表必须可迭代（真实现有 __iter__）"
    assert hasattr(reg, "__len__"), "注册表必须有 __len__（真实现有）"

    # 空表也要能迭代/取长度，不能抛
    assert list(reg) == []
    assert len(reg) == 0


def test_registry_get_handlers_by_module_name():
    """真实现有 `get_handlers_by_module_name`（`star_handler.py:191-199`）。

    语义：**精确匹配** `handler_module_path`（不是前缀匹配）——
    注意别和我们自己的 `remove_plugin`（前缀匹配，用于卸载）混了。
    """
    from astrbot_host.registry import EventType, StarHandlerMetadata, StarHandlerRegistry

    reg = StarHandlerRegistry()

    async def h_a(event):
        return None

    async def h_b(event):
        return None

    for fn, mod in ((h_a, "pkg_a.main"), (h_b, "pkg_b.main")):
        reg.append(
            StarHandlerMetadata(
                event_type=EventType.AdapterMessageEvent,
                handler_full_name=f"{mod}_{fn.__name__}",
                handler_name=fn.__name__,
                handler_module_path=mod,
                handler=fn,
            )
        )

    got = reg.get_handlers_by_module_name("pkg_a.main")
    assert [h.handler_name for h in got] == ["h_a"], "应按模块名精确过滤"
    assert reg.get_handlers_by_module_name("nope") == []


# ---------------------------------------------------------------------------
# BUG-6：handler 没绑定插件实例 → 派发时 self 会被当成 event
# ---------------------------------------------------------------------------
def test_handlers_are_bound_to_plugin_instance(tmp_path, monkeypatch):
    """插件 handler 必须被**绑定到插件实例**后再进注册表。

    🔴 真实现（`star_manager.py:1260-1271`）在加载完插件实例后做：
    ```python
    related_handlers = star_handlers_registry.get_handlers_by_module_name(metadata.module_path)
    for handler in related_handlers:
        raw_handler = (handler.handler.func
                       if isinstance(handler.handler, functools.partial)
                       else handler.handler)
        handler.handler = raw_handler
        if not plugin_disabled and metadata.star_cls is not None:
            handler.handler = functools.partial(raw_handler, metadata.star_cls)
    ```
    ⚠️ `metadata.star_cls` 是**实例**（`star_cls_type` 才是类，见 `star.py:37/42`
       与 `star_manager.py:1222` 的 `star_cls_type(context=...)`）。

    **为什么这是 P0-2 的拦路虎**：插件方法签名是 `async def cmd(self, event)`。
    注册时拿到的是**未绑定的裸函数**（类体里 `__name__` 取到的是 function），
    所以派发时若直接 `await handler.handler(event)`，
    **`event` 会被当成 `self`**，而真正的 event 参数**缺参** →
    每个指令都 `TypeError`。实测确认：我们注册表里 **203 个 handler 全是裸函数**
    （`functools.partial` 计数 = 0）。

    ⚠️ 顺带解释了服务日志里的那条：
       `已修复热更新残留回调绑定: handlers=109`
       插件**自己**也会做这个 rebind（`main.py:6123`），这正是它在补我们这个缺口 ——
       但那只是**陪伴插件自己的兜底**，别的插件（记忆/生图/内容）没有这段代码。
    """
    import functools

    pkg = tmp_path / "bind_probe_plugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "metadata.yaml").write_text(
        "name: bind_probe\nversion: 1.0\nauthor: t\ndesc: t\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text(
        "from astrbot.api.event import AstrMessageEvent, filter\n"
        "from astrbot.api.star import Context, Star, register\n"
        "\n"
        "@register('bind_probe', 't', 'probe', '1.0')\n"
        "class BindProbe(Star):\n"
        "    def __init__(self, context: Context):\n"
        "        super().__init__(context)\n"
        "        self.marker = 'instance-marker'\n"
        "\n"
        "    @filter.command('bindprobe')\n"
        "    async def bindprobe_cmd(self, event: AstrMessageEvent):\n"
        "        return self.marker\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    import sys as _sys
    for k in [k for k in list(_sys.modules) if k.startswith("bind_probe_plugin")]:
        del _sys.modules[k]

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.registry import EventType, star_handlers_registry
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    star_handlers_registry.clear()

    import asyncio
    import tempfile

    with tempfile.TemporaryDirectory() as mirror:
        loader = PluginLoader(plugin_dirs=[str(tmp_path)], mirror_root=mirror)
        asyncio.run(loader.load_all(ports=HostPorts(config={})))

    hs = [
        h
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
        if h.handler_name == "bindprobe_cmd"
    ]
    assert hs, "探针 handler 没注册上"
    h = hs[0]

    assert isinstance(h.handler, functools.partial), (
        "handler 必须是 `functools.partial(raw_handler, 插件实例)`（照 star_manager.py:1268）。"
        f"实际是 {type(h.handler).__name__} —— 这样派发时 event 会被当成 self，每个指令都 TypeError"
    )
    # 第一个位置参数应当就是插件实例
    assert getattr(h.handler.args[0], "marker", None) == "instance-marker", (
        "partial 绑定的第一个参数应当是插件**实例**（不是类）"
    )
