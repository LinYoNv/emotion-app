# AstrBot「模型供应商」真实契约（实测抓取）

> 2026-09-20 从**本机正在跑的 AstrBot v4.27.5**（`http://127.0.0.1:6185`，就是
> `http://110.42.14.233:31111` 那个站）实测抓下来的。
> **我们的实现要对齐这份，而不是我原来自己设计的形状。**
>
> ⚠️ 这份是**行为契约**（真实响应），不是猜的。

---

## 0. 关键结论：**两层结构**（我原来做成了单层，这是"功能不对"的根源）

```
provider_sources   ← 端点层：一个 API 端点（base_url + key + timeout）
      ↑ 1 : N
provider           ← 模型层：挂在某个端点下的具体模型
```

* **左侧列表 = `provider_sources`**（截图里那 7 个：mx哈基米/qwen/xy-x/mxgpt/deepseek/mx-x/xy-哈基米）
* **右侧"模型"区 = `provider`**（挂在选中源下面的模型）
* **`default_provider_id` 形如 `"deepseek/deepseek-v4-flash"`** = `源id/模型id`

🔴 **我原来的设计（单层 `{id,name,model,base_url,key}`）表达不了这个** ——
所以用户看到"功能不对"。必须改成两层。

---

## 1. 认证

```bash
POST /api/auth/login
{"username":"astrbot","password":"<pwd>"}
→ {"status":"ok","data":{"token":"<JWT>"}}

# 之后所有请求：
Authorization: Bearer <JWT>
```
⚠️ 与本项目其它 API 的 `{status, message, data}` 信封一致（我们的 `api_admin` 风格不同，
**这里对齐 AstrBot 的信封**，因为面板是照它做的）。

---

## 2. 端点层 `provider_sources`

### `GET /api/v1/provider-sources`
```json
{"status":"ok","message":null,
 "data":{"provider_sources":[
   {"provider":"google",              // 厂商实现：openai / google / deepseek ...
    "type":"openai_chat_completion",  // 适配器类型
    "provider_type":"chat_completion",// ★ 分类：chat_completion / embedding / text_to_speech / ...
    "key":["sk-..."],                 // ★★ 是【列表】不是字符串！
    "api_base":"https://api.mxzzz.xyz/v1",
    "timeout":120,
    "proxy":"",
    "custom_headers":{},              // 自定义请求头（截图里"自定义请求头"那块）
    "id":"mx哈基米",                   // ★ 用户起的名字，可含中文
    "enable":true}]}}
```

### 其余端点（同前缀 `/api/v1`）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/provider-sources` | 列表 |
| POST | `/provider-sources` | 新增 |
| GET/PUT/DELETE | `/provider-sources/by-id?id=<id>` | 单条（**用 query 参数**，不是路径） |
| GET | `/provider-sources/models` | 全部源的模型 |
| GET | `/provider-sources/<源id>/models` | 某源的**可用模型列表** |
| GET | `/provider-sources/<源id>/providers` | 某源下已配置的模型 |
| POST | `/provider-sources/<源id>/providers` | 给某源加模型 |

### `GET /api/v1/provider-sources/<id>/models` 响应
```json
{"status":"ok","data":{
  "models":["codex-auto-review","gpt-5.5","gpt-5.6-sol", ...],
  "provider_source_id":"mxgpt",
  "model_metadata":{"gpt-5.5":{"id":"gpt-5.5","reasoning":true,"tool_call":true,
                               "modalities":{"input":["text","image","pdf"],"output":["text"]}}}}}
```
⭐ 这就是截图里「**获取模型列表**」按钮干的事。

---

## 3. 模型层 `provider`

### `GET /api/v1/providers`
```json
{"status":"ok","data":{
  "providers":[
    // ① 直连式（自己带 key/api_base，如 embedding / tts）
    {"id":"openai_embedding","type":"openai_embedding","provider":"openai",
     "provider_type":"embedding","enable":true,
     "embedding_api_key":"sk-...","embedding_api_base":"...","embedding_model":"BAAI/bge-m3",
     "embedding_dimensions":1024,"embedding_dimensions_mode":"auto","timeout":20,"proxy":""},

    // ② 引用式（★ 挂到某个源上，这是主流形态）
    {"id":"mxgpt/gpt-5.6-sol",          // ★ 源id/模型id
     "enable":true,
     "provider_source_id":"mxgpt",       // ★ 指向 provider_sources
     "model":"gpt-5.6-sol",
     "modalities":["text","image","audio","tool_use"],
     "custom_extra_body":{},
     "max_context_tokens":0,
     "reasoning":false}],
  "model_metadata":{...}}}
```

### 其余
| 方法 | 路径 |
|---|---|
| GET/POST | `/providers` |
| GET/PUT/DELETE | `/providers/by-id?id=<id>` |
| PATCH | `/providers/enabled` |
| POST | `/providers/test` |
| GET | `/providers/<id>`（**id 要 URL 编码**，含 `/`） |
| PUT/DELETE | `/providers/<id>` |
| PATCH | `/providers/<id>/enabled` |
| POST | `/providers/<id>/test` |

### `POST /api/v1/providers/<id>/test`（**URL 编码 id**）响应
```json
{"status":"ok","data":{"id":"mxgpt/gpt-5.6-sol","model":"gpt-5.6-sol",
  "type":"chat_completion","name":"mxgpt/gpt-5.6-sol",
  "status":"available","error":null}}
```
实测：`POST /api/v1/providers/mxgpt%2Fgpt-5.6-sol/test` → `status: "available"`。

---

## 4. 默认模型

```
config.provider_settings.default_provider_id = "deepseek/deepseek-v4-flash"
```
即 **`<源id>/<模型id>`**。

---

## 5. UI 结构（截图对照）

```
┌─ 模型供应商 ────────────────────────────────────────────┐
│ [对话] [Agent 执行器] [语音转文字] [文字转语音] [嵌入] [重排]  ← ★ 按 provider_type 分类
├──────────────┬──────────────────────────────────────────┤
│ 提供商源  +新增│ mx哈基米                                  │
│ ┌──────────┐ │ https://api.mxzzz.xyz/v1                 │
│ │mx哈基米  🗑│ │ ── 设置 ──────────────────────────────   │
│ │https://..│ │ ID / API Key / API Base URL              │
│ ├──────────┤ │ ── 高级配置... ───────────────────────    │
│ │qwen      │ │ 超时时间 / 代理地址 / 自定义请求头          │
│ │xy-x      │ │ ── 模型 ─────────────────────────────    │
│ │mxgpt     │ │ 可用模型 N  [搜索] [获取模型列表] [自定义模型]│
│ │deepseek  │ │ 已配置的模型                            │
│ └──────────┘ │                                          │
└──────────────┴──────────────────────────────────────────┘
```

⚠️ **情绪明确说：`情绪app` 不需要 Agent 能力 → 不做「Agent 执行器」那一栏**。
   我们只做**对话**（`chat_completion`）。其余分类（embedding/tts/stt/rerank）
   **如实不显示**（不是假装有）。

---

## 6. 我们要做的（对齐后）

| # | 要做 |
|---|---|
| 1 | **改两层结构**：`provider_sources` + `provider`（模型挂在源下） |
| 2 | `key` 用**列表**（照真实契约） |
| 3 | 加 `provider`（厂商）/`type`（适配器）/`custom_headers`/`proxy`/`enable` 字段 |
| 4 | 「**获取模型列表**」按钮 → `GET /provider-sources/<id>/models` |
| 5 | 模型的 `id` 用 `源id/模型id`；默认值同形 |
| 6 | **UI 照截图布局**：左侧源列表 + 右侧"设置/高级配置/模型"三段 |
| 7 | 只做 `chat_completion` 分类，**不做 Agent 执行器** |


---

## 7. 迁移与兼容（**冻结**）

现有 `data/providers.json` 是**单层旧格式**：
```json
{"default_provider_id":"fast","providers":[{"id":"fast","name":"快速响应",
  "model":"gemini-3.5-flash","base_url":"https://api.syuan.org/v1","api_key":"sk-x","timeout":60}]}
```

### 新格式（两层，对齐 AstrBot）
```json
{
  "version": 2,
  "provider_sources": [
    {"id":"syuan","provider":"openai","type":"openai_chat_completion",
     "provider_type":"chat_completion","key":["sk-x"],
     "api_base":"https://api.syuan.org/v1","timeout":60,"proxy":"",
     "custom_headers":{},"enable":true}
  ],
  "providers": [
    {"id":"syuan/gemini-3.5-flash","provider_source_id":"syuan",
     "model":"gemini-3.5-flash","enable":true,"modalities":["text"]}
  ],
  "default_provider_id":"syuan/gemini-3.5-flash"
}
```

### 🔴 迁移规则（**自动、不丢数据、留日志**）
* 读到 `version` 缺失/为 1 → **自动迁移**：旧的一条 `{id,name,model,base_url,api_key,timeout}`
  → **一个源**（`id` = 旧的 `id`，`api_base` = 旧 `base_url`，`key=[旧api_key]`）
  + **一个模型**（`id` = `<源id>/<model>`，`model` = 旧 `model`）；
  `default_provider_id` 同步改成 `<源id>/<model>`。
* 迁移**必须落盘**（避免每次启动都迁），并**记一条 INFO 日志**说明迁移了几条
  （项目铁律：不静默改用户数据）。
* 迁移后**旧字段不再写回**（只写新格式）。
* 解析失败 → **记 ERROR + 按空处理**（不许崩、不许静默）。

### 兼容期（可选，降低风险）
`GET /api/v1/providers` 的响应里**同时**给出 `provider_sources`（新）——
但**不要**再造假的旧字段（会让人以为还有旧结构）。

---

## 8. 分工（**避免撞车**）

| 谁 | 独占文件 |
|---|---|
| **子代理 A（后端）** | `emotion_app/infra/providers_store.py`、`emotion_app/web/api_providers.py`、`tests/test_providers_api.py` |
| **子代理 B（前端）** | `emotion_app/web/static/{index.html,app.js,app.css}`、`tools/provider_panel_check.py` |
| **我（良芙）** | `host/astrbot_host/provider.py`、`emotion_app/web/provider_bridge.py`、`plugin_host.py`、`docs/` |

⚠️ A 不碰静态文件；B 不碰 `.py`。


---

## 9. 🔴 已实测复现的坑（写进契约，避免重犯）

### 9.1 模型 id **必须允许 `/`** —— 我实测踩到了

我造了一份两层格式配置打真服务，**装配出 0 个供应商**（应为 1）。

根因：`emotion_app/infra/providers_store.py:58`
```python
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")   # ❌ 不允许 `/`
```
但真实契约里**模型 id 就是 `源id/模型id`**（实测 `"deepseek/deepseek-v4-flash"`、
`"mxgpt/gpt-5.6-sol"`），`default_provider_id` 同形。

→ **两类 id 规则不同**：
| 层 | 规则 |
|---|---|
| `provider_sources[].id` | 用户起的名字，**允许中文**（真实值 `mx哈基米`、`xy_哈基米`），不允许 `/` |
| `providers[].id` | **必须 `<源id>/<模型名>`**，必须允许 `/`，且前缀要指向存在的源 |

### 9.2 `key` 是**列表**不是字符串

真实契约：`"key": ["sk-..."]`。
⚠️ 不处理列表的话 `str(["sk-x"])` 会变成 `"['sk-x']"` 发给上游 → **401**。
我们的 `provider_bridge._first_key()` 已处理（兼容旧格式的裸字符串）。

### 9.3 展平是**必需**的（不是可选优化）

配置是两层，但 AstrBot 的 `Provider` 是**一个模型一个实例**，
而**模型自己不带 `api_base`/`key`**（那些在源上）→
不展平的话每个模型都没有连接信息 → 全部调不通。
→ `provider_bridge.flatten_two_layer()` 负责这件事（已验证：旧格式直通、新格式展平、悬空引用跳过）。


---

## 10. 当前进度（2026-09-20 18:05）

| 项 | 状态 |
|---|---|
| 真实契约抓取（本机 AstrBot v4.27.5） | ✅ 完成（本文档） |
| 宿主 `Provider` 的 `type`/`provider_type` 对齐 | ✅ 完成（`openai_chat_completion` + `chat_completion`） |
| 两层 → 扁平展平（`flatten_two_layer`） | ✅ 完成 + 6 条判据 + 注入验证 |
| `key` 列表兼容（`_first_key`） | ✅ 完成 + 注入验证 |
| 后端两层改造（store + API） | 🔄 子代理进行中 |
| 前端按截图重做 | 🔄 子代理进行中（代码已写，待后端上线后终验） |

### ⚠️ 踩到并已记录的坑

1. **模型 id 必须允许 `/`**（§9.1）—— 我实测装配出 0 个供应商，根因是
   `ID_PATTERN` 不允许 `/`，而真实契约里模型 id 就是 `源id/模型id`。
2. **`key` 是列表**（§9.2）—— 不处理会让 `str(list)` 变成 `"['sk-x']"` 发给上游 → 401。
3. **`type` ≠ `provider_type`**（§9 补充）—— 真实 AstrBot 是
   `type="openai_chat_completion"`（适配器协议）+ `provider_type="chat_completion"`（用途分类）。
   我第一版两个都塞成 `chat_completion`，虽能工作但**与真实现行为分叉**。

### 🔴 一个必须记住的教训

**注入 bug 验证后必须确认"真的还原了"**：
我做 `_first_key` 的注入验证时，第二次还原**没生效**（`if False:  # INJECT` 残留在源码里），
是**测试抓住了它**（2 条判据挂）。
→ 通则：注入验证之后要**全仓 grep 注入标记**，不能只信 `cp` 那一下。
