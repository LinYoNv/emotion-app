# 情绪app

> 自用的「虚拟聊天伙伴」后端。**形态参照爱语，机制参照陪伴插件**。
> 后端部署在自己的电脑/服务器上，**手机只是 WebView 套壳**。

---

## 当前状态

**S0–S3 + S6 + S6b + 宿主合体 已完成**（**421 项测试全绿**）✅ ｜ 🎯 **定位见 `../爱语/docs/PLATFORM_POSITIONING.md`**

| 阶段 | 内容 | 状态 |
|---|---|---|
| **S0** | 目录 + 配置 + 日志 + SQLite 存储抽象 + `/health` + 鉴权 | ✅ **完成** |
| **S1** | 领域模型 + 提示词组装 + 上游客户端 + `/v1/*`（含 SSE 流式） | ✅ **完成** |
| **S2** | 人设 CRUD + 运行时设置 + **面板**（可改配置、预览提示词） | ✅ **完成** |
| **S3** | 会话存储 + **三种上下文模式** + 多轮记忆 | ✅ **完成** |
| **宿主合体** | AstrBot 插件**装 + 跑 + 挂**：266 个 Web API 真挂在 `/api/plugins/extensions/<path>` | ✅ **完成**（09-20） |
| S4 | 长期记忆（跨会话） | ⏳ P3 优先 |
| **主动消息** | 它主动找你（**差距分析里的最高优先**） | ⏳ **P1** |
| **情绪/关系状态** | 会累、会生气 | ⏳ P2 |
| **S6b** | 总结自动化（**后台静默总结**，不阻塞回复） | ✅ **完成** |
| **事件派发** | 插件的消息 handler（`@filter.command` 等）**还没有入口** | ⏳ **宿主下一步** |
| S5 | 接口预留（不实现陪伴对接） | ⏳ |
| S6 | 面板 + App 端改配置 | ⏳ |
| S7 | 手机 WebView 套壳 | ⏳ |

> **插件宿主**（可选，默认关）：`.env` 设 `EMOTION_PLUGINS_ENABLED=true` 即启用，
> 从 `/opt/astrbot/data/plugins` 加载 11 个插件、挂载 266 个 API。
> ⚠️ 两个必须知道的点（**实测事故，详见 `host/README.md` §8**）：
> ① 插件会**回写自己的源码目录**（会改坏别人 AstrBot 的生产配置）→ 已用**源码镜像**隔离；
> ② 某个插件的 `initialize()` 会联网下载 26MB 字体 → 已加**超时隔离**（启动 181.6s → 20.8s）。

---

## 快速开始

```bash
# 1) 建虚拟环境
python3 -m venv .venv          # 需要 python3.12-venv 包
.venv/bin/pip install -e ".[dev]"

# 2) 准备配置（含令牌与上游 key，已被 .gitignore 挡住）
cp .env.example .env && chmod 600 .env
.venv/bin/python -m emotion_app token    # 生成令牌 → 填进 .env

# 3) 部署前自检（不合法会明确报错并非零退出）
./run.sh check

# 4) 起服务
./run.sh serve
# → 面板 http://127.0.0.1:6190/    健康检查 /health
```

**后台常驻**（脱离终端，会话结束也不退出）：

```bash
cd /root/Ai-works/情绪app
setsid nohup ./run.sh serve > logs/server.log 2>&1 < /dev/null &
```

打开 `http://127.0.0.1:6190/` 后会看到**登录框** —— 输入 `.env` 里的
`EMOTION_AUTH_ACCESS_TOKEN` 即可。登录后服务端下发 **HttpOnly** 会话 cookie
（默认 `EMOTION_AUTH_SESSION_TTL_HOURS=168` 小时），刷新不用重输，
令牌**不落 localStorage**。

### 命令

| 命令 | 作用 |
|---|---|
| `./run.sh serve` | 启动服务（自动加载 `.env`） |
| `./run.sh check` | 只校验配置（部署前自检） |
| `./run.sh token` | 生成一个合规令牌（32 位 hex） |

> `.env` 由 `run.sh` 注入环境变量；应用本身**只读环境变量**，不解析 `.env` 文件。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMOTION_SERVER_HOST` | `127.0.0.1` | 监听地址。**`0.0.0.0` = 对整个网络开放**，见下方安全提醒 |
| `EMOTION_SERVER_PORT` | `6190` | 监听端口 |
| `EMOTION_SERVER_HTTPS_ONLY` | `false` | 会话 cookie 是否带 `Secure`。**挂了 TLS 反代后必须改 `true`**；本机 http 下若设 true，浏览器会**直接不收 cookie**（表现为"点了登录没反应"） |
| `EMOTION_AUTH_ACCESS_TOKEN` | **无（必填）** | 访问令牌，**≥16 字符**，否则拒绝启动 |
| `EMOTION_AUTH_SESSION_TTL_HOURS` | `24` | 登录会话有效期（1–168） |
| `EMOTION_STORAGE_BACKEND` | `sqlite` | `sqlite` / `json`（json 未实现） |
| `EMOTION_STORAGE_SQLITE_PATH` | `data/emotion.db` | 数据库路径 |
| `EMOTION_UPSTREAM_BASE_URL` | `https://api.syuan.org/v1` | 上游地址 |
| `EMOTION_UPSTREAM_API_KEY` | 空 | 上游密钥 |
| `EMOTION_UPSTREAM_MODEL` | `gemini-3.1-flash-lite` | 模型 |
| `EMOTION_LOGGING_LEVEL` | `INFO` | 日志级别 |
| `EMOTION_LOGGING_JSON` | `false` | JSON 日志 |

---

## 🧩 可替换接口（重要设计原则）

> 情绪 2026-09-18：「最终还是要**以陪伴为核心**，**接口别写死了**。」

领域层**只依赖 Protocol**（`domain/ports.py`），实现可换：

| 层 | Protocol | 当前实现 | 可换成 |
|---|---|---|---|
| 对话上游 | `ChatBackend` | `OpenAIChatBackend` | 陪伴的 provider / 其他协议 |
| 生图后端 | `ImageBackend` | `OpenAIImageBackend`（OpenAI 兼容） | NAI / ComfyUI / SD WebUI / 陪伴的生图插件 |
| 工具下发 | `ToolDispatch` | `FunctionCallDispatch`（默认） | `TextTagDispatch`（退路）或陪伴的工具协议 |

**用 `typing.Protocol`（结构化类型）**：实现方**不需要继承我们的类** ——
这对"对接别人的框架"很关键（陪伴插件的类不会继承我们的基类）。

### 工具下发方式（可配置）

```bash
EMOTION_IMAGE_DISPATCH=function_call   # 默认：原生 function calling（实测可用）
EMOTION_IMAGE_DISPATCH=text_tag        # 退路：文本标签（照爱语，但更宽容）
```

⭐ **为什么默认用 function calling**：实测爱语的 `<generate_image:描述>` 标签
**容易被截断**（正则 `^<...>$` 要求独占整行 + 流式半截失效 + 失败静默）。
详见 `../爱语/docs/IMAGE_TOOL_DISPATCH.md`。

---

## 🔴 安全约定（硬要求）

| 项 | 约定 | 判据 |
|---|---|---|
| 绑定 | **默认 `127.0.0.1`**；`0.0.0.0` 是显式选择 | 启动日志出现 `exposed: true` + **WARNING 告警** |
| 令牌 | **必填且 ≥16 字符** | 否则 `check`/`serve` 都以 exit=2 退出 |
| 令牌存储 | **只存摘要**（hmac+sha256） | 实例上不留明文引用 |
| 会话 | HttpOnly + SameSite=Lax | JS 读不到 |
| **登录限流** | **10 次失败 / 5 分钟 / 来源** → **429** | 限流期间**正确令牌也拒绝**（否则形同虚设） |
| **日志** | **主动脱敏**，密钥打不出来 | 见 `infra/logging_setup.py`；有回归测试 |
| 密钥 | 只走环境变量/本地文件，**不进 git** | `.gitignore` 已挡 |

### ⚠️ 绑 `0.0.0.0` 意味着什么（2026-09-19 情绪确认要对外）

对**整个网络**开放，**任何人都能访问登录接口**。需要清楚代价：

1. **唯一屏障是那个 32 位 hex 令牌**（128 bit 熵，穷举不现实）。限流是**纵深防御，不是主要屏障**。
2. **明文 http 下会话 cookie 可被嗅探** → 公网暴露**必须**挂 TLS 反代，并把
   `EMOTION_SERVER_HTTPS_ONLY=true`。
3. **限流记录在内存里，重启即清空** —— 它挡不住"重启服务再爆破"的循环攻击，
   只能减缓单次会话内的暴力尝试。
4. 单人自用场景下窗口刻意宽松（10 次/5 分钟），因为**误锁自己比被爆破更现实**。

> 公网暴露的推荐做法：`Caddy/Nginx` 终止 TLS → `reverse_proxy 127.0.0.1:6190`，
> 这样后端仍然只听本机（把 `EMOTION_SERVER_HOST` 改回 `127.0.0.1`），
> 既对外可用又不直接暴露进程。

---

## 接口（OpenAI 兼容）

| 接口 | 说明 |
|---|---|
| `POST /v1/chat/completions` | 聊天补全（支持 `stream: true` → SSE） |
| `GET /v1/models` | 模型列表 |
| `GET /health` | 存活自检（免鉴权、脱敏） |
| `GET /api/v1/status` | 运行状态（需鉴权） |
| `POST /api/v1/auth/login` | 令牌换会话 |
| `GET/POST /api/v1/personas` | 人设列表 / 新建 |
| `POST /api/v1/personas/import` | ⭐ **导入人设**（爱语备份 / SillyTavern 卡 / PNG）；**支持一个文件多人设** |
| `GET /api/v1/personas/export` | ⭐ **导出全部**为爱语备份格式 |
| `GET /api/v1/personas/<id>/export?format=` | 导出单张（`aiyu` 爱语格式 / `sillytavern` 角色卡） |
| `GET /api/v1/personas/<id>/avatar` | 人设头像（**需鉴权**） |
| `GET/PATCH/DELETE /api/v1/personas/<id>` | 人设读 / 改 / 删 |
| `POST /api/v1/personas/<id>/activate` | 设为当前人设 |
| `POST /api/v1/personas/<id>/duplicate` | 复制角色卡 |
| `GET /api/v1/personas/<id>/preview` | ⭐ **预览组装后的 system prompt** |
| `PUT /api/v1/personas/<id>/sections` | 替换提示词片段 |
| `GET/POST /api/v1/sessions` | 会话列表 / 新建 |
| `GET/PATCH/DELETE /api/v1/sessions/<id>` | 会话读 / 改（含策略）/ 删 |
| `GET /api/v1/sessions/<id>/messages` | 会话消息（`?limit=N`） |
| `POST /api/v1/sessions/<id>/clear` | 清消息但保留会话 |
| `POST /api/v1/sessions/<id>/summarize` | 手动触发总结（正常是后台自动） |
| `GET /api/v1/images/<file>` | ⭐ 取回生成的图片（**需鉴权**，防路径穿越） |
| `GET /api/v1/context-modes` | 可选上下文模式 |
| `GET/PATCH /api/v1/settings` | 运行时业务配置 |
| `DELETE /api/v1/settings/overrides` | 恢复环境变量默认 |

🔑 **鉴权**：`/v1/*` 与 `/api/v1/*` **统一** —— 会话 cookie **或**访问令牌作为 Bearer 均可。

### 功能差距

我们 vs 陪伴插件的**结构化差距统计** → `../爱语/docs/FEATURE_GAP_ANALYSIS.md`

> ⭐ 关键结论：陪伴 ~280,000 行里 **~54% 是群聊/多用户/平台**（单人**不需要**，不计缺口）。
> 单人相关约 130,000 行 → 我们代码量完成度 ~5%，但**核心闭环 100% 可用**
> （它大量代码花在 1,719 个配置项与边界情况上）。

---

### 面板

浏览器打开 `http://127.0.0.1:6190/`（**原生 HTML/JS，零构建工具**）。

⭐ **布局：可收起的侧边栏 + hash 路由**（2026-09-20 重构，取代原来的顶栏 tab）：

* 桌面：点左上汉堡 → 侧边栏收成 **60px 图标条**（状态记在 `localStorage`，刷新保持）
* 手机：侧边栏是**抽屉**（默认滑出，点汉堡滑入 + 遮罩，点遮罩收回）
* 每页都是真 URL（`#/chat`、`#/persona/<id>`…）——**可后退、可刷新、可分享**
* 手机端人设**点开即路由到详情**（列表收起 + 出现「← 返回列表」）

| 页面 | 能做什么 |
|---|---|
| **聊天** | 流式对话（带当前人设 + **会话历史**）、切换/新建会话、**渲染生成的图片** |
| **会话** | 会话列表、**三种上下文模式**、保护窗口、摘要编辑 |
| **人设** | 建/改/删/复制角色卡、**导入（爱语备份 / ST 角色卡）**、**编辑提示词片段**、**预览组装结果** |
| **插件** | 🆕 **打开 AstrBot 插件自带的拓展页**（陪伴面板 / QQ 空间与书柜 / 一起房间），同源 iframe 内嵌 |
| **设置** | 改上游地址/模型/key（**不改环境变量**）、测试连接、看部署配置 |

⚠️ **静态资源一律 `no-cache`**（踩过的大坑）：面板是零构建的原生页面，
过去 `send_from_directory` 默认 `max-age=12 小时` → **改完前端，用户手机上还是旧的**，
表现为「移动端适配没生效 / 导入按钮没有」——**代码其实早就生效了**。
现在靠 ETag 走 304，开销可忽略。另注意 `Quart(__name__, static_folder=None)`：
Quart 内置的 `/static` 路由会**静默盖掉**我们的 no-cache 版本（实测）。

⭐ **可用任意 OpenAI 客户端直连**（已用官方 `openai` SDK 实测）：

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:6190/v1", api_key="<你的访问令牌>")
r = c.chat.completions.create(model="gemini-3.1-flash-lite",
                              messages=[{"role": "user", "content": "你好"}])
```

也可直接用 Cherry Studio 等 GUI 客户端。

---

## 测试

```bash
.venv/bin/python -m pytest -q
```

**421 项**（主项目；另有 host 的 35 项单独跑，见 `host/README.md`），
重点覆盖**失败路径**（不静默降级）：

- 无令牌 / 令牌过短 / 端口非法 / 端口非数字 / 上游 URL 非法 → **全部拒绝启动**
- 未鉴权访问 → **401**（不是 200 空数据）
- **插件源码目录不得被改动**（实测事故：插件回写自己的源码目录，会改坏 AstrBot 生产配置）
- **插件 `initialize()` 挂住必须有超时**（实测：某插件联网下载字体，把启动卡了 181.6 秒）
- 导入模式非法 / 缺字段 / 类型错 → **明确报错**
- **密钥不得出现在日志**（含回归测试）
- **诊断字段不得被过度脱敏**（实测踩过：`token_configured` 被打成 `***REDACTED***`）
- **时间片段必须排在 system 最末**（踩过：排最前会毁掉稳定前缀 → 上游缓存永不命中）
- 上游拒答话术 → **换模型重试**
- 流中途出错 → **明确告知客户端**，不静默结束
- **任意 Bearer 不能放行**（必须就是访问令牌）
- **实体 id 不得被脱敏**（踩过：32 位 hex 的 uuid 被当密钥打掉）
- 不可运行时改的字段 → **明确告知被忽略**，不静默吞掉
- **流式生成器必须被完整消费**（踩过：`break` 导致流式永远不存历史）
- **预算裁剪所有模式都生效**（踩过：滚动模式跳过预算 → 长会话打爆上游）
- **会话不存在要报错**，不静默返回空列表
- **工具协议字段必须透传**（踩过：漏 `tool_calls`/`tool_call_id` → 上游 400，闭环断掉）
- **每轮最多调一次工具**（实测上游会重复调用）
- **换后端不改领域层**（用一个"假后端"验证）
- **未知的下发方式要报错**，不静默退回默认
- **两条路径都要带图**（踩过：非流式漏附件 → 图生成了用户看不到）
- **工具执行后要二次请求**（踩过：流式只有图没正文）
- **总结失败不推进游标**（否则那段对话永远不被总结）
- **后台任务必须被强引用持有**（asyncio 只持弱引用，否则会被 GC）
- **装配测试**：能力必须真的被 `__main__` 接上（踩过：模块+单测全绿但忘记绑线）
- **登录失败限流**：超限返 **429**，且限流期间**正确令牌也拒绝**（只挡错令牌 = 形同虚设）
- **限流按来源隔离**：一个 IP 被限不得影响其它 IP（否则一个人能锁死所有人）
- **登录成功清空失败计数**（否则偶尔打错会累积到被锁）· **窗口过期自动恢复**（避免永久封禁自己）
- **`https_only` 真的作用到 cookie 的 `Secure` 标志**（设错 → 浏览器不收 cookie → "点登录没反应"）

### 真浏览器验证（`curl` 验不了 JS）

```bash
TOKEN=$(grep '^EMOTION_AUTH_ACCESS_TOKEN=' .env | cut -d= -f2- | tr -d "'")

# 生图是否真的渲染进气泡（含正文）
.venv/bin/python tools/browser_check.py http://127.0.0.1:6190/ "$TOKEN" "画一只戴墨镜的柯基"

# 登录闸门 5 项判据
.venv/bin/python tools/login_check.py http://127.0.0.1:6190/ "$TOKEN"

# 手机端可用性（真手机视口 @3x）
.venv/bin/python tools/mobile_check.py http://127.0.0.1:6190/ "$TOKEN"
```

| 判据 | 内容 |
|---|---|
| `browser_check.py` | 图片节点**存在且已加载**（`naturalWidth > 0`）**且**有正文 |
| `login_check.py` | ①未登录登录框可见 ②错令牌停在框内并提示 ③对令牌进入 ④**刷新免重输** ⑤令牌不落浏览器存储 ⑥**登录态迟到生效时自动解锁** |
| `mobile_check.py` | ①**无横向溢出** ②触控目标 ≥40px ③人设页**单列堆叠** ④片段表**转卡片且有内容** ⑤输入框 ≥16px（防 iOS 自动放大） |

> 🔴 **`login_check.py` 的第 4 项抓到了一个真 bug**：浮层在 HTML 里默认可见
> （fail-closed 设计），但"刷新时 cookie 仍有效"这条路径**不经过表单提交** →
> 没人调 `hideLogin()` → 数据其实已加载，用户却被永久挡在登录框上。
> **这是纯前端路径问题，`curl` 和单测都发现不了。**
>
> 🔴 **第 6 项（2026-09-20 新增）抓到了同类的第二个入口**：页面加载时没登录 →
> 之后 cookie 才生效（别的标签页登录 / SSO）→ 切回本页。原来**永远卡在登录框**
> （数据接口其实已经通了）。修的时候我还踩了一个"接线"坑：把
> `visibilitychange` 监听写进了 `boot()`，而**没登录时根本不会调 `boot()`**
> → 监听器从没装上、修复形同虚设（已按 `test_assembly.py` 的思路改成模块作用域注册）。

---

## 目录结构

```
emotion_app/
├── infra/          配置 · 日志（含脱敏）
├── storage/        存储抽象 + SQLite 实现 + 工厂
├── web/            Quart 应用 · 鉴权 · 管理 API · 面板(static/)
│                   plugin_host(装配) · plugin_routes(插件 API) · plugin_pages_web(插件页)
├── domain/         Persona · Message · 提示词组装 · 设置
│                   Context(三模式) · Session(会话仓库)
│                   ports(可替换接口) · backends(实现) · tools · image_*
└── __main__.py     入口（serve / check / token）

host/               AstrBot 兼容宿主（并列交付物；emotion_app **可选**依赖它）
├── astrbot_host/   shadow(影子包) · compat(兼容层) · loader(加载器)
│                   host_app(装配) · plugin_pages(拓展页) · source_mirror · write_guard
└── tests/          35 项单测 + 3 个差分验证工具
```

**硬约束**（防止重蹈陪伴插件 `page_api.py` 31,646 行的覆辙）：
单文件 **≤1,500 行**、配置项 **<60**、Web 层**只做协议转换**。

---

## 设计文档

| 文档 | 位置 |
|---|---|
| 🎯 **下一步规划（先看这份）** | **`docs/NEXT_STEPS.md`** |
| **宿主（AstrBot 兼容层）** | `host/README.md` |
| **框架设计（主文档）** | `../爱语/docs/BACKEND_FRAMEWORK.md` |
| 人设与提示词体系（实测） | `../爱语/docs/PERSONA_PROMPT_SYSTEM.md` |
| 架构可行性 | `../爱语/docs/ARCHITECTURE_THIN_CLIENT.md` |
| 陪伴插件技术栈参照 | `../爱语/docs/REF_ASTRBOT_COMPANION_STACK.md` |
| 功能差距分析（对陪伴） | `../爱语/docs/FEATURE_GAP_ANALYSIS.md` |

### 关键决策记录

| 决策 | 内容 | 日期 |
|---|---|---|
| 技术栈 | Python + Quart/Hypercorn + 原生 HTML/JS + 可插拔存储 | 09-18 |
| 存储 | **SQLite**（情绪拍板） | 09-18 |
| 形态 | 后端在电脑，**手机只是 WebView 套壳** | 09-18 |
| 与陪伴插件 | 🔴 **暂不集成，留接口**（作者在做新框架） | 09-18 |
| 第一版范围 | **聊天 + 人设** | 09-18 |
| 第一版记忆 | **只要会话内上下文** | 09-18 |
| 上下文模式 | **照爱语三模式**（滚动/动态/混合） | 09-18 |
| 多用户 | ❌ 不做（一人用，但**角色卡可以有多张**） | 09-18 |
| 生图 | OpenAI 兼容 `/images/generations` | 09-18 |

---

## 上游

实测：`api.syuan.org`（备选 `ai-api.syuan.org`；`aiapi.syuan.org` 是 404）是
**new-api 系、完全兼容 OpenAI、无需适配层**。
