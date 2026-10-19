# -*- coding: utf-8 -*-
"""`astrbot.core.utils.quoted_message` 兼容层 —— 引用消息解析。

⭐ 依据：真 AstrBot `core/utils/quoted_message/`（v4.27.5，只读参考）。

## 子模块

| 模块 | 类 | 陪伴用在哪 |
|---|---|---|
| `chain_parser` | `OneBotPayloadParser` / `ReplyChainParser` | `astrbot_host/core_reply_media.py:122` |
| `image_resolver` | `ImageResolver` | 同上 `:126` |
| `onebot_client` | `OneBotClient` | 同上 `:64/:89/:119` |

## 为什么不能省（陪伴 7.0.0 的语义）

用户在私聊里**引用一张图**说"这张怎么样"时，陪伴要：
1. `OneBotClient.get_msg(message_id)` 取回被引用那条消息的原始 payload；
2. `OneBotPayloadParser().parse_get_msg_payload(raw)` 抽出 `image_refs`；
3. `ImageResolver(...).resolve_for_llm(refs)` 把引用换成**模型能吃的地址**。

⚠️ 任何一步缺失，引用图就走不通 —— 而**不报错**（陪伴把它包在
`try/except` 里，失败就当成"没有引用图"）⇒ 用户只感觉"它看不见我发的图"。
这正是项目铁律说的**静默降级**，所以这三个模块的语义要尽量对齐真实现。

## 与真实现的差异（如实记录）

* 真实现的 `settings`（`QuotedMessageParserSettings`）控制「要不要在 action 失败时
  warn」「图片地址白名单」等策略。我们用**默认值**，不暴露配置面 ——
  陪伴也没有传自定义 settings（`core_reply_media.py` 全是默认构造）。
* `ImageResolver` 真实现会把 base64 / url / 本地文件统一成可投喂的形式，
  并对**非图片**做拒绝。这里照做，但**拒绝时记日志**
  （真实现是静默过滤 —— 那是他们的问题，我们不继承）。
"""
from __future__ import annotations

__all__ = ["chain_parser", "image_resolver", "onebot_client"]
