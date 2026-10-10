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


# ---------------------------------------------------------------------------
# BUG-7：`AstrMessageEvent(...)` 只认关键字 → 插件的 4 位置参数构造整链失效
# ---------------------------------------------------------------------------
def _platform_instance():
    """取一个**真平台实例**（`meta()` 返回真 `PlatformMetadata`，不是替身）。

    插件调用点传的第三个参数就是它：`platform.meta()`
    （`_get_platform_for_session` 从 `context.platform_manager` 里按 id 找出来）。
    """
    from astrbot_host.platform_manager import build_platform_manager

    return build_platform_manager().get_insts()[0]


def _plugin_shaped_message_obj(*, message_type, session_id):
    """照 `proactive_message.py:16779-16791` 造一个 `message_obj`。

    ⚠️ 逐字段照抄那一段（`sender` 只给 `user_id`、`nickname` 保持 `None`、
        `message_str` 是空串）—— **别"补全"成更完整的对象**，
        否则就检不出插件真实形状下的推导结果。
    """
    from astrbot_host.compat.core.platform.astrbot_message import (
        AstrBotMessage,
        MessageMember,
    )

    message_obj = AstrBotMessage()
    message_obj.type = message_type
    message_obj.self_id = str(session_id)
    message_obj.session_id = str(session_id)
    message_obj.message_id = "private_companion_proactive_deadbeef"
    message_obj.sender = MessageMember(user_id=message_obj.session_id)
    message_obj.message = []
    message_obj.message_str = ""
    message_obj.raw_message = None
    return message_obj


def test_event_signature_keeps_plugin_positional_order():
    """🔴 前 4 个参数必须是**位置参数**，名字与顺序照真实现。

    真实现 `core/platform/astr_message_event.py:37-42`：
    ```python
    def __init__(self, message_str: str, message_obj: AstrBotMessage,
                 platform_meta: PlatformMetadata, session_id: str) -> None:
    ```

    为什么单列一条：插件**所有**构造点都按位置传（实测 `data/plugin_src` 里 2 处），
    而**我们自己一处都不按位置传**（`umo.py:553` 全是关键字）——
    所以"只跑我们自己的判据"永远看不出这个形状漂移，BUG-7 就是这么漏到真机日志的。

    ⚠️ 把 `*`（keyword-only 分隔符）加回签名里，这条判据立即红。
    """
    import inspect

    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    params = list(inspect.signature(AstrMessageEvent.__init__).parameters.values())[1:]
    head = params[:4]
    assert [p.name for p in head] == [
        "message_str",
        "message_obj",
        "platform_meta",
        "session_id",
    ], f"前 4 个参数的名字/顺序与真实现不一致：{[p.name for p in head]}"
    for p in head:
        assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"`{p.name}` 是 {p.kind} —— 真实现这 4 个是位置参数，"
            "插件按位置传（会 TypeError: takes 1 positional argument but 5 were given）"
        )
    for p in params[4:]:
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"`{p.name}` 不是 keyword-only —— 它是**我们自己的**扩展参数（真实现没有），"
            "允许按位置传入会让位置语义跟着漂移"
        )


def test_plugin_positional_construction_derives_every_field():
    """逐字照抄插件那种 **4 位置参数**构造：不抛错，且推导出的字段全对。

    出处（两处逐字相同）：
      * `data/plugin_src/astrbot_plugin_private_companion/proactive_message.py:16792`
      * `data/plugin_src/astrbot_plugin_image_companion/image_runtime.py:20816`
    ```python
    event = AstrMessageEvent("", message_obj, platform.meta(), message_obj.session_id)
    ```
    真机证据（`logs/serve.out`，**23 次**）：
        `WARNING astrbot: [主动消息] 主动消息主链生成失败:
         AstrMessageEvent.__init__() takes 1 positional argument but 5 were given`
    → 异常被 `proactive_message.py:16821` 的 `except Exception` 接住成一行 debug 日志，
      「主动消息装饰 hooks」整条链**永久不执行**。

    ⚠️ 光"不抛错"不算修好：`unified_msg_origin` 必须真的有值 ——
       装饰链的 `bridge_proactive_chat_outbound`（`main.py:9303`）拿 umo 里
       没有 `":FriendMessage:"` 就**静默 return**，症状与 TypeError 一模一样。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.platform.message_type import MessageType

    platform = _platform_instance()
    meta = platform.meta()
    message_obj = _plugin_shaped_message_obj(
        message_type=MessageType.FRIEND_MESSAGE, session_id="u-1"
    )

    # ↓↓↓ 与插件那一行**逐字同形** ↓↓↓
    event = AstrMessageEvent("", message_obj, platform.meta(), message_obj.session_id)

    assert event.get_message_str() == "", "真实现 `:43` 原样存传入的 message_str"
    assert event.get_sender_id() == "u-1", "应从 `message_obj.sender.user_id` 推导（真 `:205-209`）"
    assert event.get_self_id() == "u-1", "应从 `message_obj.self_id` 推导（真 `:199`）"
    assert event.get_sender_name() == "", "nickname 为 None 时真实现给空串（真 `:211-219`）"
    assert event.get_session_id() == "u-1"
    assert event.is_private_chat() is True, "`FriendMessage` 必须判成私聊（真 `:255`）"
    assert event.get_platform_name() == meta.name == "webchat"
    assert event.get_platform_id() == meta.id == "11"
    assert event.unified_msg_origin == f"{meta.id}:FriendMessage:u-1", (
        "无 umo 入参时必须照真实现 `:75-79` 从 platform_meta.id + 消息类型 + "
        "session_id 造 session（`:106-108` 的 umo 就是 `str(self.session)`）"
    )
    # 真实现 `:47` 设 `platform_meta`、`:103` 留 `platform` 别名（插件 `tts_tool_sanitizer.py:50` 读它）
    assert event.platform_meta is meta
    assert event.platform is event.platform_meta

    # 🔴 回环：插件是"umo → MessageSession → 4 位置参数构造事件"，
    #    装饰链再拿 `event.unified_msg_origin` 去查用户数据
    #    （`main.py:9303` → `_proactive_chat_bridge_user`）—— 必须逐字还原，差一段就查不到。
    from astrbot_host.compat.core.platform.message_session import MessageSession

    session = MessageSession.from_str("11:FriendMessage:sess-abc123def456")
    obj2 = _plugin_shaped_message_obj(
        message_type=session.message_type, session_id=session.session_id
    )
    ev = AstrMessageEvent("", obj2, platform.meta(), obj2.session_id)
    assert ev.unified_msg_origin == "11:FriendMessage:sess-abc123def456"


def test_plugin_positional_construction_group_message_is_not_private():
    """同一形状的**群消息**分支：`is_private` 真的读了 `message_obj.type`。

    照真实现 `:255`，`is_private_chat()` 就是"消息类型是不是 FRIEND"。
    ⚠️ 把推导写成恒 `True`（例如忘了读 `message_obj.type`），这条立即红。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.platform.message_type import MessageType

    platform = _platform_instance()
    message_obj = _plugin_shaped_message_obj(
        message_type=MessageType.GROUP_MESSAGE, session_id="g-1"
    )

    event = AstrMessageEvent("", message_obj, platform.meta(), message_obj.session_id)

    assert event.is_private_chat() is False
    assert event.unified_msg_origin == "11:GroupMessage:g-1"
    assert event.get_session_id() == "g-1"
    assert event.get_sender_id() == "g-1"


def test_is_private_and_session_id_derive_without_session():
    """没有 session（无 umo / 无 platform_meta）时，推导链**照样**要生效。

    ⚠️ 为什么单列一条：给了 `platform_meta` 时 `is_private_chat()` 读的是
       `session.message_type`（真实现 `:255` 也是这么读的）—— **看不到 `_is_private`
       这个推导结果**。而 `is_private_chat()` 插件实测调 72 次：
       关键字构造（如插件自造事件）一旦把群消息恒判成私聊，走的就是另一条分支。
    同理 `session_id` 的推导链也是"传入值 → umo → `message_obj.session_id`"。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.platform.message_type import MessageType

    friend = _plugin_shaped_message_obj(
        message_type=MessageType.FRIEND_MESSAGE, session_id="u-1"
    )
    group = _plugin_shaped_message_obj(
        message_type=MessageType.GROUP_MESSAGE, session_id="g-1"
    )

    assert AstrMessageEvent(message_str="", message_obj=friend).is_private_chat() is True
    assert AstrMessageEvent(message_str="", message_obj=group).is_private_chat() is False, (
        "群消息被判成私聊 —— `is_private` 没有从 `message_obj.type` 推导"
    )
    assert AstrMessageEvent(message_str="", message_obj=group).get_session_id() == "g-1", (
        "没传 session_id/umo 时应退到 `message_obj.session_id`"
    )


def test_explicit_keywords_win_over_derivation():
    """显式关键字**优先**于从 `message_obj` / `platform_meta` 推导。

    为什么必须守：我们自己的派发链路（`umo.py:553-568`）**全部显式传**这些字段，
    且平台三件套必须与 umo 第一段同源 —— 推导若盖掉显式值，
    umo 会写进另一个平台的会话（本项目踩过的"孤儿会话"静默数据分裂）。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.platform.message_type import MessageType

    platform = _platform_instance()
    message_obj = _plugin_shaped_message_obj(
        message_type=MessageType.FRIEND_MESSAGE, session_id="derived-sid"
    )

    # ① 不给 platform_meta：显式值原样生效（`is_private=False` 也不能被 FRIEND 覆盖）
    ev = AstrMessageEvent(
        "显式正文",
        message_obj,
        sender_id="explicit-sender",
        sender_name="显式昵称",
        self_id="explicit-bot",
        platform_name="explicit-platform",
        platform_id="explicit-id",
        is_private=False,
        session_id="explicit-sid",
    )
    assert ev.get_message_str() == "显式正文"
    assert ev.get_sender_id() == "explicit-sender"
    assert ev.get_sender_name() == "显式昵称"
    assert ev.get_platform_name() == "explicit-platform"
    assert ev.get_platform_id() == "explicit-id"
    assert ev.is_private_chat() is False, "显式 is_private=False 被 message_obj.type 盖掉了"
    assert ev.get_session_id() == "explicit-sid", "显式 session_id 被 message_obj.session_id 盖掉了"
    # ⚠️ `self_id` 两个来源的优先级**本来就是真实现那样**（`:199` 现读 message_obj）：
    #    显式值只进缓存字段；`get_self_id()` 仍是 message_obj 优先。这不是"被盖掉"。
    assert ev._self_id == "explicit-bot"
    assert ev.get_self_id() == "derived-sid"

    # ② 给了 platform_meta：显式平台三件套仍优先，且 umo 第一段用**显式** platform_id
    ev2 = AstrMessageEvent(
        "",
        message_obj,
        platform.meta(),
        "explicit-sid",
        platform_name="explicit-platform",
        platform_id="explicit-id",
    )
    assert ev2.get_platform_name() == "explicit-platform"
    assert ev2.get_platform_id() == "explicit-id", "显式 platform_id 被 platform_meta.id 盖掉了"
    assert ev2.unified_msg_origin == "explicit-id:FriendMessage:explicit-sid"


def test_tolerant_keyword_only_construction_unchanged():
    """我们自己的容忍式构造（无 `message_obj` / 无 `platform_meta`）行为**不变**。

    ⚠️ 这是 BUG-7 修复的**安全边界**：真实现 4 个参数全必填，
       "没有 message_obj 的事件"在真 AstrBot 里根本构造不出来 ——
       对它没有"真实现行为"可对齐，所以必须保持原样（不是照真实现改）。
       `host/tests` 里大量判据（含本文件 BUG-2/BUG-3）都走这个形状。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    ev = AstrMessageEvent()

    assert ev.get_platform_name() == "webchat", "没给平台时的默认值不变"
    assert ev.get_platform_id() == "webchat", "空 id 仍退化成平台类型名（今天的行为）"
    assert ev.get_sender_id() == ""
    assert ev.get_sender_name() == ""
    assert ev.get_self_id() == ""
    assert ev.get_session_id() == ""
    assert ev.unified_msg_origin == ""
    assert ev.is_private_chat() is True
    assert getattr(ev, "platform_meta", None) is None, "没给平台就**不设**这个属性（保持 hasattr 语义）"


def _plugin_source_root() -> Path:
    """插件**源码**根目录 —— 从配置读，**不写死**。

    🔴 2026-10-10 修。原来这里硬编码 `data/plugin_src`（那是我早期镜像别人的
       副本目录）。改自建 `plugins/` 之后那目录没了 ⇒ 这条判据**静默 skip**
       （全量从 `1229 passed` 变成 `1229 passed, 4 skipped`）。
       **跳过判据 = 假通过**（本项目红线：`mobile_check.py` 就因此放过过真 bug）。

    ⇒ 现在按**配置**找（`EMOTION_PLUGINS_PLUGIN_DIRS`，分号分隔），
       找不到就**失败而不是跳过** —— 让"判据没跑"这件事本身响起来。
    ⚠️ 只读第一个存在的目录即可（判据扫的是"插件源码长什么样"，
       多来源时内容同源）。
    """
    import os

    raw = os.environ.get("EMOTION_PLUGINS_PLUGIN_DIRS", "")
    cands = [Path(x.strip()) for x in raw.split(";") if x.strip()]
    if not cands:
        # 没设环境变量时按项目默认值找（与 `.env.example` 一致）
        cands = [Path(__file__).resolve().parent.parent.parent / "plugins"]
    for c in cands:
        c = c if c.is_absolute() else Path(__file__).resolve().parent.parent.parent / c
        if c.is_dir():
            return c
    raise AssertionError(
        f"找不到插件源码目录（判据**不能**跳过）：候选={[str(c) for c in cands]}。"
        f" 设 EMOTION_PLUGINS_PLUGIN_DIRS 或确认 plugins/ 存在。"
    )


def test_plugin_sources_construct_event_only_with_supported_shape():
    """扫 `data/plugin_src` 里全部 `AstrMessageEvent(...)` 构造点，形状必须被签名接受。

    为什么要"扫源码"：我们自己的代码**一处都不按位置传**，所以位置形状的漂移
    在单测里永远看不见（BUG-7 就这样漏到真机，被插件的宽 `except` 吞成一行 debug）。
    这条判据把那次扫描固化下来 —— 插件将来换形状（少传/多传参数）当场红。

    ⚠️ 用 `ast` 而不是正则：正则会把文档字符串/注释里的同名片段算进来。
    """
    import ast
    import inspect

    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    src_root = _plugin_source_root()

    sig = inspect.signature(AstrMessageEvent.__init__)
    sites: list[str] = []
    bad: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "AstrMessageEvent(" not in text:
            continue
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (getattr(func, "id", None) or getattr(func, "attr", None)) != "AstrMessageEvent":
                continue
            where = f"{path.relative_to(src_root)}:{node.lineno}"
            sites.append(f"{where}（位置参数 {len(node.args)} 个）")
            try:
                sig.bind(
                    None,
                    *[object()] * len(node.args),
                    **{k.arg: object() for k in node.keywords if k.arg},
                )
            except TypeError as exc:
                bad.append(f"{where}: {exc}")

    assert sites, (
        f"在 {src_root} 里一个 `AstrMessageEvent(` 构造点都没扫到 —— "
        "扫描逻辑失效（零命中不是证据，是判据坏了）"
    )
    assert not bad, "插件的构造形状我们的签名接不住：\n" + "\n".join(bad)
