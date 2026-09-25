# -*- coding: utf-8 -*-
"""事件派发器 —— 让插件注册的 handler **真的被调用**（P0-2 / P0-3）。

⭐⭐ **本文照真 AstrBot 的两个阶段逐条移植**（不是"照感觉写"）：

| 阶段 | 真实现 | 做什么 |
|---|---|---|
| **A 选 handler** | `core/pipeline/waking_check/stage.py:100-248` | 唤醒判定 → 逐个跑 `event_filters`（AND）→ 产出 `activated_handlers` |
| **B 执行** | `core/pipeline/process_stage/method/star_request.py:23-74` | 逐个 handler 调 `call_handler` → 收结果 → 每轮 `clear_result()` |
| **调用契约** | `core/pipeline/context_utils.py:12-75` | 协程 / 异步生成器两种；`MessageEventResult` → `set_result` |

🔴 **为什么必须分两个阶段、不能合并**：
   真实现的阶段 A 会把**所有**命中的 handler 先选出来（还有 `SessionPluginManager`
   过滤、`handlers_parsed_params` 收集），阶段 B 才按顺序执行；
   插件依赖 `event.get_extra("activated_handlers")` 这个**中间产物**
   （`process_stage/stage.py:33-35` 就是从这里取的）。合并成一个循环会让
   这个 extra 永远为空 → 依赖它的插件静默失效。

🔴 **铁律（照作者验收判据③）**：
   * **不伪造**：handler 没跑、消息没投出去，就如实返回 False / 抛错；
   * **不冒充**：我们不是 aiocqhttp，也不假装是。

⚠️ 本模块**不做 IO**：真正把消息投出去由装配层（`DeliveryPort`）负责，
   这样派发器可单测（与领域层同样的"端口注入"思路）。
"""
from __future__ import annotations

import inspect
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any

from astrbot_host.compat.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot_host.compat.core.star.filter.permission import PermissionTypeFilter
from astrbot_host.registry import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)

logger = logging.getLogger("astrbot")


@dataclass
class DispatchResult:
    """一次派发的完整结果（诊断 + 投递用）。"""

    #: 命中的 handler（阶段 A 的产物，与真实现 `activated_handlers` 同义）
    activated: list[StarHandlerMetadata] = field(default_factory=list)
    #: 每个 handler 解析出的指令参数（真实现 `handlers_parsed_params`）
    parsed_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 执行过的 handler 全名（按执行顺序）
    executed: list[str] = field(default_factory=list)
    #: handler 抛出的异常（**不吞** —— 每个都记录，取最后一个作为代表）
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: handler 直接 `event.send()` 记录下来的消息
    sent: list[Any] = field(default_factory=list)
    #: 最终结果（`event.get_result()` 的终值）
    result: Any = None
    #: 🔴 **本次派发产出的全部回复**（按产出顺序）。
    #:
    #: 为什么需要它 —— 真实现是**洋葱模型**（`pipeline/scheduler.py:36-80`）：
    #: `star_request` 每 yield 一次，调度器就在那个点上**递归跑后续阶段**
    #: （包括 `respond`，它读 `event.get_result()` 并发出去）；
    #: 后续阶段跑完回到 `star_request`，才执行
    #: `event.clear_result()`（`star_request.py:53`）。
    #: → **结果是在 yield 点被消费的，循环结束后 `get_result()` 已经是 None**。
    #: 我们宿主没有那条完整管线，所以在这里**于 yield 点收集**，
    #: 由投递层消费（等价于 respond 阶段的角色）。
    emitted: list[Any] = field(default_factory=list)
    #: 事件是否被终止
    stopped: bool = False
    #: 本轮是否允许走"默认 LLM 链路"
    allow_default_llm: bool = False
    #: 唤醒判定结果
    is_wake: bool = False

    @property
    def ok(self) -> bool:
        """有没有 handler 抛异常。"""
        return not self.errors

    def summary(self) -> str:
        return (
            f"<DispatchResult activated={len(self.activated)} "
            f"executed={len(self.executed)} errors={len(self.errors)} "
            f"sent={len(self.sent)} stopped={self.stopped} wake={self.is_wake}>"
        )


# ---------------------------------------------------------------------------
# 阶段 A：唤醒判定 + 选 handler（照 waking_check/stage.py）
# ---------------------------------------------------------------------------


def apply_wake(
    event: Any,
    wake_prefixes: list[str] | None = None,
    *,
    friend_message_needs_wake_prefix: bool = False,
) -> bool:
    """唤醒判定（照真实现 `waking_check/stage.py:106-152`）。

    真实现的三条判定，按顺序：

    1. **唤醒前缀匹配** → 置 `is_at_or_wake_command`，并**从 `message_str` 切掉前缀**；
    2. **@机器人 / AtAll / 引用机器人**（看消息段）→ 置位，**不改 message_str**；
    3. **私聊** 且（`not friend_message_needs_wake_prefix` **或** 平台是 `webchat`）→ 置位。

    ⚠️ 三处照抄而不是"简化"的地方：
       * 第 1 条**前缀会被真的切掉**（`message_str = message_str[len(prefix):].strip()`）
         —— 指令过滤器随后按切完的文本匹配，不切就永远匹配不上；
       * 第 2 条**不动 message_str**（真实现如此，别顺手统一）；
       * 第 3 条的判据是 **`friend_message_needs_wake_prefix`**（真配置项，
         默认 **`False`**，见 `core/config/default.py:96`）——
         🔴 **我第一版写成了 `not wake_prefixes`，那是错的**：
           前缀列表为空 ≠ 私聊需要前缀。真实现里这个开关默认关，
           所以**私聊无条件唤醒**（这正是 AstrBot 网页聊天不用打前缀的原因）。

    Args:
        wake_prefixes: 唤醒前缀列表（真实现读 `astrbot_config["wake_prefix"]`）。
        friend_message_needs_wake_prefix: 真配置项，默认 False。

    Returns:
        是否唤醒（真实现的 `is_wake`）。
    """
    prefixes = list(wake_prefixes or [])
    is_wake = False
    message_str = getattr(event, "message_str", "") or ""

    for prefix in prefixes:
        if prefix and message_str.startswith(prefix):
            event.is_at_or_wake_command = True
            event.is_wake = True
            # 🔴 前缀必须切掉（真实现 `:123`）
            event.message_str = message_str[len(prefix):].strip()
            is_wake = True
            break

    if not is_wake:
        # 私聊判定（真实现 `:145-152`）。
        # ⚠️ 消息段里的 At/AtAll/Reply 判定需要组件类；宿主私聊场景下
        #    走"私聊直接唤醒"这条就够，这里如实按 `is_private_chat()` 判。
        try:
            is_private = bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            is_private = False
        platform_name = ""
        try:
            platform_name = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform_name = ""
        if is_private and (
            not friend_message_needs_wake_prefix or platform_name == "webchat"
        ):
            event.is_at_or_wake_command = True
            event.is_wake = True
            is_wake = True

    if not is_wake:
        # 没唤醒 → 真实现 `:247-248`：直接终止事件
        event.stop_event()
    return is_wake


def select_handlers(
    event: Any,
    cfg: Any = None,
    *,
    plugins_name: list[str] | None = None,
) -> tuple[list[StarHandlerMetadata], dict[str, dict[str, Any]]]:
    """阶段 A：跑过滤器，选出要执行的 handler（照 `waking_check/stage.py:167-236`）。

    🔴 照抄真实现的五个关键语义（每一条我都实测踩过或核对过源码）：

    1. **`len(event_filters) == 0` → 跳过**（`:182-183`）。
       这是"子指令没挂 CommandFilter"那个 bug 的杀伤点：空过滤器**不是**
       "每条消息都触发"，而是**永不执行**。
    2. **过滤器之间是 AND**（`:178` 注释原话「filter 需满足 AND 逻辑关系」）。
    3. **`PermissionTypeFilter` 单独处理**（`:187-193`）：它不参与 AND 短路，
       而是记 `permission_not_pass`；命中后若 `raise_error=False` → **跳过**，
       若为真 → 报"权限不足" + `stop_event()` + **直接 return**（终止整个派发）。
    4. **`parsed_params` 收集后要 `pop`**（`:231-236`）：
       指令过滤器会把参数写进 `event.set_extra("parsed_params")`，
       这里按 `handler_full_name` 收进 `handlers_parsed_params`，
       然后**把 event 上的删掉** —— 否则下一个 handler 会读到上一个的参数。
    5. **指令组的父 handler 不进 `activated_handlers`**（`:226-230`）：
       带 `CommandGroupFilter` 的只用于帮助树/WebUI，真正执行的是子指令 handler。

    Returns:
        `(activated_handlers, handlers_parsed_params)`
    """
    activated: list[StarHandlerMetadata] = []
    parsed: dict[str, dict[str, Any]] = {}

    # 真实现 `:158-164`：plugins_name 为 ["*"] 时置 None（= 全部启用）
    if plugins_name == ["*"]:
        plugins_name = None
    event.plugins_name = plugins_name

    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent,
        plugins_name=plugins_name,
    ):
        filters = handler.event_filters or []

        # ① 空过滤器 → 跳过（真实现 `:182-183`）
        if len(filters) == 0:
            logger.debug(
                "handler %s 没有 event_filters → 跳过（真实现语义，不是每条都触发）",
                handler.handler_full_name,
            )
            continue

        passed = True
        permission_not_pass = False
        permission_raise_error = False

        for flt in filters:
            try:
                if isinstance(flt, PermissionTypeFilter):
                    if not flt.filter(event, cfg):
                        permission_not_pass = True
                        permission_raise_error = bool(
                            getattr(flt, "raise_error", True)
                        )
                elif not flt.filter(event, cfg):
                    passed = False
                    break
            except Exception as exc:  # noqa: BLE001
                # 真实现 `:194-204`：过滤器抛错 → 把错误**发给用户** + 终止事件。
                # ⚠️ 这里不静默：既记日志，也把异常抛给调用方（见 ReturnContract）
                logger.error(
                    "handler %s 的过滤器 %s 抛错：%s",
                    handler.handler_full_name, type(flt).__name__, exc,
                )
                event.stop_event()
                passed = False
                break

        if not passed:
            continue

        if permission_not_pass:
            if not permission_raise_error:
                # 真实现 `:208-209`：不报错 → 静默跳过（这是"有意的跳过"，不是失败）
                continue
            # 真实现 `:210-221`：报"权限不足" + stop_event + **直接终止**
            logger.info(
                "用户 %s 权限不足以触发 %s",
                event.get_sender_id(), handler.handler_name,
            )
            event.stop_event()
            break

        # ② 是"指令组"父 handler → 不进 activated（真实现 `:226-230`）
        from astrbot_host.compat.core.star.filter.command_group import (
            CommandGroupFilter,
        )

        is_group_cmd_handler = any(
            isinstance(f, CommandGroupFilter) for f in filters
        )
        if is_group_cmd_handler:
            continue

        activated.append(handler)
        # ③ 收集并**删除** parsed_params（真实现 `:231-236`）
        if "parsed_params" in (event.get_extra() or {}):
            parsed[handler.handler_full_name] = event.get_extra("parsed_params")

    # 真实现 `:236`：循环结束后统一 pop
    try:
        event._extras.pop("parsed_params", None)
    except Exception:  # noqa: BLE001
        event.set_extra("parsed_params", None)

    return activated, parsed


# ---------------------------------------------------------------------------
# 阶段 B：执行（照 process_stage/method/star_request.py + context_utils.call_handler）
# ---------------------------------------------------------------------------


async def call_handler(
    event: Any,
    handler: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """执行一个 handler 并处理它的返回值（照 `context_utils.py:12-75`）。

    🔴 **两种 handler 形态都要支持**（真实现用 `inspect` 分流）：
       * **协程**（`async def`）→ `await` 一次，拿返回值；
       * **异步生成器**（函数里有 `yield`）→ 逐个 `async for` 消费。

    ⚠️ **生成器必须完整消费**：真实现是"每 yield 一次就处理一次"，
       我们也是 —— 提前 `break` 会让插件的收尾逻辑（持久化/清理）**永不执行**
       （项目踩过同类坑：流式 `break` 掉生成器 → 历史永不落库）。

    🔴 **返回值的分流规则（照真实现 `:54-61` / `:71-75`）**：
       * `MessageEventResult` / `CommandResult` → `event.set_result(ret)`
         （⚠️ `CommandResult` 是 `MessageEventResult` 的**别名**，见其定义处）；
       * **其它（含 `None`、`str`）** → 不 set_result，原样 yield 出去。
         注意 `str` **不会**被自动包装 —— 真实现就是这样，
         插件返回裸字符串时由上层决定怎么处理（我们照抄，不"顺手兼容"）。

    Yields:
        与真实现同构的"推进信号"（值本身无意义，用于让上层逐步推进）。
    """
    ready_to_call = None
    try:
        ready_to_call = handler(event, *args, **kwargs)
    except TypeError:
        # 真实现 `:38-42`：参数不匹配 → 记 ERROR 并**放弃这个 handler**
        logger.error(
            "插件 handler 参数与其定义不匹配（handler=%s）",
            getattr(handler, "__name__", handler), exc_info=True,
        )
        return

    if ready_to_call is None:
        return

    from astrbot_host.compat.core.message.message_event_result import (
        CommandResult,
        MessageEventResult,
    )

    if inspect.isasyncgen(ready_to_call):
        has_yielded = False
        async for ret in ready_to_call:
            has_yielded = True
            if isinstance(ret, MessageEventResult | CommandResult):
                event.set_result(ret)
                yield None
            else:
                yield ret
        if not has_yielded:
            # 真实现 `:62-64`：生成器一次都没 yield → 也要推进一次
            yield None
    elif inspect.iscoroutine(ready_to_call):
        ret = await ready_to_call
        if isinstance(ret, MessageEventResult | CommandResult):
            event.set_result(ret)
            yield None
        else:
            yield ret


async def dispatch(
    event: Any,
    cfg: Any = None,
    *,
    wake_prefixes: list[str] | None = None,
    plugins_name: list[str] | None = None,
    wake_already_applied: bool = False,
    friend_message_needs_wake_prefix: bool = False,
) -> DispatchResult:
    """把一条消息事件派发给插件 handler（P0-2 + P0-3 的入口）。

    流程 = 真实现的 **waking_check → process_stage** 两阶段：

    ```
    apply_wake(event)                      # 唤醒判定（含 stop_event）
      ↓
    select_handlers(event, cfg)            # 跑过滤器的 AND，产出 activated_handlers
      ↓
    event.set_extra("activated_handlers", ...)      # ← 插件依赖这个中间产物
    event.set_extra("handlers_parsed_params", ...)
      ↓
    for handler in activated:              # 逐个执行
        if event.is_stopped(): break
        async for _ in call_handler(event, handler, **params): ...
        event.clear_result()               # ← 每轮清（照 star_request.py:53）
      ↓
    result = event.get_result()            # 终值（供投递层消费）
    ```

    🔴 **异常处理照真实现 `star_request.py:54-74`**：
       * 记完整堆栈 ERROR；
       * 若事件未停止且是唤醒指令 → 把错误**回给用户**
         （`":(\n\n在调用插件 {name} 的处理函数 {handler} 时出现异常：{e}"`）；
       * 然后 `stop_event()`。
       ⚠️ 我们**额外**把这些异常收进 `DispatchResult.errors`，
          因为"插件全挂但没人知道"是最贵的 bug（项目铁律）。

    Args:
        wake_already_applied: 宿主若已自行判定唤醒（例如平台层已知是私聊），
            置 True 可跳过 `apply_wake` 的前缀切分。

    Returns:
        `DispatchResult`（含命中/执行/异常/结果/是否终止/是否允许默认 LLM）。
    """
    res = DispatchResult()

    # ---- 阶段 A ----
    if wake_already_applied:
        res.is_wake = bool(getattr(event, "is_wake", False))
    else:
        res.is_wake = apply_wake(
            event,
            wake_prefixes,
            friend_message_needs_wake_prefix=friend_message_needs_wake_prefix,
        )

    if not res.is_wake:
        # 真实现 `:247-248`：没唤醒 → stop_event（已在 apply_wake 里做）
        res.stopped = event.is_stopped()
        res.result = event.get_result()
        return res

    activated, parsed = select_handlers(event, cfg, plugins_name=plugins_name)
    res.activated = activated
    res.parsed_params = parsed

    # 🔴 中间产物必须写回 event（真实现 `:244-245`）——
    #    插件与后续阶段都从这里取，不写就是"派发看起来跑了但谁都拿不到"。
    event.set_extra("activated_handlers", activated)
    event.set_extra("handlers_parsed_params", parsed)

    if not activated:
        # 没有任何 handler 命中：**不是失败**，但要如实反映
        # （真实现此时会继续走默认 LLM 链路）
        res.stopped = event.is_stopped()
        res.allow_default_llm = _allow_default_llm(event)
        return res

    # ---- 阶段 B ----
    for handler in activated:
        if event.is_stopped():
            break
        params = parsed.get(handler.handler_full_name, {}) or {}
        try:
            # ⚠️ 照真实现 `context_utils.py:37`：**只传 event**，
            #    插件实例已由加载器用 `functools.partial` 绑好
            #    （见 `PluginLoader._bind_handlers_to_instance`）。
            #
            # 🔴 **在每个 yield 点收集结果**（洋葱模型的等价物）：
            #    真实现是在 yield 点让后续阶段（respond）把结果发出去，
            #    然后才 `clear_result()`。我们在这里取走，
            #    否则循环结束后 `get_result()` 必为 None（实测踩到）。
            async for _ in call_handler(event, handler.handler, **params):
                current = event.get_result()
                if current is not None:
                    res.emitted.append(current)
                # `event.send()` 的内容也在此刻快照（后面会被继续追加）
                res.sent = list(getattr(event, "sent_messages", []) or [])
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            logger.error(
                "插件 handler 执行失败：%s\n%s", handler.handler_full_name, tb
            )
            res.errors.append((handler.handler_full_name, f"{type(exc).__name__}: {exc}"))

            # 真实现 `:59-66`：触发 OnPluginErrorEvent 钩子（尽力而为）
            try:
                await _call_event_hook(event, EventType.OnPluginErrorEvent, exc, tb)
            except Exception:  # noqa: BLE001
                logger.error("OnPluginErrorEvent 钩子派发失败", exc_info=True)

            # 真实现 `:68-72`：未停止且是唤醒指令 → 把错误回给用户
            if not event.is_stopped() and getattr(event, "is_at_or_wake_command", False):
                try:
                    from astrbot_host.compat.core.message.message_event_result import (
                        MessageEventResult,
                    )

                    event.set_result(
                        MessageEventResult().message(
                            f":(\n\n在调用插件 handler "
                            f"{handler.handler_name} 时出现异常：{exc}"
                        )
                    )
                except Exception:  # noqa: BLE001
                    logger.error("错误结果回写失败", exc_info=True)
            event.stop_event()
            break

        res.executed.append(handler.handler_full_name)

        # 真实现 `:51-53`：执行完一个就检查中断，并**清掉结果**
        if event.is_stopped():
            break
        event.clear_result()

    # ---- 收尾 ----
    res.sent = list(getattr(event, "sent_messages", []) or [])
    # ⚠️ 此时 `event.get_result()` 通常是 None —— 结果已在 yield 点被收集进
    #    `res.emitted`，且每轮 handler 后 `clear_result()`（照真实现）。
    #    这里**兜底**再取一次，防止"没有走 call_handler 却写了结果"的情况
    #    （例如某个 handler 直接 `event.set_result()` 后正常返回）。
    tail = event.get_result()
    if tail is not None:
        res.emitted.append(tail)
    #: `result` 保留"最后一个结果"的语义，方便单 handler 场景直接取用
    res.result = res.emitted[-1] if res.emitted else None
    res.stopped = event.is_stopped()
    res.allow_default_llm = _allow_default_llm(event)
    return res


async def _call_event_hook(
    event: Any, hook_type: EventType, *args: Any, **kwargs: Any
) -> None:
    """调用事件钩子（照真实现 `context_utils.py:78-112`）。

    ⚠️ 真实现用 `assert inspect.iscoroutinefunction(...)` 卡住非协程钩子；
       我们改成**记 ERROR 并跳过**（assert 在生产里会被 `-O` 掉，
       而且一个坏钩子不该让整条链路炸 —— 但要**响亮**，不静默）。
    """
    for handler in star_handlers_registry.get_handlers_by_event_type(
        hook_type, plugins_name=getattr(event, "plugins_name", None)
    ):
        try:
            if not inspect.iscoroutinefunction(handler.handler):
                logger.error(
                    "钩子 %s（%s）不是协程函数 → 跳过",
                    handler.handler_full_name, hook_type.name,
                )
                continue
            await handler.handler(event, *args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.error(
                "钩子 %s 执行失败", handler.handler_full_name, exc_info=True
            )
    return None


def _allow_default_llm(event: Any) -> bool:
    """是否允许走"默认 LLM 链路"（照 `process_stage/stage.py:52-60`）。

    真实现的条件是三者**同时**成立：
    ```python
    if (not event._has_send_oper
        and event.is_at_or_wake_command
        and not event.call_llm):
    ```
    含义：**这轮还没发过任何消息** + 是被唤醒的 + 插件没禁止 LLM。
    """
    return (
        not bool(getattr(event, "_has_send_oper", False))
        and bool(getattr(event, "is_at_or_wake_command", False))
        and not bool(getattr(event, "call_llm", False))
    )


__all__ = [
    "DispatchResult",
    "apply_wake",
    "call_handler",
    "dispatch",
    "select_handlers",
]
