# -*- coding: utf-8 -*-
"""差分验证：把兼容层与**真 AstrBot** 放在同一进程里逐项对比。

⚠️ 只在验证时用（真 AstrBot 在 `uv tools` 里，生产宿主**不能**依赖它）：
   靠 `sys.path` 手工把两个实现分别 import 成不同名字，避免 sys.modules 冲突。

对比项：
  ① 枚举成员名/值/位序 逐字相等（permission / platform_adapter_type / message_type）
  ② MessageSession 的 umo 往返（含 session_id 带冒号）
  ③ 路径函数：同一 ASTRBOT_ROOT 下结果相等
  ④ request_retry 常量与重试次数/等待曲线
  ⑤ AstrBotConfig：同一份配置文件下的最终 dict、BOM 写盘、点号访问
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

REAL_ROOT = "/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages"
HOST_ROOT = "/root/Ai-works/情绪app/host"

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


def load_real(dotted: str):
    """普通 import 真 AstrBot 模块。

    ⚠️ 真包名 `astrbot` 与我们影子包的实际包名 `astrbot_host.compat`
       **不冲突**（只有调 `install_shadow_package()` 才会把后者注册成前者），
       所以两个实现可以在同一进程里共存、直接对拍。
    """
    return importlib.import_module(dotted)


print("=" * 72)
print("① 枚举逐字对比（成员名 + 值）")
print("=" * 72)

# ---- PermissionType：真实现只依赖 HandlerFilter，可直接按文件加载 ----
real_perm = load_real("astrbot.core.star.filter.permission")
from astrbot_host.compat.core.star.filter import permission as host_perm

real_names = [m.name for m in real_perm.PermissionType]
host_names = [m.name for m in host_perm.PermissionType]
check("PermissionType 成员名", real_names == host_names, f"{real_names} vs {host_names}")
import enum as _enum

check(
    "PermissionType 是 Flag（不是 Enum）",
    issubclass(host_perm.PermissionType, _enum.Flag)
    and issubclass(real_perm.PermissionType, _enum.Flag),
    f"{host_perm.PermissionType.__mro__} / {real_perm.PermissionType.__mro__}",
)
check(
    "PermissionType 位值逐项相等",
    [m.value for m in real_perm.PermissionType] == [m.value for m in host_perm.PermissionType],
    f"{[m.value for m in real_perm.PermissionType]} vs {[m.value for m in host_perm.PermissionType]}",
)
check(
    "PermissionType 可组合（ADMIN|MEMBER 语义）",
    (host_perm.PermissionType.ADMIN | host_perm.PermissionType.MEMBER).value
    == (real_perm.PermissionType.ADMIN | real_perm.PermissionType.MEMBER).value,
    "组合值不一致",
)
check(
    "PermissionTypeFilter 签名默认值",
    host_perm.PermissionTypeFilter.__init__.__defaults__
    == real_perm.PermissionTypeFilter.__init__.__defaults__,
    f"{host_perm.PermissionTypeFilter.__init__.__defaults__} vs "
    f"{real_perm.PermissionTypeFilter.__init__.__defaults__}",
)

# ---- PlatformAdapterType ----
real_plat = load_real("astrbot.core.star.filter.platform_adapter_type")
from astrbot_host.compat.core.star.filter import platform_adapter_type as host_plat

rn = [m.name for m in real_plat.PlatformAdapterType]
hn = [m.name for m in host_plat.PlatformAdapterType]
check("PlatformAdapterType 成员名+顺序", rn == hn, f"{rn} vs {hn}")
check(
    "PlatformAdapterType 位值逐项相等",
    [m.value for m in real_plat.PlatformAdapterType]
    == [m.value for m in host_plat.PlatformAdapterType],
    "位序不一致会让组合值语义漂移",
)
check(
    "ADAPTER_NAME_2_TYPE 键集合",
    set(real_plat.ADAPTER_NAME_2_TYPE) == set(host_plat.ADAPTER_NAME_2_TYPE),
    f"差异={set(real_plat.ADAPTER_NAME_2_TYPE) ^ set(host_plat.ADAPTER_NAME_2_TYPE)}",
)
check(
    "ADAPTER_NAME_2_TYPE 值逐项相等",
    {k: v.name for k, v in real_plat.ADAPTER_NAME_2_TYPE.items()}
    == {k: v.name for k, v in host_plat.ADAPTER_NAME_2_TYPE.items()},
    "映射值不一致",
)

# ---- MessageType ----
real_mt = load_real("astrbot.core.platform.message_type")
from astrbot_host.compat.core.platform import message_type as host_mt

check(
    "MessageType 成员名+值",
    {m.name: m.value for m in real_mt.MessageType}
    == {m.name: m.value for m in host_mt.MessageType},
    f"{[(m.name, m.value) for m in real_mt.MessageType]} vs "
    f"{[(m.name, m.value) for m in host_mt.MessageType]}",
)

print()
print("=" * 72)
print("② MessageSession 行为对比（umo 往返）")
print("=" * 72)

real_ms = load_real("astrbot.core.platform.message_session")
_RealMT = load_real("astrbot.core.platform.message_type").MessageType
from astrbot_host.compat.core.platform.message_session import (
    MessageSession as HostMS,
    MessageSesion as HostMSTypo,
)

for platform_name, mt_name, session_id in [
    ("private_companion", "FRIEND_MESSAGE", "user123"),
    ("aiocqhttp", "GROUP_MESSAGE", "group_999"),
    ("x", "OTHER_MESSAGE", "a:b:c"),          # session_id 自带冒号
    ("wechat", "FRIEND_MESSAGE", ""),         # 空 session_id
]:
    r = real_ms.MessageSession(
        platform_name, _RealMT[mt_name], session_id
    )
    h = HostMS(platform_name, host_mt.MessageType[mt_name], session_id)
    check(f"__str__ 相等 [{session_id!r}]", str(r) == str(h), f"{r} vs {h}")
    check(
        f"from_str 往返 [{session_id!r}]",
        str(HostMS.from_str(str(r))) == str(r),
        f"{HostMS.from_str(str(r))} vs {r}",
    )
    check(
        f"platform_id 自动回填 [{session_id!r}]",
        h.platform_id == platform_name and r.platform_id == platform_name,
        f"{h.platform_id} vs {r.platform_id}",
    )

check("向后兼容别名 MessageSesion 存在", HostMSTypo is HostMS, "别名指向不一致")
check(
    "MessageSession 只收 3 个位置参数（照真实现 field(init=False)）",
    len(HostMS.__dataclass_fields__) == 4
    and HostMS.__dataclass_fields__["platform_id"].init is False,
    "platform_id 变成可传参会破坏 from_str",
)
try:
    HostMS("a", host_mt.MessageType.FRIEND_MESSAGE, "s", "extra")
    check("第 4 个参数应当被拒绝", False, "竟然接受了 platform_id")
except TypeError:
    check("第 4 个参数被拒绝", True)

print()
print("=" * 72)
print("③ astrbot_path 路径函数对比（同一 ASTRBOT_ROOT）")
print("=" * 72)

from astrbot_host.compat.core.utils import astrbot_path as host_path

with tempfile.TemporaryDirectory() as tmp:
    os.environ["ASTRBOT_ROOT"] = tmp
    real_path = load_real("astrbot.core.utils.astrbot_path")
    os.environ["ASTRBOT_ROOT"] = tmp  # 真模块 import 时读一次，这里再确认

    for fn in [
        "get_astrbot_root",
        "get_astrbot_data_path",
        "get_astrbot_config_path",
        "get_astrbot_plugin_path",
        "get_astrbot_plugin_data_path",
        "get_astrbot_t2i_templates_path",
        "get_astrbot_webchat_path",
        "get_astrbot_temp_path",
        "get_astrbot_skills_path",
        "get_astrbot_workspaces_path",
        "get_astrbot_system_tmp_path",
        "get_astrbot_site_packages_path",
        "get_astrbot_knowledge_base_path",
        "get_astrbot_backups_path",
    ]:
        rv = getattr(real_path, fn)()
        hv = getattr(host_path, fn)()
        check(f"{fn}() 相等", rv == hv, f"{rv} vs {hv}")

    # 函数集合必须与真实现一致（少一个 = 插件 import 失败）
    check(
        "公开函数集合与真实现一致",
        {n for n in dir(real_path) if n.startswith("get_astrbot")}
        == {n for n in dir(host_path) if n.startswith("get_astrbot")},
        f"差异={set(n for n in dir(real_path) if n.startswith('get_astrbot')) ^ set(n for n in dir(host_path) if n.startswith('get_astrbot'))}",
    )
os.environ.pop("ASTRBOT_ROOT", None)

print()
print("=" * 72)
print("④ request_retry：常量 + 重试次数 + 等待曲线")
print("=" * 72)

real_rr = load_real("astrbot.core.provider.sources.request_retry")
from astrbot_host.compat.core.provider.sources import request_retry as host_rr

for const in [
    "REQUEST_RETRY_ATTEMPTS",
    "REQUEST_RETRY_WAIT_MIN_S",
    "REQUEST_RETRY_WAIT_MAX_S",
    "REQUEST_RETRY_STATUS_CODES",
]:
    check(
        f"{const} 相等",
        getattr(real_rr, const) == getattr(host_rr, const),
        f"{getattr(real_rr, const)} vs {getattr(host_rr, const)}",
    )

# 可重试判定逐项对比（用真实现的私有函数做 oracle）
class FakeResp:
    def __init__(self, code):
        self.status_code = code


class FakeErr(Exception):
    def __init__(self, code=None, **kw):
        super().__init__(f"err{code}")
        if code is not None:
            self.status_code = code
        for k, v in kw.items():
            setattr(self, k, v)


cases = [
    ("无状态码普通异常", FakeErr(), True, False),
    ("408", FakeErr(408), True, False),
    ("409", FakeErr(409), True, False),
    ("429 + retry_rate_limits=True", FakeErr(429), True, False),
    ("429 + retry_rate_limits=False", FakeErr(429), False, True),
    ("500", FakeErr(500), True, False),
    ("502", FakeErr(502), True, False),
    ("529", FakeErr(529), True, False),
    ("550(5xx)", FakeErr(550), True, False),
    ("599(5xx)", FakeErr(599), True, False),
    ("600(非5xx)", FakeErr(600), False, True),
    ("400", FakeErr(400), False, True),
    ("404", FakeErr(404), False, True),
    ("response.status_code=503", FakeErr(response=FakeResp(503)), True, False),
    ("response.status_code=400", FakeErr(response=FakeResp(400)), False, True),
    ("TimeoutError", TimeoutError("t"), True, False),
]

for label, err, expect_r, expect_h in cases:
    rr = real_rr._is_retryable_provider_request_error(err, retry_rate_limits=True)
    hh = host_rr._is_retryable_provider_request_error(err, retry_rate_limits=True)
    check(f"可重试判定[{label}] 与真实现一致", rr == hh, f"真={rr} 兼容层={hh}")

for label, err, expect_r, expect_h in [
    ("429 + retry_rate_limits=False", FakeErr(429), False, True),
]:
    rr = real_rr._is_retryable_provider_request_error(err, retry_rate_limits=False)
    hh = host_rr._is_retryable_provider_request_error(err, retry_rate_limits=False)
    check(f"可重试判定[{label}]", rr == hh == expect_r, f"真={rr} 兼容层={hh}")

# 重试次数与等待曲线：真实现用 tenacity，这里直接跑它的 AsyncRetrying 等比
import asyncio
import tenacity  # noqa: E402  仅在真 AstrBot 环境里存在


def real_waits(max_attempts: int) -> list[float]:
    retrying = tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception(lambda e: True),
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential(multiplier=1, min=0.2, max=30),
        reraise=True,
    )
    return [
        retrying.wait(retrying.wait.__self__ if False else _st(attempt))  # type: ignore[attr-defined]
        for attempt in _attempts(max_attempts)
    ]


def _st(attempt_number: int):
    st = tenacity.RetryCallState(None, None, None, None)
    st.attempt_number = attempt_number
    return st


def _attempts(n: int):
    return list(range(1, n))


# 真 tenacity 的 wait 需要 RetryCallState；上面的写法兼容性不稳，
# 改用**实际重试计数**做 oracle（更贴近真行为）。
async def real_attempt_count(max_attempts: int) -> int:
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise FakeErr(503)

    try:
        await real_rr.retry_provider_request(
            "oracle", boom, max_attempts=max_attempts
        )
    except FakeErr:
        pass
    return calls


async def host_attempt_count(max_attempts: int) -> int:
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise FakeErr(503)

    try:
        await host_rr.retry_provider_request(
            "host", boom, max_attempts=max_attempts
        )
    except FakeErr:
        pass
    return calls


async def main_retry():
    for n in (1, 2, 3):
        rc = await real_attempt_count(n)
        hc = await host_attempt_count(n)
        check(f"重试次数[max_attempts={n}] 一致", rc == hc, f"真={rc} 兼容层={hc}")

    # 不可重试的异常：应当**只调一次**且原样抛出
    async def hard():
        raise FakeErr(400)

    for mod, nm in ((real_rr, "真"), (host_rr, "兼容层")):
        calls = 0

        async def boom400():
            nonlocal calls
            calls += 1
            raise FakeErr(400)

        try:
            await mod.retry_provider_request("x", boom400, max_attempts=5)
        except FakeErr:
            pass
        check(f"{nm}实现 400 不重试（调用 1 次）", calls == 1, f"调用 {calls} 次")

    # 成功路径：返回值透传
    async def ok():
        return {"v": 42}

    check(
        "成功路径返回值透传",
        await host_rr.retry_provider_request("x", ok) == {"v": 42},
        "返回值不一致",
    )

    # retry_provider_request_context：异常时 __aexit__ 返回 True → 吞掉
    class CM:
        def __init__(self):
            self.exited = []

        async def __aenter__(self):
            return "resource"

        async def __aexit__(self, *a):
            self.exited.append(a[0])
            return True  # 吞掉异常

    for mod, nm in ((real_rr, "真"), (host_rr, "兼容层")):
        box = {}

        def factory():
            box["cm"] = CM()
            return box["cm"]

        swallowed = True
        try:
            async with mod.retry_provider_request_context("x", factory) as v:
                assert v == "resource", v
                raise FakeErr(400)
        except FakeErr:
            swallowed = False
        check(
            f"{nm}实现 context: __aexit__ 返回 True 时异常被吞",
            swallowed and box["cm"].exited == [FakeErr],
            f"swallowed={swallowed} exited={box['cm'].exited}",
        )


asyncio.run(main_retry())

# 等待曲线：直接把兼容层的公式与 tenacity 的公式对比
for attempt_number in range(1, 8):
    st = _st(attempt_number)
    real_wait = tenacity.wait_exponential(multiplier=1, min=0.2, max=30)(st)
    host_wait = host_rr._wait_exponential_seconds(attempt_number)
    check(
        f"等待曲线[第{attempt_number}次失败后] 一致",
        abs(real_wait - host_wait) < 1e-9,
        f"真={real_wait} 兼容层={host_wait}",
    )

print()
print("=" * 72)
print("⑤ AstrBotConfig：同一配置文件的最终状态对比")
print("=" * 72)

# 真实现的 __init__ 会 import auth_password（同包内相对/绝对都有），
# 用 path 加载时它走 sys.path 里的真 astrbot —— 已在 sys.path[0]。
real_cfg_mod = load_real("astrbot.core.config.astrbot_config")
from astrbot_host.compat.core.config.astrbot_config import AstrBotConfig as HostCfg
from astrbot_host.compat.core.config import astrbot_config as host_cfg_mod

check(
    "DEFAULT_VALUE_MAP 与真实现逐项相等",
    real_cfg_mod.DEFAULT_VALUE_MAP == host_cfg_mod.DEFAULT_VALUE_MAP,
    f"{real_cfg_mod.DEFAULT_VALUE_MAP} vs {host_cfg_mod.DEFAULT_VALUE_MAP}",
)

DEFAULT_UNDER_TEST = {
    "api_key": "",
    "max_tokens": 0,
    "enabled": False,
    "nested": {"a": 1, "b": ""},
    "items": [],
}


def build(mod, cls, path, defaults):
    return cls(config_path=path, default_config=defaults)


for label, mod, cls in (
    ("真实现", real_cfg_mod, real_cfg_mod.AstrBotConfig),
    ("兼容层", host_cfg_mod, HostCfg),
):
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cmd_config.json")
        # 预置一份**缺项 + 乱序**的配置，考验 check_config_integrity
        with open(p, "w", encoding="utf-8-sig") as f:
            json.dump({"nested": {"b": "x"}, "enabled": True, "extra": 1}, f)
        c = build(mod, cls, p, DEFAULT_UNDER_TEST)
        c["api_key"] = "k-123"
        c.save_config()
        with open(p, "rb") as f:
            raw_bytes = f.read()
        # ⚠️ 判 BOM 必须读**原始字节**：用 encoding="utf-8-sig" 读会把 BOM 悄悄吃掉
        check(
            f"{label} 写盘含 BOM（原始字节）",
            raw_bytes.startswith(b"\xef\xbb\xbf"),
            repr(raw_bytes[:6]),
        )
        with open(p, encoding="utf-8-sig") as f:
            data = json.loads(f.read())
        check(
            f"{label} 缺项已补默认值且用户值保留",
            data.get("max_tokens") == 0
            and data.get("items") == []
            and data.get("nested") == {"a": 1, "b": "x"}
            and data.get("api_key") == "k-123",
            str(data),
        )
        check(f"{label} 点号访问生效", c.enabled is True and c.api_key == "k-123", f"{c.enabled}/{c.api_key}")
        check(f"{label} 未知键返回 None", c.definitely_missing is None, repr(c.definitely_missing))
        check(f"{label} first_deploy 标记", getattr(c, "first_deploy", None) is None, "配置文件已存在不该标 first_deploy")

        print(f"  · {label} 最终 dict: {json.dumps(dict(c), ensure_ascii=False, sort_keys=True)[:160]}")

# schema 分支对比
SCHEMA = {
    "s_int": {"type": "int"},
    "s_str": {"type": "string", "default": "d"},
    "s_obj": {"type": "object", "items": {"inner": {"type": "bool"}}},
    "s_list": {"type": "list"},
}
schema_results = {}
for label, mod, cls in (
    ("真实现", real_cfg_mod, real_cfg_mod.AstrBotConfig),
    ("兼容层", host_cfg_mod, HostCfg),
):
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.json")
        c = cls(config_path=p, schema=SCHEMA)
        schema_results[label] = dict(c)
        print(f"  · {label} schema→config: {schema_results[label]}")
check(
    "schema 分支产出的默认配置相等",
    schema_results["真实现"] == schema_results["兼容层"],
    f"{schema_results['真实现']} vs {schema_results['兼容层']}",
)

# 不支持的 schema 类型 → 必须抛 TypeError（照真实现）
for label, mod, cls in (
    ("真实现", real_cfg_mod, real_cfg_mod.AstrBotConfig),
    ("兼容层", host_cfg_mod, HostCfg),
):
    with tempfile.TemporaryDirectory() as tmp:
        try:
            cls(config_path=os.path.join(tmp, "c.json"), schema={"bad": {"type": "zzz"}})
            check(f"{label} 非法 schema 抛 TypeError", False, "没有抛")
        except TypeError:
            check(f"{label} 非法 schema 抛 TypeError", True)

# __delattr__ 不存在键 → AttributeError（照真实现）
for label, mod, cls in (
    ("真实现", real_cfg_mod, real_cfg_mod.AstrBotConfig),
    ("兼容层", host_cfg_mod, HostCfg),
):
    with tempfile.TemporaryDirectory() as tmp:
        c = cls(config_path=os.path.join(tmp, "c.json"), default_config={"a": 1})
        try:
            del c.nope
            check(f"{label} del 不存在的键抛 AttributeError", False, "没有抛")
        except AttributeError:
            check(f"{label} del 不存在的键抛 AttributeError", True)

print()
print("=" * 72)
print(f"结果：{checks - len(failures)}/{checks} 项通过")
if failures:
    print("失败项：")
    for f in failures:
        print("  ❌", f)
    sys.exit(1)
print("✅ 全部与真 AstrBot 行为一致")
