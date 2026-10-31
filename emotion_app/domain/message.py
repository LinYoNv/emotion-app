# -*- coding: utf-8 -*-
"""消息模型。

字段来源：爱语真 dex 的 `Message.java`（**实测 14 个字段**）。

⭐ 设计决策（见 `docs/BACKEND_FRAMEWORK.md` §3.2）：
   **内部保留爱语的 14 字段全信息，出口时才降维成 OpenAI 形态。**
   反向做（只存 OpenAI 的 role/content）会丢掉语音、视频、思维链，事后补不回来。
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

#: 内部 role 取值。
#: ⚠️ `reasoning` 是**内部专用** —— 实测爱语把它过滤掉、**不发给上游**（`t10.d()`）。
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_REASONING = "reasoning"
#: 工具结果角色（OpenAI function calling 协议要求）
#: ⚠️ 它**必须**带 `tool_call_id`，否则上游无法把结果对应回哪次调用（会 400）
ROLE_TOOL = "tool"

#: 发给上游时要过滤掉的 role
UPSTREAM_EXCLUDED_ROLES = (ROLE_REASONING,)


@dataclass(slots=True)
class Message:
    """一条消息（内部表示）。"""

    role: str = ROLE_USER
    content: str = ""

    # ---- 爱语实测的其余字段 ----
    timestamp: float = field(default_factory=time.time)
    reply_content: str = ""       # 引用的消息
    image_path: str = ""
    image_desc: str = ""
    video_path: str = ""
    video_desc: str = ""
    audio_path: str = ""          # 语音
    forwarded_content: str = ""   # 转发内容
    reasoning_content: str = ""   # 思维链
    reasoning_duration: int = 0
    tool_calls: str = ""
    tool_call_id: str = ""
    content_parts: list[dict[str, Any]] | None = None

    def validate(self) -> None:
        if self.role not in (
            ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT, ROLE_REASONING, ROLE_TOOL
        ):
            raise ValueError(f"未知 role：{self.role!r}")
        # 🔴 协议硬要求：tool 消息必须带 tool_call_id
        if self.role == ROLE_TOOL and not self.tool_call_id:
            raise ValueError("role=tool 的消息必须带 tool_call_id")

    # ---- 降维：内部 → OpenAI 形态 ----

    def to_upstream(
        self,
        *,
        thinking_field: str = "reasoning_content",
        inject_system_markers: bool = False,
    ) -> dict[str, Any] | None:
        """转成上游要的 `{role, content}`。

        Args:
            thinking_field: 思维链字段名（爱语可配置，默认 `reasoning_content`）。
            inject_system_markers: system 消息是否包
                `<|SYSTEM_INJECT|>…<|END_INJECT|>`。**默认 False**（见下）。

        Returns:
            None 表示**这条不该发给上游**（如内部 reasoning 角色）。
        """
        if self.role in UPSTREAM_EXCLUDED_ROLES:
            return None

        content = self.content
        if self.content_parts is not None:
            content = [
                {key: deepcopy(value) for key, value in part.items() if key != "_no_save"}
                for part in self.content_parts
            ]
        if self.role == ROLE_SYSTEM and inject_system_markers and isinstance(content, str) and content:
            # 🔴🔴 **默认已关闭**（2026-10-29 实测，不要改回 True）
            #
            # `<|SYSTEM_INJECT|>…<|END_INJECT|>` 是**爱语私有的标记**，
            # 不是 OpenAI 协议。标准中转站/官方模型不认识它，
            # 会把它当成**用户数据**而不是系统指令 ⇒ **人设整个失效**。
            #
            # 实测（同一个 `gemini-3.8-flash-high`，只切这个开关）：
            #   A 带标记 → 「不是，我是 Gemini，由 Google 训练的大语言模型。」
            #   B 无标记 → 「（慢吞吞地睁开猩红的眼眸…）除了本小姐，这里
            #               难道还会有第二只高贵又可爱的莉莉丝吗」
            #
            # 现象特征：**人设正文确实发出去了**（system 4169 字符），
            #   模型也正常回话 —— 只是它不知道自己是谁。
            #   光看"调用上游 200 + 有回复"**永远看不出**这个问题。
            content = f"<|SYSTEM_INJECT|>{content}<|END_INJECT|>"

        out: dict[str, Any] = {"role": self.role, "content": content}

        # 🔴 **工具协议字段必须透传**（实测踩到：漏了这两个字段 → 上游 400）
        #    * assistant 带 tool_calls（模型要调工具）
        #    * tool 带 tool_call_id（把结果对应回调用）
        #    少任何一个，回填后的二次请求都会被上游拒绝。
        if self.role == ROLE_ASSISTANT and self.tool_calls:
            try:
                parsed = json.loads(self.tool_calls)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list) and parsed:
                out["tool_calls"] = parsed
                # ⚠️ 带 tool_calls 时 content 允许为空，但**不能是纯空字符串**
                #    某些上游要求 content 为 null
                if not content:
                    out["content"] = None

        if self.role == ROLE_TOOL and self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id

        if self.reasoning_content:
            out[thinking_field] = self.reasoning_content
        return out

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "reply_content": self.reply_content,
            "image_path": self.image_path,
            "image_desc": self.image_desc,
            "video_path": self.video_path,
            "video_desc": self.video_desc,
            "audio_path": self.audio_path,
            "forwarded_content": self.forwarded_content,
            "reasoning_content": self.reasoning_content,
            "reasoning_duration": self.reasoning_duration,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            **({"content_parts": deepcopy(self.content_parts)} if self.content_parts is not None else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d.get("role", ROLE_USER),
            content=d.get("content", ""),
            timestamp=float(d.get("timestamp", time.time())),
            reply_content=d.get("reply_content", ""),
            image_path=d.get("image_path", ""),
            image_desc=d.get("image_desc", ""),
            video_path=d.get("video_path", ""),
            video_desc=d.get("video_desc", ""),
            audio_path=d.get("audio_path", ""),
            forwarded_content=d.get("forwarded_content", ""),
            reasoning_content=d.get("reasoning_content", ""),
            reasoning_duration=int(d.get("reasoning_duration", 0)),
            tool_calls=d.get("tool_calls", ""),
            tool_call_id=d.get("tool_call_id", ""),
            content_parts=deepcopy(d.get("content_parts")),
        )


# ---------------------------------------------------------------------------
# 上游消息 → 内部消息（入口方向）
# ---------------------------------------------------------------------------


def from_upstream_message(d: dict[str, Any]) -> Message:
    """把 OpenAI 消息转成内部 Message，媒体原文另存供上游和 JSON 往返。"""
    if not isinstance(d, dict):
        raise ValueError("消息必须是对象")

    role = d.get("role", ROLE_USER)
    content = d.get("content", "")

    # content 的文本视图用于现有 UI；原始内容段用于媒体传递。
    content_parts = deepcopy(content) if isinstance(content, list) else None
    image_desc = ""
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                texts.append(str(part.get("text", "")))
            elif ptype == "image_url":
                url = str((part.get("image_url") or {}).get("url", ""))
                if url.startswith("data:"):
                    image_desc = "[图片: base64 data URL]"
                elif url:
                    image_desc = f"[图片: {url}]"
        content = "\n".join(t for t in texts if t)

    calls = d.get("tool_calls") if role == ROLE_ASSISTANT else None
    msg = Message(
        role=str(role), content=content if content is None else str(content),
        image_desc=d.get("image_desc") or image_desc, content_parts=content_parts,
        image_path=d.get("image_path") or "",
        audio_path=d.get("audio_path") or "",
        video_path=d.get("video_path") or "",
        video_desc=d.get("video_desc") or "",
        tool_calls=json.dumps(calls, ensure_ascii=False) if isinstance(calls, list) else "",
        tool_call_id=str(d.get("tool_call_id") or "") if role == ROLE_TOOL else "",
        reasoning_content=d.get("reasoning_content") or "",
    )
    msg.validate()
    return msg


def build_upstream_messages(
    messages: list[Message],
    *,
    thinking_field: str = "reasoning_content",
    inject_system_markers: bool = False,
) -> list[dict[str, Any]]:
    """把内部消息列表转成上游要的数组，**过滤掉不该发的**。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        converted = m.to_upstream(
            thinking_field=thinking_field,
            inject_system_markers=inject_system_markers,
        )
        if converted is not None:
            out.append(converted)
    return out
