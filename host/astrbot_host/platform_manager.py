# -*- coding: utf-8 -*-
"""平台管理器 + 「我们的平台」实例（P1-2 / P1-3）。

⭐⭐ **为什么需要这个**：陪伴插件的主动消息走这条链
（`data/plugin_src/astrbot_plugin_private_companion/proactive_chat_runtime_bridge.py:534-575`）：

```python
manager = instance.context.platform_manager
getter = getattr(manager, "get_insts", None)
platforms = list(getter() or []) if callable(getter) else list(getattr(manager, "platform_insts", []) or [])
target_platform = next(
    (p for p in platforms if str(getattr(p.meta(), "id", "") or "") == str(platform_id)),
    None,
)
if target_platform is None:                 # → 兜底 context.send_message
    ...
if getattr(target_platform, "status", PlatformStatus.RUNNING) != PlatformStatus.RUNNING:
    return {"sent": False, "reason": "platform_not_running", ...}
await target_platform.send_by_session(session, chain)
```

→ 我们**必须提供**：`platform_insts` **和** `get_insts()`（两条分支分别用它们）、
`meta().id` 能按 umo 第一段找到、`status` 为 RUNNING、`send_by_session` 可调用。

🔴 **两条铁律**：
  1. **绝不冒充 `aiocqhttp`** —— `meta().name` 用 `"webchat"`，
     插件 `platform_compat.py:29`/`:102` 把 webchat 当**一等公民**
     （`delivery_policy: "normal"`、能力 `**_BASE_CAPABILITIES`、`limits: []`）。
  2. **绝不伪造"已送达"** —— 端口没接就**如实失败**，让插件走它自带的降级路径。
     作者验收判据③：「能力不对等要如实 degraded/uncertain，**不伪造 delivered**」。

⚠️ 与真实现的差异（**有意**，且如实记录）：
   真 `PlatformManager` 管理**多个**平台适配器实例（QQ/Telegram/…）；
   我们宿主是**单平台**（就是本服务自己的 Web 端），所以列表里只有一个实例。
   这不是"缺能力"，而是我们的能力面本来就如此 —— 插件按 id 查找的写法照样工作。
"""
from __future__ import annotations

import logging
from typing import Any

from astrbot_host.compat.core.platform.platform import Platform, PlatformStatus
from astrbot_host.compat.core.platform.platform_metadata import PlatformMetadata

logger = logging.getLogger("astrbot")

# 🔴 **平台 id / 类型名都从 `umo.py` 取，这里不重新定义**。
#    判据 `test_umo_format_is_defined_in_exactly_one_place` 会扫描本目录，
#    发现"第二处 umo 字面量"就报错 —— 它当场抓到了我第一版在这里
#    硬编码 `"11"` 的写法，**这个判据是有用的**：
#    格式一旦有第二个定义源，将来改一处漏一处，表现是
#    "部分功能写进了孤儿会话"（**静默**数据分裂，最贵的一类 bug）。
from astrbot_host.platforms import (
    DEFAULT_PLATFORM,
    DEFAULT_PLATFORM_ID,
    DEFAULT_PLATFORM_NAME,
    PlatformRegistry,
    PlatformSpec,
)
from astrbot_host.umo import build_platform_metadata

#: 平台实例 id —— 与 umo 第一段**同源**（同一份定义，不可能对不上）。
__all_platform_id__ = DEFAULT_PLATFORM_ID

#: 平台类型名 —— 同上。
__all_platform_name__ = DEFAULT_PLATFORM_NAME


class OurPlatform(Platform):
    """一个平台实例 —— 对插件呈现为正常的 AstrBot 平台适配器。

    关键行为：

    * `meta()` → `PlatformMetadata(name=…, id=…, description=…)`，
      **三个字段全部来自 `PlatformSpec`**（`platforms.py`，唯一事实源）
      （`support_proactive_message=True`：我们**确实**要支持主动消息，
        这正是插件的核心机制；但这要求投递端口真的接通 —— 见 `send_by_session`）
    * `status` → `RUNNING`（插件会检查，非 RUNNING 直接拒绝发送）
    * `send_by_session(session, chain)` → **经端口投递**；端口没接就返回 False

    🔴 **为什么 `description` 不再有默认值**（2026-09-21 修）：
       原来这里是 `description="情绪app 内置 Web 端（非 OneBot）"` ——
       那串里含 **`onebot`** 子串，而插件 `platform_compat.py:165-182`
       解析平台类型时会拿 `description` 做**子串兜底** ⇒
       **否定句被当成断言**，这个平台会被判成 `onebot`。
       一旦被判成 onebot，插件会：
         * 去 `int(user_id)`（`proactive_message.py:9663`）→
           非数字 id（我们的会话 id 可以是任意串）**直接崩**；
         * 走 OneBot 原生兜底 → `RuntimeError`（`:17701-17709`）**硬失败**。
       现在之所以没出事，**只因为 `name="webchat"` 先被检查到了** ——
       而"先被检查到"不是保证（改 `name` 就失效），所以必须从源头去掉。
       ✅ 校验已下沉到 `PlatformSpec`：**注册时就拒绝**这种 description。
    """

    def __init__(
        self,
        *,
        spec: PlatformSpec | None = None,
        platform_id: str = "",
        platform_name: str = "",
        delivery: Any = None,
        description: str = "",
    ) -> None:
        """造一个平台实例。

        Args:
            spec: 平台规格（**推荐**）。给了它就忽略下面三个覆盖参数。
            platform_id / platform_name / description: **向后兼容**的旧签名。
                ⚠️ 传了它们会现造一个 `PlatformSpec` —— 于是
                **同样过那三条校验**（含"description 不许含平台别名子串"）。
                所以旧的 `description="…（非 OneBot）"` 现在会**当场抛错**
                而不是静默误判 —— 这正是我们要的（响亮 > 静默）。
        """
        super().__init__(config=None)
        if spec is None:
            spec = (
                PlatformSpec(
                    id=platform_id or DEFAULT_PLATFORM_ID,
                    name=platform_name or DEFAULT_PLATFORM_NAME,
                    description=description,
                )
                if (platform_id or platform_name or description)
                else DEFAULT_PLATFORM
            )
        self._spec = spec
        self._platform_id = spec.id
        self._platform_name = spec.name
        #: 投递端口（未注入 = 不可用 → 如实失败，不伪造送达）
        self.delivery = delivery
        # 🔴 元数据**唯一来源**：`umo.build_platform_metadata(spec=…)`。
        #    不在这里自己造 `PlatformMetadata(...)` —— 那样"平台三件套"
        #    就有了第二个定义处，`id` 与 umo 第一段漂移时表现为
        #    "能构造事件但发不出去"（本项目踩过同类的静默 bug）。
        self._metadata = build_platform_metadata(spec=spec)
        #: 主动消息：我们**要**支持（插件的主动消息机制依赖它），
        #: 但"支持"意味着端口接通后真的能送达 —— 没接通就会如实失败。
        self._metadata.support_proactive_message = True
        self._metadata.support_streaming_message = True
        #: 状态必须真的是 RUNNING —— 插件按它决定要不要发
        #: （`proactive_chat_runtime_bridge.py:560`）。
        self.set_status(PlatformStatus.RUNNING)

    # ---- 真实现接口 ----

    def meta(self) -> PlatformMetadata:
        return self._metadata

    async def send_by_session(self, session: Any, message_chain: Any) -> Any:
        """按会话投递消息链（插件主动消息的落点）。

        🔴 **返回语义（照真实现 + 作者判据③）**：
           真实现里 `send_by_session` **无返回值**（None = 成功）；
           插件判断失败靠"**抛异常**"或"返回 `False`"
           （`proactive_message.py:17624` `if precise_result is not False:`）。
           → 所以我们：
             * 投递成功 → 返回 `True`（不是 None —— 显式比隐含好，
               且 `is not False` 判定下 True/None 都算成功，兼容两种写法）；
             * **端口未注入** → 返回 `False`（**如实失败**，绝不假装送达）；
             * 端口抛异常 → **不让它穿出去**，记 ERROR 后返回 `False`，
               让插件走它的降级路径（它本来就有兜底）。

        ⚠️ 为什么不把异常直接抛给插件：插件对两种失败的处理不同 ——
           抛异常会走 `except` 分支（记 `platform_send_exception`），
           返回 False 会走"回退核心发送"分支。
           两种都是**如实失败**，但返回 False 能触发它**再试一条路**
           （`context.send_message`），对我们更有利。
        """
        if self.delivery is None:
            logger.warning(
                "平台 %s 的投递端口未注入 → 无法发送（如实返回 False，不伪造送达）。"
                "插件将回退到 context.send_message",
                self._platform_id,
            )
            return False

        umo = _session_to_umo(session)
        try:
            await self.delivery.send(umo, message_chain)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "平台 %s 投递失败（umo=%s）：%s: %s",
                self._platform_id, umo, type(exc).__name__, exc,
                exc_info=True,
            )
            return False
        return True

    # ---- 诊断 ----

    def describe(self) -> dict[str, Any]:
        return {
            "id": self._platform_id,
            "name": self._platform_name,
            "status": getattr(self.status, "value", str(self.status)),
            "delivery_wired": self.delivery is not None,
        }


def _session_to_umo(session: Any) -> str:
    """从 `MessageSession` 或字符串取 umo（真实现两者都接受）。"""
    if isinstance(session, str):
        return session
    try:
        return str(session)
    except Exception:  # noqa: BLE001
        return ""


class OurPlatformManager:
    """单平台管理器（对插件呈现为 `context.platform_manager`）。

    🔴 **`platform_insts` 与 `get_insts()` 都必须有**（实测确认）：
       插件不同调用点分别用这两条 ——
       `proactive_chat_runtime_bridge.py:546-547` 是
       `getattr(manager,"get_insts",None)` 优先、`platform_insts` 兜底；
       而 `core_store.py:5629`、`platform_compat.py:214` 直接读 `platform_insts`。
       **只给一个会让走另一条分支的调用点失效。**

    ⚠️ 真实现的 `get_insts()` 就是 `return self.platform_insts`（`manager.py:316-317`），
       我们照抄这个语义（返回**同一个列表对象**，不是副本 ——
       真实现如此，插件可能就地增删）。
    """

    def __init__(self, insts: list[Any] | None = None) -> None:
        self.platform_insts: list[Any] = list(insts or [])

    def get_insts(self) -> list[Any]:
        """照真实现（`manager.py:316-317`）：返回**内部列表本身**。"""
        return self.platform_insts

    def get_inst(self, platform_id: str) -> Any | None:
        """按 id 取实例（真实现有 `_inst_map`，语义等价）。

        ⚠️ 匹配 `id` **和** `name` —— 插件的 `platform_compat.py:220-224`
           就是这么比的（两个都比），照抄以免行为分叉。
        """
        for inst in self.platform_insts:
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001
                continue
            if platform_id in {
                str(getattr(meta, "id", "") or ""),
                str(getattr(meta, "name", "") or ""),
            }:
                return inst
        return None

    def find_by_umo(self, umo: str) -> Any | None:
        """按 umo 第一段找平台（插件查找逻辑的等价物，便于宿主自测）。"""
        prefix = str(umo or "").split(":", 1)[0]
        return self.get_inst(prefix) if prefix else None

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for inst in self.platform_insts:
            fn = getattr(inst, "describe", None)
            out.append(fn() if callable(fn) else {"id": getattr(inst, "meta", lambda: None)()})
        return out


def build_platform_manager(
    *,
    registry: PlatformRegistry | None = None,
    platform_id: str = "",
    platform_name: str = "",
    delivery: Any = None,
) -> OurPlatformManager:
    """造一个已装配好的管理器（**含注册表里的每一个平台实例**）。

    宿主装配层用这个函数；测试也可以直接用（`delivery=None` 时
    实例会**如实拒绝发送**，这正是"不伪造送达"的判据）。

    Args:
        registry: 平台注册表（**推荐**）。`None` = 只含默认平台
            （行为与以前**逐字一致**）。
        platform_id / platform_name: **向后兼容**的旧签名（只造一个平台）。
            ⚠️ 两个都留空且没给 registry 时 → 用默认平台。
        delivery: 投递端口（**所有**平台实例共用同一个端口 ——
            投递按 umo 分发，umo 第一段决定落到哪个平台的用户数据）。

    Returns:
        含 N 个平台实例的管理器；`get_insts()` / `platform_insts` 都能拿到
        （插件两条分支分别用它们，见 `OurPlatformManager` 的 docstring）。
    """
    specs: list[PlatformSpec]
    if registry is not None:
        specs = registry.all()
    elif platform_id or platform_name:
        specs = [
            PlatformSpec(
                id=platform_id or DEFAULT_PLATFORM_ID,
                name=platform_name or DEFAULT_PLATFORM_NAME,
            )
        ]
    else:
        specs = [DEFAULT_PLATFORM]
    return OurPlatformManager(
        [OurPlatform(spec=spec, delivery=delivery) for spec in specs]
    )


__all__ = [
    "DEFAULT_PLATFORM_ID",
    "DEFAULT_PLATFORM_NAME",
    "OurPlatform",
    "OurPlatformManager",
    "build_platform_manager",
]
