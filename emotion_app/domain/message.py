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
        inject_system_markers: bool = True,
    ) -> dict[str, Any] | None:
        """转成上游要的 `{role, content}`。

        Args:
            thinking_field: 思维链字段名（爱语可配置，默认 `reasoning_content`）。
            inject_system_markers: system 消息是否包
                `<|SYSTEM_INJECT|>…<|END_INJECT|>`（爱语实测行为）。

        Returns:
            None 表示**这条不该发给上游**（如内部 reasoning 角色）。
        """
        if self.role in UPSTREAM_EXCLUDED_ROLES:
            return None

        content = self.content
        if self.role == ROLE_SYSTEM and inject_system_markers and content:
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
        )


# ---------------------------------------------------------------------------
# 上游消息 → 内部消息（入口方向）
# ---------------------------------------------------------------------------


def from_upstream_message(d: dict[str, Any]) -> Message:
    """把 OpenAI 形态的消息转成内部 Message。

    只取 `role`/`content` —— 因为 OpenAI 形状本身就只有这些。
    其余字段（图片/语音/思维链）在我们自己的 UI 里才有。
    """
    if not isinstance(d, dict):
        raise ValueError("消息必须是对象")

    role = d.get("role", ROLE_USER)
    content = d.get("content", "")

    # content 可能是数组（多模态）：抽出纯文本，图片另存
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

    msg = Message(role=str(role), content=str(content), image_desc=image_desc)
    msg.validate()
    return msg


def build_upstream_messages(
    messages: list[Message],
    *,
    thinking_field: str = "reasoning_content",
    inject_system_markers: bool = True,
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
