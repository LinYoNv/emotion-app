# -*- coding: utf-8 -*-
"""模型供应商配置存储 —— **两层结构**（契约 `docs/ASTRBOT_PROVIDER_CONTRACT.md` §2/§3/§7）。

## 两条纪律（都是真实事故/实测背景）

1. 🔴 **落盘在我们的数据目录**（`data/providers.json`，相对运行目录），
   **绝不写 `/opt/astrbot`** —— 那是别人正在跑的 AstrBot 生产目录。
2. 🔴 **损坏不静默**：文件缺失 = 正常（首次使用）；文件坏了 = **记 ERROR 并按空配置继续**。
   迁移旧格式同理：**落盘 + 记 INFO**，绝不静默改用户数据。

## 为什么是两层（我原来做成单层，这就是"功能不对"的根源）

真实契约（从本机在跑的 AstrBot v4.27.5 实测抓下来）是：

    provider_sources   ← 端点层：一个 API 端点（api_base + key + timeout + proxy + custom_headers）
          ↑ 1 : N
    providers          ← 模型层：挂在某个端点下的模型（id 形如 "源id/模型名"）

    默认值 default_provider_id = "源id/模型名"（真实值 "deepseek/deepseek-v4-flash"）

单层 `{id,name,model,base_url,api_key,timeout}` **表达不了**这个：
同一个端点下挂 5 个模型时，用户要在面板上把地址和 key 抄 5 遍 —— 改一次端点要改 5 处。

## 两类字段：`provider_type`（用途）与 `type`（适配器协议）

🔴 这两个字段**长得像、管的事完全不同**，混起来会让"能存"和"能跑"两件事分不清：

| 字段 | 含义 | 取值 | 谁校验 | 非法/跑不了时 |
|---|---|---|---|---|
| `provider_type` | **用途分类**：这条源拿来干什么 | `chat_completion` / `text_to_speech` / `speech_to_text` / `embedding` | `validate()` **白名单** | **400** `unsupported_provider_type` |
| `type` | **适配器协议**：怎么调这个端点 | `openai_chat_completion` / `anthropic_chat_completion` / `gemini_tts` / … | **不校验** | 存得下；用 `adapter_is_executable()` 判断能不能真跑 |

* 支持的四类（契约 §1）：`PROVIDER_TYPE_CHAT` / `PROVIDER_TYPE_TTS` / `PROVIDER_TYPE_STT` /
  `PROVIDER_TYPE_EMBEDDING` = `SUPPORTED_PROVIDER_TYPES`；
* 如实拒绝的三类：`rerank` / `agent_runner` / `image_generation`
  = `KNOWN_UNSUPPORTED_PROVIDER_TYPES`（`agent_runner` **永远**不支持）；
* `type` **不做白名单**（契约 §1）：用户能从预设里选 `anthropic_chat_completion` 并存下来
  —— **只保存配置、不执行**。能不能跑只有 `adapter_is_executable(type)` 说了算
  （白名单就两条：`openai_chat_completion` / `openai_responses`）。
  厂商预设目录见 `infra/provider_presets.py`（50 条，每条带 `executable_by_us`）。

## 两类 id，两套规则（⚠️ 这里踩过一个真 bug，别再改回去）

| 对象 | 规则 | 真实值样例 |
|---|---|---|
| `provider_sources[].id` | 允许**中文**、字母数字 `_.-`；**不允许 `/`**（它是模型 id 的前缀，含 `/` 会歧义）；≤64 字符 | `mx哈基米`、`xy_哈基米` |
| `providers[].id` | **必须** `<源id>/<模型名>` —— **必须允许 `/`** | `deepseek/deepseek-v4-flash` |

⚠️ 旧版只有一条 `^[A-Za-z0-9_.-]{1,64}$`，它**既不允许中文、也不允许 `/`**
→ 真实形态的两层配置会被整条判为非法 → **装配出 0 个供应商**（实测复现过）。
所以这里拆成 `SOURCE_ID_PATTERN` / `MODEL_ID_PATTERN` 两条规则。

## 写出口只有一个：`public_view()`

🔴 `api_key` 落盘可明文（与 `.env` 同级的本机文件），**但绝不进任何 API 响应**。
所以**没有**一个"顺手把 key 也带上"的 `to_dict()`：
想落盘只能显式调 `to_storage_dict()`，想回响应只能调 `public_view()`。
（先例：`emotion_app/infra/config.py:128` 的 `UpstreamConfig.public_view()`。）

## 写入是原子的

`_write_all()` 走"同目录临时文件 → `fsync` → `os.replace`"。
直接 `path.write_text()` 在写一半时崩溃会留下**半截 JSON**，
而那种文件下次启动只能按"损坏"处理 —— 用户的全部供应商配置一次性丢失。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)

#: 落盘位置（**相对运行目录**，与 `plugin_host._load_provider_config()` 约定一致）。
DEFAULT_PATH = "data/providers.json"

#: 配置结构版本。2 = 两层（`provider_sources` + `providers`）；
#: 读到缺失/1 会**自动迁移**（见 `migrate_v1_to_v2`）。
SCHEMA_VERSION = 2

#: 🔴 源 id：允许中文（真实站里有 `mx哈基米`），但**不允许 `/` 和空白**。
#: `/` 必须挡住：它是模型 id 的分隔符，源 id 里再有 `/` 就分不清前缀到底到哪。
SOURCE_ID_PATTERN = re.compile(r"^[^\s/]{1,64}$")

#: 🔴 模型 id：**必须有 `/`**，形式是 `<源id>/<模型名>`。
#: 模型名里**允许**再出现 `/`（真实模型名就这样：`BAAI/bge-m3`、`meta-llama/Llama-3`），
#: 所以这里只要求"第一段非空 + 后面还有内容"，前缀是否等于 `provider_source_id`
#: 由 `ProviderModel.validate_against_sources()` 单独校验（它需要知道有哪些源）。
#: ⚠️ 两边都用 `\S`（**不要**用 `.` + `re.DOTALL`）：DOTALL 下 `.` 会匹配换行，
#:    于是 `"syuan/m\n"` 也能 `fullmatch` 通过 —— 带换行的 id 会被写进配置。
#:    （这是判据 `test_id_validation_uses_fullmatch_not_match` 抓出来的真 bug：
#:     源 id 用 `[^\s/]` 天然挡住了换行，模型 id 用 `.+` + DOTALL 没挡住。）
MODEL_ID_PATTERN = re.compile(r"^[^\s/]+/\S+$")

#: ★ 用途分类（`provider_type`）—— 我们现在支持**四类**（契约 `CONDUCTOR §1`）：
#: 对话、语音合成、语音识别、嵌入。
#: ⚠️ **加类型时要顺着它流过每一层**（2026-09-21 N0 事故的教训）：
#:    存储 → `flatten_two_layer` 展平 → 宿主 `ProviderManager` → 插件消费方。
#:    只改校验和界面 = 会把新类型混进对话供应商列表（实测踩过）。
PROVIDER_TYPE_CHAT = "chat_completion"
PROVIDER_TYPE_TTS = "text_to_speech"
PROVIDER_TYPE_STT = "speech_to_text"
PROVIDER_TYPE_EMBEDDING = "embedding"
SUPPORTED_PROVIDER_TYPES: tuple[str, ...] = (
    PROVIDER_TYPE_CHAT,
    PROVIDER_TYPE_TTS,
    PROVIDER_TYPE_STT,
    PROVIDER_TYPE_EMBEDDING,
)

#: 真实 AstrBot 里存在、但**我们明确不支持**的分类。
#: ⚠️ 单独列出来是为了把 400 的文案说准（"本平台暂不支持该类型" 而不是 "未知类型"）——
#:    如实拒绝，**不假装支持**（作者验收判据③）。
#:
#: * `speech_to_text`：**已从本表移出**（2026-09-21，情绪确认"先只做配置层"）→
#:   现在**存得进**，`provider_presets` 里那 3 条 STT 预设也随之下发。
#:   ⚠️ 但"存得进" ≠ "能执行" —— 3 条 STT 适配器我们**一个都跑不了**，
#:   由 `adapter_is_executable()` 如实标注 `executable_by_us=false`。
#: * `agent_runner`：**永远不支持**（情绪明确：情绪app 不需要 agent 能力）。
KNOWN_UNSUPPORTED_PROVIDER_TYPES: tuple[str, ...] = (
    "rerank",
    "agent_runner",
    "image_generation",
)

#: 新建源时的默认值（照真实契约的取值）。
DEFAULT_VENDOR = "openai"
DEFAULT_ADAPTER_TYPE = "openai_chat_completion"
DEFAULT_TIMEOUT = 60.0

# ---------------------------------------------------------------------------
# ★ 适配器协议（`type`）与「我们能不能执行它」
# ---------------------------------------------------------------------------
#
# 🔴 这里有两件事**必须分开**，混起来就是本轮最容易犯的错：
#
#   | 字段 | 是什么 | 谁校验它 | 非法时 |
#   |---|---|---|---|
#   | `provider_type` | 用途分类（对话/语音/嵌入）| `ProviderSource.validate()` | **400** |
#   | `type` | 适配器协议（怎么调这个端点）| **不校验** | 存得下，但跑不了 |
#
# 为什么 `type` **不做白名单拒绝**（契约 §1 明确）：
#   用户可以从预设里选 `anthropic_chat_completion` / `gemini_tts` / `openai_embedding`
#   并存下来 —— 我们**只保存配置、不执行**。那正是"能力诚实"要求的行为：
#   预设照实列出、字段照填，只是每一项都带 `executable_by_us`。
#   若在这里 400 拒绝，用户会发现"面板上明明列着这个预设，却一条都存不进去"
#   —— 比"存下来但不能跑"糟得多（前者是功能缺失，后者是如实降级）。
#
# 所以"能不能跑"**只**由 `adapter_is_executable()` 回答，
# 由真正要执行的调用方（连通性测试 / 会话选模型 / 插件取用）**如实返回不可用**。

#: OpenAI 兼容的对话格式：请求打 `/chat/completions`。
ADAPTER_TYPE_OPENAI_CHAT = "openai_chat_completion"
#: OpenAI 的 Responses 格式：请求打 `/responses`（**不是** `/chat/completions`）。
ADAPTER_TYPE_OPENAI_RESPONSES = "openai_responses"

#: ★ **可执行的适配器白名单** —— 我们能真跑起来的只有这两种。
EXECUTABLE_ADAPTER_TYPES: tuple[str, ...] = (
    ADAPTER_TYPE_OPENAI_CHAT,
    ADAPTER_TYPE_OPENAI_RESPONSES,
)


def is_chat_provider_type(provider_type: str) -> bool:
    """这条源是不是**对话**类？—— 决定它能不能进宿主的**对话**供应商池。

    🔴🔴 **为什么单独抽一个函数**（2026-09-21 N0 事故的直接产物）：
        "能不能进对话池"这件事有**多条写入路径**：
          ① 服务**启动装配** → `provider_bridge.flatten_two_layer()`
          ② 配置**热同步**   → `api_providers._register_model()`
        我最初只在 ① 加了过滤 —— **单测全绿，但真环境仍然泄漏**，
        因为 ② 根本不经过 `flatten_two_layer`。

        → 抽成**唯一判据**，让每条路径都调它；将来加第 ③ 条路径时也有东西可依。
        **通则:同一概念有多条写入路径时,判据必须唯一,且每条路径各自有判据守着。**

    ⚠️ 缺失/空串按**对话**处理 —— 与 `ProviderStore` 的默认值一致
       （迁移过来的老数据没有 `provider_type` 字段，那些都是对话源）。
    """
    return (provider_type or PROVIDER_TYPE_CHAT).strip() == PROVIDER_TYPE_CHAT


def adapter_is_executable(adapter_type: str) -> bool:
    """这个适配器协议，我们**能不能真执行**？

    ⚠️ 判据是**字面量相等**，不是"名字里有没有 openai"：
       * `openrouter_chat_completion` / `groq_chat_completion` 这类不是 `openai_*`，
         但它们同样打 `/chat/completions` 形态 → 在 `provider_presets` 里
         这些预设的 `type` 本身就被标成了 `openai_chat_completion`，所以**能跑**；
       * 反过来 `anthropic_chat_completion` 名字里没有 openai、协议也不同 → **不能跑**。

    ⚠️ 只做 `strip()`，**不做大小写归一**：适配器名是**协议标识**（真实 AstrBot 里
       全小写下划线），把它当"用户输入"宽容处理反而会造出 `OpenAI_Chat_Completion`
       这种谁也匹配不上的配置。判据 `test_adapter_is_executable_is_strict` 盯着这条。

    Args:
        adapter_type: `ProviderSource.type` 的值（缺失时传 `""`）。

    Returns:
        `True` 表示我们可以拿它去打上游；`False` 表示**只能保存配置**。
    """
    if not isinstance(adapter_type, str):
        log.warning(
            "adapter_is_executable 收到非字符串（%s）→ False（按不可执行处理）",
            type(adapter_type).__name__,
        )
        return False
    return adapter_type.strip() in EXECUTABLE_ADAPTER_TYPES

#: 🔴 key 的**掩码占位符**（固定 8 个点，**不泄露真实 key 长度**）。
#: 响应里 `key` 字段用它占位，这样：①形状是列表（照真实契约）；
#: ②前端能显示"这里有一把 key"；③**内容不是凭据**。
#: 若前端把这个占位符原样提交回来，写入侧会识别成"没填"（见 `_is_key_mask`）——
#: 否则"用户只改了个超时、却把 key 改成了 8 个点"就成了静默毁数据。
KEY_MASK = "•" * 8

#: 源可改字段（`id` 单列处理：它是主键）。
SOURCE_WRITABLE_FIELDS = (
    "provider",
    "type",
    "provider_type",
    "key",
    "api_base",
    "timeout",
    "proxy",
    "custom_headers",
    "enable",
)

#: 模型可改字段。
MODEL_WRITABLE_FIELDS = ("provider_source_id", "model", "enable", "modalities")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """供应商配置错误的基类。

    ⚠️ `reason` 是**给 API 层用的机器可读原因**（映射到响应的 `reason`/`error.type`），
       不是给用户看的文案 —— 文案在 `message` 里，必须**说清是哪一项不合法**。
    """

    reason = "provider_error"

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        if reason:
            self.reason = reason


class ProviderValidationError(ProviderError, ValueError):
    """校验不通过（非法 id / 空 model / 非 http api_base / timeout <= 0 / 悬空引用 …）。

    继承 `ValueError` 是刻意的：既有的调用点普遍用 `except ValueError` 兜校验错误，
    这样两边都能接住。
    """

    reason = "invalid_provider"


class UnsupportedProviderType(ProviderValidationError):
    """`provider_type` 不是我们支持的分类（如 embedding / tts）。

    ⚠️ 单独一个子类，是为了让 API 层能把它映射成**专门的**原因码
       （`unsupported_provider_type`），前端可以据此显示"本平台没有这个能力"，
       而不是笼统的"参数非法"。
    """

    reason = "unsupported_provider_type"


class ProviderNotFound(ProviderError, KeyError):
    """对象不存在。"""

    reason = "not_found"

    def __str__(self) -> str:
        # ⚠️ `KeyError` 的默认 `str()` 会给消息加一对引号（`"'x 不存在'"`），
        #    回给用户很难看，所以覆盖掉。
        return self.message


class ProviderStoreCorrupt(ProviderError):
    """配置文件损坏 —— **只有 `load_strict()` 会抛**。

    ⚠️ `load()` 刻意**不抛**：配置坏了不该让整个服务/接口挂掉，
       但必须**记 ERROR**（见 `_read_raw()`）。
    """

    reason = "corrupt_file"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _brief(value: Any, limit: int = 60) -> str:
    """把可能很长的值截断后再放进错误消息（也顺手挡住超长输入刷屏）。"""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _is_key_mask(value: Any) -> bool:
    """判断一个提交上来的"key"是不是**掩码占位符**而不是真凭据。

    为什么需要它：我们的响应里 `key` 用 `KEY_MASK`（一串点）占位，
    前端若原样回填、用户又只改了个超时就保存 → 后端会把 8 个点当成新 key 存下来，
    **真正的 key 被静默替换**（下次调用上游才 401，最难查的那类）。
    所以写入侧统一把"全是掩码字符"的值当成"没填"。

    ⚠️ 只在**全是**掩码字符且长度 ≥4 时才算 —— 宁可少杀不可错杀
       （真实 key 不可能长这样，但不能把含点号的正常 key 误判）。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    return len(text) >= 4 and all(ch in "•*·●" for ch in text)


def _coerce_key_list(raw: Any, *, where: str) -> list[str]:
    """把 `key` 归一化成**字符串列表**（真实契约里它就是列表）。

    兼容三种历史形态：`["sk-a","sk-b"]`（真实契约）、`"sk-a"`（旧单层格式）、`None`。
    空串/纯空白项被丢掉并**留痕**（复制粘贴常带尾随换行）。
    """
    if raw is None:
        return []
    items: list[Any]
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        items = [raw]
    else:
        raise ProviderValidationError(f"{where} 的 key 必须是字符串或字符串数组")

    out: list[str] = []
    for item in items:
        if item is None:
            continue
        if not isinstance(item, str):
            raise ProviderValidationError(f"{where} 的 key 数组里只能放字符串，收到 {type(item).__name__}")
        text = item.strip()
        if not text:
            continue
        if text != item:
            log.warning("%s 的 key 首尾有空白字符，已去除（否则上游鉴权会失败）", where)
        if _is_key_mask(text):
            log.warning(
                "%s 提交的 key 是掩码占位符（响应里回显的那串点）—— 已忽略，"
                "按【不修改】处理；请填真实 key",
                where,
            )
            continue
        out.append(text)
    return out


def _coerce_timeout(raw: Any, *, where: str) -> float:
    """timeout 归一化。类型错**不能**变成 500（`"60" > 0` 在 Python 3 里直接 TypeError）。"""
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderValidationError(
            f"{where} 的 timeout 必须是数字（秒）；当前 {raw!r}"
        ) from exc


def _coerce_headers(raw: Any, *, where: str) -> dict[str, str]:
    """`custom_headers` 归一化成 `dict[str,str]`（真实契约里它是对象）。"""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise ProviderValidationError(f"{where} 的 custom_headers 必须是 JSON 对象（键值对）")
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = "" if v is None else str(v)
    return out


def _coerce_modalities(raw: Any, *, where: str) -> list[str]:
    """`modalities` 归一化成字符串列表（如 `["text","image"]`）。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [m.strip() for m in raw.split(",") if m.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(m).strip() for m in raw if str(m or "").strip()]
    raise ProviderValidationError(f"{where} 的 modalities 必须是数组，如 [\"text\"]")


# ---------------------------------------------------------------------------
# 数据模型：端点层
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProviderSource:
    """一个供应商端点（契约 §2 的 JSON 形状）。

    ⚠️ 用 `@dataclass(slots=True)` 与项目既有风格一致
       （`emotion_app/infra/config.py:42` 的 `ServerConfig` 等）。
    """

    id: str = ""
    #: 厂商实现（openai / google / deepseek …）。我们只有一个"OpenAI 兼容"实现，
    #: 但**照契约保留这个字段**（面板要显示、插件可能读）。
    provider: str = DEFAULT_VENDOR
    #: 适配器类型（真实值 `openai_chat_completion`）。
    type: str = DEFAULT_ADAPTER_TYPE
    #: ★ 分类：我们只支持 `chat_completion`。
    provider_type: str = PROVIDER_TYPE_CHAT
    #: 🔴 明文 key **列表**（真实契约如此）。落盘可明文，**绝不进响应**。
    key: list[str] = field(default_factory=list)
    api_base: str = ""
    timeout: float = DEFAULT_TIMEOUT
    proxy: str = ""
    custom_headers: dict[str, str] = field(default_factory=dict)
    enable: bool = True

    # ---- 校验 ----

    def validate(self) -> None:
        """逐项校验，每条失败都**说清是哪一项、当前值是什么**。

        Raises:
            ProviderValidationError / UnsupportedProviderType: 任一项不合法。
        """
        if not self.id:
            raise ProviderValidationError("provider source 的 id 不能为空（它是模型的 id 前缀）")
        # ⚠️ 用 `fullmatch` 而不是 `match("...$")`：`$` 会匹配结尾换行符之前的位置，
        #    那样 `"abc\n"` 也能过（判据里有专门一条盯这个）。
        if not SOURCE_ID_PATTERN.fullmatch(self.id):
            raise ProviderValidationError(
                "provider source 的 id 允许中文/字母/数字/下划线/点/连字符，长度 1..64，"
                "且**不能包含斜杠或空白**（斜杠是模型 id 的分隔符，含它会分不清前缀）；"
                f"当前 {_brief(self.id)}"
            )

        # 🔴 provider_type 白名单（**这是唯一被白名单拒绝的字段**）。
        #    ⚠️ 必须**如实拒绝**（情绪明确说情绪app 不需要 Agent 能力）——
        #       假装支持会在插件真去调语音/重排时炸在更远的地方，更难查。
        #    ⚠️ 但 `type`（适配器协议）**不在这里校验**：用户能从预设里选
        #       `anthropic_chat_completion` 并存下来（只保存不执行），
        #       那由 `adapter_is_executable()` 回答（见本文件顶部的两类字段说明）。
        if self.provider_type not in SUPPORTED_PROVIDER_TYPES:
            if self.provider_type in KNOWN_UNSUPPORTED_PROVIDER_TYPES:
                raise UnsupportedProviderType(
                    f"本平台暂不支持该类型：provider_type={self.provider_type}"
                    f"（支持的是 {list(SUPPORTED_PROVIDER_TYPES)}）。"
                    "情绪app 不做 Agent 能力；重排（rerank）、"
                    "生图（image_generation）本轮也不做。"
                )
            raise UnsupportedProviderType(
                f"未知的 provider_type={_brief(self.provider_type)}；"
                f"本平台支持 {list(SUPPORTED_PROVIDER_TYPES)}"
            )

        if not self.api_base.startswith(("http://", "https://")):
            # 照 `emotion_app/infra/config.py:123` 的同款判据
            raise ProviderValidationError(
                "api_base 必须以 http:// 或 https:// 开头"
                "（OpenAI 兼容端点通常以 /v1 结尾）；"
                f"当前 {_brief(self.api_base)}"
            )
        # ⚠️ 类型判断必须在比较之前：`"60" > 0` 在 Python 3 里直接抛 TypeError
        #    （那会变成 500 而不是"说清原因的 400"）。
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise ProviderValidationError(f"timeout 必须是数字（秒）；当前 {self.timeout!r}")
        if not (self.timeout > 0):
            raise ProviderValidationError(f"timeout 必须是大于 0 的秒数；当前 {self.timeout!r}")

    # ---- 出口（两个，用途严格分开）----

    def to_storage_dict(self) -> dict[str, Any]:
        """**落盘**用（含明文 key）。

        🔴 名字刻意不叫 `to_dict()`：避免有人"顺手" `jsonify(s.to_dict())`
           把凭据回给浏览器。要回响应，用 `public_view()`。
        """
        return {
            "id": self.id,
            "provider": self.provider,
            "type": self.type,
            "provider_type": self.provider_type,
            "key": list(self.key),
            "api_base": self.api_base,
            "timeout": self.timeout,
            # 空 proxy / 空 headers 也照写（保持形状稳定，前端不用做 undefined 判断）
            "proxy": self.proxy,
            "custom_headers": dict(self.custom_headers),
            "enable": self.enable,
        }

    def public_view(self) -> dict[str, Any]:
        """**响应/日志**用 —— key 只以掩码列表 + 布尔出现（契约 §2 冻结）。

        * `key_configured`：有没有配 key（**唯一的判定依据**，前端用它显示"已设置"）；
        * `key`：**掩码占位符列表**（`["••••••••"]`），保持列表形状但不含凭据；
          没配就是 `[]`。
        """
        return {
            "id": self.id,
            "provider": self.provider,
            "type": self.type,
            "provider_type": self.provider_type,
            "key": [KEY_MASK] if self.key else [],
            "key_configured": bool(self.key),
            "api_base": self.api_base,
            "timeout": self.timeout,
            "proxy": self.proxy,
            "custom_headers": dict(self.custom_headers),
            "enable": self.enable,
        }

    @classmethod
    def from_dict(cls, d: Any) -> ProviderSource:
        """从 JSON 对象构造（**宽容读取，严格校验**）。

        ⚠️ 这里只做类型转换，**不做业务校验** —— 校验统一在 `validate()`，
           这样"新建"和"改一条"用的是**同一套**规则（不会出现两处规则漂移）。
        """
        if not isinstance(d, dict):
            raise ProviderValidationError("provider source 必须是 JSON 对象")

        fields = {
            "id": d.get("id"),
            "provider": d.get("provider"),
            "type": d.get("type"),
            "api_base": d.get("api_base"),
            "proxy": d.get("proxy"),
        }
        cleaned = {k: str(v or "").strip() for k, v in fields.items()}
        # ⚠️ 归一化本身是对的（粘贴常带尾随换行/空格），但**不能静默** ——
        #    否则用户看到面板上的值跟自己填的不一样却找不到原因。
        for key, original in fields.items():
            if original is not None and str(original) != cleaned[key]:
                log.warning(
                    "%s 首尾有空白字符，已去除：%r → %r",
                    key,
                    _brief(original),
                    _brief(cleaned[key]),
                )

        raw_type = d.get("provider_type")
        provider_type = str(raw_type).strip() if raw_type not in (None, "") else PROVIDER_TYPE_CHAT

        # `enable` 默认 True（照真实契约：新建的源默认启用）
        raw_enable = d.get("enable", True)
        enable = True if raw_enable is None else bool(raw_enable)

        return cls(
            id=cleaned["id"],
            provider=cleaned["provider"] or DEFAULT_VENDOR,
            type=cleaned["type"] or DEFAULT_ADAPTER_TYPE,
            provider_type=provider_type,
            key=_coerce_key_list(d.get("key"), where=f"provider source {_brief(cleaned['id'])}"),
            api_base=cleaned["api_base"],
            timeout=_coerce_timeout(
                d.get("timeout"), where=f"provider source {_brief(cleaned['id'])}"
            ),
            proxy=cleaned["proxy"],
            custom_headers=_coerce_headers(
                d.get("custom_headers"), where=f"provider source {_brief(cleaned['id'])}"
            ),
            enable=enable,
        )


# ---------------------------------------------------------------------------
# 数据模型：模型层
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProviderModel:
    """挂在某个源下面的一个模型（契约 §3 的"引用式"形状）。

    ⚠️ 它**自己不带连接信息**（base_url/key 在源上）—— 这正是两层的意义：
       同一个端点下挂 N 个模型，端点信息只存一份。
    """

    #: ★ 形如 `源id/模型名`（真实值 `deepseek/deepseek-v4-flash`）。
    id: str = ""
    #: ★ 指向 `provider_sources[].id`。悬空引用是最难查的 bug → 必须校验存在性。
    provider_source_id: str = ""
    model: str = ""
    enable: bool = True
    modalities: list[str] = field(default_factory=list)

    # ---- 校验 ----

    def validate(self) -> None:
        """字段级校验（**不**查源是否存在 —— 那需要整个 store 的上下文）。"""
        if not self.id:
            raise ProviderValidationError(
                "模型 id 不能为空（契约要求形如 `<源id>/<模型名>`，如 deepseek/deepseek-v4-flash）"
            )
        if not MODEL_ID_PATTERN.fullmatch(self.id):
            raise ProviderValidationError(
                "模型 id 必须是 `<源id>/<模型名>` 形式（**必须包含斜杠**）；"
                f"当前 {_brief(self.id)}"
            )
        if not self.provider_source_id:
            raise ProviderValidationError(
                f"模型 {_brief(self.id)} 缺少 provider_source_id —— "
                "它必须指向一个已存在的 provider source（模型自己不带地址和 key）"
            )
        head = self.id.split("/", 1)[0]
        if head != self.provider_source_id:
            # 🔴 这条不是洁癖：`default_provider_id` 与插件都按"前缀 = 源"来解析，
            #    前缀与 provider_source_id 不一致时，面板说挂在 A 源、实际取 B 源的地址。
            raise ProviderValidationError(
                f"模型 id 的前缀必须等于 provider_source_id："
                f"id={_brief(self.id)} 的前缀是 {_brief(head)}，"
                f"但 provider_source_id={_brief(self.provider_source_id)}"
            )
        if not self.model:
            raise ProviderValidationError(
                f"模型 {_brief(self.id)} 的 model 不能为空 —— "
                "插件按它选模型，没有模型名就无法调用"
            )

    def validate_against_sources(self, source_ids: set[str]) -> None:
        """🔴 **悬空引用检查**（必须能失败的那种判据）。

        模型的 `provider_source_id` 指向一个不存在的源时：
        `api_base`/`key` 全都取不到 → 这个模型**永远调不通**，
        而配置看起来"配好了"（最贵的那类静默失败）。所以写入时直接 400。
        """
        self.validate()
        if self.provider_source_id not in source_ids:
            raise ProviderValidationError(
                f"模型 {_brief(self.id)} 指向不存在的 provider source："
                f"{_brief(self.provider_source_id)}；可用的源是 {sorted(source_ids)}"
            )

    # ---- 出口 ----

    def to_storage_dict(self) -> dict[str, Any]:
        """落盘用（模型层没有凭据，所以这个形状可以直接回响应）。"""
        return {
            "id": self.id,
            "provider_source_id": self.provider_source_id,
            "model": self.model,
            "enable": self.enable,
            "modalities": list(self.modalities),
        }

    def public_view(self) -> dict[str, Any]:
        """响应/日志用 —— 与 `to_storage_dict()` 同形（模型层不含凭据）。"""
        return self.to_storage_dict()

    @classmethod
    def from_dict(cls, d: Any) -> ProviderModel:
        """从 JSON 对象构造（宽容读取，严格校验交给 `validate*`）。"""
        if not isinstance(d, dict):
            raise ProviderValidationError("模型必须是 JSON 对象")

        fields = {
            "id": d.get("id"),
            "provider_source_id": d.get("provider_source_id"),
            "model": d.get("model"),
        }
        cleaned = {k: str(v or "").strip() for k, v in fields.items()}
        for key, original in fields.items():
            if original is not None and str(original) != cleaned[key]:
                log.warning(
                    "模型 %s 首尾有空白字符，已去除：%r → %r",
                    key,
                    _brief(original),
                    _brief(cleaned[key]),
                )

        raw_enable = d.get("enable", True)
        return cls(
            id=cleaned["id"],
            provider_source_id=cleaned["provider_source_id"],
            model=cleaned["model"],
            enable=True if raw_enable is None else bool(raw_enable),
            modalities=_coerce_modalities(d.get("modalities"), where=f"模型 {_brief(cleaned['id'])}"),
        )


# ---------------------------------------------------------------------------
# 迁移：v1（单层） → v2（两层）
# ---------------------------------------------------------------------------


def migrate_v1_to_v2(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把旧单层配置就地迁移成两层。**纯函数**（不改文件，落盘在 `_read_raw` 里做）。

    迁移规则**冻结**在契约 §7：

        旧 `{id,name,model,base_url,api_key,timeout}`
          → 一个**源**：`id`=旧 id、`api_base`=旧 base_url、`key=[旧 api_key]`、`enable=true`
          + 一个**模型**：`id`=`<源id>/<model>`、`provider_source_id`=源id、`model`=旧 model
        `default_provider_id` 同步改成 `<源id>/<model>`。

    Returns:
        `(迁移后的 data, 迁移说明列表)` —— 说明列表用来记日志（**不许静默改用户数据**）；
        为空表示"没有需要迁移的东西"。
    """
    notes: list[str] = []
    raw_providers = data.get("providers")
    if not isinstance(raw_providers, list):
        raw_providers = []

    sources: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    legacy_to_new: dict[str, str] = {}

    for idx, item in enumerate(raw_providers):
        if not isinstance(item, dict):
            notes.append(f"第 {idx} 条不是 JSON 对象，已跳过（原值：{_brief(item)}）")
            continue

        sid = str(item.get("id") or "").strip()
        model_name = str(item.get("model") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        name = str(item.get("name") or "").strip()
        raw_key = item.get("api_key")

        if not sid:
            # 没有 id 就没法建源（模型 id 的前缀无从谈起）→ 只能如实报出来
            notes.append(f"第 {idx} 条旧供应商没有 id，无法迁移（原值：{_brief(item)}）")
            continue

        key_list: list[str] = []
        if isinstance(raw_key, str) and raw_key.strip():
            key_list = [raw_key.strip()]
        elif isinstance(raw_key, list):
            key_list = [str(k).strip() for k in raw_key if str(k or "").strip()]

        sources.append(
            {
                "id": sid,
                "provider": DEFAULT_VENDOR,
                "type": DEFAULT_ADAPTER_TYPE,
                "provider_type": PROVIDER_TYPE_CHAT,
                "key": key_list,
                "api_base": base_url,
                "timeout": float(item.get("timeout") or DEFAULT_TIMEOUT)
                if isinstance(item.get("timeout"), (int, float))
                and not isinstance(item.get("timeout"), bool)
                else DEFAULT_TIMEOUT,
                "proxy": "",
                "custom_headers": {},
                "enable": True,
            }
        )
        notes.append(f"端点 {sid!r} ← 旧供应商 {sid!r}（原 name={name!r}）")

        if not model_name:
            # 旧记录没有 model → 建不出模型（模型的 model 字段不能为空）。
            # ⚠️ 但**端点（含 key）保住了**，用户只需在面板上补一个模型名。
            notes.append(
                f"旧供应商 {sid!r} 没有 model，只迁移了端点；请在面板上给它加一个模型"
            )
            continue

        mid = f"{sid}/{model_name}"
        models.append(
            {
                "id": mid,
                "provider_source_id": sid,
                "model": model_name,
                "enable": True,
                "modalities": ["text"],
            }
        )
        legacy_to_new[sid] = mid

    old_default = str(data.get("default_provider_id") or "").strip()
    if not old_default:
        new_default = models[0]["id"] if models else ""
    elif old_default in legacy_to_new:
        new_default = legacy_to_new[old_default]
    elif old_default in {m["id"] for m in models}:
        new_default = old_default          # 已经是新形状（手写配置的宽容路径）
    else:
        # 悬空默认值：不静默，但也不擅自编一个 —— 如实记下来（下面会变成空默认）
        notes.append(
            f"默认供应商 {old_default!r} 在迁移后找不到对应模型 → 默认将留空，请在面板上重设"
        )
        new_default = ""

    migrated = dict(data)
    migrated["version"] = SCHEMA_VERSION
    migrated["provider_sources"] = sources
    migrated["providers"] = models
    migrated["default_provider_id"] = new_default
    return migrated, notes


def needs_migration(data: dict[str, Any]) -> bool:
    """判断读到的配置是不是旧格式（缺 `version` 或 `version < 2`）。"""
    raw_version = data.get("version")
    if raw_version in (None, ""):
        # 缺 version：只有**像**旧格式（providers 里是单层记录）才迁移，
        # 免得把一份手写的、恰好没写 version 的新格式配置又"迁"一遍。
        if isinstance(data.get("provider_sources"), list):
            return False
        return isinstance(data.get("providers"), list)
    try:
        return int(raw_version) < SCHEMA_VERSION
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class ProviderStore:
    """`data/providers.json` 的读写。

    设计取舍：
      * **无内存缓存**：每次操作都从磁盘读、改完立刻原子写回。
        自用场景（一个人、一个面板）没有性能压力，而缓存会引入
        "面板改了但插件读到的还是旧的"这类难查问题；
      * **read-modify-write 加锁**：HTTP 事件循环是单线程的，但
        `api_admin` 里已有 `asyncio.to_thread` 的先例，加锁成本极低，
        能避免将来"两个写并发 → 丢一条配置"。
      * 🔴 锁是 **RLock**：`_read_raw()` 在"发现旧格式要迁移"时会**落盘**，
        而它本身常常是被 `create()/update()` 在 `with self._lock` 里调到的 ——
        普通 `Lock` 会在这里**自锁死**。
    """

    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    # ---- 读 ----

    def _read_raw(self) -> tuple[dict[str, Any], bool]:
        """读原始 JSON。返回 `(data, corrupt)`。

        🔴 损坏处理是**明确**的（契约与项目铁律都要求）：
             文件不存在 → 空配置，**正常路径，不记 ERROR**（首次使用就是没有文件）；
             读取失败 / 空文件 / 非 JSON / 顶层不是对象 → **记 ERROR**，按空配置继续。
           `corrupt=True` 还带一个副作用：下一次写入前会先把坏文件另存为
           `providers.json.corrupt`，避免"用户加一个供应商 → 坏文件被覆盖 → 无从排查"。

        🔴 **旧格式在这里自动迁移并落盘**（契约 §7：自动、不丢数据、留日志）。
           选择"读的时候就迁"而不是"启动时迁一次"：装配层（`plugin_host`）和
           API 层走的都是 `load()`，任何一条路径先跑到都能完成迁移，
           不会出现"面板已经是新格式、插件还读到旧的"。
        """
        empty: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "default_provider_id": "",
            "provider_sources": [],
            "providers": [],
        }
        path = self.path
        if not path.is_file():
            return empty, False

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("供应商配置读取失败（按空配置继续）：%s → %s", path, exc)
            return empty, True

        if not text.strip():
            log.error("供应商配置是空文件（按空配置继续）：%s", path)
            return empty, True

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.error("供应商配置 JSON 损坏（按空配置继续）：%s → %s", path, exc)
            return empty, True

        if not isinstance(data, dict):
            log.error("供应商配置顶层不是 JSON 对象（按空配置继续）：%s", path)
            return empty, True

        if needs_migration(data):
            migrated, notes = migrate_v1_to_v2(data)
            # 🔴 **记日志**：不许静默改用户数据（契约 §7 明文要求 + 项目铁律）
            log.info(
                "供应商配置从 v1（单层）迁移到 v2（两层）：%s —— 迁移 %d 个端点 / %d 个模型",
                path,
                len(migrated["provider_sources"]),
                len(migrated["providers"]),
            )
            for note in notes:
                log.info("  迁移明细：%s", note)
            # 🔴 **落盘**：不落盘的话每次启动都重迁一遍，而且下次写入前
            #    用户看到的文件始终是旧格式（会以为迁移没生效）。
            #
            # ⚠️ 这里**直接写迁移函数产出的原始 dict**，不经过 dataclass 往返 ——
            #    迁移的首要目标是"**不丢数据**"，而 `from_dict()` 有可能对某条异常
            #    记录抛错；一旦抛错就会把**整份**已迁移数据退回旧格式（静默不落盘）。
            try:
                with self._lock:
                    payload = dict(migrated)
                    payload["version"] = SCHEMA_VERSION
                    self._write_payload(payload, corrupt=False)
                log.info("迁移后的两层配置已落盘：%s", path)
            except Exception:  # noqa: BLE001
                # 落盘失败**不能**让读取也失败（用户至少还能在面板上看到数据）
                log.error("迁移结果落盘失败（本次仍按迁移后的数据继续）：%s", path, exc_info=True)
            data = migrated

        return data, False

    def _read(
        self,
    ) -> tuple[list[ProviderSource], list[ProviderModel], str, dict[str, Any], bool]:
        """读成 `(sources, models, default_provider_id, 原始 data, corrupt)`。

        ⚠️ **单条不合法的记录保留而不是丢弃**：丢了就是静默数据损失；
           保留 + 记 WARNING，用户能在面板上看到它、改它或删它。
        """
        data, corrupt = self._read_raw()

        raw_sources = data.get("provider_sources")
        if raw_sources is None:
            raw_sources = []
        if not isinstance(raw_sources, list):
            log.error(
                "供应商配置的 provider_sources 不是数组（按空列表继续）：%s → %s",
                self.path,
                type(raw_sources).__name__,
            )
            raw_sources = []
            corrupt = True

        sources: list[ProviderSource] = []
        for idx, item in enumerate(raw_sources):
            try:
                s = ProviderSource.from_dict(item)
                s.validate()
            except ProviderValidationError as exc:
                # ⚠️ 不静默、也不丢：如实报出来，让用户去修/删
                log.warning(
                    "配置里第 %d 个 provider source 不合法（仍保留，可在面板上改或删）：%s",
                    idx,
                    exc,
                )
                try:
                    s = ProviderSource.from_dict(item)
                except ProviderValidationError:
                    continue          # 连类型都转不了（比如是个字符串）→ 只能丢
            sources.append(s)

        raw_models = data.get("providers")
        if raw_models is None:
            raw_models = []
        if not isinstance(raw_models, list):
            log.error(
                "供应商配置的 providers 不是数组（按空列表继续）：%s → %s",
                self.path,
                type(raw_models).__name__,
            )
            raw_models = []
            corrupt = True

        source_ids = {s.id for s in sources}
        models: list[ProviderModel] = []
        for idx, item in enumerate(raw_models):
            try:
                m = ProviderModel.from_dict(item)
                # 🔴 悬空引用**在读取时也要报**（不只写入口拦）：
                #    手编文件同样能造出悬空引用，静默留着 = 插件看到一个永远调不通的模型。
                m.validate_against_sources(source_ids)
            except ProviderValidationError as exc:
                log.warning(
                    "配置里第 %d 个模型不合法（仍保留，可在面板上改或删）：%s", idx, exc
                )
                try:
                    m = ProviderModel.from_dict(item)
                except ProviderValidationError:
                    continue
            models.append(m)

        default_id = str(data.get("default_provider_id") or "").strip()
        if default_id and all(m.id != default_id for m in models):
            # 悬空默认值：不静默，但也不擅自改文件（用户可能正在手编配置）
            log.warning(
                "默认供应商 id=%r 在配置里不存在（插件会解析不到）—— 请在面板上重设",
                default_id,
            )
        return sources, models, default_id, data, corrupt

    # ---- 公开读接口 ----

    def load(self) -> dict[str, Any]:
        """**契约读取**：返回 `{"version","provider_sources":[...],"providers":[...],
        "default_provider_id"}`（两层新格式）。

        ⚠️ 这是 `emotion_app/web/plugin_host.py` 依赖的最小契约
           （那边用它装配宿主的 `ProviderManager`）—— 形状别改。
           注意它**含明文 key**（宿主需要 key 才能调上游），
           所以它**只能**给进程内的装配层用，绝不能直接回响应。
        """
        sources, models, default_id, data, _ = self._read()
        out = dict(data)                     # 保留未知的顶层键（别丢用户手写的东西）
        out["version"] = SCHEMA_VERSION
        out["default_provider_id"] = default_id
        out["provider_sources"] = [s.to_storage_dict() for s in sources]
        out["providers"] = [m.to_storage_dict() for m in models]
        return out

    def load_strict(self) -> dict[str, Any]:
        """同 `load()`，但**文件损坏时抛 `ProviderStoreCorrupt`**。

        ⚠️ 给"需要知道配置到底有没有坏"的调用方用（诊断/`/health`）。
           管理 API 刻意用宽容的 `load()` —— 配置坏一个字段不该让面板打不开。
        """
        _, _, _, _, corrupt = self._read()
        if corrupt:
            raise ProviderStoreCorrupt(
                f"供应商配置损坏，已按空配置继续运行：{self.path}"
                "（加/改任意供应商前会把它另存为 .corrupt 备份）"
            )
        return self.load()

    def list_sources(self) -> list[ProviderSource]:
        return self._read()[0]

    def list_models(self, source_id: str | None = None) -> list[ProviderModel]:
        """列出模型；给 `source_id` 时只列该源下的（契约 §2 的
        `GET /provider-sources/<源id>/providers`）。"""
        models = self._read()[1]
        if source_id is None:
            return models
        return [m for m in models if m.provider_source_id == source_id]

    @property
    def default_provider_id(self) -> str:
        return self._read()[2]

    def get_source(self, source_id: str) -> ProviderSource | None:
        for s in self.list_sources():
            if s.id == source_id:
                return s
        return None

    def require_source(self, source_id: str) -> ProviderSource:
        s = self.get_source(source_id)
        if s is None:
            raise ProviderNotFound(
                f"provider source 不存在：{_brief(source_id)}"
                f"（现有：{[x.id for x in self.list_sources()]}）"
            )
        return s

    def get_model(self, model_id: str) -> ProviderModel | None:
        for m in self.list_models():
            if m.id == model_id:
                return m
        return None

    def require_model(self, model_id: str) -> ProviderModel:
        m = self.get_model(model_id)
        if m is None:
            raise ProviderNotFound(
                f"模型不存在：{_brief(model_id)}（现有：{[x.id for x in self.list_models()]}）"
            )
        return m

    # ---- 响应形状（脱敏） ----

    def snapshot(self) -> dict[str, Any]:
        """`GET /api/v1/providers` 的响应体（契约 §3 冻结）。

        ⚠️ **兼容期**照契约 §7 那段：同时给出 `provider_sources`（新），
           但**不造假的旧字段**（会让人以为还有旧结构）。
        `model_metadata` 是空对象：真实站里它是上游返回的元信息缓存，
        我们**不做缓存**（宁可空着，也不编造"这个模型支持什么"）。
        """
        models = self.list_models()
        default_id = self.default_provider_id
        return {
            "providers": [
                {**m.public_view(), "is_default": m.id == default_id} for m in models
            ],
            "provider_sources": [s.public_view() for s in self.list_sources()],
            "model_metadata": {},
            "default_provider_id": default_id,
        }

    def snapshot_sources(self) -> dict[str, Any]:
        """`GET /api/v1/provider-sources` 的响应体（契约 §2）。"""
        return {"provider_sources": [s.public_view() for s in self.list_sources()]}

    def snapshot_models_by_source(self) -> dict[str, Any]:
        """`GET /api/v1/provider-sources/models`：全部源的模型，按源分组。"""
        grouped: dict[str, list[dict[str, Any]]] = {s.id: [] for s in self.list_sources()}
        for m in self.list_models():
            grouped.setdefault(m.provider_source_id, []).append(m.public_view())
        return {"models": grouped, "provider_sources": [s.public_view() for s in self.list_sources()]}

    # ---- 写：端点层 ----

    def create_source(self, source: ProviderSource) -> ProviderSource:
        """新增端点。id 重复 → `ProviderValidationError`（400）。"""
        source.validate()
        with self._lock:
            sources, models, default_id, data, corrupt = self._read()
            if any(s.id == source.id for s in sources):
                raise ProviderValidationError(
                    f"provider source id 已存在：{_brief(source.id)}"
                    "（id 是主键，请换一个或改用 PUT 修改）"
                )
            sources.append(source)
            self._write_all(sources, models, default_id, data, corrupt)
        return source

    def update_source(self, source_id: str, patch: dict[str, Any]) -> ProviderSource:
        """改一个端点（**部分更新**，照真实契约的 PUT 语义）。

        🔴 **`key` 为空 = 不修改**（契约硬要求）：
           * 字段缺失 / `""` / `[]` / 掩码占位符 → **保持原 key**；
           * `null` → **显式清空**（给"要删掉这把 key"留一条明路）。

        为什么"空 = 不改"是硬要求：面板编辑时 key 框一律留空/显示掩码
        （凭据不回显），若把空当成清空，**用户改个超时就会把 key 抹掉** ——
        全程无提示，下次调用上游才 401（静默失败里最贵的那种）。
        """
        if not isinstance(patch, dict):
            raise ProviderValidationError("patch 必须是 JSON 对象")

        with self._lock:
            sources, models, default_id, data, corrupt = self._read()
            idx = next((i for i, s in enumerate(sources) if s.id == source_id), None)
            if idx is None:
                raise ProviderNotFound(f"provider source 不存在：{_brief(source_id)}")

            target = sources[idx]

            # `id` 是主键：值相同 = 忽略（前端会把整表单发过来），值不同 = 明确拒绝
            if "id" in patch and str(patch["id"] or "").strip() not in ("", source_id):
                raise ProviderValidationError(
                    "provider source 的 id 不可修改（它是主键，改了就是换一条，"
                    "而且会成为所有模型 id 的前缀）。要改 id 请先删掉再新建。"
                )

            unknown = sorted(set(patch) - set(SOURCE_WRITABLE_FIELDS) - {"id"})
            if unknown:
                raise ProviderValidationError(
                    f"不认识的字段：{unknown}；可改的只有 {list(SOURCE_WRITABLE_FIELDS)}"
                )

            if "key" in patch:
                raw = patch["key"]
                if raw is None:
                    target.key = []                      # 显式清空
                    log.info("provider source %s 的 key 被显式清空（patch 里给了 null）", source_id)
                else:
                    new_keys = _coerce_key_list(raw, where=f"provider source {source_id}")
                    if new_keys:
                        target.key = new_keys
                    else:
                        log.info(
                            "PUT 里 key 为空 —— 按契约视为【不修改】(%s)：保留原有 %d 把",
                            source_id,
                            len(target.key),
                        )

            for f in ("provider", "type", "provider_type", "api_base", "proxy"):
                if f in patch:
                    value = patch[f]
                    setattr(target, f, "" if value is None else str(value).strip())
            # 归一化后 provider/type 为空的回落到默认（照 `from_dict` 的宽容度）
            if not target.provider:
                target.provider = DEFAULT_VENDOR
            if not target.type:
                target.type = DEFAULT_ADAPTER_TYPE
            if not target.provider_type:
                target.provider_type = PROVIDER_TYPE_CHAT

            if "custom_headers" in patch:
                target.custom_headers = _coerce_headers(
                    patch["custom_headers"], where=f"provider source {source_id}"
                )
            if "timeout" in patch:
                target.timeout = _coerce_timeout(
                    patch["timeout"], where=f"provider source {source_id}"
                )
            if "enable" in patch:
                target.enable = bool(patch["enable"])

            # 合并后再校验：这样"只改 provider"的请求也会被整条记录的规则覆盖
            target.validate()
            sources[idx] = target
            self._write_all(sources, models, default_id, data, corrupt)
        return target

    def delete_source(self, source_id: str) -> tuple[ProviderSource, list[str], str]:
        """删除端点。**级联删掉它下面的模型**（照真实契约的级联语义）。

        Returns:
            `(被删的源, 被级联删掉的模型 id 列表, 删除后的默认 id)`

        🔴 为什么要级联：不删的话那些模型的 `provider_source_id` 立刻变成悬空引用
           —— 正是我们花力气拦住的那种"最难查的 bug"。而"源都没了"已经说明
           用户不要这些模型了，留着只会让面板显示一堆永远调不通的模型。

        ⚠️ 删掉的正好是默认 → 默认让给剩下的第一条（与
           `ProviderManager.unregister()` 同款语义），绝不留悬空默认值。
        """
        with self._lock:
            sources, models, default_id, data, corrupt = self._read()
            idx = next((i for i, s in enumerate(sources) if s.id == source_id), None)
            if idx is None:
                raise ProviderNotFound(f"provider source 不存在：{_brief(source_id)}")

            gone = sources.pop(idx)
            doomed = [m.id for m in models if m.provider_source_id == source_id]
            models = [m for m in models if m.provider_source_id != source_id]
            if doomed:
                log.info(
                    "删除 provider source %s 时级联删除了 %d 个模型：%s",
                    source_id,
                    len(doomed),
                    doomed,
                )

            if default_id == source_id or default_id in doomed:
                default_id = models[0].id if models else ""
                log.info("删除的是默认供应商，默认已改为 %r", default_id or "（无）")
            self._write_all(sources, models, default_id, data, corrupt)
        return gone, doomed, default_id

    # ---- 写：模型层 ----

    def create_model(self, model: ProviderModel) -> ProviderModel:
        """新增模型。**悬空引用 → 400**（这是本模块最要紧的一条校验）。"""
        with self._lock:
            sources, models, default_id, data, corrupt = self._read()
            model.validate_against_sources({s.id for s in sources})
            if any(m.id == model.id for m in models):
                raise ProviderValidationError(
                    f"模型 id 已存在：{_brief(model.id)}（请换一个或改用 PUT 修改）"
                )
            models.append(model)
            if not default_id:
                # 与宿主 `ProviderManager.register()` 的语义对齐（"第一个注册的自动成为默认"）
                # —— 两边一致，面板显示的默认才可信。
                default_id = model.id
                log.info("首个模型自动成为默认：%s", model.id)
            self._write_all(sources, models, default_id, data, corrupt)
        return model

    def update_model(self, model_id: str, patch: dict[str, Any]) -> ProviderModel:
        """改一个模型（部分更新）。

        ⚠️ 改 `provider_source_id` **等于换一个端点**：会同步把 id 的前缀也换掉
           （否则 `validate()` 里"前缀 = 源"的不变量立刻被破坏）。
           这也是唯一允许"改 id"的情形 —— 因为 id 本来就由源决定。
        """
        if not isinstance(patch, dict):
            raise ProviderValidationError("patch 必须是 JSON 对象")

        with self._lock:
            sources, models, default_id, data, corrupt = self._read()
            idx = next((i for i, m in enumerate(models) if m.id == model_id), None)
            if idx is None:
                raise ProviderNotFound(f"模型不存在：{_brief(model_id)}")

            target = models[idx]
            canonical_id = model_id

            # `id` 是主键：同值忽略、异值拒绝（除非只是因为换了源而变前缀，见下）
            given_id = str(patch.get("id") or "").strip() if "id" in patch else ""

            unknown = sorted(set(patch) - set(MODEL_WRITABLE_FIELDS) - {"id"})
            if unknown:
                raise ProviderValidationError(
                    f"不认识的字段：{unknown}；可改的只有 {list(MODEL_WRITABLE_FIELDS)}"
                )

            if "provider_source_id" in patch:
                raw = patch["provider_source_id"]
                new_sid = "" if raw is None else str(raw).strip()
                if new_sid and new_sid != target.provider_source_id:
                    # 换源 = 换端点：id 前缀必须跟着换，否则 id 与源对不上
                    suffix = target.model or canonical_id.split("/", 1)[-1]
                    canonical_id = f"{new_sid}/{suffix}"
                    log.info(
                        "模型 %s 换了所属源 → id 前缀同步改为 %s", model_id, canonical_id
                    )
                if new_sid:
                    target.provider_source_id = new_sid

            if given_id and given_id not in (model_id, canonical_id):
                raise ProviderValidationError(
                    "模型 id 不可随意修改（它必须是 `<源id>/<模型名>`）。"
                    "要换端点请改 provider_source_id，要改模型名请改 model。"
                )

            if "model" in patch:
                raw = patch["model"]
                new_model = "" if raw is None else str(raw).strip()
                if new_model and new_model != target.model:
                    target.model = new_model
                    # 模型名变了 → id 的后缀跟着变（保持"id = 源id/模型名"不变量）
                    canonical_id = f"{target.provider_source_id}/{new_model}"
            if "enable" in patch:
                target.enable = bool(patch["enable"])
            if "modalities" in patch:
                target.modalities = _coerce_modalities(
                    patch["modalities"], where=f"模型 {model_id}"
                )

            target.id = canonical_id
            target.validate_against_sources({s.id for s in sources})

            # 🔴 "改名后是否与他人冲突"要比**除自己以外**的记录 ——
            #    否则改自己的名永远撞上自己（第一条改成 `m-new` 时 `models` 里
            #    那条就是它自己）→ 正常的改名被 400 挡住。
            #    ⚠️ 这是实测抓出来的：判据 `test_update_model_resyncs_provider_manager`
            #       直接报 "改名后与已有模型 id 冲突：syuan/m-new"（自己撞自己）。
            clash = next((m.id for m in models if m.id == canonical_id and m is not target), None)
            if clash is not None:
                raise ProviderValidationError(f"改名后与已有模型 id 冲突：{_brief(canonical_id)}")

            if default_id == model_id:
                default_id = canonical_id
            self._write_all(sources, models, default_id, data, corrupt)
        return target

    def delete_model(self, model_id: str) -> tuple[ProviderModel, str]:
        """删除模型。返回 `(被删的那条, 删除后的默认 id)`。"""
        with self._lock:
            sources, models, default_id, data, corrupt = self._read()
            idx = next((i for i, m in enumerate(models) if m.id == model_id), None)
            if idx is None:
                raise ProviderNotFound(f"模型不存在：{_brief(model_id)}")

            gone = models.pop(idx)
            if default_id == model_id:
                default_id = models[0].id if models else ""
                log.info("删除的是默认模型，默认已改为 %r", default_id or "（无）")
            self._write_all(sources, models, default_id, data, corrupt)
        return gone, default_id

    def set_default(self, model_id: str) -> ProviderModel:
        """设默认。id 不存在 → `ProviderNotFound`（**不许留悬空默认值**）。"""
        with self._lock:
            sources, models, _, data, corrupt = self._read()
            target = next((m for m in models if m.id == model_id), None)
            if target is None:
                raise ProviderNotFound(
                    f"模型不存在：{_brief(model_id)}（可用的：{[m.id for m in models]}）"
                )
            self._write_all(sources, models, model_id, data, corrupt)
        return target

    # ---- 原子写 ----

    def _write_all(
        self,
        sources: list[ProviderSource],
        models: list[ProviderModel],
        default_id: str,
        data: dict[str, Any],
        corrupt: bool,
    ) -> None:
        """原子落盘（**只写新格式**，契约 §7：迁移后旧字段不再写回）。"""
        payload = dict(data)                 # 保留未知顶层键
        payload["version"] = SCHEMA_VERSION
        payload["default_provider_id"] = default_id
        payload["provider_sources"] = [s.to_storage_dict() for s in sources]
        payload["providers"] = [m.to_storage_dict() for m in models]
        self._write_payload(payload, corrupt=corrupt)

    def _write_payload(self, payload: dict[str, Any], *, corrupt: bool) -> None:
        """把一份**已经是最终形状**的 payload 原子落盘。

        🔴 `os.replace` 是**原子**的（同一文件系统内）—— 所以临时文件必须
           建在**目标目录**里，不能图省事扔 `/tmp`（跨文件系统时 `os.replace`
           退化成"拷贝 + 删"，就不再原子了；判据在 `tests/test_providers_api.py` 里）。
        """
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)

        # 坏文件先另存备份 —— 否则"加一个供应商"就把用户原本（可能手改坏的）配置永久覆盖了
        if corrupt and path.is_file():
            self._preserve_corrupt(path)

        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())        # 先让内容真的落盘，再换名
            # 🔴 文件里有**明文 api_key** → 只给属主读写（与 `.env` 同级处理）
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            # 失败时清掉临时文件，别在数据目录里留一地 `.tmp`
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _preserve_corrupt(self, path: Path) -> None:
        """把损坏的配置另存为 `*.corrupt`（已存在则加时间戳，绝不覆盖旧备份）。"""
        backup = path.with_name(path.name + ".corrupt")
        if backup.exists():
            backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        try:
            os.replace(path, backup)
        except OSError as exc:
            log.error("备份损坏的供应商配置失败（继续写入）：%s → %s", path, exc)
            return
        log.warning("损坏的供应商配置已备份为：%s", backup)


__all__ = [
    "ADAPTER_TYPE_OPENAI_CHAT",
    "ADAPTER_TYPE_OPENAI_RESPONSES",
    "DEFAULT_ADAPTER_TYPE",
    "DEFAULT_PATH",
    "DEFAULT_TIMEOUT",
    "DEFAULT_VENDOR",
    "EXECUTABLE_ADAPTER_TYPES",
    "KEY_MASK",
    "KNOWN_UNSUPPORTED_PROVIDER_TYPES",
    "MODEL_ID_PATTERN",
    "MODEL_WRITABLE_FIELDS",
    "PROVIDER_TYPE_CHAT",
    "PROVIDER_TYPE_EMBEDDING",
    "PROVIDER_TYPE_STT",
    "PROVIDER_TYPE_TTS",
    "is_chat_provider_type",
    "Provider",
    "ProviderError",
    "ProviderModel",
    "ProviderNotFound",
    "ProviderSource",
    "ProviderStore",
    "ProviderStoreCorrupt",
    "ProviderValidationError",
    "SCHEMA_VERSION",
    "SOURCE_ID_PATTERN",
    "SOURCE_WRITABLE_FIELDS",
    "SUPPORTED_PROVIDER_TYPES",
    "UnsupportedProviderType",
    "adapter_is_executable",
    "migrate_v1_to_v2",
    "needs_migration",
]

#: 旧名字（单层时代叫 `Provider`）。保留成别名，避免任何遗留 import 直接 ImportError；
#: ⚠️ 新代码请用 `ProviderSource` / `ProviderModel` —— 单层的 `Provider` 已经不存在了。
Provider = ProviderModel
