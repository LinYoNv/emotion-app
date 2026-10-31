# -*- coding: utf-8 -*-
"""默认 LLM 回复 —— 补上宿主缺失的「插件不说话就由核心管线回」这一环。

## 为什么需要它（2026-10-29 实测挖出来的真断点）

QQ 私聊的消息链路走到了：

```
[CompanionV2] 核心回复准入检查 event_ref=… channel=private
[CompanionV2] 交由 AstrBot 核心回复管线 event_ref=… channel=private
                                          ↑ 日志到此为止
```

然后**什么都没有**：没有模型调用、没有发出消息（`sent=0`）、没有报错。

根因：陪伴 7.0.0 对普通私聊的策略是「**交出**这一轮，让 AstrBot 自己的
默认回复管线去说」（日志里 `action=reply reason=private_default_reply`），
它自己不产出任何消息。

而我们的宿主在 `dispatch_event()` 之后**只投递 `result.emitted`** ——
插件不产出 ⇒ 什么都不发。`DispatchResult.allow_default_llm` 这个字段
虽然被算出来了（`dispatch.py:535` 照着真实现的三条件），
但**除测试之外没有任何生产代码消费它**。

⇒ 真 AstrBot 的「插件没接话 → 核心 LLM 回复」这一步，我们从来没实现过。
   面板聊天没暴露这个问题，是因为面板那条路（`app.py:411`）
   自己就把"插件没说话"回落到 LLM 了 —— **QQ 这条路没有**。

## 判据（照 `dispatch.py:535` 的真实现三条件）

```python
if (not event._has_send_oper          # 本轮还没发过任何消息
    and event.is_at_or_wake_command   # 是被唤醒的
    and not event.call_llm):          # 插件没有禁止 LLM
    → 走默认 LLM 管线
```

⚠️ 三个条件**缺一不可**：
  * `_has_send_oper` 为真 = 插件已经发过话了，再走 LLM 就是**重复回复**；
  * `is_at_or_wake_command` 为假 = 没被唤醒（群聊里没 @），**不该插嘴**；
  * `call_llm` 为真 = 插件明确禁止 LLM（它要自己处理）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("emotion_app.web")


def should_use_default_llm(result: Any, event: Any) -> bool:
    """本轮该不该由核心 LLM 管线来回复（**逐条照真实现**）。

    Args:
        result: `dispatch()` 的返回值（`DispatchResult`）。
        event: 同一个 `AstrMessageEvent`。

    Returns:
        True = 交给核心 LLM 回复。
    """
    if result is None:
        return False

    # ① 宿主自己的判定（`dispatch._allow_default_llm`，照真实现三条件）
    allowed = bool(getattr(result, "allow_default_llm", False))
    if not allowed:
        return False

    # ② 插件**已经产出**了消息 ⇒ 这轮已经有人接话，再回就是重复。
    #    ⚠️ 这一条在宿主侧再守一次是**刻意冗余**：
    #       真实现的 `_has_send_oper` 靠插件主动置位，而插件可能"产出了但没置位"；
    #       重复回复对用户是**可见事故**，值得多一道闸。
    emitted = list(getattr(result, "emitted", None) or [])
    if emitted:
        return False

    # ③ `result.result` 有内容也算"插件说了话"
    #    （老路径把结果放在这里，不检查会重复）
    if getattr(result, "result", None) is not None:
        return False

    # ④ 事件已被插件 stop_event 拦下 ⇒ 尊重它
    try:
        if callable(getattr(event, "is_stopped", None)) and event.is_stopped():
            return False
    except Exception:  # noqa: BLE001 — 判不了就当没停，继续走 LLM
        pass

    return True


def external_message_parts(event: Any) -> list[dict[str, Any]]:
    from types import SimpleNamespace
    from astrbot_host.provider_content import provider_request_to_messages, to_media_part

    getter = getattr(event, "get_messages", None)
    components = getter() if callable(getter) else []
    parts: list[dict[str, Any]] = []
    for component in components or []:
        raw_type = component.get("type", "") if isinstance(component, dict) else getattr(component, "type", "")
        kind = str(getattr(raw_type, "value", raw_type) or type(component).__name__).lower()
        data = component.get("data", component) if isinstance(component, dict) else component
        get = data.get if isinstance(data, dict) else lambda key, default=None: getattr(data, key, default)
        if kind in ("plain", "text"):
            text = get("text", "")
            if text:
                parts.append({"type": "text", "text": str(text)})
        elif kind in ("image", "record", "audio"):
            locator = next((get(key) for key in ("url", "file", "path") if get(key)), "")
            parts.append(to_media_part("image" if kind == "image" else "audio", str(locator)))
        elif kind == "video":
            from emotion_app.domain.chat import ChatError

            logger.error("默认模型视频输入尚未接入（umo=%s）", event.unified_msg_origin)
            raise ChatError("默认模型视频输入尚未接入")
    text = str(getattr(event, "message_str", "") or "")
    if text.strip() and not any(part.get("type") == "text" for part in parts):
        parts.insert(0, {"type": "text", "text": text})
    if parts:
        request = SimpleNamespace(prompt=text, contexts=[{"role": "user", "content": parts}])
        parts = provider_request_to_messages(request, current_user_index=0, include_system=False)[0]["content"]
    if not any(part.get("type") != "text" or str(part.get("text", "")).strip() for part in parts):
        return []
    return parts


async def build_external_chat_request(
    chat: Any, plugins: Any, event: Any, *, content_parts: list[dict[str, Any]] | None = None,
) -> Any:
    from emotion_app.domain.chat import ChatError, ChatRequest, LLMRequestCancelled
    from emotion_app.domain.message import from_upstream_message

    umo = str(event.unified_msg_origin or "")
    if not umo:
        raise ChatError("外部消息缺少来源，无法隔离会话")
    context = getattr(getattr(plugins, "host", None), "context", None)
    manager = getattr(context, "persona_manager", None)
    conversations = getattr(plugins, "conversation_manager", None)
    cid = ""
    conversation = None
    if conversations is not None:
        cid = await conversations.get_curr_conversation_id(umo) or ""
        if cid:
            conversation = await conversations.get_conversation(umo, cid)
            if conversation is None:
                raise ChatError("当前原生会话不存在，拒绝使用其他窗口历史")
    if manager is None:
        persona = await chat.resolve_persona(session_id=umo)
        selected = getattr(persona, "name", None)
    else:
        conversation_persona = getattr(conversation, "persona_id", None) or None
        config = context.get_config(umo)
        provider_settings = config.get("provider_settings", {}) if isinstance(config, dict) else {}
        selected, native_persona, _, special = await manager.resolve_selected_persona(
            umo=umo, conversation_persona_id=conversation_persona,
            platform_name=event.get_platform_name(), provider_settings=provider_settings,
        )
        persona = None
        if not special and selected != "[%None]":
            for item in await plugins.personas.list():
                if selected in (item.id, item.name):
                    persona = item
                    break
            if persona is None:
                raise ChatError(f"外部会话选中的人格不可用：{selected!r}")
    if conversations is not None and not cid:
        conversation = await conversations.get_or_create_conversation(
            umo, platform_id=event.get_platform_id(), persona_id=selected,
        )
        cid = conversation.cid
    session_id = f"{umo}:conversation:{cid}" if cid else umo
    if chat.sessions is not None:
        await chat.sessions.ensure(
            session_id, title=f"{event.get_platform_id()} · {event.get_sender_name() or event.session_id}",
            user_id=str(event.get_sender_id()),
        )
    history = None
    reset_revision = None
    if cid:
        if chat.sessions is not None:
            conversation = await conversations.project_conversation(umo, cid, session_id)
        if conversation is None or not isinstance(conversation.history, str):
            raise ChatError("原生会话历史不可用")
        from emotion_app.domain.conversation_manager import conversation_messages

        history = conversation_messages(conversation.history)
        reset_revision = int(getattr(conversation, "reset_revision", 0))

        async def validate_history() -> None:
            if await conversations.get_curr_conversation_id(umo) != cid:
                raise LLMRequestCancelled("原生会话已切换，本轮回复已取消")
            current = await conversations.get_conversation(umo, cid)
            if current is None or int(getattr(current, "reset_revision", 0)) != reset_revision:
                raise LLMRequestCancelled("原生会话历史已重置，本轮回复已取消")

        event.set_extra("_emotion_native_history_guard", validate_history)
        event.set_extra("_emotion_native_conversation_id", cid)
    parts = external_message_parts(event) if content_parts is None else content_parts
    if not parts:
        raise ChatError("外部消息没有可处理的文本或媒体")
    return ChatRequest(
        messages=[from_upstream_message({
            "role": "user", "content": (parts if any(part.get("type") != "text" for part in parts)
                                         else "".join(part["text"] for part in parts)),
        })],
        persona=persona, session_id=session_id, llm_event=event,
        conversation_id=cid, history_override=history, persist=False,
        conversation_reset_revision=reset_revision,
    )


async def record_external_chat_reply(
    chat: Any, plugins: Any, event: Any, request: Any,
) -> None:
    if event.get_extra("_emotion_delivery_recorded") or event.get_extra("_emotion_delivery_discarded"):
        return
    delivered = event.get_extra("_emotion_delivered_messages") or []
    if not delivered:
        return
    assistant = {"role": "assistant", "content": "".join(item["content"] for item in delivered)}
    if not assistant["content"].strip():
        return
    for item in delivered:
        for field in ("image_path", "audio_path", "video_path"):
            if item.get(field) and field not in assistant:
                assistant[field] = item[field]
    user = next(message for message in reversed(request.messages) if message.role == "user")
    if request.conversation_id:
        recorded = await plugins.conversation_manager.record_reply(
            str(event.unified_msg_origin), request.conversation_id, request.session_id,
            user.to_upstream(), assistant,
            expected_reset_revision=request.conversation_reset_revision,
        )
        if recorded is False:
            event.set_extra("_emotion_delivery_discarded", True)
            logger.info("原生历史已重置，未将旧轮次送达内容写回已清历史（umo=%s）", event.unified_msg_origin)
            return
    else:
        from emotion_app.domain.message import from_upstream_message

        if getattr(chat, "sessions", None) is not None:
            await chat.sessions.append(request.session_id, [user, from_upstream_message(assistant)])
        else:
            media = {"user_message": user} if user.content_parts is not None else {}
            await chat.record(request.session_id, user_text=user.content,
                              assistant_text=assistant["content"], **media)
    event.set_extra("_emotion_delivery_recorded", True)
    chat.schedule_summarize(request.session_id)


def _delivered_message(message: Any) -> dict[str, Any]:
    parts = getattr(message, "chain", message if isinstance(message, (list, tuple)) else [])
    text: list[str] = []
    metadata: dict[str, str] = {}
    for component in parts:
        raw = component.get("type", "") if isinstance(component, dict) else getattr(component, "type", "")
        kind = str(getattr(raw, "value", raw) or type(component).__name__).lower()
        data = component.get("data", component) if isinstance(component, dict) else component
        get = data.get if isinstance(data, dict) else lambda key, default=None: getattr(data, key, default)
        if kind in ("plain", "text"):
            text.append(str(get("text", "") or ""))
            continue
        labels = {"image": "图片", "record": "语音", "audio": "语音", "video": "视频", "file": "文件"}
        text.append("[" + labels.get(kind, "消息") + "]")
        field = {"image": "image_path", "record": "audio_path", "audio": "audio_path", "video": "video_path"}.get(kind)
        locator = next((get(key) for key in ("url", "file", "path") if get(key)), "")
        if field and locator and field not in metadata:
            metadata[field] = str(locator)
    if isinstance(message, str):
        text.append(message)
    return {"role": "assistant", "content": "".join(text), **metadata}


def _lifecycle_response(event: Any, result: Any, failed: bool = False) -> Any:
    """给 `on_agent_done` 一个带 `role` 的 response。

    🔴 官方 `main.py:1106` 只在 `getattr(response, "role", None) == "assistant"`
       时才把原生回合标成 `completed`。而宿主原来只在**有工具**的轮次里
       才通过 `response_hook` 写 `_emotion_llm_response`（`llm_turn.py:159`），
       纯文本轮没有它 ⇒ 直接传 None 会让 done 钩子写不进去，
       **修了派发却仍然拿不到 completed**（假修复）。
    ✅ 这里优先用钩子已产出的 `LLMResponse`；没有就按结果现场构造一个。
    """
    # 🔴 失败/取消轮**不能**复用 `_emotion_llm_response`：有工具调用的轮次
    #    `response_hook` 会把那一轮写成 `role="tool"`（`llm_turn.py:159`），
    #    复用就等于把**上一轮残留**当成本轮结果上报。官方只认
    #    `role=="assistant"` 才写 completed，用 "err" 明确表达"这轮没有可用回复"。
    if failed:
        try:
            from astrbot_host.compat.core.provider.entities import LLMResponse

            return LLMResponse(role="err",
                               completion_text=str(getattr(result, "content", "") or ""))
        except Exception:  # noqa: BLE001
            logger.exception("构造失败轮生命周期 response 失败")
            return None
    response = event.get_extra("_emotion_llm_response") if callable(getattr(event, "get_extra", None)) else None
    if response is not None:
        return response
    if result is None:
        return None
    try:
        from astrbot_host.compat.core.provider.entities import LLMResponse

        return LLMResponse(completion_text=str(getattr(result, "content", "") or ""))
    except Exception:  # noqa: BLE001 — 只影响生命周期遥测，不该影响回复
        logger.exception("构造 agent 生命周期 response 失败")
        return None


async def dispatch_agent_lifecycle(event: Any, *, phase: str, response: Any = None) -> None:
    """派发官方 agent 运行生命周期钩子（`on_agent_begin` / `on_agent_done`）。

    🔴🔴 **2026-10-30 补的真缺口**：宿主原来**从不派发**这两个事件
       （全仓 `call_event_hook` 只有 request/response/decorating/after_sent/plugin_error）。
       后果是**静默的**、且与「核心用户」无关：
       官方 `main.py:1097-1106` 的 `@filter.on_agent_done()` 永不执行 ⇒
       `companion_v2/conversation/native_turns.py:147` 那句
       `CASE WHEN generation_state='prepared' THEN 'completed' END` 永不命中
       （请求阶段已被 `main.py:1054` 推成 `running`）⇒ 原生回合停在 running/failed
       ⇒ `proactive/scheduling.py:1490` 只取 `completed` 作候选 ⇒
       **主动消息候选集恒空**，而调度器日志只会一直显示 `running`，不报错。

    ⚠️ 语义照官方：`done` 表示**运行结束**（成功/失败/取消都算），不是"成功"。
       因此调用方应在 `finally` 里调它，且**一次运行只派发一次**。
    ⚠️ `run_context` 传 None 是安全的：官方 `_active_runner(event, None)` 解析不到
       runner 时只跳过可选遥测，写 `completed` 的 `_observe_core_phase` 不依赖它。
    """
    from astrbot_host.llm_hooks import call_event_hook
    from astrbot_host.registry import EventType

    if event is None:
        return
    try:
        if phase == "begin":
            await call_event_hook(event, EventType.OnAgentBeginEvent, None)
        else:
            await call_event_hook(event, EventType.OnAgentDoneEvent, None, response)
    except Exception:  # noqa: BLE001 — 生命周期钩子绝不能让回复链挂掉
        logger.exception("agent 生命周期钩子派发失败（%s）", phase)


async def begin_agent_lifecycle(request: Any) -> None:
    await dispatch_agent_lifecycle(request.llm_event, phase="begin")


async def done_agent_lifecycle(request: Any, result: Any, failed: bool = False) -> None:
    if request.llm_event is None:
        return
    await dispatch_agent_lifecycle(
        request.llm_event, phase="done",
        response=_lifecycle_response(request.llm_event, result, failed),
    )


def bind_event_sender(event: Any, platform_manager: Any) -> None:
    from astrbot_host.compat.core.platform.message_session import MessageSession

    session = MessageSession.from_str(event.unified_msg_origin)
    platform = next((inst for inst in platform_manager.get_insts()
                     if str(inst.meta().id) == session.platform_id), None)
    if platform is None:
        raise RuntimeError(f"找不到来源平台：{session.platform_id}")

    async def send(message: Any) -> None:
        if message is None:
            return
        guard = event.get_extra("_emotion_native_history_guard")
        if callable(guard):
            await guard()
        captured = _delivered_message(message)
        ok = await platform.send_by_session(session, message)
        if ok is False:
            raise RuntimeError(f"平台 {session.platform_id} 如实报告投递失败")
        delivered = event.get_extra("_emotion_delivered_messages")
        if delivered is None:
            delivered = []
            event.set_extra("_emotion_delivered_messages", delivered)
        if captured["content"].strip():
            delivered.append(captured)
            event.set_extra("_emotion_delivered_reply", captured["content"])
        event._has_send_oper = True

    event.send = send


async def deliver_default_llm_reply(event: Any, result: Any) -> bool:
    from astrbot_host.compat.core.message.message_event_result import (
        MessageEventResult, ResultContentType,
    )
    from astrbot_host.llm_hooks import call_event_hook
    from astrbot_host.registry import EventType

    if event._has_send_oper:
        logger.info("本轮工具已发送回复，未重复投递（umo=%s）", event.unified_msg_origin)
        return False
    response = event.get_extra("_emotion_llm_response")
    chain = getattr(response, "result_chain", None)
    message = MessageEventResult().set_result_content_type(ResultContentType.LLM_RESULT)
    if chain is not None:
        message.chain = list(chain.chain)
        if not message.chain:
            logger.info("核心回复被响应钩子空链抑制（umo=%s）", event.unified_msg_origin)
            return False
    else:
        text = str(getattr(result, "content", "") or "")
        if text:
            message.message(text)
        from astrbot_host.compat.core.message.components import Image

        for attachment in getattr(result, "attachments", None) or []:
            if attachment.get("type") != "image" or not attachment.get("url"):
                raise RuntimeError("核心回复附件不可投递：需要真实图片 URL")
            message.chain.append(Image.fromURL(attachment["url"]))
    if not _delivered_message(message)["content"].strip():
        logger.info("核心回复没有可投递内容（umo=%s）", event.unified_msg_origin)
        return False
    event.set_result(message)
    cancelled = await call_event_hook(event, EventType.OnDecoratingResultEvent)
    message = event.get_result()
    if cancelled or not message or not _delivered_message(message)["content"].strip():
        logger.info("核心回复在发送前由插件取消或清空（umo=%s）", event.unified_msg_origin)
        return False
    before = len(event.get_extra("_emotion_delivered_messages") or [])
    await event.send(message)
    if len(event.get_extra("_emotion_delivered_messages") or []) == before:
        logger.info("核心回复未获得平台成功投递记录（umo=%s）", event.unified_msg_origin)
        return False
    await call_event_hook(event, EventType.OnAfterMessageSentEvent)
    return True
