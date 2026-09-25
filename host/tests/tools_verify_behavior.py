# -*- coding: utf-8 -*-
"""差分验证 ②：CommandGroupFilter / PermissionTypeFilter / PlatformAdapterTypeFilter 的**行为**。

⚠️ 真实现里这些 `filter()` 会读 event 的多个成员（`is_at_or_wake_command` /
   `message_str` / `get_message_str()` / `is_admin()` / `get_platform_name()`），
   所以这里用假 event 把两边**喂同一份输入**再比输出。
"""
from __future__ import annotations

import importlib
import sys

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ✅ {label}")
    else:
        failures.append(f"{label} —— {detail}")
        print(f"  ❌ {label} —— {detail}")


class FakeEvent:
    def __init__(self, message_str="", at_or_wake=True, admin=False, platform="webchat"):
        self.message_str = message_str
        self.is_at_or_wake_command = at_or_wake
        self._admin = admin
        self._platform = platform

    def get_message_str(self):
        return self.message_str

    def is_admin(self):
        return self._admin

    def get_platform_name(self):
        return self._platform

    def set_extra(self, k, v):
        setattr(self, k, v)


CFG = {}  # 两边都传空配置

print("=" * 72)
print("① PermissionTypeFilter 行为（含 ADMIN/MEMBER 语义）")
print("=" * 72)

from astrbot_host.compat.core.star.filter import permission as hp
import astrbot.core.star.filter.permission as rp

for pt_name in ("ADMIN", "MEMBER"):
    for admin in (True, False):
        r = rp.PermissionTypeFilter(getattr(rp.PermissionType, pt_name)).filter(FakeEvent(admin=admin), CFG)
        h = hp.PermissionTypeFilter(getattr(hp.PermissionType, pt_name)).filter(FakeEvent(admin=admin), CFG)
        check(f"{pt_name} + admin={admin} → {r}", r == h, f"真={r} 兼容层={h}")

check(
    "raise_error 默认值一致",
    hp.PermissionTypeFilter.__init__.__defaults__ == rp.PermissionTypeFilter.__init__.__defaults__,
    f"{hp.PermissionTypeFilter.__init__.__defaults__} vs {rp.PermissionTypeFilter.__init__.__defaults__}",
)

print()
print("=" * 72)
print("② PlatformAdapterTypeFilter 行为（字符串/枚举/组合/未知平台）")
print("=" * 72)

from astrbot_host.compat.core.star.filter import platform_adapter_type as hpat
import astrbot.core.star.filter.platform_adapter_type as rpat

cases = []
# 字符串构造
for s in ("webchat", "aiocqhttp", "telegram", "不存在的平台"):
    for plat in ("webchat", "aiocqhttp", "telegram", "wecom"):
        cases.append((f"str({s!r}) vs 事件平台 {plat!r}", s, plat))
# 枚举构造
for name in ("WEBCHAT", "AIOCQHTTP", "ALL"):
    for plat in ("webchat", "aiocqhttp", "telegram"):
        cases.append((f"enum({name}) vs 事件平台 {plat!r}", name, plat))

for label, spec, plat in cases:
    if isinstance(spec, str) and spec in rpat.ADAPTER_NAME_2_TYPE:
        r_arg, h_arg = spec, spec
    elif isinstance(spec, str) and spec == "不存在的平台":
        r_arg, h_arg = spec, spec
    else:
        r_arg = getattr(rpat.PlatformAdapterType, spec)
        h_arg = getattr(hpat.PlatformAdapterType, spec)
    rf = rpat.PlatformAdapterTypeFilter(r_arg)
    hf = hpat.PlatformAdapterTypeFilter(h_arg)
    r = rf.filter(FakeEvent(platform=plat), CFG)
    h = hf.filter(FakeEvent(platform=plat), CFG)
    check(label, r == h, f"真={r} 兼容层={h}")

# 组合值（插件真实写法）
combo_pairs = [
    (("AIOCQHTTP", "TELEGRAM"), "telegram"),
    (("AIOCQHTTP", "TELEGRAM"), "aiocqhttp"),
    (("AIOCQHTTP", "TELEGRAM"), "webchat"),
    (("ALL", "TELEGRAM"), "webchat"),
]
for names, plat in combo_pairs:
    r_arg = rpat.PlatformAdapterType[names[0]]
    h_arg = hpat.PlatformAdapterType[names[0]]
    for n in names[1:]:
        r_arg = r_arg | rpat.PlatformAdapterType[n]
        h_arg = h_arg | hpat.PlatformAdapterType[n]
    r = rpat.PlatformAdapterTypeFilter(r_arg).filter(FakeEvent(platform=plat), CFG)
    h = hpat.PlatformAdapterTypeFilter(h_arg).filter(FakeEvent(platform=plat), CFG)
    check(f"{'+'.join(names)} vs {plat!r} → {r}", r == h, f"真={r} 兼容层={h}")

print()
print("=" * 72)
print("③ CommandGroupFilter 行为（完整指令名 / 树 / 过滤 / 报错）")
print("=" * 72)

from astrbot_host.compat.core.star.filter.command_group import CommandGroupFilter as HostCGF
from astrbot_host.compat.core.star.filter.command import CommandFilter as HostCF
import astrbot.core.star.filter.command_group as rcgroup
import astrbot.core.star.filter.command as rcmd


# ⚠️ 真实现 `CommandFilter.print_cmd_tree` 依赖 `print_types()` / `handler_md` /
#    `handler_params`，这三样由 `init_handler_md()` 在传入 `handler_md` 时建立。
#    本项目 host 侧 `command.py`（**不属本次移植范围**）目前是精简版、没有它们，
#    所以这里给 host 侧一个**照真实现补齐的忠实地替身**，
#    以便对拍「真 CommandFilter 喂进我这支 command_group.py」的树形输出。
#    「host 侧缺方法时两边报错一致」另有一条独立断言。
class FaithfulHostCommandFilter(HostCF):
    """仅测试用：补齐真 `CommandFilter` 的 print_types / handler_md / custom_filter_ok。"""
    handler_md = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.handler_params = {}
        self.custom_filter_list = []

    def print_types(self):
        import types as _t
        import typing as _ty
        parts = []
        for k, v in self.handler_params.items():
            if isinstance(v, type):
                parts.append(f"{k}({v.__name__}),")
            elif isinstance(v, _t.UnionType) or _ty.get_origin(v) is _ty.Union:
                parts.append(f"{k}({v}),")
            else:
                parts.append(f"{k}({type(v).__name__})={v},")
        return "".join(parts).rstrip(",")

    def custom_filter_ok(self, event, cfg):
        for f in getattr(self, "custom_filter_list", []):
            if not f.filter(event, cfg):
                return False
        return True


# 真侧 handler：给它一个有默认值的签名，print_types() 才有内容可比
class _RealHandler:
    def __init__(self, a, b, count: int = 1, name: str = "x"):
        pass

    def __call__(self, *a, **kw):
        pass


from astrbot.core.star.star_handler import (  # noqa: E402
    EventType as RealET,
    StarHandlerMetadata as RealMD,
)

_REAL_MD = RealMD(RealET.AdapterMessageEvent, "m_h", "h", "m", _RealHandler, [])


def build(GroupCls, CmdCls, leaf_kwargs=None):
    """构造同样的两层指令组：陪伴 / 陪伴 记忆 / 陪伴 记忆 查看。"""
    root = GroupCls("陪伴", alias={"私聊陪伴"})
    sub = GroupCls("记忆", parent_group=root)
    root.add_sub_command_filter(sub)
    leaf = CmdCls("查看", **(leaf_kwargs or {}))
    leaf.parent_command_names = root.get_complete_command_names()
    sub.add_sub_command_filter(leaf)
    return root, sub, leaf


rg, rs, rl = build(
    rcgroup.CommandGroupFilter, rcmd.CommandFilter, {"handler_md": _REAL_MD}
)
hg, hs, hl = build(HostCGF, FaithfulHostCommandFilter)
# 两侧 handler_params 都填成同一份，print_types 才可比
rl.handler_params = {"count": 1, "name": "x"}
hl.handler_params = {"count": 1, "name": "x"}

check(
    "根组 get_complete_command_names()",
    rg.get_complete_command_names() == hg.get_complete_command_names(),
    f"{rg.get_complete_command_names()} vs {hg.get_complete_command_names()}",
)
check(
    "子组 get_complete_command_names()",
    rs.get_complete_command_names() == hs.get_complete_command_names(),
    f"{rs.get_complete_command_names()} vs {hs.get_complete_command_names()}",
)
check(
    "根节点两次调用都返回新 list（照真实现：根分支不写缓存）",
    (rg.get_complete_command_names() is rg.get_complete_command_names())
    == (hg.get_complete_command_names() is hg.get_complete_command_names()),
    "缓存语义与真实现不一致",
)
check(
    "非根节点命中缓存（_cmpl_cmd_names 生效）",
    (rs.get_complete_command_names() is rs.get_complete_command_names())
    == (hs.get_complete_command_names() is hs.get_complete_command_names()),
    "缓存语义与真实现不一致",
)

check("startswith 一致", rg.startswith("陪伴 记忆 查看 x") == hg.startswith("陪伴 记忆 查看 x"), "不一致")
check("equals 一致", rg.equals("陪伴") == hg.equals("陪伴"), "不一致")

# filter() 三分支：未唤醒 / 只有组名（抛 ValueError）/ 带子指令
for label, ev in [
    ("未唤醒 → False", FakeEvent("陪伴", at_or_wake=False)),
    ("命中子指令 → True", FakeEvent("陪伴 记忆 查看", at_or_wake=True)),
]:
    r = rg.filter(ev, CFG)
    h = hg.filter(FakeEvent(ev.message_str, ev.is_at_or_wake_command), CFG)
    check(f"filter [{label}]", r == h, f"真={r} 兼容层={h}")

# 只输入组名 → 真实现抛 ValueError（列出子指令树）
r_exc = h_exc = None
try:
    rg.filter(FakeEvent("陪伴"), CFG)
except ValueError as e:
    r_exc = str(e)
try:
    hg.filter(FakeEvent("陪伴"), CFG)
except ValueError as e:
    h_exc = str(e)
check("只输入组名抛 ValueError", r_exc is not None and h_exc is not None, f"真={r_exc} 兼容层={h_exc}")
if r_exc and h_exc:
    check("ValueError 文案逐字相同", r_exc == h_exc, f"\n真:\n{r_exc}\n兼容层:\n{h_exc}")

# 树形打印逐字对比（两侧结构等价，仅 CommandFilter 实现不同）
r_tree = rg.print_cmd_tree(rg.sub_command_filters)
h_tree = hg.print_cmd_tree(hg.sub_command_filters)
check("print_cmd_tree 输出逐字相同", r_tree == h_tree, f"\n真:\n{r_tree!r}\n兼容层:\n{h_tree!r}")
print("  · 树形输出：")
for line in r_tree.splitlines():
    print("     ", line)

print()
print("=" * 72)
print("④ 交叉文件缺口：host 侧 command.py 未补齐 print_types/handler_md 时的表现")
print("=" * 72)

# 真实现：CommandFilter 若没走 init_handler_md 就没 handler_params → AttributeError
r_err = h_err = None
try:
    rg.print_cmd_tree([rcmd.CommandFilter("裸指令")])
except AttributeError as e:
    r_err = str(e)
try:
    hg.print_cmd_tree([HostCF("裸指令")])
except AttributeError as e:
    h_err = str(e)
check(
    "未初始化 handler_params 时，两边报错类型一致（AttributeError）",
    (r_err is not None) == (h_err is not None),
    f"真={r_err!r} 兼容层={h_err!r}",
)
print(f"  · 真实现报错: {r_err!r}")
print(f"  · 兼容层报错: {h_err!r}")

print()
print("=" * 72)
print(f"结果：{checks - len(failures)}/{checks} 项通过")
if failures:
    print("失败项：")
    for f in failures:
        print("  ❌", f)
    sys.exit(1)
print("✅ 全部与真 AstrBot 行为一致")

print()
print("=" * 72)
print("⑤ 指令注册的【副作用】差分：子指令的 handler 到底挂了什么过滤器")
print("=" * 72)
print("  🔴 为什么必须对拍这个：这一项抓到了 BUG-1 —— 我们原来在子指令分支")
print("     提前 return，只把 CommandFilter 挂进父指令组、**没挂到 handler 上**。")
print("     纯读源码容易看漏（真实现的尾部是裸指令/子指令**共用**的），")
print("     而对拍一次就现形。")

import json as _json


def _snapshot(impl: str) -> dict:
    """在指定实现上注册同一组指令，快照每个 handler 的 event_filters。"""
    if impl == "real":
        # ⚠️ `astrbot.api.event` 不会自动带出 `filter` 子模块 → 必须显式 import
        F = importlib.import_module("astrbot.api.event.filter")
        from astrbot.core.star.star_handler import (  # type: ignore
            EventType as REventType, star_handlers_registry as rreg,
        )
        from astrbot.core.star import Star as RStar
        reg, ET, StarBase = rreg, REventType, RStar
    else:
        F = importlib.import_module("astrbot_host.compat.api.event.filter")
        from astrbot_host.compat.core.star.star_handler import (
            EventType as HEventType, star_handlers_registry as hreg,
        )
        from astrbot_host.compat.core.star.star import Star as HStar
        reg, ET, StarBase = hreg, HEventType, HStar

    reg.clear()
    # ⚠️ 每次都要一个**新类名**，否则 get_handler_or_create 按 full_name 幂等
    #    复用上一次的实现留下的 metadata（两边会串台）。
    ns = {"F": F, "StarBase": StarBase}
    code = (
        "class Probe(StarBase):\n"
        "    @F.command_group('grp', alias={'g'})\n"
        "    def grp(self):\n"
        "        pass\n"
        "    @grp.command('sub1')\n"
        "    async def sub1(self, event):\n"
        "        return 'a'\n"
        "    @F.command('bare', alias={'b'})\n"
        "    async def bare(self, event):\n"
        "        return 'c'\n"
    )
    exec(compile(code, "<probe>", "exec"), ns)

    out = {}
    for h in reg.get_handlers_by_event_type(ET.AdapterMessageEvent, only_activated=False):
        fl = []
        for f in h.event_filters:
            g = getattr(f, "get_complete_command_names", None)
            try:
                names = g() if callable(g) else None
            except Exception:
                names = "<err>"
            fl.append(
                {
                    "type": type(f).__name__,
                    "names": names,
                    "parent": getattr(f, "parent_command_names", None),
                }
            )
        out[h.handler_name] = fl
    reg.clear()
    return out


real_reg = _snapshot("real")
ours_reg = _snapshot("ours")

for name in sorted(set(real_reg) | set(ours_reg)):
    r, o = real_reg.get(name), ours_reg.get(name)
    check(
        f"handler `{name}` 的 event_filters 与真实现一致",
        r == o,
        f"真={_json.dumps(r, ensure_ascii=False)} 兼容层={_json.dumps(o, ensure_ascii=False)}",
    )

# 单独把"子指令必须有过滤器"钉成一条**语义**判据（防止两边一起退化成空）
check(
    "真实现的子指令 handler 确实挂着 CommandFilter（BUG-1 的基线）",
    bool(real_reg.get("sub1")),
    f"真实现 sub1 的 filters={real_reg.get('sub1')}",
)

print()
print("=" * 72)
print(f"结果：{checks - len(failures)}/{checks} 项通过")
if failures:
    print("失败项：")
    for f in failures:
        print("  ❌", f)
    sys.exit(1)
print("✅ 全部与真 AstrBot 行为一致")
