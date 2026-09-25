# -*- coding: utf-8 -*-
"""厂商预设目录 —— AstrBot v4.27.5 的 50 个预设（**静态表，不读运行时文件**）。

契约：`conductor/CONTRACT.md` §2 / §2.1 / §7.5（冻结）。

## 为什么单独一个文件

`infra/providers_store.py` 已经 1300+ 行（存/取/迁移/校验都在里面），
预设是**纯数据 + 三个纯函数**，塞进去只会让它更难读。
所以契约明确要求单独成文件（§2）。

## 数据来源与「不读 JSON」的理由

表内容照 `conductor/presets_source.json`（50 条，抓自本机真跑的 AstrBot v4.27.5）
**逐条抄成下面的字面量**。运行期**不**去读那个 JSON：

* `conductor/` 是**本轮并行开发的契约目录**，不是交付物的一部分 ——
  应用依赖它 = 换个部署目录就 ImportError/FileNotFound；
* 预设是**产品数据**，必须随代码走、可 review、可 diff；
* 少一次磁盘 IO 和一次"文件坏了怎么办"的分支（读 JSON 就得处理损坏）。

🔴 **改法**：要增删预设，改下面的 `PRESETS` 元组（**照 AstrBot 抄的那份，逐字对照**）
或 `LOCAL_EXTENSION_PRESETS`（**我们自己补的**），并同步
`tests/test_provider_presets.py` 里的数量判据（那份测试会拿
`conductor/presets_source.json` 当**对照物**逐项比 —— 数据源丢了/被改了它就会挂，
所以"抄错一个字段"必被抓住）。

## 字段（照契约 §2，**冻结**）

| 字段 | 含义 | 例 |
|---|---|---|
| `id` | 预设 id（= AstrBot 的 provider id） | `openai-responses` |
| `label` | **展示名**（契约 §2.1 对照表，别自创） | `OpenAI (Responses)` |
| `provider` | **厂商标签**，不参与执行决策 | `openai` / `microsoft` |
| `type` | **适配器协议** —— 唯一决定"我们能不能执行" | `openai_responses` |
| `provider_type` | 用途分类 | `chat_completion` |
| `api_base` | 默认地址（可为 `""` = 让用户自己填） | `https://api.openai.com/v1` |
| `note` | 一句话说明（见下） | |

⚠️ **不带任何"类型专属字段"**（契约 §7.5 明确决定）：AstrBot 的 embedding 用
`embedding_api_base`/`embedding_dimensions`、TTS 用 `voice`/`rate`/`volume`/`pitch`，
**我们不照抄** —— 本轮只做配置层，那些字段没有消费方，存了就是"看起来支持、
其实没人读"的假能力。真要做 TTS 出声/嵌入检索时再加，那时才知道该给什么默认值。

## `executable_by_us` 与「能力诚实」

50 条里**只有 `type ∈ {openai_chat_completion, openai_responses}` 的能跑**
（共 19 条，见 `providers_store.EXECUTABLE_ADAPTER_TYPES`）。
其余 31 条的适配器协议我们**没有实现**，`preset_public_view()` 会如实给出
`executable_by_us=false`，前端必须显式提示"本平台暂不能执行该适配器，仅保存配置"。

🔴 **绝不假装成功**：判据是 `adapter_is_executable(type)`，不是"名字里有没有 openai"
—— 例如 `openrouter_chat_completion` 虽然不是 `openai_*`，但它同样打
`/chat/completions`，**能跑**；而 `googlegenai_chat_completion` 走的是 Gemini 原生协议，
**不能跑**。所以这里只认那两个字面量。

## 3 条 `speech_to_text` 预设：**存得进，但我们执行不了**（2026-09-21 起）

`whisper` / `mimo_stt` / `xinference_stt` 照实列出（"预设目录是 AstrBot 的忠实快照"），
**2026-09-21 起 STT 已加入支持集**（情绪选"先只做配置层"）→ 这 3 条**能存进去了**
（此前 `ProviderStore` 会用 `unsupported_provider_type` 400 拒绝）。

🔴 **但"存得进" ≠ "能执行"**：三者的适配器（`openai_whisper_api` /
`xinference_stt` / `mimo_stt_api`）我们**一个都跑不了** ——
`preset_public_view()['executable_by_us']` 对它们**全部如实标 false**。

→ 这正是"能力诚实"要的形状：**配置层完整可用，执行层如实标注不可用**。
   与 TTS 同类处理（语音类共 14 条预设，`executable_by_us` 全为 false）。
（判据：`test_stt_presets_are_listed_and_storable_but_not_executable`。）

## 排序

按 `provider_type` 分组，组内按 `label` 排（契约 §2）。
组顺序用 `PROVIDER_TYPE_ORDER` 固定下来（不是字典序，也不是随机）——
判据 `test_presets_are_grouped_by_provider_type_then_label` 盯着它。
"""
from __future__ import annotations

from dataclasses import dataclass

from .logging_setup import get_logger
from .providers_store import (
    ADAPTER_TYPE_OPENAI_CHAT,
    ADAPTER_TYPE_OPENAI_RESPONSES,
    PROVIDER_TYPE_CHAT,
    PROVIDER_TYPE_EMBEDDING,
    PROVIDER_TYPE_STT,
    PROVIDER_TYPE_TTS,
    adapter_is_executable,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# note 文案（三条，**只写我们知道的事实**，不编造能力描述）
# ---------------------------------------------------------------------------

#: 可执行的两个 OpenAI 兼容格式 —— 写清**具体差别**（契约 §2 要求）。
#: 🔴 这句是给用户看的"我该选哪种"，所以必须落到**路径**上：
#:    两种格式的模型名/key/地址可能完全一样，差别只在请求打哪个端点。
NOTE_OPENAI_CHAT = (
    "OpenAI 兼容格式：请求打到 /chat/completions —— 本平台可以直接执行。"
)
NOTE_OPENAI_RESPONSES = (
    "OpenAI Responses 格式：请求打到 /responses（不是 /chat/completions）"
    "—— 本平台可以直接执行。"
)

#: 不可执行的统一文案（契约 §2 原文，**别改**：前端/判据都可能按它匹配）。
NOTE_ADAPTER_REQUIRED = "需 AstrBot 适配器，本平台暂不能执行"

#: 🔴 **本地扩展**的文案：这些是**我们自己加的**，AstrBot 目录里没有对应的那一条。
#: 为什么要加：AstrBot 给这些厂商写的适配器其实是 `ProviderOpenAIOfficial`
#: 的**薄包装**（10~59 行，只加 header / 归一化 URL / 改默认模型），
#: 也就是说**这些厂商的接口本来就是 OpenAI 格式** —— AstrBot 只是给它挂了个名字。
#: 出处（本机装在 `/root/.local/share/uv/tools/astrbot/.../core/provider/sources/`）：
#:   `oai_aihubmix_source.py`(10 行) · `zhipu_source.py`(16) ·
#:   `groq_source.py`(23) · `longcat_source.py`(23) · `openrouter_source.py`(23) ·
#:   `xiaomi_source.py`(56) · `ssycloud_source.py`(59) —— 全都 `class ProviderXxx(ProviderOpenAIOfficial)`。
#: ⚠️ 对照：`anthropic_source.py`(1010 行) / `gemini_source.py`(1118 行) 是**真专有**，不在此列；
#:   `kimi_code` / `minimax_token_plan` / `xiaomi_token_plan` 继承的是 `ProviderAnthropic`。
#: 每条的 note 里写清**我们没搬过来的那些适配器行为**（不写 = 用户以为一模一样）。
NOTE_LOCAL_EXTENSION = "本平台添加的 OpenAI 兼容预设（AstrBot 该厂商的适配器即此格式）"

#: `provider_type` 的分组顺序（契约 §2 的"按 provider_type 分组"具体是哪种顺序）。
#: ⚠️ 用 `PROVIDER_TYPE_STT` 常量（**别写字符串字面量**）—— 同一概念只留一处定义，
#:    否则改了 store 忘了这里就会出现"分组顺序里有个不存在的类型"。
PROVIDER_TYPE_ORDER: tuple[str, ...] = (
    PROVIDER_TYPE_CHAT,
    PROVIDER_TYPE_TTS,
    PROVIDER_TYPE_STT,
    PROVIDER_TYPE_EMBEDDING,
)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderPreset:
    """一条厂商预设（**不可变** —— 它是常量表里的数据，不是用户配置）。

    ⚠️ `frozen=True` 是刻意的：预设被谁"顺手改一下"就会让面板预填出错误的值，
        而且那种改动在代码里看不出来（`p.id = "x"` 会被 dataclass 拦住）。
    """

    id: str
    label: str
    provider: str
    type: str
    provider_type: str
    api_base: str = ""
    note: str = ""

    # ---- 出口 ----

    def public_view(self) -> dict:
        """响应/日志用的形状（**带 `executable_by_us`**）。

        ⚠️ 这里刻意**不叫** `to_dict()`：契约 §2 只冻结了 `preset_public_view(p)`，
           两个名字并存必然漂移。要么用这个，要么用模块级那个函数，语义一致。
        """
        return preset_public_view(self)


# ---------------------------------------------------------------------------
# 全量预设（50 条，照 conductor/presets_source.json 逐条抄；改数据要改这里）
# ---------------------------------------------------------------------------

PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id='302ai', label='302.AI', provider='302ai',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.302.ai/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='aihubmix', label='AiHubMix', provider='aihubmix',
        type='aihubmix_chat_completion', provider_type='chat_completion', api_base='https://aihubmix.com/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='anthropic', label='Anthropic', provider='anthropic',
        type='anthropic_chat_completion', provider_type='chat_completion', api_base='https://api.anthropic.com/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='azure_openai', label='Azure OpenAI', provider='azure',
        type='openai_chat_completion', provider_type='chat_completion', api_base='',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='compshare', label='Compshare', provider='compshare',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.modelverse.cn/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='deepseek', label='DeepSeek', provider='deepseek',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.deepseek.com/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='deepseek-responses', label='DeepSeek (Responses)', provider='deepseek',
        type='openai_responses', provider_type='chat_completion', api_base='https://api.deepseek.com/v1',
        note=NOTE_OPENAI_RESPONSES,
    ),
    ProviderPreset(
        id='fastgpt', label='FastGPT', provider='fastgpt',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.fastgpt.in/api/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='google_gemini', label='Google Gemini', provider='google',
        type='googlegenai_chat_completion', provider_type='chat_completion', api_base='https://generativelanguage.googleapis.com/',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='google_gemini_openai', label='Google Gemini (OpenAI 格式)', provider='google',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://generativelanguage.googleapis.com/v1beta/openai/',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='groq', label='Groq', provider='groq',
        type='groq_chat_completion', provider_type='chat_completion', api_base='https://api.groq.com/openai/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='kimi-code', label='Kimi 编程套餐', provider='kimi-code',
        type='kimi_code_chat_completion', provider_type='chat_completion', api_base='https://api.kimi.com/coding',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='lm_studio', label='LM Studio', provider='lm_studio',
        type='openai_chat_completion', provider_type='chat_completion', api_base='http://127.0.0.1:1234/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='longcat', label='LongCat', provider='longcat',
        type='longcat_chat_completion', provider_type='chat_completion', api_base='https://api.longcat.chat/openai',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='minimax', label='MiniMax', provider='minimax',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.minimaxi.com/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='minimax-token-plan', label='MiniMax 套餐', provider='minimax-token-plan',
        type='minimax_token_plan', provider_type='chat_completion', api_base='https://api.minimaxi.com/anthropic',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='moonshot', label='Moonshot (Kimi)', provider='moonshot',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.moonshot.cn/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='nvidia', label='NVIDIA', provider='nvidia',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://integrate.api.nvidia.com/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='ollama', label='Ollama', provider='ollama',
        type='openai_chat_completion', provider_type='chat_completion', api_base='http://127.0.0.1:11434/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='openai', label='OpenAI', provider='openai',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.openai.com/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='openai-responses', label='OpenAI (Responses)', provider='openai',
        type='openai_responses', provider_type='chat_completion', api_base='https://api.openai.com/v1',
        note=NOTE_OPENAI_RESPONSES,
    ),
    ProviderPreset(
        id='openrouter', label='OpenRouter', provider='openrouter',
        type='openrouter_chat_completion', provider_type='chat_completion', api_base='https://openrouter.ai/api/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='ppio', label='PPIO 派欧云', provider='ppio',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.ppinfra.com/v3/openai',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='tokenpony', label='TokenPony', provider='tokenpony',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.tokenpony.cn/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='xai', label='xAI Grok', provider='xai',
        type='openai_responses', provider_type='chat_completion', api_base='https://api.x.ai/v1',
        note=NOTE_OPENAI_RESPONSES,
    ),
    ProviderPreset(
        id='xiaomi', label='小米 MiMo', provider='xiaomi',
        type='xiaomi_chat_completion', provider_type='chat_completion', api_base='https://api.xiaomimimo.com/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='xiaomi-token-plan', label='小米 MiMo 套餐', provider='xiaomi-token-plan',
        type='xiaomi_token_plan', provider_type='chat_completion', api_base='https://token-plan-cn.xiaomimimo.com/anthropic',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='zhipu', label='智谱 GLM', provider='zhipu',
        type='zhipu_chat_completion', provider_type='chat_completion', api_base='https://open.bigmodel.cn/api/paas/v4/',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='siliconflow', label='硅基流动', provider='siliconflow',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api.siliconflow.cn/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='ssycloud', label='胜算云', provider='ssycloud',
        type='ssycloud_chat_completion', provider_type='chat_completion', api_base='https://router.shengsuanyun.com/api/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='modelscope', label='魔搭 ModelScope', provider='modelscope',
        type='openai_chat_completion', provider_type='chat_completion', api_base='https://api-inference.modelscope.cn/v1',
        note=NOTE_OPENAI_CHAT,
    ),
    ProviderPreset(
        id='azure_tts', label='Azure TTS', provider='azure',
        type='azure_tts', provider_type='text_to_speech', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='edge_tts', label='Edge TTS（免 Key）', provider='microsoft',
        type='edge_tts', provider_type='text_to_speech', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='fishaudio_tts', label='Fish Audio', provider='fishaudio',
        type='fishaudio_tts_api', provider_type='text_to_speech', api_base='https://api.fish.audio/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='gsvi_tts', label='GPT-SoVITS 推理服务', provider='gpt_sovits_inference',
        type='gsvi_tts_api', provider_type='text_to_speech', api_base='http://127.0.0.1:8000',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='gsv_tts', label='GPT-SoVITS（自建）', provider='gpt_sovits',
        type='gsv_tts_selfhost', provider_type='text_to_speech', api_base='http://127.0.0.1:9880',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='genie_tts', label='Genie TTS', provider='genie_tts',
        type='genie_tts', provider_type='text_to_speech', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='gemini_tts', label='Google Gemini TTS', provider='google',
        type='gemini_tts', provider_type='text_to_speech', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='minimax_tts', label='MiniMax TTS', provider='minimax',
        type='minimax_tts_api', provider_type='text_to_speech', api_base='https://api.minimax.chat/v1/t2a_v2',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='openai_tts', label='OpenAI TTS', provider='openai',
        type='openai_tts_api', provider_type='text_to_speech', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='mimo_tts', label='小米 MiMo TTS', provider='mimo',
        type='mimo_tts_api', provider_type='text_to_speech', api_base='https://api.xiaomimimo.com/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='volcengine_tts', label='火山引擎 TTS', provider='volcengine',
        type='volcengine_tts', provider_type='text_to_speech', api_base='https://openspeech.bytedance.com/api/v1/tts',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='gemini_embedding', label='Google Gemini Embedding', provider='google',
        type='gemini_embedding', provider_type='embedding', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='nvidia_embedding', label='NVIDIA Embedding', provider='nvidia',
        type='nvidia_embedding', provider_type='embedding', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='ollama_embedding', label='Ollama Embedding', provider='ollama',
        type='ollama_embedding', provider_type='embedding', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='openai_embedding', label='OpenAI Embedding', provider='openai',
        type='openai_embedding', provider_type='embedding', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='dashscope_embedding', label='阿里云百炼 Embedding', provider='dashscope',
        type='dashscope_embedding', provider_type='embedding', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='whisper', label='OpenAI Whisper', provider='openai',
        type='openai_whisper_api', provider_type='speech_to_text', api_base='',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='xinference_stt', label='Xinference STT', provider='xinference',
        type='xinference_stt', provider_type='speech_to_text', api_base='http://127.0.0.1:9997',
        note=NOTE_ADAPTER_REQUIRED,
    ),
    ProviderPreset(
        id='mimo_stt', label='小米 MiMo STT', provider='mimo',
        type='mimo_stt_api', provider_type='speech_to_text', api_base='https://api.xiaomimimo.com/v1',
        note=NOTE_ADAPTER_REQUIRED,
    ),
)


# ---------------------------------------------------------------------------
# 🆕 本地扩展预设（AstrBot 目录里**没有**这些条目；我们按实测补的）
# ---------------------------------------------------------------------------
#
# 🔴 为什么不直接改 `PRESETS`：那张表是**照 AstrBot v4.27.5 逐字抄的对照物**，
#    `tests/test_provider_presets.py` 拿 `conductor/presets_source.json` 逐字段比它。
#    本地扩展**另立一张表**，两者互不污染 —— 抄错一个字照样被抓，扩展有没有问题
#    由另一组判据守。
#
# 加这些的取证（2026-09-25，**不需要真 key**）：带一个假 key 打 `GET {api_base}/models`，
#   · 401/403 ⇒ 端点存在且要鉴权（URL 对了，只差用户的 key）
#   · 404     ⇒ 路径不对，说明不是 OpenAI 格式 → **不许加**
#   实测：groq 403 · zhipu 401 · xiaomi 401 · longcat 401 ·
#        aihubmix/openrouter/ssycloud 200 ⇒ 这 7 条可以加；
#        kimi-code / minimax-token-plan / xiaomi-token-plan 全 404 ⇒ **不加**。
#   ⚠️ 这个探针只证明"那个 URL 上有 OpenAI 形状的端点"，**不证明 `/chat/completions`
#      一定通** —— 真正的通过与否取决于用户填的 key 和订阅。所以 note 里写清事实，
#      不写"已验证可用"。

LOCAL_EXTENSION_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id='zhipu-openai', label='智谱 GLM（OpenAI 格式）', provider='zhipu',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://open.bigmodel.cn/api/paas/v4/',
        note=NOTE_LOCAL_EXTENSION,
    ),
    ProviderPreset(
        id='groq-openai', label='Groq（OpenAI 格式）', provider='groq',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://api.groq.com/openai/v1',
        note=NOTE_LOCAL_EXTENSION + "；⚠️ AstrBot 的 groq 适配器会从 assistant 历史里剔除 reasoning 字段（Groq 会拒收），本平台未做该清理，长对话若带上思维链可能被 Groq 拒绝",
    ),
    ProviderPreset(
        id='longcat-openai', label='LongCat（OpenAI 格式）', provider='longcat',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://api.longcat.chat/openai/v1',
        note=NOTE_LOCAL_EXTENSION + "；⚠️ 地址已按 AstrBot 的归一化补上 /v1（AstrBot 源数据里写的是不带 /v1 的 /openai）",
    ),
    ProviderPreset(
        id='openrouter-openai', label='OpenRouter（OpenAI 格式）', provider='openrouter',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://openrouter.ai/api/v1',
        note=NOTE_LOCAL_EXTENSION + "；未带 OpenRouter 的归属 header（不影响调用）",
    ),
    ProviderPreset(
        id='aihubmix-openai', label='AIHubMix（OpenAI 格式）', provider='aihubmix',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://aihubmix.com/v1',
        note=NOTE_LOCAL_EXTENSION + "；未带 AstrBot 的 APP-Code 折扣 header（不影响调用，仅少了折扣）",
    ),
    ProviderPreset(
        id='xiaomi-openai', label='小米 MiMo（OpenAI 格式）', provider='xiaomi',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://api.xiaomimimo.com/v1',
        note=NOTE_LOCAL_EXTENSION,
    ),
    ProviderPreset(
        id='ssycloud-openai', label='胜算云（OpenAI 格式）', provider='ssycloud',
        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://router.shengsuanyun.com/api/v1',
        note=NOTE_LOCAL_EXTENSION + "；未带 X-Title header（不影响调用）",
    ),
)


#: 面板与查找用的**全量目录** = AstrBot 忠实副本 + 本地扩展。
#: ⚠️ 现有的 `PRESETS` 引用一律**不要**换成它 —— 判据拿 `PRESETS` 跟源数据比对，
#:    换了就等于把扩展混进"忠实副本"里，那条判据就失去意义了。
ALL_PRESETS: tuple[ProviderPreset, ...] = PRESETS + LOCAL_EXTENSION_PRESETS


# ---------------------------------------------------------------------------
# 导入时的自检（不静默：重 id 会让 `find_preset` 永远只返回第一条）
# ---------------------------------------------------------------------------

_PRESET_IDS: tuple[str, ...] = tuple(p.id for p in ALL_PRESETS)

#: 重 id 是**抄数据时最可能犯的错**，而且症状很隐蔽：面板上两条同名，
#: `find_preset` 只返回先出现的那条。这里在导入时就喊出来（判据另有专门一条）。
_DUPLICATE_IDS: tuple[str, ...] = tuple(
    sorted({pid for pid in _PRESET_IDS if _PRESET_IDS.count(pid) > 1})
)
if _DUPLICATE_IDS:  # pragma: no cover — 正常数据下不会进（判据是测试里的）
    log.error(
        "厂商预设表里有重复 id：%s —— find_preset() 只会返回第一条，请修 PRESETS / LOCAL_EXTENSION_PRESETS",
        list(_DUPLICATE_IDS),
    )

#: 未知的 `provider_type` 说明抄数据时把分类写错了（排序也会跟着乱）。
_UNKNOWN_TYPES: tuple[str, ...] = tuple(
    sorted(
        {p.provider_type for p in ALL_PRESETS if p.provider_type not in PROVIDER_TYPE_ORDER}
    )
)
if _UNKNOWN_TYPES:  # pragma: no cover — 同上
    log.error(
        "厂商预设表里出现了 PROVIDER_TYPE_ORDER 之外的 provider_type：%s",
        list(_UNKNOWN_TYPES),
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def list_presets(
    provider_type: str | None = None, *, storable_only: bool = True
) -> list[ProviderPreset]:
    """按分类取预设；`None` = 全量（契约 §2）。

    ⚠️ **`""` 与 `None` 语义不同**，别"顺手"把它们归一：
       * `None` → 全部（不过滤）；
       * `""` → 空串不是合法分类 → **空列表 + WARNING**（调用方大概率是
         "顺手把表单里的空串传进来了"，静默返回全部会让面板显示错误的清单）。

    🔴 **`storable_only`（默认 True）—— 防"选得中、存不进"**（2026-09-20 建，2026-09-21 生效过）：
       这是照 `SUPPORTED_PROVIDER_TYPES` **动态**过滤的，不是写死的清单。
       * 建它时：AstrBot 目录里有 3 条 `speech_to_text` 预设，而我们**当时不支持 STT**
         → 若照全量下发，面板会列出用户**选得中但一保存就 400** 的预设
         （典型的"看着能用、其实不能用"，本项目最忌讳）→ 于是默认只给"存得进"的。
       * 2026-09-21 支持 STT 后：**这 3 条自动变得可下发**（50 条全出），
         一行过滤代码都不用改 —— 这正是"过滤条件跟着唯一的类型定义走"的好处。
       ⚠️ 仍保留 `storable_only=False` 给"只是想看看 AstrBot 有什么"的场景
         （例如能力对照页），**但那时调用方必须自己处理不可存的类型**。
       ⚠️ 「存得进」≠「能执行」：3 条 STT 预设的适配器（`openai_whisper_api` /
         `xinference_stt` / `mimo_stt_api`）我们**一个都跑不了**，
         由 `preset_public_view()['executable_by_us']` 如实标 false。

    ⚠️ 返回的是**新列表**（`ALL_PRESETS` 本体是常量元组，外部怎么改都影响不到它）。
    ⚠️ 取的是 **`ALL_PRESETS`（忠实副本 + 本地扩展）**，不是 `PRESETS` ——
       面板要能看到我们补的那 7 条 OpenAI 兼容预设。
    """
    if not isinstance(provider_type, str) and provider_type is not None:
        # 🔴 不是"过滤/跳过"分支就一定静默 —— 这里每一条异常输入都留痕：
        #    返回空列表是**响亮**的（调用方会看到"一个都没有"），但日志能说清为什么。
        log.warning(
            "list_presets 的 provider_type 必须是字符串或 None，收到 %s；按不匹配处理（空列表）",
            type(provider_type).__name__,
        )
        return []

    if provider_type is None:
        picked = list(ALL_PRESETS)
    else:
        wanted = provider_type.strip()
        if not wanted:
            log.warning(
                "list_presets 收到空字符串 provider_type —— 按不匹配处理（空列表）；"
                "要全部请传 None（不是 \"\"）"
            )
            return []
        if wanted not in PROVIDER_TYPE_ORDER:
            log.warning(
                "list_presets 收到目录里没有的 provider_type=%r（有的是 %s）；返回空列表",
                wanted,
                list(PROVIDER_TYPE_ORDER),
            )
            return []
        picked = [p for p in ALL_PRESETS if p.provider_type == wanted]

    if storable_only:
        from .providers_store import SUPPORTED_PROVIDER_TYPES

        dropped = [p.id for p in picked if p.provider_type not in SUPPORTED_PROVIDER_TYPES]
        if dropped:
            log.debug(
                "list_presets 略过 %d 条本平台**存不进**的预设（provider_type 不在 %s）：%s",
                len(dropped),
                list(SUPPORTED_PROVIDER_TYPES),
                dropped,
            )
        picked = [p for p in picked if p.provider_type in SUPPORTED_PROVIDER_TYPES]

    return picked


def find_preset(preset_id: str) -> ProviderPreset | None:
    """按 id 取一条；没有 → `None`（契约 §2）。

    ⚠️ 未命中**打 DEBUG 而不是 WARNING**：`None` 是契约规定的正常返回值
       （面板会用它判断"这个 id 是不是预设"），每次都告警会把日志淹掉。
       真正该响亮的是**调用方拿到 None 之后的处置**，那不该由这里替他喊。
    """
    if not isinstance(preset_id, str):
        log.debug("find_preset 收到非字符串 id（%s）→ None", type(preset_id).__name__)
        return None
    wanted = preset_id.strip()
    if not wanted:
        log.debug("find_preset 收到空 id → None")
        return None
    for preset in ALL_PRESETS:
        if preset.id == wanted:
            return preset
    log.debug("find_preset 未命中：%r（目录共 %d 条）", wanted, len(ALL_PRESETS))
    return None


def preset_public_view(p: ProviderPreset) -> dict:
    """响应形状（契约 §2 冻结，键的顺序也照它）。

    🔴 `executable_by_us` **只由 `adapter_is_executable(type)` 决定** ——
       不要改成"猜名字"（`openrouter_chat_completion` 能跑、
       `googlegenai_chat_completion` 不能跑，名字像不像没用）。
    """
    return {
        "id": p.id,
        "label": p.label,
        "provider": p.provider,
        "type": p.type,
        "provider_type": p.provider_type,
        "api_base": p.api_base,
        "note": p.note,
        "executable_by_us": adapter_is_executable(p.type),
    }


__all__ = [
    "ALL_PRESETS",
    "LOCAL_EXTENSION_PRESETS",
    "NOTE_ADAPTER_REQUIRED",
    "NOTE_LOCAL_EXTENSION",
    "NOTE_OPENAI_CHAT",
    "NOTE_OPENAI_RESPONSES",
    "PRESETS",
    "PROVIDER_TYPE_ORDER",
    "ProviderPreset",
    "find_preset",
    "list_presets",
    "preset_public_view",
]
