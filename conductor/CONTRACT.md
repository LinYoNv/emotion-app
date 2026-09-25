# 接口契约（本轮并行开发用）—— 冻结，改动必须先说

> 目的：4 条并行工作流（后端预设 / 后端设置 / 前端供应商页 / 前端设置页）按**同一份接口**写，
> 避免"各自实现一半、拼不起来"。
> 🔴 铁律：**任何一条与本文件不符的实现都算 bug**；需要改契约先停下来在文件里改，再动手。

---

## 0. 背景与范围（情绪已拍板）

- **TTS/STT：只做配置层** —— 供应商能增删改测 + 会话里能选默认 TTS/STT 模型 + 触发概率；**暂不真出声**。
- **设置页本轮做两段**：「会话」+「上下文管理策略」。
- **上下文**：我们已有的三种模式（`recent_only`/`rolling_summary`/`semantic`）当**策略下拉**，
  AstrBot 的参数作**细项**。
- **剔除**：agent 执行器 / 子代理编排 / 群聊 / 多平台 / 管理面板与管理员权限。

### 🔴 能力诚实原则（本项目铁律，违反=事故）

AstrBot 的 50 个预设里，**我们只能执行 2 种适配器类型**：`openai_chat_completion`、`openai_responses`。
其余（`anthropic_chat_completion`/`googlegenai_chat_completion`/`zhipu_chat_completion`/`gemini_tts`/…）
**我们跑不了**。处理方式**必须是**：

- 预设**照实列出**（用户能看到、能建，字段也会照填）；
- 但每一项都带 `executable_by_us: bool`；
- 前端对 `executable_by_us=false` 的**必须显式提示**「本平台暂不能执行该适配器，仅保存配置」；
- 后端在**真正要用它**的时候（测试连通 / 会话选模型 / 插件取用）**如实返回不可用**，
  **绝不允许假装成功或静默降级**。

---

## 1. 后端：`provider_type` 支持的取值域

`emotion_app/infra/providers_store.py`

```python
PROVIDER_TYPE_CHAT = "chat_completion"
PROVIDER_TYPE_TTS = "text_to_speech"
PROVIDER_TYPE_EMBEDDING = "embedding"
SUPPORTED_PROVIDER_TYPES = (PROVIDER_TYPE_CHAT, PROVIDER_TYPE_TTS, PROVIDER_TYPE_EMBEDDING)
```

- **STT（`speech_to_text`）本轮不加入**（情绪选"只做配置层"且 STT 未确认要；保持"已知不支持"如实 400）。
- `KNOWN_UNSUPPORTED_PROVIDER_TYPES` 保留 `speech_to_text`/`rerank`/`agent_runner`/`image_generation`。
- **`agent_runner` 永远不支持**（情绪明确：情绪app 不需要 agent 能力）。

### 可执行适配器白名单（新概念，`providers_store.py`）

```python
ADAPTER_TYPE_OPENAI_CHAT = "openai_chat_completion"
ADAPTER_TYPE_OPENAI_RESPONSES = "openai_responses"
EXECUTABLE_ADAPTER_TYPES = (ADAPTER_TYPE_OPENAI_CHAT, ADAPTER_TYPE_OPENAI_RESPONSES)

def adapter_is_executable(adapter_type: str) -> bool: ...
```

⚠️ **注意**：`type`（适配器协议）**不做白名单拒绝** —— 用户可以从预设里选
`anthropic_chat_completion` 并存下来（只保存不执行）。**是否可执行**用上面的函数判断，
并在需要执行的地方如实返回不可用。

---

## 2. 后端：厂商预设目录（新文件）

`emotion_app/infra/provider_presets.py`（新建，**不要**塞进 providers_store.py，它已 1350 行）

```python
@dataclass(frozen=True)
class ProviderPreset:
    id: str                 # "openai" / "deepseek-responses" / "edge_tts" ...
    label: str              # 展示名，如 "OpenAI"、"深度求索(Responses)"
    provider: str           # 厂商键，如 "openai"
    type: str               # 适配器协议，如 "openai_chat_completion"
    provider_type: str      # 分类：chat_completion / text_to_speech / embedding
    api_base: str           # 默认地址（可为 ""）
    note: str               # 一句话说明（可空）

PRESETS: tuple[ProviderPreset, ...]          # 全量，按 provider_type 分组再按 label 排序
def list_presets(provider_type: str | None = None) -> list[ProviderPreset]
def find_preset(preset_id: str) -> ProviderPreset | None
def preset_public_view(p: ProviderPreset) -> dict   # 加 executable_by_us 字段
```

`preset_public_view` 返回：

```json
{"id": "...", "label": "...", "provider": "...", "type": "...",
 "provider_type": "chat_completion", "api_base": "https://...", "note": "...",
 "executable_by_us": true}
```

数据源：`conductor/presets_source.json`（50 条，抓自 AstrBot v4.27.5 真实现）。字段映射：
`id` 直接用；`label` 用**我们自己的中文/通用名**（见 §2.1 对照表，别把 AstrBot 的 id 当展示名）；
`provider`/`type`/`provider_type`/`api_base` 照搬。

### 2.1 展示名对照（必须用这些，别自创）

`chat_completion`：openai→**OpenAI**、openai-responses→**OpenAI (Responses)**、
google_gemini→**Google Gemini**、anthropic→**Anthropic**、kimi-code→**Kimi 编程套餐**、
moonshot→**Moonshot (Kimi)**、minimax→**MiniMax**、minimax-token-plan→**MiniMax 套餐**、
xiaomi→**小米 MiMo**、xiaomi-token-plan→**小米 MiMo 套餐**、xai→**xAI Grok**、
deepseek→**DeepSeek**、deepseek-responses→**DeepSeek (Responses)**、zhipu→**智谱 GLM**、
longcat→**LongCat**、aihubmix→**AiHubMix**、openrouter→**OpenRouter**、ssycloud→**胜算云**、
nvidia→**NVIDIA**、azure_openai→**Azure OpenAI**、ollama→**Ollama**、lm_studio→**LM Studio**、
google_gemini_openai→**Google Gemini (OpenAI 格式)**、groq→**Groq**、302ai→**302.AI**、
siliconflow→**硅基流动**、ppio→**PPIO 派欧云**、tokenpony→**TokenPony**、
compshare→**Compshare**、modelscope→**魔搭 ModelScope**、fastgpt→**FastGPT**

`text_to_speech`：openai_tts→**OpenAI TTS**、mimo_tts→**小米 MiMo TTS**、genie_tts→**Genie TTS**、
edge_tts→**Edge TTS（免 Key）**、gsv_tts→**GPT-SoVITS（自建）**、gsvi_tts→**GPT-SoVITS 推理服务**、
fishaudio_tts→**Fish Audio**、azure_tts→**Azure TTS**、minimax_tts→**MiniMax TTS**、
volcengine_tts→**火山引擎 TTS**、gemini_tts→**Google Gemini TTS**

`embedding`：openai_embedding→**OpenAI Embedding**、gemini_embedding→**Google Gemini Embedding**、
nvidia_embedding→**NVIDIA Embedding**、ollama_embedding→**Ollama Embedding**、
dashscope_embedding→**阿里云百炼 Embedding**

`note`：**只写我们知道的事实**。可执行的两个 OpenAI 格式写清区别；
不可执行的统一写「需 AstrBot 适配器，本平台暂不能执行」。**别编造能力描述。**

---

## 3. 后端：预设 API

`emotion_app/web/api_providers.py` 新增一条（挂在既有 `providers` 蓝图，前缀 `/api/v1`）：

```
GET /api/v1/provider-presets            → 全部
GET /api/v1/provider-presets?provider_type=text_to_speech  → 过滤
```

响应**照本项目供应商蓝图既有信封**（成功）：

```json
{"status": "ok", "message": null, "data": {"presets": [ ...preset_public_view... ], "total": 50}}
```

- `provider_type` 非法（不在支持集）→ **400**，`error.type="invalid_provider_type"`，消息里列出合法值。
- 需要鉴权（与其它供应商路由一致）。
- **不需要** body。

---

## 4. 后端：设置配置文件（会话 + 上下文）

新文件 `emotion_app/domain/config_profile.py`（领域层，**不碰 Quart**）：
负责读写"配置文件"文档，落盘 `data/config_profile.json`。

```python
SECTION_SESSION = "session"
SECTION_CONTEXT = "context"

@dataclass
class ConfigProfile:
    session: dict[str, Any]
    context: dict[str, Any]
```

### 4.1 字段定义（**这是权威清单**）

**`session`（会话）**

| 键 | 类型 | 默认 | 含义 | 取值 |
|---|---|---|---|---|
| `default_chat_provider_id` | str | `""` | 默认对话模型（`源id/模型名`） | 空 = 用第一个可用的 |
| `fallback_chat_provider_ids` | list[str] | `[]` | 回退对话模型（按顺序） | |
| `default_tts_provider_id` | str | `""` | 默认 TTS 模型 | |
| `tts_enable` | bool | `false` | 启用文本转语音（**只存配置，暂不出声**） | |
| `tts_trigger_probability` | float | `1.0` | TTS 触发概率 | 0.0–1.0 |
| `default_persona_id` | str | `""` | 默认采用的人格 | 空 = 用当前激活人格 |
| `request_max_retries` | int | `3` | 请求最大重试次数 | 0–10 |
| `display_reasoning_text` | bool | `false` | 显示思考内容 | |
| `datetime_system_prompt` | bool | `true` | 现实世界时间感知 | |
| `streaming_response` | bool | `true` | 流式输出 | |

> ⚠️ `streaming_response` 我们默认 `true`（我们本来就支持流式），AstrBot 默认 false —— **这是有意不同**。

**`context`（上下文管理策略）**

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `mode` | str | `"recent_only"` | **我们的三模式**：`recent_only`/`rolling_summary`/`semantic` |
| `protected_tail` | int | `6` | 保留最近 N 条原文 |
| `max_context_length` | int | `-1` | 压缩前最多保留对话轮数（-1 = 不限） |
| `dequeue_context_length` | int | `1` | 轮次超限时一次丢弃轮数 |
| `context_limit_reached_strategy` | str | `"llm_compress"` | `truncate`(按轮数截断) / `llm_compress`(由 LLM 压缩) |
| `llm_compress_instruction` | str | 见下 | 上下文压缩提示词 |
| `llm_compress_keep_recent_ratio` | float | `0.15` | 压缩时保留最近上下文比例，0–0.9 |
| `llm_compress_provider_id` | str | `""` | 用于压缩的模型（空 = 用当前对话模型） |
| `fallback_max_context_tokens` | int | `128000` | 上下文窗口兜底值 |
| `summary_trigger` | int | `20` | 累积超过多少条触发总结（**我们既有字段，保留**） |

`llm_compress_instruction` 默认值（照 AstrBot 原文，**注意它是英文**）：
```
Based on our full conversation history, produce a concise summary of key takeaways and/or project progress.
```
> 🔴 我们的库里**已有** `summary_trigger`/`protected_tail` 等字段（见会话的 policy）。
> **本配置文件是它们的"默认值来源"**，不替换既有会话级 policy。别去改会话表结构。

### 4.2 落盘格式

`data/config_profile.json`：
```json
{"version": 1, "session": {...}, "context": {...}}
```
- 缺失/损坏 → **按默认值继续 + 打 ERROR**（损坏时另存 `.corrupt`），**绝不静默当空**。
- 写入**原子**（临时文件 → fsync → `os.replace`）。
- 未知键**保留**（别丢用户/未来版本的数据）。
- 校验失败 → 抛领域异常，由 API 层转 **400** 并**说清哪个字段**。

### 4.3 API

`emotion_app/web/api_admin.py`（挂既有 `admin` 蓝图，前缀 `/api/v1`）：

```
GET   /api/v1/config-profile                 → {"profile": {...}, "schema": {...}}
PATCH /api/v1/config-profile                 → body 可以是 {"session": {...}} / {"context": {...}} / 两者
DELETE /api/v1/config-profile                → 恢复默认（等价于删掉 overrides）
```

- `GET` 的 `schema` 是**给前端渲染用的字段元数据**（`key`/`label`/`type`/`default`/`options`/`hint`），
  形如：
  ```json
  {"session": [{"key":"default_chat_provider_id","label":"默认对话模型","type":"provider_select",
                "provider_type":"chat_completion","default":"","hint":"留空时使用第一个可用模型"}],
   "context": [{"key":"mode","label":"上下文模式","type":"enum","default":"recent_only",
                "options":[{"value":"recent_only","label":"只保留最近原文"}, ...]}]}
  ```
  字段元数据**放在后端**（单一事实来源），前端**照它渲染**，别在 JS 里hardcode 一份。
- `PATCH` 未知键 → **400**（别静默忽略）；类型不符 → **400** 说清字段名。
- 鉴权与其他 admin 路由一致。

---

## 5. 前端：模型供应商页

`emotion_app/web/static/index.html` + `app.js`

1. **类型切换**：左列表顶部加三个筛选（**全部 / 对话 / 语音 / 嵌入**）。
   - 🔴 现状：`isChatSource()` 会**过滤掉非对话源**并显示"另有 N 个未显示"。
     改成：**默认只显示对话**（保持现状行为），但提供切换；切到某类时**只显示该类**，
     hint 要如实说"当前筛选：语音（共 N 个）"。
   - 源列表项在 `provider_type` 非对话时**带一个类型徽标**（如「语音」「嵌入」）。
2. **厂商预设**：「+ 新增」时先弹**预设选择**（按 `provider_type` 分组的 `<select>` 或列表），
   选中后**预填** `provider`/`type`/`provider_type`/`api_base`，用户可改。
   - 预设从 `GET /api/v1/provider-presets` 拉；**失败要如实报错**，别静默变成"没有预设"。
   - `executable_by_us=false` 的预设，选中后**显式提示**："本平台暂不能执行该适配器，仅保存配置"。
3. **OpenAI 两种格式**：设置段里 `type` 要有可选项
   - `openai_chat_completion` → 展示「OpenAI Compatible（/chat/completions）」
   - `openai_responses` → 展示「OpenAI Responses（/responses）」
   - 其余适配器类型只读展示（因为来自预设），并标注不可执行。
4. **保存时** `collectSourcePayload()` 现在**硬编码** `provider_type:"chat_completion"` /
   `provider:"openai"` / `type:"openai_chat_completion"` —— **必须改成用表单/预设的真实值**。
   🔴 这是本轮最容易漏的一处（硬编码会让 TTS 源被存成对话源）。

---

## 6. 前端：设置页（配置文件式）

`index.html` 的 `tab-settings` 改成**配置文件式**，结构：

```
设置
├─ 会话            ← 按 schema.session 渲染
├─ 上下文管理策略   ← 按 schema.context 渲染
└─ 部署配置（只读）  ← 保留现有 /health 渲染
```

- **保留**现有"上游（业务配置）"表单（API 地址/模型/Key/获取模型）吗？
  → **保留**，但移到最上面并改标题为「上游连接（本机直连上游）」，因为它是**本服务自己**用的，
  与"选哪个供应商模型"是两个概念。别删（删了就没有配 key 的入口了）。
- 渲染驱动：**照 `GET /api/v1/config-profile` 的 `schema` 渲染**，支持控件类型
  `string` / `int` / `float` / `bool`（开关）/ `enum`（下拉）/ `text`（多行）/
  `provider_select`（下拉，选项来自 `/api/v1/provider-sources` + `/providers`，**按 `provider_type` 过滤**）。
- `provider_select` 的选项**必须是真实已配置的模型**；没有可选时显示"还没有配置该类模型"并给跳转链接，
  **别显示一个空下拉**。
- 保存：`PATCH /api/v1/config-profile`，只发**改动过的**字段；成功后 toast 并把 schema 里标"需重启"的
  字段如实提示（本轮**没有**需重启字段，所以别乱提示）。
- 恢复默认：`DELETE`，二次确认。

---

## 7. 判据要求（谁改谁加，全部要能失败）

- 供应商页：扩 `tools/provider_panel_check.py`（现有 70 条）—— 新增类型切换、预设选择、
  两格式、`executable_by_us` 提示、**TTS 源不被存成对话源**。
- 设置页：新建 `tools/settings_check.py`（真浏览器），至少覆盖：
  两段渲染出来 / 字段值来自后端 / 保存真的发出 PATCH 且只发改动字段 /
  未知键 400 时如实报错 / `provider_select` 空列表时不说谎 / 恢复默认二次确认。
- 🔴 每条判据都要**注入 bug 验证它会挂**。判据不许"跳过"（项目铁律）。
- 后端：新增单测覆盖预设目录（数量/分组/`executable_by_us`）、`provider_type` 扩展、
  `EXECUTABLE_ADAPTER_TYPES`、配置文件读写/校验/损坏/原子性。

---

## 7.5 🔴 补充澄清（核实 AstrBot 源码后加，**不改变任何接口**）

### AstrBot 的真实字段形状（已逐字核实 `core/config/default.py`）

```
openai             → provider="openai",   type="openai_chat_completion", provider_type="chat_completion", api_base="https://api.openai.com/v1"
openai-responses   → provider="openai",   type="openai_responses",       provider_type="chat_completion", api_base="https://api.openai.com/v1"
deepseek           → provider="deepseek", type="openai_chat_completion", provider_type="chat_completion"
deepseek-responses → provider="deepseek", type="openai_responses",       provider_type="chat_completion"
edge_tts           → provider="microsoft",type="edge_tts",               provider_type="text_to_speech",  额外字段：edge-tts-voice/rate/volume/pitch
openai_embedding   → provider="openai",   type="openai_embedding",       provider_type="embedding",
                     额外字段用的是**另一套名字**：embedding_api_key / embedding_api_base / embedding_model / embedding_dimensions
```

### ⚠️ 我们的决定：**统一字段，不做 per-类型字段名**

AstrBot 的 embedding 用 `embedding_api_key`/`embedding_api_base`，TTS 用 `voice`/`rate`/`volume`/`pitch` ——
**我们不照抄这套**。我们的两层存储对**所有类型**统一用：

```
provider_sources[]: id / provider / type / provider_type / key(list) / api_base / timeout / proxy / custom_headers / enable
```

理由（写清楚，免得后面有人以为是漏做）：
1. 统一字段 = 面板一套表单就能覆盖三种类型，**不必为每种类型维护一套字段映射**；
2. 我们**本轮只做配置层**（TTS 不出声），那些专属字段（音色/语速/维度）**现在没有消费方** ——
   存了也没人读，属于"看起来支持、其实没人用"的假能力；
3. 真要用时（将来做 TTS 出声 / 嵌入检索）再加**该类型专属字段**，
   那时它们才有真实语义，也才知道该给什么默认值。

→ **本轮不实现** `voice`/`rate`/`embedding_dimensions` 等专属字段。
   预设里照实带上 `api_base`（有用），**专属字段一律不预填**。

### 关于 `type` 与 `provider` 的取值

- `provider` 只作**厂商标签**（`openai`/`deepseek`/`microsoft`/…），**不参与执行决策**。
- 真正决定"能不能跑"的只有 `type`（适配器协议），判据是 `adapter_is_executable(type)`。
- 所以预设里 `provider=microsoft` 的 `edge_tts` **能存**，但因为 `type="edge_tts"` 不可执行 →
  `executable_by_us=false`，前端必须如实标注。

---

## 8. 已知真 bug（本轮顺手修，别漏）

🔴 `emotion_app/web/provider_bridge.py` 的 `UpstreamLLMPort.generate()`
只 `except TypeError`，但装配给它的 `OpenAIChatBackend`（`emotion_app/domain/backends.py`）
**没有 `complete()` 方法**（只有 `chat`/`chat_stream`）→ 插件在**启动路径**上拿到端口后一调用
就 **AttributeError**，且不会被兜住。
- 修法：`generate()` 要能识别"后端没有 complete()"，**回落到 `chat()`** 并把消息/模型正确传过去；
  若两条路都不通，**如实抛错**（不静默返回空）。
- 同时给 `OpenAIChatBackend` 加一个 `complete()`（形状与 `UpstreamLLMPort` 期望一致），
  两条都做，互为保险。**加断言测试证明"启动路径真的能调通"**（而不是只在热同步路径上能调通）。
