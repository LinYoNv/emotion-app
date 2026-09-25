# 模型供应商（Provider）— 接口契约（冻结版）

> 2026-09-20 冻结。**并行开发前先定契约**，避免几个线程各写一套形状。
> 冻结后**任何一方不得单方面改**；要改先在 `docs/P0_P1_TASKS.md` 记一笔。

---

## 1. 已完成的宿主层（**别改**，直接用）

`host/astrbot_host/provider.py`（已通过 17 项判据）：

```python
class Provider:
    def __init__(self, *, provider_id, model="", base_url="", api_key="",
                 name="", timeout=60.0, llm=None): ...
    provider_id: str
    provider_config: dict      # {"id","type","model","api_base","key":[...],"name","timeout"}
    name: str
    def get_model() -> str
    def meta() -> SimpleNamespace(id, model, type)
    async def text_chat(prompt=None, session_id=None, image_urls=None, audio_urls=None,
                        func_tool=None, contexts=None, system_prompt=None,
                        tool_calls_result=None, model=None, extra_user_content_parts=None,
                        tool_choice="auto", request_max_retries=None, **kw) -> LLMResponse
    async def text_chat_stream(...) -> AsyncGenerator   # 单块（如实）

class ProviderManager:
    inst_map: dict[str, Provider]      # 🔴 必须是 dict（插件第二取值路径）
    default_provider_id: str
    def register(provider, *, make_default=False)
    def unregister(provider_id) -> bool
    def get_provider_by_id(provider_id) -> Provider | None
    def get_all() -> list[Provider]
    def get_using(umo=None) -> Provider | None
    def set_default(provider_id) -> bool
    def describe() -> list[dict]       # [{id,name,model,type,is_default}]
```

`Context` 已接好（`HostPorts(provider_manager=pm)`）：
`context.provider_manager` / `get_using_provider(umo)` /
`get_provider_by_id(id)` / `get_all_providers()`。

---

## 2. 配置存储（**子代理 A 负责实现**）

### 落盘位置
```
data/providers.json          # 我们的数据目录，不是 AstrBot 的
```
⚠️ **绝不写 `/opt/astrbot`**。

### JSON 形状（冻结）
```json
{
  "version": 1,
  "default_provider_id": "fast",
  "providers": [
    {
      "id": "fast",
      "name": "快速响应",
      "model": "gemini-3.1-flash-lite",
      "base_url": "https://api.syuan.org/v1",
      "api_key": "sk-...",
      "timeout": 60.0
    }
  ]
}
```

* `id`：**非空、唯一**，只允许 `[A-Za-z0-9_.-]{1,64}`（插件把它当配置值存进
  `*_PROVIDER_ID`，含特殊字符会出问题）
* `api_key`：**落盘可明文**（与项目既有 `.env` 同级的本机文件），
  但**任何 API 响应都不许回显**（见 §3 的 `api_key_configured`）
* `base_url`：必须 `http(s)://` 开头
* `timeout`：> 0

---

## 3. 管理 API（**子代理 A 负责实现**）

挂在现有 admin blueprint 下（`/api/v1/...`，与 `settings/models` 同级）。
⚠️ 沿用既有风格：`@bp.get/post/patch/delete`，返回 `jsonify(...)`，鉴权用现有的
`_require_auth`（**照抄 `api_admin.py` 里其它路由的写法**）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/providers` | 列表（**脱敏**） |
| POST | `/api/v1/providers` | 新增 |
| PATCH | `/api/v1/providers/<provider_id>` | 修改（`api_key` 为空=不改） |
| DELETE | `/api/v1/providers/<provider_id>` | 删除 |
| POST | `/api/v1/providers/<provider_id>/default` | 设为默认 |
| POST | `/api/v1/providers/<provider_id>/test` | 连通性测试 |

### 响应形状（冻结）
```json
// GET /api/v1/providers
{
  "default_provider_id": "fast",
  "providers": [
    {"id":"fast","name":"快速响应","model":"...","base_url":"https://api.syuan.org/v1",
     "api_key_configured": true, "timeout": 60.0, "is_default": true}
  ]
}
```
🔴 **`api_key` 永不回显**；只给 `api_key_configured` 布尔
（项目踩过"过度脱敏"和"泄露凭据"两个方向的坑，这里取**只报存在性**）。

```json
// POST /api/v1/providers/<id>/test
{"ok": true, "model": "...", "latency_ms": 123, "sample": "回复前 40 字"}
// 失败（HTTP 仍 200，用 ok=false 表达业务失败 —— 照 settings/models 的先例）
{"ok": false, "reason": "upstream_auth", "message": "上游 401：key 无效"}
```
失败原因**逐条说清**（照 `settings/models` 的先例）：
`upstream_timeout` / `upstream_unreachable` / `upstream_auth` /
`upstream_error` / `upstream_bad_payload` / `upstream_empty`。

### 与宿主联动（**关键**）
新增/修改/删除/设默认后，**必须同步 `ProviderManager`**（`register`/`unregister`/`set_default`），
否则插件解析到的还是旧的。`host=...` 从 `app.config["EMOTION_PLUGIN_HOST"]` 拿。

---

## 4. 面板（**子代理 B 负责实现**）

### 侧边栏（截图里那几个之一）
在 `#/settings` **之前**插入（用户明确说「加在侧边栏」）：
```html
<a class="nav-item" href="#/providers" data-nav="providers">
  <span class="nav-icon">🧩</span><span class="nav-label">模型供应商</span>
</a>
```
⚠️ **照抄相邻 `nav-item` 的完整结构**（图标/标签/class 一个都不能少，
否则收起态图标条会错位）。
⚠️ 路由要同步加进 `app.js` 的 `parseRoute` / `showPanel` / 面板 switch。

### 页面
`#tab-providers`，复用既有 settings 页的 `.field-row` / 表单样式（**别新造一套 CSS**）。
功能：
1. 列表（id / 名称 / 模型 / 是否默认 / 测试按钮 / 编辑 / 删除）
2. 新增 + 编辑（内联表单即可）
3. **测试连通**按钮 → 显示成功/失败原因
4. 设默认

⚠️ **key 输入框只写**：编辑时**留空 = 不改**（与 PATCH 的语义一致）。
⚠️ 别把 key 塞进页面（用户上一轮明确在意凭据）。

---

## 5. 验收判据（**子代理 A/B 各自负责**）

### A（后端）
1. 增删改查 + 设默认全通
2. 🔴 `api_key` **不出现在任何响应里**（注入 bug 验证：故意回显 → 判据必须挂）
3. 校验：非法 id / 空 model / 非 http base_url → **400 且说明原因**
4. `test` 的失败原因逐条（超时/连不上/401/非 JSON/空）—— **空列表不算成功**
5. 落盘在**我们的**目录，且重启后仍在
6. 与 `ProviderManager` 同步（改完插件能解析到新值）

### B（前端）
1. 侧边栏出现「模型供应商」，**收起态图标不缺失**
2. 点击能路由到 `#/providers`，刷新后仍在该页
3. 列表渲染出真实数据
4. 新增 → 列表出现；测试 → 显示结果
5. key 框**不回显**（值为空）

### 共同
* **每条判据都要能失败**（注入 bug 验证）
* 跑测试：`cd host && ../.venv/bin/python -m pytest -q`（基线 **123**）
  与 `cd . && .venv/bin/python -m pytest -q -k "not live"`（基线 **456**）
* 🔴 **绝不在 `/opt/astrbot` 下写文件**

---

## 6. 分工边界（**避免撞车**）

| 子代理 | 独占文件 |
|---|---|
| **A（后端）** | `emotion_app/infra/providers_store.py`（新建）、`emotion_app/web/api_providers.py`（新建）、`emotion_app/web/app.py`（**只加挂载那一行**） |
| **B（前端）** | `emotion_app/web/static/index.html`、`emotion_app/web/static/app.js`、`emotion_app/web/static/app.css` |
| **我自己** | `host/astrbot_host/*`、`emotion_app/web/plugin_host.py`（装配）、文档 |

⚠️ A 要改 `app.py` 时**只加一行挂载**，不要动别的；
⚠️ B 不碰任何 `.py`；A 不碰任何静态文件。
