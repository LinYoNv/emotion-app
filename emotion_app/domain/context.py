# -*- coding: utf-8 -*-
"""上下文策略：爱语的三种模式。

⭐ 依据（`docs/AIYU_MEMORY_REVIEW.md` + 实测配置键）：

| 爱语模式 | 配置键（实测） | 本模块的 mode |
|---|---|---|
| 混合模式 | `key_mixed_context_enable` | `semantic` |
| 动态上下文 | `key_dynamic_context_enable` | `rolling_summary` |
| 滚动上下文 | `key_scroll_context_enable` | `recent_only` |

⭐ **为什么"稳定前缀"重要**（爱语记忆好的真实机制）：
   `AIYU_MEMORY_REVIEW.md` 的结论是 —— 混合模式「刻意让请求前缀保持稳定以提高缓存命中……
   使模型每轮接收的『自己是谁、记得什么』更一致」。
   → 所以本模块保证：**system 提示词只依赖人设，不依赖历史**；
     历史一律作为**独立的 message** 追加，从而前缀稳定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .message import ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_USER, Message

#: 三种模式（值刻意与陪伴插件的 `MemoryContextPolicy.mode` 对齐）
MODE_SEMANTIC = "semantic"            # 混合：摘要 + 最近原文
MODE_ROLLING_SUMMARY = "rolling_summary"  # 动态：累积到 N 轮后总结
MODE_RECENT_ONLY = "recent_only"      # 滚动：固定窗口

VALID_MODES = (MODE_SEMANTIC, MODE_ROLLING_SUMMARY, MODE_RECENT_ONLY)

#: 提示词片段的 order 常量（供各模块统一引用，避免魔法数字）
ORDER_TOOLS = 800      # 工具使用规则（较稳定，放人设之后、记忆之前）


class ContextError(Exception):
    """上下文策略配置错误。"""


@dataclass(slots=True)
class ContextPolicy:
    """上下文策略（照陪伴插件 `MemoryContextPolicy` 的字段，做减法）。

    ⚠️ 这些是**运行参数**，不是人格规则 —— 照 `AIYU_MEMORY_REVIEW.md` §5 的批评：
       「固定 N/Y/Z 轮数是资源策略，不应被写成角色行为」。
    """

    mode: str = MODE_RECENT_ONLY

    #: 保留最近多少条消息（所有模式下都生效的硬下限）
    protected_tail: int = 20

    #: 累积多少条消息后触发总结（rolling/semantic 模式用）
    summary_trigger: int = 40

    #: 摘要最多占多少字符（预算硬限制，防摘要本身膨胀）
    summary_max_chars: int = 2000

    #: 送入上游的总字符预算（粗粒度保护；精确 token 计数留待后续）
    budget_chars: int = 24000

    #: 🔴 **用哪个模型做总结**（情绪 2026-10-09：上下文策略改成会话级）。
    #:
    #: 取值形态 = 模型供应商页里的模型 id（`<源id>/<模型名>`）；
    #: 空串 = **用会话当前的对话模型**（`Summarizer` 构造时那个 model）。
    #:
    #: ⚠️ 为什么值得有：总结是**低频但可能很贵**的调用（一次要把整段旧对话读进去）。
    #:    用户常想让主对话用好模型、总结用便宜快的。
    #: ⚠️ 它**真会被用**：`Summarizer.maybe_summarize` 收 `model` 并透传到上游
    #:    （`summarizer.py` 的 `model=model or self._model`）。**不是空壳。**
    summary_model: str = ""

    #: 🔴 **总结用的提示词**（留空 = 用内置的 `SUMMARY_PROMPT`）。
    #:
    #: ⚠️ 它**真会被用**：`build_summary_messages(..., instruction=...)` 拿它当 system 消息。
    #: ⚠️ 为什么值得暴露：不同人设想要的摘要粒度差别很大（有的要"关键事实"、
    #:    有的要"情绪走向"），写死一种必然有一半不合用。
    summary_instruction: str = ""

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ContextError(f"未知 mode：{self.mode!r}，允许 {VALID_MODES}")
        if self.protected_tail < 0:
            raise ContextError("protected_tail 不能为负")
        if self.summary_trigger < 1:
            raise ContextError("summary_trigger 必须 >= 1")
        if self.summary_max_chars < 1:
            raise ContextError("summary_max_chars 必须 >= 1")
        if self.budget_chars < 1:
            raise ContextError("budget_chars 必须 >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "protected_tail": self.protected_tail,
            "summary_trigger": self.summary_trigger,
            "summary_max_chars": self.summary_max_chars,
            "budget_chars": self.budget_chars,
            "summary_model": self.summary_model,
            "summary_instruction": self.summary_instruction,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextPolicy:
        return cls(
            mode=str(d.get("mode") or MODE_RECENT_ONLY),
            protected_tail=int(d.get("protected_tail", 20)),
            summary_trigger=int(d.get("summary_trigger", 40)),
            summary_max_chars=int(d.get("summary_max_chars", 2000)),
            budget_chars=int(d.get("budget_chars", 24000)),
            # ⚠️ 旧会话没有这两个字段 → 空串（= 用内置提示词 / 会话当前模型），
            #    这正是"没设过"的正确语义，**不是**回落成某个写死的模型名。
            summary_model=str(d.get("summary_model") or ""),
            # ⚠️ 提示词是**用户原文**，不能 strip 掉尾部换行再存
            #    （本项目踩过：`.strip()` 静默改掉用户原文）。
            summary_instruction=str(d.get("summary_instruction") or ""),
        )


# ---------------------------------------------------------------------------
# 组装结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContextResult:
    """上下文组装结果。

    ⭐ 刻意分成 `history` 与 `summary` 两块 —— 便于观测"到底是原文还是摘要生效了"。
    """

    history: list[Message] = field(default_factory=list)
    summary: str = ""
    dropped: int = 0          # 因预算被丢掉的消息数
    mode: str = MODE_RECENT_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "history_count": len(self.history),
            "dropped": self.dropped,
            "has_summary": bool(self.summary),
            "summary_chars": len(self.summary),
        }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _is_dialog(m: Message) -> bool:
    return m.role in (ROLE_USER, ROLE_ASSISTANT)


def build_context(
    messages: list[Message],
    policy: ContextPolicy,
    *,
    summary: str = "",
) -> ContextResult:
    """按策略挑出要送上游的历史。

    Args:
        messages: 该会话的**全部**消息（按时间升序）。
        policy: 策略。
        summary: 已生成的摘要（rolling/semantic 模式用）。

    Returns:
        ContextResult —— **不含** system 提示词（那部分由 `prompt.assemble` 负责，
        以保证前缀只依赖人设 → 前缀稳定）。
    """
    policy.validate()

    dialog = [m for m in messages if _is_dialog(m)]
    total = len(dialog)

    # ---- recent_only：固定窗口 ----
    if policy.mode == MODE_RECENT_ONLY:
        kept = dialog[-policy.protected_tail:] if policy.protected_tail else []
        # ⚠️ 实测踩到：这里最初直接 return，**跳过了预算裁剪**
        #    → protected_tail 设大 + 单条消息很长时，会把上游直接打爆（context 超限）。
        #    预算检查必须**所有模式都走**。
        result = ContextResult(
            history=kept, summary="", dropped=total - len(kept), mode=policy.mode
        )
        return _apply_budget(result, policy)

    # ---- rolling_summary / semantic：摘要 + 最近原文 ----
    # 保留最近 tail 条原文；更早的部分"应该"已经被摘要覆盖
    tail = policy.protected_tail or 0
    kept = dialog[-tail:] if tail else []

    use_summary = summary.strip() if policy.mode in (MODE_SEMANTIC, MODE_ROLLING_SUMMARY) else ""
    if use_summary and len(use_summary) > policy.summary_max_chars:
        # 预算硬限制：摘要本身也不能无限长
        use_summary = use_summary[: policy.summary_max_chars]

    result = ContextResult(
        history=kept,
        summary=use_summary,
        dropped=max(0, total - len(kept)),
        mode=policy.mode,
    )
    return _apply_budget(result, policy)


def _apply_budget(result: ContextResult, policy: ContextPolicy) -> ContextResult:
    """字符预算保护：**从最旧的开始丢**，但绝不丢掉最后一条用户消息。

    ⚠️ 判据：宁可丢历史，也不能让"当前这句话"发不出去。
    """
    def size(r: ContextResult) -> int:
        return sum(len(m.content) for m in r.history) + len(r.summary)

    # 从最旧的开始丢，**但至少留一条**（最后一条通常是当前问题）
    while size(result) > policy.budget_chars and len(result.history) > 1:
        result.history.pop(0)
        result.dropped += 1

    # 极端情况：单条消息本身就超预算 → 截断它（总比发给上游被拒好）
    if len(result.history) == 1 and len(result.history[0].content) > policy.budget_chars:
        trimmed = result.history[0].content[-policy.budget_chars:]
        log = __import__("logging").getLogger(__name__)
        log.warning(
            "单条消息超过上下文预算，已截断",
            extra={"data": {"original": len(result.history[0].content), "kept": len(trimmed)}},
        )
        result.history[0].content = trimmed

    return result


def needs_summary(messages: list[Message], policy: ContextPolicy) -> bool:
    """是否该触发总结（rolling/semantic 模式）。

    判据：可对话消息数**超过** protected_tail + summary_trigger。
    → 即"已经有一批消息滑出保护窗口，且数量够多，值得总结"。
    """
    if policy.mode not in (MODE_SEMANTIC, MODE_ROLLING_SUMMARY):
        return False
    dialog = [m for m in messages if _is_dialog(m)]
    return len(dialog) > policy.protected_tail + policy.summary_trigger


def to_summarize(messages: list[Message], policy: ContextPolicy) -> list[Message]:
    """挑出"该被总结进摘要"的消息（滑出保护窗口的那些）。

    ⚠️ 刻意**不在回复链里同步做** —— 调用方应放到后台任务
       （照爱语的"静默总结"与 `AIYU_MEMORY_REVIEW.md` §5 的建议）。
    """
    dialog = [m for m in messages if _is_dialog(m)]
    tail = policy.protected_tail or 0
    return dialog[:-tail] if tail and len(dialog) > tail else []


#: 摘要提示词模板。
#: ⚠️ 刻意**不带**爱语那些硬编码（"严重健忘症"、"每条消息必须记录"…）。
SUMMARY_PROMPT = (
    "请把下面这段对话压缩成简洁的摘要，保留：\n"
    "1) 用户的稳定偏好、身份、承诺；\n"
    "2) 正在进行或待办的事情及其状态；\n"
    "3) 重要的情绪与关系变化。\n"
    "只输出摘要正文，不要评论。若信息不足以总结，就输出空字符串。"
)


def build_summary_messages(
    chunk: list[Message], *, previous_summary: str = "", instruction: str = ""
) -> list[Message]:
    """构造"生成摘要"的请求消息。

    Args:
        instruction: 自定义总结提示词（情绪 2026-10-09 的会话级字段）。
            **空串 = 用内置的 `SUMMARY_PROMPT`** —— 不能拿空串去当 system 消息
            （那会让模型完全没有摘要格式的约束，输出会漂）。
            ⚠️ 传进来的用户原文**不做 strip**（只判空），理由见 `ContextPolicy.summary_instruction`。
    """
    lines: list[str] = []
    if previous_summary.strip():
        lines.append(f"【已有摘要】\n{previous_summary.strip()}\n")
    lines.append("【新增对话】")
    for m in chunk:
        who = "用户" if m.role == ROLE_USER else "助手"
        content = (m.content or "").strip()
        if content:
            lines.append(f"{who}：{content}")

    return [
        Message(role=ROLE_SYSTEM, content=(instruction or "").strip() or SUMMARY_PROMPT),
        Message(role=ROLE_USER, content="\n".join(lines)),
    ]
