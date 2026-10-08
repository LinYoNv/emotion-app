# 情绪app

> 自用的「虚拟聊天伙伴」**后端**。**形态参照爱语，机制参照陪伴插件**。
> 后端跑在自己的电脑/服务器上，**手机是前端**（当前形态：爱语 App 改配置直连，见 `docs/APP_PLAN.md`）。

| | |
|---|---|
| 技术栈 | Python 3.12 + **Quart/Hypercorn** + SQLite + **零构建原生 HTML/JS 面板** |
| 端口 | 本地 **6191**（外部 NAT `31818 → 6191`） |
| 判据 | **1142 passed, 1 deselected** |
| 接口 | **67 条路由** → 详见 **`docs/API.md`** |
| 下一步 | 手机 App 接入 → **`docs/APP_PLAN.md`** |

---

## 这是什么

一个**宿主**：让 `astrbot_plugin_private_companion`（「我会永远陪着你」陪伴插件）
**以为自己在 AstrBot 上跑**。

> 🎯 **路线已定，不再重新论证**：
> 「**类似 AstrBot 的底子做后端 + 爱语 UI 做前端 App**」。
> **不搬也不剥，造宿主管兼容** —— **不改插件源码**（插件升级了也不用改我们）。
>
> 权威定位：`../爱语/docs/PLATFORM_POSITIONING.md`

**四条已打通的链路**：

| 链路 | 说明 |
|---|---|
| 命令派发 | 用户在面板说话 → 派发给插件 |
| LLM 钩子 | 插件在发给模型前改提示词（记忆/人设注入靠它） |
| 模型端口 | 插件通过我们调上游 LLM |
| 投递端口 | 插件的**主动消息**落到我们的会话里 |

---

## 当前状态

| 能力 | 状态 |
|---|---|
| 后端主干（配置/存储/鉴权/日志） | ✅ |
| `/v1/*` OpenAI 兼容（含 SSE 流式） | ✅ |
| 人设 CRUD + 片段 + 导入导出 + 参考图 | ✅ |
| 会话 + 三种上下文模式 + 多轮记忆 + 总结自动化 | ✅ |
| **面板**（零构建原生 HTML/JS） | ✅ |
| **AstrBot 插件宿主**（装/跑/挂） | ✅ 已加载 **9** 个插件 · 挂载 **268** 个 API |
| **插件面板 + 详情页 + 拓展页** | ✅ |
| **主动消息**（插件发、我们投递） | ✅ 已开（克制档 3 条/天） |
| **未读徽标** | ✅（只有插件主动消息 +1） |
| **每会话唯一 umo** | ✅ `sess-<12位十六进制>` |
| 生图（含参考图） | ✅ |
| 手机 App 接入 | 📋 **计划已定**（`docs/APP_PLAN.md`），未开始 |
| 真实语音（TTS/ASR） | ⏳ 只有配置层 |

---

## 快速开始

```bash
# 1) 建虚拟环境
python3 -m venv .venv                # 需要 python3.12-venv 包
.venv/bin/pip install -e ".[dev]"    # 4 个运行时依赖，很轻

# 2) 准备配置（含令牌与上游 key，已被 .gitignore 挡住）
cp .env.example .env && chmod 600 .env
.venv/bin/python -m emotion_app token     # 生成令牌 → 填进 .env

# 3) 部署前自检（不合法会明确报错并非零退出）
./run.sh check

# 4) 起服务
./run.sh serve
# → 面板 http://127.0.0.1:6191/     健康检查 /health
```

**后台常驻**（脱离终端）：
```bash
setsid nohup ./run.sh serve >> logs/serve.out 2>&1 < /dev/null &
```
⚠️ **用 `&` 起服务后，别在同一个 shell 里跑后续命令**（cwd 会失效）。

**停机**：
```bash
ss -ltnp | grep 6191        # 取 pid
kill <pid>
```
🔴 **绝不要 `pkill -f "emotion_app serve"`** —— 它会匹配到**发起命令的那行 shell**，
连自己的会话一起杀（本项目踩过 5 次的头号惯犯）。

---

## 🔴 一个必须知道的配置陷阱

**模型名有两处来源，运行时的会覆盖 `.env` 的**：

```bash
./run.sh check   # 显示 gemini-3.1-flash-lite ← 这是【.env 启动值】
```

但**实际生效的**是运行时设置（面板里改过的）：
```bash
TOKEN=$(grep -m1 '^EMOTION_AUTH_ACCESS_TOKEN' .env | cut -d= -f2- | tr -d '"')
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:6191/v1/models
# → {"id": "gemini-3.8-flash-high"}   ← 这个才是真正用的
```
⇒ **判断"现在用哪个模型"要看 `/v1/models` 或 `settings.upstream.model`，别抄 `.env`。**
（本项目曾因"回显启动快照而非运行时值"产生过一个 bug。）

---

## 🧩 可替换接口（重要设计原则）

> 情绪 2026-09-18：「最终还是要**以陪伴为核心**，**接口别写死了**。」

领域层**只依赖 Protocol**（`domain/ports.py`），实现可换：

| 层 | Protocol | 当前实现 | 可换成 |
|---|---|---|---|
| 对话上游 | `ChatBackend` | `OpenAIChatBackend` | 陪伴的 provider / 其他协议 |
| 生图后端 | `ImageBackend` | `OpenAIImageBackend`（OpenAI 兼容） | NAI / ComfyUI / SD WebUI |
| 工具下发 | `ToolDispatch` | `FunctionCallDispatch`（默认） | `TextTagDispatch`（退路） |

**用 `typing.Protocol`（结构化类型）**：实现方**不需要继承我们的类** ——
这对"对接别人的框架"很关键（插件不会继承我们的基类）。

### 工具下发方式（可配置）

```bash
EMOTION_IMAGE_DISPATCH=function_call   # 默认：原生 function calling（实测可用）
EMOTION_IMAGE_DISPATCH=text_tag        # 退路：文本标签（照爱语，但更宽容）
```
⭐ **为什么默认 function calling**：实测爱语的 `<generate_image:描述>` 标签
**容易被截断**（正则要求独占整行 + 流式半截失效 + **失败静默**）。
详见 `../爱语/docs/IMAGE_TOOL_DISPATCH.md`。

---

## 🔴 安全约定（硬要求）

| 项 | 约定 | 判据 |
|---|---|---|
| 绑定 | **默认 `127.0.0.1`**；`0.0.0.0` 是显式选择 | 启动日志 `exposed: true` + **WARNING** |
| 令牌 | **必填且 ≥16 字符** | 否则 `check`/`serve` 都 exit=2 |
| 令牌存储 | **只存摘要**（hmac+sha256） | 实例上不留明文 |
| 会话 | HttpOnly + SameSite=Lax | JS 读不到 |
| **登录限流** | **10 次失败 / 5 分钟** → **429** | 限流期间**正确令牌也拒绝**（否则形同虚设） |
| **日志** | **主动脱敏** | `infra/logging_setup.py`，有回归测试 |
| 密钥 | 只走环境变量/本地文件 | `.gitignore` 已挡 |

### ⚠️ 绑 `0.0.0.0` 意味着什么

对**整个网络**开放。需要清楚代价：

1. **唯一屏障是那个 128 bit 令牌**（穷举不现实）。限流是**纵深防御，不是主要屏障**。
2. **明文 http 下 cookie 可被嗅探** ⇒ 公网暴露**必须**挂 TLS 反代 +
   `EMOTION_SERVER_HTTPS_ONLY=true`。
3. **限流记录在内存里，重启即清空** —— 挡不住"重启再爆破"的循环。
4. 单人自用场景刻意宽松（10 次/5 分钟），因为**误锁自己比被爆破更现实**。

> 推荐做法：`Caddy/Nginx` 终止 TLS → `reverse_proxy 127.0.0.1:6191`，
> 把 `EMOTION_SERVER_HOST` 改回 `127.0.0.1` —— 既对外可用又不直接暴露进程。

---

## 接口

> **完整接口文档（67 条路由 + 字段级说明 + 客户端注意事项）→ `docs/API.md`**

最常用的四个：

| 接口 | 说明 |
|---|---|
| `POST /v1/chat/completions` | **聊天**（支持 `stream: true` → SSE） |
| `GET /v1/models` | 模型列表（**回的是运行时生效的模型**） |
| `GET /health` | 存活自检（**免鉴权**、脱敏） |
| `GET /api/v1/status` | 运行状态 ⚠️ **只认 cookie，用令牌会 401** |

**鉴权**：`Authorization: Bearer <EMOTION_AUTH_ACCESS_TOKEN>`
（`/v1/*` 与 `/api/v1/*` 都接受令牌；**唯一例外是 `/api/v1/status`**）。

⚠️ 流式请求出错时**先返 200 再在流里发 error 帧** ⇒ 客户端必须检查流内 `error` 字段。

---

## 测试

```bash
.venv/bin/python -m pytest tests host/tests -q -p no:cacheprovider \
  --deselect tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live
# → 1142 passed, 1 deselected
```
⚠️ 那条 `--deselect` 是**项目惯例**（它要真连上游，不适合放进回归）。

### 判据文化（**本项目最特别的地方**）

> 🔴 **单测全绿 + 接口全绿 + 页面能打开 —— 全都不算证据。**

1. **每条判据必须能"真的失败"** —— 写完要做**注入验证**（故意改坏源码，确认判据会红）
   现成工具：`tools/inject_*_check.py`（含落盘基线 + 自动还原）
2. **判据只有"验过"和"失败"两种，没有"跳过"**
3. **清单类能力用「集合比较」不是「数量比较」**
   （踩过：详情页该列 113 项，`113 == 113` 看着自洽，其实**少 1 个 handler、被 1 个 page 补平**）
4. **判据输入要自造或显式钉住**，别依赖用户可改的持久设置
5. **断言失败先查清"哪个值才是对的"** —— 本项目**两次**是断言错、代码对

### 重点覆盖的失败路径（部分）

- 无令牌 / 令牌过短 / 端口非法 / URL 非法 → **全部拒绝启动**
- 未鉴权访问 → **401**（不是 200 空数据）
- **插件源码目录不得被改动**（实测事故：插件回写自己的源码目录）
- **插件 `initialize()` 挂住必须有超时**（实测：某插件联网下载字体，启动卡 181.6 秒）
- **密钥不得出现在日志** + **诊断字段不得被过度脱敏**（`token_configured` 被打成 `***`）
- **流式生成器必须被完整消费**（踩过：`break` 导致流式永远不存历史）
- **流中途出错要明确告知客户端**，不静默结束
- **任意 Bearer 不能放行**（必须就是访问令牌）
- **装配测试**：能力必须真的被 `__main__` 接上（踩过：模块+单测全绿但忘绑线）
- **投递失败必须留日志**（踩过：删掉日志后 11 条判据全绿，而用户只看到"伴侣不找我"）

### 真浏览器验证（`curl` 验不了 JS）

```bash
TOKEN=$(grep '^EMOTION_AUTH_ACCESS_TOKEN=' .env | cut -d= -f2- | tr -d "'")

.venv/bin/python tools/mobile_sidebar_check.py http://127.0.0.1:6191/ "$TOKEN"  # 手机侧边栏
.venv/bin/python tools/session_umo_hint_check.py …                            # umo 提示
.venv/bin/python tools/plugin_detail_check.py …                               # 插件详情页
.venv/bin/python tools/mobile_check.py …                                      # 无横向溢出
.venv/bin/python tools/login_check.py …                                       # 登录闸门 6 项
```
（都是真 headless Chrome + CDP **真点击**，`.click()` 会掩盖遮挡类 bug。）

---

## 目录结构

```
情绪app/
├── run.sh                      # 入口（加载 .env 后跑 python -m emotion_app）
├── .env / .env.example         # 配置（.env 不进 git）
├── pyproject.toml              # 依赖（quart/hypercorn/aiosqlite/httpx）
├── emotion_app/                # ★ 主代码
│   ├── domain/                 #   业务逻辑
│   │   ├── chat.py             #   聊天主链（含 resolve_target 模型解析）
│   │   ├── conversation_manager.py  # 插件"对话历史"端口实现
│   │   ├── settings.py         #   运行时设置（覆盖 .env）
│   │   └── persona*.py         #   人设
│   ├── web/                    #   HTTP 层
│   │   ├── app.py              #   路由 + 鉴权闸门 + _effective_model()
│   │   ├── delivery.py         #   ★ 插件主动消息的投递端口
│   │   ├── plugin_host.py      #   ★ 插件宿主装配
│   │   └── static/             #   面板前端（index.html / app.js / app.css）
│   ├── infra/                  #   配置、供应商预设、日志
│   └── storage/                #   SQLite 存储
├── host/astrbot_host/          # ★ AstrBot 兼容层
│   ├── compat/                 #   影子包：注册成顶层 `astrbot`
│   └── platform_manager.py     #   ★ send_by_session（插件主动消息出口）
├── data/                       # 🔴 真实数据（不进 git）
│   ├── emotion.db(+wal/shm)    #   聊天记录/会话/人设
│   ├── plugins/                #   插件运行时数据
│   ├── plugin_src/             #   插件源码镜像（92M，自包含）
│   └── images/                 #   生成的图
├── docs/                       # 文档（API.md / APP_PLAN.md / …）
├── tests/ + host/tests/        # 判据（1142 条）
└── tools/                      # 诊断/验证脚本（含注入验证）
```

---

## 插件宿主

`.env` 设 `EMOTION_PLUGINS_ENABLED=true` 启用。

⚠️ **两个必须知道的点**（实测事故，详见 `host/README.md` §8）：

1. 插件会**回写自己的源码目录**（会改坏别人 AstrBot 的生产配置）
   → 已用**源码镜像**隔离（`EMOTION_PLUGINS_MIRROR_SOURCES=true`）
2. 某插件 `initialize()` 会联网下载 26MB 字体 → 已加**超时隔离**（启动 181.6s → 20.8s）

**插件目录**：`EMOTION_PLUGINS_PLUGIN_DIRS` 默认指向 `/opt/astrbot/data/plugins`。
⚠️ **换设备时**若没有 AstrBot，改指向 `data/plugin_src`（已镜像好的自包含副本）。

**停用/启用插件**：面板可改，落点是 `data/config_profile.json` 的 `plugins.inactivated_plugins`。
⚠️ **保存即落盘，但需重启服务才生效。**

---

## 设计文档

| 文档 | 内容 |
|---|---|
| **`docs/API.md`** | **接口文档**（67 条路由 + 字段 + 注意事项） |
| **`docs/APP_PLAN.md`** | **手机 App 接入计划**（下一步任务） |
| `docs/GAP_TO_GOAL.md` | 离最终目标还差什么 |
| `docs/NEXT_STEPS.md` | 规划 |
| `host/README.md` | 兼容层细节 |
| `docs/WIKI_REVIEW_结论.md` | 陪伴插件 wiki 审读结论 |

---

## 上游

OpenAI 兼容即可。当前实测配置见 `.env`（`api.syuan.org`）。
⚠️ **key 按渠道组隔离**：用 A 组 key 调 B 组模型会 **503**（`No available channel`），100% 确定性。

---

## 已知问题

| # | 项 | 状态 |
|---|---|---|
| 1 | 服务曾**"无声消失"约 2 小时**（6191 未监听、无 traceback、磁盘正常） | ⚠️ **死因未查明**，留意 |
| 2 | `tts` 宿主能力未接 | 启动自检会报，**设计内**的如实降级 |
| 3 | 三个功能缺口（生图到 UI / `/sticker` / TTS-ASR） | 见 `docs/APP_PLAN.md` §4 |
| 4 | 未提交 git 的改动较多 | 作者要求"先本地验证、不急着提交" |
