# 情绪app — 兼容性审计清单（只读侦察）

> 生成方式：只读遍历 `/root/Ai-works/情绪app`。**本次审计未修改任何既有文件**，只新建本文件。
> 每条断言都带 `file:line`。凡「ABSENT」= 在指定搜索范围内**零匹配**。
> 搜索范围约定：`OUR CODE` = `emotion_app/` + `host/astrbot_host/`（排除 `__pycache__`、`data/`）。

---

## 0. 顶层结构（事实）

| 路径 | 内容 |
|---|---|
| `emotion_app/domain/` | 15 个模块，2,013 行（`wc -l` 汇总） |
| `emotion_app/infra/` | 5 个模块，2,976 行 |
| `emotion_app/storage/` | 3 个模块，334 行 |
| `emotion_app/web/` | 9 个模块 + 4 个静态文件，4,119 行（不含 static） |
| `host/astrbot_host/` | 12 个模块 + `compat/` 影子包 + `loader/`，3,979 行（不含 compat） |
| `tests/` | 21 个 `test_*.py`，**584** 个 `def test_` |
| `host/tests/` | 9 个 `test_*.py`，**136** 个 `def test_`；另有 3 个 `tools_verify_*`、2 个 `probe_*` |
| `tools/` | 7 个真浏览器/CDP 判据脚本 + 1 个注入验证脚本 |
| `docs/` | 仅 5 个 md：`ASTRBOT_CONFIG_PAGE.md`、`ASTRBOT_PROVIDER_CONTRACT.md`、`NEXT_STEPS.md`、`P0_P1_TASKS.md`、`PROVIDER_CONTRACT.md` |
| 部署依赖 | `pyproject.toml:6-11` = quart / hypercorn / aiosqlite / httpx（**仅 4 个**）；dev 加 pytest / pytest-asyncio（`pyproject.toml:14`） |
| pytest 配置 | `pyproject.toml:23-26`：`testpaths=["tests"]`、`asyncio_mode="auto"`、`filterwarnings=["error::DeprecationWarning"]` |

**ABSENT**：`emotion_app/` 下**没有** `domain/ports.py` 以外的端口定义文件；`docs/` 下**没有** `BACKEND_FRAMEWORK.md` / `PERSONA_PROMPT_SYSTEM.md` / `AIYU_MEMORY_REVIEW.md` / `FEATURE_GAP_ANALYSIS.md` / `PLATFORM_POSITIONING.md` — 这些文件在源码注释里被反复引用（如 `emotion_app/storage/base.py:5`、`emotion_app/domain/context.py:4`、`emotion_app/domain/prompt.py` 顶部），但 `docs/` 里**不存在**。当前 `docs/` 只有上表 5 个文件。

---

## 1. 领域端口 / 抽象边界

### 1.1 `emotion_app/domain/ports.py`（189 行）— 3 个 Protocol

| Protocol | 装饰器 | 方法签名 | 行 |
|---|---|---|---|
| `ChatBackend` | `@runtime_checkable` | `async open() -> None` | `ports.py:52` |
| | | `async close() -> None` | `ports.py:56` |
| | | `async chat(messages: list[Message], *, model: str, base_url: str = "", api_key: str = "", timeout: float = 60.0, tools: list[dict[str, Any]] | None = None, extra: dict[str, Any] | None = None) -> Any` | `ports.py:60-72` |
| | | `chat_stream(messages, *, model, base_url="", api_key="", timeout=60.0, tools=None, extra=None) -> AsyncIterator[Any]`（**同步 def**，返回异步迭代器） | `ports.py:74-86` |
| `ImageBackend` | `@runtime_checkable` | `@property name -> str` | `ports.py:103-106` |
| | | `is_configured() -> bool` | `ports.py:108` |
| | | `async open() -> None` / `async close() -> None` | `ports.py:112`/`:114` |
| | | `async generate(*, prompt: str, kind: str = "text2img", size: str = "", reference_image: str = "", n: int = 1, extra: dict | None = None) -> Any` | `ports.py:116-138` |
| `ToolDispatch` | `@runtime_checkable` | `@property name -> str` | `ports.py:157-160` |
| | | `build_payload(tools: list[Any]) -> dict[str, Any]` | `ports.py:162-173` |
| | | `extract_calls(response: Any) -> list[ToolCall]` | `ports.py:175` |
| | | `extract_stream_calls(fragments: list[Any]) -> list[ToolCall]` | `ports.py:179` |
| | | `render_result(result: ToolResult) -> Message` | `ports.py:183` |
| | | `prompt_sections(tools: list[Any]) -> list[Any]` | `ports.py:187` |

- 三个 Protocol 都是 `typing.Protocol` + `runtime_checkable`（`ports.py:33`、`:44`、`:94`、`:146`），**不继承 ABC**。
- 实现方：`ChatBackend` → `OpenAIChatBackend`（`emotion_app/domain/backends.py:41`）；`ImageBackend` → `OpenAIImageBackend`（`backends.py:234`）；`ToolDispatch` → `FunctionCallDispatch`（`backends.py:315`）/ `TextTagDispatch`（`backends.py:376`），工厂 `build_dispatch(name, **kwargs)`（`backends.py:472`）。
- **⚠️ 注意：`ToolDispatch.render_result` 在 ports.py 里声明为 1 个参数（`result`），但实际调用点是 2 个参数** —— `emotion_app/domain/chat.py:502` 调 `dispatch.render_result(result, call_id=call.id)`、`chat.py:645` 同款。端口声明与实际调用形状不一致（`ports.py:183` vs `chat.py:502`）。

### 1.2 领域层 → 宿主 的依赖边界

- 搜索 `emotion_app/domain/` 全部 15 个 `.py` 的 **148 行 import 语句**：`import astrbot` / `from astrbot...` / `from host...` / `import host...` → **零匹配**（0/148）。
- `emotion_app/domain/` 里唯一出现 `astrbot` 这个词的地方全在**注释**：`chat.py:83`、`:93`、`config_profile.py:2`、`:6`、`:7`、`:10`、`:14`、`:22`、`:51`、`:56`、`:59`、`:158`、`:178`、`:188`、`:201`。
- 反向：`host/astrbot_host/__init__.py:14-15` 显式写明「`情绪app/emotion_app/domain/` **绝不能** import 本包」；`host/astrbot_host/host_app.py:26-28` 同样声明「接线动作只能发生在这里（或 web 层）」。
- 实际跨层引用（唯一一处领域层 → host 名）是**字符串级端口注入**，不是 import：`ChatService._llm_hook` 由装配层注入（`emotion_app/web/app.py:695-696` 调 `_chat_svc.bind_llm_hook(service.apply_llm_hooks)`）。

**结论**：领域层 → `host/` 的 import 依赖 = **零匹配**。

---

## 2. 存储层

### 2.1 `emotion_app/storage/base.py`（103 行）— `StoreBackend(abc.ABC)`

方法清单（原文顺序，`base.py:33-92`）：

| 方法 | 签名 | 行 |
|---|---|---|
| `backend_name` | `() -> str`（抽象，同步） | `:34` |
| `load_store` | `async () -> dict[str, Any]` 整体文档 | `:40` |
| `load_sections` | `async (keys: list[str]) -> dict[str, Any]` 按需读 | `:44` |
| `exists` | `async () -> bool` | `:52` |
| `save_store` | `async (data: dict) -> None` **整体覆盖** | `:58` |
| `save_sections` | `async (sections: dict) -> None` 局部更新 | `:62` |
| `next_revision` | `async () -> int` | `:68` |
| `export_store` | `async () -> dict[str, Any]` | `:74` |
| `import_store` | `async (data: dict, *, mode: str = "replace") -> None` | `:78` |
| `health_check` | `async () -> dict[str, Any]` | `:87` |
| `close` | `async () -> None` | `:91` |

- 共 **11 个**抽象方法（模块 docstring 自称「起步 9 个」，`base.py:6` —— 与实际 11 个不符）。
- `IMPORT_MODES = ("replace", "merge")`（`base.py:96`）+ `validate_import_mode(mode)`（`base.py:99`）。

### 2.2 `emotion_app/storage/sqlite_store.py`（218 行）

**存储形状 = 分 section 的整文档 JSON 存储，不是 per-entity 表**：

```sql
CREATE TABLE store (section TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT ...);
CREATE TABLE meta  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```
（`sqlite_store.py:26-37`，仅 **2 张表**）

| 项 | 事实 | 行 |
|---|---|---|
| schema 版本 | `SCHEMA_VERSION = 1`，写入 `meta['schema_version']` | `:24`、`:65-68` |
| migrations | **ABSENT** — 无 `migrate` / `ALTER TABLE` / 版本分支任何代码。只有 `SCHEMA_VERSION` 常量与 `export_store()` 里回显 | `:24`、`:180` |
| revision / 乐观并发 | **只有单调计数器，没有并发校验**。`next_revision()` = 读+1 写回（`:150-159`）；`_bump_revision()` = 每次写 +1（`:170-174`）；`current_revision()`（`:161`）。**无 version/etag 参数、无 CAS、写入不校验调用方持有的 revision** | `:150-174` |
| outbox | **ABSENT** | — |
| inbox | **ABSENT** | — |
| export / import | 有：`export_store()` 返回 `{schema_version, revision, backend, data}`（`:178-184`）；`import_store(data, mode)` 支持 `replace`/`merge`（`:186-200`） |
| 事务 | `save_store` = `DELETE FROM store` + `executemany INSERT` + bump + commit（`:128-136`）；`save_sections` = `INSERT ... ON CONFLICT DO UPDATE` + bump + commit（`:138-146`） |
| WAL | `PRAGMA journal_mode=WAL`（`:62`）、`PRAGMA foreign_keys=ON`（`:63`） |
| 健康检查 | `health_check()` 返回 `{ok, backend, path, sections, revision, schema_version}`；异常也结构化返回（`:204-218`） |

⚠️ `load_sections()` 用 f-string 拼 `IN ({placeholders})`（`:112`），placeholders 由 `"?" * len(keys)` 生成 —— 值本身走参数绑定。

### 2.3 `emotion_app/storage/factory.py`（32 行）

- `build_store(cfg: StorageConfig, *, default_factory: Any = None) -> StoreBackend`（`:15`）。
- `"sqlite"` → `SqliteStore(cfg.sqlite_path, default_factory=...)`（`:23-24`）。
- `"json"` → **抛 `StorageError`**，未实现（`:26-30`）。
- 其它名字 → 抛 `StorageError`（`:32`）。

### 2.4 section 布局（在各 repo 里定义，不在 storage 层）

| section | 内容 | 定义处 |
|---|---|---|
| `personas` | `{persona_id: persona_dict}` | `emotion_app/domain/persona_repo.py:18`、`:57-59` |
| `settings` | 运行时覆盖 4 字段 | `emotion_app/domain/settings.py:25`、`:50-55` |
| `sessions` | `{session_id: 会话元数据}` | `emotion_app/domain/session_repo.py:25` |
| `messages` | `{session_id: [消息,...]}` | `emotion_app/domain/session_repo.py:26` |

---

## 3. 聊天链路

### 3.1 `emotion_app/domain/chat.py`（743 行）

**公开函数 / 类**

| 名称 | 位置 | 签名要点 |
|---|---|---|
| `ChatError(Exception)` | `:37` | — |
| `ChatRequest`（`@dataclass(slots=True)`） | `:41-55` | `messages, model, stream, persona, custom_params, fallback_models, session_id, persist` |
| `ChatService.__init__` | `:66-95` | `(config, client: ChatBackend | None = None, settings=None, personas=None)` |
| `bind_llm_hook(hook)` | `:97` | 注入 `async (messages) -> list[Message] | None` |
| `bind_tools(registry, dispatch=None)` | `:130` | — |
| `bind_sessions(sessions)` | `:170` | — |
| `bind_summarizer(summarizer)` | `:174` | — |
| `schedule_summarize(session_id)` | `:178` | `asyncio.create_task` + 强引用集 `_bg_tasks`（`:95`、`:189-191`） |
| `resolve_target(persona, model_override="") -> UpstreamTarget` | `:234` | 优先级：人设覆盖 > 请求 model > 运行时设置 > 环境变量（`:243-253`） |
| `resolve_persona(persona_id="") -> Persona | None` | `:255` | 显式 id > `settings.active_persona_id` |
| `build_messages(req, *, history=None, summary="") -> list[Message]` | `:270` | 见下 |
| `prepare_session(req) -> (Session|None, list[Message], str)` | `:329` | — |
| `record(session_id, *, user_text, assistant_text)` | `:352` | 失败只记日志，不影响回复（`:374-375`） |
| `async complete(req) -> ChatResult` | `:379` | 非流式主入口 |
| `async stream(req) -> AsyncIterator[StreamChunk]` | `:568` | 流式主入口 |
| `parse_chat_request(body: dict) -> ChatRequest`（模块函数） | `:718` | OpenAI 形态 → `ChatRequest` |
| `_extract_attachments(result)`（模块函数） | `:692` | — |

**消息如何组装成上游请求**（`chat.py:270-325`）：

```
[system(人设 prompt.assemble)] [system(摘要, f"【此前对话摘要】\n…")] [历史...] [本轮输入...]
```
- 顺序由 `build_messages` 写死：`out.append(prompt_mod.to_system_message(assembled))`（`:308-309`）→ 摘要 system（`:312-315`）→ `out.extend(history)`（`:317-318`）→ `out.extend(req.messages)`（`:320`）。
- **前缀稳定性**是刻意设计：system 只由人设决定、不含历史（`:279-283` 注释）。
- 校验：至少要有一条 `user`/`assistant`，否则 `ChatError`（`:322-323`）。
- 工具提示词：`_tool_payload_and_sections()` 返回 `(请求体字段, 提示词片段)`；文本标签方式片段走 `sections`，function calling 返回空（`:154-168`、`:285-301`）。
- 上游请求体由 `upstream.build_body()`（`emotion_app/domain/upstream.py:124`）构造，自定义参数走 `apply_nested_params`（`upstream.py:161`）支持 `.` 嵌套。
- **发给上游前会过插件 LLM 钩子**：`messages = await self._apply_llm_hook(messages)`（`complete` `:385`、`stream` `:579`）；钩子未注入/抛错/返回非 list → 原样返回（`:105-128`）。

**上下文 / 摘要策略 —— 确实是「三种模式」**（`emotion_app/domain/context.py`）

| 常量 | 值 | 行 |
|---|---|---|
| `MODE_SEMANTIC` | `"semantic"`（混合：摘要 + 最近原文） | `context.py:26` |
| `MODE_ROLLING_SUMMARY` | `"rolling_summary"`（累积 N 条后总结） | `context.py:27` |
| `MODE_RECENT_ONLY` | `"recent_only"`（固定窗口，**默认**） | `context.py:28` |
| `VALID_MODES` | `(semantic, rolling_summary, recent_only)` | `context.py:30` |

- 三模式与爱语配置键的对应表写在 `context.py:5-10`（`key_mixed_context_enable`→semantic 等）。
- `ContextPolicy`（`context.py:40-91`）：`mode`、`protected_tail=20`、`summary_trigger=40`、`summary_max_chars=2000`、`budget_chars=24000`，含 `validate()` / `to_dict()` / `from_dict()`。
- 公开函数：`build_context(messages, policy, *, summary="") -> ContextResult`（`:130`）、`needs_summary(messages, policy) -> bool`（`:208`）、`to_summarize(messages, policy) -> list[Message]`（`:220`）、`build_summary_messages(chunk, *, previous_summary="") -> list[Message]`（`:242`）、`_apply_budget(result, policy)`（`:182`）。
- `ContextResult`（`:99-118`）：`history` / `summary` / `dropped` / `mode`。
- 预算裁剪**所有模式都走**（`recent_only` 分支 `:161` 也调 `_apply_budget`），从最旧开始丢、至少留一条、单条超预算则截尾（`:191-203`）。
- 摘要**后台异步做**，不在回复链里：`schedule_summarize` → `_summarize_bg`（`:193-226`），摘要与游标**一次写入**（`:213-216`）；`Summarizer.maybe_summarize(...)`（`emotion_app/domain/summarizer.py:58`）、`merge_summary(...)`（`summarizer.py:187`）。

**幂等 / operation id / 重试 / 截止时间 / 取消**（`chat.py` + `web/app.py` 全文检索）

| 概念 | 状态 | 证据 |
|---|---|---|
| `idempotency` / `operation_id` / `request_id` | **ABSENT（零匹配）** | 见 §9 |
| 请求级去重 / 同一请求重放保护 | **ABSENT** | 无任何 request-id / 去重键 |
| 重试 | **有，但是"换模型重试"，不是失败重试**：`_chat_with_fallback`（`:424-462`）在**疑似拒答**时按 `fallback_models` 换下一个 model，最多 `len(models)` 次；`looks_like_refusal()` 判据在 `upstream.py:53`，关键词表 `upstream.py:38` |
| 兜底重试计数 | `ChatRequest.fallback_models`（`:50`）由调用方给；HTTP 层不设 | — |
| 截止时间 / deadline | **ABSENT** — 只有单次请求 `timeout`（`UpstreamTarget.timeout`，`upstream.py:67` 起；来源 `config.upstream.timeout_seconds`，`chat.py:252`）。无整轮 deadline，无超时预算分配 |
| 取消 / cancellation | **ABSENT** — 全 `chat.py`/`app.py` 无 `CancelledError` / `abort` / `is_disconnected` / `request.timeout` 处理 |
| 客户端断连传播 | **ABSENT** — 流式生成器不检查 `request.is_disconnected()`（`app.py:439-514` 的 `sse()` 无此逻辑） |
| 工具防重复 | **有**：`ToolRegistry(max_calls_per_turn=1)`（`emotion_app/__main__.py:99`），拒绝第二次调用并返回 `"本轮已经调用过该工具，不重复执行"`（`emotion_app/domain/tools.py:222-228`） |

**工具循环**（`_run_tool_loop`，`:466-564`）：只跑**一轮**（`:483-484` 注释明说）；第二次请求失败时退化返回 `finish_reason="tool_executed"` 并保住附件（`:541-551`）。
**流式工具**：`extract_stream_calls` → 执行 → 以额外 `StreamChunk(finish_reason="tool_executed")` 推给上层（`:617-646`），然后**再问一次模型**并把新 chunk 继续 yield（`:651-680`）。

### 3.2 HTTP 层如何接插件（`emotion_app/web/app.py`）

- 非流式：**先给插件一次机会** —— `_plugin_first_reply(_last_user_text(req), ...)`（`app.py:398-402`）；插件没接管 → 回落 LLM（`:285-286`）。
- 判定规则只有一处实现：`plugin_reply_text()`（`emotion_app/web/plugin_host.py:56-83`）—— `emitted` 有非空文本 **或** `stopped=True` 才算"插件说话了"。
- 流式路径**不走** `_plugin_first_reply`（`app.py:438` 起直接 `svc.stream(req)`）。

---

## 4. HTTP 接口面（方法 + 路径 + 一句话用途）

### 4.1 `emotion_app/web/app.py`（774 行）

| # | 方法 | 路径 | 用途 | 行 |
|---|---|---|---|---|
| 1 | GET | `/health` | 存活 + 配置自检（**免鉴权**，脱敏）；store 不健康 → 503 | `:120` |
| 2 | POST | `/api/v1/auth/login` | 令牌换会话 cookie（含失败限流 → 429） | `:146` |
| 3 | POST | `/api/v1/auth/logout` | 清会话 + 删 cookie | `:189` |
| 4 | GET | `/api/v1/status` | 运行状态（需鉴权，脱敏） | `:200` |
| 5 | GET | `/v1/models` | OpenAI 形状模型列表（只回**一个** id） | `:338` |
| 6 | POST | `/v1/chat/completions` | 聊天补全（OpenAI 兼容，SSE 流式；先派发插件） | `:364` |
| 7 | GET | `/` | 面板首页（**免鉴权**，`no-cache`） | `:548` |
| 8 | GET | `/static/<path:filename>` | 静态资源（`no-cache`，靠 ETag 走 304） | `:563` |
| 9 | GET | `/api/v1/images/<path:filename>` | 取生成图（**需鉴权**，挡路径穿越） | `:580` |
| 10 | GET/POST/PUT/PATCH/DELETE | `/api/plugins/extensions/<path:plugin_path>` | 插件 Web API 通配派发 | `:701` |
| 11 | 404 handler | — | 统一 JSON 404 | `:520` |
| 12 | 500 handler | — | 统一 JSON 500（不泄漏内部细节） | `:534` |
| — | `before_serving` | — | 启动插件宿主 `EMOTION_PLUGIN_HOST].start()` | `:628-652` |
| — | `after_serving` | — | 停宿主 + 关 store | `:654-660` |

- 鉴权闸门两套：`_require_auth()`（只认会话 cookie，`:77`）与 `_require_auth_or_api_key()`（cookie **或**访问令牌当 Bearer，`:99-114`）。
- 条件注册：只有 `config.plugins.enabled and config.plugins.mount_web_apis` 才挂插件路由（`:621-622`）—— 此时才 import `host/`。
- LLM 钩子接线点：`_mount_plugin_routes` 内 `_chat_svc.bind_llm_hook(service.apply_llm_hooks)`（`:695-696`）。
- `Quart(__name__, static_folder=None)`（`:53`）是刻意的（避免自动 `/static` 路由盖掉 no-cache 头）。

### 4.2 `emotion_app/web/api_admin.py`（1051 行，Blueprint `admin`，`url_prefix="/api/v1"`，`:92`）

| 方法 | 路径 | 用途 | 行 |
|---|---|---|---|
| GET | `/personas` | 人设列表（带 `active` 标记） | `:108` |
| GET | `/personas/<persona_id>` | 单个人设 | `:127` |
| POST | `/personas` | 新建人设 | `:139` |
| PATCH | `/personas/<persona_id>` | 局部更新 | `:161` |
| DELETE | `/personas/<persona_id>` | 删除 | `:181` |
| POST | `/personas/<persona_id>/duplicate` | 复制一份 | `:198` |
| POST | `/personas/<persona_id>/activate` | 设为"当前人设" | `:213` |
| POST | `/personas/deactivate` | 取消当前人设 | `:229` |
| GET | `/personas/<persona_id>/preview` | 预览组装后的 system prompt | `:243` |
| GET | `/settings` | 读运行时设置（key 脱敏） | `:271` |
| PATCH | `/settings` | 改业务配置（上游地址/模型/key/当前人设） | `:283` |
| DELETE | `/settings/overrides` | 清运行时覆盖，回落环境变量 | `:309` |
| GET | `/config-profile` | 读配置 + 字段元数据 `schema` | `:342` |
| PATCH | `/config-profile` | 部分更新（未知键/越界 → 400 且**点名 field**） | `:356` |
| DELETE | `/config-profile` | 恢复默认 | `:385` |
| POST | `/settings/models` | **服务端代拉**上游模型列表（key 不下发浏览器） | `:394` |
| POST | `/personas/import` | 批量导入人设（多格式） | `:581` |
| GET | `/personas/export` | 导出全部（爱语备份格式） | `:743` |
| GET | `/personas/<persona_id>/export` | 导出单个 | `:764` |
| GET | `/personas/<persona_id>/avatar` | 取头像（需鉴权） | `:798` |
| PUT | `/personas/<persona_id>/sections` | 整体替换提示词片段列表 | `:825` |
| GET | `/sessions` | 会话列表 | `:857` |
| POST | `/sessions` | 建会话 | `:871` |
| GET | `/sessions/<session_id>` | 单会话元数据 | `:899` |
| GET | `/sessions/<session_id>/messages` | 会话消息（`?limit=N`） | `:911` |
| PATCH | `/sessions/<session_id>` | 改会话（标题/人设/策略） | `:940` |
| DELETE | `/sessions/<session_id>` | 删会话 | `:966` |
| POST | `/sessions/<session_id>/clear` | 只清消息，保留会话 | `:978` |
| POST | `/sessions/<session_id>/summarize` | 手动触发一次总结（调试） | `:992` |
| GET | `/context-modes` | 可选上下文模式（给面板下拉框） | `:1032` |

（蓝图注册于 `app.py:608`）

### 4.3 `emotion_app/web/api_providers.py`（1216 行，Blueprint，路径全在 `/api/v1` 下）

| 方法 | 路径 | 用途 | 行 |
|---|---|---|---|
| GET | `/provider-presets` | 厂商预设目录 | `:416` |
| GET | `/provider-sources` | 源列表 | `:452` |
| POST | `/provider-sources` | 新增源 | `:464` |
| GET / PUT / DELETE | `/provider-sources/by-id` | 单条源（**query 参数 `source_id`**） | `:485` |
| GET | `/provider-sources/models` | 全部源的模型（按源分组） | `:546` |
| GET | `/provider-sources/<source_id>/providers` | 某源下已配置的模型 | `:559` |
| POST | `/provider-sources/<source_id>/providers` | 给某源加模型 | `:580` |
| GET / POST | `/provider-sources/<source_id>/models` | 打上游 `GET /models` 取模型列表 | `:636` |
| GET / POST | `/providers` | 模型列表（脱敏）/ 新增模型 | `:796` |
| GET / PUT / DELETE | `/providers/by-id` | 单条模型（query 参数 **`provider_id`**） | `:829` |
| GET/PUT/DELETE/PATCH/POST | `/providers/<path:provider_id>` | 单条模型；POST 按后缀分派 `/default`、`/test` | `:928` |
| — | — | `_method_hint`：方法用错 → 405 并说清该用什么 | `:973` |

### 4.4 `emotion_app/web/plugin_pages_web.py`（247 行）

| 方法 | 路径 | 用途 | 行 |
|---|---|---|---|
| GET | `/api/v1/plugins/pages` | 插件拓展页列表（面板渲染入口） | `:70` |
| GET | `/api/v1/plugins/<plugin_id>/pages/<page_name>` | **308 重定向**到带斜杠版本 | `:159` |
| GET | `/api/v1/plugins/<plugin_id>/pages/<page_name>/` | 页面入口 HTML（注入桥接垫片） | `:166` |
| GET | `/api/v1/plugins/<plugin_id>/pages/<page_name>/<path:asset_path>` | 页面静态资源 | `:173` |
| GET/POST/PUT/PATCH/DELETE | `/<plugin_dir>/<path:rest>` | 根路径兜底（插件页 JS 硬编码 `/astrbot_plugin_xxx/page`） | `:182` |

- 保留前缀 `RESERVED_PREFIXES = ("api","static","health","v1","favicon.ico")`（`:37-43`）。
- 页面与资源一律 `Cache-Control: no-cache, must-revalidate`（`:150`）。

### 4.5 `emotion_app/web/plugin_routes.py`（143 行）

**无路由**，是协议转换层：`to_quart_response(result)`（`:58`）把插件返回值（Quart `Response` / `PluginResponse` / tuple / dict / list / bytes / str / None）统一成 Quart 响应；`not_found_payload(plugin_path, hint)`（`:135`）生成未命中路由的标准信封。`PluginResponse` 延迟导入（`:42-52`）。

### 4.6 `emotion_app/web/auth.py`（199 行）

`Session`（`:30`）、`AuthManager`（`:40`）：`is_access_token`（`:86`）、`is_rate_limited`（`:107`）、`login`（`:115`）、`verify_session`（`:158`）、`logout`（`:170`）、`stats`（`:184`）、`parse_bearer`（`:192`）。令牌比较用 HMAC digest（`_digest` `:78`、`_token_matches` `:81`）；`MIN_TOKEN_LENGTH = 16`（`infra/config.py:24`）。

---

## 5. 插件宿主面（`host/astrbot_host/`）

### 5.1 `umo.py`（515 行）— umo 唯一权威定义

**`unified_msg_origin` 的构造（核心事实）**

- 格式：`<platform_id>:<message_type>:<session_id>`（`umo.py:14`）。
- `PLATFORM_ID: str = "11"`（`umo.py:113`）—— umo **第一段**，即平台**实例** id（对应真实现 `PlatformMetadata.id`）。
- `PLATFORM_NAME: str = "webchat"`（`umo.py:124`）—— 平台**类型**名（`PlatformMetadata.name`），**与第一段不是一回事**。
- `PLATFORM_DESCRIPTION = "情绪app WebChat"`（`umo.py:131`）。
- `build_umo(user_id: str, *, group: bool = False) -> str`（`:174`）：**委托真实现** `str(MessageSession(platform_name=PLATFORM_ID, message_type=..., session_id=user_id))`（`:208-214`），不手写 f-string；`user_id` 为空 / 非 str → **抛 `ValueError`**（`:199-205`）。
- 解析：`parse_umo(umo) -> Umo | None`（`:217`，失败返回 None）；`parse_umo_strict`（`:263`，失败抛 `ValueError`）；都走真实现 `MessageSession.from_str`（`split(":", 2)` 语义 → `session_id` 允许含冒号）。
- 派生：`session_id_of`（`:286`）、`user_id_of`（`:298`）、`is_our_umo`（`:303`）、`is_private`（`:313`）。
- `Umo(NamedTuple)`：`platform_id` / `message_type` / `session_id`（`:150-166`）。
- `DEFAULT_SELF_ID = PLATFORM_NAME`（`:147`）—— 绝不等于用户 id（`build_inbound_event` 里强制校验 `self_id == user_id` → 抛错，`:427-432`）。
- `build_platform_metadata(*, platform_id=PLATFORM_ID) -> PlatformMetadata`（`:329`），`name=webchat`、`id=11`（`:345-349`）。
- `build_inbound_event(text, user_id, *, group=False, sender_name="", self_id=DEFAULT_SELF_ID, message_id="", extra=None) -> AstrMessageEvent`（`:357`）—— 入站事件工厂，从**同一个** `MessageType` 同时产出 umo 第二段与 `message_obj.type`（`:444-452`），并补 `event.platform_meta` / `event.platform`（`:484-485`）。

**平台实例如何被识别**：按 `PlatformMetadata.id`（`= "11"`）查表；插件侧先试 `manager.get_insts()`、兜底 `manager.platform_insts`，再拿 `str(getattr(p.meta(),"id","")) == str(platform_id)`（`platform_manager.py:8-20` 引用真实现 `proactive_chat_runtime_bridge.py:534-575`）。

### 5.2 `platform_manager.py`（243 行）

- `DEFAULT_PLATFORM_ID = _UMO_PLATFORM_ID`（`:57`）、`DEFAULT_PLATFORM_NAME = _UMO_PLATFORM_NAME`（`:60`）—— 从 `umo.py` 取，不二次定义。
- `class OurPlatform(Platform)`（`:63`）：`__init__(*, platform_id=…, platform_name=…, delivery=None, description="情绪app 内置 Web 端（非 OneBot）")`（`:75-99`）；`meta()`（`:103`）；`describe()`（`:148`）。元数据带 `support_proactive_message=True`、`support_streaming_message=True`（`:94-95`），`set_status(PlatformStatus.RUNNING)`（`:99`）。
- **`send_by_session(session, message_chain)`（`:106-144`）返回语义**：
  - `self.delivery is None`（**未接端口**）→ 打 WARNING，**返回 `False`**（`:126-132`）—— 如实失败，不伪造送达。
  - 端口抛异常 → 记 ERROR，**返回 `False`**（不让异常穿出去，`:137-143`）。
  - 成功 → **返回 `True`**（`:144`）。
- `_session_to_umo(session)`（`:157`）：str 直接用，否则 `str(session)`。
- `class OurPlatformManager`（`:167`）：`platform_insts: list`（`:183`）、`get_insts()`（`:185`，返回**内部列表本身**）、`get_inst(platform_id)`（`:189`，**同时匹配 id 和 name**）、`find_by_umo(umo)`（`:207`）、`describe()`（`:212`）。
- `build_platform_manager(*, platform_id=…, platform_name=…, delivery=None) -> OurPlatformManager`（`:220`）。
- **装配时的实际取值**：`emotion_app/web/plugin_host.py:296` → `build_platform_manager(delivery=None)` —— 即**生产上投递端口未接**，`send_by_session` 恒返回 `False`。

### 5.3 `dispatch.py`（559 行）— 两阶段派发

| 公开成员 | 行 | 说明 |
|---|---|---|
| `DispatchResult`（dataclass） | `:48-92` | `activated` / `parsed_params` / `executed` / `errors` / `sent` / `result` / `emitted` / `stopped` / `allow_default_llm` / `is_wake`；`ok` property（`:82`）、`summary()`（`:87`） |
| `apply_wake(event, wake_prefixes=None, *, friend_message_needs_wake_prefix=False) -> bool` | `:100` | 阶段 A 前：前缀匹配（**切掉前缀**）→ 私聊判定（`not friend_message_needs_wake_prefix or platform_name=="webchat"`）；未唤醒则 `event.stop_event()` |
| `select_handlers(event, cfg=None, *, plugins_name=None) -> (list[StarHandlerMetadata], dict[str, dict])` | `:170` | 阶段 A：跑过滤器 AND |
| `async call_handler(event, handler, *args, **kwargs)` | `:290` | 阶段 B 单次调用；协程 / 异步生成器两种；`MessageEventResult`/`CommandResult` → `event.set_result` |
| `async dispatch(event, cfg=None, *, wake_prefixes=None, plugins_name=None, wake_already_applied=False, friend_message_needs_wake_prefix=False) -> DispatchResult` | `:356` | 总入口：`apply_wake` → `select_handlers` → 设 `activated_handlers`/`handlers_parsed_params` extra → 逐个执行 + 每轮 `clear_result()` |
| `_call_event_hook(event, hook_type, *args, **kwargs)` | `:508` | 事件钩子调用（非协程 → 记 ERROR 跳过） |
| `_allow_default_llm(event)` | `:535` | `not _has_send_oper and is_at_or_wake_command and not call_llm` |

### 5.4 `registry.py`（304 行）

- `EventType(enum.Enum)`（`:27`）：**16 个**成员（`OnAstrBotLoadedEvent` … `OnPluginUnloadedEvent`，`:34-49`）。
- `StarHandlerMetadata(Generic[H])`（`:53`）：`event_type` / `handler_full_name` / `handler_name` / `handler_module_path` / …；`__lt__`（`:67`）、`is_enabled` property（`:74`）。
- `StarHandlerRegistry(Generic[H])`（`:78`）：`append`（`:92`）、`get_handler_by_full_name`（`:102`）、`get_handlers_by_event_type`（`:107`）、`get_handlers_by_module_name`（`:132`）、`__iter__`（`:158`）、`__len__`（`:162`）、`all_handlers` property（`:167`）、`count_by_event`（`:171`）、`remove_plugin`（`:178`）、`clear`（`:192`）。
- `star_handlers_registry`（模块级单例，被 `dispatch.py:42` import）。
- `FunctionToolSpec`（`:207`）+ `to_openai_schema()`（`:215`）；`FunctionToolManager`（`:241`）：`add_func`/`get_func`/`get_all`/`names`/`remove`/`clear`。
- `PY_TO_JSON_TYPE`（`:287`）、`SUPPORTED_TYPES`（`:302`）。

### 5.5 `provider.py`（347 行）

- `ADAPTER_TYPE_OPENAI_CHAT = "openai_chat_completion"`（`:45`）、`PROVIDER_TYPE_CHAT = "chat_completion"`（`:50`）。
- `class Provider`（`:53`）：`__init__(*, provider_id, model="", base_url="", api_key="", name="", timeout=60.0, llm=None)`（`:67-96`）；`provider_config` 是 dict，含 `id`/`type`/`provider_type`/`model`/`api_base`/`key`（**列表**）/`name`/`timeout`（`:80-92`）；`get_model()`（`:100`）、`meta()` → `SimpleNamespace(id, model, type)`（`:104`）、`set_model`（`:121`）。
- `async text_chat(prompt=None, session_id=None, image_urls=None, audio_urls=None, func_tool=None, contexts=None, system_prompt=None, tool_calls_result=None, model=None, extra_user_content_parts=None, tool_choice="auto", request_max_retries=None, **kwargs) -> LLMResponse`（`:127-205`）：转调 `llm.generate(...)`；`llm is None` → **抛 `RuntimeError`**（`:158-163`）；返回 `LLMResponse(completion_text=...)`（`:197`）。
- `async text_chat_stream(*args, **kwargs)`（`:207-219`）：**假流式** —— 内部 `_single()` 生成器只 yield 一次 `await text_chat(...)`。
- `_IGNORED_TEXT_CHAT_KWARGS`（`:231-241`）：7 个不转发上游的参数。
- `_extract_text(out)`（`:244`）：取 `content`/`text`/`completion_text`，取不到打 WARNING 返回 `""`。
- `class ProviderManager`（`:266`）：`inst_map: dict[str, Provider]`（`:280`）、`default_provider_id: str`（`:282`）、`register(provider, *, make_default=False)`（`:286`）、`unregister`（`:291`）、`get_provider_by_id`（`:298`）、`get_all`（`:308`）、`get_using(umo=None)`（`:311`）、`set_default`（`:322`）、`describe`（`:328`）。

**`get_using_provider()` 如何被满足**（完整链路）

```
插件   context.get_using_provider(umo)
  └─ host/astrbot_host/compat/core/star/context.py:403   → self.provider_manager.get_using(umo)
       └─ host/astrbot_host/provider.py:311              → inst_map[self.default_provider_id]（无默认 → None）:317-319
装配：emotion_app/web/plugin_host.py:317 _build_providers()
  ├─ provider_bridge.flatten_two_layer(store)  → (扁平列表, default_id)   :355
  └─ provider_bridge.build_provider_manager(backend, providers, default_id) :245
       └─ 每个模型 = 一个 Provider，llm = UpstreamLLMPort(backend, model, base_url, api_key, timeout) :272-289
```
- `context.get_all_providers()`（`context.py:385`）/ `get_provider_by_id`（`:424`）都从 `provider_manager` 取。
- `HostPorts.capabilities()` 单独暴露 `provider_manager`（有对象）与 `provider_usable`（真的能调模型），`_provider_usable()` 判据 = 「至少一个 provider 的 `_llm` 非 None」（`context.py:139-150`）。

### 5.6 `llm_hooks.py`（177 行）

- `async call_event_hook(event, hook_type, *args, **kwargs) -> bool`（`:44`）—— `True` = 事件被终止。
- `_is_stopped(event)`（`:98`）、`async run_llm_request_hooks(event, req) -> tuple[Any, bool]`（`:109`，**传同一个 req 对象**）、`async run_waiting_llm_request_hooks(event) -> bool`（`:132`）、`async run_llm_response_hooks(event, response) -> bool`（`:142`）、`hook_counts() -> dict[str, int]`（`:157`）。
- 覆盖的钩子：`OnLLMRequestEvent`（24 处）/ `OnLLMResponseEvent`（12）/ `OnWaitingLLMRequestEvent`（2）（`llm_hooks.py:8-11` 的实测统计表）。

### 5.7 `loader/plugin_loader.py`（718 行）

- `METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")`（`:48`）。
- `PluginRecord`（`:52`，`ok` property `:79`）、`LoadReport`（`:85`，`loaded`/`failed` property、`summary()`）。
- `class PluginLoader`（`:114`）：`__init__(plugin_dirs, *, mirror_root=None)`（`:124-161`，**传 `mirror_root` 会先镜像再加载**，`:146-152`）；`discover() -> list[Path]`（`:163`）；`async load_all(*, ports=None, only=None, fail_fast=False) -> LoadReport`（`:182`）；`async load_one(path) -> PluginRecord`（`:211`）；`async initialize_all(report=None) -> list[str]`（`:310`）；`async terminate_all()`（`:328`）；内部 `_ensure_on_sys_path`（`:341`）、`_check_name_conflict`（`:353`）、`_read_metadata`（`:370`）、`_import_plugin`（`:393`）、`_find_star_class`（`:454`）、`_bind_handlers_to_instance`（`:480`）、`_count_plugin_handlers`（`:521`）、`_inject_class_attrs`（`:538`）、`_build_plugin_config`（`:558`）、`_fill_star_metadata`（`:647`）。
- 模块函数：`_normalize_pages(raw)`（`:664`）、`_lite_yaml(text)`（`:699`）。

### 5.8 `host_app.py`（357 行）— 装配

- `EXTENSION_PREFIX = "/api/plugins/extensions"`（`:39`）。
- `MountedApi`（`:43`：`route`/`methods`/`desc`/`owner`/`handler`）、`HostReport`（`:54`：`loaded`/`loaded_plugins`/`failed`/`mounted`/`initialized`/`init_failed`/`source_writes`/`pages`；`mounted_count` property `:75`；`find_page` `:79`；`summary` `:86`）。
- `class PluginHost`（`:106`）：`__init__(*, plugin_dirs, auto_initialize=True, initialize_timeout=..., mirror_root=...)`（`:116`）；`async start(*, ports=None, install_shadow=True) -> HostReport`（`:141`，幂等 `:145-147`；装影子包 → 源码快照 → 加载 → 收集 API → 发现拓展页 → 可选 initialize → diff 源码目录）；`async stop()`（`:215`）；`async _initialize_all(records) -> (list[str], list[tuple[str,str]])`（`:223`）；`_collect_apis(load_report) -> list[MountedApi]`（`:273`）；`async call_api(path, *, request_obj=None, method="GET", username="") -> Any`（`:299`）。

### 5.9 `web_router.py`（87 行）

照抄真实现的路由匹配器：`normalize_plugin_api_route(route)`（`:28`）、`plugin_api_route_pattern(route) -> str`（`:36`，`<name>`→`[^/]+`、`<path:name>`→`.*`）、`match_registered_web_api(registered_web_apis, subpath, method) -> (handler, path_params) | None`（`:56`，**先到先得**顺序敏感）。

### 5.10 `shadow.py`（137 行）

把 `astrbot_host.compat` 注册成 `sys.modules` 里的 `astrbot`：`SHADOW_NAME = "astrbot"`（`:24`）、`SUBMODULES`（`:29`，显式列举的完整子模块清单）、`is_installed()`（`:84`）、`install_shadow_package(*, force=False) -> bool`（`:88`）、`uninstall_shadow_package()`（`:126`）、`_is_our_shadow(mod)`（`:135`）。

### 5.11 `source_mirror.py`（180 行）

单向镜像插件源码到我们可写目录：`_SKIP_DIR_NAMES` / `_SKIP_SUFFIXES`（`:49-50`）、`MirrorReport`（`:54`，`total_touched` property `:65`、`summary()` `:68`）、`_iter_source_files(root)`（`:76`）、`_needs_copy(src, dst)`（`:87`，比 `(size, mtime_ns)`）、`mirror_one(src_dir, dst_dir, report)`（`:96`，增量 + 传播删除）、`mirror_plugin_sources(sources, dest_root) -> MirrorReport`（`:135`，同名插件**先出现的源为准** `:171-177`）。

### 5.12 `write_guard.py`（122 行）

只发现不阻止：`_IGNORED_DIR_NAMES` / `_IGNORED_SUFFIXES`（`:46-47`）、`SourceWrite`（`:51`）、`SourceSnapshot`（`:60`，只记 `(mtime_ns, size)`）、`SourceSnapshot.take(roots)`（`:65`）、`diff(other) -> list[SourceWrite]`（`:85`）、`warn_on_source_writes(writes)`（`:107`）。

### 5.13 `plugin_pages.py`（227 行，另属 `host/astrbot_host/`）

`PluginPage`（`:69`）、`discover_pages(...)`（`:89`）、`resolve_page_asset(page, asset_path) -> Path | None`（`:136`，目录穿越防护）、`guess_mimetype(path)`（`:173`）、`inject_page_context(html, *, is_dark, bridge_src)`（`:186`）、`_find_head_end(html)`（`:218`）。

### 5.14 `emotion_app/web/plugin_host.py`（488 行）— 宿主服务（装配层）

- `ensure_host_on_path()`（`:26`）、`plugin_reply_text(result) -> str | None`（`:56`）、`_messages_to_provider_request`（`:86`）、`_last_user_text_from`（`:118`）、`_provider_request_to_messages`（`:137`）、`_load_provider_config()`（`:175`）。
- `class PluginHostService`（`:217`）：`__init__(config)`（`:224`）、`async start()`（`:235`，建数据目录 → 设 data_root → `PluginHost(mirror_root=…)` → 平台管理器 → 供应商管理器 → `host.start(ports=HostPorts(...))`）、`async stop()`（`:310`）、`_build_providers()`（`:317`）、`async apply_llm_hooks(messages)`（`:370`）、`async dispatch_message(text, *, user_id="", group=False)`（`:408`）、`status()`（`:460`）。
- `dispatch_message` 里用户 id：`user_id or str(self.config.plugins.umo_user_id or "default")`（`:441`）。

---

## 6. 供应商 / 模型层

### 6.1 两层模型（端点 → 模型）

**`provider_sources`（端点层，`emotion_app/infra/providers_store.py:391` `ProviderSource`）字段**

| 字段 | 默认 | 行 |
|---|---|---|
| `id: str` | `""` | `:397` |
| `provider: str` | `DEFAULT_VENDOR = "openai"`（`:130`） | `:400` |
| `type: str`（适配器协议） | `DEFAULT_ADAPTER_TYPE = "openai_chat_completion"`（`:131`） | `:402` |
| `provider_type: str`（分类） | `PROVIDER_TYPE_CHAT = "chat_completion"`（`:105`） | `:404` |
| `key: list[str]` | `[]`（**明文列表**，落盘可明文、绝不进响应，`:405-406`） | `:405` |
| `api_base: str` | `""` | `:407` |
| `timeout: float` | `DEFAULT_TIMEOUT = 60.0`（`:132`） | `:408` |
| `proxy: str` | `""` | `:409` |
| `custom_headers: dict[str,str]` | `{}` | `:410` |
| `enable: bool` | `True` | `:411` |

- `validate()`（`:416`）：id 必填 + `SOURCE_ID_PATTERN = ^[^\s/]{1,64}$`（`:91`，**不含斜杠/空白**）；`provider_type ∈ SUPPORTED_PROVIDER_TYPES`（`:108-114` = chat / tts / embedding）否则抛 `UnsupportedProviderType`；`api_base` 必须 http(s)；`timeout` 必须数字且 > 0。
- `to_storage_dict()`（`:468`，含明文 key）/ `public_view()`（`:488`，key → `[KEY_MASK]` + `key_configured`，`KEY_MASK = "•"*8` `:199`）。
- `SUPPORTED_PROVIDER_TYPES`（`:108-114`）、`KNOWN_UNSUPPORTED_PROVIDER_TYPES`（`:122`）、`EXECUTABLE_ADAPTER_TYPES = (openai_chat_completion, openai_responses)`（`:156-164`）、`adapter_is_executable()`（`:167`）。

**`providers`（模型层，`providers_store.py:569` `ProviderModel`）字段**

| 字段 | 说明 | 行 |
|---|---|---|
| `id: str` | **`<源id>/<模型名>`**，`MODEL_ID_PATTERN = ^[^\s/]+/\S+$`（`:101`） | `:578` |
| `provider_source_id: str` | 指向 `provider_sources[].id` | `:580` |
| `model: str` | 模型名 | `:581` |
| `enable: bool` | 默认 `True` | `:582` |
| `modalities: list[str]` | 默认 `[]` | `:583` |

- `validate()`（`:586`）额外要求 **id 前缀必须等于 `provider_source_id`**（`:610-616`）；`validate_against_sources(source_ids)`（`:617`）做**悬空引用检查**。
- `ProviderModel.public_view()` 与 `to_storage_dict()` 同形（模型层不含凭据，`:643-646`）。

**id 格式与默认选择**

- 默认落盘路径 `DEFAULT_PATH = "data/providers.json"`（`:83`），`SCHEMA_VERSION = 2`（`:87`）。
- `default_provider_id`（store 级，`ProviderStore.default_provider_id()` `:1028`）。
- 迁移：`migrate_v1_to_v2(data) -> (dict, list[str])`（`:683`）、`needs_migration(data)`（`:789`）。
- `ProviderStore`（`:809`）方法：`load`（`:985`）/`load_strict`（`:1002`）/`list_sources`（`:1016`）/`list_models(source_id=None)`（`:1019`）/`default_provider_id`（`:1028`）/`get_source`（`:1031`）/`require_source`（`:1037`）/`get_model`（`:1046`）/`require_model`（`:1052`）/`snapshot`（`:1062`）/`snapshot_sources`（`:1081`）/`snapshot_models_by_source`（`:1085`）/`create_source`（`:1094`）/`update_source`（`:1108`）/`delete_source`（`:1188`）/`create_model`（`:1226`）/`update_model`（`:1244`）/`delete_model`（`:1322`）/`set_default`（`:1337`）；内部 `_read_raw`（`:830`）/`_read`（`:904`）/`_write_all`（`:1351`）/`_write_payload`（`:1367`）/`_preserve_corrupt`（`:1400`）。
- 异常类：`ProviderError`（`:223`）、`ProviderValidationError`（`:239`）、`UnsupportedProviderType`（`:249`）、`ProviderNotFound`（`:260`）、`ProviderStoreCorrupt`（`:271`）。
- 可写字段白名单：`SOURCE_WRITABLE_FIELDS`（`:202`）、`MODEL_WRITABLE_FIELDS = ("provider_source_id","model","enable","modalities")`（`:215`）。

### 6.2 `emotion_app/infra/provider_presets.py`（555 行）

- `ProviderPreset`（`:122`，`public_view()` `:139`）；`PRESETS: tuple[ProviderPreset, ...]`（`:152`）—— 实际条目数 **49**（`ProviderPreset(` 出现 49 次，见 `:152-410` 区间）；`_PRESET_IDS`（`:410`）、`_DUPLICATE_IDS`（`:414`）、`_UNKNOWN_TYPES`（`:424`）。
- `PROVIDER_TYPE_ORDER`（`:108`）；`NOTE_OPENAI_CHAT`（`:95`）、`NOTE_OPENAI_RESPONSES`（`:98`）、`NOTE_ADAPTER_REQUIRED`（`:104`）。
- `list_presets(provider_type=None, *, storable_only=True) -> list[ProviderPreset]`（`:439`）；`find_preset(preset_id)`（`:505`）；`preset_public_view(p)`（`:526`），其中 `executable_by_us` 只由 `adapter_is_executable(type)` 决定。
- **能力诚实**：`openai_responses` 也在 `EXECUTABLE_ADAPTER_TYPES` 内（`providers_store.py:158-164`）。

### 6.3 `emotion_app/web/provider_bridge.py`（301 行）

- `class UpstreamLLMPort`（`:39`）：`__init__(*, backend, model="", base_url="", api_key="", timeout=60.0)`（`:51`）；`async generate(*, prompt=None, messages=None, system_prompt=None, model=None, tools=None, **kwargs)`（`:66`）—— 有 `complete()` 用它、否则**回落 `chat()`**（`:111-147`，回落时把 base_url/api_key/timeout 一并传），两者都没有 → 抛 `RuntimeError`（`:150-153`）；`describe()`（`:155`，不含明文 key）。
- `_first_key(raw)`（`:165`，兼容 key 列表与旧裸字符串）。
- `flatten_two_layer(store) -> (list[dict], str)`（`:182`）：每个模型继承所属源的 `base_url`/`api_key`/`timeout`；源缺失/禁用 → 跳过并 WARNING（`:221-230`）。
- `build_provider_manager(*, backend, providers=None, default_id="") -> ProviderManager`（`:245`）：空列表 → **空管理器**（`get_using()` → None）。

### 6.4 `host/astrbot_host/provider.py`

见 §5.5。要点：`provider_id` = `源id/模型名`（`provider_bridge.py:187-190` 与 `providers_store.py:578` 一致）。

---

## 7. 人设（Persona）

### 7.1 对象形状

**`Persona`（`emotion_app/domain/persona.py:73` `@dataclass(slots=True)`）**

| 字段 | 默认 | 行 |
|---|---|---|
| `name` | `""` | `:82` |
| `description` | `""` | `:83` |
| `personality` | `""` | `:84` |
| `scenario` | `""` | `:85` |
| `creator_notes` | `""` | `:86` |
| `avatar_path` | `""` | `:87` |
| `id` | `uuid4().hex` | `:90` |
| `created_at` / `updated_at` | `time.time()` | `:91-92` |
| `prompt_sections: list[PromptSection]` | `[]` | `:95` |
| `api_url` / `api_key` / `model` | `""`（per-persona 上游覆盖） | `:98-100` |

- 前 6 个字段是「爱语 `PersonaInfo` 实测 6 字段」（`:81`、`:4`）。
- `validate()`（`:102`）：name 非空、≤200；逐片段校验。
- `to_dict(*, include_secrets: bool = False)`（`:112`）—— 默认**不含** `api_key`，只给 `api_key_configured` 布尔（`:132-135`）。
- `from_dict(d)`（`:138`）；`enabled_sections()`（`:160`，按 `(order, title)` 稳定排序）。

**`PromptSection`（`persona.py:28`）**：`title` / `content` / `enabled=True` / `order=100` / `source="user"` / `condition=""`；`SECTION_SOURCES = ("builtin","persona","user","memory","module")`（`:19-25`）。

### 7.2 存储方式

- **整文档 section 存 JSON**：`SECTION = "personas"`，形状 `{id: persona_dict}`（`emotion_app/domain/persona_repo.py:18`、`_persist` `:55-59` 调 `store.save_sections({SECTION: {...}})`，**每次写整段**）。
- `PersonaRepository`（`:29`）：内存缓存 `_cache`（`:34`），`_ensure()` 首次从 `load_store()` 全量读（`:38-50`）、`_invalidate()`（`:52`）；公开方法 `list()`（`:63`，按 updated_at 倒序）、`get(id)`（`:68`）、`exists`（`:74`）、`count`（`:77`）、`create(persona)`（`:82`，id 冲突重新分配 `:90-94`）、`update(id, patch)`（`:104`，白名单 `allowed` `:116-120`，`id`/`created_at` 不可改）、`delete`（`:149`）、`duplicate(id, *, new_name="")`（`:157`）。
- `PersonaNotFound(KeyError)`（`:25`）；`MAX_PERSONAS = 500`（`:22`）。

### 7.3 导入 / 导出（`emotion_app/domain/persona_import.py`，628 行）

- `CardImportError(ValueError)`（`:60`）、`ParsedCard`（`:65`，`personas` / `avatar_bytes` / `source_format` / `notes` / `extra_fields`）。
- 四种来源：**爱语备份**（主路径，`{"data":{"prompts":{...}}}`）、本程序导出（`spec=chara_card_v2` + `extensions.emotion_app`）、SillyTavern V2/V1、PNG 内嵌卡（`tEXt` `chara`/`ccv3`）。
- 函数：`parse_card(data, *, filename="") -> ParsedCard`（`:439`）、`parse_pasted_text(text) -> ParsedCard`（`:484`）、`to_aiyu_backup(personas)`（`:511`）、`to_export_dict(persona)`（`:543`）、`merge_import_sections(...)`（`:577`）、`resolve_name_conflict(name, existing, mode)`（`:604`）、`export_filename(persona, *, ext="json")`（`:625`）。
- 三种冲突策略：`CONFLICT_OVERWRITE="overwrite"` / `CONFLICT_RENAME="rename"` / `CONFLICT_SKIP="skip"`（`:597-601`）。
- 上限：`MAX_CARD_BYTES = 32MB`（`:46`）、`MAX_PERSONAS_PER_FILE = 500`（`:49`）；`AIYU_FIELDS`（`:55`）。
- 纯标准库实现（`struct`/`zlib`/`base64`/`json`，`:29-31`），**不引入 Pillow**。

### 7.4 人设 HTTP 路由

见 §4.2 的 13 条 `/personas*` 路由（`api_admin.py:108` … `:825`）。

### 7.5 人设绑定 / 修订（binding revision）

- **`persona_binding` 这个词在 OUR CODE 里 ABSENT（零匹配）**，`persona_revision` / `binding_revision` / `generation` 同样 ABSENT。
- 实际存在的**绑定关系只有两处弱引用**：
  1. 会话上的 `Session.persona_id: str = ""`（`emotion_app/domain/session_repo.py:41`），会话没绑人设时跟随当前人设（`emotion_app/domain/chat.py:342-344`）；
  2. 运行时设置里的 `active_persona_id`（`emotion_app/domain/settings.py:45`，白名单 `:30-35`）。
- **没有版本/修订号**：改人设后已存在的会话不会感知"人设变了"，也没有 `persona_revision` 快照。唯一带版本语义的是存储层的全局 `revision`（`storage/base.py:68`）。

---

## 8. 配置 Profile（`emotion_app/domain/config_profile.py`，494 行）

- 落盘 `DEFAULT_PATH = "data/config_profile.json"`（`:42`），`SCHEMA_VERSION = 1`（`:45`）。
- 两个 section：`SECTION_SESSION = "session"` / `SECTION_CONTEXT = "context"`，`SECTIONS = (session, context)`（`:47-49`）。
- `ConfigProfileError(ValueError)`（`:65`，带 `field_name`）；`FieldSpec`（`@dataclass(frozen=True)`，`:72`）：`key` / `label` / `type` / `default` / `hint` / `options` / `provider_type` / `minimum` / `maximum`；`to_public()`（`:92`）。
- `SPECS: dict[str, tuple[FieldSpec, ...]]`（`:107`）是**唯一权威字段清单**（同时是默认值 / 校验 / 前端渲染来源，`:14-20`）。

**session 段（10 个字段，`config_profile.py:108-160`）**

| key | label | type | default | 约束 |
|---|---|---|---|---|
| `default_chat_provider_id` | 默认对话模型 | `provider_select` | `""` | `provider_type=chat_completion` |
| `fallback_chat_provider_ids` | 回退对话模型 | `provider_multi` | `[]` | chat_completion |
| `tts_enable` | 启用文本转语音 | `bool` | `False` | — |
| `default_tts_provider_id` | 默认文本转语音模型 | `provider_select` | `""` | text_to_speech |
| `tts_trigger_probability` | TTS 触发概率 | `float` | `1.0` | 0.0–1.0 |
| `default_persona_id` | 默认采用的人格 | `persona_select` | `""` | — |
| `request_max_retries` | 请求最大重试次数 | `int` | `3` | 0–10 |
| `display_reasoning_text` | 显示思考内容 | `bool` | `False` | — |
| `datetime_system_prompt` | 现实世界时间感知 | `bool` | `True` | — |
| `streaming_response` | 流式输出 | `bool` | `True` | — |

**context 段（10 个字段，`config_profile.py:161-230`）**

| key | label | type | default | 约束 |
|---|---|---|---|---|
| `mode` | 上下文模式 | `enum` | `recent_only` | `CONTEXT_MODES = (recent_only, rolling_summary, semantic)`（`:51`） |
| `protected_tail` | 保留最近原文条数 | `int` | `6` | 0–200 |
| `max_context_length` | 压缩前最多保留对话轮数 | `int` | `-1` | -1–10000 |
| `dequeue_context_length` | 轮次超限时一次丢弃轮数 | `int` | `1` | 1–1000 |
| `context_limit_reached_strategy` | 历史超限时的处理方式 | `enum` | `llm_compress` | `CONTEXT_STRATEGIES = (truncate, llm_compress)`（`:56`） |
| `llm_compress_instruction` | 上下文压缩提示词 | `text` | `DEFAULT_COMPRESS_INSTRUCTION`（`:59-62`，英文原文） | — |
| `llm_compress_keep_recent_ratio` | 压缩时保留最近上下文比例 | `float` | `0.15` | 0.0–0.9 |
| `llm_compress_provider_id` | 用于压缩的模型 | `provider_select` | `""` | chat_completion |
| `fallback_max_context_tokens` | 上下文窗口兜底值 | `int` | `128000` | 1000–10000000 |
| `summary_trigger` | 累积多少条触发总结 | `int` | `20` | 2–1000 |

- 函数：`_defaults(section)`（`:233`）、`default_profile()`（`:239`）、`schema()`（`:244`）、`_coerce(spec, value)`（`:252`，**不静默修正**，越界/类型不符即 `ConfigProfileError`）、`validate_patch(section, patch)`（`:305`）。
- `ConfigProfileStore`（`:334`）：`load()`（`:349`）、`_read_raw()`（`:355`）、`_preserve_corrupt()`（`:386`）、`_merge_defaults(raw)`（`:395`，**未知键保留**）、`patch(changes)`（`:429`）、`reset()`（`:442`）、`_write_all(data)`（`:451`，原子写）。
- 路由：`GET/PATCH/DELETE /api/v1/config-profile`（`api_admin.py:342`/`:356`/`:385`）；测试可用 `app.config["EMOTION_CONFIG_PROFILE_PATH"]` 覆盖路径（`api_admin.py:339`）。

**⚠️ 字段的消费方事实**（在 OUR CODE 全量检索，不含 `config_profile.py` 自身与其测试）

| 字段 | 有真实消费方？ |
|---|---|
| `streaming_response` / `request_max_retries` | **ABSENT** — 只出现在文档字符串与测试（`api_admin.py:360` 是示例文本；`tests/test_config_profile.py` 多处；`host/astrbot_host/compat/core/astr_main_agent.py:52` 是**另一个同名 dataclass 字段**，不是本配置） |
| `default_chat_provider_id` / `fallback_chat_provider_ids` / `tts_enable` / `default_tts_provider_id` / `tts_trigger_probability` / `default_persona_id` / `display_reasoning_text` / `datetime_system_prompt` / `llm_compress_instruction` / `llm_compress_keep_recent_ratio` / `llm_compress_provider_id` / `max_context_length` / `dequeue_context_length` / `context_limit_reached_strategy` | **ABSENT**（零消费方） |
| `protected_tail` / `summary_trigger` | 有，但在 `ContextPolicy`/`SPECS` 各自定义；`config_profile` 的 `SPECS` 与 `ContextPolicy` 之间**没有代码连线**（`ContextPolicy` 默认值 `protected_tail=20`/`summary_trigger=40` 见 `context.py:51`/`:54`，与 profile 默认 `6`/`20` **不一致**） |

**结论**：`config_profile` 目前是**纯配置面**（存 + 校验 + 下发 schema + 前端渲染），**没有消费方把它读回领域行为**。唯一例外是 `mode`（语义上与 `ContextPolicy.mode` 同名同域，但无代码连线）。

---

## 9. 身份概念 token 检索（OUR CODE = `emotion_app/` + `host/astrbot_host/`）

检索方式：`re.search(r'\b' + token + r'\b')` 逐行扫描全部文本文件（排除 `__pycache__`）。

| token | 结论 | 位置 |
|---|---|---|
| `ecosystem_id` | **ABSENT (0)** | — |
| `installation_id` | **ABSENT (0)** | — |
| `runtime_instance` | **ABSENT (0)** | — |
| `host_kind` | **ABSENT (0)** | — |
| `bot_id` | **ABSENT (0)** | — |
| `persona_binding` | **ABSENT (0)** | — |
| `conversation_ref` | **ABSENT (0)** | — |
| `sharing_mode` | **ABSENT (0)** | — |
| `sharing_anchor` | **ABSENT (0)** | — |
| `visibility` | **ABSENT (0)** | （唯一命中在 `emotion_app/web/static/app.js:2789` 的 DOM `visibilitychange` 事件，非本概念） |
| `audience` | **ABSENT (0)** | — |
| `operation_id` | **ABSENT (0)** | — |
| `idempotency` | **ABSENT (0)** | （子串 `idempot`/`幂等` 亦 0 命中；`sqlite_store.py:56` 与 `ports.py:53` 的"幂等"是中文描述 `open()` 语义） |
| `outbox` | **ABSENT (0)** | （子串亦 0） |
| `inbox` | **ABSENT (0)** | （子串亦 0） |
| `receipt` | **ABSENT (0)** | （子串亦 0） |
| `capability` | **ABSENT (0)** | （子串亦 0。）⚠️ 唯一近似物是 `HostPorts.capabilities()` 方法名（`host/astrbot_host/compat/core/star/context.py:118`）与 `_provider_usable`/`_platform_delivery_wired`（`:139`/`:153`）—— **方法名复数形式**，token 级零匹配 |
| `manifest` | **ABSENT (0)** | （子串亦 0） |
| `revision` | **PRESENT (22 行)** | `emotion_app/storage/base.py:8,28,68,69,75`；`emotion_app/storage/sqlite_store.py:8,70,154,161-181`（具体：`:150` `next_revision`、`:161` `current_revision`、`:165` `_read_revision`、`:171` `_bump_revision`、`:180` export 字段）—— **仅存储层全局单调计数**，与"实体修订"无关 |
| `generation` | **ABSENT (0)** | （子串 14 命中全是 `/images/generations` 端点名，如 `emotion_app/domain/image_gen.py:2,61`、`backends.py:235,294`） |
| `subject_ref` | **ABSENT (0)** | — |
| `canonical_subject` | **ABSENT (0)** | — |

**扩范围复核**（含 `host/data/` 第三方插件源码 / `docs/` / `tools/` / `tests/` / `host/tests/` / `conductor/` / `README.md`）

- 第三方插件侧（`host/` 全树，含 `data/`）唯一命中：`host/data/plugins/config/astrbot_plugin_memory_companion_config.json:178` `"visibility"`；`host/data/plugins/astrbot_plugin_private_companion/companions.json:174` `"req036-capability-v1"`（键名）。
- `docs/` + `tools/` + `tests/` + `host/tests/` + `conductor/` + `README.md`：上表除 `revision` 外**全部为 0**。

**结论**：审计关心的 23 个身份/治理 token 里，**22 个在自研代码中零匹配**，唯一 PRESENT 的 `revision` 含义是**存储层全局计数器**（无实体级修订、无 CAS）。

---

## 10. 测试与验证资产

### 10.1 主项目 `tests/`（21 文件，584 个 `def test_`）

| 文件 | test 数 | 行数 |
|---|---:|---:|
| `tests/test_assembly.py` | 12 | 159 |
| `tests/test_chat_http.py` | 24 | 397 |
| `tests/test_config_logging.py` | 18 | 209 |
| `tests/test_config_profile.py` | 33 | 498 |
| `tests/test_domain.py` | 31 | 300 |
| `tests/test_persona_import_http.py` | 23 | 422 |
| `tests/test_persona_import.py` | 45 | 530 |
| `tests/test_plugin_chat_integration.py` | 17 | 567 |
| `tests/test_plugin_host_e2e.py` | 29 | 1056 |
| `tests/test_ports_backends.py` | 27 | 381 |
| `tests/test_provider_presets.py` | 19 | 323 |
| `tests/test_providers_api.py` | 74 | 2123 |
| `tests/test_s2_http.py` | 37 | 629 |
| `tests/test_s2_services.py` | 28 | 285 |
| `tests/test_s3_context.py` | 36 | 363 |
| `tests/test_s3_http.py` | 21 | 399 |
| `tests/test_s6b_summarizer.py` | 17 | 445 |
| `tests/test_s6_image.py` | 20 | 467 |
| `tests/test_storage.py` | 20 | 183 |
| `tests/test_upstream.py` | 28 | 361 |
| `tests/test_web_auth.py` | 25 | 298 |

（另有 `tests/conftest.py` 100 行、`tests/fixtures/aiyu_real_export.json`）

### 10.2 宿主 `host/tests/`（9 个 `test_*.py`，136 个 `def test_`）

| 文件 | test 数 | 行数 |
|---|---:|---:|
| `test_compat_regressions.py` | 12 | 517 |
| `test_dispatch.py` | 11 | 359 |
| `test_llm_hooks.py` | 12 | 351 |
| `test_loader_all_plugins.py` | 9 | 227 |
| `test_loader_real_plugin.py` | 9 | 145 |
| `test_platform_manager.py` | 11 | 285 |
| `test_provider.py` | 33 | 645 |
| `test_shadow_and_loader.py` | 17 | 358 |
| `test_umo.py` | 22 | 618 |

判别工具/探针（非 `test_` 前缀，`pytest host/tests` 不收集为测试）：
- `tools_verify_compat.py`（544 行）：差分验证①（枚举/`MessageSession` umo 往返/路径函数/`request_retry` 常量/`AstrBotConfig`），需真 AstrBot 解释器。
- `tools_verify_behavior.py`（384 行）：差分验证②（`CommandGroupFilter`/`PermissionTypeFilter`/`PlatformAdapterTypeFilter` 行为，用假 event 喂同一份输入）。
- `tools_verify_router.py`（79 行）：路由匹配器差分（真 `_plugin_api_route_pattern`/`_match_registered_web_api` vs 我们的）。
- `probe_auto_initialize.py`（121 行）：隔离进程 + 临时目录 + 硬超时探 `auto_initialize=True` 的真实行为。
- `probe_init_per_plugin.py`（100 行）：逐插件 `initialize()` 计时/超时定位。

**跳过条件**：`test_loader_all_plugins.py:37` 与 `test_loader_real_plugin.py:30` 都有 `pytestmark = pytest.mark.skipif(not PLUGIN_ROOT.is_dir(), ...)`，`PLUGIN_ROOT = Path("/opt/astrbot/data/plugins")`（`:35`）—— 该目录不存在时这些测试**跳过**。

### 10.3 `tools/` 真浏览器 / CDP 判据工具

| 工具 | 行 | 判据数（`check(` 调用点计数） | 说明 |
|---|---:|---:|---|
| `tools/browser_check.py` | 188 | 非 `check()` 结构 | Chrome CDP 起真浏览器；退出码 0 = 图片节点真出现且 `naturalWidth > 0`（`:12`）并被 `panel_binding_check.py:26` 复用（导出 `CHROME`/`PORT`/`CDP`/`_http_json`） |
| `tools/login_check.py` | 219 | **6**（docstring `:8-16` 列 6 条；`:208` 打印"6 项判据全部满足"） | 登录闸门：框可见/错令牌/对令牌/刷新免重输/令牌不落 storage/**迟到解锁** |
| `tools/mobile_check.py` | 265 | **5+截图**（`:9-17` 列 6 条，第 6 条是截图存证；`:254` 打印"5 项判据"） | 无横向溢出 / 触控 ≥40px / 单列 / 片段表转卡片 / 字号 ≥16px |
| `tools/panel_binding_check.py` | 198 | **8**（docstring 未编号，`:39` 定义 check，8 个字面标签） | 陪伴面板 DOM 里真的渲染出"已绑定" |
| `tools/provider_panel_check.py` | 1057 | **74**（字面标签计数；`docs` 记 70/70） | 供应商面板两层结构全流程（真浏览器 + 假后端桩） |
| `tools/settings_check.py` | 345 | **21**（字面标签计数；docstring `:15-25` 列 7 类覆盖；`docs` 记 21/21） | 设置页配置文件式页面：渲染/取值/PATCH 只发改动项/`error.field` 点名/空态/二次确认/读取失败如实报错 |
| `tools/inject_providers_check.py` | 571 | **56 个注入点**（`INJECTIONS` 列表条目计数，`:54-…`） | 把实现改坏 → 判据必须挂 → 精确恢复；`INJECTION_MARKERS = ("if False","if False else","# INJECT",…)`（`:444`） |

### 10.4 `docs/` 里记录的通过数（文档自述，按文档日期先后）

| 出处 | 记的内容 |
|---|---|
| `docs/P0_P1_TASKS.md:20` | 单测基线「**主 440 + host 35**」（称 `pytest --collect-only -q`） |
| `docs/P0_P1_TASKS.md:12-19` | 磁盘插件 12 / **加载成功 11** / 累计 handler **203** / 主插件 109 handler + 190 API / `AdapterMessageEvent` **82** / `OnCallingFuncToolEvent` 33 / `OnDecoratingResultEvent` 32 / `OnLLMRequestEvent` 24 |
| `docs/P0_P1_TASKS.md:339` | 「5/5 项通过」（面板绑定判据） |
| `docs/P0_P1_TASKS.md:515` | `tools/provider_panel_check.py` 真浏览器 **49/49**（旧数） |
| `docs/P0_P1_TASKS.md:763-767` | 面板判据 **70/70**；主测试 `541 passed, 1 deselected`；host **139 passed**；供应商 API **81 passed**；差分（真 AstrBot 4.27.5）**92/92 · 50/50 · 56/56** |
| `docs/P0_P1_TASKS.md:768` | 注入 bug 验证：**未挂判据 0 条**（约 60 个注入点） |
| `docs/P0_P1_TASKS.md:769` | 生产完整性：`/opt/astrbot/.../_conf_schema.json` sha256 `06113bf50e2698e3` **未变** |
| `docs/ASTRBOT_CONFIG_PAGE.md:161-164` | 设置页 **21/21**；供应商页 **70/70**；主测试 **614 passed, 1 deselected**；host **139 passed** |
| `docs/ASTRBOT_CONFIG_PAGE.md:165` | 注入 bug 验证：预设 3 / 配置 5 / 设置页 5 / bridge 2，**全部被抓** |
| `README.md:10` | 「S0–S3 + S6 + S6b + 宿主合体已完成（**421 项测试全绿**）」（最旧的一份数） |

**⚠️ 静态计数与文档数字的差异（事实记录，不解释）**

- 磁盘上 `def test_` 静态计数：主 **584**、host **136**；文档最新自述为 主 **614**、host **139**（`docs/ASTRBOT_CONFIG_PAGE.md:163-164`）。静态计数不含 `@pytest.mark.parametrize` 展开，因此**小于**运行数属预期方向。
- `docs/P0_P1_TASKS.md:724` 给出的复现命令：`pytest -q`（主，cwd=`情绪app`）与 `pytest -q`（host，cwd=`host`）—— **分开跑**，因为 `pyproject.toml:24` 的 `testpaths=["tests"]` 不管 `host/`，且 `host/tests/conftest.py` 自己把 `host/` 加进 `sys.path`。
- `1 deselected`：需真 AstrBot 解释器的 `live` 用例（`docs/P0_P1_TASKS.md:764` 说明「**故意不 skip**」）。

---

## 11. 运行期配置项（`emotion_app/infra/config.py`）

`ENV_PREFIX = "EMOTION_"`（`:30`）；`_ENV_MAP`（`:327`）登记的环境变量，与 `.env.example` 对齐：

**plugins 段（`PluginsConfig`，`config.py:212`；环境变量 `:350-359`）**：`PLUGINS_ENABLED`、`PLUGINS_PLUGIN_DIRS`、`PLUGINS_DATA_DIR`、`PLUGINS_AUTO_INITIALIZE`、`PLUGINS_INITIALIZE_TIMEOUT`、`PLUGINS_MIRROR_SOURCES`、`PLUGINS_SOURCE_MIRROR_DIR`、`PLUGINS_MOUNT_WEB_APIS`、`PLUGINS_UMO_USER_ID`。

**其它段**：`ServerConfig`（`:43`）、`AuthConfig`（`:66`）、`StorageConfig`（`:96`）、`UpstreamConfig`（`:114`）、`ImageConfig`（`:137`）、`LoggingConfig`（`:200`）、聚合 `Config`（`:283`）。

**当前 `.env` 实态**：`EMOTION_PLUGINS_ENABLED=true`（`.env:48`），`EMOTION_PLUGINS_PLUGIN_DIRS=/opt/astrbot/data/plugins`（`.env.example:47`），`PLUGINS_MIRROR_SOURCES=true` / `SOURCE_MIRROR_DIR=data/plugin_src`（`.env.example:54-55`），`AUTO_INITIALIZE=true`（`.env.example:57`），`INITIALIZE_TIMEOUT=20`（`.env.example:61`），`MOUNT_WEB_APIS=true`（`.env.example:63`）。

---

## 12. 组装线（`emotion_app/__main__.py`）— 「能力是否真被接上」

| 接线 | 行 |
|---|---|
| `store = build_store(config.storage)` | `:68` |
| `settings = SettingsService(store, config)` | `:81` |
| `personas = PersonaRepository(store)` | `:83` |
| `sessions = SessionRepository(store)` | `:85` |
| `chat = ChatService(config, upstream, settings=settings, personas=personas)` | `:90` |
| `chat.bind_sessions(sessions)` | `:91` |
| `chat.bind_summarizer(Summarizer(upstream, model=config.upstream.model))` | `:96` |
| `tools = ToolRegistry(max_calls_per_turn=1)` | `:99` |
| `dispatch = build_dispatch(config.image.dispatch)`；`chat.bind_tools(tools, dispatch)` | `:124-125` |
| `app = create_app(...)`（传入 personas/settings/sessions/chat_backend） | `:141` |

**未在 `__main__` 接线、只在 `app.py` 内接线的**：插件宿主与 LLM 钩子 —— `_mount_plugin_routes` 里 `service._backend_getter = lambda: app.config.get("EMOTION_CHAT_BACKEND")`（`app.py:687`）与 `_chat_svc.bind_llm_hook(service.apply_llm_hooks)`（`app.py:696`），且**只在 `config.plugins.enabled and config.plugins.mount_web_apis` 为真时发生**（`app.py:621`）。

**`/api/v1/status` 不含插件状态**：`app.py:200-216` 只回 `service` / `config` / `storage` / `auth` 四个键；`PluginHostService.status()`（`plugin_host.py:460`）**没有 HTTP 路由暴露**。
