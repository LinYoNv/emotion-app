# -*- coding: utf-8 -*-
"""总结服务：把长对话压缩成摘要（**后台异步，不阻塞回复**）。

⭐ 设计依据（`docs/AIYU_MEMORY_REVIEW.md`）：
   爱语的三种上下文模式里，**动态/混合**都依赖摘要。它做对的地方：
   * 「**静默总结**」—— 不在回复链里同步做；
   * 「后台维护不打断对话……能把维护成本从对话体验中隐藏起来」；
   * 「退出、后台或达到摘要边界时再处理旧消息」。

🔴 本项目已踩过的对应坑（`docs/BACKEND_FRAMEWORK.md` §13.4）：
   **不在回复链里插同步步骤** —— 否则每次到边界都要多等一次上游往返。

设计要点：
  * **游标（`summarized_upto`）**：只总结"还没总结过"的部分，防重复；
  * **失败可重试**：失败时游标不动，下次接着来；
  * **幂等**：同一条消息不会被总结两次；
  * **不阻塞**：由调用方决定何时触发（本模块只提供 `maybe_summarize`）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..infra.logging_setup import get_logger
from . import context as context_mod
from .context import ContextPolicy
from .message import Message
from .ports import ChatBackend

log = get_logger(__name__)


@dataclass(slots=True)
class SummarizeOutcome:
    """一次总结尝试的结果。"""

    ran: bool = False
    ok: bool = False
    summarized: int = 0          # 本次并入摘要的消息条数
    summary: str = ""            # ⭐ 新摘要正文（调用方据此更新会话）
    summary_chars: int = 0
    cursor: int = 0              # 新的游标（已总结到第几条对话消息）
    reason: str = ""             # 未执行/失败的原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "summarized": self.summarized,
            "summary_chars": self.summary_chars,
            # ⚠️ 不含 summary 正文（可能含隐私，别进日志/API）
            "cursor": self.cursor,
            "reason": self.reason,
        }


class Summarizer:
    """把旧消息压成摘要。

    ⚠️ 只依赖 `ChatBackend` Protocol —— 换后端不改这里。
    """

    def __init__(self, client: ChatBackend, *, model: str = "") -> None:
        self._client = client
        self._model = model
        #: 同一会话的总结不能并发（防两次总结覆盖同一段消息）
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def maybe_summarize(
        self,
        *,
        session_id: str,
        messages: list[Message],
        policy: ContextPolicy,
        current_summary: str,
        cursor: int,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        model: str = "",
        force: bool = False,
    ) -> SummarizeOutcome:
        """按需总结。

        Args:
            messages: 该会话**全部**消息（升序）。
            cursor: 已总结到的位置（`summarized_upto`）。
            force: 忽略触发阈值，强制总结一次（用于手动触发/测试）。

        Returns:
            SummarizeOutcome —— 调用方据此更新会话的 summary 与 cursor。
        """
        policy.validate()

        if not force and not context_mod.needs_summary(messages, policy):
            return SummarizeOutcome(reason="未到总结边界")

        dialog = [m for m in messages if m.role in ("user", "assistant")]
        if cursor >= len(dialog):
            return SummarizeOutcome(reason="没有新消息需要总结", cursor=cursor)

        # 只取"滑出保护窗口"且"还没总结过"的部分
        boundary = max(0, len(dialog) - (policy.protected_tail or 0))
        chunk = dialog[cursor:boundary]
        if not chunk:
            return SummarizeOutcome(reason="窗口内的消息不参与总结", cursor=cursor)

        async with self._lock(session_id):
            try:
                summary = await self._summarize_once(
                    chunk,
                    previous_summary=current_summary,
                    base_url=base_url,
                    api_key=api_key,
                    timeout=timeout,
                    # 🔴 模型优先取**调用方传的**（会话级 `policy.summary_model`），
                    #    否则用 Summarizer 自己的默认（= 服务启动时的上游模型）。
                    model=model or self._model,
                    # 🔴 会话级自定义提示词（空 = 用内置）
                    instruction=policy.summary_instruction,
                )
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 失败**不推进游标** —— 下次重试同一段（幂等：结果一样）
                log.warning(
                    "总结失败，游标不推进，下次重试",
                    extra={"data": {"session": session_id, "chunk": len(chunk),
                                    "error": str(exc)[:120]}},
                )
                return SummarizeOutcome(reason=f"总结失败：{exc}", cursor=cursor)

        if not summary:
            # 模型认为不值得总结 —— 仍然推进游标（否则会反复问同一段）
            log.info(
                "模型判断无需总结，游标照常推进",
                extra={"data": {"session": session_id, "chunk": len(chunk)}},
            )
            return SummarizeOutcome(
                ran=True, ok=True, summarized=len(chunk), cursor=boundary,
                summary=current_summary,      # 保持旧摘要不变
                reason="模型输出空摘要",
            )

        log.info(
            "总结完成",
            extra={"data": {"session": session_id, "chunk": len(chunk),
                            "chars": len(summary)}},
        )
        return SummarizeOutcome(
            ran=True, ok=True, summarized=len(chunk),
            summary=summary, summary_chars=len(summary), cursor=boundary,
        )

    async def _summarize_once(
        self,
        chunk: list[Message],
        *,
        previous_summary: str,
        base_url: str,
        api_key: str,
        timeout: float,
        model: str,
        instruction: str = "",
    ) -> str:
        """真正调一次上游做总结。"""
        prompt_msgs = context_mod.build_summary_messages(
            chunk, previous_summary=previous_summary, instruction=instruction
        )
        result = await self._client.chat(
            prompt_msgs,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        text = str(getattr(result, "content", "") or "").strip()

        # 摘要也要守预算（防它自己膨胀）
        policy_guard = ContextPolicy()
        if len(text) > policy_guard.summary_max_chars:
            text = text[: policy_guard.summary_max_chars]
        return text


def merge_summary(previous: str, new: str, *, max_chars: int = 2000) -> str:
    """把新摘要并入旧摘要。

    ⚠️ 刻意**简单**（不是"再总结一次"）—— 因为上游已经被要求
       "把已有摘要 + 新增对话一起压缩"，所以新的就是全量结果。
       这里只在没有旧摘要时直接返回新的。
    """
    new = (new or "").strip()
    if not new:
        return (previous or "").strip()
    return new[:max_chars]
