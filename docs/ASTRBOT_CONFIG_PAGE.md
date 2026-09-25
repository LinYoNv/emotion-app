# AstrBot 配置页结构（实测抓取，v4.27.5）

> 数据来源：**真实安装**（`/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot`）
> · 分组/标题/字段说明来自 `dashboard/dist/assets/index-BiTHkcB1.js`（前端 bundle，
>   配置页的中文标题与 hint 都在这里，**不在** Python 侧）；
> · 默认值与类型来自 `core/config/default.py` 的 `DEFAULT_CONFIG`。
> 抓取日期 2026-09-20。**这是一手实测，不是照文档抄的。**

## 1. 配置页的渲染模型

配置页**不是**按 Python 的 `DEFAULT_CONFIG` 扁平渲染的，而是：

```
<分组键>: { hint: "...", description: "<中文小节标题>", <配置段>: { <字段>: {description, hint, labels?} } }
```

- `description` = 页面上那个**加粗小节标题**（如「上下文管理策略」）；
- `hint` = 标题下那行灰色说明（常带 AstrBot 文档链接）；
- `<配置段>` = `provider_settings` / `provider_tts_settings` / `platform_settings` …；
- 字段的 `description` = 表单项标签，`hint` = 标签下的说明，`labels` = 枚举项的中文名。

## 2. 实测到的顶层分组

| 分组键 | 中文标题 | 对应配置段 | 与「情绪app」的关系 |
|---|---|---|---|
| `ai` | **模型** | `provider_settings` | ✅ 采用（默认对话模型/回退模型/图片转述） |
| `provider_tts_settings` | **文本转语音** | `provider_tts_settings` | ✅ 采用（本轮只做配置层） |
| `provider_stt_settings` | **语音转文本** | `provider_stt_settings` | ⏸ 本轮不做（情绪未确认要） |
| `persona` | **人格** | `provider_settings` | ✅ 采用（默认采用的人格） |
| `truncate_and_compress` | **上下文管理策略** | `provider_settings` | ✅ 采用（图3 上半部分） |
| `others` | **其他配置** | `provider_settings` | 🟡 部分采用（剔除 agent/群聊项） |
| `plugin` / `segmented_reply` | **插件 / 分段回复** | `plugin_set` / `platform_settings` | ⏸ 未定 |
| `ltm` | **群聊上下文感知** | `provider_ltm_settings` | ❌ 剔除（群聊） |
| `agent_runner` | **执行器** | `provider_settings` | ❌ 剔除（agent） |
| `subagent_orchestrator` | **子代理编排** | `subagent_orchestrator` | ❌ 剔除（agent） |
| `dashboard` | （管理面板） | `dashboard` | ❌ 剔除（管理员权限） |
| `platform_settings` / `platform_specific` | 平台 / 平台专属 | 同名 | ❌ 剔除（多平台，我们只有 webchat） |
| `content_safety` | **内容安全** | `content_safety` | ⏸ 未定（含百度内容审核） |
| `file_extract` | **文档解析能力** | `provider_settings` | ⏸ 未定 |
| `knowledgebase` | **知识库** | — | ⏸ 未定 |
| `system` | **系统配置** | `t2i_strategy` 等 | 🟡 我们已有等价物（server/logging/storage） |

## 3. 「上下文管理策略」的真实字段（逐字）

| 字段 | 中文标签 | 实测说明 |
|---|---|---|
| `max_context_length` | 压缩前最多保留对话轮数 | 超过该轮数后才按下方策略持久化截断或 LLM 压缩；请求发送前也先按该值约束。**-1 = 不按轮数限制** |
| `dequeue_context_length` | 轮次超限时一次丢弃轮数 | 超过上一项且无法 LLM 压缩时，一次丢弃多少轮旧对话 |
| `context_limit_reached_strategy` | 历史超限或上下文接近上限时的处理方式 | 枚举：`按对话轮数截断` / `由 LLM 压缩上下文` |
| `llm_compress_instruction` | 上下文压缩提示词 | 默认英文：`Based on our full conversation history, produce a concise summary of key takeaways and/or project progress.` |
| `llm_compress_keep_recent_ratio` | 压缩时保留最近上下文比例 | 0–0.3（滑块），默认 `0.15` |
| `llm_compress_provider_id` | 用于上下文压缩的模型提供商 ID | 留空时用第一个模型 |
| `fallback_max_context_tokens` | 上下文窗口兜底值 | 默认 128000 |

## 4. 「其他配置」里的字段（含**必须剔除**的）

保留候选：`display_reasoning_text`(显示思考内容) · `llm_safety_mode`(健康模式) ·
`safety_mode_strategy` · `identifier`(用户识别) · `datetime_system_prompt`(现实世界时间感知) ·
`streaming_response`(流式输出) · `image_compress_enabled`(启用图片压缩) ·
`image_compress_options`(最长边/质量) · `request_max_retries`(请求最大重试次数)

🔴 **必须剔除**（agent / 群聊 / 管理员 / 多平台）：
`agent_runner_type` · `dify_agent_runner_provider_id` · `coze_agent_runner_provider_id` ·
`dashscope_agent_runner_provider_id` · `deerflow_agent_runner_provider_id` · `max_agent_step` ·
`tool_call_timeout` · `tool_schema_mode` · `show_tool_use_status` · `show_tool_call_result` ·
`buffer_intermediate_messages` · `sanitize_context_by_modalities` · `computer_use_runtime` ·
`computer_use_require_admin` · `sandbox` · `subagent_orchestrator` · `group_name_display` ·
`group_icl_enable` · `group_message_max_cnt` · `group_message_history_enable` ·
`quoted_message_parser` 系列（转发消息）· `max_quoted_fallback_images` · `platform_settings.*` ·
`dashboard.*`（含 `auth_rate_limit`/`totp`/`ssl`）· `provider_pool` · 全部 websearch_* （待定）

## 5. 模型供应商预设（`provider` 段）

实测 **59 个预设**，按 `provider_type` 分布：

| provider_type | 数量 | 我们的处理 |
|---|---|---|
| `chat_completion` | 31 | ✅ 采用（其中只有 `openai_chat_completion`/`openai_responses` 我们能执行） |
| `text_to_speech` | 11 | ✅ 采用（配置层） |
| `embedding` | 5 | ✅ 采用 |
| `speech_to_text` | 3 | ⏸ 本轮不做 |
| `rerank` | 5 | ❌ 剔除 |
| `agent_runner` | 4 | ❌ 剔除（情绪明确：不需要 agent） |

### 我们**能执行**的只有两种适配器

```
openai_chat_completion  → POST <api_base>/chat/completions
openai_responses        → POST <api_base>/responses
```

其余（`anthropic_chat_completion`/`googlegenai_chat_completion`/`zhipu_chat_completion`/
`gemini_tts`/`edge_tts`/`openai_embedding` …）是 **AstrBot 自己的适配器**，我们跑不了。
→ 按项目铁律：**照实列出预设、如实标注 `executable_by_us=false`**，绝不假装能用。

### 实测：上游**真的支持** `/responses`

```
POST https://api.syuan.org/v1/responses  {"model":"gemini-3.5-flash","input":"hi"}
→ HTTP 200，返回标准 Responses 形状（object:"response", output:[{type:"message",...}]）
```
（对照 `POST /chat/completions` 同样 200。）**所以 `openai_responses` 我们真实现，不是假装支持。**

### AstrBot 的 per-类型字段名（我们**不照抄**）

| 类型 | AstrBot 的额外字段 |
|---|---|
| TTS | `edge-tts-voice` / `rate` / `volume` / `pitch` |
| embedding | `embedding_api_key` / `embedding_api_base` / `embedding_model` / `embedding_dimensions` / `embedding_dimensions_mode` |

→ 我们对所有类型**统一**用 `key`(list) / `api_base` / `timeout` / `proxy` / `custom_headers`。
理由：本轮只做配置层，这些专属字段**没有消费方**，存了就是"看起来支持、其实没人读"的假能力；
将来真做 TTS 出声 / 嵌入检索时再加，那时才知道该给什么默认值。

## 6. 复现命令

```bash
# 预设目录（59 条，含 provider/type/provider_type/api_base）
python3 - <<'PY'
import re, json
src = open('/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/core/config/default.py',
           encoding='utf-8').read()
out = []
for pid, rest in re.findall(r'\{\s*"id"\s*:\s*"([^"]+)"(.*?)\}', src, re.S):
    g = lambda k: (re.search(r'"%s"\s*:\s*"([^"]*)"' % k, rest) or [None, None])[1]
    if g('provider_type'):
        out.append({'id': pid, 'provider': g('provider'), 'type': g('type'),
                    'provider_type': g('provider_type'), 'api_base': g('api_base')})
print(len(out)); json.dump(out, open('/tmp/ab_presets.json','w'), ensure_ascii=False, indent=1)
PY

# 配置页分组（中文标题来自前端 bundle）
python3 - <<'PY'
import re
src = open('/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot/'
           'dashboard/dist/assets/index-BiTHkcB1.js', encoding='utf-8', errors='replace').read()
for m in re.finditer(r'(\w+):\{hint:"[^"]*",description:"([^"]{2,40})"', src):
    print(m.group(1), '→', m.group(2))
PY
```

---

# 附录：本轮落地结果（2026-09-20）

## 做了什么

| 需求（情绪原话） | 落地 |
|---|---|
| ①「设置」改成配置文件式，先做「会话」 | ✅ 设置页新增**两段**（会话 10 字段 + 上下文管理策略 10 字段），**按后端 schema 动态渲染** |
| ②「模型供应商」加 TTS + 嵌入式供应商 | ✅ `provider_type` 从 1 类扩到 3 类（chat / text_to_speech / embedding） |
| ② 加常见厂商预设 | ✅ **47 条**预设（照真 AstrBot v4.27.5 抓的 50 条，剔掉 3 条我们**存不进**的 STT） |
| ② 兼容 OpenAI 两种格式 | ✅ `openai_chat_completion`（/chat/completions）+ `openai_responses`（/responses），**都真能执行** |
| ③ 剔除 agent / 群聊 / 管理员 | ✅ 配置字段有判据专门盯这条（`test_no_agent_or_group_fields_leaked_in`） |
| ④ 更新 AGENTS.md | ✅ |

## 判据与验证（全部实测，非推断）

| 项 | 结果 |
|---|---|
| 设置页真浏览器判据（**新建** `tools/settings_check.py`） | **21/21 通过** |
| 供应商页真浏览器判据（`tools/provider_panel_check.py`，扩了假后端） | **70/70 通过** |
| 主测试 | **614 passed, 1 deselected** |
| host 测试 | **139 passed** |
| 注入 bug 验证 | 预设 3 项 / 配置 5 项 / 设置页 5 项 / bridge 2 项，**全部被抓** |

## 🔴 本轮抓到的真 bug（都有注入验证）

1. **`UpstreamLLMPort` 接不住 `AttributeError`**（启动路径打不通）
   `generate()` 只 `except TypeError`，而装配给它的 `OpenAIChatBackend` **没有 `complete()`**
   → 插件在启动路径上一用模型就炸。**修法：两处都补**——
   ①`generate()` 在缺 `complete` 时**回落 `chat()`**；②给 `OpenAIChatBackend` 补 `complete()`。
   ⚠️ 顺带修掉两个我**自己引入**的缺陷：`complete()` 漏传 `base_url/api_key/timeout`（插件用的是
   它自己那条源，漏传会打到错上游）；`_url()` 对空 base_url 会**静默拼出相对地址**
   （现场离根因很远），现在响亮报错。

2. **形状闸门错杀**：`_llm_backend()` 原来只认 `complete()`，
   修好回落以后它会**拒掉一个本来能用的后端** → 判据改成 `complete` **或** `chat`。

3. **前端 `provider_select` 的 id 落错了元素**：id 设在外层 `<span>` 而不是内层 `<select>`
   → 取值取到 `undefined`、收集改动也读不到真值。判据 ⑤ 当场抓出。

4. **`api()` 把 `error.field` 丢了**：后端 400 明明带了字段名，前端 `new Error` 时没带上
   → 界面上"提示点名了字段"但**红框一个都没有**。判据 ④ 当场抓出。

## 我犯的判据错误（记下来，免得重犯）

- **取证装晚了**：`settings_check.py` 第一版在 `Page.reload` **之后**才覆盖 `window.fetch`，
  而 `boot()` 早就请求完 `/api/v1/config-profile` → "页面确实请求了" 读到 0 次，
  **看着像产品缺陷，其实是取证装晚了**。改用 `Page.addScriptToEvaluateOnNewDocument`。
- **判据测错了对象**：`test_tests_do_not_write_project_data_dir` 断言"项目里那个文件不存在"，
  但**正在跑的生产服务**（cwd 就是项目根）会合法创建它 → 判据因为"服务在跑"而失败。
  → 改成测**隔离性本身**（保存后落在 tmp，且项目路径的 sha256 没变）。
  **通则：判据要测"该发生的行为"，不是测"某个文件恰好不在"。**

## 给后来者（架构上的两个决定）

1. **TTS/STT 只做配置层**（情绪选的）：能建供应商、能在会话里选默认 TTS 模型、有触发概率，
   但**暂不出声**。做出声要多做音频链路 + 面板播放器，是独立一轮。
2. **不照抄 AstrBot 的 per-类型字段名**（它 embedding 用 `embedding_api_key`…、TTS 用 `voice/rate`）：
   我们对所有类型**统一**用 `key`/`api_base`/`timeout`/`proxy`/`custom_headers`。
   理由：本轮只做配置层，那些专属字段**没有消费方**，存了就是"看起来支持、其实没人读"；
   将来真做 TTS 出声 / 嵌入检索时再加，那时才知道默认值该给什么。
