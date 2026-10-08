# 情绪app · 接口文档

> **版本**：2026-10-02 · 服务端口 **6191**
> ⚠️ 本文的**路由清单是从运行中的 app 里取出来的**（`app.url_map`），不是 grep 源码 ——
> 实测 grep 只能看到 18 条，而真实有 **67 条**（很多路由在子模块里注册）。
>
> **证据等级**：`[实测]` = 真发过请求 · `[源码]` = 读代码 · `[未验证]` = 未跑过

---

## 1. 通用约定

### 1.1 基础

| 项 | 值 |
|---|---|
| Base URL | `http://<host>:6191` |
| 编码 | UTF-8，JSON 请求/响应 |
| 时间戳 | **Unix 秒（浮点）**，例 `1790598728.9270039` |

### 1.2 🔴 鉴权（**两种，但有一个例外**）

```
Authorization: Bearer <EMOTION_AUTH_ACCESS_TOKEN>      # 推荐，App / API 调用用这个
Cookie: <登录后拿到的会话 cookie>                        # 面板用这个
```

| 范围 | 认什么 | 证据 |
|---|---|---|
| `/v1/*`（OpenAI 兼容） | **Bearer 令牌** 或 cookie | `[源码]` `_require_auth_or_api_key()` |
| `/api/v1/*`（管理接口，**绝大多数**） | **Bearer 令牌** 或 cookie | `[源码]` 蓝本注册时传入 `_require_auth_or_api_key`（`app.py:785`） |
| 🔴 **`GET /api/v1/status`** | **只认 cookie** | `[实测]` 带 Bearer 调 → **401**；`[源码]` 它用的是 `_require_auth()`（`app.py:202`） |

⚠️ **那个例外是真实的不一致**（不是文档写漏）：
`app.py:86-89` 的注释说「两条路径都接受令牌当 Bearer」，但 `status` 那个端点漏改了。
**⇒ 如果你的客户端只用令牌（不登录拿 cookie），`/api/v1/status` 会 401。
用 `/health` 做健康检查即可（它不需要鉴权）。**

**登录拿 cookie**：
```bash
POST /api/v1/auth/login
{"token": "<EMOTION_AUTH_ACCESS_TOKEN>"}
→ 200，Set-Cookie（HttpOnly）
```
⚠️ **登录有失败限流**（10 次 / 5 分钟）`[源码]`。

### 1.3 错误形状

```json
{"error": {"message": "未授权", "type": "unauthorized"}}
```
| 状态码 | 含义 |
|---|---|
| 400 | 参数不合法 |
| 401 | 未鉴权 |
| 404 | 不存在 |
| 502 | **上游调用失败**（`type: upstream_error`） |

⚠️ **流式请求出错时**：先返回 200 + 一个 SSE 帧，**再在流里发 error 帧**：
```
data: {"error": {"message": "上游调用失败：上游返回 503", "type": "upstream_error"}}
data: [DONE]
```
`[实测]` —— **客户端必须检查流内的 `error` 字段**，不能只看 HTTP 状态码。

---

## 2. 🌐 OpenAI 兼容接口（**App 对接用这两个**）

### 2.1 `GET /v1/models`

模型列表（**回的是运行时生效的模型**，不是 `.env` 启动值）。

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:6191/v1/models
```
```json
{"data":[{"created":1790924177,"id":"gemini-3.8-flash-high",
          "object":"model","owned_by":"emotion-app"}],
 "object":"list"}
```
`[实测]`

### 2.2 `POST /v1/chat/completions`

**聊天（支持流式）** —— 这是 App 唯一要调的接口。

**请求体**：
```json
{
  "model": "gemini-3.8-flash-high",
  "stream": true,
  "messages": [{"role": "user", "content": "说两个字"}],
  "session_id": "可选：会话 id；不传 = 无状态单轮"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `messages` | ✅ | `[{role, content}]`；`role` 支持 `user`/`assistant`/`system` |
| `stream` | — | `true` = SSE 流式（**面板永远走这条**） |
| `model` | — | ⚠️ **必须填后端当前生效的模型名**（用 `/v1/models` 查）。填错 ⇒ 后端原样转发给上游 ⇒ **503** `[实测踩到]` |
| `session_id` | — | 传了才存历史；不传是无状态单轮 |

**流式响应**（`Content-Type: text/event-stream`）：
```
data: {"id":"chatcmpl-…","object":"chat.completion.chunk","created":…,
       "model":"gemini-3.8-flash-high",
       "choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {…"delta":{"content":"在呢。"}…}

data: {…"delta":{},"finish_reason":"stop"}

data: [DONE]
```
`[实测]`

**非流式响应**：
```json
{"id":"chatcmpl-…","object":"chat.completion","created":…,
 "model":"…","choices":[{"index":0,
   "message":{"role":"assistant","content":"在呢。"},"finish_reason":"stop"}],
 "usage":{…}}
```

**扩展字段**（本项目特有）：
| 字段 | 说明 |
|---|---|
| `reasoning_content` | 推理过程（上游有的话） |
| `images` | 🔴 **生图结果**（`[{url, kind}]`）—— ⚠️ **爱语主聊天不读它**，图到不了它的 UI |

---

## 3. 🖥️ 面板后端接口（`/api/v1/*`）

> 面板前端用的；App 一般不需要，但**会话/设置相关的对做 App 很有用**。

### 3.1 会话

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/v1/sessions` | GET | 会话列表 |
| `/api/v1/sessions` | POST | 新建会话 |
| `/api/v1/sessions/<id>` | GET / PATCH / DELETE | 单条会话 |
| `/api/v1/sessions/<id>/messages` | GET | 消息（`?limit=50`） |
| `/api/v1/sessions/<id>/clear` | POST | 清空消息 |
| `/api/v1/sessions/<id>/read` | POST | 标记已读（**未读清零**） |
| `/api/v1/sessions/<id>/summarize` | POST | 生成总结 |
| `/api/v1/sessions/<id>/umo` | POST | 设置该会话的 umo |

**`GET /api/v1/sessions` 真实响应** `[实测]`：
```json
{"data": [{
  "created_at": 1790598728.9270039,
  "id": "plugin:63bde1ed6ef60b85",
  "message_count": 6,
  "persona_id": "",
  "platform": "11",
  "policy": {"budget_chars": 24000, "mode": "recent_only",
             "protected_tail": 20, "summary_max_chars": 2000,
             "summary_trigger": 40},
  "summarized_upto": 0,
  "summary": "",
  "title": "陪伴（主动消息）",
  "unread": …,
  "user_id": "default"
}]}
```

### 3.2 未读

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/v1/unread` | GET | 各会话未读数 |

```json
{"sessions": {"plugin:63bde1ed6ef60b85": 6}, "total": 6}
```
`[实测]` ⚠️ **只有插件主动消息会让未读 +1**；打开会话（`/read`）清零。

### 3.3 人设

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/v1/personas` | GET / POST | 列表 / 新建 |
| `/api/v1/personas/<id>` | GET / PATCH / DELETE | 单条 |
| `/api/v1/personas/<id>/activate` | POST | 设为当前 |
| `/api/v1/personas/deactivate` | POST | 取消当前 |
| `/api/v1/personas/<id>/avatar` | GET | 头像 |
| `/api/v1/personas/<id>/duplicate` | POST | 复制 |
| `/api/v1/personas/<id>/preview` | GET | 预览（组装后的提示词） |
| `/api/v1/personas/<id>/export` | GET | 导出 |
| `/api/v1/personas/export` | GET | 全量导出 |
| `/api/v1/personas/import` | POST | 导入 |
| `/api/v1/personas/<id>/sections` | PUT | 更新片段 |
| `/api/v1/personas/<id>/reference` | GET / POST / DELETE | **人设参考图**（生图用） |

**`GET /api/v1/personas` 真实响应** `[实测]`：
```json
{"active_persona_id": "",
 "data": [{"api_key_configured": false, "api_url": "", "avatar_path": "",
           "created_at": 1789820408.2011685,
           "creator_notes": "…", "description": "…", "name": "…",
           "personality": "…", "scenario": "…", …}]}
```

### 3.4 设置

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/v1/settings` | GET / PATCH | **运行时设置**（覆盖 `.env`） |
| `/api/v1/settings/models` | POST | 拉取上游可用模型 |
| `/api/v1/settings/overrides` | DELETE | 清掉全部覆盖（回落 `.env`） |

**`GET /api/v1/settings` 真实响应** `[实测]`：
```json
{"settings": {
   "active_persona_id": "",
   "overrides": {"active_persona_id": false,
                 "upstream_base_url": true, "upstream_model": true},
   "upstream": {"api_key_configured": true, "api_key_source": "env",
                "base_url": "https://api.syuan.org/v1",
                "model": "gemini-3.8-flash-high"}},
 "writable_fields": ["upstream_base_url","upstream_model",
                     "upstream_api_key","active_persona_id"]}
```
🔴 **`settings.upstream.model` 才是"当前生效的模型"** ——
`.env` 里那个是启动值，会被它覆盖 `[实测]`。

### 3.5 插件

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/v1/plugins` | GET | 插件列表（含 `state`） |
| `/api/v1/plugins/<plugin_id>` | GET | **插件详情**（六类 components） |
| `/api/v1/plugins/inactivated` | PUT | **停用/启用**（**整体替换**语义） |
| `/api/v1/plugins/pages` | GET | 拓展页列表 |
| `/api/v1/plugins/<id>/pages/<name>` | GET | 打开插件拓展页（HTML） |
| `/api/v1/plugins/<id>/pages/<name>/<asset>` | GET | 插件页静态资源 |
| `/api/v1/plugins/...` 之外：`/<plugin_dir>/<path>` | 全方法 | 插件自己的 API（垫片路由） |

**`PUT /api/v1/plugins/inactivated` 请求体**：
```json
{"inactivated_plugins": ["prompt_tools.main", "astrbot_plugin_together_companion.main"]}
```
⚠️ **整体替换**（不是增量）；⚠️ **保存即落盘，但需重启服务才生效**（返回里有 `restart_required`）`[实测]`。

### 3.6 供应商

| 路由 | 方法 |
|---|---|
| `/api/v1/providers` · `/api/v1/providers/by-id` · `/api/v1/providers/<id>` | GET/POST/PUT/PATCH/DELETE |
| `/api/v1/provider-presets` | GET（**预设模板**，只给"厂商+适配器+默认 api_base"） |
| `/api/v1/provider-sources` · `/by-id` · `/<id>/models` · `/<id>/providers` | GET/POST |

⚠️ **`executable_by_us`**：预设里标 `false` 的表示「能存配置但**我们不能执行**该适配器」
（实测 57 条预设只有 **26** 条可执行）`[实测]`。

### 3.7 其它

| 路由 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | **健康检查（无需鉴权）** ✅ 推荐用它 |
| `/api/v1/status` | GET | 详细状态 ⚠️ **只认 cookie** |
| `/api/v1/logs` | GET | 运行日志 |
| `/api/v1/context-modes` | GET | 上下文模式 |
| `/api/v1/config-profile` | GET/PATCH/DELETE | 配置档案 |
| `/api/v1/images/<filename>` | GET | **生成的图片**（相对路径，同源） |
| `/` | GET | 面板首页 |
| `/static/<path>` | GET | 面板静态资源（带 `no-cache`） |

---

## 4. 完整路由清单（67 条）

> **来源**：从运行中的 app 取 `app.url_map` `[实测]`。⚠️ 源码 grep 只能看到 18 条。

```
GET                       /
DELETE|GET|PATCH|POST|PUT /<plugin_dir>/<path:rest>
DELETE|GET|PATCH|POST|PUT /api/plug/<path:plugin_path>
DELETE|GET|PATCH|POST|PUT /api/plugins/extensions/<path:plugin_path>
POST                      /api/v1/auth/login
POST                      /api/v1/auth/logout
DELETE                    /api/v1/config-profile
GET                       /api/v1/config-profile
PATCH                     /api/v1/config-profile
GET                       /api/v1/context-modes
GET                       /api/v1/images/<path:filename>
GET                       /api/v1/logs
GET                       /api/v1/personas
POST                      /api/v1/personas
DELETE                    /api/v1/personas/<persona_id>
GET                       /api/v1/personas/<persona_id>
PATCH                     /api/v1/personas/<persona_id>
POST                      /api/v1/personas/<persona_id>/activate
GET                       /api/v1/personas/<persona_id>/avatar
POST                      /api/v1/personas/<persona_id>/duplicate
GET                       /api/v1/personas/<persona_id>/export
GET                       /api/v1/personas/<persona_id>/preview
DELETE                    /api/v1/personas/<persona_id>/reference
GET                       /api/v1/personas/<persona_id>/reference
POST                      /api/v1/personas/<persona_id>/reference
PUT                       /api/v1/personas/<persona_id>/sections
POST                      /api/v1/personas/deactivate
GET                       /api/v1/personas/export
POST                      /api/v1/personas/import
GET                       /api/v1/plugins
GET                       /api/v1/plugins/<plugin_id>
GET                       /api/v1/plugins/<plugin_id>/pages/<page_name>
GET                       /api/v1/plugins/<plugin_id>/pages/<page_name>/
GET                       /api/v1/plugins/<plugin_id>/pages/<page_name>/<path:asset_path>
PUT                       /api/v1/plugins/inactivated
GET                       /api/v1/plugins/pages
GET                       /api/v1/provider-presets
GET                       /api/v1/provider-sources
POST                      /api/v1/provider-sources
GET|POST                  /api/v1/provider-sources/<source_id>/models
GET                       /api/v1/provider-sources/<source_id>/providers
POST                      /api/v1/provider-sources/<source_id>/providers
DELETE|GET|PUT            /api/v1/provider-sources/by-id
GET                       /api/v1/provider-sources/models
GET|POST                  /api/v1/providers
DELETE|GET|PATCH|POST|PUT /api/v1/providers/<path:provider_id>
DELETE|GET|PUT            /api/v1/providers/by-id
GET                       /api/v1/sessions
POST                      /api/v1/sessions
DELETE                    /api/v1/sessions/<session_id>
GET                       /api/v1/sessions/<session_id>
PATCH                     /api/v1/sessions/<session_id>
POST                      /api/v1/sessions/<session_id>/clear
GET                       /api/v1/sessions/<session_id>/messages
POST                      /api/v1/sessions/<session_id>/read
POST                      /api/v1/sessions/<session_id>/summarize
POST                      /api/v1/sessions/<session_id>/umo
GET                       /api/v1/settings
PATCH                     /api/v1/settings
POST                      /api/v1/settings/models
DELETE                    /api/v1/settings/overrides
GET                       /api/v1/status
GET                       /api/v1/unread
GET                       /health
GET                       /static/<path:filename>
POST                      /v1/chat/completions
GET                       /v1/models
```

**怎么自己再取一次**（路由会随开发变化）：
```bash
cd <项目目录>
bash -c 'set -a; source .env; set +a; .venv/bin/python -c "
import asyncio
from emotion_app.infra.config import load_config
from emotion_app.web.app import create_app
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.auth import AuthManager
cfg = load_config()
async def main():
    store = SqliteStore(cfg.storage.sqlite_path); await store.open()
    app = create_app(cfg, store, AuthManager(access_token=cfg.auth.access_token))
    for p, m in sorted({(str(r), \"|\".join(sorted(r.methods - {\"HEAD\",\"OPTIONS\"})))
                        for r in app.url_map.iter_rules()}):
        print(f\"{m:20} {p}\")
    await store.close()
asyncio.run(main())
"'
```

---

## 5. 客户端注意事项（**踩过的坑**）

| # | 注意 | 原因 |
|---|---|---|
| 1 | **填对 `model`** | 填错 → 后端原样转发上游 → **503** `[实测]` |
| 2 | **查生效模型别抄 `.env`** | `.env` 是启动值，运行时设置会覆盖它 |
| 3 | **流式要检查流内 `error` 帧** | 出错时 HTTP 仍是 200 |
| 4 | **`/api/v1/status` 只认 cookie** | 用令牌调会 401；健康检查用 `/health` |
| 5 | **`PUT /plugins/inactivated` 是整体替换** | 不是增量；且需重启才生效 |
| 6 | **生图靠 SSE `images` 扩展字段** | 标准 OpenAI 客户端不认；爱语 UI 也不读 |
| 7 | **无 CORS 头** | 同源前端（面板、套壳）不受影响；**跨域/`file://` 必须先补 CORS** |
| 8 | **明文 HTTP** | 内网可用；**公网必须挂 TLS 反代** + `EMOTION_SERVER_HTTPS_ONLY=true` |

---

## 6. 相关文档

| 文档 | 内容 |
|---|---|
| `../README.md` | 项目总览（配置项、部署） |
| `APP_PLAN.md` | 手机 App 接入计划 |
| `NEXT_STEPS.md` | 规划 |
| `GAP_TO_GOAL.md` | 缺口分析 |
